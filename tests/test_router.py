"""Tests for Ramp Router coding-agent configuration."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import tomllib

import click
import httpx
import pytest
import zstandard
from click.testing import CliRunner

import ramp_cli.commands.router as router_module
from ramp_cli import claude_cowork
from ramp_cli.commands import claude_code
from ramp_cli.commands.router import DEFAULT_ROUTER_BASE_URL as ROUTER_BASE_URL
from ramp_cli.main import cli


@pytest.fixture(autouse=True)
def _complete_browser_key_setup(monkeypatch):
    """Keep configuration tests focused beyond host-only setup boundaries."""
    monkeypatch.setattr(
        router_module,
        "acquire_router_api_key",
        lambda _url, *, no_browser: "router-secret",
    )
    monkeypatch.setattr(claude_cowork, "preflight", lambda: None)


def _router_metadata(identifier, **overrides):
    """The description Router publishes for every model it serves."""
    metadata = {
        "schema_version": 1,
        "request_name": identifier,
        "display_name": identifier,
        "description": "",
        "listing": {"order": 0},
        "limits": {"context_window": 128000, "max_output_tokens": 16384},
        "capabilities": {
            "modalities": {"input": ["text", "image"]},
            "tools": {"supported": True},
            "reasoning": {"efforts": [], "default_effort": ""},
        },
    }
    metadata.update(overrides)
    return metadata


def _mock_models(
    monkeypatch, models=None, key="router-secret", base_url=ROUTER_BASE_URL
):
    models = models or [{"id": "gpt-5.4"}]
    models = [
        {**m, "router": m.get("router", _router_metadata(m["id"]))} for m in models
    ]

    def get(url, *, headers, timeout):
        if url.endswith("/claude-code-statusline"):
            # The status line asset has its own tests in test_claude_code.py;
            # here it is simply unavailable, so configure skips it.
            return httpx.Response(404, request=httpx.Request("GET", url))
        assert url == f"{base_url}/models"
        assert headers["Authorization"] == f"Bearer {key}"
        assert set(headers) <= {
            "Authorization",
            "X-Gateway-Client",
            "X-Gateway-Model-View",
        }
        if "X-Gateway-Client" in headers:
            assert headers["X-Gateway-Client"] in {"claude-code", "codex"}
        if "X-Gateway-Model-View" in headers:
            assert headers["X-Gateway-Model-View"] == "all"
        assert timeout == 10
        if headers.get("X-Gateway-Client") == "codex":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "slug": model["id"],
                            "display_name": model["router"]["display_name"],
                            "base_instructions": "",
                        }
                        for model in models
                    ]
                },
                request=httpx.Request("GET", url),
            )
        return httpx.Response(
            200, json={"data": models}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr("ramp_cli.commands.router.httpx.get", get)


def _mock_codex_catalog(monkeypatch, slugs=("gpt-5.4",)):
    catalog = {
        "models": [
            {"slug": slug, "base_instructions": f"NATIVE PROMPT FOR {slug}"}
            for slug in slugs
        ]
    }
    monkeypatch.setattr(
        router_module.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"codex", "python3"} else None,
    )
    monkeypatch.setattr(
        router_module.subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args, 0, json.dumps(catalog), ""
        ),
    )
    return catalog


def _router_model(identifier, *, request_name=None, display_name=None):
    metadata = _router_metadata(
        identifier,
        request_name=request_name or identifier,
        display_name=display_name or identifier,
    )
    return router_module.RouterModel(
        id=identifier,
        metadata=router_module._model_metadata(metadata, identifier),
    )


def test_configure_cowork_consumes_the_setup_file_and_deletes_it_after_success(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(router_module.CONFIGURE_KEY_ENV, "ambient-secret")
    setup_file = tmp_path / "ramp-router-setup-abc123.json"
    setup_file.write_text(
        json.dumps(
            {
                "version": 1,
                "name": "Ramp Router",
                "base_url": "https://router.example/v1",
                "api_key": "router-secret",
            }
        )
    )
    captured = {}

    def fetch_models(api_key, **kwargs):
        captured["fetch"] = (api_key, kwargs)
        return [
            _router_model(
                "claude-router-5-6-sol-abc123",
                request_name="gpt-5.6-sol",
                display_name="5.6 Sol",
            )
        ]

    def configure(api_key, base_url):
        captured["configure"] = (api_key, base_url)
        return tmp_path / "Claude-3p" / "configLibrary" / "profile.json"

    monkeypatch.setattr(router_module, "_fetch_models", fetch_models)
    monkeypatch.setattr(claude_cowork, "configure", configure)

    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "configure-cowork",
            "--setup-file",
            str(setup_file),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["fetch"] == (
        "router-secret",
        {
            "gateway_client": "claude-cowork",
            "base_url": "https://router.example/v1",
            "wait_for_key": True,
        },
    )
    assert captured["configure"] == (
        "router-secret",
        "https://router.example/v1",
    )
    assert not setup_file.exists()
    assert "router-secret" not in result.output
    assert "ambient-secret" not in result.output
    assert "Connected to: Claude Cowork" in result.output
    assert "1 model discovered" in result.output
    assert "5.6 Sol" in result.output


def test_configure_cowork_rejects_an_explicit_key_with_a_setup_file(
    tmp_path, monkeypatch
):
    setup_file = tmp_path / "ramp-router-setup-abc123.json"
    setup_file.write_text(
        json.dumps(
            {
                "version": 1,
                "name": "Ramp Router",
                "base_url": "https://router.example/v1",
                "api_key": "router-secret",
            }
        )
    )
    monkeypatch.setattr(
        router_module,
        "_fetch_models",
        lambda *_args, **_kwargs: pytest.fail("discovery should not run"),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "configure-cowork",
            "--setup-file",
            str(setup_file),
            "--api-key",
            "explicit-secret",
        ],
    )

    assert result.exit_code != 0
    assert "cannot be combined" in result.output
    assert setup_file.exists()
    assert "explicit-secret" not in result.output


def test_configure_codex_consumes_the_setup_file_and_pins_the_live_catalog(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv(router_module.CONFIGURE_KEY_ENV, "ambient-secret")
    monkeypatch.setattr(router_module.shutil, "which", lambda _name: None)
    setup_file = tmp_path / "ramp-router-setup-codex123.json"
    setup_file.write_text(
        json.dumps(
            {
                "version": 1,
                "name": "Ramp Router",
                "base_url": "https://router.example/v1",
                "api_key": "router-secret",
            }
        )
    )
    _mock_models(
        monkeypatch,
        [{"id": "router-model-a"}, {"id": "router-model-b"}],
        base_url="https://router.example/v1",
    )

    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "router",
            "configure",
            "codex",
            "--setup-file",
            str(setup_file),
        ],
    )

    assert result.exit_code == 0, result.output
    assert not setup_file.exists()
    assert "router-secret" not in result.output
    assert "ambient-secret" not in result.output
    config = tomllib.loads((codex_home / "config.toml").read_text())
    assert config["model_providers"]["ramp-router"]["base_url"] == (
        "https://router.example/v1"
    )
    catalog_path = codex_home / router_module.CODEX_ROUTER_CATALOG
    assert config["model_catalog_json"] == str(catalog_path)
    assert [
        model["slug"] for model in json.loads(catalog_path.read_text())["models"]
    ] == ["router-model-a", "router-model-b"]
    payload = json.loads(result.output)["data"][0]
    assert payload["setup_file_deleted"] is True


def test_configure_codex_keeps_the_setup_file_when_configuration_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    setup_file = tmp_path / "ramp-router-setup-codex123.json"
    setup_file.write_text(
        json.dumps(
            {
                "version": 1,
                "name": "Ramp Router",
                "base_url": "https://router.example/v1",
                "api_key": "router-secret",
            }
        )
    )
    monkeypatch.setattr(
        router_module,
        "_fetch_models",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            click.ClickException("catalog unavailable")
        ),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "codex",
            "--setup-file",
            str(setup_file),
        ],
    )

    assert result.exit_code != 0
    assert "catalog unavailable" in result.output
    assert setup_file.exists()
    assert "router-secret" not in result.output


@pytest.mark.parametrize("source", ["flag", "environment"])
def test_configure_cowork_reports_empty_api_key_as_a_bad_parameter(monkeypatch, source):
    args = ["--human", "router", "configure-cowork"]
    if source == "flag":
        args += ["--api-key", ""]
    else:
        monkeypatch.setenv(router_module.CONFIGURE_KEY_ENV, " ")

    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 2
    assert "Invalid value for '--api-key': cannot be empty" in result.output


def test_configure_cowork_names_the_model_that_was_actually_discovered(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        router_module,
        "_fetch_models",
        lambda *_args, **_kwargs: [
            _router_model(
                "claude-router-model-abc123",
                request_name="other-model",
                display_name="Other Model",
            )
        ],
    )
    monkeypatch.setattr(
        claude_cowork,
        "configure",
        lambda *_args, **_kwargs: tmp_path / "profile.json",
    )

    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "configure-cowork",
            "--api-key",
            "router-secret",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Pick Other Model in Cowork" in result.output


def test_configure_cowork_agent_output_recommends_without_claiming_a_default(
    tmp_path, monkeypatch
):
    model = _router_model(
        "claude-router-model-abc123",
        request_name="other-model",
        display_name="Other Model",
    )
    monkeypatch.setattr(
        router_module, "_fetch_models", lambda *_args, **_kwargs: [model]
    )
    monkeypatch.setattr(
        claude_cowork,
        "configure",
        lambda *_args, **_kwargs: tmp_path / "profile.json",
    )

    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "router",
            "configure-cowork",
            "--api-key",
            "router-secret",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"][0]
    assert payload["recommended_model"] == model.id
    assert "default_model" not in payload


def test_configure_cowork_quiet_mode_does_not_start_a_spinner(tmp_path, monkeypatch):
    monkeypatch.setattr(
        router_module,
        "_fetch_models",
        lambda *_args, **_kwargs: [_router_model("claude-router-model-abc123")],
    )
    monkeypatch.setattr(
        claude_cowork,
        "configure",
        lambda *_args, **_kwargs: tmp_path / "profile.json",
    )
    monkeypatch.setattr(
        router_module,
        "start_spinner",
        lambda *_args, **_kwargs: pytest.fail("spinner should be quiet"),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "--quiet",
            "router",
            "configure-cowork",
            "--api-key",
            "router-secret",
        ],
    )

    assert result.exit_code == 0, result.output


def test_configure_cowork_rejects_plaintext_base_url_before_model_discovery(
    monkeypatch,
):
    monkeypatch.setenv("RAMP_ROUTER_BASE_URL", "http://router.example/v1")
    called = False

    def fetch_models(*_args, **_kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(router_module, "_fetch_models", fetch_models)

    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "configure-cowork",
            "--api-key",
            "router-secret",
        ],
    )

    assert result.exit_code != 0
    assert "HTTPS" in result.output
    assert called is False


def test_configure_cowork_runs_host_preflight_before_model_discovery(monkeypatch):
    discovered = False

    def fetch_models(*_args, **_kwargs):
        nonlocal discovered
        discovered = True
        return []

    monkeypatch.setattr(router_module, "_fetch_models", fetch_models)
    monkeypatch.setattr(
        claude_cowork,
        "preflight",
        lambda: (_ for _ in ()).throw(
            click.ClickException("Claude Desktop is not installed on this Mac.")
        ),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "configure-cowork",
            "--api-key",
            "router-secret",
        ],
    )

    assert result.exit_code != 0
    assert "Claude Desktop is not installed" in result.output
    assert discovered is False


def test_configure_cowork_runs_host_preflight_before_browser_key_setup(monkeypatch):
    monkeypatch.setattr(
        router_module,
        "acquire_router_api_key",
        lambda *_args, **_kwargs: pytest.fail("browser setup should not run"),
    )
    monkeypatch.setattr(
        claude_cowork,
        "preflight",
        lambda: (_ for _ in ()).throw(
            click.ClickException("Claude Desktop is not installed on this Mac.")
        ),
    )

    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure-cowork"],
    )

    assert result.exit_code != 0
    assert "Claude Desktop is not installed" in result.output


def test_configure_cowork_keeps_the_setup_file_when_configuration_fails(
    tmp_path, monkeypatch
):
    setup_file = tmp_path / "ramp-router-setup-abc123.json"
    setup_file.write_text(
        json.dumps(
            {
                "version": 1,
                "name": "Ramp Router",
                "base_url": "https://router.example/v1",
                "api_key": "router-secret",
            }
        )
    )
    monkeypatch.setattr(
        router_module,
        "_fetch_models",
        lambda *_args, **_kwargs: [_router_model("claude-router-model-abc123")],
    )
    monkeypatch.setattr(
        claude_cowork,
        "configure",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            click.ClickException("host setup failed")
        ),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "configure-cowork",
            "--setup-file",
            str(setup_file),
        ],
    )

    assert result.exit_code != 0
    assert setup_file.exists()
    assert "router-secret" not in result.output


def test_configure_cowork_reports_malformed_setup_json_as_a_bad_parameter(
    tmp_path,
):
    setup_file = tmp_path / "ramp-router-setup-broken.json"
    setup_file.write_text("{not-json")

    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "configure-cowork",
            "--setup-file",
            str(setup_file),
        ],
    )

    assert result.exit_code == 2
    assert "Invalid value for '--setup-file'" in result.output
    assert "internal error" not in result.output
    assert setup_file.exists()


def test_fetch_models_can_request_the_claude_cowork_projection(monkeypatch):
    seen = {}

    def get(url, *, headers, timeout):
        seen["request"] = (url, headers, timeout)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "claude-router-model-abc123",
                        "router": _router_metadata(
                            "claude-router-model-abc123",
                            request_name="gpt-5.6-sol",
                        ),
                    }
                ]
            },
            request=httpx.Request("GET", f"{ROUTER_BASE_URL}/models"),
        )

    monkeypatch.setattr(router_module.httpx, "get", get)

    models = router_module._fetch_models(
        "router-secret", gateway_client="claude-cowork"
    )

    assert models[0].id == "claude-router-model-abc123"
    assert seen["request"] == (
        f"{ROUTER_BASE_URL}/models",
        {
            "Authorization": "Bearer router-secret",
            "X-Gateway-Client": "claude-cowork",
        },
        10,
    )


def test_configured_router_clients_requires_receipts_and_credentials(
    tmp_path, monkeypatch
):
    claude_home = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    pi_home = tmp_path / "pi"
    config_home = tmp_path / "config"
    opencode_home = config_home / "opencode"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    # An interrupted first-time setup can leave a receipt before its
    # credential. It must not make updates try to refresh that client.
    opencode_home.mkdir(parents=True)
    (opencode_home / "opencode.json").write_text("{}\n")
    (opencode_home / "ramp-router-state.json").write_text("{}\n")
    for home in (codex_home, pi_home):
        home.mkdir()
        (home / "ramp-router-state.json").write_text("{}\n")
    (codex_home / "ramp-router-key").write_text("codex-key\n")
    (pi_home / "auth.json").write_text(
        json.dumps(
            {
                "ramp-router": {
                    "type": "api_key",
                    "key": "pi-key",
                }
            }
        )
        + "\n"
    )

    assert router_module.configured_router_clients() == ("codex", "pi")


def test_refresh_reapplies_only_clients_with_router_receipts(tmp_path, monkeypatch):
    opencode_home = tmp_path / "opencode"
    pi_home = tmp_path / "pi"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_home))
    opencode_home.mkdir()
    pi_home.mkdir()

    opencode_config = opencode_home / "opencode.json"
    opencode_config.write_text(
        json.dumps(
            {
                "plugin": [
                    [
                        "file:///old/ramp-cli/opencode-provider",
                        {"providerID": "ramp-router", "apiKey": "keep-me"},
                    ]
                ]
            }
        )
    )
    (opencode_home / "ramp-router-state.json").write_text("{}\n")

    pi_config = pi_home / "settings.json"
    pi_config.write_text(json.dumps({"packages": ["/old/ramp-cli/pi-provider"]}) + "\n")
    _mock_models(monkeypatch, key="keep-me")

    result = CliRunner().invoke(cli, ["--human", "router", "refresh"])

    assert result.exit_code == 0
    assert "OpenCode" in result.output
    assert "Pi" not in result.output
    plugin = json.loads(opencode_config.read_text())["plugin"][0]
    assert plugin == [
        router_module._bundled_plugin_path("opencode").as_uri(),
        {
            "providerID": "ramp-router",
            "name": "Ramp Router",
            "baseURL": ROUTER_BASE_URL,
            "usageBaseURL": "https://router.ramp.com",
            "apiKey": "keep-me",
        },
    ]
    # A refresh rewrites the TUI registration too, so a rotated key never
    # leaves the sidebar querying with the old one.
    tui_plugins = json.loads(router_module._opencode_tui_config_path().read_text())[
        "plugin"
    ]
    assert tui_plugins == [plugin]
    assert json.loads(pi_config.read_text()) == {
        "packages": ["/old/ramp-cli/pi-provider"]
    }


def test_refresh_fetches_models_once_per_api_key(tmp_path, monkeypatch):
    clients = ("codex", "opencode", "pi")
    keys = {"codex": "shared-key", "opencode": "shared-key", "pi": "pi-key"}
    fetched_keys = []
    configured_models = {}

    monkeypatch.setattr(router_module, "configured_router_clients", lambda: clients)
    monkeypatch.setattr(
        router_module,
        "_client_config_path",
        lambda client: tmp_path / client / "config",
    )
    monkeypatch.setattr(
        router_module,
        "_stored_router_api_key",
        lambda client, _path: keys[client],
    )
    monkeypatch.setattr(router_module, "_configured_model", lambda _client, _path: None)

    def fetch_models(api_key, **_kwargs):
        fetched_keys.append(api_key)
        return [object()]

    def configure_client(client, _api_key, models, *, base_url, selected_model):
        assert base_url == ROUTER_BASE_URL
        assert selected_model is None
        configured_models[client] = models
        return tmp_path / client / "config", "model", False

    monkeypatch.setattr(router_module, "_fetch_models", fetch_models)
    monkeypatch.setattr(router_module, "_configure_client", configure_client)

    result = CliRunner().invoke(cli, ["--human", "router", "refresh"])

    assert result.exit_code == 0, result.output
    assert fetched_keys == ["shared-key", "pi-key"]
    assert configured_models["codex"] is configured_models["opencode"]
    assert configured_models["codex"] is not configured_models["pi"]


def test_refresh_reapplies_codex_config_and_preserves_selected_model(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch, [{"id": "a"}, {"id": "b"}])
    configured = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "codex"],
        input="router-secret\n",
    )
    assert configured.exit_code == 0

    config_path = codex_home / "config.toml"
    outdated = config_path.read_text().replace('model = "a"', 'model = "b"')
    outdated = outdated.replace('wire_api = "responses"', 'wire_api = "chat"')
    config_path.write_text(outdated)
    catalog_path = codex_home / router_module.CODEX_ROUTER_CATALOG
    catalog_path.write_text(json.dumps({"models": [{"slug": "openai-only"}]}) + "\n")

    refreshed = CliRunner().invoke(cli, ["--human", "router", "refresh"])

    assert refreshed.exit_code == 0
    assert "Refreshed the Ramp Router configuration for Codex." in refreshed.output
    config = tomllib.loads(config_path.read_text())
    assert config["model"] == "b"
    assert config["model_providers"]["ramp-router"]["wire_api"] == "responses"
    assert [
        model["slug"] for model in json.loads(catalog_path.read_text())["models"]
    ] == ["a", "b"]


def test_refresh_reapplies_claude_config_and_preserves_selected_model(
    tmp_path, monkeypatch
):
    claude_home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    _mock_models(monkeypatch, [{"id": "claude-router-a"}, {"id": "claude-router-b"}])
    configured = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "claude-code"],
        input="router-secret\n",
    )
    assert configured.exit_code == 0

    settings_path = claude_home / "settings.json"
    settings = json.loads(settings_path.read_text())
    settings["model"] = "claude-router-b"
    settings["env"]["ANTHROPIC_BASE_URL"] = "https://stale.example"
    settings_path.write_text(json.dumps(settings) + "\n")

    refreshed = CliRunner().invoke(cli, ["--human", "router", "refresh"])

    assert refreshed.exit_code == 0, refreshed.output
    assert (
        "Refreshed the Ramp Router configuration for Claude Code." in refreshed.output
    )
    settings = json.loads(settings_path.read_text())
    assert settings["model"] == "claude-router-b"
    assert settings["env"]["ANTHROPIC_BASE_URL"] == "https://router-api.ramp.com"


def test_refresh_does_not_claim_a_user_selected_claude_model_view(
    tmp_path, monkeypatch
):
    claude_home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    _mock_models(monkeypatch)
    configured = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "claude-code",
            "--api-key",
            "router-secret",
        ],
    )
    assert configured.exit_code == 0, configured.output
    settings_path = claude_home / "settings.json"
    settings = json.loads(settings_path.read_text())
    settings["env"]["ANTHROPIC_CUSTOM_HEADERS"] += "\nX-Gateway-Model-View: all"
    settings_path.write_text(json.dumps(settings))

    refreshed = CliRunner().invoke(cli, ["--human", "router", "refresh"])
    assert refreshed.exit_code == 0, refreshed.output
    removed = CliRunner().invoke(
        cli, ["--human", "router", "unconfigure", "claude-code"]
    )
    assert removed.exit_code == 0, removed.output

    restored = json.loads(settings_path.read_text())
    assert restored["env"]["ANTHROPIC_CUSTOM_HEADERS"] == ("X-Gateway-Model-View: all")


def test_refresh_does_not_add_subagent_overrides(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    _mock_models(
        monkeypatch,
        [
            {
                "id": "claude-router-retired",
                "router": _router_metadata(
                    "retired-model", display_name="Retired Model"
                ),
            }
        ],
    )
    configured = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "claude-code"],
        input="router-secret\n",
    )
    assert configured.exit_code == 0, configured.output

    _mock_models(
        monkeypatch,
        [
            {
                "id": "claude-router-current",
                "router": _router_metadata(
                    "current-model", display_name="Current Model"
                ),
            }
        ],
    )
    refreshed = CliRunner().invoke(cli, ["--human", "router", "refresh"])

    assert refreshed.exit_code == 0, refreshed.output
    environment = json.loads((claude_home / "settings.json").read_text())["env"]
    assert not set(claude_code.SUBAGENT_ENV_KEYS) & environment.keys()


def test_configure_claude_fetches_only_its_model_projection(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    requests = []

    def get(url, *, headers, timeout):
        requests.append(headers)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "claude-router-a",
                        "router": _router_metadata("a"),
                    }
                ]
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(router_module.httpx, "get", get)

    configured = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "claude-code"],
        input="router-secret\n",
    )

    assert configured.exit_code == 0, configured.output
    # The status line download shares the mocked transport; only the
    # authenticated model fetch matters here.
    assert [headers for headers in requests if "Authorization" in headers] == [
        {
            "Authorization": "Bearer router-secret",
            "X-Gateway-Client": "claude-code",
        }
    ]


def test_configure_claude_all_models_sets_wire_header(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    requests = []

    def get(url, *, headers, timeout):
        requests.append(headers)
        return httpx.Response(
            200,
            json={"data": [{"id": "claude-router-a", "router": _router_metadata("a")}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(router_module.httpx, "get", get)
    configured = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "claude-code",
            "--api-key",
            "router-secret",
            "--claude-models",
            "all",
        ],
    )

    assert configured.exit_code == 0, configured.output
    assert [headers for headers in requests if "Authorization" in headers] == [
        {
            "Authorization": "Bearer router-secret",
            "X-Gateway-Client": "claude-code",
            "X-Gateway-Model-View": "all",
        }
    ]
    headers = json.loads((claude_home / "settings.json").read_text())["env"][
        "ANTHROPIC_CUSTOM_HEADERS"
    ]
    assert "X-Gateway-Model-View: all" in headers

    requests.clear()
    refreshed = CliRunner().invoke(cli, ["--human", "router", "refresh"])
    assert refreshed.exit_code == 0, refreshed.output
    assert [headers for headers in requests if "Authorization" in headers] == [
        {
            "Authorization": "Bearer router-secret",
            "X-Gateway-Client": "claude-code",
            "X-Gateway-Model-View": "all",
        }
    ]


def test_reconfigure_claude_only_updates_model_view(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    _mock_models(monkeypatch)
    configured = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "claude-code",
            "--api-key",
            "router-secret",
        ],
    )
    assert configured.exit_code == 0, configured.output
    settings_path = claude_home / "settings.json"
    settings = json.loads(settings_path.read_text())
    settings["userSetting"] = "keep"
    settings["env"]["ANTHROPIC_CUSTOM_HEADERS"] += "\nX-Unrelated: keep"
    settings_path.write_text(json.dumps(settings))

    monkeypatch.setattr(
        router_module.httpx,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("local preference update must not call Router")
        ),
    )
    expanded = CliRunner().invoke(
        cli,
        [
            "--human",
            "--no-input",
            "router",
            "configure",
            "claude-code",
            "--claude-models",
            "all",
        ],
    )

    assert expanded.exit_code == 0, expanded.output
    updated = json.loads(settings_path.read_text())
    assert updated["userSetting"] == "keep"
    assert updated["env"]["ANTHROPIC_AUTH_TOKEN"] == "router-secret"
    assert "X-Unrelated: keep" in updated["env"]["ANTHROPIC_CUSTOM_HEADERS"]
    assert "X-Gateway-Model-View: all" in updated["env"]["ANTHROPIC_CUSTOM_HEADERS"]

    machine = CliRunner().invoke(
        cli,
        [
            "--agent",
            "--no-input",
            "router",
            "configure",
            "claude-code",
            "--claude-models",
            "all",
        ],
    )
    assert machine.exit_code == 0, machine.output
    payload = json.loads(machine.output)["data"][0]
    original_setup_command = payload.pop("original_setup_command")
    assert original_setup_command.startswith("claude --settings ")
    assert payload == {
        "client": "claude-code",
        "config_path": str(settings_path),
        "provider": "ramp-router",
        "default_model": updated["model"],
        "models_available": None,
        "setup_file_deleted": False,
        "replaced_outdated_setup": False,
        "model_view": "all",
    }

    compact = CliRunner().invoke(
        cli,
        [
            "--human",
            "--no-input",
            "router",
            "configure",
            "claude-code",
            "--claude-models",
            "compact",
        ],
    )
    assert compact.exit_code == 0, compact.output
    headers = json.loads(settings_path.read_text())["env"]["ANTHROPIC_CUSTOM_HEADERS"]
    assert "X-Unrelated: keep" in headers
    assert "X-Gateway-Model-View" not in headers


def test_configure_claude_rejects_versions_that_mislabel_gateway_models(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    _mock_models(monkeypatch)
    monkeypatch.setattr(
        router_module.shutil,
        "which",
        lambda name: "/fake/claude" if name == "claude" else None,
    )
    monkeypatch.setattr(
        router_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["claude", "--version"],
            returncode=0,
            stdout="2.1.117 (Claude Code)\n",
            stderr="",
        ),
    )

    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "claude-code"],
        input="router-secret\n",
    )

    assert result.exit_code != 0
    assert "2.1.118 or newer is required" in result.output
    assert not (tmp_path / "claude" / "settings.json").exists()


def test_subagent_writes_reject_old_claude_but_reset_still_works(tmp_path, monkeypatch):
    settings_path = _configure_claude_for_subagents(
        monkeypatch,
        tmp_path,
        [
            {
                "id": "claude-router-current",
                "router": _router_metadata(
                    "current-model", display_name="Current Model"
                ),
            }
        ],
    )
    before = settings_path.read_text()
    monkeypatch.setattr(
        router_module.shutil,
        "which",
        lambda name: "/fake/old-claude-subagents" if name == "claude" else None,
    )
    monkeypatch.setattr(
        router_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["claude", "--version"],
            returncode=0,
            stdout="2.1.117 (Claude Code)\n",
            stderr="",
        ),
    )

    result = CliRunner().invoke(
        cli,
        ["--human", "router", "subagents", "--sonnet", "current-model"],
    )

    assert result.exit_code != 0
    assert "2.1.118 or newer is required" in result.output
    assert settings_path.read_text() == before

    reset = CliRunner().invoke(cli, ["--human", "router", "subagents", "--reset"])
    assert reset.exit_code == 0, reset.output


def test_configure_claude_does_not_set_subagent_defaults(tmp_path, monkeypatch):
    settings_path = _configure_claude_for_subagents(
        monkeypatch,
        tmp_path,
        [
            {
                "id": "claude-router-terra",
                "router": _router_metadata(
                    "gpt-5.6-terra",
                    display_name="GPT-5.6 Terra",
                    description="Balanced GPT-5.6 model",
                ),
            },
            {
                "id": "claude-router-luna",
                "router": _router_metadata(
                    "gpt-5.6-luna",
                    display_name="GPT-5.6 Luna",
                    description="Fast GPT-5.6 model",
                ),
            },
            {
                "id": "claude-router-sol",
                "router": _router_metadata(
                    "gpt-5.6-sol",
                    display_name="GPT-5.6 Sol",
                    description="Most capable GPT-5.6 model",
                ),
            },
        ],
    )

    environment = json.loads(settings_path.read_text())["env"]
    assert not set(claude_code.SUBAGENT_ENV_KEYS) & environment.keys()

    removed = CliRunner().invoke(
        cli, ["--human", "router", "unconfigure", "claude-code"]
    )
    assert removed.exit_code == 0, removed.output
    restored = json.loads(settings_path.read_text())
    assert not set(claude_code.SUBAGENT_ENV_KEYS) & restored.get("env", {}).keys()


def test_configure_claude_preserves_existing_tiers_then_restores_them(
    tmp_path, monkeypatch
):
    claude_home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    claude_home.mkdir()
    settings_path = claude_home / "settings.json"
    settings_path.write_text(
        json.dumps({"env": {"ANTHROPIC_DEFAULT_SONNET_MODEL": "user-selected-model"}})
    )
    _mock_models(monkeypatch)

    configured = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "claude-code"],
        input="router-secret\n",
    )

    assert configured.exit_code == 0, configured.output
    environment = json.loads(settings_path.read_text())["env"]
    assert environment["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "user-selected-model"
    assert (
        not (set(claude_code.SUBAGENT_ENV_KEYS) - {"ANTHROPIC_DEFAULT_SONNET_MODEL"})
        & environment.keys()
    )

    removed = CliRunner().invoke(
        cli, ["--human", "router", "unconfigure", "claude-code"]
    )
    assert removed.exit_code == 0, removed.output
    assert json.loads(settings_path.read_text()) == {
        "env": {"ANTHROPIC_DEFAULT_SONNET_MODEL": "user-selected-model"}
    }


def test_configure_claude_keeps_an_existing_router_tier_choice(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    claude_home.mkdir()
    settings_path = claude_home / "settings.json"
    settings_path.write_text(
        json.dumps({"env": {"ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-router-custom"}})
    )
    _mock_models(
        monkeypatch,
        [
            {
                "id": "claude-router-custom",
                "router": _router_metadata("custom-model"),
            }
        ],
    )

    configured = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "claude-code"],
        input="router-secret\n",
    )

    assert configured.exit_code == 0, configured.output
    settings = json.loads(settings_path.read_text())
    assert settings["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "claude-router-custom"
    state = json.loads((claude_home / "ramp-router-state.json").read_text())
    assert "ANTHROPIC_DEFAULT_SONNET_MODEL" not in state["written"]["env"]
    assert "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME" not in settings["env"]


def test_reconfigure_preserves_legacy_manual_tier_overrides(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    claude_home.mkdir()
    settings_path = claude_home / "settings.json"
    configured, state = claude_code.plan_configuration(
        {},
        settings_path,
        "https://router-api.ramp.com",
        "router-secret",
        "claude-router-terra",
    )
    configured, state = claude_code.plan_subagent_update(
        configured,
        settings_path,
        state,
        {
            "sonnet": "claude-router-terra",
            "opus": "claude-router-terra",
            "haiku": "claude-router-mini",
        },
    )
    # State from the prior CLI release had model overrides but none of the
    # truthful presentation settings and knew nothing about the fable tier.
    for key in (
        *claude_code.SUBAGENT_TIER_NAME_ENV_KEYS.values(),
        *claude_code.SUBAGENT_TIER_DESCRIPTION_ENV_KEYS.values(),
        claude_code.SUBAGENT_TIER_ENV_KEYS["fable"],
    ):
        configured["env"].pop(key, None)
        state["env"].pop(key, None)
        state["written"]["env"].pop(key, None)
    settings_path.write_text(json.dumps(configured))
    claude_code.state_path(settings_path).write_text(json.dumps(state))
    _mock_models(
        monkeypatch,
        [
            {
                "id": "claude-router-terra",
                "router": _router_metadata(
                    "gpt-5.6-terra", display_name="GPT-5.6 Terra"
                ),
            },
            {
                "id": "claude-router-mini",
                "router": _router_metadata("gpt-5-mini", display_name="GPT-5 Mini"),
            },
        ],
    )

    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "claude-code"],
        input="router-secret\n",
    )

    assert result.exit_code == 0, result.output
    environment = json.loads(settings_path.read_text())["env"]
    assert environment["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "claude-router-terra"
    assert environment["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-router-mini"
    assert "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME" not in environment
    assert "ANTHROPIC_DEFAULT_FABLE_MODEL" not in environment


def test_reconfigure_migrates_unchanged_router_owned_tier_defaults(
    tmp_path, monkeypatch
):
    claude_home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    claude_home.mkdir()
    settings_path = claude_home / "settings.json"
    configured, state = claude_code.plan_configuration(
        {},
        settings_path,
        "https://router-api.ramp.com",
        "router-secret",
        "claude-router-terra",
    )
    configured, state = claude_code.plan_subagent_update(
        configured,
        settings_path,
        state,
        {
            "sonnet": "claude-router-terra",
            "opus": "claude-router-terra",
            "haiku": "claude-router-mini",
            "fable": "claude-router-terra",
        },
        display_names={
            "sonnet": "GPT-5.6 Terra",
            "opus": "GPT-5.6 Terra",
            "haiku": "GPT-5 Mini",
            "fable": "GPT-5.6 Terra",
        },
        automatic_tiers=set(router_module.SUBAGENT_TIERS),
    )
    # Persist the presentation metadata written by the previous CLI release.
    for tier in router_module.SUBAGENT_TIERS:
        description_key = claude_code.SUBAGENT_TIER_DESCRIPTION_ENV_KEYS[tier]
        configured["env"][description_key] = "Old automatic default"
        state["written"]["env"][description_key] = "Old automatic default"
    for tier in ("opus", "fable"):
        name_key = claude_code.SUBAGENT_TIER_NAME_ENV_KEYS[tier]
        configured["env"][name_key] += f" ({tier.title()} tier)"
        state["written"]["env"][name_key] = configured["env"][name_key]
    settings_path.write_text(json.dumps(configured))
    claude_code.state_path(settings_path).write_text(json.dumps(state))
    _mock_models(
        monkeypatch,
        [
            {
                "id": "claude-router-terra",
                "router": _router_metadata(
                    "gpt-5.6-terra", display_name="GPT-5.6 Terra"
                ),
            },
            {
                "id": "claude-router-sol",
                "router": _router_metadata("gpt-5.6-sol", display_name="GPT-5.6 Sol"),
            },
            {
                "id": "claude-router-luna",
                "router": _router_metadata("gpt-5.6-luna", display_name="GPT-5.6 Luna"),
            },
            {
                "id": "claude-router-mini",
                "router": _router_metadata("gpt-5-mini", display_name="GPT-5 Mini"),
            },
        ],
    )

    # Values changed after configure are no longer Router-owned and must stay.
    opus_name_key = claude_code.SUBAGENT_TIER_NAME_ENV_KEYS["opus"]
    opus_description_key = claude_code.SUBAGENT_TIER_DESCRIPTION_ENV_KEYS["opus"]
    configured["env"][opus_name_key] = "My custom Opus name"
    configured["env"][opus_description_key] = "My custom Opus description"
    settings_path.write_text(json.dumps(configured))

    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "claude-code"],
        input="router-secret\n",
    )

    assert result.exit_code == 0, result.output
    environment = json.loads(settings_path.read_text())["env"]
    automatic_keys = {
        key
        for tier in router_module.SUBAGENT_TIERS
        for key in (
            claude_code.SUBAGENT_TIER_ENV_KEYS[tier],
            claude_code.SUBAGENT_TIER_NAME_ENV_KEYS[tier],
            claude_code.SUBAGENT_TIER_DESCRIPTION_ENV_KEYS[tier],
        )
    }
    assert (
        not (automatic_keys - {opus_name_key, opus_description_key})
        & environment.keys()
    )
    assert environment["ANTHROPIC_DEFAULT_OPUS_MODEL_NAME"] == "My custom Opus name"
    assert environment[opus_description_key] == "My custom Opus description"
    state = json.loads(claude_code.state_path(settings_path).read_text())
    assert state[claude_code.SUBAGENT_DEFAULTS_STATE_KEY] == {}


def test_reconfigure_preserves_a_manually_pinned_previous_default(
    tmp_path, monkeypatch
):
    models = [
        {
            "id": "claude-router-terra",
            "router": _router_metadata("gpt-5.6-terra", display_name="GPT-5.6 Terra"),
        },
        {
            "id": "claude-router-sol",
            "router": _router_metadata("gpt-5.6-sol", display_name="GPT-5.6 Sol"),
        },
        {
            "id": "claude-router-luna",
            "router": _router_metadata("gpt-5.6-luna", display_name="GPT-5.6 Luna"),
        },
    ]
    settings_path = _configure_claude_for_subagents(monkeypatch, tmp_path, models)
    pinned = CliRunner().invoke(
        cli,
        ["--human", "router", "subagents", "--opus", "gpt-5.6-terra"],
    )
    assert pinned.exit_code == 0, pinned.output

    reconfigured = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "claude-code"],
        input="router-secret\n",
    )

    assert reconfigured.exit_code == 0, reconfigured.output
    environment = json.loads(settings_path.read_text())["env"]
    assert environment["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-router-terra"
    state = json.loads(claude_code.state_path(settings_path).read_text())
    assert "opus" not in state[claude_code.SUBAGENT_DEFAULTS_STATE_KEY]


def test_subagents_uses_the_router_saved_by_nonproduction_configure(
    tmp_path, monkeypatch
):
    local_router = "http://127.0.0.1:24490/v1"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("RAMP_ROUTER_BASE_URL", local_router)
    _mock_models(monkeypatch, base_url=local_router)
    configured = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "claude-code"],
        input="router-secret\n",
    )
    assert configured.exit_code == 0, configured.output
    monkeypatch.delenv("RAMP_ROUTER_BASE_URL")

    result = CliRunner().invoke(cli, ["--human", "router", "subagents"])

    assert result.exit_code == 0, result.output
    assert "not set" in result.output


def _configure_claude_for_subagents(monkeypatch, tmp_path, models):
    claude_home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    _mock_models(monkeypatch, models)
    configured = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "claude-code"],
        input="router-secret\n",
    )
    assert configured.exit_code == 0, configured.output
    return claude_home / "settings.json"


def test_subagents_requires_a_configured_claude_code(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))

    result = CliRunner().invoke(
        cli, ["--human", "router", "subagents", "--sonnet", "gpt-5.4"]
    )

    assert result.exit_code != 0
    assert "not configured" in result.output
    # Refusing must leave no trace: the settings lock creates the directory,
    # so the not-configured check has to run before it.
    assert not claude_home.exists()


def test_unconfigure_without_a_setup_creates_nothing(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))

    result = CliRunner().invoke(
        cli, ["--human", "router", "unconfigure", "claude-code"]
    )

    assert result.exit_code != 0
    assert not claude_home.exists()


def test_subagents_set_resolves_fragments_and_unconfigure_restores(
    tmp_path, monkeypatch
):
    settings_path = _configure_claude_for_subagents(
        monkeypatch,
        tmp_path,
        [
            {
                "id": "claude-router-gpt-5-4-aaaaaa",
                "router": _router_metadata("gpt-5.4", display_name="GPT-5.4"),
            },
            {
                "id": "claude-router-kimi-k3-bbbbbb",
                "router": _router_metadata(
                    "accounts/fireworks/models/kimi-k3", display_name="Kimi K3"
                ),
            },
        ],
    )

    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "subagents",
            # An exact id and a display-name fragment must both resolve.
            "--sonnet",
            "claude-router-gpt-5-4-aaaaaa",
            "--haiku",
            "kimi",
        ],
    )

    assert result.exit_code == 0, result.output
    settings = json.loads(settings_path.read_text())
    environment = settings["env"]
    assert (
        environment["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "claude-router-gpt-5-4-aaaaaa"
    )
    assert (
        environment["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-router-kimi-k3-bbbbbb"
    )
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in environment
    assert "GPT-5.4" in result.output
    assert "Kimi K3" in result.output

    # The overrides name Router models, which mean nothing once Router is
    # gone, so unconfigure must take them away with everything else.
    removed = CliRunner().invoke(
        cli, ["--human", "router", "unconfigure", "claude-code"]
    )
    assert removed.exit_code == 0, removed.output
    restored = json.loads(settings_path.read_text())
    assert "ANTHROPIC_DEFAULT_SONNET_MODEL" not in restored.get("env", {})
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in restored.get("env", {})


def test_subagents_show_reports_tiers_in_agent_mode(tmp_path, monkeypatch):
    _configure_claude_for_subagents(
        monkeypatch,
        tmp_path,
        [
            {
                "id": "claude-router-gpt-5-4-aaaaaa",
                "router": _router_metadata("gpt-5.4", display_name="GPT-5.4"),
            }
        ],
    )
    assigned = CliRunner().invoke(
        cli, ["--human", "router", "subagents", "--opus", "GPT-5.4"]
    )
    assert assigned.exit_code == 0, assigned.output

    result = CliRunner().invoke(cli, ["--agent", "router", "subagents"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"][0]
    assert payload["tiers"]["opus"] == {
        "model": "claude-router-gpt-5-4-aaaaaa",
        "display_name": "GPT-5.4",
    }
    assert payload["tiers"]["sonnet"] == {"model": None}


def test_subagents_rejects_an_ambiguous_fragment(tmp_path, monkeypatch):
    _configure_claude_for_subagents(
        monkeypatch,
        tmp_path,
        [
            {
                "id": "claude-router-kimi-k3-aaaaaa",
                "router": _router_metadata("kimi-k3", display_name="Kimi K3"),
            },
            {
                "id": "claude-router-kimi-k3-fast-bbbbbb",
                "router": _router_metadata("kimi-k3-fast", display_name="Kimi K3 Fast"),
            },
        ],
    )

    result = CliRunner().invoke(
        cli, ["--human", "router", "subagents", "--sonnet", "kimi"]
    )

    assert result.exit_code != 0
    assert "claude-router-kimi-k3-aaaaaa" in result.output
    assert "claude-router-kimi-k3-fast-bbbbbb" in result.output

    # An exact request name that is also a fragment of the other model's name
    # must resolve to its own model rather than be reported ambiguous.
    exact = CliRunner().invoke(
        cli, ["--human", "router", "subagents", "--sonnet", "kimi-k3"]
    )
    assert exact.exit_code == 0, exact.output
    assert "Kimi K3 (claude-router-kimi-k3-aaaaaa)" in exact.output


def test_subagents_reset_needs_no_credential_or_network(tmp_path, monkeypatch):
    settings_path = _configure_claude_for_subagents(
        monkeypatch,
        tmp_path,
        [
            {
                "id": "claude-router-gpt-5-4-aaaaaa",
                "router": _router_metadata("gpt-5.4", display_name="GPT-5.4"),
            }
        ],
    )
    assigned = CliRunner().invoke(
        cli, ["--human", "router", "subagents", "--sonnet", "gpt-5.4"]
    )
    assert assigned.exit_code == 0, assigned.output

    # The exact situation reset exists for: the stored key no longer works and
    # Router is unreachable, while the overrides keep failing every sub-agent
    # spawn. Clearing them must not require either.
    settings = json.loads(settings_path.read_text())
    del settings["env"]["ANTHROPIC_AUTH_TOKEN"]
    settings_path.write_text(json.dumps(settings) + "\n")

    def refuse(*_args, **_kwargs):
        raise AssertionError("reset must not call Router")

    monkeypatch.setattr("ramp_cli.commands.router.httpx.get", refuse)

    cleared = CliRunner().invoke(cli, ["--human", "router", "subagents", "--reset"])

    assert cleared.exit_code == 0, cleared.output
    settings = json.loads(settings_path.read_text())
    assert not set(claude_code.SUBAGENT_ENV_KEYS) & settings["env"].keys()


def test_subagents_survive_a_refresh_then_unconfigure_restores(tmp_path, monkeypatch):
    settings_path = _configure_claude_for_subagents(
        monkeypatch,
        tmp_path,
        [
            {
                "id": "claude-router-gpt-5-4-aaaaaa",
                "router": _router_metadata("gpt-5.4", display_name="GPT-5.4"),
            }
        ],
    )
    assigned = CliRunner().invoke(
        cli, ["--human", "router", "subagents", "--haiku", "gpt-5.4"]
    )
    assert assigned.exit_code == 0, assigned.output

    refreshed = CliRunner().invoke(cli, ["--human", "router", "refresh"])
    assert refreshed.exit_code == 0, refreshed.output
    settings = json.loads(settings_path.read_text())
    assert (
        settings["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"]
        == "claude-router-gpt-5-4-aaaaaa"
    )

    removed = CliRunner().invoke(
        cli, ["--human", "router", "unconfigure", "claude-code"]
    )
    assert removed.exit_code == 0, removed.output
    restored = json.loads(settings_path.read_text())
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in restored.get("env", {})


def test_every_claude_code_writer_holds_the_settings_lock(tmp_path, monkeypatch):
    """Configure, subagents, and unconfigure all rewrite the same two files.

    Any of them running outside the shared lock reintroduces the lost-update
    race: a concurrent writer's settings land unpaired with its receipt, or a
    receipt is deleted for a setup that just re-wrote itself.
    """
    entered = []
    real_lock = claude_code.settings_lock

    @contextlib.contextmanager
    def recording_lock(path):
        entered.append(path)
        with real_lock(path):
            yield

    monkeypatch.setattr(claude_code, "settings_lock", recording_lock)
    claude_home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    _mock_models(
        monkeypatch,
        [
            {
                "id": "claude-router-gpt-5-4-aaaaaa",
                "router": _router_metadata("gpt-5.4", display_name="GPT-5.4"),
            }
        ],
    )

    configured = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "claude-code"],
        input="router-secret\n",
    )
    assert configured.exit_code == 0, configured.output
    assert len(entered) == 1

    assigned = CliRunner().invoke(
        cli, ["--human", "router", "subagents", "--sonnet", "gpt-5.4"]
    )
    assert assigned.exit_code == 0, assigned.output
    assert len(entered) == 2

    removed = CliRunner().invoke(
        cli, ["--human", "router", "unconfigure", "claude-code"]
    )
    assert removed.exit_code == 0, removed.output
    assert len(entered) == 3


def test_subagents_reset_clears_overrides_and_conflicts_with_tiers(
    tmp_path, monkeypatch
):
    settings_path = _configure_claude_for_subagents(
        monkeypatch,
        tmp_path,
        [
            {
                "id": "claude-router-gpt-5-4-aaaaaa",
                "router": _router_metadata("gpt-5.4", display_name="GPT-5.4"),
            }
        ],
    )
    conflict = CliRunner().invoke(
        cli, ["--human", "router", "subagents", "--reset", "--sonnet", "gpt-5.4"]
    )
    assert conflict.exit_code != 0
    assert "--reset" in conflict.output

    assigned = CliRunner().invoke(
        cli, ["--human", "router", "subagents", "--sonnet", "gpt-5.4"]
    )
    assert assigned.exit_code == 0, assigned.output

    cleared = CliRunner().invoke(cli, ["--human", "router", "subagents", "--reset"])

    assert cleared.exit_code == 0, cleared.output
    settings = json.loads(settings_path.read_text())
    assert "ANTHROPIC_DEFAULT_SONNET_MODEL" not in settings["env"]
    assert "not set" in cleared.output


def test_refresh_agent_mode_does_not_emit_success_before_partial_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        router_module, "configured_router_clients", lambda: ("codex", "pi")
    )

    def stored_key(client, _path):
        if client == "pi":
            raise click.ClickException("expired credential")
        return "router-secret"

    monkeypatch.setattr(router_module, "_stored_router_api_key", stored_key)
    monkeypatch.setattr(
        router_module,
        "_fetch_models",
        lambda _api_key, **_kwargs: [
            router_module.RouterModel(id="model", metadata=None)
        ],
    )
    monkeypatch.setattr(router_module, "_configured_model", lambda _client, _path: None)
    monkeypatch.setattr(
        router_module,
        "_configure_client",
        lambda client, _api_key, _models, base_url=None, selected_model=None: (
            tmp_path / client,
            "model",
            False,
        ),
    )

    result = CliRunner().invoke(cli, ["--agent", "router", "refresh"])

    assert result.exit_code != 0
    assert result.output == "Error: Could not refresh Pi: expired credential\n"
    assert '"clients"' not in result.output


def test_configure_codex_writes_router_provider(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch, [{"id": "a"}, {"id": "b"}])

    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "codex"],
        input="router-secret\n",
    )

    assert result.exit_code == 0
    config_path = codex_home / "config.toml"
    config = tomllib.loads(config_path.read_text())
    key_path = codex_home / "ramp-router-key"
    provider = config["model_providers"]["ramp-router"]
    assert provider == {
        "name": "Ramp Router",
        "base_url": "https://router-api.ramp.com/v1",
        "wire_api": "responses",
        "supports_websockets": False,
        # Command auth, not experimental_bearer_token: Codex only refreshes its
        # model list for providers using a command or a ChatGPT account.
        "auth": {
            "command": "/bin/cat",
            "args": [str(key_path)],
            "timeout_ms": 5000,
        },
        # Router answers with Codex's own catalog shape only for a client that
        # declares itself. That shape carries no harness prompt, so it must
        # arrive with the model_instructions_file written below and never on
        # its own.
        "http_headers": {"X-Gateway-Client": "codex"},
    }
    assert config["model_provider"] == "ramp-router"
    assert config["model"] == "a"
    catalog_path = codex_home / "ramp-router-models.json"
    assert config["model_catalog_json"] == str(catalog_path)
    assert [
        model["slug"] for model in json.loads(catalog_path.read_text())["models"]
    ] == [
        "a",
        "b",
    ]
    assert key_path.read_text() == "router-secret"
    assert "Connecting Ramp Router to your coding agent" in result.output
    assert "Connected to: Codex" in result.output
    assert "2 models added. Start an agent and pick a model." in result.output
    assert "Create or copy an API key" not in result.output
    assert "router-secret" not in result.output
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert catalog_path.stat().st_mode & 0o777 == 0o600


def test_listing_order_does_not_choose_the_default_model():
    def model(identifier, order):
        return router_module.RouterModel(
            id=identifier,
            metadata=router_module.RouterModelMetadata(
                request_name=identifier,
                display_name=identifier,
                description="",
                listing_order=order,
                efforts=(),
                default_effort="",
                input_modalities=("text",),
                context_window=128000,
                max_output_tokens=16384,
                tool_calls=True,
            ),
        )

    # Router's order is alphabetical-ish presentation, so the earliest entry is
    # whatever sorts first, not the best model to hand a coding agent.
    models = [
        model("accounts/fireworks/models/deepseek-v4-flash", 0),
        model(router_module.DEFAULT_MODEL, 40),
    ]

    assert router_module._preferred_model(models) == router_module.DEFAULT_MODEL


def test_a_router_without_the_default_still_configures_something():
    # The default is named here rather than published by Router, so a stack
    # that does not serve it must not leave the agent with no model at all.
    def model(identifier):
        return router_module.RouterModel(
            id=identifier,
            metadata=router_module.RouterModelMetadata(
                request_name=identifier,
                display_name=identifier,
                description="",
                listing_order=0,
                efforts=(),
                default_effort="",
                input_modalities=("text",),
                context_window=128000,
                max_output_tokens=16384,
                tool_calls=True,
            ),
        )

    assert (
        router_module._preferred_model([model("some-other-model")])
        == "some-other-model"
    )


def test_fetch_models_reads_the_router_metadata_extension(monkeypatch):
    payload = {
        "data": [
            {
                "id": "gpt-5.6-sol",
                "owned_by": "openai",
                "router": {
                    "schema_version": 1,
                    "request_name": "gpt-5.6-sol",
                    "display_name": "GPT-5.6 Sol",
                    "description": "A fast frontier model.",
                    "listing": {"order": 5},
                    "limits": {"context_window": 1050000, "max_output_tokens": 128000},
                    "capabilities": {
                        "modalities": {"input": ["text", "image"]},
                        "reasoning": {
                            "efforts": [
                                {"value": "low", "description": "Fast"},
                                {"value": "high", "description": "Deep"},
                            ],
                            "default_effort": "low",
                        },
                    },
                },
            },
        ]
    }

    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return payload

    monkeypatch.setattr(router_module.httpx, "get", lambda *a, **k: _Response())

    models = router_module._fetch_models("router-secret")

    assert [model.id for model in models] == ["gpt-5.6-sol"]
    described = models[0].metadata
    assert described.display_name == "GPT-5.6 Sol"
    assert described.listing_order == 5
    assert described.efforts == (("low", "Fast"), ("high", "Deep"))
    assert described.default_effort == "low"


def test_codex_catalog_respects_the_desktop_limit_and_keeps_the_selected_model(
    monkeypatch,
):
    models = [
        {"slug": f"model-{number:03d}", "base_instructions": ""}
        for number in range(101)
    ]

    def get(url, *, headers, timeout):
        assert url == f"{ROUTER_BASE_URL}/models"
        assert headers["X-Gateway-Client"] == "codex"
        assert timeout == 10
        return httpx.Response(
            200,
            json={"models": models},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(router_module.httpx, "get", get)

    catalog = json.loads(
        router_module._render_codex_catalog(
            router_module._fetch_codex_catalog("router-secret"), "model-100"
        )
    )

    assert len(catalog["models"]) == router_module.CODEX_CATALOG_MODEL_LIMIT
    assert catalog["models"][-1]["slug"] == "model-100"
    assert "model-099" not in {model["slug"] for model in catalog["models"]}


def test_codex_catalog_refuses_a_selected_model_missing_from_its_projection(
    monkeypatch,
):
    def get(url, *, headers, timeout):
        return httpx.Response(
            200,
            json={"models": [{"slug": "codex-only", "base_instructions": ""}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(router_module.httpx, "get", get)

    with pytest.raises(click.ClickException, match="generic-only"):
        router_module._render_codex_catalog(
            router_module._fetch_codex_catalog("router-secret"), "generic-only"
        )


def test_configure_codex_preserves_unrelated_config_and_is_idempotent(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text(
        """# Keep this comment
