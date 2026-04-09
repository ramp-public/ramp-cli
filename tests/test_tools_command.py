"""Tests for the ramp tools command group."""

from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from ramp_cli.main import cli
from ramp_cli.tools.parser import ToolDef


@pytest.fixture()
def runner():
    return CliRunner()


FAKE_TOOLS = [
    ToolDef(
        name="get-funds",
        description="List funds",
        summary="List funds",
        path="/v1/agent-tools/get-funds",
        http_method="POST",
        category="funds",
    ),
    ToolDef(
        name="get-transactions",
        description="List transactions",
        summary="List transactions",
        path="/v1/agent-tools/get-transactions",
        http_method="POST",
        category="transactions",
    ),
]


class TestToolsRefresh:
    @patch("ramp_cli.commands.tools.reload")
    @patch("ramp_cli.commands.tools.fetch_spec", return_value=5)
    @patch("ramp_cli.main.maybe_sync")
    def test_refresh_human(self, _mock_sync, mock_fetch, mock_reload, runner):
        result = runner.invoke(cli, ["--human", "tools", "refresh"])
        assert result.exit_code == 0
        assert "Refreshed" in result.output
        assert "5 tools" in result.output
        mock_fetch.assert_called_once_with("production")
        mock_reload.assert_called_once_with("production")

    @patch("ramp_cli.commands.tools.reload")
    @patch("ramp_cli.commands.tools.fetch_spec", return_value=3)
    @patch("ramp_cli.main.maybe_sync")
    def test_refresh_json(self, _mock_sync, mock_fetch, _mock_reload, runner):
        result = runner.invoke(cli, ["--agent", "tools", "refresh"])
        assert result.exit_code == 0
        assert '"refreshed": true' in result.output
        assert '"tool_count": 3' in result.output

    @patch("ramp_cli.commands.tools.reload")
    @patch("ramp_cli.commands.tools.fetch_spec", return_value=2)
    @patch("ramp_cli.main.maybe_sync")
    def test_refresh_respects_env(self, _mock_sync, mock_fetch, _mock_reload, runner):
        result = runner.invoke(cli, ["--env", "sandbox", "tools", "refresh"])
        assert result.exit_code == 0
        mock_fetch.assert_called_once_with("sandbox")

    @patch(
        "ramp_cli.commands.tools.fetch_spec",
        side_effect=httpx.ConnectError("offline"),
    )
    @patch("ramp_cli.main.maybe_sync")
    def test_refresh_network_error(self, _mock_sync, _mock_fetch, runner):
        result = runner.invoke(cli, ["--human", "tools", "refresh"])
        assert result.exit_code != 0
        assert "Failed to fetch spec" in result.output


FAKE_CATEGORIES = {
    "funds": [FAKE_TOOLS[0]],
    "transactions": [FAKE_TOOLS[1]],
}


class TestToolsList:
    @patch(
        "ramp_cli.commands.tools.list_categories",
        return_value=FAKE_CATEGORIES,
    )
    @patch("ramp_cli.commands.tools.maybe_sync")
    @patch("ramp_cli.main.maybe_sync")
    def test_list_human(self, _ms1, _ms2, _mock_cats, runner):
        result = runner.invoke(cli, ["--human", "tools", "list"])
        assert result.exit_code == 0
        assert "get-funds" in result.output or "Get Funds" in result.output
        assert "2 tools" in result.output

    @patch(
        "ramp_cli.commands.tools.list_categories",
        return_value=FAKE_CATEGORIES,
    )
    @patch("ramp_cli.commands.tools.maybe_sync")
    @patch("ramp_cli.main.maybe_sync")
    def test_list_json(self, _ms1, _ms2, _mock_cats, runner):
        result = runner.invoke(cli, ["--agent", "tools", "list"])
        assert result.exit_code == 0
        assert "get-funds" in result.output
        assert "get-transactions" in result.output
        assert '"category": "funds"' in result.output

    @patch(
        "ramp_cli.commands.tools.list_categories",
        return_value=FAKE_CATEGORIES,
    )
    @patch("ramp_cli.commands.tools.maybe_sync")
    @patch("ramp_cli.main.maybe_sync")
    def test_list_calls_maybe_sync(self, _ms1, mock_sync, _mock_cats, runner):
        runner.invoke(cli, ["tools", "list"])
        mock_sync.assert_called_once_with("production")


class TestToolsGroup:
    @patch("ramp_cli.main.maybe_sync")
    def test_tools_help(self, _mock_sync, runner):
        result = runner.invoke(cli, ["tools", "--help"])
        assert result.exit_code == 0
        assert "refresh" in result.output
        assert "list" in result.output
