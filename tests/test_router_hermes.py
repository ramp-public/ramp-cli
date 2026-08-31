"""Tests for the Hermes Agent Ramp Router configuration client.

Hermes configuration flows through its own ``hermes config`` scripting
surface rather than direct YAML edits, so these tests run against a fake
``hermes`` binary that reproduces the observed contract of that surface
(nested dotted keys, scalar-``model`` promotion, "Config key not set"
misses, whole-mapping unset) on top of a JSON store.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

import ramp_cli.commands.router as router_module
from ramp_cli import claude_cowork, hermes_agent
from ramp_cli.commands.router import DEFAULT_ROUTER_BASE_URL as ROUTER_BASE_URL
from ramp_cli.main import cli


@pytest.fixture(autouse=True)
def _complete_browser_key_setup(monkeypatch, tmp_path_factory):
    """Keep configuration tests focused beyond host-only setup boundaries."""
    monkeypatch.setattr(
        router_module,
        "acquire_router_api_key",
        lambda _url, *, no_browser: "router-secret",
    )
    monkeypatch.setattr(claude_cowork, "preflight", lambda: None)
    monkeypatch.setattr(claude_cowork, "is_available", lambda: False)
    monkeypatch.setenv(
        "RAMP_CLAUDE_DESKTOP_APP_SUPPORT",
        str(tmp_path_factory.mktemp("claude-app-support")),
    )


def _router_metadata(identifier):
    """The description Router publishes for every model it serves."""
    return {
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


def _mock_models(monkeypatch, models=None, key="router-secret"):
    models = models or [{"id": "gpt-5.4"}]
    models = [{**m, "router": _router_metadata(m["id"])} for m in models]

    def get(url, *, headers, timeout):
        if "/session-usage/usage/balance" in url or url.endswith(
            ("/claude-code-statusline", "/codex-cost-hook")
        ):
            # Each optional asset has its own tests; unavailable here, so
            # configure simply skips it.
            return httpx.Response(404, request=httpx.Request("GET", url))
        assert url == f"{ROUTER_BASE_URL}/models"
        assert headers["Authorization"] == f"Bearer {key}"
        if headers.get("X-Gateway-Client") == "codex":
            # Codex discovery reads Router's Codex-shaped catalog projection.
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


def _coerce(value: str):
    """Hermes's documented set-value coercion: bool, then number, then str."""
    if value == "true":
        return True
    if value == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


class FakeHermes:
    """A ``hermes config`` double against a JSON store.

    Reproduces the behavior verified against hermes-agent main: ``get
    --json`` prints JSON or exits 1 with "Config key not set", ``set
    --force`` creates nested mappings and promotes a scalar ``model`` so the
    scalar becomes ``model.default``, and ``unset`` removes leaves or whole
    mappings.
    """

    def __init__(self, home):
        self.store_path = home / "config-store.json"

    def _load(self):
        try:
            return json.loads(self.store_path.read_text())
        except FileNotFoundError:
            return {}

    def _save(self, data):
        self.store_path.write_text(json.dumps(data))

    def run(self, argv, **_kwargs):
        assert argv[0] == "/fake/hermes"
        assert argv[1] == "config"
        args = list(argv[2:])
        action = args.pop(0)
        data = self._load()
        if action == "get":
            assert args[-1] == "--json"
            segments = args[0].split(".")
            node = data
            for segment in segments:
                if not isinstance(node, dict) or segment not in node:
                    return subprocess.CompletedProcess(
                        argv, 1, stdout=f"Config key not set: {args[0]}\n", stderr=""
                    )
                node = node[segment]
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(node), stderr=""
            )
        if action == "set":
            assert args[0] == "--force"
            key, value = args[1], _coerce(args[2])
            segments = key.split(".")
            node = data
            for segment in segments[:-1]:
                child = node.get(segment)
                if not isinstance(child, dict):
                    # Scalar promotion, as hermes does for ``model``.
                    child = {"default": child} if child is not None else {}
                    node[segment] = child
                node = child
            node[segments[-1]] = value
            self._save(data)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if action == "unset":
            segments = args[0].split(".")
            node = data
            for segment in segments[:-1]:
                node = node.get(segment)
                if not isinstance(node, dict):
                    return subprocess.CompletedProcess(
                        argv, 1, stdout=f"Config key not set: {args[0]}\n", stderr=""
                    )
            if segments[-1] not in node:
                return subprocess.CompletedProcess(
                    argv, 1, stdout=f"Config key not set: {args[0]}\n", stderr=""
                )
            del node[segments[-1]]
            self._save(data)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected hermes config invocation: {argv}")


@pytest.fixture
def fake_hermes(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    fake = FakeHermes(home)
    fake.home = home
    monkeypatch.setattr(hermes_agent, "hermes_executable", lambda: "/fake/hermes")
    monkeypatch.setattr(hermes_agent.subprocess, "run", fake.run)
    return fake


def test_configure_hermes_writes_config_env_and_receipt(fake_hermes, monkeypatch):
    fake_hermes._save({"model": "anthropic/claude-sonnet-4"})
    env_file = fake_hermes.home / ".env"
    env_file.write_text("OTHER_KEY=untouched\n")
    _mock_models(monkeypatch, [{"id": "a"}, {"id": "b"}])

    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )

    assert result.exit_code == 0, result.output
    store = json.loads(fake_hermes.store_path.read_text())
    assert store["providers"]["router"] == {
        "base_url": ROUTER_BASE_URL,
        "api_mode": "codex_responses",
        "key_env": "RAMP_ROUTER_API_KEY",
    }
    # The scalar model promotes to a mapping and Router becomes the default.
    assert store["model"] == {
        "default": "a",
        "provider": "router",
    }
    env_lines = env_file.read_text().splitlines()
    assert "OTHER_KEY=untouched" in env_lines
    assert "RAMP_ROUTER_API_KEY=router-secret" in env_lines
    state = json.loads((fake_hermes.home / "ramp-router-state.json").read_text())
    assert state == {
        "model": "anthropic/claude-sonnet-4",
        "provider_entry": None,
        "env_key": None,
        # No other provider referenced the key variable before setup.
        "preexisting_key_env_providers": {},
        "written_provider_entry": {
            "base_url": ROUTER_BASE_URL,
            "api_mode": "codex_responses",
            "key_env": "RAMP_ROUTER_API_KEY",
        },
        # A fingerprint of the written key, never the credential itself.
        "written_env_key_sha256": hashlib.sha256(b"router-secret").hexdigest(),
        "written_model_default": "a",
    }
    receipt = fake_hermes.home / "ramp-router-state.json"
    # The receipt can carry a pre-existing key, so it is owner-only.
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert "Connected to: Hermes" in result.output
    assert "2 models added. Start an agent and pick a model." in result.output
    assert "router-secret" not in result.output


def test_configure_hermes_creates_env_with_owner_only_permissions(
    fake_hermes, monkeypatch
):
    _mock_models(monkeypatch)

    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )

    assert result.exit_code == 0, result.output
    env_file = fake_hermes.home / ".env"
    assert env_file.read_text() == "RAMP_ROUTER_API_KEY=router-secret\n"
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_configure_hermes_requires_the_hermes_binary(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(hermes_agent, "hermes_executable", lambda: None)
    _mock_models(monkeypatch)

    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )

    assert result.exit_code != 0
    assert "Hermes: Hermes is not installed" in result.output
    # Nothing was written: no receipt, no credential.
    assert not (home / "ramp-router-state.json").exists()
    assert not (home / ".env").exists()


