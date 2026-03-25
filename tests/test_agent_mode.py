"""Parametric test: agent mode outputs valid JSON for all hand-written commands."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from ramp_cli.main import cli


def _invoke(args):
    runner = CliRunner()
    return runner.invoke(cli, args, catch_exceptions=False)


@pytest.mark.parametrize(
    "args",
    [
        ["--agent", "config", "list"],
        ["--agent", "config", "get", "environment"],
        ["--agent", "config", "path"],
        ["--agent", "env"],
    ],
    ids=["config-list", "config-get", "config-path", "env"],
)
def test_agent_mode_valid_json(isolated_config, args):
    result = _invoke(args)
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "schema_version" in data


@pytest.mark.parametrize(
    "args",
    [
        ["--agent", "config", "set", "environment", "sandbox"],
    ],
    ids=["config-set"],
)
def test_agent_mode_set_commands(isolated_config, args):
    result = _invoke(args)
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "schema_version" in data


def test_agent_mode_auth_logout(isolated_config):
    result = _invoke(["--agent", "auth", "logout"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "schema_version" in data