model = "existing-model"
model_provider = "existing"

[model_providers.existing]
name = "Existing"
base_url = "https://example.com/v1"

[model_providers.ramp-router]
name = "Stale"
base_url = "https://stale.example.com"

[features]
web_search = true
"""
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    runner = CliRunner()

    first = runner.invoke(
        cli, ["--human", "router", "configure", "codex"], input="router-secret\n"
    )
    first_content = config_path.read_text()
    second = runner.invoke(
        cli, ["--human", "router", "configure", "codex"], input="router-secret\n"
    )

    assert first.exit_code == second.exit_code == 0
    assert config_path.read_text() == first_content
    assert "# Keep this comment" in first_content
    config = tomllib.loads(first_content)
    assert config["model"] == "gpt-5.4"
    assert config["model_provider"] == "ramp-router"
    assert "https://stale.example.com" not in first_content
    assert first_content.count("[model_providers.ramp-router]") == 1
    assert "[features]\nweb_search = true" in first_content


def test_configure_codex_reads_config_after_catalog_discovery(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('model = "before-discovery"\n')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(router_module.shutil, "which", lambda _name: None)
    _mock_models(monkeypatch)

    def fetch_catalog(*_args, **_kwargs):
        config_path.write_text(
            'model = "edited-during-discovery"\n[features]\nweb_search = true\n'
        )
        return {"models": [{"slug": "gpt-5.4", "base_instructions": ""}]}

    monkeypatch.setattr(router_module, "_fetch_codex_catalog", fetch_catalog)
    runner = CliRunner()

    configured = runner.invoke(
        cli, ["--human", "router", "configure", "codex"], input="router-secret\n"
    )
    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert configured.exit_code == removed.exit_code == 0
    restored = tomllib.loads(config_path.read_text())
    assert restored["model"] == "edited-during-discovery"
    assert restored["features"]["web_search"] is True


def test_refresh_preserves_a_model_changed_during_catalog_discovery(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(router_module.shutil, "which", lambda _name: None)
    _mock_models(monkeypatch, [{"id": "a"}, {"id": "b"}])
    runner = CliRunner()
    configured = runner.invoke(
        cli, ["--human", "router", "configure", "codex"], input="router-secret\n"
    )
    assert configured.exit_code == 0, configured.output
    config_path = codex_home / "config.toml"
    real_fetch = router_module._fetch_codex_catalog

    def fetch_catalog(*args, **kwargs):
        config_path.write_text(
            config_path.read_text().replace('model = "a"', 'model = "b"')
        )
        return real_fetch(*args, **kwargs)

    monkeypatch.setattr(router_module, "_fetch_codex_catalog", fetch_catalog)

    refreshed = runner.invoke(cli, ["--human", "router", "refresh"])

    assert refreshed.exit_code == 0, refreshed.output
    assert tomllib.loads(config_path.read_text())["model"] == "b"


def test_configure_codex_replaces_quoted_router_provider(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text(
        """["model_providers" . 'ramp-router']
