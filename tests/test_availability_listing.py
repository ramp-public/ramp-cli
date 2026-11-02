"""Business-disabled tools are hidden from every command listing."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from ramp_cli.main import cli
from ramp_cli.tools.availability import AvailabilitySnapshot, ToolAvailability
from ramp_cli.tools.parser import ToolDef


def _tool(name: str, category: str) -> ToolDef:
    return ToolDef(
        name=name,
        description=f"{name} description",
        summary=name,
        path=f"/developer/v1/agent-tools/{name}",
        http_method="POST",
        category=category,
        operation_id=f"post_{name}",
    )


TOOLS = {
    "travel": [_tool("search-flights", "travel"), _tool("search-hotel", "travel")],
    "sourcing": [_tool("find-suppliers", "sourcing"), _tool("rank-quotes", "sourcing")],
    "agent": [_tool("list-agents", "agent")],
}
DISABLED = {"search-hotel", "find-suppliers", "rank-quotes", "list-agents"}
SNAPSHOT = AvailabilitySnapshot(
    content_hash="sha256:abc",
    entries={
        f"post_{name}": ToolAvailability(
            available=False, unavailable_reasons=("disabled_for_business",)
        )
        for name in DISABLED
    },
)


@pytest.fixture()
def fetches(monkeypatch):
    """Serve TOOLS and a fake availability endpoint that records each fetch."""
    state = SimpleNamespace(snapshot=SNAPSHOT, calls=[])

    def fake_fetch(env, profile=None):
        state.calls.append(env)
        return state.snapshot

    all_tools = [tool for tools in TOOLS.values() for tool in tools]
    monkeypatch.setattr("ramp_cli.main.maybe_sync", lambda env: None)
    monkeypatch.setattr("ramp_cli.tools.commands.maybe_sync", lambda env: None)
    monkeypatch.setattr("ramp_cli.main.list_categories", lambda env: TOOLS)
    monkeypatch.setattr("ramp_cli.tools.commands.list_tool_defs", lambda env: all_tools)
    monkeypatch.setattr("ramp_cli.tools.availability.fetch_availability", fake_fetch)
    return state


def test_group_help_hides_business_disabled_tools(fetches):
    result = CliRunner().invoke(cli, ["--human", "travel", "--help"])

    assert result.exit_code == 0, result.output
    assert "search-flights" in result.output
    assert "search-hotel" not in result.output


def test_generated_group_help_hides_disabled_tools_but_keeps_static(fetches):
    result = CliRunner().invoke(cli, ["--human", "agent", "--help"])

    assert result.exit_code == 0, result.output
    assert "login" in result.output
    assert "list-agents" not in result.output


def test_root_help_hides_fully_disabled_categories_with_one_fetch(fetches):
    result = CliRunner().invoke(cli, ["--human", "--help"])

    assert result.exit_code == 0, result.output
    assert "travel" in result.output
    assert "sourcing" not in result.output
    # Hand-written groups stay listed even when their generated tools are off.
    assert "agent" in result.output
    assert len(fetches.calls) == 1


def test_fail_open_without_availability_shows_everything(fetches):
    fetches.snapshot = None

    root = CliRunner().invoke(cli, ["--human", "--help"])
    group = CliRunner().invoke(cli, ["--human", "travel", "--help"])

    assert "sourcing" in root.output
    assert "search-hotel" in group.output


def test_direct_invocation_of_hidden_tool_still_reaches_core(fetches, monkeypatch):
    client = MagicMock()
    client.post.return_value = b"{}"
    monkeypatch.setattr("ramp_cli.tools.commands.RampClient", lambda env: client)

    result = CliRunner().invoke(cli, ["--agent", "travel", "search-hotel"])

    assert result.exit_code == 0, result.output
    assert client.post.called
    assert fetches.calls == []


@pytest.mark.parametrize("env_args", [["--env", "sandbox"], ["--env=sandbox"]])
def test_root_help_uses_env_flag(fetches, monkeypatch, env_args):
    envs: list[str] = []
    monkeypatch.setattr(
        "ramp_cli.main.list_categories", lambda env: envs.append(env) or TOOLS
    )

    result = CliRunner().invoke(cli, [*env_args, "--help"])

    assert result.exit_code == 0, result.output
    assert set(envs) == {"sandbox"}
    assert fetches.calls == ["sandbox"]
