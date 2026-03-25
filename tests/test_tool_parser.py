"""Tests for the agent-tool OpenAPI spec parser."""

import pytest

from ramp_cli.specs import AGENT_TOOL_SPEC
from ramp_cli.tools.parser import (
    ParamType,
    ToolDef,
    ToolParam,
    parse_spec,
    parse_spec_dict,
)


@pytest.fixture(scope="module")
def tools() -> list[ToolDef]:
    return parse_spec(AGENT_TOOL_SPEC)


@pytest.fixture(scope="module")
def tool_map(tools: list[ToolDef]) -> dict[str, ToolDef]:
    return {t.name: t for t in tools}


# ── Spec loading ──


class TestSpecLoading:
    def test_parses_all_tools(self, tools: list[ToolDef]):
        assert len(tools) >= 40, f"Expected 40+ tools, got {len(tools)}"

    def test_tools_are_sorted(self, tools: list[ToolDef]):
        names = [t.name for t in tools]
        assert names == sorted(names)

    def test_all_tools_have_names(self, tools: list[ToolDef]):
        for tool in tools:
            assert tool.name, "Tool has empty name"
            assert "/" not in tool.name, f"Tool name contains slash: {tool.name}"

    def test_all_tools_have_paths(self, tools: list[ToolDef]):
        for tool in tools:
            assert tool.path.startswith("/developer/v1/agent-tools/")

    def test_all_tools_are_post(self, tools: list[ToolDef]):
        for tool in tools:
            assert tool.http_method == "post", f"{tool.name} is {tool.http_method}"

    def test_most_tools_have_scopes(self, tools: list[ToolDef]):
        tools_with_scopes = [t for t in tools if t.required_scopes]
        assert len(tools_with_scopes) >= len(tools) - 5, "Too many tools without scopes"

    def test_all_tools_have_request_schema(self, tools: list[ToolDef]):
        for tool in tools:
            assert tool.request_schema_name, f"{tool.name} has no request schema"


# ── Specific tools ──


class TestGetFunds:
    def test_exists(self, tool_map: dict[str, ToolDef]):
        assert "get-funds" in tool_map

    def test_scopes(self, tool_map: dict[str, ToolDef]):
        tool = tool_map["get-funds"]
        assert "limits:read" in tool.required_scopes

    def test_param_count(self, tool_map: dict[str, ToolDef]):
        tool = tool_map["get-funds"]
        assert len(tool.params) == 9

    def test_no_required_params(self, tool_map: dict[str, ToolDef]):
        tool = tool_map["get-funds"]
        required = [p for p in tool.params if p.required]
        assert len(required) == 0

    def test_funds_to_retrieve_is_enum(self, tool_map: dict[str, ToolDef]):
        param = _find_param(tool_map["get-funds"], "funds_to_retrieve")
        assert param is not None
        assert param.type is ParamType.ENUM
        assert param.enum_values is not None
        assert "ALL_FUNDS" in param.enum_values
        assert "MY_FUNDS" in param.enum_values

    def test_include_balance_is_bool(self, tool_map: dict[str, ToolDef]):
        param = _find_param(tool_map["get-funds"], "include_balance")
        assert param is not None
        assert param.type is ParamType.BOOL
        assert param.default is False

    def test_user_uuids_is_array(self, tool_map: dict[str, ToolDef]):
        param = _find_param(tool_map["get-funds"], "user_uuids")
        assert param is not None
        assert param.type is ParamType.ARRAY

    def test_search_by_fund_display_name_is_string(self, tool_map: dict[str, ToolDef]):
        param = _find_param(tool_map["get-funds"], "search_by_fund_display_name")
        assert param is not None
        assert param.type is ParamType.STRING


class TestActivateCard:
    def test_exists(self, tool_map: dict[str, ToolDef]):
        assert "activate-card" in tool_map

    def test_scopes(self, tool_map: dict[str, ToolDef]):
        assert "cards:write" in tool_map["activate-card"].required_scopes

    def test_has_one_required_param(self, tool_map: dict[str, ToolDef]):
        required = [p for p in tool_map["activate-card"].params if p.required]
        assert len(required) == 1
        assert required[0].name == "last_four"
        assert required[0].type is ParamType.STRING


class TestGetTransactions:
    def test_exists(self, tool_map: dict[str, ToolDef]):
        assert "get-transactions" in tool_map

    def test_has_enum_params(self, tool_map: dict[str, ToolDef]):
        param = _find_param(tool_map["get-transactions"], "state")
        assert param is not None
        assert param.type is ParamType.ENUM
        assert "cleared" in param.enum_values
        assert "declined" in param.enum_values

    def test_has_complex_params(self, tool_map: dict[str, ToolDef]):
        param = _find_param(tool_map["get-transactions"], "filters")
        assert param is not None
        assert param.is_complex

    def test_has_required_param(self, tool_map: dict[str, ToolDef]):
        required = [p for p in tool_map["get-transactions"].params if p.required]
        assert len(required) >= 1