def test_bare_configure_skips_hermes_when_its_binary_is_missing(tmp_path, monkeypatch):
    """A bare run configures everything configurable and does not fail.

    Every other agent accepts configuration before it is installed because
    the CLI writes its files directly; Hermes is configured through its own
    executable, so a machine without it is skipped rather than failed —
    only naming hermes explicitly reports the missing binary.
    """
    home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(hermes_agent, "hermes_executable", lambda: None)
    _mock_models(monkeypatch)

    result = CliRunner().invoke(
        cli,
        ["--human", "--no-input", "router", "configure", "--api-key", "router-secret"],
    )

    assert result.exit_code == 0, result.output
    assert "Hermes" not in result.output
    assert not home.exists()


def test_unconfigure_hermes_restores_previous_settings(fake_hermes, monkeypatch):
    fake_hermes._save(
        {
            "model": "anthropic/claude-sonnet-4",
            "providers": {"router": {"base_url": "https://old.example/v1"}},
        }
    )
    env_file = fake_hermes.home / ".env"
    env_file.write_text("OTHER_KEY=untouched\nRAMP_ROUTER_API_KEY=old-secret\n")
    _mock_models(monkeypatch)
    runner = CliRunner()

    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert configure.exit_code == unconfigure.exit_code == 0, unconfigure.output
    store = json.loads(fake_hermes.store_path.read_text())
    assert store["model"] == "anthropic/claude-sonnet-4"
    assert store["providers"]["router"] == {"base_url": "https://old.example/v1"}
    env_lines = env_file.read_text().splitlines()
    assert "OTHER_KEY=untouched" in env_lines
    assert "RAMP_ROUTER_API_KEY=old-secret" in env_lines
    assert not (fake_hermes.home / "ramp-router-state.json").exists()
    assert (
        "Removed Ramp Router and restored your previous settings for: Hermes."
        in unconfigure.output
    )


def test_unconfigure_hermes_removes_key_it_added(fake_hermes, monkeypatch):
    _mock_models(monkeypatch)
    runner = CliRunner()

    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert configure.exit_code == unconfigure.exit_code == 0, unconfigure.output
    store = json.loads(fake_hermes.store_path.read_text())
    assert "router" not in store.get("providers", {})
    assert "model" not in store
    assert "RAMP_ROUTER_API_KEY" not in (fake_hermes.home / ".env").read_text()


def test_unconfigure_hermes_preserves_a_user_switched_model(fake_hermes, monkeypatch):
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    # The user has since pointed Hermes elsewhere; that choice is theirs.
    store = json.loads(fake_hermes.store_path.read_text())
    store["model"] = {"provider": "nous", "default": "hermes-4"}
    fake_hermes.store_path.write_text(json.dumps(store))

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    store = json.loads(fake_hermes.store_path.read_text())
    assert store["model"] == {"provider": "nous", "default": "hermes-4"}
    assert "router" not in store.get("providers", {})


def test_refresh_hermes_preserves_the_selected_model(fake_hermes, monkeypatch):
    _mock_models(monkeypatch, [{"id": "a"}, {"id": "b"}])
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    # The user picked the other Router model; refresh must keep it.
    store = json.loads(fake_hermes.store_path.read_text())
    store["model"]["default"] = "b"
    fake_hermes.store_path.write_text(json.dumps(store))

    refresh = runner.invoke(cli, ["--human", "router", "refresh"])

    assert refresh.exit_code == 0, refresh.output
    assert "Refreshed the Ramp Router configuration for Hermes." in refresh.output
    store = json.loads(fake_hermes.store_path.read_text())
    assert store["model"]["default"] == "b"


def test_repeat_configure_keeps_the_original_receipt(fake_hermes, monkeypatch):
    fake_hermes._save({"model": "anthropic/claude-sonnet-4"})
    _mock_models(monkeypatch)
    runner = CliRunner()

    first = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    second = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )

    assert first.exit_code == second.exit_code == 0
    state = json.loads((fake_hermes.home / "ramp-router-state.json").read_text())
    # The snapshot still describes the pre-Router world, not the first setup.
    assert state["model"] == "anthropic/claude-sonnet-4"


def test_unconfigure_restores_a_default_model_by_unsetting_it(fake_hermes, monkeypatch):
    """Hermes reports merged defaults, so an empty model restores to unset.

    ``hermes config get model`` on a never-configured home answers the
    schema default (an empty value) instead of a miss; writing that back
    verbatim would leave a literal empty ``model`` key behind. Unsetting
    reproduces the same default without the residue.
    """
    fake_hermes._save({"model": ""})
    _mock_models(monkeypatch)
    runner = CliRunner()

    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert configure.exit_code == unconfigure.exit_code == 0, unconfigure.output
    store = json.loads(fake_hermes.store_path.read_text())
    assert "model" not in store


def test_hermes_config_path_honors_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "custom-home"))
    assert router_module._client_config_path("hermes") == (
        tmp_path / "custom-home" / "config.yaml"
    )


def test_configure_tightens_a_world_readable_env_file(fake_hermes, monkeypatch):
    env_file = fake_hermes.home / ".env"
    env_file.write_text("OTHER_KEY=untouched\n")
    env_file.chmod(0o644)
    _mock_models(monkeypatch)

    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )

    assert result.exit_code == 0, result.output
    # The file now carries a Router credential, so owner-only wins over the
    # permissions the pre-existing file arrived with.
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_unconfigure_preserves_the_receipt_when_an_unset_fails(
    fake_hermes, monkeypatch
):
    """A rejected restore keeps the receipt so recovery stays possible."""
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output

    real_run = fake_hermes.run

    def failing_run(argv, **kwargs):
        if argv[2] == "unset":
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="config.yaml is managed"
            )
        return real_run(argv, **kwargs)

    monkeypatch.setattr(hermes_agent.subprocess, "run", failing_run)

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code != 0
    assert "config.yaml is managed" in unconfigure.output
    # Nothing was torn down: receipt and credential both survive for a retry.
    assert (fake_hermes.home / "ramp-router-state.json").exists()
    assert (
        "RAMP_ROUTER_API_KEY=router-secret" in (fake_hermes.home / ".env").read_text()
    )


def test_unconfigure_leaves_a_user_edited_provider_entry_alone(
    fake_hermes, monkeypatch
):
    """Post-setup provider edits belong to the user, like a switched model.

    The surviving entry still names RAMP_ROUTER_API_KEY as its key_env, so
    the credential stays with it rather than being torn out from under a
    setup the user kept.
    """
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    store = json.loads(fake_hermes.store_path.read_text())
    store["providers"]["router"]["base_url"] = "https://router.staging.example/v1"
    fake_hermes.store_path.write_text(json.dumps(store))

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    store = json.loads(fake_hermes.store_path.read_text())
    assert store["providers"]["router"]["base_url"] == (
        "https://router.staging.example/v1"
    )
    # The surviving user-owned entry keeps its dependencies: the model that
    # calls through it stays, like the credential below.
    assert store["model"]["provider"] == "router"
    assert (
        "RAMP_ROUTER_API_KEY=router-secret" in (fake_hermes.home / ".env").read_text()
    )
    assert not (fake_hermes.home / "ramp-router-state.json").exists()


def test_fallback_picker_offers_hermes_when_its_binary_exists(monkeypatch):
    """The nothing-detected fallback includes Hermes only when it can run."""
    captured = {}

    class Prompt:
        def ask(self):
            return ["hermes"]

    def checkbox(message, **kwargs):
        captured.update(kwargs)
        return Prompt()

    monkeypatch.setattr(router_module.questionary, "checkbox", checkbox)
    monkeypatch.setattr(router_module, "_installed_clients", lambda: ())
    # Pinned so the offered lines depend on the test, not on whether the
    # machine running the suite happens to have Cursor installed.
    monkeypatch.setattr(router_module, "_cursor_is_installed", lambda: False)
    monkeypatch.setattr(hermes_agent, "hermes_executable", lambda: "/fake/hermes")

    assert router_module._pick_installed_clients() == ("hermes",)
    assert [choice.value for choice in captured["choices"]] == [
        "claude-code",
        "codex",
        "opencode",
        "pi",
        "hermes",
    ]


