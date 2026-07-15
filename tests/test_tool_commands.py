"""Tests for agent tool command generation, registry, and CLI integration."""

import json
from unittest.mock import MagicMock

import click
import pytest
from click.testing import CliRunner

from ramp_cli.auth import store
from ramp_cli.main import ToolGroup, cli
from ramp_cli.specs import AGENT_TOOL_SPEC
from ramp_cli.tools.commands import (
    _build_body,
    build_tool_command,
)
from ramp_cli.tools.parser import (
    JsonSchema,
    ParamType,
    ToolDef,
    ToolParam,
    parse_spec_dict,
)
from ramp_cli.tools.registry import _registry, get_tool, list_tool_defs, list_tools
from ramp_cli.tools.registry import reload as reload_tools


def _use_bundled_spec(monkeypatch):
    monkeypatch.setattr("ramp_cli.main.maybe_sync", lambda env: None)
    monkeypatch.setattr(
        "ramp_cli.tools.registry._resolve_generated_spec_path",
        lambda definition, env: AGENT_TOOL_SPEC,
    )
    reload_tools("production")


def _ctx_obj() -> dict:
    return {
        "env": "sandbox",
        "format": "json",
        "config_format": "json",
        "quiet": False,
        "no_input": True,
        "wide": False,
        "agent_mode": True,
    }


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

    def test_shared_procurement_path_operations_have_unique_registry_entries(
        self, monkeypatch
    ):
        _use_bundled_spec(monkeypatch)

        operations = {
            name: get_tool(name, env="production")
            for name in (
                "delete-procurement-draft",
                "get-procurement-draft",
                "post-procurement-draft",
            )
        }

        assert {name: tool.http_method for name, tool in operations.items()} == {
            "delete-procurement-draft": "delete",
            "get-procurement-draft": "get",
            "post-procurement-draft": "post",
        }

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

        monkeypatch.setattr("ramp_cli.specs.config_dir", lambda: tmp_path)

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

    def test_parameterless_tool_omits_json_option(self):
        tool = ToolDef(
            name="progress",
            path="/developer/v1/things/progress",
            http_method="get",
            summary="Fetch progress",
            description="Fetch progress",
        )

        cmd = build_tool_command(tool)

        option_names = {p.name for p in cmd.params if isinstance(p, click.Option)}
        assert "json_body" not in option_names
        assert "dry_run" in option_names

    def test_application_progress_tool_has_wait_options(self):
        tool = ToolDef(
            name="get_application_progress_resource",
            path="/developer/v1/applications/progress",
            http_method="get",
            summary="Fetch financing application progress",
            description="Fetch financing application progress",
            category="applications",
            alias="progress",
        )

        cmd = build_tool_command(tool)

        option_names = {p.name for p in cmd.params if isinstance(p, click.Option)}
        assert "wait_for_action" in option_names
        assert "wait_for_phone_verification" in option_names
        assert "wait_for_identity_verification" in option_names
        assert "wait_interval" in option_names
        assert "wait_timeout" in option_names

    def test_application_progress_wait_polls_until_phone_action_disappears(
        self, monkeypatch
    ):
        tool = ToolDef(
            name="get_application_progress_resource",
            path="/developer/v1/applications/progress",
            http_method="get",
            summary="Fetch financing application progress",
            description="Fetch financing application progress",
            category="applications",
            alias="progress",
        )
        client = MagicMock()
        client.get.side_effect = [
            json.dumps(
                {
                    "status": "STARTED",
                    "required_actions": [
                        {
                            "type": "COMPLETE_APPLICANT_ACTION",
                            "applicant_action": "VERIFY_EMAIL_OR_PHONE",
                            "page_key": "phone_verification",
                            "section_key": "phone_verification",
                        }
                    ],
                }
            ).encode(),
            json.dumps(
                {
                    "status": "STARTED",
                    "required_actions": [],
                    "ready_for_submission": True,
                }
            ).encode(),
        ]
        monkeypatch.setattr("ramp_cli.tools.commands.RampClient", lambda env: client)
        monkeypatch.setattr("ramp_cli.tools.commands.maybe_sync", lambda env: None)
        sleep_calls = []
        monkeypatch.setattr(
            "ramp_cli.tools.commands.time.sleep",
            lambda seconds: sleep_calls.append(seconds),
        )

        result = CliRunner().invoke(
            build_tool_command(tool),
            [
                "--wait_for_phone_verification",
                "--wait_interval",
                "1",
                "--wait_timeout",
                "5",
            ],
            obj=_ctx_obj(),
        )

        assert result.exit_code == 0
        assert client.get.call_count == 2
        assert sleep_calls == [1]
        payload = json.loads(result.output)
        assert payload["data"][0]["ready_for_submission"] is True

    def test_application_progress_wait_accepts_identity_alias(self, monkeypatch):
        tool = ToolDef(
            name="get_application_progress_resource",
            path="/developer/v1/applications/progress",
            http_method="get",
            summary="Fetch financing application progress",
            description="Fetch financing application progress",
            category="applications",
            alias="progress",
        )
        client = MagicMock()
        client.get.side_effect = [
            json.dumps(
                {
                    "status": "FOLLOW_UPS_REQUIRED",
                    "required_actions": [
                        {
                            "type": "COMPLETE_APPLICANT_ACTION",
                            "applicant_action": "COMPLETE_IDENTITY_VERIFICATION",
                        }
                    ],
                }
            ).encode(),
            json.dumps({"status": "IN_REVIEW", "required_actions": []}).encode(),
        ]
        monkeypatch.setattr("ramp_cli.tools.commands.RampClient", lambda env: client)
        monkeypatch.setattr("ramp_cli.tools.commands.maybe_sync", lambda env: None)
        monkeypatch.setattr("ramp_cli.tools.commands.time.sleep", lambda seconds: None)

        result = CliRunner().invoke(
            build_tool_command(tool),
            [
                "--wait_for_action",
                "onfido",
                "--wait_interval",
                "1",
                "--wait_timeout",
                "5",
            ],
            obj=_ctx_obj(),
        )

        assert result.exit_code == 0
        assert client.get.call_count == 2
        assert json.loads(result.output)["data"][0]["status"] == "IN_REVIEW"

    def test_application_progress_wait_matches_custom_page_key(self, monkeypatch):
        tool = ToolDef(
            name="get_application_progress_resource",
            path="/developer/v1/applications/progress",
            http_method="get",
            summary="Fetch financing application progress",
            description="Fetch financing application progress",
            category="applications",
            alias="progress",
        )
        client = MagicMock()
        client.get.side_effect = [
            json.dumps(
                {
                    "status": "STARTED",
                    "required_actions": [
                        {
                            "type": "PROVIDE_APPLICATION_DATA",
                            "page_key": "business_info",
                            "section_key": "principal_place_of_business",
                        }
                    ],
                }
            ).encode(),
            json.dumps(
                {
                    "status": "STARTED",
                    "required_actions": [],
                    "ready_for_submission": True,
                }
            ).encode(),
        ]
        monkeypatch.setattr("ramp_cli.tools.commands.RampClient", lambda env: client)
        monkeypatch.setattr("ramp_cli.tools.commands.maybe_sync", lambda env: None)
        monkeypatch.setattr("ramp_cli.tools.commands.time.sleep", lambda seconds: None)

        result = CliRunner().invoke(
            build_tool_command(tool),
            [
                "--wait_for_action",
                "business_info",
                "--wait_interval",
                "1",
                "--wait_timeout",
                "5",
            ],
            obj=_ctx_obj(),
        )

        assert result.exit_code == 0
        assert client.get.call_count == 2
        assert json.loads(result.output)["data"][0]["ready_for_submission"] is True

    def test_application_progress_wait_timeout_reports_pending_action(
        self, monkeypatch
    ):
        tool = ToolDef(
            name="get_application_progress_resource",
            path="/developer/v1/applications/progress",
            http_method="get",
            summary="Fetch financing application progress",
            description="Fetch financing application progress",
            category="applications",
            alias="progress",
        )
        client = MagicMock()
        client.get.return_value = json.dumps(
            {
                "status": "STARTED",
                "required_actions": [
                    {
                        "type": "COMPLETE_APPLICANT_ACTION",
                        "applicant_action": "VERIFY_EMAIL_OR_PHONE",
                        "page_key": "phone_verification",
                    }
                ],
            }
        ).encode()
        monkeypatch.setattr("ramp_cli.tools.commands.RampClient", lambda env: client)
        monkeypatch.setattr("ramp_cli.tools.commands.maybe_sync", lambda env: None)
        monkeypatch.setattr(
            "ramp_cli.tools.commands.time.monotonic",
            MagicMock(side_effect=[0, 2]),
        )

        result = CliRunner().invoke(
            build_tool_command(tool),
            [
                "--wait_for_phone_verification",
                "--wait_interval",
                "1",
                "--wait_timeout",
                "1",
            ],
            obj=_ctx_obj(),
        )

        assert result.exit_code == 1
        assert "Timed out waiting for application actions" in result.output
        assert "VERIFY_EMAIL_OR_PHONE" in result.output

    def test_json_query_fields_are_not_sent_as_a_body(self):
        tool = ToolDef(
            name="list-things",
            path="/developer/v1/things",
            http_method="get",
            summary="List things",
            description="List things",
            params=[
                ToolParam(
                    name="page_size",
                    flag="page_size",
                    description="Page size",
                    type=ParamType.INT,
                    location="query",
                )
            ],
            json_schema=JsonSchema(properties={"page_size": JsonSchema()}),
        )
        command = build_tool_command(tool)

        result = CliRunner().invoke(
            command,
            ["--dry_run", "--json", '{"page_size":2}'],
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
        payload = json.loads(result.output)["data"][0]
        assert payload["url"].endswith("/developer/v1/things?page_size=2")
        assert payload["body"] == {}

    def test_multipart_uses_declared_method_and_omits_null_fields(
        self, tmp_path, monkeypatch
    ):
        document = tmp_path / "document.pdf"
        document.write_bytes(b"test document")
        tool = ToolDef(
            name="upload-document",
            path="/developer/v1/documents",
            http_method="patch",
            summary="Upload a document",
            description="Upload a document",
            params=[
                ToolParam(
                    name="file",
                    flag="file",
                    description="Document file",
                    type=ParamType.FILE,
                    required=True,
                    location="form",
                ),
                ToolParam(
                    name="association_id",
                    flag="association_id",
                    description="Optional association",
                    type=ParamType.STRING,
                    location="form",
                ),
            ],
            json_schema=JsonSchema(
                properties={
                    "file": JsonSchema(),
                    "association_id": JsonSchema(nullable=True),
                }
            ),
            request_content_type="multipart/form-data",
        )
        command = build_tool_command(tool)
        client = MagicMock()
        client.request_multipart.return_value = b"{}"
        monkeypatch.setattr("ramp_cli.tools.commands.RampClient", lambda env: client)
        monkeypatch.setattr(
            "ramp_cli.tools.commands._sync_tool_for_json_validation",
            lambda env, current_tool: current_tool,
        )

        result = CliRunner().invoke(
            command,
            [
                "--json",
                json.dumps(
                    {
                        "file": str(document),
                        "association_id": None,
                    }
                ),
            ],
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
        method, path, form_data, files = client.request_multipart.call_args.args
        assert method == "patch"
        assert path == "/developer/v1/documents"
        assert form_data == {}
        assert set(files) == {"file"}

    def test_closed_body_schema_routes_path_query_and_body_inputs(self, monkeypatch):
        spec = {
            "paths": {
                "/developer/v1/things/{thing_id}": {
                    "parameters": [
                        {
                            "name": "thing_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "patch": {
                        "summary": "Update a thing",
                        "parameters": [
                            {
                                "name": "page_size",
                                "in": "query",
                                "schema": {"type": "integer"},
                            }
                        ],
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "name": {"type": "string"},
                                        },
                                    }
                                }
                            }
                        },
                    },
                }
            }
        }
        tool = parse_spec_dict(
            spec,
            path_prefix=None,
            synthesize_cli_tools=False,
        )[0]
        client = MagicMock()
        client.patch.return_value = b"{}"
        monkeypatch.setattr("ramp_cli.tools.commands.RampClient", lambda env: client)
        monkeypatch.setattr(
            "ramp_cli.tools.commands._sync_tool_for_json_validation",
            lambda env, current_tool: current_tool,
        )

        result = CliRunner().invoke(
            build_tool_command(tool),
            [
                "--json",
                '{"thing_id":"thing/1","page_size":2,"name":"Updated"}',
            ],
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
        path, payload = client.patch.call_args.args
        assert path == "/developer/v1/things/thing%2F1?page_size=2"
        assert json.loads(payload) == {"name": "Updated"}

    def test_empty_delete_response_is_supported(self, monkeypatch):
        tool = ToolDef(
            name="delete-thing",
            path="/developer/v1/things/{thing_id}",
            http_method="delete",
            summary="Delete a thing",
            description="Delete a thing",
            params=[
                ToolParam(
                    name="thing_id",
                    flag="thing_id",
                    description="Thing ID",
                    type=ParamType.STRING,
                    required=True,
                    location="path",
                )
            ],
        )
        client = MagicMock()
        client.delete.return_value = b""
        monkeypatch.setattr("ramp_cli.tools.commands.RampClient", lambda env: client)
        monkeypatch.setattr("ramp_cli.tools.commands.maybe_sync", lambda env: None)

        result = CliRunner().invoke(
            build_tool_command(tool),
            ["thing/1"],
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
        client.delete.assert_called_once_with(
            "/developer/v1/things/thing%2F1",
            None,
        )
        assert json.loads(result.output)["data"] == [{}]

    def test_non_dry_run_procurement_get_json_keeps_get_transport(
        self, monkeypatch, isolated_config
    ):
        """Dry-run skips JSON schema sync, so exercise the real non-dry-run path."""
        _use_bundled_spec(monkeypatch)
        monkeypatch.setattr(
            "ramp_cli.tools.commands.maybe_sync", lambda env, force=False: None
        )
        client = MagicMock()
        client.get.return_value = b"{}"
        monkeypatch.setattr("ramp_cli.tools.commands.RampClient", lambda env: client)
        request_body = {
            "rationale": "Refresh the draft before continuing",
            "spend_request_uuid": "00000000-0000-4000-8000-000000000001",
        }

        result = CliRunner().invoke(
            cli,
            [
                "--agent",
                "procurement_requests",
                "get",
                "--json",
                json.dumps(request_body),
            ],
        )

        assert result.exit_code == 0, result.output
        client.get.assert_called_once_with(
            "/developer/v1/agent-tools/procurement-draft", request_body
        )
        client.post.assert_not_called()
        client.delete.assert_not_called()

    def test_non_dry_run_procurement_delete_json_keeps_delete_transport(
        self, monkeypatch, isolated_config
    ):
        """Dry-run skips JSON schema sync, so exercise the real non-dry-run path."""
        _use_bundled_spec(monkeypatch)
        monkeypatch.setattr(
            "ramp_cli.tools.commands.maybe_sync", lambda env, force=False: None
        )
        client = MagicMock()
        client.delete.return_value = b"{}"
        monkeypatch.setattr("ramp_cli.tools.commands.RampClient", lambda env: client)
        request_body = {
            "rationale": "Delete the abandoned draft",
            "spend_request_uuid": "00000000-0000-4000-8000-000000000001",
        }

        result = CliRunner().invoke(
            cli,
            [
                "--agent",
                "procurement_requests",
                "delete",
                "--json",
                json.dumps(request_body),
            ],
        )

        assert result.exit_code == 0, result.output
        path, payload = client.delete.call_args.args
        assert path == "/developer/v1/agent-tools/procurement-draft"
        assert json.loads(payload) == request_body
        client.get.assert_not_called()
        client.post.assert_not_called()

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

    @pytest.mark.parametrize(
        ("granted_scopes", "expected_help"),
        [
            ("cards:read_agentic", "Get agent card funds"),
            ("limits:read", "Get funds"),
            ("cards:read_agentic limits:read", "Get funds"),
            ("", "Get funds"),
        ],
    )
    def test_alias_collision_uses_known_grants_only_to_break_ties(
        self, isolated_config, granted_scopes, expected_help
    ):
        remapped_tool = ToolDef(
            name="get-agent-card-funds",
            path="/developer/v1/agent-tools/get-agent-card-funds",
            http_method="post",
            summary="Get agent card funds",
            description="Get agent card funds",
            category="agent_cards",
            alias="list",
            params=[],
            required_scopes=["cards:read_agentic"],
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
            required_scopes=["limits:read"],
        )
        if granted_scopes:
            store.save_tokens(
                "production",
                "tok",
                "refresh",
                granted_scopes=granted_scopes,
            )

        group = ToolGroup.build(
            "funds",
            [remapped_tool, native_tool],
            "Funds",
            env="production",
        )

        list_command = group.get_command(click.Context(group), "list")
        assert list_command is not None
        assert list_command.help == expected_help

    def test_external_token_ignores_stale_grants_for_alias_collision(
        self, isolated_config, monkeypatch
    ):
        remapped_tool = ToolDef(
            name="get-agent-card-funds",
            path="/developer/v1/agent-tools/get-agent-card-funds",
            http_method="post",
            summary="Get agent card funds",
            description="Get agent card funds",
            category="agent_cards",
            alias="list",
            params=[],
            required_scopes=["cards:read_agentic"],
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
            required_scopes=["limits:read"],
        )
        store.save_tokens(
            "production",
            "stored",
            "refresh",
            granted_scopes="cards:read_agentic",
        )
        monkeypatch.setenv("RAMP_ACCESS_TOKEN", "external")

        group = ToolGroup.build(
            "funds",
            [remapped_tool, native_tool],
            "Funds",
            env="production",
        )

        list_command = group.get_command(click.Context(group), "list")
        assert list_command is not None
        assert list_command.help == "Get funds"


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

    def test_idempotency_header_defaults_from_session(self, monkeypatch):
        monkeypatch.setattr("ramp_cli.tools.commands.get_session_id", lambda: "sid-123")
        nonces = iter(["nonce-a", "nonce-b"])
        monkeypatch.setattr("ramp_cli.tools.commands.uuid4", lambda: next(nonces))
        tool = ToolDef(
            name="request-funds",
            path="/developer/v1/things/{thing_id}",
            http_method="post",
            summary="Request funds",
            description="Request funds",
            params=[
                ToolParam(
                    name="thing_id",
                    flag="thing_id",
                    description="Thing ID",
                    type=ParamType.STRING,
                    required=True,
                    location="path",
                ),
                ToolParam(
                    name="amount",
                    flag="amount",
                    description="Amount",
                    type=ParamType.STRING,
                    required=True,
                    location="body",
                ),
                ToolParam(
                    name="X-Idempotency-Key",
                    flag="idempotency_key",
                    description="Idempotency key",
                    type=ParamType.STRING,
                    required=True,
                    location="header",
                ),
            ],
        )
        command = build_tool_command(tool)
        runner = CliRunner()

        first = runner.invoke(
            command,
            ["thing-1", "--amount", "12.34", "--dry_run"],
            obj=_ctx_obj(),
        )
        second = runner.invoke(
            command,
            ["thing-1", "--amount", "12.34", "--dry_run"],
            obj=_ctx_obj(),
        )

        assert first.exit_code == 0, first.output
        assert second.exit_code == 0, second.output
        first_payload = json.loads(first.output)["data"][0]
        second_payload = json.loads(second.output)["data"][0]
        first_key = first_payload["headers"]["X-Idempotency-Key"]
        second_key = second_payload["headers"]["X-Idempotency-Key"]
        assert first_key == "sid-123:request-funds:nonce-a"
        assert second_key == "sid-123:request-funds:nonce-b"
        assert first_key != second_key
        assert len(first_key) <= 255
        assert first_payload["body"] == {"amount": "12.34"}

    def test_explicit_idempotency_header_is_sent(self, monkeypatch):
        tool = ToolDef(
            name="request-funds",
            path="/developer/v1/things/{thing_id}",
            http_method="post",
            summary="Request funds",
            description="Request funds",
            params=[
                ToolParam(
                    name="thing_id",
                    flag="thing_id",
                    description="Thing ID",
                    type=ParamType.STRING,
                    required=True,
                    location="path",
                ),
                ToolParam(
                    name="X-Idempotency-Key",
                    flag="idempotency_key",
                    description="Idempotency key",
                    type=ParamType.STRING,
                    required=True,
                    location="header",
                ),
            ],
        )
        command = build_tool_command(tool)
        client = MagicMock()
        client.post.return_value = b"{}"
        monkeypatch.setattr("ramp_cli.tools.commands.RampClient", lambda env: client)
        monkeypatch.setattr("ramp_cli.tools.commands.maybe_sync", lambda env: None)

        result = CliRunner().invoke(
            command,
            ["thing-1", "--idempotency_key", "manual-key"],
            obj=_ctx_obj(),
        )

        assert result.exit_code == 0, result.output
        assert client.post.call_args.kwargs["headers"] == {
            "X-Idempotency-Key": "manual-key"
        }

    def test_json_merges_positional_body_ids(self):
        tool = ToolDef(
            name="request-funds",
            path="/developer/v1/banking/accounts/{banking_account_id}/drawdown-requests",
            http_method="post",
            summary="Request funds",
            description="Request funds",
            params=[
                ToolParam(
                    name="banking_account_id",
                    flag="banking_account_id",
                    description="Banking account ID",
                    type=ParamType.STRING,
                    required=True,
                    location="path",
                ),
                ToolParam(
                    name="external_bank_account_id",
                    flag="external_bank_account_id",
                    description="External bank account ID",
                    type=ParamType.STRING,
                    required=True,
                    location="body",
                ),
                ToolParam(
                    name="amount",
                    flag="amount",
                    description="Amount",
                    type=ParamType.OBJECT,
                    required=True,
                    is_complex=True,
                    location="body",
                ),
            ],
        )

        result = CliRunner().invoke(
            build_tool_command(tool),
            [
                "bank-1",
                "external-1",
                "--json",
                '{"amount":{"currency_code":"USD","amount":"12.00"}}',
                "--dry_run",
            ],
            obj=_ctx_obj(),
        )

        assert result.exit_code == 0, result.output
        body = json.loads(result.output)["data"][0]["body"]
        assert body == {
            "amount": {"currency_code": "USD", "amount": "12.00"},
            "external_bank_account_id": "external-1",
        }


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
        assert "ai-spend" in result.output
        assert "Inspect AI token spend" in result.output
        assert "ai-token-spend" not in result.output
        # cards is its own resource group; agent_cards is remapped into funds
        assert "cards" in result.output
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

    def test_resource_remains_visible_without_required_scope(
        self, monkeypatch, isolated_config
    ):
        task_tool = ToolDef(
            name="get-attention-feed",
            path="/developer/v1/agent-tools/get-attention-feed",
            http_method="post",
            summary="Get attention feed",
            description="Return the authenticated user's homepage attention feed",
            category="tasks",
            alias="list",
            params=[],
            required_scopes=["tasks:read"],
        )
        store.save_tokens("production", "tok", "refresh", granted_scopes="users:read")

        monkeypatch.setattr("ramp_cli.main.maybe_sync", lambda env: None)
        monkeypatch.setattr(
            "ramp_cli.main.list_categories", lambda env: {"tasks": [task_tool]}
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["--env", "production", "tasks", "list", "--help"])

        assert result.exit_code == 0
        assert "Usage: cli tasks list" in result.output

    def test_singleton_tool_remains_visible_without_required_scope(
        self, monkeypatch, isolated_config
    ):
        communication_tool = ToolDef(
            name="post-comment",
            path="/developer/v1/agent-tools/post-comment",
            http_method="post",
            summary="Post comment",
            description="Post a comment",
            category="communication",
            alias="comment",
            params=[],
            required_scopes=["comments:write"],
        )
        store.save_tokens("production", "tok", "refresh", granted_scopes="users:read")

        monkeypatch.setattr("ramp_cli.main.maybe_sync", lambda env: None)
        monkeypatch.setattr(
            "ramp_cli.main.list_categories",
            lambda env: {"communication": [communication_tool]},
        )

        runner = CliRunner()
        result = runner.invoke(
            cli, ["--env", "production", "general", "comment", "--help"]
        )

        assert result.exit_code == 0
        assert "Usage: cli general comment" in result.output

    def test_partially_scoped_resource_lists_all_commands(
        self, monkeypatch, isolated_config
    ):
        accessible_tool = ToolDef(
            name="get-requests-to-review",
            path="/developer/v1/agent-tools/get-requests-to-review",
            http_method="post",
            summary="Get requests to review",
            description="Get requests to review",
            category="requests",
            alias="review",
            params=[],
            required_scopes=["requests:read"],
        )
        hidden_tool = ToolDef(
            name="approve-request",
            path="/developer/v1/agent-tools/approve-request",
            http_method="post",
            summary="Approve request",
            description="Approve request",
            category="requests",
            alias="approve",
            params=[],
            required_scopes=["requests:write"],
        )
        store.save_tokens(
            "production", "tok", "refresh", granted_scopes="requests:read"
        )

        monkeypatch.setattr("ramp_cli.main.maybe_sync", lambda env: None)
        monkeypatch.setattr(
            "ramp_cli.main.list_categories",
            lambda env: {"requests": [accessible_tool, hidden_tool]},
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["--env", "production", "requests", "--help"])

        assert result.exit_code == 0
        assert "review" in result.output
        assert "approve" in result.output

    def test_ai_spend_group_contains_token_spend_tools(self, monkeypatch):
        _use_bundled_spec(monkeypatch)
        runner = CliRunner()

        result = runner.invoke(cli, ["ai-spend", "--help"])

        assert result.exit_code == 0
        assert "aggregates" in result.output
        assert "connections" in result.output
        assert "current-spend" in result.output
        assert "filter-options" in result.output

    def test_ai_token_spend_legacy_group_still_works(self, monkeypatch):
        _use_bundled_spec(monkeypatch)
        runner = CliRunner()

        result = runner.invoke(cli, ["ai-token-spend", "--help"])

        assert result.exit_code == 0
        assert "aggregates" in result.output
        assert "connections" in result.output
        assert "current-spend" in result.output
        assert "filter-options" in result.output

    def test_ai_spend_and_legacy_group_hit_same_endpoint(self, monkeypatch):
        _use_bundled_spec(monkeypatch)
        runner = CliRunner()
        args = [
            "aggregates",
            "--rationale",
            "Testing dry-run body",
            "--start_at",
            "2026-06-01T00:00:00Z",
            "--end_at",
            "2026-06-28T00:00:00Z",
            "--metric",
            "cost",
            "--dry_run",
        ]

        ai_spend = runner.invoke(cli, ["ai-spend", *args])
        legacy = runner.invoke(cli, ["ai-token-spend", *args])

        assert ai_spend.exit_code == 0, ai_spend.output
        assert legacy.exit_code == 0, legacy.output
        ai_spend_payload = json.loads(ai_spend.output)["data"][0]
        legacy_payload = json.loads(legacy.output)["data"][0]
        assert ai_spend_payload["url"].endswith("/get-ai-token-spend-aggregates")
        assert ai_spend_payload == legacy_payload

    def test_ai_spend_visible_without_required_scope(
        self, monkeypatch, isolated_config
    ):
        _use_bundled_spec(monkeypatch)
        store.save_tokens("production", "tok", "refresh", granted_scopes="users:read")
        monkeypatch.setattr("ramp_cli.main.maybe_sync", lambda env: None)

        runner = CliRunner()
        public_result = runner.invoke(
            cli, ["--env", "production", "ai-spend", "--help"]
        )
        legacy_result = runner.invoke(
            cli, ["--env", "production", "ai-token-spend", "--help"]
        )

        assert public_result.exit_code == 0
        assert "aggregates" in public_result.output
        assert legacy_result.exit_code == 0
        assert "aggregates" in legacy_result.output

    def test_funds_group_contains_card_tools(self, monkeypatch):
        _use_bundled_spec(monkeypatch)
        # Write-oriented card tools remain reachable under funds via the
        # cards->funds remap. Card listing now lives in the funds list tool.
        runner = CliRunner()
        result = runner.invoke(cli, ["funds", "--help"])
        assert result.exit_code == 0
        assert "activate-card" in result.output or "activate" in result.output
        assert "get-funds" in result.output or "list" in result.output
        assert "lock-or-unlock-card" in result.output

    def test_cards_group_contains_card_tools(self, monkeypatch):
        _use_bundled_spec(monkeypatch)
        # Additive alias group: cards is its own group with short aliases for
        # the card-specific command surface exposed by the current spec.
        runner = CliRunner()
        result = runner.invoke(cli, ["cards", "--help"])
        assert result.exit_code == 0
        assert "activate" in result.output
        assert "lock" in result.output

    def test_funds_list_uses_funds_endpoint(self, monkeypatch):
        _use_bundled_spec(monkeypatch)
        runner = CliRunner()
        funds = runner.invoke(cli, ["funds", "list", "--rationale", "x", "--dry_run"])
        assert funds.exit_code == 0
        funds_url = json.loads(funds.output)["data"][0]["url"]
        assert funds_url.endswith("/get-funds")

    def test_funds_list_invokable_with_readonly_limits_scope(
        self, monkeypatch, isolated_config
    ):
        _use_bundled_spec(monkeypatch)
        # Discovery remains complete even when the token only has `limits:read`.
        store.save_tokens("production", "tok", "refresh", granted_scopes="limits:read")
        monkeypatch.setattr("ramp_cli.main.maybe_sync", lambda env: None)

        runner = CliRunner()
        help_res = runner.invoke(
            cli, ["--env", "production", "funds", "list", "--help"]
        )
        assert help_res.exit_code == 0, help_res.output

        dry = runner.invoke(
            cli,
            ["--env", "production", "funds", "list", "--rationale", "x", "--dry_run"],
        )
        assert dry.exit_code == 0, dry.output
        assert json.loads(dry.output)["data"][0]["url"].endswith("/get-funds")

        cards = runner.invoke(cli, ["--env", "production", "cards", "--help"])
        assert cards.exit_code == 0
        assert "activate" in cards.output

    def test_treasury_account_numbers_invokable_with_readonly_scope(
        self, monkeypatch, isolated_config
    ):
        _use_bundled_spec(monkeypatch)
        # Regression: a least-privilege account-number token only sees one
        # treasury tool. Treasury must stay grouped so the advertised command
        # path remains available instead of folding into ``general``.
        store.save_tokens(
            "production",
            "tok",
            "refresh",
            granted_scopes="agent_account_numbers:read",
        )
        monkeypatch.setattr("ramp_cli.main.maybe_sync", lambda env: None)

        runner = CliRunner()
        help_res = runner.invoke(
            cli, ["--env", "production", "treasury", "account-numbers", "--help"]
        )
        assert help_res.exit_code == 0, help_res.output
        assert "No such command 'treasury'" not in help_res.output

        dry = runner.invoke(
            cli,
            [
                "--env",
                "production",
                "treasury",
                "account-numbers",
                "--dry_run",
            ],
        )
        assert dry.exit_code == 0, dry.output
        assert json.loads(dry.output)["data"][0]["url"].endswith(
            "/banking/agent-account-numbers"
        )

    def test_cards_is_alias_group_agent_cards_merged_into_funds(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        # "cards" is an additive alias resource group; "agent_cards" is still
        # merged into "funds" and should not appear as a standalone resource.
        lines = result.output.splitlines()
        resource_names = [line.strip().split()[0] for line in lines if line.strip()]
        assert "cards" in resource_names
        assert "funds" in resource_names
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

    def test_procurement_draft_generated_group_help_shows_change_request_params(
        self, monkeypatch
    ):
        _use_bundled_spec(monkeypatch)
        runner = CliRunner()
        help_result = runner.invoke(cli, ["procurement_requests", "draft", "--help"])
        assert help_result.exit_code == 0, help_result.output
        assert "--clear_change_request_field_ids" in help_result.output
        assert "change_request_answers" in help_result.output

    def test_procurement_new_draft_generated_group_builds_schema_payload(
        self, monkeypatch
    ):
        _use_bundled_spec(monkeypatch)
        result = CliRunner().invoke(
            cli,
            [
                "--agent",
                "--env",
                "sandbox",
                "procurement_requests",
                "draft",
                "--json",
                json.dumps(
                    {
                        "spend_intent_uuid": "spend-intent-uuid",
                        "rationale": "Create a procurement request draft",
                    }
                ),
                "--dry_run",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["data"][0]["body"] == {
            "spend_intent_uuid": "spend-intent-uuid",
            "rationale": "Create a procurement request draft",
        }

    def test_procurement_change_request_draft_generated_group_builds_schema_payload(
        self, monkeypatch
    ):
        _use_bundled_spec(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--agent",
                "--env",
                "sandbox",
                "procurement_requests",
                "draft",
                "--json",
                json.dumps(
                    {
                        "existing_spend_request_uuid": "existing-spend-request-uuid",
                        "rationale": "Populate procurement request draft",
                        "request_name": "Laptop request",
                        "currency": "USD",
                        "line_items": [
                            {
                                "description": "Laptop",
                                "amount": "2000.00",
                            }
                        ],
                        "answers": [
                            {
                                "answer_type": "text",
                                "field_id": "business_justification",
                                "value": "Need laptops",
                            },
                            {
                                "answer_type": "file_upload",
                                "field_id": "quote_upload",
                                "file_uuids": ["file-uuid"],
                            },
                        ],
                        "change_request_answers": [
                            {
                                "answer_type": "text",
                                "field_id": "change_reason",
                                "value": "Increase contract value",
                            }
                        ],
                    }
                ),
                "--dry_run",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        body = payload["data"][0]["body"]
        assert payload["data"][0]["url"].endswith(
            "/developer/v1/agent-tools/procurement-draft"
        )
        assert body == {
            "existing_spend_request_uuid": "existing-spend-request-uuid",
            "rationale": "Populate procurement request draft",
            "request_name": "Laptop request",
            "currency": "USD",
            "line_items": [
                {
                    "description": "Laptop",
                    "amount": "2000.00",
                }
            ],
            "answers": [
                {
                    "answer_type": "text",
                    "field_id": "business_justification",
                    "value": "Need laptops",
                },
                {
                    "answer_type": "file_upload",
                    "field_id": "quote_upload",
                    "file_uuids": ["file-uuid"],
                },
            ],
            "change_request_answers": [
                {
                    "answer_type": "text",
                    "field_id": "change_reason",
                    "value": "Increase contract value",
                }
            ],
        }

    def test_procurement_upload_file_generated_group_builds_multipart_payload(
        self, tmp_path, monkeypatch
    ):
        _use_bundled_spec(monkeypatch)
        upload = tmp_path / "quote.pdf"
        upload.write_text("quote")
        runner = CliRunner()

        result = runner.invoke(
            cli,
            [
                "--agent",
                "--env",
                "sandbox",
                "procurement_requests",
                "upload-file",
                "quote_upload",
                "spend-request-uuid",
                "--file",
                str(upload),
                "--rationale",
                "Attach quote",
                "--dry_run",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        body = payload["data"][0]["body"]
        assert payload["data"][0]["url"].endswith(
            "/developer/v1/agent-tools/procurement-upload-file"
        )
        assert body == {
            "spend_request_uuid": "spend-request-uuid",
            "field_id": "quote_upload",
            "rationale": "Attach quote",
            "file": str(upload),
        }

    def test_procurement_submit_generated_group_builds_confirmed_payload(
        self, monkeypatch
    ):
        _use_bundled_spec(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--agent",
                "--env",
                "sandbox",
                "procurement_requests",
                "submit",
                "spend-request-uuid",
                "--confirmed",
                "--rationale",
                "Submit confirmed procurement request",
                "--dry_run",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        body = payload["data"][0]["body"]
        assert payload["data"][0]["url"].endswith(
            "/developer/v1/agent-tools/procurement-submit"
        )
        assert body == {
            "spend_request_uuid": "spend-request-uuid",
            "confirmed": True,
            "rationale": "Submit confirmed procurement request",
        }

    def test_travel_search_keeps_legacy_alias(self, monkeypatch):
        _use_bundled_spec(monkeypatch)
        runner = CliRunner()

        help_result = runner.invoke(cli, ["travel", "--help"])
        assert help_result.exit_code == 0, help_result.output
        assert "search-flight" in help_result.output

        body = json.dumps(
            {
                "departure": "JFK",
                "arrival": "SFO",
                "departure_date": "2026-07-06",
                "rationale": "Search flights for the user",
            }
        )
        for command_name in ("search-flight", "search"):
            result = runner.invoke(
                cli,
                ["travel", command_name, "--dry_run", "--json", body],
            )
            assert result.exit_code == 0, result.output
            payload = json.loads(result.output)
            assert payload["data"][0]["url"].endswith(
                "/developer/v1/agent-tools/search-flights"
            )

    def test_travel_profile_tools_accept_traveler_user_id(self, monkeypatch):
        _use_bundled_spec(monkeypatch)
        runner = CliRunner()

        profile_result = runner.invoke(
            cli,
            [
                "travel",
                "profile",
                "--traveler_user_id",
                "traveler-uuid",
                "--dry_run",
                "--rationale",
                "test",
            ],
        )
        assert profile_result.exit_code == 0, profile_result.output
        profile_body = json.loads(profile_result.output)["data"][0]["body"]
        assert profile_body["traveler_user_id"] == "traveler-uuid"

        update_result = runner.invoke(
            cli,
            [
                "travel",
                "profile-update",
                "--traveler_user_id",
                "traveler-uuid",
                "--first_name",
                "Taylor",
                "--dry_run",
                "--rationale",
                "test",
            ],
        )
        assert update_result.exit_code == 0, update_result.output
        update_body = json.loads(update_result.output)["data"][0]["body"]
        assert update_body["traveler_user_id"] == "traveler-uuid"
        assert update_body["first_name"] == "Taylor"

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
