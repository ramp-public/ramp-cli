"""Tests for agent tool command generation, registry, and CLI integration."""

import json
from unittest.mock import MagicMock

import click
from click.testing import CliRunner

from ramp_cli.main import ToolGroup, cli
from ramp_cli.specs import AGENT_TOOL_SPEC
from ramp_cli.tools.commands import _build_body, build_tool_command
from ramp_cli.tools.parser import JsonSchema, ParamType, ToolDef, ToolParam
from ramp_cli.tools.registry import _registry, get_tool, list_tool_defs, list_tools
from ramp_cli.tools.registry import reload as reload_tools


def _use_bundled_spec(monkeypatch):
    monkeypatch.setattr("ramp_cli.main.maybe_sync", lambda env: None)
    monkeypatch.setattr(
        "ramp_cli.tools.registry.local_agent_tool_spec", lambda env: AGENT_TOOL_SPEC
    )
    reload_tools("production")


class TestRegistry:
    def test_list_tools_returns_names(self):
        names = list_tools()
        assert len(names) >= 40
        assert "get-funds" in names
        assert "activate-card" in names
        assert "list-bills" in names

    def test_get_tool_found(self):
        tool = get_tool("get-funds")
        assert tool is not None
        assert tool.name == "get-funds"

    def test_get_tool_not_found(self):
        assert get_tool("nonexistent-tool") is None

    def test_env_switch_reloads_spec(self, tmp_path, monkeypatch):
        """Registry auto-reloads when a different env is requested."""

        # Create two minimal specs with different tool sets.
        # The parser requires a $ref to components/schemas.
        def _make_spec(tool_name):
            schema_name = f"{tool_name.title().replace('-', '')}Request"
            return {
                "paths": {
                    f"/developer/v1/agent-tools/{tool_name}": {
                        "post": {
                            "operationId": tool_name,
                            "summary": f"{tool_name} tool",
                            "description": f"Tool {tool_name}",
                            "x-tool-category": "test",
                            "security": [{"oauth2": ["test:read"]}],
                            "requestBody": {
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": f"#/components/schemas/{schema_name}"
                                        }
                                    }
                                }
                            },
                        }
                    }
                },
                "components": {
                    "schemas": {schema_name: {"type": "object", "properties": {}}}
                },
            }

        prod_spec = _make_spec("prod-only")
        sandbox_spec = _make_spec("sandbox-only")

        prod_file = tmp_path / "agent-tool-production.json"
        sandbox_file = tmp_path / "agent-tool-sandbox.json"
        prod_file.write_text(json.dumps(prod_spec))
        sandbox_file.write_text(json.dumps(sandbox_spec))

        monkeypatch.setattr(
            "ramp_cli.tools.registry.local_agent_tool_spec",
            lambda env: tmp_path / f"agent-tool-{env}.json",
        )

        # Force a fresh load
        _registry._tools = None
        _registry._loaded_env = None

        try:
            prod_names = list_tools(env="production")
            assert "prod-only" in prod_names
            assert "sandbox-only" not in prod_names

            sandbox_names = list_tools(env="sandbox")
            assert "sandbox-only" in sandbox_names
            assert "prod-only" not in sandbox_names

            # Switching back works
            prod_names_2 = list_tools(env="production")
            assert "prod-only" in prod_names_2
        finally:
            # Reset registry to bundled spec for other tests
            _registry._tools = None
            _registry._loaded_env = None


