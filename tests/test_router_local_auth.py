"""Credential isolation and recovery for the local Router listener."""

from __future__ import annotations

import json
import os
import stat
import tomllib
from concurrent.futures import ThreadPoolExecutor

import click
import pytest

from ramp_cli.commands import router, router_repo
from ramp_cli.commands import router_local_auth as auth


@pytest.fixture(autouse=True)
def local_listener(monkeypatch, tmp_path, isolated_config):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(router_repo, "enabled", lambda: True)
    monkeypatch.setattr(router_repo, "ensure_running", lambda: True)


def test_token_is_private_stable_and_bound_to_key_and_upstream():
    url, token = router_repo.client_credentials("https://api.router.com/v1", "real-key")
    assert url == "http://127.0.0.1:47317/u/api.router.com/v1"
    assert token.startswith(auth.TOKEN_PREFIX)
    assert "real-key" not in token
    assert router_repo.client_credentials("https://api.router.com/v1", "real-key") == (
        url,
        token,
    )
    assert (
        router_repo.client_credentials("https://api.router.com/v1", "other-key")[1]
        != token
    )
    assert (
        router_repo.client_credentials(
            "https://internal-api.router.com/v1", "real-key"
        )[1]
        != token
    )
    assert router_repo.upstream_api_key(token, url) == "real-key"
    with pytest.raises(click.ClickException):
        router_repo.upstream_api_key(token, "https://internal-api.router.com/v1")
    path = router_repo.local_token_path(token)
    assert path.read_text() == token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.with_suffix(".json").stat().st_mode) == 0o600


def test_failed_credential_write_keeps_real_key_off_loopback(monkeypatch):
    monkeypatch.setattr(
        auth, "register", lambda *a: (_ for _ in ()).throw(OSError("disk full"))
    )
    assert router_repo.client_credentials("https://api.router.com/v1", "real-key") == (
        "https://api.router.com/v1",
        "real-key",
    )


@pytest.mark.parametrize(
    "record", [[], None, {"upstream": "api.router.com", "api_key": ""}]
)
def test_malformed_credential_record_fails_closed(record):
    endpoint, token = router_repo.client_credentials(
        "https://api.router.com/v1", "real-key"
    )
    router_repo.local_token_path(token).with_suffix(".json").write_text(
        json.dumps(record)
    )
    with pytest.raises(
        click.ClickException, match="Could not read the Router credential"
    ):
        router_repo.upstream_api_key(token, endpoint)


@pytest.mark.parametrize("contents", ["", "partial", "g" * 64])
def test_malformed_private_health_token_is_repaired(contents):
    router_repo.health_token(create=True)
    path = router_repo.config_dir() / "router-agent-health-token"
    path.write_text(contents)
    assert router_repo.health_token() is None
    token = router_repo.health_token(create=True)
    assert token is not None and len(token) == 64
    assert router_repo.health_token() == token


def test_interrupted_health_publication_can_retry(monkeypatch):
    original = os.replace
    monkeypatch.setattr(
        auth.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("interrupted"))
    )
    assert router_repo.health_token(create=True) is None
    path = router_repo.config_dir() / "router-agent-health-token"
    assert not path.exists()
    monkeypatch.setattr(auth.os, "replace", original)
    assert router_repo.health_token(create=True)
    assert not list(path.parent.glob(f".{path.name}.*"))


def test_concurrent_health_creation_returns_one_token():
    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(
            pool.map(lambda _: router_repo.health_token(create=True), range(16))
        )
    assert None not in tokens
    assert len(set(tokens)) == 1


def test_health_repair_does_not_follow_symlinks_or_replace_public_files(tmp_path):
    router_repo.health_token(create=True)
    path = router_repo.config_dir() / "router-agent-health-token"
    path.chmod(0o644)
    assert router_repo.health_token(create=True) is None
    path.unlink()
    target = tmp_path / "target"
    target.write_text("do not change")
    path.symlink_to(target)
    assert router_repo.health_token(create=True) is None
    assert target.read_text() == "do not change"


def test_codex_uses_local_token_but_retains_real_key_for_cost_hook(tmp_path):
    path = tmp_path / "ramp-router-key"
    path.write_text("real-key")
    config = tomllib.loads(
        router._codex_provider(
            path, "https://api.router.com/v1", repository_attribution=True
        )
    )
    provider = config["model_providers"]["ramp-router"]
    assert router_repo.is_wrapped(provider["base_url"])
    assert str(path) not in str(provider["auth"])
    assert "real-key" not in str(provider)
    assert path.read_text() == "real-key"
    assert str(path) in router._codex_cost_hook_command(tmp_path)


def test_claude_separates_local_and_usage_keys_and_restores_direct_setup(
    monkeypatch, tmp_path
):
    path = tmp_path / "claude" / "settings.json"
    monkeypatch.setattr(
        router, "_require_truthful_claude_code_model_names", lambda: None
    )
    monkeypatch.setattr(
        router, "_claude_code_default_model", lambda *a, **kw: "claude-router-a"
    )
    monkeypatch.setattr(router, "_install_claude_code_statusline", lambda *a: None)
    monkeypatch.setattr(router, "session_sync_hook_command", lambda *a: None)
    for enabled in [True, False]:
        monkeypatch.setattr(router_repo, "enabled", lambda: enabled)
        router._configure_claude_code(
            path, "real-key", [], base_url="https://api.router.com/v1"
        )
        env = json.loads(path.read_text())["env"]
        assert env["ANTHROPIC_AUTH_TOKEN"].startswith(auth.TOKEN_PREFIX) is enabled
        assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:") is enabled
        assert env["ROUTER_API_KEY"] == "real-key"
        assert router._stored_router_api_key("claude-code", path) == "real-key"


@pytest.mark.parametrize("client", ["opencode", "pi"])
def test_plugin_setup_and_disable_keep_endpoint_credentials_paired(
    monkeypatch, tmp_path, client
):
    path = (
        tmp_path
        / client
        / ("opencode.json" if client == "opencode" else "settings.json")
    )
    monkeypatch.setattr(router, "_opencode_major_version", lambda: 1)
    monkeypatch.setattr(
        router, "_opencode_tui_config_path", lambda: path.parent / "tui.json"
    )
    for enabled in [True, False]:
        monkeypatch.setattr(router_repo, "enabled", lambda: enabled)
        router._configure_plugin_client(
            client, path, "real-key", "model", "https://api.router.com/v1"
        )
        assert router._stored_router_api_key(client, path) == "real-key"
        if client == "opencode":
            options = json.loads(path.read_text())["plugin"][-1][1]
            endpoint, key = options["baseURL"], options["apiKey"]
        else:
            options = json.loads(
                (path.parent / router.PI_PLUGIN_CONFIG_FILE).read_text()
            )
            endpoint = options["baseUrl"]
            key = json.loads((path.parent / "auth.json").read_text())[
                router.ROUTER_PROVIDER
            ]["key"]
        assert router_repo.is_wrapped(endpoint) is enabled
        assert key.startswith(auth.TOKEN_PREFIX) is enabled
        if enabled:
            assert options["usageApiKey"] == "real-key"
        else:
            assert key == "real-key"
            assert "usageApiKey" not in options
