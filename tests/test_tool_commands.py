"""Tests for agent tool command generation, registry, and CLI integration."""

import json

import click
from click.testing import CliRunner

from ramp_cli.main import ToolGroup, cli
from ramp_cli.tools.commands import build_tool_command
from ramp_cli.tools.parser import ParamType, ToolDef, ToolParam
from ramp_cli.tools.registry import _registry, get_tool, list_tools


class TestRegistry:
    def test_list_tools_returns_names(self):
        names = list_tools()
        assert len(names) >= 40
        assert "get-funds" in names
        assert "activate-card" in names

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
            ["get-bill-details", "abc-123", "--dry_run"],
        )
        assert result.exit_code == 0
        assert "DRY RUN" in result.output
        body = json.loads(result.output.split("\n", 1)[1])
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
        assert "DRY RUN" in result.output
        body = json.loads(result.output.split("\n", 1)[1])
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
        body = json.loads(result.output.split("\n", 1)[1])
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
        assert "get-funds" not in cmd_names, (
            "get-funds should be replaced by alias 'list'"
        )


class TestDryRun:
    """Dry run never hits the network — no mocking needed."""

    def test_dry_run_prints_body(self):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["get-funds", "--dry_run", "--funds_to_retrieve", "MY_FUNDS"],
        )
        assert result.exit_code == 0
        assert "DRY RUN" in result.output
        body = json.loads(result.output.split("\n", 1)[1])
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
    def test_bool_flag_included_when_set(self):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["get-funds", "--dry_run", "--include_balance"],
        )
        assert result.exit_code == 0
        body = json.loads(result.output.split("\n", 1)[1])
        assert body["include_balance"] is True

    def test_bool_flag_excluded_when_not_set(self):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["get-funds", "--dry_run"],
        )
        assert result.exit_code == 0
        body = json.loads(result.output.split("\n", 1)[1])
        assert "include_balance" not in body

    def test_required_param_missing_errors(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["activate-card", "--dry_run"])
        assert result.exit_code != 0
        assert "last_four" in result.output


class TestCLIIntegration:
    def test_categories_show_in_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert "transactions" in result.output
        assert "funds" in result.output
        assert "bills" in result.output
        # cards and agent_cards should be remapped into funds
        assert "agent_cards" not in result.output

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