def test_refresh_fails_rather_than_defaulting_the_endpoint(fake_hermes, monkeypatch):
    """An unreadable receipt-backed endpoint is a failure, not a fallback.

    Falling back to the active/default Router would transmit this setup's
    stored credential to a different environment.
    """
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    # Hermes has since left PATH; the key in .env still enrolls the receipt.
    monkeypatch.setattr(hermes_agent, "hermes_executable", lambda: None)

    refresh = runner.invoke(cli, ["--human", "router", "refresh"])

    assert refresh.exit_code != 0
    assert "Hermes" in refresh.output
    assert "Refreshed the Ramp Router configuration for Hermes." not in refresh.output


def test_unconfigure_retry_completes_a_partially_failed_restore(
    fake_hermes, monkeypatch
):
    """A transient restore failure is retryable without losing the snapshot."""
    fake_hermes._save(
        {
            "providers": {
                "router": {"base_url": "https://old.example/v1", "note": "keep"}
            }
        }
    )
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output

    real_run = fake_hermes.run
    calls = {"set": 0}

    def flaky_run(argv, **kwargs):
        if argv[2] == "set" and argv[4].startswith("providers.router."):
            calls["set"] += 1
            # The leaf-scoped restore only rewrites managed leaves, so the
            # first provider write is the one to fail transiently.
            if calls["set"] >= 1:
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="transient failure"
                )
        return real_run(argv, **kwargs)

    monkeypatch.setattr(hermes_agent.subprocess, "run", flaky_run)
    first = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])
    assert first.exit_code != 0
    # The receipt survived the failure, so a retry can finish the job.
    assert (fake_hermes.home / "ramp-router-state.json").exists()

    monkeypatch.setattr(hermes_agent.subprocess, "run", real_run)
    second = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert second.exit_code == 0, second.output
    store = json.loads(fake_hermes.store_path.read_text())
    assert store["providers"]["router"] == {
        "base_url": "https://old.example/v1",
        "note": "keep",
    }
    assert not (fake_hermes.home / "ramp-router-state.json").exists()


def test_installed_clients_needs_the_hermes_binary_not_just_its_directory(
    tmp_path, monkeypatch
):
    """A leftover ~/.hermes without the executable is not a configurable install."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(router_module.shutil, "which", lambda _name: None)
    assert "hermes" not in router_module._installed_clients()

    monkeypatch.setattr(
        router_module.shutil,
        "which",
        lambda name: "/fake/hermes" if name == "hermes" else None,
    )
    assert "hermes" in router_module._installed_clients()


def test_unconfigure_leaves_a_user_rotated_key_alone(fake_hermes, monkeypatch):
    """A credential rotated after setup is the user's only copy — keep it."""
    env_file = fake_hermes.home / ".env"
    env_file.write_text("RAMP_ROUTER_API_KEY=pre-router-secret\n")
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    # The user rotated the key after setup (for example from the dashboard).
    hermes_agent.write_env_value("RAMP_ROUTER_API_KEY", "rotated-secret")

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    assert "RAMP_ROUTER_API_KEY=rotated-secret" in env_file.read_text()
    # Everything else is still cleaned up.
    store = json.loads(fake_hermes.store_path.read_text())
    assert "router" not in store.get("providers", {})
    assert not (fake_hermes.home / "ramp-router-state.json").exists()


def test_refresh_aborts_when_the_receipt_disappears_mid_flight(
    fake_hermes, monkeypatch
):
    """Refresh must not resurrect a setup a concurrent unconfigure removed."""
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output

    receipt = fake_hermes.home / "ramp-router-state.json"
    original_lock = router_module._hermes_config_lock

    def racing_lock():
        # The unconfigure wins the race after refresh's enrollment scan but
        # before its write transaction takes the lock.
        if receipt.exists():
            receipt.unlink()
        return original_lock()

    monkeypatch.setattr(router_module, "_hermes_config_lock", racing_lock)
    refresh = runner.invoke(cli, ["--human", "router", "refresh"])

    assert refresh.exit_code != 0
    assert "no longer configured" in refresh.output
    # Nothing was resurrected.
    assert not receipt.exists()


def test_env_writes_preserve_a_symlinked_env_file(fake_hermes, tmp_path, monkeypatch):
    """A dotfiles-managed .env symlink survives; the target gets the write."""
    target = tmp_path / "dotfiles" / "hermes.env"
    target.parent.mkdir()
    target.write_text("OTHER_KEY=untouched\n")
    env_file = fake_hermes.home / ".env"
    env_file.symlink_to(target)
    _mock_models(monkeypatch)

    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )

    assert result.exit_code == 0, result.output
    assert env_file.is_symlink()
    lines = target.read_text().splitlines()
    assert "OTHER_KEY=untouched" in lines
    assert "RAMP_ROUTER_API_KEY=router-secret" in lines
    assert target.stat().st_mode & 0o777 == 0o600


def test_malformed_config_json_is_an_error_not_an_absence(fake_hermes, monkeypatch):
    """Garbage from `hermes config get` must not snapshot a setting as absent."""
    real_run = fake_hermes.run

    def garbled_run(argv, **kwargs):
        if argv[2] == "get" and argv[3] == "model":
            return subprocess.CompletedProcess(argv, 0, stdout="not json{", stderr="")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(hermes_agent.subprocess, "run", garbled_run)
    _mock_models(monkeypatch)

    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )

    assert result.exit_code != 0
    assert "Could not parse Hermes config key" in result.output
    # No receipt was written from a corrupt snapshot.
    assert not (fake_hermes.home / "ramp-router-state.json").exists()


def test_unconfigure_restores_a_user_picked_model_when_its_provider_is_torn_down(
    fake_hermes, monkeypatch
):
    """A user-picked Router model cannot outlive the provider it calls through.

    Unconfigure removes providers.router and the credential, so leaving
    model.provider pointed at the removed entry would strand Hermes on a
    provider that no longer exists; the pre-Router selection comes back.
    """
    fake_hermes._save({"model": "anthropic/claude-sonnet-4"})
    _mock_models(monkeypatch, [{"id": "a"}, {"id": "b"}])
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    # The user moved to the other Router model after setup.
    store = json.loads(fake_hermes.store_path.read_text())
    store["model"]["default"] = "b"
    fake_hermes.store_path.write_text(json.dumps(store))

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    store = json.loads(fake_hermes.store_path.read_text())
    assert store["model"] == "anthropic/claude-sonnet-4"
    # The credential and provider entry are still cleaned up.
    assert "router" not in store.get("providers", {})
    assert "RAMP_ROUTER_API_KEY" not in (fake_hermes.home / ".env").read_text()


def test_unconfigure_keeps_a_user_model_on_a_user_repointed_provider(
    fake_hermes, monkeypatch
):
    """A re-picked model whose provider entry the user now owns keeps working.

    The surviving entry still names RAMP_ROUTER_API_KEY as its key_env, so
    the credential stays too — removing it would leave the retained
    provider and model without anything to authenticate with.
    """
    fake_hermes._save({"model": "anthropic/claude-sonnet-4"})
    _mock_models(monkeypatch, [{"id": "a"}, {"id": "b"}])
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    # The user re-pointed the provider entry at their own deployment and
    # picked another model: both edits are theirs, and the surviving entry
    # means the model selection still has a provider to call through.
    store = json.loads(fake_hermes.store_path.read_text())
    store["providers"]["router"]["base_url"] = "https://router.example.test/v1"
    store["model"]["default"] = "b"
    fake_hermes.store_path.write_text(json.dumps(store))

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    store = json.loads(fake_hermes.store_path.read_text())
    assert store["providers"]["router"]["base_url"] == "https://router.example.test/v1"
    assert store["model"] == {"provider": "router", "default": "b"}
    # The surviving user-owned entry depends on the key, so it stays.
    assert (
        "RAMP_ROUTER_API_KEY=router-secret" in (fake_hermes.home / ".env").read_text()
    )
    assert not (fake_hermes.home / "ramp-router-state.json").exists()