class TestApproveOrRejectBill:
    def test_exists(self, tool_map: dict[str, ToolDef]):
        assert "approve-or-reject-bill" in tool_map

    def test_scopes(self, tool_map: dict[str, ToolDef]):
        assert "bills:write" in tool_map["approve-or-reject-bill"].required_scopes

    def test_has_required_params(self, tool_map: dict[str, ToolDef]):
        required = [p for p in tool_map["approve-or-reject-bill"].params if p.required]
        assert len(required) >= 2


class TestSearchBills:
    def test_exists(self, tool_map: dict[str, ToolDef]):
        assert "search-bills" in tool_map

    def test_scopes(self, tool_map: dict[str, ToolDef]):
        assert "bills:read" in tool_map["search-bills"].required_scopes


# ── Param type classification ──


class TestParamTypes:
    def test_all_params_have_names(self, tools: list[ToolDef]):
        for tool in tools:
            for param in tool.params:
                assert param.name, f"{tool.name} has param with empty name"

    def test_flags_match_names(self, tools: list[ToolDef]):
        for tool in tools:
            for param in tool.params:
                assert param.flag == param.name, (
                    f"{tool.name}.{param.name}: flag '{param.flag}' != name"
                )

    def test_enum_params_have_values(self, tools: list[ToolDef]):
        for tool in tools:
            for param in tool.params:
                if param.type is ParamType.ENUM:
                    assert param.enum_values, (
                        f"{tool.name}.{param.name}: enum with no values"
                    )
                    assert len(param.enum_values) >= 2, (
                        f"{tool.name}.{param.name}: enum with <2 values"
                    )

    def test_enum_array_params_have_values(self, tools: list[ToolDef]):
        for tool in tools:
            for param in tool.params:
                if param.type is ParamType.ENUM_ARRAY:
                    assert param.enum_values, (
                        f"{tool.name}.{param.name}: enum_array with no values"
                    )

    def test_complex_params_flagged(self, tools: list[ToolDef]):
        for tool in tools:
            for param in tool.params:
                if param.type is ParamType.OBJECT:
                    assert param.is_complex, (
                        f"{tool.name}.{param.name}: object not marked complex"
                    )

    def test_valid_types(self, tools: list[ToolDef]):
        for tool in tools:
            for param in tool.params:
                assert isinstance(param.type, ParamType), (
                    f"{tool.name}.{param.name}: type is not ParamType"
                )


# ── Edge cases ──


class TestAlias:
    def test_alias_parsed_from_spec(self):
        spec = {
            "paths": {
                "/developer/v1/agent-tools/get-funds": {
                    "post": {
                        "summary": "Get funds",
                        "x-alias": "list",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Req"}
                                }
                            }
                        },
                    },
                }
            },
            "components": {"schemas": {"Req": {"type": "object", "properties": {}}}},
        }
        tools = parse_spec_dict(spec)
        assert len(tools) == 1
        assert tools[0].alias == "list"
        assert tools[0].name == "get-funds"

    def test_alias_defaults_to_empty(self):
        spec = {
            "paths": {
                "/developer/v1/agent-tools/get-funds": {
                    "post": {
                        "summary": "Get funds",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Req"}
                                }
                            }
                        },
                    },
                }
            },
            "components": {"schemas": {"Req": {"type": "object", "properties": {}}}},
        }
        tools = parse_spec_dict(spec)
        assert len(tools) == 1
        assert tools[0].alias == ""

    def test_bundled_spec_tools_have_alias_or_empty(self, tools: list[ToolDef]):
        for tool in tools:
            assert isinstance(tool.alias, str), f"{tool.name}: alias is not a string"


class TestEdgeCases:
    def test_empty_spec(self):
        assert parse_spec_dict({}) == []

    def test_spec_with_no_agent_tools(self):
        spec = {"paths": {"/developer/v1/users": {"get": {}}}}
        assert parse_spec_dict(spec) == []

    def test_skips_x_prefixed_keys(self):
        spec = {
            "paths": {
                "/developer/v1/agent-tools/test": {
                    "x-source-details": {"class": "Foo"},
                    "post": {
                        "summary": "Test",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/TestReq"}
                                }
                            }
                        },
                        "security": [{"oauth2": ["test:read"]}],
                    },
                }
            },
            "components": {
                "schemas": {
                    "TestReq": {
                        "type": "object",
                        "properties": {"foo": {"type": "string"}},
                    }
                }
            },
        }
        tools = parse_spec_dict(spec)
        assert len(tools) == 1
        assert tools[0].name == "test"
        assert tools[0].params[0].type is ParamType.STRING


# ── Helpers ──


def _find_param(tool: ToolDef, name: str) -> ToolParam | None:
    for p in tool.params:
        if p.name == name:
            return p
    return None
