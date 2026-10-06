"""Compact Router menus, grouped actions and backwards-compatible shortcuts."""

import json

import httpx
import pytest
from click.testing import CliRunner

from ramp_cli.commands import router as router_module
from ramp_cli.main import cli


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(
        httpx.Client,
        "request",
        lambda *_a, **_kw: pytest.fail("Menu tests must not make network requests"),
    )


def test_main_router_menu_has_short_choices_and_workspace():
    group = router_module.router_group
    visible = {name for name, command in group.commands.items() if not command.hidden}
    assert visible == {
        "account",
        "configure",
        "keys",
        "refresh",
        "strategies",
        "subagents",
        "ui",
    }
    for name in visible:
        assert len(group.commands[name].short_help) <= 32
        assert "..." not in group.commands[name].short_help
    result = CliRunner().invoke(cli, ["--human", "router", "--help"], terminal_width=60)
    assert result.exit_code == 0, result.output
    assert "Connect or disconnect harnesses." in result.output
    assert "Choose subagent models." in result.output


def test_configure_help_contains_cursor_and_disconnect():
    result = CliRunner().invoke(cli, ["router", "configure", "--help"])
    assert result.exit_code == 0, result.output
    assert "connect" in result.output
    assert "cursor" in result.output
    assert "disconnect" in result.output


def test_bare_configure_without_the_workspace_starts_configuration(monkeypatch):
    seen = []
    monkeypatch.setattr(
        router_module, "_run_configure", lambda *_a, **kw: seen.append(kw["clients"])
    )
    result = CliRunner().invoke(cli, ["--human", "router", "configure"])
    assert result.exit_code == 0, result.output
    assert seen == [()]


@pytest.mark.parametrize(
    "path",
    [
        ["configure", "codex"],
        ["configure", "connect", "codex"],
    ],
)
def test_connect_preserves_old_path_flags_and_context(monkeypatch, path):
    seen = []

    def configure(ctx, **kwargs):
        seen.append((kwargs["clients"], kwargs["api_key"], ctx.obj["no_input"]))

    monkeypatch.setattr(router_module, "_run_configure", configure)
    result = CliRunner().invoke(
        cli, ["router", *path, "--api-key", "test-key", "--no-input"]
    )
    assert result.exit_code == 0, result.output
    assert seen == [(("codex",), "test-key", True)]


@pytest.mark.parametrize(
    "path", [["unconfigure", "codex"], ["configure", "disconnect", "codex"]]
)
def test_disconnect_keeps_the_legacy_alias(monkeypatch, path):
    seen = []
    monkeypatch.setattr(
        router_module, "_run_unconfigure", lambda _ctx, **kw: seen.append(kw["clients"])
    )
    result = CliRunner().invoke(cli, ["router", *path])
    assert result.exit_code == 0, result.output
    assert seen == [("codex",)]


@pytest.mark.parametrize("path", [["cursor"], ["configure", "cursor"]])
def test_cursor_keeps_the_legacy_alias_without_network(monkeypatch, path):
    monkeypatch.setattr(
        router_module, "acquire_router_api_key", lambda **_kw: "test-key"
    )
    monkeypatch.setattr(router_module, "_copy_to_clipboard", lambda *_a: True)
    monkeypatch.setattr(router_module, "_fetch_models", lambda *_a, **_kw: [])
    monkeypatch.setattr(router_module, "_ensure_claude_aliases", lambda *_a, **_kw: {})
    monkeypatch.setattr(
        router_module, "_fetch_configure_summary", lambda *_a, **_kw: None
    )
    result = CliRunner().invoke(
        cli, ["--human", "router", *path, "--api-key", "test-key"]
    )
    assert result.exit_code == 0, result.output
    assert "Cursor" in result.output


def test_account_shows_actions_and_agent_mode_returns_status():
    human = CliRunner().invoke(cli, ["--human", "router", "account"])
    assert human.exit_code == 0, human.output
    assert all(name in human.output for name in ("login", "logout", "status"))
    agent = CliRunner().invoke(cli, ["--agent", "router", "account"])
    assert agent.exit_code == 0, agent.output
    assert json.loads(agent.output)["data"][0]["authenticated"] is False


@pytest.mark.parametrize("path", [["status"], ["account", "status"]])
def test_account_status_keeps_the_legacy_alias(path):
    result = CliRunner().invoke(cli, ["--agent", "router", *path])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"][0]["authenticated"] is False
