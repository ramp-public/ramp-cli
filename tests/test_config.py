"""Tests for config load/save and environment resolution."""

from __future__ import annotations

import json

from click.testing import CliRunner

from ramp_cli.config import settings
from ramp_cli.config.constants import ENV_PRODUCTION, ENV_SANDBOX
from ramp_cli.main import cli


def test_load_missing_file(isolated_config):
    cfg = settings.load()
    assert cfg.environment == ""
    assert cfg.format == ""


def test_save_and_load_roundtrip(isolated_config):
    cfg = settings.Config()
    cfg.environment = "production"
    cfg.format = "json"
    cfg.sandbox.access_token = "sandbox-tok"
    cfg.sandbox.access_token_issued_at = 100
    cfg.sandbox.access_token_expires_in = 300
    settings.save(cfg)

    loaded = settings.load()
    assert loaded.environment == "production"
    assert loaded.format == "json"
    assert loaded.sandbox.access_token == "sandbox-tok"
    assert loaded.sandbox.access_token_issued_at == 100
    assert loaded.sandbox.access_token_expires_in == 300


def test_resolve_environment_flag_wins(isolated_config):
    cfg = settings.Config()
    cfg.environment = "production"
    settings.save(cfg)

    assert settings.resolve_environment("sandbox") == ENV_SANDBOX


def test_resolve_environment_env_var(isolated_config, monkeypatch):
    monkeypatch.setenv("RAMP_ENVIRONMENT", "production")
    assert settings.resolve_environment() == ENV_PRODUCTION


def test_resolve_environment_config(isolated_config):
    cfg = settings.Config()
    cfg.environment = "production"
    settings.save(cfg)

    assert settings.resolve_environment() == ENV_PRODUCTION


def test_resolve_environment_default(isolated_config):
    assert settings.resolve_environment() == ENV_PRODUCTION


def test_normalize_env_prod_alias(isolated_config):
    assert settings.resolve_environment("prod") == ENV_PRODUCTION


def test_config_file_permissions(isolated_config):
    cfg = settings.Config()
    cfg.environment = "sandbox"
    settings.save(cfg)

    path = settings.config_path()
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


class TestConfigAgentMode:
    def test_config_set_agent_json(self, isolated_config):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--agent", "config", "set", "environment", "production"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["schema_version"] == "1.0"
        assert data["data"][0]["key"] == "environment"
        assert data["data"][0]["value"] == "production"

    def test_config_get_agent_json(self, isolated_config):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--agent", "config", "get", "environment"], catch_exceptions=False
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["schema_version"] == "1.0"
        assert data["data"][0]["key"] == "environment"

    def test_config_list_agent_json(self, isolated_config):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--agent", "config", "list"], catch_exceptions=False
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["schema_version"] == "1.0"
        assert isinstance(data["data"], list)

    def test_config_path_agent_json(self, isolated_config):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--agent", "config", "path"], catch_exceptions=False
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["schema_version"] == "1.0"
        assert "path" in data["data"][0]


class TestConfigProdAlias:
    def test_config_set_prod_alias(self, isolated_config):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["config", "set", "environment", "prod"], catch_exceptions=False
        )
        assert result.exit_code == 0
        cfg = settings.load()
        assert cfg.environment == "production"


class TestOutputValidation:
    def test_invalid_output_format(self, isolated_config):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--output", "yaml", "config", "list"], catch_exceptions=False
        )
        assert result.exit_code != 0
        assert (
            "unsupported format" in result.output.lower()
            or "unsupported format" in (result.output + str(result.exception)).lower()
        )