class TestBuildToolCommand:
    def _simple_tool(self) -> ToolDef:
        return ToolDef(
            name="test-tool",
            path="/developer/v1/agent-tools/test-tool",
            http_method="post",
            summary="A test tool",
            description="A test tool for testing",
            params=[
                ToolParam(
                    name="name",
                    flag="name",
                    description="A name",
                    type=ParamType.STRING,
                    required=True,
                ),
                ToolParam(
                    name="count",
                    flag="count",
                    description="A count",
                    type=ParamType.INT,
                ),
                ToolParam(
                    name="verbose",
                    flag="verbose",
                    description="Be verbose",
                    type=ParamType.BOOL,
                    default=False,
                ),
                ToolParam(
                    name="status",
                    flag="status",
                    description="Status filter",
                    type=ParamType.ENUM,
                    enum_values=["active", "inactive"],
                ),
            ],
            required_scopes=["test:read"],
        )

    def test_generates_click_command(self):
        cmd = build_tool_command(self._simple_tool())
        assert isinstance(cmd, click.Command)
        assert cmd.name == "test-tool"

    def test_has_help_text(self):
        cmd = build_tool_command(self._simple_tool())
        assert "A test tool" in cmd.help

    def test_has_json_and_dry_run_options(self):
        cmd = build_tool_command(self._simple_tool())
        option_names = {p.name for p in cmd.params if isinstance(p, click.Option)}
        assert "json_body" in option_names
        assert "dry_run" in option_names

    def test_has_param_options(self):
        cmd = build_tool_command(self._simple_tool())
        option_names = {p.name for p in cmd.params if isinstance(p, click.Option)}
        assert "name" in option_names
        assert "count" in option_names
        assert "verbose" in option_names
        assert "status" in option_names

    def test_enum_param_values_in_help(self):
        cmd = build_tool_command(self._simple_tool())
        status_opt = next(p for p in cmd.params if p.name == "status")
        # Enum values are listed in help text, not as click.Choice metavar
        assert "active" in status_opt.help
        assert "inactive" in status_opt.help
        assert "values:" in status_opt.help

    def test_complex_params_excluded_from_flags(self):
        tool = ToolDef(
            name="complex-tool",
            path="/developer/v1/agent-tools/complex-tool",
            http_method="post",
            summary="Complex",
            description="Complex tool",
            params=[
                ToolParam(
                    name="filters",
                    flag="filters",
                    description="Filters",
                    type=ParamType.OBJECT,
                    is_complex=True,
                ),
                ToolParam(
                    name="query",
                    flag="query",
                    description="Query text",
                    type=ParamType.STRING,
                ),
            ],
        )
        cmd = build_tool_command(tool)
        option_names = {p.name for p in cmd.params if isinstance(p, click.Option)}
        assert "filters" not in option_names
        assert "query" in option_names
        assert "Complex" in cmd.help
        assert "--json" in cmd.help