def test_refresh_does_not_stamp_a_user_selection_as_cli_owned(fake_hermes, monkeypatch):
    """Refresh echoing the user's live pick must not defeat the ownership check.

    configure writes A; the user moves to B; a background refresh passes the
    live B through as default_model. The receipt's ownership marker must
    keep recording A — the default this CLI chose — so B still reads as the
    user's selection afterwards.
    """
    fake_hermes._save({"model": "anthropic/claude-sonnet-4"})
    _mock_models(monkeypatch, [{"id": "a"}, {"id": "b"}])
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    store = json.loads(fake_hermes.store_path.read_text())
    store["model"]["default"] = "b"
    fake_hermes.store_path.write_text(json.dumps(store))

    refresh = runner.invoke(cli, ["--human", "router", "refresh"])
    assert refresh.exit_code == 0, refresh.output

    state = json.loads((fake_hermes.home / "ramp-router-state.json").read_text())
    assert state["written_model_default"] == "a"
    # The live selection itself is untouched by the refresh.
    store = json.loads(fake_hermes.store_path.read_text())
    assert store["model"]["default"] == "b"


def test_key_removal_is_atomic_and_keeps_file_permissions(fake_hermes, monkeypatch):
    """Removing our key preserves the file's other entries and its mode."""
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    env_file = fake_hermes.home / ".env"
    # The user added another credential and loosened the mode after setup.
    hermes_agent.write_env_value("OTHER_KEY", "keep-me")
    env_file.chmod(0o640)

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    lines = env_file.read_text().splitlines()
    assert "OTHER_KEY=keep-me" in lines
    assert not any(line.startswith("RAMP_ROUTER_API_KEY=") for line in lines)
    # A removal stores no secret of ours, so the user's chosen mode stays.
    assert env_file.stat().st_mode & 0o777 == 0o640


def test_an_unreadable_env_file_is_an_error_not_an_absence(fake_hermes, monkeypatch):
    """A .env that exists but cannot be read must not snapshot the key as absent.

    Recording ``env_key: null`` from a transient read failure poisons the
    receipt: a retry then overwrites the user's key and a later unconfigure
    deletes it instead of restoring it.
    """
    env_file = fake_hermes.home / ".env"
    env_file.write_bytes(b"RAMP_ROUTER_API_KEY=\xff\xfe-not-utf8\n")
    _mock_models(monkeypatch)

    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )

    assert result.exit_code != 0
    assert "Could not read" in result.output
    # No receipt was written from a corrupt snapshot, and the file is intact.
    assert not (fake_hermes.home / "ramp-router-state.json").exists()
    assert env_file.read_bytes() == b"RAMP_ROUTER_API_KEY=\xff\xfe-not-utf8\n"


def test_refresh_reads_nothing_after_a_concurrent_unconfigure(fake_hermes, monkeypatch):
    """The lock covers refresh's reads and discovery, not just its writes.

    When unconfigure wins the race, the credential and endpoint on disk are
    the user's restored state; refresh must not read them and must not send
    that credential anywhere — the models request would otherwise pair the
    key with whatever endpoint the restore put back.
    """
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output

    receipt = fake_hermes.home / "ramp-router-state.json"
    original_lock = router_module._hermes_config_lock

    def racing_lock():
        if receipt.exists():
            receipt.unlink()
        return original_lock()

    def no_requests(url, **_kwargs):
        raise AssertionError(f"refresh must not fetch after unconfigure: {url}")

    monkeypatch.setattr(router_module, "_hermes_config_lock", racing_lock)
    monkeypatch.setattr("ramp_cli.commands.router.httpx.get", no_requests)
    refresh = runner.invoke(cli, ["--human", "router", "refresh"])

    assert refresh.exit_code != 0
    assert "no longer configured" in refresh.output