name = "Stale"
base_url = "https://stale.example.com"
"""
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)

    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "codex"], input="router-secret\n"
    )

    assert result.exit_code == 0
    config = tomllib.loads(config_path.read_text())
    assert config["model_providers"]["ramp-router"]["base_url"] == ROUTER_BASE_URL
    assert "https://stale.example.com" not in config_path.read_text()


def test_unconfigure_restores_previous_codex_settings(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text(
        f"""model = "existing-model"
model_provider = "existing"
model_catalog_json = "/tmp/existing-models.json"

[model_providers.existing]
name = "Existing"
base_url = "https://example.com/v1"

[model_providers.ramp-router]
name = "Manual Router"
base_url = "{ROUTER_BASE_URL}"
experimental_bearer_token = "original-secret"

[features]
web_search = true
"""
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    runner = CliRunner()

    configure = runner.invoke(
        cli, ["--human", "router", "configure", "codex"], input="router-secret\n"
    )
    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert configure.exit_code == unconfigure.exit_code == 0
    restored = tomllib.loads(config_path.read_text())
    assert restored["model"] == "existing-model"
    assert restored["model_provider"] == "existing"
    assert restored["model_catalog_json"] == "/tmp/existing-models.json"
    assert restored["model_providers"]["ramp-router"] == {
        "name": "Manual Router",
        "base_url": ROUTER_BASE_URL,
        "experimental_bearer_token": "original-secret",
    }
    assert restored["features"]["web_search"] is True
    assert not (codex_home / "ramp-router-models.json").exists()
    assert not (codex_home / "ramp-router-state.json").exists()
    assert (
        "Removed Ramp Router and restored your previous settings for: Codex."
        in unconfigure.output
    )


def test_unconfigure_preserves_a_router_catalog_the_user_modified(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('model_catalog_json = "/tmp/user-models.json"\n')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    runner = CliRunner()

    configured = runner.invoke(
        cli, ["--human", "router", "configure", "codex"], input="router-secret\n"
    )
    assert configured.exit_code == 0, configured.output
    router_catalog = codex_home / router_module.CODEX_ROUTER_CATALOG
    user_edit = '{"models":[{"slug":"user-edited"}]}\n'
    router_catalog.write_text(user_edit)

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert removed.exit_code == 0, removed.output
    assert router_catalog.read_text() == user_edit
    assert tomllib.loads(config_path.read_text())["model_catalog_json"] == (
        "/tmp/user-models.json"
    )


def test_unconfigure_reports_the_client_error_when_every_restore_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))

    result = CliRunner().invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert result.exit_code != 0
    assert (
        "Could not unconfigure Codex: Ramp Router is not configured in Codex."
        in result.output
    )
    assert not isinstance(result.exception, IndexError)


def test_configure_codex_syncs_existing_sessions_and_unconfigure_restores_them(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    session_path = codex_home / "sessions" / "2026" / "07" / "session.jsonl"
    session_path.parent.mkdir(parents=True)
    session_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-24T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "0198398b-443e-7000-8000-000000000001",
                    "model_provider": "openai",
                    "cwd": "/tmp/project",
                },
            }
        )
        + "\n"
        + json.dumps({"type": "response_item", "payload": {}})
        + "\n"
        + json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": "0198398b-443e-7000-8000-000000000001",
                    "model_provider": "openai",
                },
            }
        )
        + "\n"
    )
    database_path = codex_home / "state_5.sqlite"
    with sqlite3.connect(database_path) as database:
        database.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT NOT NULL)"
        )
        database.execute(
            "INSERT INTO threads VALUES (?, ?)",
            ("0198398b-443e-7000-8000-000000000001", "openai"),
        )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    runner = CliRunner()

    configure = runner.invoke(
        cli, ["--human", "router", "configure", "codex"], input="router-secret\n"
    )

    assert configure.exit_code == 0
    synced_meta = json.loads(session_path.read_text().splitlines()[0])
    assert synced_meta["payload"]["model_provider"] == "ramp-router"
    with sqlite3.connect(database_path) as database:
        assert database.execute("SELECT model_provider FROM threads").fetchone() == (
            "ramp-router",
        )

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert unconfigure.exit_code == 0
    restored_meta = json.loads(session_path.read_text().splitlines()[0])
    assert restored_meta["payload"]["model_provider"] == "openai"
    with sqlite3.connect(database_path) as database:
        assert database.execute("SELECT model_provider FROM threads").fetchone() == (
            "openai",
        )


def test_configure_codex_syncs_session_without_provider(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    session_path = codex_home / "sessions" / "session.jsonl"
    session_path.parent.mkdir(parents=True)
    session_path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "0198398b-443e-7000-8000-000000000002"},
            }
        )
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    runner = CliRunner()

    configure = runner.invoke(
        cli, ["--human", "router", "configure", "codex"], input="router-secret\n"
    )
    assert configure.exit_code == 0
    assert (
        json.loads(session_path.read_text().splitlines()[0])["payload"][
            "model_provider"
        ]
        == "ramp-router"
    )

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])
    assert unconfigure.exit_code == 0
    assert (
        "model_provider"
        not in json.loads(session_path.read_text().splitlines()[0])["payload"]
    )


def test_configure_codex_syncs_indexed_compressed_session(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    sessions_dir = codex_home / "sessions"
    sessions_dir.mkdir(parents=True)
    rollout_path = sessions_dir / "session.jsonl"
    compressed_path = sessions_dir / "session.jsonl.zst"
    transcript = (
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": "0198398b-443e-7000-8000-000000000003",
                    "model_provider": "openai",
                },
            }
        )
        + "\n"
    )
    compressed_path.write_bytes(
        zstandard.ZstdCompressor().compress(transcript.encode("utf-8"))
    )
    database_path = codex_home / "state_5.sqlite"
    with sqlite3.connect(database_path) as database:
        database.execute(
            "CREATE TABLE threads ("
            "id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, "
            "model_provider TEXT NOT NULL)"
        )
        database.execute(
            "INSERT INTO threads VALUES (?, ?, ?)",
            (
                "0198398b-443e-7000-8000-000000000003",
                str(rollout_path),
                "openai",
            ),
        )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    runner = CliRunner()

    configure = runner.invoke(
        cli, ["--human", "router", "configure", "codex"], input="router-secret\n"
    )
    assert configure.exit_code == 0
    with zstandard.ZstdDecompressor().stream_reader(
        io.BytesIO(compressed_path.read_bytes())
    ) as reader:
        synced = reader.read()
    assert json.loads(synced)["payload"]["model_provider"] == "ramp-router"
    with sqlite3.connect(database_path) as database:
        assert database.execute("SELECT model_provider FROM threads").fetchone() == (
            "ramp-router",
        )

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])
    assert unconfigure.exit_code == 0
    with zstandard.ZstdDecompressor().stream_reader(
        io.BytesIO(compressed_path.read_bytes())
    ) as reader:
        restored = reader.read()
    assert json.loads(restored)["payload"]["model_provider"] == "openai"
    with sqlite3.connect(database_path) as database:
        assert database.execute("SELECT model_provider FROM threads").fetchone() == (
            "openai",
        )


def test_configure_codex_syncs_sessions_for_existing_router_setup(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    sessions_dir = codex_home / "sessions"
    sessions_dir.mkdir(parents=True)
    session_path = sessions_dir / "session.jsonl"
    session_path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": "0198398b-443e-7000-8000-000000000004",
                    "model_provider": "openai",
                },
            }
        )
        + "\n"
    )
    state_path = codex_home / "ramp-router-state.json"
    state_path.write_text(
        json.dumps({"root": {}, "provider": [], "sessions": []}) + "\n"
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)

    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "codex"], input="router-secret\n"
    )

    assert result.exit_code == 0
    assert (
        json.loads(session_path.read_text().splitlines()[0])["payload"][
            "model_provider"
        ]
        == "ramp-router"
    )
    assert json.loads(state_path.read_text())["sessions"][0]["id"] == (
        "0198398b-443e-7000-8000-000000000004"
    )


def test_configure_codex_repairs_index_on_retry(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    sessions_dir = codex_home / "sessions"
    sessions_dir.mkdir(parents=True)
    session_id = "0198398b-443e-7000-8000-000000000006"
    session_path = sessions_dir / "session.jsonl"
    session_path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": session_id, "model_provider": "ramp-router"},
            }
        )
        + "\n"
    )
    (codex_home / "ramp-router-state.json").write_text(
        json.dumps(
            {
                "root": {},
                "provider": [],
                "sessions": [
                    {
                        "path": "sessions/session.jsonl",
                        "id": session_id,
                        "transcript_updated": True,
                        "had_model_provider": True,
                        "model_provider": "openai",
                        "index_model_provider": "openai",
                    }
                ],
            }
        )
        + "\n"
    )
    database_path = codex_home / "state_5.sqlite"
    with sqlite3.connect(database_path) as database:
        database.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT NOT NULL)"
        )
        database.execute("INSERT INTO threads VALUES (?, ?)", (session_id, "openai"))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)

    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "codex"], input="router-secret\n"
    )

    assert result.exit_code == 0
    with sqlite3.connect(database_path) as database:
        assert database.execute("SELECT model_provider FROM threads").fetchone() == (
            "ramp-router",
        )


def test_configure_codex_aborts_if_transcript_changes(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    session_path = codex_home / "sessions" / "session.jsonl"
    session_path.parent.mkdir(parents=True)
    session_path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": "0198398b-443e-7000-8000-000000000007",
                    "model_provider": "openai",
                },
            }
        )
        + "\n"
    )
    original_copy = router_module._copy_codex_session

    def copy_with_concurrent_append(*args, **kwargs):
        original_copy(*args, **kwargs)
        with session_path.open("a") as session:
            session.write(json.dumps({"type": "response_item", "payload": {}}) + "\n")

    monkeypatch.setattr(
        router_module, "_copy_codex_session", copy_with_concurrent_append
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)

    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "codex"], input="router-secret\n"
    )

    assert result.exit_code != 0
    assert "Close Codex and try again" in result.output
    assert (
        json.loads(session_path.read_text().splitlines()[0])["payload"][
            "model_provider"
        ]
        == "openai"
    )
    assert (
        json.loads(session_path.read_text().splitlines()[-1])["type"] == "response_item"
    )


def test_unconfigure_codex_rolls_sessions_back_if_config_write_fails(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    session_path = codex_home / "sessions" / "session.jsonl"
    session_path.parent.mkdir(parents=True)
    session_path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": "0198398b-443e-7000-8000-000000000008",
                    "model_provider": "openai",
                },
            }
        )
        + "\n"
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    runner = CliRunner()
    assert (
        runner.invoke(
            cli,
            ["--human", "router", "configure", "codex"],
            input="router-secret\n",
        ).exit_code
        == 0
    )
    config_path = codex_home / "config.toml"
    original_write = router_module._write_private_file

    def fail_config_write(path, content):
        if path == config_path:
            raise OSError("read-only config")
        original_write(path, content)

    monkeypatch.setattr(router_module, "_write_private_file", fail_config_write)

    result = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert result.exit_code != 0
    assert (
        json.loads(session_path.read_text().splitlines()[0])["payload"][
            "model_provider"
        ]
        == "ramp-router"
    )
    assert (codex_home / "ramp-router-state.json").exists()


def test_unconfigure_codex_preserves_newer_session_provider(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    session_path = codex_home / "sessions" / "session.jsonl"
    session_path.parent.mkdir(parents=True)
    session_id = "0198398b-443e-7000-8000-000000000009"
    session_path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": session_id, "model_provider": "openai"},
            }
        )
        + "\n"
    )
    database_path = codex_home / "state_5.sqlite"
    with sqlite3.connect(database_path) as database:
        database.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT NOT NULL)"
        )
        database.execute("INSERT INTO threads VALUES (?, ?)", (session_id, "openai"))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    runner = CliRunner()
    assert (
        runner.invoke(
            cli,
            ["--human", "router", "configure", "codex"],
            input="router-secret\n",
        ).exit_code
        == 0
    )
    metadata = json.loads(session_path.read_text())
    metadata["payload"]["model_provider"] = "new-provider"
    session_path.write_text(json.dumps(metadata) + "\n")
    with sqlite3.connect(database_path) as database:
        database.execute(
            "UPDATE threads SET model_provider = 'new-provider' WHERE id = ?",
            (session_id,),
        )

    result = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert result.exit_code == 0
    assert json.loads(session_path.read_text())["payload"]["model_provider"] == (
        "new-provider"
    )
    with sqlite3.connect(database_path) as database:
        assert database.execute("SELECT model_provider FROM threads").fetchone() == (
            "new-provider",
        )


def test_configure_codex_requires_interactive_input(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))

    result = CliRunner().invoke(cli, ["--agent", "router", "configure"])

    assert result.exit_code != 0
    assert (
        "Pass --setup-file or --api-key when using non-interactive mode"
        in result.output
    )
    assert "https://router.ramp.com" in result.output
    assert not (tmp_path / "codex" / "config.toml").exists()


def test_configure_reuses_one_existing_router_key_without_browser(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    pi_home = tmp_path / "pi"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_home))
    (codex_home / "ramp-router-state.json").write_text("{}\n")
    (codex_home / "ramp-router-key").write_text("existing-key\n")
    (codex_home / "config.toml").write_text(
        f'[model_providers.ramp-router]\nbase_url = "{ROUTER_BASE_URL}"\n'
    )
    _mock_models(monkeypatch, key="existing-key")

    def unexpected_browser_setup(*_args, **_kwargs):
        raise AssertionError("browser setup should not run")

    monkeypatch.setattr(
        router_module, "acquire_router_api_key", unexpected_browser_setup
    )
    monkeypatch.setattr(router_module, "_can_prompt", lambda _ctx, _fmt: True)

    class Prompt:
        def ask(self):
            return 0

    captured = {}

    def select(message, **kwargs):
        captured["message"] = message
        captured.update(kwargs)
        return Prompt()

    monkeypatch.setattr(router_module.questionary, "select", select)

    result = CliRunner().invoke(cli, ["--human", "router", "configure", "pi"])

    assert result.exit_code == 0, result.output
    assert captured["message"] == "Choose a Ramp Router API key"
    assert [choice.title for choice in captured["choices"]] == [
        "Reuse existing key used by Codex",
        "Create a new key",
    ]
    assert json.loads((pi_home / "auth.json").read_text())["ramp-router"]["key"] == (
        "existing-key"
    )
    assert "existing-key" not in result.output


def test_configure_does_not_reuse_a_key_from_another_router_endpoint(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    pi_home = tmp_path / "pi"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_home))
    (codex_home / "ramp-router-state.json").write_text("{}\n")
    (codex_home / "ramp-router-key").write_text("nonproduction-key\n")
    (codex_home / "config.toml").write_text(
        '[model_providers.ramp-router]\nbase_url = "https://qa-router.ramp.dev/v1"\n'
    )
    _mock_models(monkeypatch, key="browser-key")
    browser_calls = []

    def acquire(url, *, no_browser):
        browser_calls.append((url, no_browser))
        return "browser-key"

    monkeypatch.setattr(router_module, "acquire_router_api_key", acquire)

    result = CliRunner().invoke(cli, ["--human", "router", "configure", "pi"])

    assert result.exit_code == 0, result.output
    assert browser_calls == [("https://router.ramp.com", False)]
    assert json.loads((pi_home / "auth.json").read_text())["ramp-router"]["key"] == (
        "browser-key"
    )


def test_configure_uses_browser_when_existing_router_keys_disagree(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    pi_home = tmp_path / "pi"
    for home in (codex_home, pi_home):
        home.mkdir()
        (home / "ramp-router-state.json").write_text("{}\n")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_home))
    (codex_home / "ramp-router-key").write_text("codex-key\n")
    (codex_home / "config.toml").write_text(
        f'[model_providers.ramp-router]\nbase_url = "{ROUTER_BASE_URL}"\n'
    )
    (pi_home / "auth.json").write_text(
        json.dumps({"ramp-router": {"type": "api_key", "key": "pi-key"}}) + "\n"
    )
    (pi_home / "ramp-router-config.json").write_text(
        json.dumps({"baseUrl": ROUTER_BASE_URL}) + "\n"
    )
    _mock_models(monkeypatch, key="browser-key")
    browser_calls = []

    def acquire(url, *, no_browser):
        browser_calls.append((url, no_browser))
        return "browser-key"

    monkeypatch.setattr(router_module, "acquire_router_api_key", acquire)
    monkeypatch.setattr(router_module, "_can_prompt", lambda _ctx, _fmt: True)

    class Prompt:
        def ask(self):
            return "new"

    captured = {}

    def select(_message, **kwargs):
        captured.update(kwargs)
        return Prompt()

    monkeypatch.setattr(router_module.questionary, "select", select)

    result = CliRunner().invoke(cli, ["--human", "router", "configure", "codex"])

    assert result.exit_code == 0, result.output
    assert [choice.title for choice in captured["choices"]] == [
        "Reuse existing key used by Codex",
        "Reuse existing key used by Pi",
        "Create a new key",
    ]
    assert browser_calls == [("https://router.ramp.com", False)]
    assert (codex_home / "ramp-router-key").read_text() == "browser-key"


def test_configure_codex_accepts_api_key_flag_noninteractively(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)

    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "router",
            "configure",
            "codex",
            "--api-key",
            "router-secret",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["data"][0]["client"] == "codex"
    # The key reaches Codex through the auth command's file, never inline.
    assert (codex_home / "ramp-router-key").read_text() == "router-secret"
    assert tomllib.loads((codex_home / "config.toml").read_text())["model_providers"][
        "ramp-router"
    ]["auth"]["args"] == [str(codex_home / "ramp-router-key")]
    assert "router-secret" not in result.output


def test_configure_codex_rejects_empty_api_key_flag(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "codex",
            "--api-key",
            " ",
        ],
    )

    assert result.exit_code != 0
    assert "The API key cannot be empty" in result.output
    assert not (codex_home / "config.toml").exists()


def test_configure_codex_rejects_invalid_key_without_writing(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    def get(url, **kwargs):
        return httpx.Response(401, request=httpx.Request("GET", url))

    monkeypatch.setattr("ramp_cli.commands.router.httpx.get", get)
    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "codex",
            "--api-key",
            "router-secret",
        ],
    )

    assert result.exit_code != 0
    assert "wasn't accepted by Ramp Router" in result.output
    assert "https://router.ramp.com" in result.output
    assert "router-secret" not in result.output
    assert not (codex_home / "config.toml").exists()


def test_configure_waits_for_a_browser_created_key_to_reach_the_data_plane(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(router_module.shutil, "which", lambda _name: None)
    sleeps = []
    responses = [401, 401, 200, 200]

    def get(url, **kwargs):
        status = responses.pop(0)
        payload = (
            {
                "error": {
                    "message": "Invalid API key.",
                    "type": "authentication_error",
                    "code": "invalid_api_key",
                }
            }
            if status == 401
            else {"models": [{"slug": "gpt-5.4", "base_instructions": ""}]}
            if kwargs.get("headers", {}).get("X-Gateway-Client") == "codex"
            else {
                "data": [
                    {
                        "id": "gpt-5.4",
                        "router": _router_metadata("gpt-5.4"),
                    }
                ]
            }
        )
        return httpx.Response(
            status,
            json=payload,
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(router_module.httpx, "get", get)
    monkeypatch.setattr(router_module.time, "sleep", sleeps.append)

    configured = CliRunner().invoke(cli, ["--human", "router", "configure", "codex"])

    assert configured.exit_code == 0, configured.output
    assert sleeps == [0.25, 0.5]
    assert not responses
    assert (codex_home / "ramp-router-key").read_text() == "router-secret"


def test_configure_codex_refuses_malformed_existing_config(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text("this is not = valid toml")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)

    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "codex"], input="router-secret\n"
    )

    assert result.exit_code != 0
    assert "Could not read Codex config" in result.output
    assert config_path.read_text() == "this is not = valid toml"
    assert not (codex_home / "ramp-router-models.json").exists()


def test_configure_opencode_installs_plugin_and_preserves_config(tmp_path, monkeypatch):
    config_path = tmp_path / "opencode" / "opencode.json"
    config_path.parent.mkdir()
    config_path.write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "model": "existing/model",
                "autoupdate": False,
                "provider": {"existing": {"models": {"model": {}}}},
            }
        )
    )
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    _mock_models(monkeypatch, [{"id": "a"}, {"id": "b"}])

    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "opencode"],
        input="router-secret\n",
    )

    assert result.exit_code == 0
    config = json.loads(config_path.read_text())
    assert config["autoupdate"] is False
    assert config["provider"]["existing"] == {"models": {"model": {}}}
    assert "ramp-router" not in config["provider"]
    plugin_path = router_module.integration_package_path("opencode").resolve()
    expected_entry = [
        plugin_path.as_uri(),
        {
            "providerID": "ramp-router",
            "name": "Ramp Router",
            "baseURL": "https://router-api.ramp.com/v1",
            # The dashboard origin the plugin's session-usage status
            # display queries; the data plane does not serve it.
            "usageBaseURL": "https://router.ramp.com",
            "apiKey": "router-secret",
        },
    ]
    assert config["plugin"] == [expected_entry]
    assert config["model"] == "ramp-router/a"
    # OpenCode's TUI loads plugins only from tui.json, so the sidebar entry
    # must be registered there as well, with the same tuple.
    tui_path = router_module._opencode_tui_config_path()
    tui_config = json.loads(tui_path.read_text())
    assert tui_config["$schema"] == "https://opencode.ai/tui.json"
    assert tui_config["plugin"] == [expected_entry]
    assert tui_path.stat().st_mode & 0o777 == 0o600
    assert "Connecting Ramp Router to your coding agent" in result.output
    assert "Connected to: OpenCode" in result.output
    assert "2 models added. Start an agent and pick a model." in result.output
    assert "router-secret" not in result.output
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_configure_opencode_derives_usage_origin_from_base_url_override(
    tmp_path, monkeypatch
):
    """A base-URL override names a single-origin deployment serving usage too."""
    config_path = tmp_path / "opencode.json"
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    monkeypatch.setenv("RAMP_ROUTER_BASE_URL", "https://router.internal.example/v1")
    _mock_models(monkeypatch, base_url="https://router.internal.example/v1")

    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "opencode"],
        input="router-secret\n",
    )

    assert result.exit_code == 0
    options = json.loads(config_path.read_text())["plugin"][0][1]
    assert options["baseURL"] == "https://router.internal.example/v1"
    assert options["usageBaseURL"] == "https://router.internal.example"


def test_unconfigure_opencode_restores_previous_settings(tmp_path, monkeypatch):
    config_path = tmp_path / "opencode.json"
    original = {
        "model": "existing/model",
        "plugin": [
            [
                "@llm-router/opencode-provider",
                {"providerID": "router", "apiKey": "old-secret"},
            ]
        ],
        "provider": {
            "ramp-router": {"name": "Previous Router"},
            "existing": {"models": {}},
        },
    }
    config_path.write_text(json.dumps(original))
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    _mock_models(monkeypatch)
    runner = CliRunner()

    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "opencode"],
        input="router-secret\n",
    )
    tui_path = router_module._opencode_tui_config_path()
    assert tui_path.exists()
    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "opencode"])

    assert configure.exit_code == unconfigure.exit_code == 0
    assert json.loads(config_path.read_text()) == original
    assert not (tmp_path / "ramp-router-state.json").exists()
    # Configure created tui.json, so unconfigure restores its absence.
    assert not tui_path.exists()
    assert (
        "Removed Ramp Router and restored your previous settings for: OpenCode."
        in unconfigure.output
    )


def test_configure_opencode_preserves_foreign_tui_plugins(tmp_path, monkeypatch):
    config_path = tmp_path / "opencode.json"
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    tui_path = tmp_path / "custom" / "tui.json"
    tui_path.parent.mkdir()
    monkeypatch.setenv("OPENCODE_TUI_CONFIG", str(tui_path))
    tui_path.write_text(
        json.dumps(
            {
                "theme": "dark",
                "plugin": [
                    "some-other-plugin",
                    ["@llm-router/opencode-provider", {"providerID": "ramp-router"}],
                ],
            }
        )
    )
    _mock_models(monkeypatch)
    runner = CliRunner()

    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "opencode"],
        input="router-secret\n",
    )

    assert configure.exit_code == 0
    tui_config = json.loads(tui_path.read_text())
    # The user's file gains no schema rewrite and keeps unrelated plugins;
    # only the stale Router entry is replaced.
    assert "$schema" not in tui_config
    assert tui_config["theme"] == "dark"
    assert tui_config["plugin"][0] == "some-other-plugin"
    assert len(tui_config["plugin"]) == 2
    assert tui_config["plugin"][1][1]["apiKey"] == "router-secret"

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "opencode"])

    assert unconfigure.exit_code == 0
    restored = json.loads(tui_path.read_text())
    # The file predates configure, so it survives with the original Router
    # entry put back beside the untouched foreign plugin.
    assert restored["theme"] == "dark"
    assert restored["plugin"] == [
        "some-other-plugin",
        ["@llm-router/opencode-provider", {"providerID": "ramp-router"}],
    ]


def test_unconfigure_opencode_tolerates_receipt_without_tui_state(
    tmp_path, monkeypatch
):
    """A receipt from before configure wrote tui.json still unconfigures."""
    config_path = tmp_path / "opencode.json"
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "opencode"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0
    state_path = tmp_path / "ramp-router-state.json"
    state = json.loads(state_path.read_text())
    for legacy_missing in (
        "tui_file_present",
        "tui_plugin_present",
        "tui_plugin_entries",
    ):
        state.pop(legacy_missing)
    state_path.write_text(json.dumps(state) + "\n")
    tui_path = router_module._opencode_tui_config_path()
    tui_config = json.loads(tui_path.read_text())
    tui_config["plugin"].insert(0, "user-added-plugin")
    tui_path.write_text(json.dumps(tui_config) + "\n")

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "opencode"])

    assert unconfigure.exit_code == 0
    restored = json.loads(tui_path.read_text())
    # Without a recorded snapshot, only the Router entries this CLI
    # recognizes are stripped; everything else stays exactly as found.
    assert restored["plugin"] == ["user-added-plugin"]
    assert tui_path.exists()


def test_configure_opencode_accepts_jsonc(tmp_path, monkeypatch):
    config_path = tmp_path / "opencode.json"
    config_path.write_text(
        """{
  // OpenCode supports comments and trailing commas.
  "model": "existing/model",
  "autoupdate": false,
}
"""
    )
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    _mock_models(monkeypatch)

    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "opencode"],
        input="router-secret\n",
    )

    assert result.exit_code == 0
    config = json.loads(config_path.read_text())
    assert config["autoupdate"] is False
    assert config["model"] == "ramp-router/gpt-5.4"


def test_unconfigure_opencode_keeps_newer_model_selection(tmp_path, monkeypatch):
    config_path = tmp_path / "opencode.json"
    config_path.write_text(json.dumps({"model": "existing/model"}))
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "opencode"],
        input="router-secret\n",
    )
    config = json.loads(config_path.read_text())
    config["model"] = "new/provider-model"
    config_path.write_text(json.dumps(config))

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "opencode"])

    assert configure.exit_code == unconfigure.exit_code == 0
    assert json.loads(config_path.read_text())["model"] == "new/provider-model"


def test_configure_pi_installs_plugin_and_is_idempotent(tmp_path, monkeypatch):
    pi_home = tmp_path / "pi"
    pi_home.mkdir()
    config_path = pi_home / "models.json"
    config_path.write_text(
        json.dumps({"providers": {"existing": {"baseUrl": "https://example.com"}}})
    )
    settings_path = pi_home / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "defaultProvider": "existing",
                "defaultModel": "existing-model",
                "theme": "light",
            }
        )
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_home))
    _mock_models(monkeypatch, [{"id": "a"}, {"id": "b"}])
    runner = CliRunner()

    first = runner.invoke(
        cli,
        ["--human", "router", "configure", "pi"],
        input="router-secret\n",
    )
    first_content = settings_path.read_text()
    second = runner.invoke(
        cli,
        ["--human", "router", "configure", "pi"],
        input="router-secret\n",
    )

    assert first.exit_code == second.exit_code == 0
    assert settings_path.read_text() == first_content
    config = json.loads(config_path.read_text())
    assert config["providers"]["existing"] == {"baseUrl": "https://example.com"}
    assert "ramp-router" not in config["providers"]
    plugin_path = router_module.integration_package_path("pi").resolve()
    assert json.loads(settings_path.read_text()) == {
        "defaultProvider": "ramp-router",
        "defaultModel": "a",
        "theme": "light",
        "packages": [str(plugin_path)],
    }
    assert json.loads((pi_home / "auth.json").read_text()) == {
        "ramp-router": {"type": "api_key", "key": "router-secret"}
    }
    assert "Connecting Ramp Router to your coding agent" in first.output
    assert "Connected to: Pi" in first.output
    assert "2 models added. Start an agent and pick a model." in first.output


def test_unconfigure_pi_removes_router_and_preserves_other_providers(
    tmp_path, monkeypatch
):
    pi_home = tmp_path / "pi"
    pi_home.mkdir()
    original_models = {"providers": {"existing": {"baseUrl": "https://example.com"}}}
    original_settings = {
        "defaultProvider": "existing",
        "defaultModel": "existing-model",
        "theme": "dark",
        "packages": ["npm:other-package", "/old/pi-provider"],
    }
    original_auth = {
        "ramp-router": {"type": "api_key", "key": "old-secret"},
        "existing": {"type": "api_key", "key": "existing-secret"},
    }
    (pi_home / "models.json").write_text(json.dumps(original_models))
    (pi_home / "settings.json").write_text(json.dumps(original_settings))
    (pi_home / "auth.json").write_text(json.dumps(original_auth))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_home))
    _mock_models(monkeypatch)
    runner = CliRunner()

    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "pi"],
        input="router-secret\n",
    )
    (pi_home / "ramp-router-model-cache.json").write_text("cached")
    (pi_home / "ramp-router-model-cache-key").write_text("cache-key")
    (pi_home / "ramp-router-runtime-models.json").write_text("runtime")
    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "pi"])

    assert configure.exit_code == unconfigure.exit_code == 0
    assert json.loads((pi_home / "models.json").read_text()) == original_models
    assert json.loads((pi_home / "settings.json").read_text()) == original_settings
    assert json.loads((pi_home / "auth.json").read_text()) == original_auth
    assert not (pi_home / "ramp-router-state.json").exists()
    assert not (pi_home / "ramp-router-model-cache.json").exists()
    assert not (pi_home / "ramp-router-model-cache-key").exists()
    assert not (pi_home / "ramp-router-runtime-models.json").exists()
    assert (
        "Removed Ramp Router and restored your previous settings for: Pi."
        in unconfigure.output
    )


def test_unconfigure_pi_keeps_newer_default_selection(tmp_path, monkeypatch):
    pi_home = tmp_path / "pi"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_home))
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "pi"],
        input="router-secret\n",
    )
    settings_path = pi_home / "settings.json"
    settings = json.loads(settings_path.read_text())
    settings.update({"defaultProvider": "new", "defaultModel": "new-model"})
    settings_path.write_text(json.dumps(settings))

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "pi"])

    assert configure.exit_code == unconfigure.exit_code == 0
    assert json.loads(settings_path.read_text()) == {
        "defaultProvider": "new",
        "defaultModel": "new-model",
    }


def test_unconfigure_pi_keeps_newer_auth_and_provider(tmp_path, monkeypatch):
    pi_home = tmp_path / "pi"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_home))
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "pi"],
        input="configured-secret\n",
    )
    assert configure.exit_code == 0

    auth_path = pi_home / "auth.json"
    auth = json.loads(auth_path.read_text())
    auth["ramp-router"] = {"type": "api_key", "key": "newer-login-secret"}
    auth_path.write_text(json.dumps(auth))

    models_path = pi_home / "models.json"
    models_path.write_text(
        json.dumps(
            {
                "providers": {
                    "newer-provider": {
                        "baseUrl": "https://newer-provider.example/v1",
                        "models": [],
                    },
                    "ramp-router": {
                        "baseUrl": "https://newer-router.example/v1",
                        "models": [],
                    },
                }
            }
        )
    )

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "pi"])

    assert unconfigure.exit_code == 0
    assert json.loads(auth_path.read_text())["ramp-router"] == {
        "type": "api_key",
        "key": "newer-login-secret",
    }
    assert json.loads(models_path.read_text())["providers"]["ramp-router"] == {
        "baseUrl": "https://newer-router.example/v1",
        "models": [],
    }
    assert json.loads(models_path.read_text())["providers"]["newer-provider"] == {
        "baseUrl": "https://newer-provider.example/v1",
        "models": [],
    }


def test_unconfigure_pi_fails_closed_for_legacy_auth_state(tmp_path, monkeypatch):
    pi_home = tmp_path / "pi"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_home))
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "pi"],
        input="configured-secret\n",
    )
    assert configure.exit_code == 0

    state_path = pi_home / "ramp-router-state.json"
    state = json.loads(state_path.read_text())
    state.pop("configured_auth_marker")
    state["auth_present"] = True
    state["auth"] = {"type": "api_key", "key": "original-secret"}
    state_path.write_text(json.dumps(state))
    settings_before = (pi_home / "settings.json").read_text()
    auth_before = (pi_home / "auth.json").read_text()

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "pi"])

    assert unconfigure.exit_code != 0
    assert "legacy Pi credentials" in unconfigure.output
    assert (pi_home / "settings.json").read_text() == settings_before
    assert (pi_home / "auth.json").read_text() == auth_before
    assert state_path.exists()


def test_unconfigure_pi_restores_original_auth_after_reconfigure(tmp_path, monkeypatch):
    pi_home = tmp_path / "pi"
    pi_home.mkdir()
    auth_path = pi_home / "auth.json"
    original_auth = {"ramp-router": {"type": "api_key", "key": "original-secret"}}
    auth_path.write_text(json.dumps(original_auth))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_home))
    _mock_models(monkeypatch)
    runner = CliRunner()

    first = runner.invoke(
        cli,
        ["--human", "router", "configure", "pi"],
        input="first-configured-secret\n",
    )
    second = runner.invoke(
        cli,
        ["--human", "router", "configure", "pi"],
        input="second-configured-secret\n",
    )
    assert (
        "second-configured-secret"
        not in (pi_home / "ramp-router-state.json").read_text()
    )
    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "pi"])

    assert first.exit_code == second.exit_code == unconfigure.exit_code == 0
    assert json.loads(auth_path.read_text()) == original_auth


def _capture_picker(monkeypatch, answer):
    """Record the choices the picker is built with instead of drawing it."""
    captured = {}

    class Prompt:
        def ask(self):
            return answer

    def checkbox(message, **kwargs):
        captured["message"] = message
        captured.update(kwargs)
        return Prompt()

    monkeypatch.setattr(router_module.questionary, "checkbox", checkbox)
    return captured


def _notice_lines(output):
    """The text inside the NOTICE box, with its frame and padding removed."""
    lines = output.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("┌─── NOTICE"))
    end = next(i for i, line in enumerate(lines) if line.startswith("└"))
    return [line[1:-1].strip() for line in lines[start + 1 : end]]


def _write_codex_session(directory, name, session_id, provider):
    directory.mkdir(parents=True, exist_ok=True)
    transcript = directory / name
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": session_id, "model_provider": provider},
            }
        )
        + "\n"
    )
    return transcript


def _codex_session_provider(transcript):
    return json.loads(transcript.read_text().splitlines()[0])["payload"][
        "model_provider"
    ]


def test_unconfigure_moves_router_created_conversations_to_the_restored_provider(
    tmp_path, monkeypatch
):
    # A conversation started while Router was configured is tagged with Router
    # at birth, so it is absent from the receipt and vanishes the moment the
    # provider goes back. Nothing else can bring it back.
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model_provider = "openai"\n')
    existing = _write_codex_session(
        codex_home / "sessions", "before.jsonl", "before", "openai"
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    runner = CliRunner()

    configured = runner.invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )
    assert configured.exit_code == 0, configured.output
    assert _codex_session_provider(existing) == "ramp-router"
    during = _write_codex_session(
        codex_home / "sessions", "during.jsonl", "during", "ramp-router"
    )
    archived = _write_codex_session(
        codex_home / "archived_sessions", "filed.jsonl", "filed", "ramp-router"
    )

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert removed.exit_code == 0, removed.output
    assert _codex_session_provider(existing) == "openai"
    assert _codex_session_provider(during) == "openai"
    assert _codex_session_provider(archived) == "openai"


def test_unconfigure_preserves_router_conversations_that_predate_configure(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model_provider = "openai"\n')
    preexisting = _write_codex_session(
        codex_home / "sessions", "preexisting.jsonl", "preexisting", "ramp-router"
    )
    database_path = codex_home / "state_5.sqlite"
    with sqlite3.connect(database_path) as database:
        database.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT NOT NULL)"
        )
        database.execute(
            "INSERT INTO threads VALUES (?, ?)", ("preexisting", "ramp-router")
        )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    runner = CliRunner()

    configured = runner.invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )
    assert configured.exit_code == 0, configured.output
    during = _write_codex_session(
        codex_home / "sessions", "during.jsonl", "during", "ramp-router"
    )

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert removed.exit_code == 0, removed.output
    assert _codex_session_provider(preexisting) == "ramp-router"
    assert _codex_session_provider(during) == "openai"
    with sqlite3.connect(database_path) as database:
        assert database.execute(
            "SELECT model_provider FROM threads WHERE id = 'preexisting'"
        ).fetchone() == ("ramp-router",)


def test_legacy_receipt_preserves_unrecorded_router_conversations(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    runner = CliRunner()
    configured = runner.invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )
    assert configured.exit_code == 0, configured.output
    state_path = codex_home / "ramp-router-state.json"
    state = json.loads(state_path.read_text())
    state.pop(router_module.CODEX_PREEXISTING_ROUTER_SESSIONS_STATE_KEY)
    state_path.write_text(json.dumps(state) + "\n")
    unknown = _write_codex_session(
        codex_home / "sessions", "unknown.jsonl", "unknown", "ramp-router"
    )

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert removed.exit_code == 0, removed.output
    assert _codex_session_provider(unknown) == "ramp-router"


def test_failed_reclamation_keeps_the_receipt_so_it_can_be_retried(
    tmp_path, monkeypatch
):
    # A locked session index is the ordinary cause and it clears on its own.
    # Dropping the receipt would leave those conversations hidden for good.
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model_provider = "openai"\n')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    runner = CliRunner()
    assert (
        runner.invoke(
            cli,
            ["--human", "router", "configure", "codex", "--api-key", "router-secret"],
        ).exit_code
        == 0
    )
    during = _write_codex_session(
        codex_home / "sessions", "during.jsonl", "during", "ramp-router"
    )
    failures = {"count": 0}
    real = router_module._reclaim_codex_router_sessions

    def flaky(home, sessions, provider, *, preexisting_router_sessions=()):
        failures["count"] += 1
        if failures["count"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(
            home,
            sessions,
            provider,
            preexisting_router_sessions=preexisting_router_sessions,
        )

    monkeypatch.setattr(router_module, "_reclaim_codex_router_sessions", flaky)

    first = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert first.exit_code != 0
    assert "run this command again" in first.output
    assert (codex_home / "ramp-router-state.json").exists()
    assert _codex_session_provider(during) == "ramp-router"

    second = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert second.exit_code == 0, second.output
    assert not (codex_home / "ramp-router-state.json").exists()
    assert _codex_session_provider(during) == "openai"


def test_reclaimed_conversations_follow_the_provider_actually_restored(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model_provider = "my-own-gateway"\n')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    runner = CliRunner()
    assert (
        runner.invoke(
            cli,
            ["--human", "router", "configure", "codex", "--api-key", "router-secret"],
        ).exit_code
        == 0
    )
    during = _write_codex_session(
        codex_home / "sessions", "during.jsonl", "during", "ramp-router"
    )

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert removed.exit_code == 0, removed.output
    assert _codex_session_provider(during) == "my-own-gateway"


def test_reclaiming_leaves_a_recorded_session_the_user_changed_alone(
    tmp_path, monkeypatch
):
    # Restoration declines a recorded session whose provider the user changed.
    # Sweeping it afterwards would override the very choice that protected it.
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model_provider = "openai"\n')
    recorded = _write_codex_session(
        codex_home / "sessions", "recorded.jsonl", "recorded", "openai"
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    runner = CliRunner()
    assert (
        runner.invoke(
            cli,
            ["--human", "router", "configure", "codex", "--api-key", "router-secret"],
        ).exit_code
        == 0
    )
    assert _codex_session_provider(recorded) == "ramp-router"

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert removed.exit_code == 0, removed.output
    # Its own captured provider, not the root default the sweep would apply.
    assert _codex_session_provider(recorded) == "openai"


def test_unconfigure_reclaims_router_rows_left_in_the_session_index(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model_provider = "openai"\n')
    database_path = codex_home / "state_5.sqlite"
    with sqlite3.connect(database_path) as database:
        database.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT NOT NULL)"
        )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    runner = CliRunner()
    assert (
        runner.invoke(
            cli,
            ["--human", "router", "configure", "codex", "--api-key", "router-secret"],
        ).exit_code
        == 0
    )
    # The index also holds threads whose transcript is gone, so it is swept in
    # its own right rather than only alongside a rewritten file.
    with sqlite3.connect(database_path) as database:
        database.execute("INSERT INTO threads VALUES (?, ?)", ("during", "ramp-router"))

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert removed.exit_code == 0, removed.output
    with sqlite3.connect(database_path) as database:
        assert database.execute(
            "SELECT model_provider FROM threads WHERE id = 'during'"
        ).fetchone() == ("openai",)


def test_claude_escape_command_is_written_to_be_typed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))

    # A quoted tilde is a literal one, so the shell would look for a directory
    # named "~". Only the part after it may be quoted.
    assert claude_code.original_settings_command(claude_code.settings_path()) == (
        "claude --settings ~/.claude/original.settings.json"
    )
    assert claude_code.original_settings_command(
        claude_code.settings_path(), use_default_model=True
    ) == ("claude --settings ~/.claude/original.settings.json --model default")

    spaced = tmp_path / "My Settings"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(spaced))
    assert claude_code.original_settings_command(claude_code.settings_path()) == (
        "claude --settings ~/'My Settings/original.settings.json'"
    )

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/etc/claude")
    assert claude_code.original_settings_command(claude_code.settings_path()) == (
        "claude --settings /etc/claude/original.settings.json"
    )


def test_no_notice_for_agents_that_keep_their_own_default(tmp_path, monkeypatch):
    # OpenCode and Pi take a provider without giving up their default, so
    # there is nothing displaced to warn about and no profile to offer.
    pi_home = tmp_path / "pi"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_home))
    _mock_models(monkeypatch)

    configured = CliRunner().invoke(
        cli, ["--human", "router", "configure", "pi", "--api-key", "router-secret"]
    )

    assert configured.exit_code == 0, configured.output
    assert "NOTICE" not in configured.output
    assert (
        "Run 'ramp router unconfigure pi' to restore the previous settings."
        in configured.output
    )


def test_notice_reports_the_cost_of_taking_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    _mock_models(monkeypatch)

    configured = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "claude-code", "--api-key", "router-secret"],
    )

    assert configured.exit_code == 0, configured.output
    notice = _notice_lines(configured.output)
    text = " ".join(notice)
    assert "Claude Code only supports one model provider at a time" in text
    assert (
        "claude --settings ~/.claude/original.settings.json --model default" in notice
    )
    assert "ramp router unconfigure claude-code" in notice
    # Codex was not configured, so its profile is not advertised.
    assert "codex --profile original" not in notice
    # The consequence to leave with, so it closes the notice.
    assert "claude.ai connectors" in notice[-2]


def test_notice_is_not_printed_in_agent_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    _mock_models(monkeypatch)

    configured = CliRunner().invoke(
        cli,
        ["--agent", "router", "configure", "claude-code", "--api-key", "router-secret"],
    )

    assert configured.exit_code == 0, configured.output
    assert "NOTICE" not in configured.output
    # The status line notice shares this stream, so take the JSON document.
    document = configured.output[configured.output.index("{") :]
    payload = json.loads(document)["data"][0]
    assert payload["original_setup_command"].startswith("claude --settings ")


def test_codex_original_profile_runs_the_setup_router_replaced(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model = "gpt-5.4"\nmodel_provider = "openai"\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    _mock_codex_catalog(monkeypatch)
    runner = CliRunner()

    configured = runner.invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )

    assert configured.exit_code == 0, configured.output
    profile = codex_home / "original.config.toml"
    profile_config = tomllib.loads(profile.read_text())
    assert profile_config["model"] == "gpt-5.4"
    assert profile_config["model_provider"] == "openai"
    assert profile_config["model_catalog_json"] == str(
        codex_home / router_module.CODEX_ORIGINAL_CATALOG
    )
    # Router owns the default, which is the whole point of the profile.
    assert tomllib.loads((codex_home / "config.toml").read_text())[
        "model_provider"
    ] == ("ramp-router")
    assert "codex --profile original" in configured.output

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert removed.exit_code == 0, removed.output
    assert not profile.exists()
    restored = tomllib.loads((codex_home / "config.toml").read_text())
    assert restored["model"] == "gpt-5.4"
    assert restored["model_provider"] == "openai"


def test_codex_original_profile_uses_a_provider_specific_model_catalog(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model = "gpt-5.4"\nmodel_provider = "openai"\n'
    )
    # This is the upstream Codex failure mode: Router was the last provider to
    # refresh the global cache, even though the selected profile says OpenAI.
    (codex_home / "models_cache.json").write_text(
        json.dumps(
            {
                "client_version": "0.147.0",
                "models": [{"slug": "accounts/fireworks/models/kimi-k3"}],
            }
        )
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    monkeypatch.setattr(router_module.shutil, "which", lambda _name: "/usr/bin/codex")

    native_catalog = {
        "models": [
            {
                "slug": "gpt-5.4",
                "base_instructions": "THE NATIVE CODEX PROMPT",
            }
        ]
    }

    def debug_models(args, **_kwargs):
        if "-c" in args:
            assert 'model_provider="openai"' in args
            assert "--bundled" in args
        return subprocess.CompletedProcess(args, 0, json.dumps(native_catalog), "")

    monkeypatch.setattr(router_module.subprocess, "run", debug_models)
    runner = CliRunner()

    configured = runner.invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )

    assert configured.exit_code == 0, configured.output
    catalog_path = codex_home / router_module.CODEX_ORIGINAL_CATALOG
    profile = tomllib.loads((codex_home / "original.config.toml").read_text())
    assert profile["model_provider"] == "openai"
    assert profile["model_catalog_json"] == str(catalog_path)
    catalog = json.loads(catalog_path.read_text())
    assert [model["slug"] for model in catalog["models"]] == ["gpt-5.4"]
    assert "kimi" not in catalog_path.read_text()

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert removed.exit_code == 0, removed.output
    assert not catalog_path.exists()


def test_reconfigure_creates_an_isolated_profile_once_codex_is_available(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model_provider = "openai"\n')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    runner = CliRunner()

    # Preparing Codex before it is installed cannot capture an isolated model
    # catalog, so the first run must not advertise a leaky profile.
    monkeypatch.setattr(router_module.shutil, "which", lambda _name: None)
    first = runner.invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )
    assert first.exit_code == 0, first.output
    assert not (codex_home / "original.config.toml").exists()
    assert "codex --profile original" not in first.output

    _mock_codex_catalog(monkeypatch)

    again = runner.invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )

    assert again.exit_code == 0, again.output
    profile = tomllib.loads((codex_home / "original.config.toml").read_text())
    assert profile["model_catalog_json"] == str(
        codex_home / router_module.CODEX_ORIGINAL_CATALOG
    )


def test_codex_does_not_replace_a_model_catalog_of_the_users_own(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    mine = codex_home / router_module.CODEX_ORIGINAL_CATALOG
    mine.write_text('{"models": [{"slug": "my-private-model"}]}\n')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    monkeypatch.setattr(router_module.shutil, "which", lambda _name: "/usr/bin/codex")
    native = {"models": [{"slug": "gpt-5.4", "base_instructions": "NATIVE"}]}
    monkeypatch.setattr(
        router_module.subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args, 0, json.dumps(native), ""
        ),
    )

    configured = CliRunner().invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )

    assert configured.exit_code == 0, configured.output
    assert json.loads(mine.read_text())["models"][0]["slug"] == "my-private-model"
    assert not (codex_home / "original.config.toml").exists()
    assert "codex --profile original" not in configured.output


def test_codex_original_profile_does_not_inherit_router_instructions(
    tmp_path, monkeypatch
):
    # Router writes model_instructions_file at the root, and a profile cannot
    # blank it: Codex resolves an empty value to CODEX_HOME and refuses to
    # start. Left unanswered, the escape profile loads Router's file.
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model = "gpt-5.4"\n')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch, [{"id": "router-model"}])
    monkeypatch.setattr(router_module.shutil, "which", lambda _name: "/usr/bin/codex")
    catalog = {
        "models": [
            {"slug": "router-model", "base_instructions": "PROMPT FOR ROUTER MODEL"},
            {"slug": "gpt-5.4", "base_instructions": "PROMPT FOR THE ORIGINAL MODEL"},
        ]
    }
    monkeypatch.setattr(
        router_module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(catalog), ""),
    )
    runner = CliRunner()

    configured = runner.invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )

    assert configured.exit_code == 0, configured.output
    instructions = codex_home / "ramp-router-original-instructions.md"
    profile = tomllib.loads((codex_home / "original.config.toml").read_text())
    assert profile["model_instructions_file"] == str(instructions)
    # Its own model's prompt, not the one Router configured.
    assert instructions.read_text() == "PROMPT FOR THE ORIGINAL MODEL"
    assert (codex_home / "ramp-router-instructions.md").read_text() == (
        "PROMPT FOR ROUTER MODEL"
    )

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert removed.exit_code == 0, removed.output
    assert not instructions.exists()


def test_codex_original_profile_keeps_an_instructions_file_of_the_users_own(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model = "gpt-5.4"\nmodel_instructions_file = "/my/own/prompt.md"\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    monkeypatch.setattr(router_module.shutil, "which", lambda _name: "/usr/bin/codex")
    monkeypatch.setattr(
        router_module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a,
            0,
            json.dumps({"models": [{"slug": "gpt-5.4", "base_instructions": "p"}]}),
            "",
        ),
    )

    configured = CliRunner().invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )

    assert configured.exit_code == 0, configured.output
    profile = tomllib.loads((codex_home / "original.config.toml").read_text())
    assert profile["model_instructions_file"] == "/my/own/prompt.md"
    assert not (codex_home / "ramp-router-original-instructions.md").exists()


def test_unconfigure_keeps_an_unowned_file_at_the_managed_instructions_name(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    instructions = codex_home / router_module.CODEX_ORIGINAL_INSTRUCTIONS
    instructions.write_text("MY OWN INSTRUCTIONS")
    (codex_home / "config.toml").write_text(
        f'model = "gpt-5.4"\nmodel_instructions_file = "{instructions}"\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    _mock_codex_catalog(monkeypatch)
    runner = CliRunner()

    configured = runner.invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )
    assert configured.exit_code == 0, configured.output
    state = json.loads((codex_home / "ramp-router-state.json").read_text())
    assert "original_instructions" not in state

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert removed.exit_code == 0, removed.output
    assert instructions.read_text() == "MY OWN INSTRUCTIONS"


def test_codex_original_profile_names_the_builtin_provider_when_none_was_set(
    tmp_path, monkeypatch
):
    # A profile overrides only the keys it states, so a config that never named
    # a provider would otherwise inherit the Router one it exists to escape.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    _mock_models(monkeypatch)
    _mock_codex_catalog(monkeypatch)

    configured = CliRunner().invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )

    assert configured.exit_code == 0, configured.output
    profile = tmp_path / "codex" / "original.config.toml"
    profile_config = tomllib.loads(profile.read_text())
    assert profile_config["model_provider"] == "openai"
    assert "model" not in profile_config
    assert profile_config["model_catalog_json"] == str(
        tmp_path / "codex" / router_module.CODEX_ORIGINAL_CATALOG
    )


def test_codex_never_replaces_or_removes_a_profile_of_the_users_own(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    mine = codex_home / "original.config.toml"
    mine.write_text('model = "my-own-profile"\n')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    runner = CliRunner()

    configured = runner.invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )
    assert configured.exit_code == 0, configured.output
    assert mine.read_text() == 'model = "my-own-profile"\n'
    assert "codex --profile original" not in configured.output

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert removed.exit_code == 0, removed.output
    assert mine.read_text() == 'model = "my-own-profile"\n'


def test_unconfigure_keeps_an_escape_profile_the_user_edited(tmp_path, monkeypatch):
    # The receipt proves the CLI created the file, not that it still holds
    # what the CLI put there.
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model = "gpt-5.4"\n')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    monkeypatch.setattr(router_module.shutil, "which", lambda _name: "/usr/bin/codex")
    catalog = {"models": [{"slug": "gpt-5.4", "base_instructions": "NATIVE PROMPT"}]}
    monkeypatch.setattr(
        router_module.subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args, 0, json.dumps(catalog), ""
        ),
    )
    runner = CliRunner()
    assert (
        runner.invoke(
            cli,
            ["--human", "router", "configure", "codex", "--api-key", "router-secret"],
        ).exit_code
        == 0
    )
    profile = codex_home / "original.config.toml"
    profile.write_text(profile.read_text() + 'model_reasoning_effort = "high"\n')

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert removed.exit_code == 0, removed.output
    assert "model_reasoning_effort" in profile.read_text()

    configured_again = runner.invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )

    assert configured_again.exit_code == 0, configured_again.output
    assert "codex --profile original" in configured_again.output
    assert "model_reasoning_effort" in profile.read_text()


@pytest.mark.parametrize("unsafe_edit", ["router-provider", "missing-catalog"])
def test_configure_does_not_advertise_an_edited_unsafe_profile(
    tmp_path, monkeypatch, unsafe_edit
):
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    _mock_codex_catalog(monkeypatch)
    runner = CliRunner()
    first = runner.invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )
    assert first.exit_code == 0, first.output
    profile = codex_home / "original.config.toml"
    contents = profile.read_text()
    if unsafe_edit == "router-provider":
        contents = contents.replace(
            'model_provider = "openai"', 'model_provider = "ramp-router"'
        )
    else:
        contents = "".join(
            line
            for line in contents.splitlines(keepends=True)
            if not line.startswith("model_catalog_json = ")
        )
    profile.write_text(contents)

    configured_again = runner.invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )

    assert configured_again.exit_code == 0, configured_again.output
    assert "codex --profile original" not in configured_again.output


def test_unconfigure_keeps_an_escape_overlay_the_user_edited(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    _mock_models(monkeypatch)
    runner = CliRunner()
    assert (
        runner.invoke(
            cli,
            [
                "--human",
                "router",
                "configure",
                "claude-code",
                "--api-key",
                "router-secret",
            ],
        ).exit_code
        == 0
    )
    overlay = claude_home / "original.settings.json"
    edited = json.loads(overlay.read_text())
    edited["env"]["MY_OWN_SETTING"] = "keep me"
    overlay.write_text(json.dumps(edited, indent=2) + "\n")

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "claude-code"])

    assert removed.exit_code == 0, removed.output
    assert json.loads(overlay.read_text())["env"]["MY_OWN_SETTING"] == "keep me"


def test_configure_rewrites_an_escape_artifact_that_went_missing(tmp_path, monkeypatch):
    # The receipt names an artifact that is no longer on disk, which is what a
    # user who deleted it by hand is left with. The next configure has to put
    # it back rather than keep advertising a file that is not there.
    claude_home = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    _mock_codex_catalog(monkeypatch)
    runner = CliRunner()
    assert (
        runner.invoke(
            cli,
            [
                "--human",
                "router",
                "configure",
                "claude-code",
                "codex",
                "--api-key",
                "router-secret",
            ],
        ).exit_code
        == 0
    )
    overlay = claude_home / "original.settings.json"
    profile = codex_home / "original.config.toml"
    overlay.unlink()
    profile.unlink()

    again = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "claude-code",
            "codex",
            "--api-key",
            "router-secret",
        ],
    )

    assert again.exit_code == 0, again.output
    assert overlay.is_file()
    assert profile.is_file()
    notice = _notice_lines(again.output)
    assert "codex --profile original" in notice
    assert any(line.startswith("claude --settings") for line in notice)


def test_configure_does_not_claim_a_matching_profile_without_provenance(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model = "gpt-5.4"\n')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    _mock_codex_catalog(monkeypatch)
    runner = CliRunner()
    assert (
        runner.invoke(
            cli,
            ["--human", "router", "configure", "codex", "--api-key", "router-secret"],
        ).exit_code
        == 0
    )
    profile = codex_home / "original.config.toml"
    body = profile.read_text()
    catalog = codex_home / router_module.CODEX_ORIGINAL_CATALOG
    instructions = codex_home / router_module.CODEX_ORIGINAL_INSTRUCTIONS
    receipt_path = codex_home / "ramp-router-state.json"
    receipt = json.loads(receipt_path.read_text())
    for key in (
        router_module.CODEX_ORIGINAL_PROFILE_STATE_KEY,
        router_module.CODEX_ORIGINAL_PROFILE_DIGEST_KEY,
        router_module.CODEX_ORIGINAL_CATALOG_STATE_KEY,
        router_module.CODEX_ORIGINAL_CATALOG_DIGEST_KEY,
        router_module.CODEX_ORIGINAL_INSTRUCTIONS_STATE_KEY,
        router_module.CODEX_ORIGINAL_INSTRUCTIONS_DIGEST_KEY,
    ):
        receipt.pop(key)
    receipt_path.write_text(json.dumps(receipt) + "\n")

    again = runner.invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )

    assert again.exit_code == 0, again.output
    assert profile.read_text() == body
    updated_receipt = json.loads(receipt_path.read_text())
    assert router_module.CODEX_ORIGINAL_PROFILE_STATE_KEY not in updated_receipt
    assert router_module.CODEX_ORIGINAL_CATALOG_STATE_KEY not in updated_receipt
    assert router_module.CODEX_ORIGINAL_INSTRUCTIONS_STATE_KEY not in updated_receipt
    assert "codex --profile original" in again.output

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert removed.exit_code == 0, removed.output
    assert profile.exists()
    assert catalog.exists()
    assert instructions.exists()


def test_claude_original_settings_restore_the_previous_provider(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude"
    claude_home.mkdir()
    (claude_home / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-mine"}, "model": "opus"})
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    _mock_models(monkeypatch)
    runner = CliRunner()

    configured = runner.invoke(
        cli,
        ["--human", "router", "configure", "claude-code", "--api-key", "router-secret"],
    )

    assert configured.exit_code == 0, configured.output
    overlay = json.loads((claude_home / "original.settings.json").read_text())
    assert overlay["env"]["ANTHROPIC_API_KEY"] == "sk-mine"
    assert overlay["model"] == "opus"
    # Keys Router introduced are blanked rather than omitted: an omitted key
    # would inherit the Router value from the settings file this overlays.
    assert overlay["env"]["ANTHROPIC_BASE_URL"] == ""
    assert overlay["env"]["ANTHROPIC_AUTH_TOKEN"] == ""
    assert overlay["env"]["ENABLE_CLAUDEAI_MCP_SERVERS"] == ""
    assert "disableClaudeAiConnectors" not in overlay
    assert "router-secret" not in json.dumps(overlay)
    assert "--model default" not in configured.output

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "claude-code"])

    assert removed.exit_code == 0, removed.output
    assert not (claude_home / "original.settings.json").exists()


def test_configure_does_not_claim_a_matching_claude_overlay_without_provenance(
    tmp_path, monkeypatch
):
    claude_home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    _mock_models(monkeypatch)
    runner = CliRunner()
    first = runner.invoke(
        cli,
        ["--human", "router", "configure", "claude-code", "--api-key", "router-secret"],
    )
    assert first.exit_code == 0, first.output
    overlay = claude_home / "original.settings.json"
    original_body = overlay.read_text()
    state_path = claude_home / "ramp-router-state.json"
    state = json.loads(state_path.read_text())
    state.pop(claude_code.ORIGINAL_SETTINGS_STATE_KEY)
    state.pop(claude_code.ORIGINAL_SETTINGS_DIGEST_KEY)
    state_path.write_text(json.dumps(state) + "\n")

    again = runner.invoke(
        cli,
        ["--human", "router", "configure", "claude-code", "--api-key", "router-secret"],
    )

    assert again.exit_code == 0, again.output
    assert overlay.read_text() == original_body
    assert claude_code.ORIGINAL_SETTINGS_STATE_KEY not in json.loads(
        state_path.read_text()
    )
    assert "claude --settings" in again.output
    assert "--model default" in again.output

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "claude-code"])

    assert removed.exit_code == 0, removed.output
    assert overlay.read_text() == original_body


def test_claude_original_settings_are_not_taken_from_a_previous_configure(
    tmp_path, monkeypatch
):
    # A repeat configure must keep describing the pre-Router setup. Planning it
    # from the current settings would capture Router's own values instead.
    claude_home = tmp_path / "claude"
    claude_home.mkdir()
    (claude_home / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-mine"}})
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    _mock_models(monkeypatch)
    runner = CliRunner()
    overlay_path = claude_home / "original.settings.json"
    first = runner.invoke(
        cli,
        ["--human", "router", "configure", "claude-code", "--api-key", "router-secret"],
    )
    assert first.exit_code == 0, first.output
    overlay_path.unlink()

    again = runner.invoke(
        cli,
        ["--human", "router", "configure", "claude-code", "--api-key", "router-secret"],
    )

    assert again.exit_code == 0, again.output
    # Rebuilt from the receipt's snapshot, so it still describes the setup that
    # existed before Router rather than the one Router installed.
    overlay = json.loads(overlay_path.read_text())
    assert overlay["env"]["ANTHROPIC_API_KEY"] == "sk-mine"
    assert overlay["env"]["ANTHROPIC_BASE_URL"] == ""
    assert "model" not in overlay
    assert "router-secret" not in json.dumps(overlay)


def test_client_picker_preselects_every_installed_agent(monkeypatch):
    captured = _capture_picker(monkeypatch, ["codex", "pi"])
    monkeypatch.setattr(
        router_module, "_installed_clients", lambda: ("claude-code", "codex", "pi")
    )

    assert router_module._pick_installed_clients() == ("codex", "pi")
    assert (
        captured["message"]
        == "Choose the agents you want to configure with Ramp Router"
    )
    # OpenCode is not installed, so offering it would only write configuration
    # into a directory for an agent that cannot read it.
    assert [
        (choice.title, choice.value, choice.checked) for choice in captured["choices"]
    ] == [
        ("Claude Code", "claude-code", True),
        ("Codex", "codex", True),
        ("Pi", "pi", True),
    ]
    assert captured["validate"](["codex"]) is True
    assert captured["validate"]([]) == "Select at least one coding agent."


def test_configure_without_a_terminal_targets_every_agent(tmp_path, monkeypatch):
    # An installer or CI job supplies the key through the environment without
    # passing --no-input. Drawing a full-screen picker there aborts the command
    # instead of configuring anything.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(
        router_module,
        "_pick_installed_clients",
        lambda: pytest.fail("a picker cannot be drawn without a terminal"),
    )
    _mock_models(monkeypatch)

    configure = CliRunner().invoke(
        cli, ["--human", "router", "configure", "--api-key", "router-secret"]
    )

    assert configure.exit_code == 0, configure.output
    assert "Connected to: Claude Code, Codex, OpenCode, and Pi" in configure.output


def test_the_picker_is_offered_only_when_both_streams_are_a_terminal(monkeypatch):
    context = click.Context(cli)
    context.obj = {"no_input": False}
    monkeypatch.setattr(router_module.sys, "stdin", io.StringIO())
    monkeypatch.setattr(router_module.sys, "stdout", io.StringIO())

    assert router_module._can_draw_picker(context) is False

    class _Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(router_module.sys, "stdin", _Terminal())
    monkeypatch.setattr(router_module.sys, "stdout", _Terminal())
    assert router_module._can_draw_picker(context) is True

    # Explicitly non-interactive stays non-interactive even on a terminal.
    context.obj = {"no_input": True}
    assert router_module._can_draw_picker(context) is False


def test_client_picker_offers_everything_when_no_agent_is_found(monkeypatch, capsys):
    # Detection can miss, and preparing an agent before installing it is
    # supported, so finding nothing must not leave an empty list.
    captured = _capture_picker(monkeypatch, ["pi"])
    monkeypatch.setattr(router_module, "_installed_clients", lambda: ())

    assert router_module._pick_installed_clients() == ("pi",)
    assert [choice.value for choice in captured["choices"]] == [
        "claude-code",
        "codex",
        "opencode",
        "pi",
    ]
    assert "No coding agents found" in capsys.readouterr().out


def test_claude_model_picker_offers_recommended_and_all(monkeypatch):
    captured = {}

    class Prompt:
        def ask(self):
            return "all"

    def select(message, **kwargs):
        captured["message"] = message
        captured.update(kwargs)
        return Prompt()

    monkeypatch.setattr(router_module.questionary, "select", select)

    assert router_module._pick_claude_models() == "all"
    assert captured["default"] == "compact"
    assert [choice.title for choice in captured["choices"]] == [
        "Recommended models (default)",
        "All available models",
    ]


def test_installed_clients_finds_agents_on_path_or_with_a_config_dir(
    tmp_path, monkeypatch
):
    # Codex is on PATH. Pi is installed somewhere PATH does not reach but has
    # already written its directory. The other two are absent entirely.
    (tmp_path / "pi").mkdir(parents=True)
    monkeypatch.setattr(
        router_module.shutil,
        "which",
        lambda name: "/usr/local/bin/codex" if name == "codex" else None,
    )

    assert router_module._installed_clients() == ("codex", "pi")


def test_client_picker_does_not_inverse_video_every_checked_row():
    # prompt_toolkit styles a checked row as "reverse". Every agent starts
    # checked, so inheriting that default renders the whole list as one
    # indistinguishable block of inverted text.
    rules = dict(router_module._PICKER_STYLE.style_rules)

    assert "noreverse" in rules["selected"]
    assert "noreverse" in rules["highlighted"]
    # Checked and unchecked rows have to differ by more than the indicator
    # glyph, and the cursor has to stay legible on a row that is also checked.
    assert rules["selected"] != rules["text"]
    assert "bold" in rules["pointer"]


def test_configure_picker_targets_only_selected_clients(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(
        router_module, "_pick_installed_clients", lambda: ("codex", "pi")
    )
    monkeypatch.setattr(router_module, "_can_draw_picker", lambda _ctx: True)
    _mock_models(monkeypatch)

    configure = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "--api-key", "router-secret"],
    )

    assert configure.exit_code == 0, configure.output
    assert "Connected to: Codex and Pi" in configure.output
    notice = _notice_lines(configure.output)
    # Pi keeps its own default, so the notice speaks only for Codex.
    assert notice[0].startswith("Codex only supports one model provider")
    assert not any("claude.ai connectors" in line for line in notice)
    assert "ramp router unconfigure codex pi" in notice
    assert (tmp_path / ".codex" / "ramp-router-state.json").exists()
    assert (tmp_path / ".pi" / "agent" / "ramp-router-state.json").exists()
    assert not (tmp_path / ".claude" / "settings.json").exists()
    assert not (tmp_path / ".config" / "opencode" / "opencode.json").exists()


def test_configure_json_output_bypasses_the_picker(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(router_module, "_can_draw_picker", lambda _ctx: True)
    monkeypatch.setattr(
        router_module,
        "_pick_installed_clients",
        lambda: pytest.fail("JSON output cannot draw an interactive picker"),
    )
    monkeypatch.setattr(
        router_module, "_install_claude_code_statusline", lambda _path: None
    )
    monkeypatch.setattr(router_module.shutil, "which", lambda _name: None)
    _mock_models(monkeypatch)

    configured = CliRunner().invoke(
        cli,
        ["--output", "json", "router", "configure", "--api-key", "router-secret"],
    )

    assert configured.exit_code == 0, configured.output
    payload = json.loads(configured.output)
    assert len(payload["data"][0]["clients"]) == len(router_module.CLIENT_NAMES)


def test_unconfigure_picker_offers_only_configured_agents(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    pi_home = tmp_path / "pi"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_home))
    _mock_models(monkeypatch)
    runner = CliRunner()
    for client in ("codex", "pi"):
        assert (
            runner.invoke(
                cli,
                [
                    "--human",
                    "router",
                    "configure",
                    client,
                    "--api-key",
                    "router-secret",
                ],
            ).exit_code
            == 0
        )
    captured = _capture_picker(monkeypatch, ["codex"])
    monkeypatch.setattr(router_module, "_can_draw_picker", lambda _ctx: True)

    removed = runner.invoke(cli, ["--human", "router", "unconfigure"])

    assert removed.exit_code == 0, removed.output
    # Claude Code and OpenCode have no setup, so there is nothing to undo.
    assert [choice.value for choice in captured["choices"]] == ["codex", "pi"]
    assert all(choice.checked for choice in captured["choices"])
    assert "remove Ramp Router from" in captured["message"]
    # Only the picked agent is undone.
    assert "Removed Ramp Router and restored your previous settings for: Codex." in (
        removed.output
    )
    assert not (codex_home / "ramp-router-state.json").exists()
    assert (pi_home / "ramp-router-state.json").exists()


def test_unconfigure_json_output_bypasses_the_picker_and_keeps_its_envelope(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    pi_home = tmp_path / "pi"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_home))
    monkeypatch.setattr(router_module.shutil, "which", lambda _name: None)
    _mock_models(monkeypatch)
    runner = CliRunner()
    for client in ("codex", "pi"):
        configured = runner.invoke(
            cli,
            [
                "--human",
                "router",
                "configure",
                client,
                "--api-key",
                "router-secret",
            ],
        )
        assert configured.exit_code == 0, configured.output
    monkeypatch.setattr(router_module, "_can_draw_picker", lambda _ctx: True)
    monkeypatch.setattr(
        router_module,
        "_pick_clients",
        lambda *_args: pytest.fail("JSON output cannot draw an interactive picker"),
    )

    removed = runner.invoke(cli, ["--output", "json", "router", "unconfigure"])

    assert removed.exit_code == 0, removed.output
    payload = json.loads(removed.output)
    assert [item["client"] for item in payload["data"][0]["clients"]] == [
        "codex",
        "pi",
    ]


def test_unconfigure_offers_an_agent_whose_credential_is_unusable(
    tmp_path, monkeypatch
):
    # A key that no longer works is a reason to unconfigure, not a reason to
    # leave the agent out of the list of what can be undone.
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    assert (
        CliRunner()
        .invoke(
            cli,
            ["--human", "router", "configure", "codex", "--api-key", "router-secret"],
        )
        .exit_code
        == 0
    )
    (codex_home / "ramp-router-key").unlink()

    assert router_module.configured_router_clients() == ()
    assert router_module._clients_with_a_receipt() == ("codex",)


def test_unconfigure_without_a_terminal_undoes_every_configured_agent(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    _mock_models(monkeypatch)
    runner = CliRunner()
    assert (
        runner.invoke(
            cli,
            ["--human", "router", "configure", "codex", "--api-key", "router-secret"],
        ).exit_code
        == 0
    )
    monkeypatch.setattr(
        router_module,
        "_pick_clients",
        lambda *_args: pytest.fail("a picker cannot be drawn without a terminal"),
    )

    removed = runner.invoke(cli, ["--human", "router", "unconfigure"])

    assert removed.exit_code == 0, removed.output
    assert not (tmp_path / "codex" / "ramp-router-state.json").exists()


def test_unconfigure_shows_a_spinner_while_restoring(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(router_module.shutil, "which", lambda _name: None)
    _mock_models(monkeypatch)
    runner = CliRunner()
    configured = runner.invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )
    assert configured.exit_code == 0, configured.output

    events = []

    def spinner(message):
        events.append(message)
        return lambda: events.append("stopped")

    monkeypatch.setattr(router_module, "start_spinner", spinner)

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "codex"])

    assert removed.exit_code == 0, removed.output
    assert events == ["Restoring your coding agent", "stopped"]


def test_configure_and_unconfigure_accept_multiple_clients(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(
        router_module,
        "_pick_installed_clients",
        lambda: pytest.fail("explicit clients must skip the picker"),
    )
    _mock_models(monkeypatch)
    _mock_codex_catalog(monkeypatch)
    runner = CliRunner()

    configure = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "claude-code",
            "codex",
            "--api-key",
            "router-secret",
        ],
    )

    original_settings = tmp_path / ".claude" / "original.settings.json"
    escape = "claude --settings ~/.claude/original.settings.json --model default"
    assert configure.exit_code == 0, configure.output
    assert configure.output.splitlines()[:4] == [
        "Connecting Ramp Router to your coding agents",
        "Skipping the Claude Code Router status line: it could not be "
        "downloaded from https://router.ramp.com/claude-code-statusline.",
        "Connected to: Claude Code and Codex",
        "1 model added. Start an agent and pick a model.",
    ]
    notice = _notice_lines(configure.output)
    assert notice[0].startswith("Claude Code & Codex only support one model provider")
    assert any("claude.ai connectors" in line for line in notice)
    # Typed by hand, so the home directory is shortened rather than printed as
    # the absolute path the settings file actually has.
    assert escape in notice
    assert "codex --profile original" in notice
    assert "ramp router unconfigure claude-code codex" in notice
    # The notice carries the restore command, so it is not repeated below.
    assert "Run 'ramp router unconfigure" not in configure.output
    assert (tmp_path / ".claude" / "ramp-router-state.json").exists()
    assert (tmp_path / ".codex" / "ramp-router-state.json").exists()
    assert not (tmp_path / ".config" / "opencode" / "opencode.json").exists()
    assert not (tmp_path / ".pi" / "agent" / "settings.json").exists()

    unconfigure = runner.invoke(
        cli, ["--human", "router", "unconfigure", "claude-code", "codex"]
    )

    assert unconfigure.exit_code == 0, unconfigure.output
    assert unconfigure.output.splitlines() == [
        "Removed Ramp Router and restored your previous settings for: "
        "Claude Code and Codex."
    ]
    assert not (tmp_path / ".claude" / "ramp-router-state.json").exists()
    assert not (tmp_path / ".codex" / "ramp-router-state.json").exists()
    # The originals are back at the default, so the escapes would only be
    # stale duplicates of it.
    assert not original_settings.exists()
    assert not (tmp_path / ".codex" / "original.config.toml").exists()


def test_reconfigure_advertises_both_escape_commands_after_unconfigure(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    _mock_models(monkeypatch)
    _mock_codex_catalog(monkeypatch)
    runner = CliRunner()
    configure_args = [
        "--human",
        "router",
        "configure",
        "claude-code",
        "codex",
        "--api-key",
        "router-secret",
    ]

    first = runner.invoke(cli, configure_args)
    assert first.exit_code == 0, first.output
    removed = runner.invoke(
        cli, ["--human", "router", "unconfigure", "claude-code", "codex"]
    )
    assert removed.exit_code == 0, removed.output

    second = runner.invoke(cli, configure_args)

    assert second.exit_code == 0, second.output
    notice = _notice_lines(second.output)
    assert (
        "claude --settings ~/.claude/original.settings.json --model default" in notice
    )
    assert "codex --profile original" in notice


def test_configure_and_unconfigure_without_client_targets_everything(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(
        router_module,
        "_pick_installed_clients",
        lambda: pytest.fail("non-interactive mode must skip the picker"),
    )
    _mock_models(monkeypatch)
    _mock_codex_catalog(monkeypatch)
    runner = CliRunner()

    configure = runner.invoke(
        cli,
        [
            "--no-input",
            "--human",
            "router",
            "configure",
            "--api-key",
            "router-secret",
        ],
    )

    assert configure.exit_code == 0
    assert configure.output.splitlines()[:4] == [
        "Connecting Ramp Router to your coding agents",
        # _mock_models serves no status line asset, so configure says why the
        # extra was skipped without failing anything.
        "Skipping the Claude Code Router status line: it could not be "
        "downloaded from https://router.ramp.com/claude-code-statusline.",
        "Connected to: Claude Code, Codex, OpenCode, and Pi",
        "1 model added. Start an agent and pick a model.",
    ]
    notice = _notice_lines(configure.output)
    assert "claude --settings ~/claude/original.settings.json --model default" in notice
    assert "codex --profile original" in notice
    # OpenCode and Pi keep their own defaults, so the notice does not name them.
    assert notice[0].startswith("Claude Code & Codex only support one model provider")
    assert "ramp router unconfigure" in notice
    assert (tmp_path / ".codex" / "config.toml").exists()
    assert (tmp_path / ".config" / "opencode" / "opencode.json").exists()
    assert not (tmp_path / ".pi" / "agent" / "models.json").exists()
    assert (
        json.loads((tmp_path / ".pi" / "agent" / "settings.json").read_text())[
            "defaultProvider"
        ]
        == "ramp-router"
    )

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure"])

    assert unconfigure.exit_code == 0
    assert unconfigure.output.splitlines() == [
        "Removed Ramp Router and restored your previous settings for: "
        "Claude Code, Codex, OpenCode, and Pi."
    ]
    assert not (tmp_path / ".codex" / "ramp-router-state.json").exists()
    assert not (tmp_path / ".config" / "opencode" / "ramp-router-state.json").exists()
    assert not (tmp_path / ".pi" / "agent" / "ramp-router-state.json").exists()


def test_pi_models_json_keeps_its_required_providers_key(tmp_path, monkeypatch):
    # Pi's schema requires "providers". Emptying the object and dropping the
    # key leaves {} on disk, which Pi rejects wholesale: it discards the file
    # and silently falls back to cached models.
    pi_home = tmp_path / "pi"
    pi_home.mkdir()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_home))
    models_path = pi_home / "models.json"
    # Router as the only statically configured provider, which is what a
    # previous configure leaves behind.
    models_path.write_text(json.dumps({"providers": {"ramp-router": {"models": []}}}))
    _mock_models(monkeypatch)

    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "pi", "--api-key", "router-secret"]
    )

    assert result.exit_code == 0, result.output
    written = json.loads(models_path.read_text())
    assert written == {"providers": {}}, written


def test_codex_replaces_an_old_router_catalog_with_live_discovery(
    tmp_path, monkeypatch
):
    # The managed snapshot is refreshed from Router rather than left pinned to
    # whatever models existed when an earlier setup ran.
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    stale = codex_home / "ramp-router-models.json"
    stale.write_text(json.dumps({"models": [{"slug": "long-gone"}]}))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch, [{"id": "a"}])

    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )

    assert result.exit_code == 0, result.output
    assert [model["slug"] for model in json.loads(stale.read_text())["models"]] == ["a"]
    assert tomllib.loads((codex_home / "config.toml").read_text())[
        "model_catalog_json"
    ] == str(stale)


def test_failed_codex_update_preserves_a_legacy_catalog(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    stale = codex_home / "ramp-router-models.json"
    stale_contents = json.dumps({"models": [{"slug": "still-needed-on-rollback"}]})
    stale.write_text(stale_contents)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch, [{"id": "a"}])

    def fail_index_update(*_args, **_kwargs):
        raise click.ClickException("index unavailable")

    monkeypatch.setattr(router_module, "_update_codex_session_index", fail_index_update)

    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )

    assert result.exit_code != 0
    assert stale.read_text() == stale_contents


def test_a_model_router_does_not_describe_is_an_error(monkeypatch):
    # Router describes everything it serves, so silence means the CLI and
    # Router are out of step. Guessing would hide that until a request failed.
    for router in (None, {"schema_version": 99}):
        payload = {"data": [{"id": "gpt-5.4", "owned_by": "openai"}]}
        if router is not None:
            payload["data"][0]["router"] = router

        class _Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return payload

        monkeypatch.setattr(router_module.httpx, "get", lambda *a, **k: _Response())

        with pytest.raises(click.ClickException) as raised:
            router_module._fetch_models("router-secret")
        assert "gpt-5.4" in str(raised.value)


def test_a_model_without_token_limits_is_an_error(monkeypatch):
    # The plugins refuse a model whose limits Router did not state, so the CLI
    # accepting it would let configure claim success for a list OpenCode and
    # Pi then reject.
    metadata = _router_metadata("gpt-5.4")
    metadata["limits"] = {"max_output_tokens": 16384}
    payload = {"data": [{"id": "gpt-5.4", "owned_by": "openai", "router": metadata}]}

    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return payload

    monkeypatch.setattr(router_module.httpx, "get", lambda *a, **k: _Response())

    with pytest.raises(click.ClickException) as raised:
        router_module._fetch_models("router-secret")
    assert "context_window" in str(raised.value)


def test_a_model_without_an_id_is_an_error(monkeypatch):
    # Same reasoning: the plugins abort on a nameless entry.
    payload = {"data": [{"owned_by": "openai", "router": _router_metadata("x")}]}

    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return payload

    monkeypatch.setattr(router_module.httpx, "get", lambda *a, **k: _Response())

    with pytest.raises(click.ClickException):
        router_module._fetch_models("router-secret")


def test_codex_keeps_its_own_harness_prompt(tmp_path, monkeypatch):
    # Codex replaces its prompt with whatever base_instructions says, and its
    # own runs to ~18k characters of editing and tool rules. Router sends an
    # empty one, so the prompt has to come from the user's install or the agent
    # silently gets worse.
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch, [{"id": "gpt-5.4"}])
    monkeypatch.setattr(router_module.shutil, "which", lambda _: "/usr/bin/codex")
    catalog = {
        "models": [
            {"slug": "gpt-5.4", "base_instructions": "REAL CODEX PROMPT"},
            {"slug": "gpt-5.6-sol", "base_instructions": "NEWER PROMPT"},
        ]
    }
    monkeypatch.setattr(
        router_module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(catalog), ""),
    )

    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )

    assert result.exit_code == 0, result.output
    instructions = codex_home / "ramp-router-instructions.md"
    # The prompt for the model being configured, not merely the first found.
    assert instructions.read_text() == "REAL CODEX PROMPT"
    config = tomllib.loads((codex_home / "config.toml").read_text())
    assert config["model_instructions_file"] == str(instructions.resolve())


def test_codex_is_configured_without_a_prompt_when_it_is_not_installed(
    tmp_path, monkeypatch
):
    # A missing prompt is better than a wrong one, and config writing must not
    # require the binary.
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch, [{"id": "gpt-5.4"}])
    monkeypatch.setattr(router_module.shutil, "which", lambda _: None)

    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )

    assert result.exit_code == 0, result.output
    assert not (codex_home / "ramp-router-instructions.md").exists()
    config = tomllib.loads((codex_home / "config.toml").read_text())
    assert "model_instructions_file" not in config


def test_a_failed_codex_configure_leaves_no_wreckage(tmp_path, monkeypatch):
    # Sessions are re-tagged to ramp-router so Codex keeps showing them. Left
    # that way after a failure, every existing conversation points at a
    # provider that was never configured and Codex hides all of them, which
    # looks to the user like their history was deleted.
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    sessions = codex_home / "sessions"
    sessions.mkdir()
    transcript = sessions / "rollout-1.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "s1", "model_provider": "openai"},
            }
        )
        + "\n"
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch, [{"id": "gpt-5.4"}])
    monkeypatch.setattr(router_module.shutil, "which", lambda _: None)

    real_write = router_module._write_private_file

    def fail_on_key(target, content):
        if target.name == "ramp-router-key":
            raise OSError("No space left on device")
        real_write(target, content)

    monkeypatch.setattr(router_module, "_write_private_file", fail_on_key)

    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )

    assert result.exit_code != 0
    assert "router-secret" not in result.output
    # The credential must not survive a failed run.
    assert not (codex_home / "ramp-router-key").exists()
    assert not (codex_home / "ramp-router-state.json").exists()
    # And the user's history must still be visible under their old provider.
    restored = json.loads(transcript.read_text().splitlines()[0])
    assert restored["payload"]["model_provider"] == "openai"


def test_the_key_environment_variable_the_help_names_is_read(tmp_path, monkeypatch):
    # The help text and the non-interactive error both point at this variable,
    # so a user following either was told to do something that did not work.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("RAMP_ROUTER_CONFIGURE_API_KEY", "secret-from-the-environment")
    _mock_models(monkeypatch, key="secret-from-the-environment")
    monkeypatch.setattr(router_module.shutil, "which", lambda _name: "/usr/bin/codex")

    def render_catalog(*_args, **_kwargs):
        assert "RAMP_ROUTER_CONFIGURE_API_KEY" not in os.environ
        return subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                {
                    "models": [
                        {
                            "slug": "gpt-5.4",
                            "base_instructions": "Codex instructions",
                        }
                    ]
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(router_module.subprocess, "run", render_catalog)

    result = CliRunner().invoke(
        cli, ["--no-input", "--human", "router", "configure", "codex"]
    )

    assert result.exit_code == 0, result.output
    key_path = tmp_path / "codex" / "ramp-router-key"
    assert key_path.read_text() == "secret-from-the-environment"
    # The key must not reach the terminal, where it would be scrolled back to.
    assert "secret-from-the-environment" not in result.output


def test_a_failed_codex_configure_gives_back_the_previous_config(tmp_path, monkeypatch):
    # The undo used to delete the key while leaving the config that points at
    # it, so Codex kept starting against a provider it could not authenticate.
    codex_home = tmp_path / "codex"
    codex_home.mkdir(parents=True)
    config_path = codex_home / "config.toml"
    original = 'model = "gpt-5"\nmodel_provider = "openai"\n'
    config_path.write_text(original)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)

    real_write = router_module._write_private_file

    def fail_on_the_key(path, content):
        if path.name == "ramp-router-key":
            raise OSError("read-only")
        real_write(path, content)

    monkeypatch.setattr(router_module, "_write_private_file", fail_on_the_key)

    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "codex"], input="router-secret\n"
    )

    assert result.exit_code != 0
    assert config_path.read_text() == original
    assert not (codex_home / "ramp-router-key").exists()


def test_codex_instructions_are_given_back_on_unconfigure(tmp_path, monkeypatch):
    # This command writes model_instructions_file, so the value it replaced has
    # to be captured or the user's own prompt never comes back.
    codex_home = tmp_path / "codex"
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text(
        'model_instructions_file = "/my/own/prompt.md"\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    runner = CliRunner()
    assert (
        runner.invoke(
            cli, ["--human", "router", "configure", "codex"], input="router-secret\n"
        ).exit_code
        == 0
    )

    assert (
        runner.invoke(cli, ["--human", "router", "unconfigure", "codex"]).exit_code == 0
    )

    restored = tomllib.loads((codex_home / "config.toml").read_text())
    assert restored["model_instructions_file"] == "/my/own/prompt.md"


def test_the_shared_harness_prompt_wins_over_the_alphabetically_last_one(monkeypatch):
    # Picking max() over the slugs chose whichever family sorted last, which
    # is unrelated to which prompt Codex generally uses.
    catalog = {
        "models": [
            {"slug": "gpt-5.6", "base_instructions": "the general prompt"},
            {"slug": "gpt-5.5", "base_instructions": "the general prompt"},
            {"slug": "o3-special", "base_instructions": "a specialization"},
        ]
    }

    def render(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, stdout=json.dumps(catalog), stderr="")

    monkeypatch.setattr(router_module.subprocess, "run", render)
    monkeypatch.setattr(router_module.shutil, "which", lambda _name: "/usr/bin/codex")

    assert (
        router_module._codex_harness_prompt("not-in-the-catalog")
        == "the general prompt"
    )


def test_shared_harness_prompt_ties_break_by_minimum_slug(monkeypatch):
    catalog = {
        "models": [
            {"slug": "a-family", "base_instructions": "first prompt"},
            {"slug": "z-family", "base_instructions": "first prompt"},
            {"slug": "b-family", "base_instructions": "second prompt"},
            {"slug": "c-family", "base_instructions": "second prompt"},
        ]
    }

    monkeypatch.setattr(
        router_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, stdout=json.dumps(catalog), stderr=""
        ),
    )
    monkeypatch.setattr(router_module.shutil, "which", lambda _name: "/usr/bin/codex")

    assert router_module._codex_harness_prompt("not-in-the-catalog") == "second prompt"


def test_a_configured_stack_other_than_production_is_used(tmp_path, monkeypatch):
    # The bundled plugins already read this. Without it here, reaching another
    # stack meant hand-editing the files this command writes, which the next
    # configure would overwrite.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("RAMP_ROUTER_BASE_URL", "http://127.0.0.1:28362/v1")
    _mock_models(monkeypatch, base_url="http://127.0.0.1:28362/v1")

    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "codex"], input="router-secret\n"
    )

    assert result.exit_code == 0, result.output
    config = tomllib.loads((tmp_path / "codex" / "config.toml").read_text())
    provider = config["model_providers"]["ramp-router"]
    assert provider["base_url"] == "http://127.0.0.1:28362/v1"


def test_a_setup_written_before_the_client_marker_is_marked_as_replaced(
    tmp_path, monkeypatch
):
    # Router answers with Codex's own catalog shape only for a client that asks
    # by name, so a block written earlier leaves the picker empty with nothing
    # said. Rewriting it is the fix; machine output records that repair without
    # adding one-off detail to the concise human success message.
    codex_home = tmp_path / "codex"
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text(
        "[model_providers.ramp-router]\n"
        'name = "Ramp Router"\n'
        'base_url = "https://router-api.ramp.com/v1"\n'
        'wire_api = "responses"\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)

    result = CliRunner().invoke(
        cli,
        ["router", "configure", "codex", "--api-key", "router-secret"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"][0]["replaced_outdated_setup"] is True
    config = tomllib.loads((codex_home / "config.toml").read_text())
    headers = config["model_providers"]["ramp-router"]["http_headers"]
    assert headers["X-Gateway-Client"] == "codex"


def test_a_current_setup_is_not_marked_as_replaced(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    _mock_models(monkeypatch)
    runner = CliRunner()
    assert (
        runner.invoke(
            cli, ["--human", "router", "configure", "codex"], input="router-secret\n"
        ).exit_code
        == 0
    )

    again = runner.invoke(
        cli,
        ["router", "configure", "codex", "--api-key", "router-secret"],
    )

    assert again.exit_code == 0, again.output
    assert json.loads(again.output)["data"][0]["replaced_outdated_setup"] is False


def test_pi_is_told_which_router_to_call(tmp_path, monkeypatch):
    # Pi gives an extension no configuration, so without this the base URL
    # could only come from the environment, which means setting it again in
    # every shell.
    pi_home = tmp_path / "pi"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_home))
    monkeypatch.setenv("RAMP_ROUTER_BASE_URL", "http://127.0.0.1:28362/v1")
    _mock_models(monkeypatch, base_url="http://127.0.0.1:28362/v1")
    runner = CliRunner()

    result = runner.invoke(
        cli, ["--human", "router", "configure", "pi"], input="router-secret\n"
    )

    assert result.exit_code == 0, result.output
    recorded = json.loads((pi_home / "ramp-router-config.json").read_text())
    assert recorded == {"baseUrl": "http://127.0.0.1:28362/v1"}

    assert runner.invoke(cli, ["--human", "router", "unconfigure", "pi"]).exit_code == 0
    assert not (pi_home / "ramp-router-config.json").exists()


def test_a_failed_reconfigure_leaves_the_working_setup_intact(tmp_path, monkeypatch):
    # The second configure finds a key and prompt already on disk that the
    # restored config still points at. Deleting them would leave Codex unable
    # to authenticate against a setup that worked a moment earlier.
    codex_home = tmp_path / "codex"
    sessions_dir = codex_home / "sessions"
    sessions_dir.mkdir(parents=True)
    managed_session = sessions_dir / "managed.jsonl"
    managed_session.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "managed", "model_provider": "openai"},
            }
        )
        + "\n"
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch)
    runner = CliRunner()
    assert (
        runner.invoke(
            cli, ["--human", "router", "configure", "codex"], input="router-secret\n"
        ).exit_code
        == 0
    )
    working_config = (codex_home / "config.toml").read_text()
    working_key = (codex_home / "ramp-router-key").read_text()
    state_path = codex_home / "ramp-router-state.json"
    working_state = state_path.read_text()
    assert (
        json.loads(managed_session.read_text())["payload"]["model_provider"]
        == "ramp-router"
    )
    new_session = sessions_dir / "new.jsonl"
    new_session.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "new", "model_provider": "openai"},
            }
        )
        + "\n"
    )

    real_write = router_module._write_private_file

    def fail_writing_the_config(path, content):
        if path.name == "config.toml":
            raise OSError("read-only")
        real_write(path, content)

    monkeypatch.setattr(router_module, "_write_private_file", fail_writing_the_config)
    result = runner.invoke(
        cli, ["--human", "router", "configure", "codex"], input="router-secret\n"
    )

    assert result.exit_code != 0, result.output
    assert (codex_home / "ramp-router-key").exists(), "the working key was deleted"
    assert (codex_home / "ramp-router-key").read_text() == working_key
    assert (codex_home / "config.toml").read_text() == working_config
    assert state_path.read_text() == working_state
    assert (
        json.loads(managed_session.read_text())["payload"]["model_provider"]
        == "ramp-router"
    )
    assert json.loads(new_session.read_text())["payload"]["model_provider"] == "openai"
