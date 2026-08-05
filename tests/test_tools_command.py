"""Tests for the ramp tools command group."""

import json
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from ramp_cli.auth import store
from ramp_cli.main import cli
from ramp_cli.specs import AGENT_TOOL_SPEC
from ramp_cli.tools.availability import AvailabilitySnapshot, ToolAvailability
from ramp_cli.tools.parser import ToolDef, parse_spec


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

COLLIDING_TOOLS = [
    ToolDef(
        name="get-user-trips",
        alias="list",
        description="List trips",
        summary="List trips",
        path="/developer/v1/agent-tools/get-user-trips",
        http_method="POST",
        category="travel",
        request_schema_name="GetUserTrips",
    ),
    ToolDef(
        name="list-eligible-travel-funds",
        alias="list",
        description="List eligible travel funds",
        summary="List eligible travel funds",
        path="/developer/v1/agent-tools/list-eligible-travel-funds",
        http_method="POST",
        category="travel",
        request_schema_name="ListEligibleTravelFunds",
    ),
]

FUND_COLLIDING_TOOLS = [
    ToolDef(
        name="get-agent-card-funds",
        alias="list",
        description="List agent card funds",
        summary="List agent card funds",
        path="/developer/v1/agent-tools/get-agent-card-funds",
        http_method="POST",
        category="agent_cards",
        required_scopes=["cards:read_agentic"],
        request_schema_name="GetAgentCardFunds",
    ),
    ToolDef(
        name="get-funds",
        alias="list",
        description="List funds",
        summary="List funds",
        path="/developer/v1/agent-tools/get-funds",
        http_method="POST",
        category="funds",
        required_scopes=["limits:read"],
        request_schema_name="GetFunds",
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


class TestToolsList:
    @patch(
        "ramp_cli.commands.tools.list_tool_defs",
        return_value=FAKE_TOOLS,
    )
    @patch("ramp_cli.commands.tools.maybe_sync")
    @patch("ramp_cli.main.maybe_sync")
    def test_list_human(self, _ms1, _ms2, _mock_cats, runner):
        result = runner.invoke(cli, ["--human", "tools", "list"])
        assert result.exit_code == 0
        assert "get-funds" in result.output or "Get Funds" in result.output
        assert "2 tools" in result.output

    @patch(
        "ramp_cli.commands.tools.list_tool_defs",
        return_value=FAKE_TOOLS,
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
        "ramp_cli.commands.tools.list_tool_defs",
        return_value=FAKE_TOOLS,
    )
    @patch("ramp_cli.commands.tools.maybe_sync")
    @patch("ramp_cli.main.maybe_sync")
    def test_list_calls_maybe_sync(self, _ms1, mock_sync, _mock_cats, runner):
        runner.invoke(cli, ["tools", "list"])
        mock_sync.assert_called_once_with("production")

    @patch(
        "ramp_cli.commands.tools.list_tool_defs",
        return_value=COLLIDING_TOOLS,
    )
    @patch("ramp_cli.commands.tools.maybe_sync")
    @patch("ramp_cli.main.maybe_sync")
    def test_list_json_uses_invokable_names_for_alias_collisions(
        self, _root_sync, _tool_sync, _mock_categories, runner
    ):
        result = runner.invoke(cli, ["--agent", "tools", "list"])

        assert result.exit_code == 0
        names = {item["name"] for item in json.loads(result.output)["data"]}
        assert names == {"get-user-trips", "list-eligible-travel-funds"}

    @pytest.mark.parametrize("mode", ["--human", "--agent"])
    def test_list_includes_tools_missing_from_token_scopes(
        self, isolated_config, monkeypatch, runner, mode
    ):
        tool = ToolDef(
            name="restricted-tool",
            description="Restricted tool",
            summary="Restricted tool",
            path="/developer/v1/agent-tools/restricted-tool",
            http_method="POST",
            category="general",
            required_scopes=["restricted:read"],
        )
        store.save_tokens("production", "tok", "refresh", granted_scopes="users:read")
        monkeypatch.setattr(
            "ramp_cli.commands.tools.list_tool_defs", lambda env: [tool]
        )
        monkeypatch.setattr("ramp_cli.commands.tools.maybe_sync", lambda env: None)
        monkeypatch.setattr("ramp_cli.main.maybe_sync", lambda env: None)

        result = runner.invoke(cli, [mode, "tools", "list"])

        assert result.exit_code == 0
        assert "restricted-tool" in result.output

    def test_list_matches_scope_resolved_root_alias(
        self, isolated_config, monkeypatch, runner
    ):
        store.save_tokens(
            "production",
            "tok",
            "refresh",
            granted_scopes="cards:read_agentic",
        )
        monkeypatch.setattr(
            "ramp_cli.commands.tools.list_tool_defs",
            lambda env: FUND_COLLIDING_TOOLS,
        )
        monkeypatch.setattr("ramp_cli.commands.tools.maybe_sync", lambda env: None)
        monkeypatch.setattr("ramp_cli.main.maybe_sync", lambda env: None)

        result = runner.invoke(cli, ["--agent", "tools", "list"])

        assert result.exit_code == 0
        entries = {
            (entry["category"], entry["name"]): entry
            for entry in json.loads(result.output)["data"]
        }
        assert entries[("funds", "list")]["description"] == "List agent card funds"
        assert entries[("funds", "get-funds")]["description"] == "List funds"
        assert not any(category == "agent_cards" for category, _ in entries)


class TestToolsListAvailability:
    """`tools list` decorates output with effective availability when known."""

    AVAILABILITY_TOOLS = [
        ToolDef(
            name="get-funds",
            description="List funds",
            summary="List funds",
            path="/developer/v1/agent-tools/get-funds",
            http_method="POST",
            category="funds",
        ),
        ToolDef(
            name="get-transactions",
            description="List transactions",
            summary="List transactions",
            path="/developer/v1/agent-tools/get-transactions",
            http_method="POST",
            category="transactions",
        ),
        ToolDef(
            name="get-bills",
            description="List bills",
            summary="List bills",
            path="/developer/v1/agent-tools/get-bills",
            http_method="POST",
            category="bills",
        ),
    ]

    @staticmethod
    def _snapshot():
        return AvailabilitySnapshot(
            content_hash="sha256:abc",
            entries={
                ("get-funds", "POST"): ToolAvailability(available=True),
                ("get-transactions", "POST"): ToolAvailability(
                    available=False,
                    unavailable_reasons=("missing_scopes",),
                    missing_scopes=("transactions:read",),
                ),
                ("get-bills", "POST"): ToolAvailability(
                    available=False,
                    unavailable_reasons=("missing_scopes", "disabled_for_business"),
                    missing_scopes=("bills:read",),
                ),
            },
        )

    def _setup(self, monkeypatch, snapshot):
        monkeypatch.setattr(
            "ramp_cli.commands.tools.list_tool_defs",
            lambda env: self.AVAILABILITY_TOOLS,
        )
        monkeypatch.setattr("ramp_cli.commands.tools.maybe_sync", lambda env: None)
        monkeypatch.setattr("ramp_cli.main.maybe_sync", lambda env: None)
        monkeypatch.setattr(
            "ramp_cli.commands.tools.fetch_availability", lambda env: snapshot
        )

    @pytest.mark.parametrize("format_args", [["--agent"], ["--output", "json"]])
    def test_json_includes_availability_fields(self, monkeypatch, runner, format_args):
        self._setup(monkeypatch, self._snapshot())

        result = runner.invoke(cli, [*format_args, "tools", "list"])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["availability"] == {"content_hash": "sha256:abc"}
        records = {record["name"]: record for record in payload["data"]}
        assert records["get-funds"]["available"] is True
        assert records["get-funds"]["unavailable_reasons"] is None
        assert records["get-transactions"]["available"] is False
        assert records["get-transactions"]["unavailable_reasons"] == ["missing_scopes"]
        assert records["get-transactions"]["missing_scopes"] == ["transactions:read"]
        assert "get-bills" not in records

    def test_json_without_availability_matches_current_shape(self, monkeypatch, runner):
        self._setup(monkeypatch, None)

        result = runner.invoke(cli, ["--agent", "tools", "list"])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert "availability" not in payload
        assert {record["name"] for record in payload["data"]} == {
            "get-funds",
            "get-transactions",
            "get-bills",
        }
        for record in payload["data"]:
            assert set(record) == {"name", "category", "description"}

    def test_human_annotates_unavailable_and_filters_business_disabled_tools(
        self, monkeypatch, runner
    ):
        self._setup(monkeypatch, self._snapshot())

        result = runner.invoke(cli, ["--human", "tools", "list"])

        assert result.exit_code == 0
        # The box formatter may wrap the annotation, so assert on whole tokens.
        assert "[unavailable:" in result.output
        assert "transactions:read]" in result.output
        assert "get-transactions" in result.output
        assert "get-bills" not in result.output
        assert "2 tools across 2 categories" in result.output


class TestToolsGroup:
    @patch("ramp_cli.main.maybe_sync")
    def test_tools_help(self, _mock_sync, runner):
        result = runner.invoke(cli, ["tools", "--help"])
        assert result.exit_code == 0
        assert "refresh" in result.output
        assert "list" in result.output
        assert "schema" in result.output


class TestToolsSchema:
    @staticmethod
    def _use_bundled_tools(monkeypatch):
        monkeypatch.setattr(
            "ramp_cli.commands.tools.list_tool_defs",
            lambda env: parse_spec(AGENT_TOOL_SPEC),
        )

    @patch("ramp_cli.main.maybe_sync")
    @patch("ramp_cli.commands.tools.maybe_sync")
    def test_prints_schema_by_category_and_alias(
        self, _tool_sync, _root_sync, isolated_config, monkeypatch, runner
    ):
        self._use_bundled_tools(monkeypatch)
        result = runner.invoke(
            cli,
            ["--agent", "tools", "schema", "applications", "edit"],
        )

        assert result.exit_code == 0
        schema = json.loads(result.output)["data"][0]
        assert "$ref" not in json.dumps(schema)
        assert "manual_bank_account" in schema["properties"]

    @patch("ramp_cli.main.maybe_sync")
    @patch("ramp_cli.commands.tools.maybe_sync")
    def test_rejects_tool_without_request_body(
        self, _tool_sync, _root_sync, isolated_config, monkeypatch, runner
    ):
        self._use_bundled_tools(monkeypatch)
        result = runner.invoke(
            cli,
            ["--human", "tools", "schema", "applications", "progress"],
        )

        assert result.exit_code == 2
        assert "does not accept a request body" in result.output

    @patch("ramp_cli.main.maybe_sync")
    @patch("ramp_cli.commands.tools.maybe_sync")
    def test_rejects_ambiguous_alias_with_invokable_choices(
        self, _tool_sync, _root_sync, monkeypatch, runner
    ):
        monkeypatch.setattr(
            "ramp_cli.commands.tools.list_tool_defs",
            lambda env: COLLIDING_TOOLS,
        )

        result = runner.invoke(
            cli,
            ["--human", "tools", "schema", "travel", "list"],
        )

        assert result.exit_code == 2
        assert "Ambiguous generated tool 'travel list'" in result.output
        assert "get-user-trips, list-eligible-travel-funds" in result.output

    @patch("ramp_cli.main.maybe_sync")
    @patch("ramp_cli.commands.tools.maybe_sync")
    def test_accepts_collision_resolved_command_name(
        self, _tool_sync, _root_sync, monkeypatch, runner
    ):
        monkeypatch.setattr(
            "ramp_cli.commands.tools.list_tool_defs",
            lambda env: COLLIDING_TOOLS,
        )
        monkeypatch.setattr(
            "ramp_cli.commands.tools.load_component_schema",
            lambda path, name: {"title": name},
        )

        result = runner.invoke(
            cli,
            [
                "--agent",
                "tools",
                "schema",
                "travel",
                "list-eligible-travel-funds",
            ],
        )

        assert result.exit_code == 0
        assert json.loads(result.output)["data"][0] == {
            "title": "ListEligibleTravelFunds"
        }

    def test_schema_matches_scope_resolved_root_alias(
        self, isolated_config, monkeypatch, runner
    ):
        store.save_tokens(
            "production",
            "tok",
            "refresh",
            granted_scopes="cards:read_agentic",
        )
        monkeypatch.setattr(
            "ramp_cli.commands.tools.list_tool_defs",
            lambda env: FUND_COLLIDING_TOOLS,
        )
        monkeypatch.setattr(
            "ramp_cli.commands.tools.load_component_schema",
            lambda path, name: {"title": name},
        )
        monkeypatch.setattr(
            "ramp_cli.commands.tools.maybe_sync", lambda env, force=False: None
        )
        monkeypatch.setattr("ramp_cli.main.maybe_sync", lambda env: None)

        result = runner.invoke(
            cli,
            ["--agent", "tools", "schema", "funds", "list"],
        )

        assert result.exit_code == 0
        assert json.loads(result.output)["data"][0] == {"title": "GetAgentCardFunds"}