def test_unconfigure_restores_after_a_failed_reconfigure_to_a_new_endpoint(
    fake_hermes, monkeypatch
):
    """A marker overwritten before a failed write must not orphan the old setup.

    A repeat configure toward a different endpoint records its new marker in
    the receipt as *pending* before writing; when the first provider write
    then fails, the live entry still holds the prior CLI-written endpoint,
    which the retained *written* marker keeps recognizable as ours — so
    unconfigure restores instead of classifying the CLI's own entry as a
    user edit.
    """
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output

    # A repeat configure pointed at a different Router whose first provider
    # write fails after the receipt was updated.
    new_base = "https://router.staging.example/v1"
    monkeypatch.setenv("RAMP_ROUTER_BASE_URL", new_base)

    def get_new_endpoint(url, *, headers, timeout):
        if "/session-usage/usage/balance" in url or url.endswith(
            ("/claude-code-statusline", "/codex-cost-hook")
        ):
            return httpx.Response(404, request=httpx.Request("GET", url))
        assert url == f"{new_base}/models"
        return httpx.Response(
            200,
            json={"data": [{"id": "gpt-5.4", "router": _router_metadata("gpt-5.4")}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("ramp_cli.commands.router.httpx.get", get_new_endpoint)
    real_run = fake_hermes.run

    def failing_run(argv, **kwargs):
        if argv[2] == "set" and argv[4] == "providers.router.base_url":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="disk full")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(hermes_agent.subprocess, "run", failing_run)
    reconfigure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="\n",
    )
    assert reconfigure.exit_code != 0
    # The failure landed inside the window the fix covers: the receipt
    # carries the old generation as written and the new one as pending,
    # while the live entry still holds the prior CLI-written endpoint.
    state = json.loads((fake_hermes.home / "ramp-router-state.json").read_text())
    assert state["written_provider_entry"]["base_url"] == ROUTER_BASE_URL
    assert [m["base_url"] for m in state["pending_provider_entries"]] == [new_base]
    store = json.loads(fake_hermes.store_path.read_text())
    assert store["providers"]["router"]["base_url"] == ROUTER_BASE_URL

    monkeypatch.setattr(hermes_agent.subprocess, "run", real_run)
    monkeypatch.delenv("RAMP_ROUTER_BASE_URL")
    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    store = json.loads(fake_hermes.store_path.read_text())
    assert "router" not in store.get("providers", {})
    assert "model" not in store
    assert "RAMP_ROUTER_API_KEY" not in (fake_hermes.home / ".env").read_text()
    assert not (fake_hermes.home / "ramp-router-state.json").exists()


def test_a_key_rotated_back_to_an_old_cli_write_is_the_users(fake_hermes, monkeypatch):
    """Ownership covers the written and pending digests, never older history.

    After a completed reconfigure the receipt records only the newest key as
    written (no pending). A user who then deliberately rotates .env back to
    an earlier CLI-written value has made their own edit, and unconfigure
    must leave it alone rather than restoring the pre-Router snapshot over
    the only copy.
    """
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    receipt = fake_hermes.home / "ramp-router-state.json"
    # A later reconfigure completed with a second key: written digest is the
    # new key's, and no pending digest remains.
    state = json.loads(receipt.read_text())
    state["written_env_key_sha256"] = hashlib.sha256(b"second-secret").hexdigest()
    receipt.write_text(json.dumps(state))
    # The user then rotated back to the first key by hand.
    hermes_agent.write_env_value("RAMP_ROUTER_API_KEY", "router-secret")

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    assert (
        "RAMP_ROUTER_API_KEY=router-secret" in (fake_hermes.home / ".env").read_text()
    )
    assert not receipt.exists()


def test_unconfigure_does_not_resurrect_a_field_the_user_deleted(
    fake_hermes, monkeypatch
):
    """Deleting a snapshot-only field after setup is a user edit that sticks.

    Configure and restore only ever write leaves, so a pre-existing custom
    field missing from the live entry can only mean the user removed it;
    replaying the snapshot would bring it back against their intent, so the
    whole entry stays theirs.
    """
    fake_hermes._save(
        {
            "providers": {
                "router": {"base_url": "https://old.example/v1", "note": "mine"}
            }
        }
    )
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    # The user deletes the custom field configure never touched.
    store = json.loads(fake_hermes.store_path.read_text())
    del store["providers"]["router"]["note"]
    fake_hermes.store_path.write_text(json.dumps(store))

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    store = json.loads(fake_hermes.store_path.read_text())
    assert "note" not in store["providers"]["router"]


def test_unconfigure_does_not_resurrect_a_key_the_user_deleted(
    fake_hermes, monkeypatch
):
    """Deleting the key line after setup is a user edit that sticks.

    The snapshot held a pre-Router key, but the user removed the variable
    entirely after configuration; restoring the snapshot would resurrect a
    credential they deliberately deleted.
    """
    env_file = fake_hermes.home / ".env"
    env_file.write_text("RAMP_ROUTER_API_KEY=pre-router-secret\n")
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    # The user deletes the variable outright.
    hermes_agent.write_env_value("RAMP_ROUTER_API_KEY", None)

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    assert "RAMP_ROUTER_API_KEY" not in env_file.read_text()
    assert not (fake_hermes.home / "ramp-router-state.json").exists()


def test_unconfigure_tears_down_around_a_user_added_field(fake_hermes, monkeypatch):
    """An unrelated user field must not stop the Router teardown.

    Ownership is leaf-scoped: the user's custom field is preserved exactly,
    while the CLI-managed endpoint leaves, the credential, and the model
    selection are still cleaned up rather than abandoned wholesale.
    """
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    # The user adds an unrelated field under the CLI-created entry.
    store = json.loads(fake_hermes.store_path.read_text())
    store["providers"]["router"]["note"] = "mine"
    fake_hermes.store_path.write_text(json.dumps(store))

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    store = json.loads(fake_hermes.store_path.read_text())
    assert store["providers"]["router"] == {"note": "mine"}
    assert "model" not in store
    assert "RAMP_ROUTER_API_KEY" not in (fake_hermes.home / ".env").read_text()
    assert not (fake_hermes.home / "ramp-router-state.json").exists()


def test_a_failed_receipt_write_is_a_per_client_failure(fake_hermes, monkeypatch):
    """Receipt write failures report Hermes as the failed client, not a crash."""
    _mock_models(monkeypatch)

    def failing_write(path, content):
        raise OSError("disk full")

    monkeypatch.setattr(router_module, "_write_private_file", failing_write)
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Could not write" in result.output


def test_unconfigure_recovers_from_two_failed_first_time_configures(
    fake_hermes, monkeypatch
):
    """Failed retries must not orphan what an earlier failed attempt wrote.

    A first-time configure toward endpoint A fails after writing the
    endpoint; a retry toward endpoint B replaces nothing on disk but records
    its own pending marker. With only one pending slot, A's live leaves
    would read as user edits; the pending set keeps every unpromoted
    generation, so unconfigure still tears the CLI's own writes down.
    """
    _mock_models(monkeypatch)
    runner = CliRunner()
    real_run = fake_hermes.run

    def fail_after_endpoint(argv, **kwargs):
        if argv[2] == "set" and argv[4] == "providers.router.api_mode":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(hermes_agent.subprocess, "run", fail_after_endpoint)
    first = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert first.exit_code != 0
    store = json.loads(fake_hermes.store_path.read_text())
    assert store["providers"]["router"]["base_url"] == ROUTER_BASE_URL

    # The retry aims at a different Router and fails before writing anything.
    new_base = "https://router.staging.example/v1"
    monkeypatch.setenv("RAMP_ROUTER_BASE_URL", new_base)

    def get_new_endpoint(url, *, headers, timeout):
        if "/session-usage/usage/balance" in url or url.endswith(
            ("/claude-code-statusline", "/codex-cost-hook")
        ):
            return httpx.Response(404, request=httpx.Request("GET", url))
        assert url == f"{new_base}/models"
        return httpx.Response(
            200,
            json={"data": [{"id": "gpt-5.4", "router": _router_metadata("gpt-5.4")}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("ramp_cli.commands.router.httpx.get", get_new_endpoint)

    def fail_on_endpoint(argv, **kwargs):
        if argv[2] == "set" and argv[4] == "providers.router.base_url":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(hermes_agent.subprocess, "run", fail_on_endpoint)
    second = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="\n",
    )
    assert second.exit_code != 0
    state = json.loads((fake_hermes.home / "ramp-router-state.json").read_text())
    assert "written_provider_entry" not in state
    assert [m["base_url"] for m in state["pending_provider_entries"]] == [
        ROUTER_BASE_URL,
        new_base,
    ]

    monkeypatch.setattr(hermes_agent.subprocess, "run", real_run)
    monkeypatch.delenv("RAMP_ROUTER_BASE_URL")
    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    store = json.loads(fake_hermes.store_path.read_text())
    assert "router" not in store.get("providers", {})
    assert "RAMP_ROUTER_API_KEY" not in (fake_hermes.home / ".env").read_text()
    assert not (fake_hermes.home / "ramp-router-state.json").exists()


def test_unconfigure_does_not_resurrect_an_entry_the_user_deleted(
    fake_hermes, monkeypatch
):
    """Deleting the whole providers.router mapping after setup sticks.

    The snapshot held a pre-Router entry, but the user removed the mapping
    entirely after configuration; replaying the snapshot would resurrect a
    provider they deliberately deleted. The CLI-owned credential and model
    are still cleaned up.
    """
    fake_hermes._save({"providers": {"router": {"base_url": "https://old.example/v1"}}})
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    # The user deletes the entire provider mapping.
    store = json.loads(fake_hermes.store_path.read_text())
    del store["providers"]["router"]
    fake_hermes.store_path.write_text(json.dumps(store))

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    store = json.loads(fake_hermes.store_path.read_text())
    assert "router" not in store.get("providers", {})
    assert "model" not in store
    assert "RAMP_ROUTER_API_KEY" not in (fake_hermes.home / ".env").read_text()
    assert not (fake_hermes.home / "ramp-router-state.json").exists()


def test_unconfigure_preserves_unrelated_model_fields(fake_hermes, monkeypatch):
    """A user field under model survives the CLI's provider/default restore."""
    fake_hermes._save({"model": "anthropic/claude-sonnet-4"})
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    # The user adds an unrelated model field after setup.
    store = json.loads(fake_hermes.store_path.read_text())
    store["model"]["custom"] = "keep"
    fake_hermes.store_path.write_text(json.dumps(store))

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    store = json.loads(fake_hermes.store_path.read_text())
    # provider is gone, the snapshot default is back, the user's field stays.
    assert store["model"] == {"default": "anthropic/claude-sonnet-4", "custom": "keep"}


def test_a_failed_receipt_removal_is_a_per_client_failure(fake_hermes, monkeypatch):
    """Receipt deletion failures report Hermes as the failed client, not a crash."""
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output

    real_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self.name == "ramp-router-state.json":
            raise OSError("read-only file system")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code != 0
    assert unconfigure.exception is None or isinstance(
        unconfigure.exception, SystemExit
    )
    assert "Could not remove" in unconfigure.output


def test_unconfigure_does_not_resurrect_a_deleted_model_field(fake_hermes, monkeypatch):
    """A snapshot model field the user deleted after setup stays deleted."""
    fake_hermes._save(
        {"model": {"default": "anthropic/claude-sonnet-4", "custom": "old"}}
    )
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    # The user deletes the unrelated field configure never touched.
    store = json.loads(fake_hermes.store_path.read_text())
    del store["model"]["custom"]
    fake_hermes.store_path.write_text(json.dumps(store))

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    store = json.loads(fake_hermes.store_path.read_text())
    assert store["model"] == {"default": "anthropic/claude-sonnet-4"}


def test_a_stale_receipt_after_failed_deletion_never_reenrolls(
    fake_hermes, monkeypatch
):
    """A completed teardown whose receipt survives must stay torn down.

    The receipt is marked terminal before the restore writes, so even when
    its deletion fails, refresh treats Hermes as unconfigured instead of
    reading the restored key and reapplying the removed setup. A retry of
    unconfigure completes the deletion; an explicit configure re-enrolls.
    """
    env_file = fake_hermes.home / ".env"
    env_file.write_text("RAMP_ROUTER_API_KEY=pre-router-secret\n")
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output

    real_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self.name == "ramp-router-state.json":
            raise OSError("read-only file system")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    first = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])
    assert first.exit_code != 0
    # Hermes was restored; only the receipt is left, marked terminal.
    assert "RAMP_ROUTER_API_KEY=pre-router-secret" in env_file.read_text()
    receipt = fake_hermes.home / "ramp-router-state.json"
    assert json.loads(receipt.read_text())["unconfigured"] is True
    assert "hermes" not in router_module.configured_router_clients()

    # A background refresh must not read the restored key or fetch models.
    def no_requests(url, **_kwargs):
        raise AssertionError(f"refresh must not fetch after teardown: {url}")

    monkeypatch.setattr("ramp_cli.commands.router.httpx.get", no_requests)
    refresh = runner.invoke(cli, ["--human", "router", "refresh"])
    assert refresh.exit_code == 0, refresh.output
    assert "not configured in any coding agent" in refresh.output
    _mock_models(monkeypatch)

    # Retrying the unconfigure finishes the deletion.
    monkeypatch.setattr(Path, "unlink", real_unlink)
    second = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])
    assert second.exit_code == 0, second.output
    assert not receipt.exists()

    # And an explicit configure afterwards enrolls fresh, clearing the mark.
    reconfigure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert reconfigure.exit_code == 0, reconfigure.output
    assert json.loads(receipt.read_text()).get("unconfigured") is None
    assert "hermes" in router_module.configured_router_clients()