class TestPositionalIdParams:
    """ID params (*_id, *_uuid, bare id) should be positional arguments."""

    def _tool_with_id(self, id_name: str = "bill_id", **extra) -> ToolDef:
        return ToolDef(
            name="get-bill-details",
            path="/developer/v1/agent-tools/get-bill-details",
            http_method="post",
            summary="Get bill",
            description="Get bill details",
            category="bills",
            alias="get",
            params=[
                ToolParam(
                    name=id_name,
                    flag=id_name.replace("_", "-") if "-" in id_name else id_name,
                    description="The bill ID",
                    type=ParamType.STRING,
                    required=True,
                    **extra,
                ),
                ToolParam(
                    name="verbose",
                    flag="verbose",
                    description="Be verbose",
                    type=ParamType.BOOL,
                    default=False,
                ),
            ],
        )

    def test_id_param_is_positional(self):
        cmd = build_tool_command(self._tool_with_id("bill_id"))
        args = [p for p in cmd.params if isinstance(p, click.Argument)]
        assert len(args) == 1
        assert args[0].name == "bill_id"

    def test_uuid_param_is_positional(self):
        cmd = build_tool_command(self._tool_with_id("transaction_uuid"))
        args = [p for p in cmd.params if isinstance(p, click.Argument)]
        assert len(args) == 1
        assert args[0].name == "transaction_uuid"

    def test_bare_id_is_positional(self):
        cmd = build_tool_command(self._tool_with_id("id"))
        args = [p for p in cmd.params if isinstance(p, click.Argument)]
        assert len(args) == 1
        assert args[0].name == "id"

    def test_non_id_param_stays_option(self):
        cmd = build_tool_command(self._tool_with_id("bill_id"))
        option_names = {p.name for p in cmd.params if isinstance(p, click.Option)}
        assert "verbose" in option_names
        assert "bill_id" not in option_names

    def test_optional_id_stays_option(self):
        """Optional ID params should remain options, not positional."""
        tool = ToolDef(
            name="get-funds",
            path="/developer/v1/agent-tools/get-funds",
            http_method="post",
            summary="Get funds",
            description="Get funds",
            params=[
                ToolParam(
                    name="for_transaction_id",
                    flag="for_transaction_id",
                    description="Filter by transaction",
                    type=ParamType.STRING,
                    required=False,
                ),
            ],
        )
        cmd = build_tool_command(tool)
        args = [p for p in cmd.params if isinstance(p, click.Argument)]
        assert len(args) == 0

    def test_multiple_ids_all_positional(self):
        tool = ToolDef(
            name="attach-receipt",
            path="/developer/v1/agent-tools/attach-receipt-to-transaction",
            http_method="post",
            summary="Attach receipt",
            description="Attach receipt",
            params=[
                ToolParam(
                    name="receipt_uuid",
                    flag="receipt_uuid",
                    description="Receipt UUID",
                    type=ParamType.STRING,
                    required=True,
                ),
                ToolParam(
                    name="transaction_uuid",
                    flag="transaction_uuid",
                    description="Transaction UUID",
                    type=ParamType.STRING,
                    required=True,
                ),
            ],
        )
        cmd = build_tool_command(tool)
        args = [p for p in cmd.params if isinstance(p, click.Argument)]
        assert len(args) == 2
        assert args[0].name == "receipt_uuid"
        assert args[1].name == "transaction_uuid"

    def test_positional_id_in_dry_run(self):
        """Positional ID value should appear in the dry-run body."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "get-bill-details",
                "abc-123",
                "--dry_run",
                "--rationale",
                "Testing dry-run body",
            ],
        )
        assert result.exit_code == 0
        assert "dry_run" in result.output
        body = json.loads(result.output)["data"][0]["body"]
        assert body["bill_id"] == "abc-123"

    def test_missing_positional_id_errors(self):
        """Omitting a required positional ID should produce a clear error."""
        runner = CliRunner()
        result = runner.invoke(cli, ["get-bill-details", "--dry_run"])
        assert result.exit_code != 0

    def test_real_tool_bill_id_positional(self):
        """get-bill-details from the bundled spec should have bill_id as positional."""
        tool = get_tool("get-bill-details")
        assert tool is not None
        cmd = build_tool_command(tool)
        args = [p for p in cmd.params if isinstance(p, click.Argument)]
        arg_names = [a.name for a in args]
        assert "bill_id" in arg_names

    def test_real_tool_lock_card_id_positional(self):
        """lock-or-unlock-card should have id as positional."""
        tool = get_tool("lock-or-unlock-card")
        assert tool is not None
        cmd = build_tool_command(tool)
        args = [p for p in cmd.params if isinstance(p, click.Argument)]
        arg_names = [a.name for a in args]
        assert "id" in arg_names

    def test_json_bypasses_positional_id(self):
        """--json should work without providing positional ID args."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["get-bill-details", "--json", '{"bill_id": "abc-123"}', "--dry_run"],
        )
        assert result.exit_code == 0
        assert "dry_run" in result.output
        body = json.loads(result.output)["data"][0]["body"]
        assert body["bill_id"] == "abc-123"

    def test_json_with_positional_id_also_works(self):
        """--json with a positional ID should use the JSON body, not the arg."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "get-bill-details",
                "ignored-id",
                "--json",
                '{"bill_id": "from-json"}',
                "--dry_run",
            ],
        )
        assert result.exit_code == 0
        body = json.loads(result.output)["data"][0]["body"]
        assert body["bill_id"] == "from-json"


class TestDisplayName:
    def test_display_name_with_category_and_alias(self):
        tool = ToolDef(
            name="get-funds",
            path="/test",
            http_method="post",
            summary="Get funds",
            description="",
            category="funds",
            alias="list",
        )
        assert tool.display_name == "funds list"

    def test_display_name_alias_only(self):
        tool = ToolDef(
            name="get-funds",
            path="/test",
            http_method="post",
            summary="Get funds",
            description="",
            alias="list",
        )
        assert tool.display_name == "list"

    def test_display_name_no_alias(self):
        tool = ToolDef(
            name="get-funds",
            path="/test",
            http_method="post",
            summary="Get funds",
            description="",
            category="funds",
        )
        assert tool.display_name == "get-funds"

    def test_display_name_bare(self):
        tool = ToolDef(
            name="get-funds",
            path="/test",
            http_method="post",
            summary="Get funds",
            description="",
        )
        assert tool.display_name == "get-funds"


class TestErrorExampleFormat:
    def test_error_shows_positional_before_options(self):
        """Error example should show positional args before options."""
        runner = CliRunner()
        # Provide the ID so Click doesn't intercept the missing arg error.
        # The missing --action triggers our custom error with the example.
        result = runner.invoke(
            cli,
            ["lock-or-unlock-card", "abc-123"],
        )
        assert result.exit_code != 0
        # Extract the Example line and verify ordering within it
        example_line = [
            line
            for line in result.output.splitlines()
            if line.strip().startswith("Example:")
        ]
        assert len(example_line) == 1
        example = example_line[0]
        assert "<id>" in example
        assert "--action" in example
        id_pos = example.index("<id>")
        action_pos = example.index("--action")
        assert id_pos < action_pos, "Positional <id> should come before --action"


class TestAliasInCategoryGroup:
    def test_category_uses_alias(self):
        """Category subcommands use alias when present."""
        tool_with_alias = ToolDef(
            name="get-funds",
            path="/developer/v1/agent-tools/get-funds",
            http_method="post",
            summary="Get funds",
            description="Get funds",
            category="funds",
            alias="list",
            params=[],
        )
        tool_without_alias = ToolDef(
            name="create-fund",
            path="/developer/v1/agent-tools/create-fund",
            http_method="post",
            summary="Create fund",
            description="Create fund",
            category="funds",
            params=[],
        )

        group = ToolGroup.build(
            "funds", [tool_with_alias, tool_without_alias], "Funds (2 tools)"
        )
        cmd_names = list(group.list_commands(click.Context(group)))
        assert "list" in cmd_names, f"Expected 'list' in {cmd_names}"
        assert "create-fund" in cmd_names, f"Expected 'create-fund' in {cmd_names}"

        hidden = group.get_command(click.Context(group), "get-funds")
        assert hidden is not None
        assert hidden.hidden is True
        help_text = group.get_help(click.Context(group))
        assert "get-funds" not in help_text

    def test_legacy_name_does_not_replace_visible_command(self):
        legacy_source = ToolDef(
            name="get-funds",
            path="/developer/v1/agent-tools/get-funds",
            http_method="post",
            summary="Get funds",
            description="Get funds",
            category="funds",
            alias="list",
            params=[],
        )
        visible_tool = ToolDef(
            name="other-funds-tool",
            path="/developer/v1/agent-tools/other-funds-tool",
            http_method="post",
            summary="Visible compatibility command",
            description="Visible compatibility command",
            category="funds",
            alias="get-funds",
            params=[],
        )

        group = ToolGroup.build("funds", [legacy_source, visible_tool], "Funds")
        cmd = group.get_command(click.Context(group), "get-funds")
        assert cmd is not None
        assert cmd.hidden is False
        assert cmd.help == "Visible compatibility command"

    def test_alias_collision_fallback_does_not_add_duplicate_hidden_command(self):
        remapped_tool = ToolDef(
            name="get-agent-card-funds",
            path="/developer/v1/agent-tools/get-agent-card-funds",
            http_method="post",
            summary="Get agent card funds",
            description="Get agent card funds",
            category="agent_cards",
            alias="list",
            params=[],
        )
        native_tool = ToolDef(
            name="get-funds",
            path="/developer/v1/agent-tools/get-funds",
            http_method="post",
            summary="Get funds",
            description="Get funds",
            category="funds",
            alias="list",
            params=[],
        )

        group = ToolGroup.build("funds", [remapped_tool, native_tool], "Funds")
        cmd_names = list(group.list_commands(click.Context(group)))
        assert "list" in cmd_names
        assert "get-agent-card-funds" in cmd_names

        hidden = group.get_command(click.Context(group), "get-funds")
        assert hidden is not None
        assert hidden.hidden is True
        help_text = group.get_help(click.Context(group))
        assert "get-funds" not in help_text


class TestDryRun:
    """Dry run never hits the network — no mocking needed."""

    def test_dry_run_prints_body(self):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "get-funds",
                "--dry_run",
                "--funds_to_retrieve",
                "MY_FUNDS",
                "--rationale",
                "Testing dry-run body",
            ],
        )
        assert result.exit_code == 0
        assert "dry_run" in result.output
        body = json.loads(result.output)["data"][0]["body"]
        assert body["funds_to_retrieve"] == "MY_FUNDS"

    def test_dry_run_with_json(self):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["get-funds", "--dry_run", "--json", '{"funds_to_retrieve": "ALL_FUNDS"}'],
        )
        assert result.exit_code == 0
        assert "ALL_FUNDS" in result.output


class TestBodyBuilding:
    def test_required_param_default_is_used_when_omitted(self):
        tool = ToolDef(
            name="list-bills",
            path="/developer/v1/agent-tools/search-bills",
            http_method="post",
            summary="List bills",
            description="List bills",
            category="bills",
            alias="list",
            params=[
                ToolParam(
                    name="query",
                    flag="query",
                    description="Optional bill search query",
                    type=ParamType.STRING,
                    required=True,
                    default="",
                )
            ],
        )

        assert _build_body(tool, {"query": None}) == {"query": ""}

    def test_bool_flag_included_when_set(self):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "get-funds",
                "--dry_run",
                "--include_balance",
                "--rationale",
                "Testing dry-run body",
            ],
        )
        assert result.exit_code == 0
        body = json.loads(result.output)["data"][0]["body"]
        assert body["include_balance"] is True

    def test_bool_flag_included_when_explicitly_false(self):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "get-funds",
                "--dry_run",
                "--no-include_balance",
                "--rationale",
                "Testing dry-run body",
            ],
        )
        assert result.exit_code == 0
        body = json.loads(result.output)["data"][0]["body"]
        assert body["include_balance"] is False

    def test_bool_flag_excluded_when_not_set(self):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["get-funds", "--dry_run", "--rationale", "Testing dry-run body"],
        )
        assert result.exit_code == 0
        body = json.loads(result.output)["data"][0]["body"]
        assert "include_balance" not in body

    def test_required_param_missing_errors(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["activate-card", "--dry_run"])
        assert result.exit_code != 0
        assert "last_four" in result.output

    def test_bills_list_dry_run_works(self):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["bills", "list", "--dry_run", "--rationale", "Testing dry-run body"],
        )
        assert result.exit_code == 0
        body = json.loads(result.output)["data"][0]["body"]
        # The real list-bills endpoint has query as optional;
        # when omitted, no query key should be in the body.
        assert "query" not in body or body["query"] == ""


class TestCLIIntegration:
    def test_categories_show_in_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert "transactions" in result.output
        assert "funds" in result.output
        assert "bills" in result.output
        # cards and agent_cards should be remapped into funds
        assert "agent_cards" not in result.output

    def test_tasks_singleton_stays_resource(self, monkeypatch):
        task_tool = ToolDef(
            name="get-attention-feed",
            path="/developer/v1/agent-tools/get-attention-feed",
            http_method="post",
            summary="Get attention feed",
            description="Return the authenticated user's homepage attention feed",
            category="tasks",
            alias="list",
            params=[],
        )

        monkeypatch.setattr("ramp_cli.main.maybe_sync", lambda env: None)
        monkeypatch.setattr(
            "ramp_cli.main.list_categories",
            lambda env: {"tasks": [task_tool]},
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "tasks" in result.output
        assert "Review tasks" in result.output

        result = runner.invoke(cli, ["tasks", "--help"])

        assert result.exit_code == 0
        assert "list" in result.output

    def test_funds_group_contains_card_tools(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["funds", "--help"])
        assert result.exit_code == 0
        assert "activate-card" in result.output or "activate" in result.output
        assert "get-funds" in result.output or "list" in result.output

    def test_cards_and_agent_cards_groups_gone(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        # "cards" and "agent_cards" should not appear as standalone resource groups
        # (they're merged into "funds")
        lines = result.output.splitlines()
        resource_names = [line.strip().split()[0] for line in lines if line.strip()]
        assert "cards" not in resource_names
        assert "agent_cards" not in resource_names

    def test_descriptive_help_text_present(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        # Check that descriptive help text is used instead of generic "N tools"
        assert "Manage funds" in result.output or "funds" in result.output
        assert "Search, review" in result.output or "transactions" in result.output

    def test_existing_commands_still_work(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["auth", "--help"])
        assert result.exit_code == 0
        assert "login" in result.output

    def test_category_shows_tools(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["transactions", "--help"])
        assert result.exit_code == 0
        # Category subcommands use aliases
        assert "list" in result.output or "get-recent-transactions" in result.output

    def test_requests_group_uses_spec_search_alias(self, monkeypatch):
        _use_bundled_spec(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(cli, ["requests", "--help"])
        assert result.exit_code == 0
        assert "search" in result.output
        assert "search-unified-requests" not in result.output

        result = runner.invoke(
            cli,
            [
                "requests",
                "search",
                "--dry_run",
                "--json",
                '{"filters":{"search":"Paola"},"limit":10}',
            ],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["data"][0]["url"].endswith(
            "/developer/v1/agent-tools/search-unified-requests"
        )

        result = runner.invoke(
            cli,
            [
                "requests",
                "search-unified-requests",
                "--dry_run",
                "--json",
                '{"filters":{"search":"Paola"},"limit":10}',
            ],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["data"][0]["url"].endswith(
            "/developer/v1/agent-tools/search-unified-requests"
        )

    def test_flat_tool_access_still_works(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["get-funds", "--help"])
        assert result.exit_code == 0
        assert "funds_to_retrieve" in result.output

    def test_invalid_json_errors(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["get-funds", "--json", "not-json"])
        assert result.exit_code != 0
        assert "invalid JSON" in result.output

    def test_json_allows_nullable_enum_value(self):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "funds",
                "list",
                "--dry_run",
                "--json",
                '{"funds_to_retrieve": null}',
            ],
        )
        assert result.exit_code == 0
        body = json.loads(result.output)["data"][0]["body"]
        assert body["funds_to_retrieve"] is None

    def test_json_allows_unknown_top_level_field(self, monkeypatch):
        _use_bundled_spec(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "requests",
                "search",
                "--dry_run",
                "--json",
                '{"query":"Paola"}',
            ],
        )
        assert result.exit_code == 0
        body = json.loads(result.output)["data"][0]["body"]
        assert body["query"] == "Paola"

    def test_json_allows_unknown_nested_filter_field(self, monkeypatch):
        _use_bundled_spec(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "requests",
                "search",
                "--dry_run",
                "--json",
                '{"filters":{"foo":"bar"}}',
            ],
        )
        assert result.exit_code == 0
        body = json.loads(result.output)["data"][0]["body"]
        assert body["filters"]["foo"] == "bar"

    def test_json_rejects_unknown_field_for_closed_schema(self):
        tool = ToolDef(
            name="closed-schema-test",
            path="/developer/v1/agent-tools/closed-schema-test",
            http_method="post",
            summary="Closed schema test",
            description="Closed schema test",
            json_schema=JsonSchema(
                properties={"known": JsonSchema()},
                additional_properties_allowed=False,
            ),
        )
        cmd = build_tool_command(tool)
        group = click.Group("test")
        group.add_command(cmd, "closed-schema-test")

        runner = CliRunner()
        result = runner.invoke(
            group,
            ["closed-schema-test", "--dry_run", "--json", '{"unknown":"value"}'],
            obj={
                "env": "sandbox",
                "format": "json",
                "config_format": "json",
                "quiet": False,
                "no_input": True,
                "wide": False,
                "agent_mode": True,
            },
        )

        assert result.exit_code == 2
        assert "unknown JSON field 'unknown'" in result.output

    def test_json_rejects_invalid_enum_value(self, monkeypatch):
        _use_bundled_spec(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "requests",
                "search",
                "--dry_run",
                "--json",
                '{"filters":{"request_statuses":["SUBMITTED"]}}',
            ],
        )
        assert result.exit_code == 2
        assert "invalid enum value 'SUBMITTED'" in result.output
        assert "filters.request_statuses[0]" in result.output
        assert "PENDING" in result.output

    def test_json_rejects_non_string_enum_value(self, monkeypatch):
        _use_bundled_spec(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "requests",
                "search",
                "--dry_run",
                "--json",
                '{"filters":{"request_statuses":[1]}}',
            ],
        )
        assert result.exit_code == 2
        assert "invalid enum value '1'" in result.output
        assert "filters.request_statuses[0]" in result.output

    def test_json_validates_purchase_order_status_enum_separately(self):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "purchase_orders",
                "search",
                "--dry_run",
                "--json",
                '{"filters":{"spend_request_statuses":["PENDING"]}}',
            ],
        )
        assert result.exit_code == 2
        assert "invalid enum value 'PENDING'" in result.output
        assert "SUBMITTED" in result.output

        result = runner.invoke(
            cli,
            [
                "purchase_orders",
                "search",
                "--dry_run",
                "--json",
                '{"filters":{"spend_request_statuses":["SUBMITTED"]}}',
            ],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["data"][0]["body"]["filters"]["spend_request_statuses"] == [
            "SUBMITTED"
        ]

    def test_json_body_must_be_object(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["get-funds", "--dry_run", "--json", "[]"])
        assert result.exit_code == 2
        assert "JSON body must be an object" in result.output

    def test_json_validation_uses_synced_tool_schema(self, monkeypatch):
        stale_tool = ToolDef(
            name="schema-test",
            path="/developer/v1/agent-tools/schema-test",
            http_method="post",
            summary="Schema test",
            description="Schema test",
            json_schema=JsonSchema(
                properties={"status": JsonSchema(enum_values=["old"])}
            ),
        )
        refreshed_tool = ToolDef(
            name="schema-test",
            path="/developer/v1/agent-tools/schema-test",
            http_method="post",
            summary="Schema test",
            description="Schema test",
            json_schema=JsonSchema(
                properties={"status": JsonSchema(enum_values=["new"])}
            ),
        )
        mock_sync = MagicMock()
        mock_client = MagicMock()
        mock_client.post.return_value = b'{"ok": true}'
        monkeypatch.setattr("ramp_cli.tools.commands.maybe_sync", mock_sync)
        monkeypatch.setattr(
            "ramp_cli.tools.commands.RampClient", lambda env: mock_client
        )
        monkeypatch.setattr(
            "ramp_cli.tools.commands.get_tool",
            lambda name, env: refreshed_tool if name == stale_tool.name else None,
        )

        cmd = build_tool_command(stale_tool)
        group = click.Group("test")
        group.add_command(cmd, "schema-test")

        runner = CliRunner()
        result = runner.invoke(
            group,
            ["schema-test", "--json", '{"status":"new"}'],
            obj={
                "env": "sandbox",
                "format": "json",
                "config_format": "json",
                "quiet": False,
                "no_input": True,
                "wide": False,
                "agent_mode": True,
            },
        )

        assert result.exit_code == 0
        mock_sync.assert_called_once_with("sandbox", force=True)
        body = json.loads(mock_client.post.call_args.args[1])
        assert body == {"status": "new"}

    def test_json_dry_run_does_not_sync_tool_schema(self, monkeypatch):
        mock_sync = MagicMock()
        monkeypatch.setattr("ramp_cli.tools.commands.maybe_sync", mock_sync)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["get-funds", "--dry_run", "--json", '{"funds_to_retrieve":"ALL_FUNDS"}'],
        )

        assert result.exit_code == 0
        mock_sync.assert_not_called()


class TestPaginationFlagAliases:
    """--limit and --page_size should be bidirectional aliases.

    Some tools use ``page_size``, others use ``limit``.  Both names should be
    accepted on every paginated command so agents don't have to discover which
    one a particular endpoint chose.
    """

    def _tool_with_page_size(self) -> ToolDef:
        return ToolDef(
            name="get-things",
            path="/developer/v1/agent-tools/get-things",
            http_method="post",
            summary="Get things",
            description="Get things",
            category="things",
            alias="list",
            params=[
                ToolParam(
                    name="page_size",
                    flag="page_size",
                    description="Max results per page",
                    type=ParamType.INT,
                    required=False,
                ),
                ToolParam(
                    name="name",
                    flag="name",
                    description="A name",
                    type=ParamType.STRING,
                    required=False,
                ),
            ],
        )

    def _tool_with_limit(self) -> ToolDef:
        return ToolDef(
            name="search-things",
            path="/developer/v1/agent-tools/search-things",
            http_method="post",
            summary="Search things",
            description="Search things",
            category="things",
            alias="search",
            params=[
                ToolParam(
                    name="limit",
                    flag="limit",
                    description="Max results",
                    type=ParamType.INT,
                    required=False,
                ),
                ToolParam(
                    name="query",
                    flag="query",
                    description="Search query",
                    type=ParamType.STRING,
                    required=False,
                ),
            ],
        )

    # --- page_size → --limit direction ---

    def test_limit_alias_present_on_page_size(self):
        cmd = build_tool_command(self._tool_with_page_size())
        page_opt = next(p for p in cmd.params if p.name == "page_size")
        assert any("--limit" in d for d in page_opt.opts)

    def test_page_size_body_via_limit(self):
        """--limit on a page_size tool maps to page_size in the body."""
        tool = self._tool_with_page_size()
        body = _build_body(tool, {"page_size": 5, "name": None})
        assert body == {"page_size": 5}

    # --- limit → --page_size direction ---

    def test_page_size_alias_present_on_limit(self):
        cmd = build_tool_command(self._tool_with_limit())
        limit_opt = next(p for p in cmd.params if p.name == "limit")
        assert any("--page_size" in d for d in limit_opt.opts)

    def test_limit_body_via_page_size(self):
        """--page_size on a native limit tool maps to limit in the body."""
        tool = self._tool_with_limit()
        body = _build_body(tool, {"limit": 5, "query": None})
        assert body == {"limit": 5}

    # --- no spurious aliases ---

    def test_non_pagination_param_has_no_alias(self):
        cmd = build_tool_command(self._tool_with_page_size())
        name_opt = next(p for p in cmd.params if p.name == "name")
        assert "--limit" not in name_opt.opts
        assert "--page_size" not in name_opt.opts

    # --- real spec integration ---

    def test_real_transactions_list_has_limit_alias(self):
        """get-transactions (page_size) should accept --limit."""
        tool = get_tool("get-transactions")
        assert tool is not None
        cmd = build_tool_command(tool)
        page_opt = next((p for p in cmd.params if p.name == "page_size"), None)
        assert page_opt is not None
        assert any("--limit" in d for d in page_opt.opts)

    def test_real_search_bills_has_page_size_alias(self):
        """search-bills (limit) should accept --page_size."""
        tool = get_tool("search-bills")
        assert tool is not None
        cmd = build_tool_command(tool)
        limit_opt = next((p for p in cmd.params if p.name == "limit"), None)
        assert limit_opt is not None
        assert any("--page_size" in d for d in limit_opt.opts)

    # --- end-to-end CLI dry-run ---

    def test_limit_dry_run_via_cli(self):
        """--limit works end-to-end on a page_size tool."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "get-transactions",
                "--limit",
                "5",
                "--dry_run",
                "--rationale",
                "test",
                "--transactions_to_retrieve",
                "MY_TRANSACTIONS",
            ],
        )
        assert result.exit_code == 0
        body = json.loads(result.output)["data"][0]["body"]
        assert body["page_size"] == 5

    def test_page_size_dry_run_via_cli(self):
        """--page_size still works end-to-end on a page_size tool."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "get-transactions",
                "--page_size",
                "10",
                "--dry_run",
                "--rationale",
                "test",
                "--transactions_to_retrieve",
                "MY_TRANSACTIONS",
            ],
        )
        assert result.exit_code == 0
        body = json.loads(result.output)["data"][0]["body"]
        assert body["page_size"] == 10

    def test_page_size_alias_dry_run_on_limit_tool(self):
        """--page_size works end-to-end on a native limit tool (bills search)."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search-bills",
                "--page_size",
                "5",
                "--dry_run",
                "--rationale",
                "test",
                "--query",
                "test",
            ],
        )
        assert result.exit_code == 0
        body = json.loads(result.output)["data"][0]["body"]
        assert body["limit"] == 5

    # --- exhaustive spec coverage ---

    def test_all_page_size_tools_have_limit_alias(self):
        """Every tool with page_size in the bundled spec should have --limit."""
        tools_with_page_size = [
            t for t in list_tool_defs() if any(p.name == "page_size" for p in t.params)
        ]
        assert len(tools_with_page_size) > 0
        for tool in tools_with_page_size:
            cmd = build_tool_command(tool)
            page_opt = next((p for p in cmd.params if p.name == "page_size"), None)
            assert page_opt is not None, f"{tool.name} missing page_size option"
            assert any("--limit" in d for d in page_opt.opts), (
                f"{tool.name}: --limit alias missing on page_size"
            )

    def test_all_limit_tools_have_page_size_alias(self):
        """Every tool with limit in the bundled spec should have --page_size."""
        tools_with_limit = [
            t
            for t in list_tool_defs()
            if any(p.name == "limit" for p in t.params)
            and not any(p.name == "page_size" for p in t.params)
        ]
        assert len(tools_with_limit) > 0
        for tool in tools_with_limit:
            cmd = build_tool_command(tool)
            limit_opt = next((p for p in cmd.params if p.name == "limit"), None)
            assert limit_opt is not None, f"{tool.name} missing limit option"
            assert any("--page_size" in d for d in limit_opt.opts), (
                f"{tool.name}: --page_size alias missing on limit"
            )


class TestGetEndpointDryRun:
    def test_dry_run_shows_get_method(self):
        tool = ToolDef(
            name="get-status",
            path="/developer/v1/agent-tools/get-status",
            http_method="get",
            summary="Get status",
            description="Get status",
            category="vendors",
            alias="status",
            params=[
                ToolParam(
                    name="batch_id",
                    flag="batch_id",
                    description="The batch ID",
                    type=ParamType.STRING,
                    required=True,
                ),
            ],
            required_scopes=["vendors:read"],
        )
        cmd = build_tool_command(tool)
        group = click.Group("test")
        group.add_command(cmd, "get-status")

        runner = CliRunner()
        result = runner.invoke(
            group,
            ["get-status", "--dry_run", "abc-123"],
            obj={
                "env": "sandbox",
                "format": "json",
                "config_format": "json",
                "quiet": False,
                "no_input": True,
                "wide": False,
                "agent_mode": True,
            },
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"][0]["method"] == "GET"
        assert parsed["data"][0]["body"] == {"batch_id": "abc-123"}
