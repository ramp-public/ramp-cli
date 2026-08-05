"""Tests for Ramp Router coding-agent configuration."""

from __future__ import annotations

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
from ramp_cli.commands.router import DEFAULT_ROUTER_BASE_URL as ROUTER_BASE_URL
from ramp_cli.main import cli


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
        assert set(headers) <= {"Authorization", "X-Gateway-Client"}
        if "X-Gateway-Client" in headers:
            assert headers["X-Gateway-Client"] == "claude-code"
        assert timeout == 10
        return httpx.Response(
            200, json={"data": models}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr("ramp_cli.commands.router.httpx.get", get)


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
            "apiKey": "keep-me",
        },
    ]
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

    def fetch_models(api_key):
        fetched_keys.append(api_key)
        return [object()]

    def configure_client(client, _api_key, models, *, selected_model):
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

    refreshed = CliRunner().invoke(cli, ["--human", "router", "refresh"])

    assert refreshed.exit_code == 0
    assert "Refreshed the Ramp Router configuration for Codex." in refreshed.output
    config = tomllib.loads(config_path.read_text())
    assert config["model"] == "b"
    assert config["model_providers"]["ramp-router"]["wire_api"] == "responses"


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
        lambda _api_key: [router_module.RouterModel(id="model", metadata=None)],
    )
    monkeypatch.setattr(router_module, "_configured_model", lambda _client, _path: None)
    monkeypatch.setattr(
        router_module,
        "_configure_client",
        lambda client, _api_key, _models, selected_model=None: (
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
    # No catalog file: Codex asks Router for the model list on every launch, so
    # a file here would freeze it at setup time.
    assert "model_catalog_json" not in config
    assert not (codex_home / "ramp-router-models.json").exists()
    assert key_path.read_text() == "router-secret"
    assert "Connecting Ramp Router to your coding agent" in result.output
    assert "Connected to: Codex" in result.output
    assert "2 models added. Start an agent and pick a model." in result.output
    assert "Create or copy an API key at https://router.ramp.com" in result.output
    assert "router-secret" not in result.output
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert key_path.stat().st_mode & 0o777 == 0o600


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
        cli, ["--human", "router", "configure"], input="router-secret\n"
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
    assert "Pass --api-key when using non-interactive mode" in result.output
    assert "https://router.ramp.com" in result.output
    assert not (tmp_path / "codex" / "config.toml").exists()


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
        cli, ["--human", "router", "configure", "codex"], input="router-secret\n"
    )

    assert result.exit_code != 0
    assert "wasn't accepted by Ramp Router" in result.output
    assert "https://router.ramp.com" in result.output
    assert "router-secret" not in result.output
    assert not (codex_home / "config.toml").exists()


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
    assert config["plugin"] == [
        [
            plugin_path.as_uri(),
            {
                "providerID": "ramp-router",
                "name": "Ramp Router",
                "baseURL": "https://router-api.ramp.com/v1",
                "apiKey": "router-secret",
            },
        ]
    ]
    assert config["model"] == "ramp-router/a"
    assert "Connecting Ramp Router to your coding agent" in result.output
    assert "Connected to: OpenCode" in result.output
    assert "2 models added. Start an agent and pick a model." in result.output
    assert "router-secret" not in result.output
    assert config_path.stat().st_mode & 0o777 == 0o600


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
    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "opencode"])

    assert configure.exit_code == unconfigure.exit_code == 0
    assert json.loads(config_path.read_text()) == original
    assert not (tmp_path / "ramp-router-state.json").exists()
    assert (
        "Removed Ramp Router and restored your previous settings for: OpenCode."
        in unconfigure.output
    )


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
    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure", "pi"])

    assert configure.exit_code == unconfigure.exit_code == 0
    assert json.loads((pi_home / "models.json").read_text()) == original_models
    assert json.loads((pi_home / "settings.json").read_text()) == original_settings
    assert json.loads((pi_home / "auth.json").read_text()) == original_auth
    assert not (pi_home / "ramp-router-state.json").exists()
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


def test_configure_and_unconfigure_without_client_targets_everything(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _mock_models(monkeypatch)
    runner = CliRunner()

    configure = runner.invoke(
        cli,
        ["--human", "router", "configure", "--api-key", "router-secret"],
    )

    assert configure.exit_code == 0
    assert configure.output.splitlines() == [
        "Connecting Ramp Router to your coding agents",
        # _mock_models serves no status line asset, so configure says why the
        # extra was skipped without failing anything.
        "Skipping the Claude Code Router status line: it could not be "
        "downloaded from https://router.ramp.com/claude-code-statusline.",
        "Connected to: Claude Code, Codex, OpenCode, and Pi",
        "1 model added. Start an agent and pick a model.",
        "Run 'ramp router unconfigure' to restore the previous settings.",
    ]
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


def test_codex_drops_a_catalog_left_by_an_earlier_version(tmp_path, monkeypatch):
    # A file left behind would keep overriding discovery forever, pinning the
    # user to whatever models existed when they last configured.
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
    assert not stale.exists()
    assert "model_catalog_json" not in tomllib.loads(
        (codex_home / "config.toml").read_text()
    )


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
