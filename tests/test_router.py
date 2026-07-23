"""Tests for Ramp Router coding-agent configuration."""

from __future__ import annotations

import json
import tomllib

import httpx
from click.testing import CliRunner

from ramp_cli.commands.router import ROUTER_BASE_URL
from ramp_cli.main import cli


def _mock_models(monkeypatch, models=None):
    models = models or [{"id": "gpt-5.4"}]

    def get(url, *, headers, timeout):
        assert url == f"{ROUTER_BASE_URL}/models"
        assert headers == {"Authorization": "Bearer router-secret"}
        assert timeout == 10
        return httpx.Response(
            200, json={"data": models}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr("ramp_cli.commands.router.httpx.get", get)


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
    catalog_path = codex_home / "ramp-router-models.json"
    catalog = json.loads(catalog_path.read_text())
    assert config["model_providers"]["ramp-router"] == {
        "name": "Ramp Router",
        "base_url": "https://router-api.ramp.com/v1",
        "wire_api": "responses",
        "supports_websockets": False,
        "experimental_bearer_token": "router-secret",
    }
    assert "http_headers" not in config["model_providers"]["ramp-router"]
    assert config["model_provider"] == "ramp-router"
    assert config["model"] == "a"
    assert config["model_catalog_json"] == str(catalog_path.resolve())
    assert [model["slug"] for model in catalog["models"]] == ["a", "b"]
    assert all(model["visibility"] == "list" for model in catalog["models"])
    assert all(
        model["supports_reasoning_summaries"] is False
        and model["default_reasoning_summary"] == "none"
        and model["input_modalities"] == ["text", "image"]
        for model in catalog["models"]
    )
    assert "Added 2 Ramp Router model(s)" in result.output
    assert "Restart Codex, then open /model" in result.output
    assert "Create or copy an API key at https://router.ramp.com" in result.output
    assert "router-secret" not in result.output
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert catalog_path.stat().st_mode & 0o777 == 0o600


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
    assert "restored your previous Codex settings" in unconfigure.output


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
    assert (
        tomllib.loads((codex_home / "config.toml").read_text())["model_providers"][
            "ramp-router"
        ]["experimental_bearer_token"]
        == "router-secret"
    )
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


def test_configure_opencode_writes_provider_and_preserves_config(tmp_path, monkeypatch):
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
    assert config["provider"]["ramp-router"] == {
        "npm": "@ai-sdk/openai",
        "name": "Ramp Router",
        "options": {
            "baseURL": "https://router-api.ramp.com/v1",
            "apiKey": "router-secret",
        },
        "models": {"a": {"name": "a"}, "b": {"name": "b"}},
    }
    assert config["model"] == "ramp-router/a"
    assert "Restart OpenCode, then open /models" in result.output
    assert "router-secret" not in result.output
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_unconfigure_opencode_restores_previous_settings(tmp_path, monkeypatch):
    config_path = tmp_path / "opencode.json"
    original = {
        "model": "existing/model",
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
    assert "restored your previous OpenCode settings" in unconfigure.output


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


def test_configure_pi_writes_provider_and_is_idempotent(tmp_path, monkeypatch):
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
    first_content = config_path.read_text()
    second = runner.invoke(
        cli,
        ["--human", "router", "configure", "pi"],
        input="router-secret\n",
    )

    assert first.exit_code == second.exit_code == 0
    assert config_path.read_text() == first_content
    config = json.loads(first_content)
    assert config["providers"]["existing"] == {"baseUrl": "https://example.com"}
    assert config["providers"]["ramp-router"] == {
        "baseUrl": "https://router-api.ramp.com/v1",
        "api": "openai-responses",
        "apiKey": "router-secret",
        "models": [
            {"id": "a", "name": "a", "input": ["text", "image"]},
            {"id": "b", "name": "b", "input": ["text", "image"]},
        ],
    }
    assert json.loads(settings_path.read_text()) == {
        "defaultProvider": "ramp-router",
        "defaultModel": "a",
        "theme": "light",
    }
    assert "Open /model to choose one" in first.output


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
    }
    (pi_home / "models.json").write_text(json.dumps(original_models))
    (pi_home / "settings.json").write_text(json.dumps(original_settings))
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
    assert not (pi_home / "ramp-router-state.json").exists()
    assert "restored your previous Pi settings" in unconfigure.output


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
    _mock_models(monkeypatch)
    runner = CliRunner()

    configure = runner.invoke(
        cli,
        ["--human", "router", "configure"],
        input="router-secret\n",
    )

    assert configure.exit_code == 0
    assert "Added 1 Ramp Router model(s) to Codex" in configure.output
    assert "Added 1 Ramp Router model(s) to OpenCode" in configure.output
    assert "Added 1 Ramp Router model(s) to Pi" in configure.output
    assert (tmp_path / ".codex" / "config.toml").exists()
    assert (tmp_path / ".config" / "opencode" / "opencode.json").exists()
    assert (tmp_path / ".pi" / "agent" / "models.json").exists()
    assert (
        json.loads((tmp_path / ".pi" / "agent" / "settings.json").read_text())[
            "defaultProvider"
        ]
        == "ramp-router"
    )

    unconfigure = runner.invoke(cli, ["--human", "router", "unconfigure"])

    assert unconfigure.exit_code == 0
    assert "previous Codex settings" in unconfigure.output
    assert "previous OpenCode settings" in unconfigure.output
    assert "previous Pi settings" in unconfigure.output
    assert not (tmp_path / ".codex" / "ramp-router-state.json").exists()
    assert not (tmp_path / ".config" / "opencode" / "ramp-router-state.json").exists()
    assert not (tmp_path / ".pi" / "agent" / "ramp-router-state.json").exists()