def test_reconfigure_over_a_stale_teardown_receipt_is_refused(fake_hermes, monkeypatch):
    """A stale teardown receipt is never reused as an enrollment snapshot.

    The teardown completed but its receipt survived; the user then reshapes
    Hermes. Reconfiguring over that receipt would let a later unconfigure
    replay the obsolete snapshot over the intervening settings, so configure
    refuses until the receipt is cleared — after which a fresh configure
    snapshots the live state.
    """
    env_file = fake_hermes.home / ".env"
    env_file.write_text("RAMP_ROUTER_API_KEY=pre-router-secret\n")
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output

    real_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self.name == "ramp-router-state.json":
            raise OSError("read-only file system")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    teardown = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])
    assert teardown.exit_code != 0
    monkeypatch.setattr(Path, "unlink", real_unlink)

    # The user reshapes Hermes after the completed teardown.
    hermes_agent.write_env_value("RAMP_ROUTER_API_KEY", "user-new-key")

    reconfigure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert reconfigure.exit_code != 0
    assert "could not delete its receipt" in reconfigure.output
    # Nothing was overwritten by the refused attempt.
    assert "RAMP_ROUTER_API_KEY=user-new-key" in env_file.read_text()

    # Clearing the receipt lets a fresh configure snapshot the live state.
    (fake_hermes.home / "ramp-router-state.json").unlink()
    fresh = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert fresh.exit_code == 0, fresh.output
    state = json.loads((fake_hermes.home / "ramp-router-state.json").read_text())
    assert state["env_key"] == "RAMP_ROUTER_API_KEY=user-new-key"

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])
    assert unconfigure.exit_code == 0, unconfigure.output
    assert "RAMP_ROUTER_API_KEY=user-new-key" in env_file.read_text()


def test_an_incomplete_teardown_receipt_refuses_manual_deletion(
    fake_hermes, monkeypatch
):
    """A mid-restore failure keeps the receipt as the only recovery snapshot.

    Configure over it is refused without offering manual deletion — deleting
    would discard the pre-Router snapshot the retry needs — and a retried
    unconfigure completes the recovery.
    """
    env_file = fake_hermes.home / ".env"
    env_file.write_text("RAMP_ROUTER_API_KEY=pre-router-secret\n")
    fake_hermes._save({"providers": {"router": {"base_url": "https://old.example/v1"}}})
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output

    real_run = fake_hermes.run

    def failing_restore(argv, **kwargs):
        if argv[2] == "set" and argv[4].startswith("providers.router."):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(hermes_agent.subprocess, "run", failing_restore)
    teardown = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])
    assert teardown.exit_code != 0
    receipt = fake_hermes.home / "ramp-router-state.json"
    state = json.loads(receipt.read_text())
    assert state["unconfigured"] is True
    assert "restore_complete" not in state

    monkeypatch.setattr(hermes_agent.subprocess, "run", real_run)
    reconfigure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert reconfigure.exit_code != 0
    assert "could not delete its receipt" in reconfigure.output
    assert "delete the file" not in reconfigure.output

    retry = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])
    assert retry.exit_code == 0, retry.output
    assert not receipt.exists()
    store = json.loads(fake_hermes.store_path.read_text())
    assert store["providers"]["router"] == {"base_url": "https://old.example/v1"}
    assert "RAMP_ROUTER_API_KEY=pre-router-secret" in env_file.read_text()


def test_duplicate_env_assignments_snapshot_the_effective_value(
    fake_hermes, monkeypatch
):
    """With duplicate key lines, the last assignment — dotenv's winner — rules.

    The snapshot records the effective definition, so unconfigure restores
    the credential Hermes was actually using instead of an earlier shadowed
    line, and the user's other entries survive untouched.
    """
    env_file = fake_hermes.home / ".env"
    env_file.write_text(
        "RAMP_ROUTER_API_KEY=shadowed-old\n"
        "OTHER_KEY=untouched\n"
        "RAMP_ROUTER_API_KEY=effective-key\n"
    )
    assert hermes_agent.read_env_value("RAMP_ROUTER_API_KEY") == "effective-key"
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    lines = env_file.read_text().splitlines()
    assert "OTHER_KEY=untouched" in lines
    assert lines.count("RAMP_ROUTER_API_KEY=effective-key") == 1
    assert not any("shadowed-old" in line for line in lines)


