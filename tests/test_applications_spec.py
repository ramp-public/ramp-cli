"""Unified agent-tool spec coverage for financing application operations."""

from __future__ import annotations

import json

from ramp_cli.auth import oauth
from ramp_cli.specs import AGENT_TOOL_SPEC
from ramp_cli.tools.parser import ParamType, parse_spec
from ramp_cli.tools.registry import get_tool, list_categories, reload

EXPECTED_APPLICATION_ALIASES = {
    "get_application_resource": "get",
    "patch_application_update_resource": "edit",
    "get_application_document_resource": "documents",
    "post_application_document_resource": "upload",
    "delete_application_document_detail_resource": "delete-document",
    "get_application_followup_list_resource": "followups",
    "post_application_followup_submit_resource": "submit",
    "patch_application_followup_resource": "update-followup",
    "get_application_progress_resource": "progress",
}


def _unified_tools():
    return parse_spec(
        AGENT_TOOL_SPEC,
        path_prefix=None,
        synthesize_cli_tools=True,
    )


class TestUnifiedApplicationOperations:
    def test_uses_operation_ids_internally_and_aliases_for_cli(self):
        tools = {
            tool.name: tool
            for tool in _unified_tools()
            if tool.category == "applications"
        }

        assert {name: tool.alias for name, tool in tools.items()} == (
            EXPECTED_APPLICATION_ALIASES
        )
        assert all(tool.source == "agent-tools" for tool in tools.values())

    def test_parses_transport_scope_and_parameter_metadata(self):
        tools = {tool.name: tool for tool in _unified_tools()}

        progress = tools["get_application_progress_resource"]
        assert progress.http_method == "get"
        assert progress.required_scopes == ["applications:read"]

        edit = tools["patch_application_update_resource"]
        assert edit.http_method == "patch"
        assert edit.required_scopes == ["applications:write"]

        upload = tools["post_application_document_resource"]
        assert upload.request_content_type == "multipart/form-data"
        assert next(param for param in upload.params if param.name == "file").type is (
            ParamType.FILE
        )

        delete = tools["delete_application_document_detail_resource"]
        document_id = next(
            param for param in delete.params if param.name == "document_id"
        )
        assert document_id.location == "path"
        assert document_id.required

    def test_registry_loads_unified_operations(self, isolated_config):
        reload("production")

        assert get_tool("get_application_progress_resource", "production") is not None
        application_tools = list_categories("production")["applications"]
        assert {tool.alias for tool in application_tools} == set(
            EXPECTED_APPLICATION_ALIASES.values()
        )

    def test_oauth_scopes_include_unified_operations(self, isolated_config):
        scopes = set(oauth._resolve_scopes("production").split())
        assert {
            "applications:read",
            "applications:write",
            "agent_account_numbers:read",
            "bank_accounts:read",
            "banking_drawdown_requests:write",
        } <= scopes

    def test_oauth_scope_catalog_includes_generated_operation_scopes(self):
        spec = json.loads(AGENT_TOOL_SPEC.read_text())
        flows = spec["components"]["securitySchemes"]["oauth2"]["flows"]

        for flow in flows.values():
            assert "agent_account_numbers:read" in flow["scopes"]
            assert "banking_drawdown_requests:write" in flow["scopes"]