def test_unconfigure_keeps_a_key_another_provider_references(fake_hermes, monkeypatch):
    """A user provider built around the CLI-written key keeps its credential.

    The user added their own provider entry whose key_env names
    RAMP_ROUTER_API_KEY and switched Hermes to it. Tearing down
    providers.router must not delete the variable that entry depends on.
    """
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    store = json.loads(fake_hermes.store_path.read_text())
    store["providers"]["custom"] = {
        "base_url": "https://my-proxy.example/v1",
        "key_env": "RAMP_ROUTER_API_KEY",
    }
    store["model"] = {"provider": "custom", "default": "gpt-5.4"}
    fake_hermes.store_path.write_text(json.dumps(store))

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    store = json.loads(fake_hermes.store_path.read_text())
    # The CLI-owned router entry is gone; the user's provider and model stay.
    assert "router" not in store["providers"]
    assert store["providers"]["custom"]["key_env"] == "RAMP_ROUTER_API_KEY"
    # The provider switch and its model pairing are the user's and stay.
    assert store["model"] == {"provider": "custom", "default": "gpt-5.4"}
    # The credential the surviving provider depends on stays with it.
    assert (
        "RAMP_ROUTER_API_KEY=router-secret" in (fake_hermes.home / ".env").read_text()
    )
    assert not (fake_hermes.home / "ramp-router-state.json").exists()


def test_a_preexisting_provider_reference_gets_the_snapshot_key_back(
    fake_hermes, monkeypatch
):
    """A provider that referenced the key before setup expects it restored.

    Configure overwrote the credential that provider was using; keeping the
    CLI-written key at teardown would leave it authenticated with the wrong
    secret, so the pre-Router value comes back.
    """
    env_file = fake_hermes.home / ".env"
    env_file.write_text("RAMP_ROUTER_API_KEY=pre-router-secret\n")
    fake_hermes._save(
        {
            "providers": {
                "mirror": {
                    "base_url": "https://mirror.example/v1",
                    "key_env": "RAMP_ROUTER_API_KEY",
                }
            }
        }
    )
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    state = json.loads((fake_hermes.home / "ramp-router-state.json").read_text())
    assert list(state["preexisting_key_env_providers"]) == ["mirror"]

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    store = json.loads(fake_hermes.store_path.read_text())
    assert store["providers"]["mirror"]["key_env"] == "RAMP_ROUTER_API_KEY"
    assert "RAMP_ROUTER_API_KEY=pre-router-secret" in env_file.read_text()


def test_a_failed_scalar_model_restore_is_retryable(fake_hermes, monkeypatch):
    """A crash between the model unset and its scalar re-set is recoverable.

    The first attempt leaves the model absent with the marked receipt still
    on disk; the retry recognizes its own interrupted restore and finishes
    it instead of deleting the receipt with the snapshot inside.
    """
    fake_hermes._save({"model": "anthropic/claude-sonnet-4"})
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output

    real_run = fake_hermes.run

    def fail_scalar_model_set(argv, **kwargs):
        if argv[2] == "set" and argv[4] == "model":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(hermes_agent.subprocess, "run", fail_scalar_model_set)
    first = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])
    assert first.exit_code != 0
    store = json.loads(fake_hermes.store_path.read_text())
    assert "model" not in store
    assert (fake_hermes.home / "ramp-router-state.json").exists()

    # The real `hermes config get model --json` answers an *empty default*
    # rather than a miss when model is unset (verified against the live
    # binary); the retry must recognize that shape as absent too.
    def empty_default_run(argv, **kwargs):
        if argv[2] == "get" and argv[3] == "model":
            data = json.loads(fake_hermes.store_path.read_text())
            if "model" not in data:
                return subprocess.CompletedProcess(argv, 0, stdout='""', stderr="")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(hermes_agent.subprocess, "run", empty_default_run)
    second = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert second.exit_code == 0, second.output
    store = json.loads(fake_hermes.store_path.read_text())
    assert store["model"] == "anthropic/claude-sonnet-4"
    assert not (fake_hermes.home / "ramp-router-state.json").exists()


def test_a_quoted_preexisting_key_is_restored_verbatim(fake_hermes, monkeypatch):
    """An exported, dotenv-quoted key keeps its exact line through the trip.

    Stripping the quotes restored a line whose spaced '#' parses as a
    comment, and dropping the export prefix broke consumers that source the
    shared .env — the snapshot now carries the assignment verbatim.
    """
    env_file = fake_hermes.home / ".env"
    env_file.write_text("export RAMP_ROUTER_API_KEY='secret # suffix'\n")
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    state = json.loads((fake_hermes.home / "ramp-router-state.json").read_text())
    assert state["env_key"] == "export RAMP_ROUTER_API_KEY='secret # suffix'"

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    assert (
        "export RAMP_ROUTER_API_KEY='secret # suffix'"
        in env_file.read_text().splitlines()
    )


def test_an_unpromoted_first_configure_never_enrolls_in_refresh(
    fake_hermes, monkeypatch
):
    """A first configure that failed before promotion is not a live setup.

    Its receipt holds only pending markers while the pre-existing key sits
    untouched in .env; enrolling that would let a background refresh pair
    the user's own credential with whatever endpoint is configured and
    complete an enrollment the user never finished. An explicit configure
    remains the completion path.
    """
    env_file = fake_hermes.home / ".env"
    env_file.write_text("RAMP_ROUTER_API_KEY=pre-router-secret\n")
    _mock_models(monkeypatch)
    runner = CliRunner()

    real_write_env_value = hermes_agent.write_env_value

    def refuse_key_write(name, value):
        raise router_module.click.ClickException("boom")

    monkeypatch.setattr(hermes_agent, "write_env_value", refuse_key_write)
    first = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert first.exit_code != 0
    receipt = fake_hermes.home / "ramp-router-state.json"
    state = json.loads(receipt.read_text())
    assert "written_provider_entry" not in state
    assert state["pending_provider_entries"]

    monkeypatch.setattr(hermes_agent, "write_env_value", real_write_env_value)
    assert "hermes" not in router_module.configured_router_clients()

    def no_requests(url, **_kwargs):
        raise AssertionError(f"refresh must not fetch for an unpromoted setup: {url}")

    monkeypatch.setattr("ramp_cli.commands.router.httpx.get", no_requests)
    refresh = runner.invoke(cli, ["--human", "router", "refresh"])
    assert refresh.exit_code == 0, refresh.output
    assert "not configured in any coding agent" in refresh.output
    _mock_models(monkeypatch)

    # The explicit completion path still works and enrolls.
    retry = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert retry.exit_code == 0, retry.output
    assert "hermes" in router_module.configured_router_clients()


def test_dotenv_decoding_of_quoted_values_with_comments(fake_hermes):
    """Quoted values decode to their content; trailing comments are ignored."""
    env_file = fake_hermes.home / ".env"
    env_file.write_text("RAMP_ROUTER_API_KEY='secret' # note\n")
    assert hermes_agent.read_env_value("RAMP_ROUTER_API_KEY") == "secret"
    env_file.write_text('RAMP_ROUTER_API_KEY="secret two" # note\n')
    assert hermes_agent.read_env_value("RAMP_ROUTER_API_KEY") == "secret two"
    env_file.write_text("RAMP_ROUTER_API_KEY=plain # comment\n")
    assert hermes_agent.read_env_value("RAMP_ROUTER_API_KEY") == "plain"


def test_a_corrupt_receipt_never_enrolls_refresh(fake_hermes, monkeypatch):
    """Refresh fails closed on a corrupt receipt instead of fetching first.

    Enrollment happens before any receipt parsing on the refresh path, so
    an optimistic answer would transmit the stored key — possibly toward a
    user-modified endpoint — before the corrupt state is ever reported.
    """
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    (fake_hermes.home / "ramp-router-state.json").write_text("not json{")

    assert "hermes" not in router_module.configured_router_clients()

    def no_requests(url, **_kwargs):
        raise AssertionError(f"refresh must not fetch with a corrupt receipt: {url}")

    monkeypatch.setattr("ramp_cli.commands.router.httpx.get", no_requests)
    refresh = runner.invoke(cli, ["--human", "router", "refresh"])
    assert refresh.exit_code == 0, refresh.output
    assert "not configured in any coding agent" in refresh.output


def test_unconfigure_survives_a_deleted_managed_leaf(fake_hermes, monkeypatch):
    """Deleting one managed leaf does not surrender the whole teardown.

    The user removed base_url after setup; that deletion sticks, while the
    other managed leaves, the credential, and the model are still cleaned
    up instead of being abandoned with a success message.
    """
    fake_hermes._save({"providers": {"router": {"base_url": "https://old.example/v1"}}})
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    store = json.loads(fake_hermes.store_path.read_text())
    del store["providers"]["router"]["base_url"]
    fake_hermes.store_path.write_text(json.dumps(store))

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    store = json.loads(fake_hermes.store_path.read_text())
    # The deleted leaf stays deleted; the other managed leaves are gone too.
    assert store["providers"]["router"] == {}
    assert "model" not in store
    assert "RAMP_ROUTER_API_KEY" not in (fake_hermes.home / ".env").read_text()
    assert not (fake_hermes.home / "ramp-router-state.json").exists()


def test_dotenv_decoding_of_escaped_quotes(fake_hermes):
    """An escaped quote inside a quoted credential decodes to its literal."""
    env_file = fake_hermes.home / ".env"
    env_file.write_text('RAMP_ROUTER_API_KEY="se\\"cret" # note\n')
    assert hermes_agent.read_env_value("RAMP_ROUTER_API_KEY") == 'se"cret'
    env_file.write_text("RAMP_ROUTER_API_KEY='it\\'s-a-key'\n")
    assert hermes_agent.read_env_value("RAMP_ROUTER_API_KEY") == "it's-a-key"
    env_file.write_text('RAMP_ROUTER_API_KEY="back\\\\slash"\n')
    assert hermes_agent.read_env_value("RAMP_ROUTER_API_KEY") == "back\\slash"


def test_an_edited_preexisting_reference_keeps_the_live_key(fake_hermes, monkeypatch):
    """A pre-existing referencing provider the user edits expects the live key.

    They repointed its endpoint after setup while keeping key_env, rebuilding
    it around the credential currently on disk — restoring the snapshot key
    would leave their edited provider authenticated with the wrong secret.
    """
    env_file = fake_hermes.home / ".env"
    env_file.write_text("RAMP_ROUTER_API_KEY=pre-router-secret\n")
    fake_hermes._save(
        {
            "providers": {
                "mirror": {
                    "base_url": "https://mirror.example/v1",
                    "key_env": "RAMP_ROUTER_API_KEY",
                }
            }
        }
    )
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    # The user rebuilds the pre-existing provider around the live key.
    store = json.loads(fake_hermes.store_path.read_text())
    store["providers"]["mirror"]["base_url"] = "https://mirror-v2.example/v1"
    fake_hermes.store_path.write_text(json.dumps(store))

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    assert (
        "RAMP_ROUTER_API_KEY=router-secret" in (fake_hermes.home / ".env").read_text()
    )


def test_a_label_edit_keeps_a_preexisting_reference_preexisting(
    fake_hermes, monkeypatch
):
    """An unrelated edit to a pre-existing referencing provider changes nothing.

    Only its connection fields decide which credential it expects; a note
    added after setup must not stop the snapshot key from being restored.
    """
    env_file = fake_hermes.home / ".env"
    env_file.write_text("RAMP_ROUTER_API_KEY=pre-router-secret\n")
    fake_hermes._save(
        {
            "providers": {
                "mirror": {
                    "base_url": "https://mirror.example/v1",
                    "key_env": "RAMP_ROUTER_API_KEY",
                }
            }
        }
    )
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    store = json.loads(fake_hermes.store_path.read_text())
    store["providers"]["mirror"]["note"] = "my mirror"
    fake_hermes.store_path.write_text(json.dumps(store))

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    assert (
        "RAMP_ROUTER_API_KEY=pre-router-secret"
        in (fake_hermes.home / ".env").read_text()
    )


def test_a_pending_generation_blocks_refresh_enrollment(fake_hermes, monkeypatch):
    """A receipt with unpromoted markers never enrolls Hermes in refresh.

    A reconfigure toward a new endpoint writes the new key before the
    provider entry; refreshing mid-flight would send that credential to the
    previous generation's endpoint. Explicit configure and unconfigure are
    the recovery paths.
    """
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    assert "hermes" in router_module.configured_router_clients()
    # Simulate a reconfigure that failed after recording its pending marker.
    receipt = fake_hermes.home / "ramp-router-state.json"
    state = json.loads(receipt.read_text())
    state["pending_provider_entries"] = [
        {
            "base_url": "https://router.staging.example/v1",
            "api_mode": "codex_responses",
            "key_env": "RAMP_ROUTER_API_KEY",
        }
    ]
    receipt.write_text(json.dumps(state))

    assert "hermes" not in router_module.configured_router_clients()

    def no_requests(url, **_kwargs):
        raise AssertionError(f"refresh must not fetch mid-generation: {url}")

    monkeypatch.setattr("ramp_cli.commands.router.httpx.get", no_requests)
    refresh = runner.invoke(cli, ["--human", "router", "refresh"])
    assert refresh.exit_code == 0, refresh.output
    assert "not configured in any coding agent" in refresh.output


def test_unconfigure_cleans_a_default_whose_provider_leaf_was_deleted(
    fake_hermes, monkeypatch
):
    """Deleting model.provider does not orphan the CLI-written default.

    The provider-leaf deletion sticks, but the CLI's model id must not be
    left dangling under Hermes's fallback provider; the pre-Router
    selection comes back.
    """
    fake_hermes._save({"model": "anthropic/claude-sonnet-4"})
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    store = json.loads(fake_hermes.store_path.read_text())
    del store["model"]["provider"]
    fake_hermes.store_path.write_text(json.dumps(store))

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    store = json.loads(fake_hermes.store_path.read_text())
    # Only the default leaf is ours in this state; Hermes reads a
    # {"default": ...} mapping and a scalar model identically.
    assert store["model"] == {"default": "anthropic/claude-sonnet-4"}


def test_a_deleted_provider_leaf_stays_deleted_with_a_mapping_snapshot(
    fake_hermes, monkeypatch
):
    """A mapping snapshot does not resurrect the deleted model.provider leaf.

    The CLI-written default is still cleaned up (restored to the snapshot
    value), while the provider leaf the user deleted stays gone.
    """
    fake_hermes._save({"model": {"provider": "fallback", "default": "old"}})
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    store = json.loads(fake_hermes.store_path.read_text())
    del store["model"]["provider"]
    fake_hermes.store_path.write_text(json.dumps(store))

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    store = json.loads(fake_hermes.store_path.read_text())
    assert store["model"] == {"default": "old"}


def test_a_provider_switch_keeps_the_users_model_pairing(fake_hermes, monkeypatch):
    """Switching model.provider keeps the model the user paired with it.

    Retaining the same model id under a new provider is a deliberate user
    pairing; value-equality with the CLI's marker alone is not evidence of
    ownership once the provider is no longer Router.
    """
    fake_hermes._save({"model": "anthropic/claude-sonnet-4"})
    _mock_models(monkeypatch)
    runner = CliRunner()
    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "hermes"],
        input="router-secret\n",
    )
    assert configure.exit_code == 0, configure.output
    store = json.loads(fake_hermes.store_path.read_text())
    store["model"]["provider"] = "fallback"
    fake_hermes.store_path.write_text(json.dumps(store))

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "hermes"])

    assert unconfigure.exit_code == 0, unconfigure.output
    store = json.loads(fake_hermes.store_path.read_text())
    assert store["model"] == {"provider": "fallback", "default": "gpt-5.4"}
