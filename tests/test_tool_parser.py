"""Tests for the agent-tool OpenAPI spec parser."""

import json

import pytest

from ramp_cli.specs import AGENT_TOOL_SPEC
from ramp_cli.tools.parser import (
    ParamType,
    ToolDef,
    ToolParam,
    _parse_endpoint,
    extract_all_scopes,
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
            assert tool.path.startswith("/developer/v1/")

    def test_all_tools_have_valid_method(self, tools: list[ToolDef]):
        for tool in tools:
            assert tool.http_method in ("post", "get", "patch", "delete"), (
                f"{tool.name} has unexpected method {tool.http_method}"
            )

    def test_most_tools_have_scopes(self, tools: list[ToolDef]):
        tools_with_scopes = [t for t in tools if t.required_scopes]
        assert len(tools_with_scopes) >= len(tools) - 5, "Too many tools without scopes"

    def test_all_tools_have_request_schema(self, tools: list[ToolDef]):
        for tool in tools:
            if tool.http_method == "post" and any(
                param.location in {"body", "form"} for param in tool.params
            ):
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
        assert len(tool.params) == 13

    def test_rationale_is_required(self, tool_map: dict[str, ToolDef]):
        tool = tool_map["get-funds"]
        required = [p for p in tool.params if p.required]
        assert [p.name for p in required] == ["rationale"]

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

    def test_has_expected_required_params(self, tool_map: dict[str, ToolDef]):
        required = [p for p in tool_map["activate-card"].params if p.required]
        assert [p.name for p in required] == ["last_four", "rationale"]
        assert required[0].type is ParamType.STRING


class TestHeaderParams:
    def test_required_idempotency_header_is_parsed(self, tool_map: dict[str, ToolDef]):
        tool = tool_map["post_banking_drawdown_requests_resource"]
        param = _find_param(tool, "X-Idempotency-Key")

        assert param is not None
        assert param.location == "header"
        assert param.flag == "idempotency_key"
        assert param.required is True
        assert param.type is ParamType.STRING

    def test_optional_idempotency_header_is_parsed(self, tool_map: dict[str, ToolDef]):
        tool = tool_map["post_application_document_resource"]
        param = _find_param(tool, "X-Idempotency-Key")

        assert param is not None
        assert param.location == "header"
        assert param.flag == "idempotency_key"
        assert param.required is False


class TestGetTransactions:
    def test_exists(self, tool_map: dict[str, ToolDef]):
        assert "get-transactions" in tool_map

    def test_transaction_info_includes_decline_reason(self):
        spec = json.loads(AGENT_TOOL_SPEC.read_text())
        transaction_info = spec["components"]["schemas"]["TransactionInfo"]

        assert "decline_reason" in transaction_info["properties"]

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


class TestGetBillsForApproval:
    def test_exists(self, tool_map: dict[str, ToolDef]):
        assert "get-bills-for-approval" in tool_map

    def test_scopes(self, tool_map: dict[str, ToolDef]):
        assert "bills:read" in tool_map["get-bills-for-approval"].required_scopes


class TestSearchBills:
    def test_exists(self, tool_map: dict[str, ToolDef]):
        assert "search-bills" in tool_map

    def test_scopes(self, tool_map: dict[str, ToolDef]):
        assert "bills:read" in tool_map["search-bills"].required_scopes


class TestHotelBookingTools:
    def test_aliases_and_scopes(self, tool_map: dict[str, ToolDef]):
        search = tool_map["search-hotel"]
        rates = tool_map["get-hotel-rates"]
        submit = tool_map["submit-hotel-booking"]

        assert search.alias == ""
        assert rates.alias == "hotel-rates"
        assert submit.alias == "book-hotel"
        assert search.category == rates.category == submit.category == "travel"
        assert search.required_scopes == rates.required_scopes == ["trips:read"]
        assert submit.required_scopes == ["trips:write"]

    def test_search_parameters(self, tool_map: dict[str, ToolDef]):
        search = tool_map["search-hotel"]
        required = [param.name for param in search.params if param.required]

        assert required == ["rationale"]
        assert _find_param(search, "check_in_date").default is None
        assert _find_param(search, "check_out_date").default is None
        assert _find_param(search, "location_query").default is None
        assert _find_param(search, "cursor").type is ParamType.STRING
        assert _find_param(search, "hotel_name").type is ParamType.STRING
        assert _find_param(search, "traveler_user_id").type is ParamType.STRING
        assert _find_param(search, "filters").is_complex
        assert _find_param(search, "sort").is_complex

    def test_submit_parameters(self, tool_map: dict[str, ToolDef]):
        submit = tool_map["submit-hotel-booking"]
        required = [param.name for param in submit.params if param.required]

        assert required == [
            "check_in_date",
            "check_out_date",
            "hotel_id",
            "rate_id",
            "rationale",
        ]
        assert _find_param(submit, "confirm").type is ParamType.BOOL
        assert _find_param(submit, "expected_total_amount").type is ParamType.STRING
        assert _find_param(submit, "oop_reason").type is ParamType.STRING
        assert _find_param(submit, "reason").type is ParamType.STRING
        assert all(param.name != "simulate" for param in submit.params)

    def test_hotel_rates_parameters(self, tool_map: dict[str, ToolDef]):
        rates = tool_map["get-hotel-rates"]
        required = [param.name for param in rates.params if param.required]

        assert required == [
            "check_in_date",
            "check_out_date",
            "hotel_id",
            "rationale",
        ]
        assert _find_param(rates, "traveler_user_id").type is ParamType.STRING

        spec = json.loads(AGENT_TOOL_SPEC.read_text())
        result = spec["components"]["schemas"]["GetHotelRatesResult"]["properties"]
        room = spec["components"]["schemas"]["HotelRoomRates"]["properties"]
        rate = spec["components"]["schemas"]["HotelRateOptionV1"]["properties"]
        assert {"all_rates", "hotel", "recommended_rates"} <= result.keys()
        assert {
            "best_rate",
            "rates",
            "room_amenities",
            "room_description",
            "room_id",
            "room_name",
        } <= room.keys()
        assert {
            "all_in_nightly_amount",
            "cancellation_policy",
            "currency",
            "earns_loyalty_points",
            "is_corporate_rate",
            "loyalty_eligible",
            "loyalty_required",
            "nightly_amount",
            "payment_type",
            "policy_violations",
            "refundability",
            "total_amount",
        } <= rate.keys()

    def test_search_contract_returns_best_rate_with_metadata(self):
        spec = json.loads(AGENT_TOOL_SPEC.read_text())
        operation = spec["paths"]["/developer/v1/agent-tools/search-hotel"]["post"]
        request = spec["components"]["schemas"]["SearchHotelsRequestBody"]
        hotel = spec["components"]["schemas"]["HotelSearchOfferV1"]["properties"]
        rate = spec["components"]["schemas"]["HotelRateOptionV1"]["properties"]

        assert "first call runs static hotel search" in operation["description"]
        assert "x-alias" not in operation
        assert "Pagination reads from that" in request["description"]
        for description in (operation["description"], request["description"]):
            assert "Before booking" in description
            assert "GetHotelRates" in description
            assert "selected current rate" in description
        assert {
            "coworker_booking_count",
            "hotel_amenities",
            "office_travel_mode",
            "office_travel_time_minutes",
            "rates",
        } <= hotel.keys()
        assert {
            "cancellation_policy",
            "earns_loyalty_points",
            "is_company_preferred",
            "is_corporate_rate",
            "loyalty_eligible",
            "loyalty_required",
            "nightly_amount",
            "policy_violations",
            "refundability",
            "room_amenities",
            "total_amount",
        } <= rate.keys()

    def test_submit_contract_returns_preview_and_booking_details(self):
        spec = json.loads(AGENT_TOOL_SPEC.read_text())
        result = spec["components"]["schemas"]["HotelBookingResult"]

        assert {
            "address",
            "approval_steps",
            "booked",
            "booking",
            "check_in_date",
            "check_out_date",
            "hotel_name",
            "in_policy",
            "nightly_rate",
            "policy_violations",
            "requires_approval",
            "room_name",
            "total_amount",
        } <= result["properties"].keys()
        assert {"eligible_funds", "loyalty_programs"} <= result["properties"].keys()

        fund = spec["components"]["schemas"]["SuggestedFundInfo"]["properties"]
        loyalty = spec["components"]["schemas"]["ApplicableTravelerLoyaltyProgram"][
            "properties"
        ]
        assert {"available_balance", "fund_name", "fund_uuid", "spending_limit"} <= (
            fund.keys()
        )
        assert {"display_name", "logo", "loyalty_number", "loyalty_program_id"} <= (
            loyalty.keys()
        )


class TestListBills:
    def test_exists(self, tool_map: dict[str, ToolDef]):
        assert "list-bills" in tool_map

    def test_alias(self, tool_map: dict[str, ToolDef]):
        assert tool_map["list-bills"].alias == "list"

    def test_scopes(self, tool_map: dict[str, ToolDef]):
        assert "bills:read" in tool_map["list-bills"].required_scopes

    def test_query_is_optional(self, tool_map: dict[str, ToolDef]):
        param = _find_param(tool_map["list-bills"], "query")
        assert param is not None
        assert not param.required


class TestEnrollBusinessInAgentCards:
    def test_alias(self, tool_map: dict[str, ToolDef]):
        assert tool_map["enroll-business-in-agent-cards"].alias == "enroll"


class TestAgentAccountNumbers:
    def test_exists(self, tool_map: dict[str, ToolDef]):
        assert "get_agent_account_numbers_list_resource" in tool_map

    def test_alias_and_category(self, tool_map: dict[str, ToolDef]):
        tool = tool_map["get_agent_account_numbers_list_resource"]
        assert tool.alias == "account-numbers"
        assert tool.category == "treasury"

    def test_scopes(self, tool_map: dict[str, ToolDef]):
        tool = tool_map["get_agent_account_numbers_list_resource"]
        assert tool.required_scopes == ["agent_account_numbers:read"]

    def test_query_params(self, tool_map: dict[str, ToolDef]):
        tool = tool_map["get_agent_account_numbers_list_resource"]
        assert [(p.name, p.type, p.required) for p in tool.params] == [
            ("page_size", ParamType.INT, False),
            ("start", ParamType.STRING, False),
        ]


# ── Param type classification ──


class TestParamTypes:
    @pytest.mark.parametrize("constraint", [{"enum": [True]}, {"const": True}])
    def test_boolean_constraints_remain_boolean_params(self, constraint):
        spec = {
            "paths": {
                "/developer/v1/agent-tools/create-x402": {
                    "post": {
                        "summary": "Create an x402 payment",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "confirmed": {
                                                "type": "boolean",
                                                **constraint,
                                            }
                                        },
                                    }
                                }
                            }
                        },
                    }
                }
            }
        }

        confirmed = parse_spec_dict(spec)[0].params[0]

        assert confirmed.type is ParamType.BOOL
        assert confirmed.enum_values is None

    def test_all_params_have_names(self, tools: list[ToolDef]):
        for tool in tools:
            for param in tool.params:
                assert param.name, f"{tool.name} has param with empty name"

    def test_flags_match_names(self, tools: list[ToolDef]):
        for tool in tools:
            for param in tool.params:
                if param.location == "header":
                    assert "-" not in param.flag
                    assert param.flag == param.flag.lower()
                    continue
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

    def test_unified_request_search_alias_comes_from_spec(self, tools: list[ToolDef]):
        tool = next(t for t in tools if t.name == "search-unified-requests")
        assert tool.alias == "search"

    def test_bundled_spec_tools_have_alias_or_empty(self, tools: list[ToolDef]):
        for tool in tools:
            assert isinstance(tool.alias, str), f"{tool.name}: alias is not a string"

    def test_shared_agent_tool_path_uses_method_qualified_internal_names(self):
        spec = {
            "paths": {
                "/developer/v1/agent-tools/procurement-draft": {
                    method: {
                        "summary": f"{method.title()} draft",
                        "x-alias": alias,
                    }
                    for method, alias in (
                        ("delete", "delete"),
                        ("get", "get"),
                        ("post", "draft"),
                    )
                }
            }
        }

        tools = parse_spec_dict(spec, synthesize_cli_tools=False)

        assert {(tool.name, tool.alias, tool.http_method) for tool in tools} == {
            ("delete-procurement-draft", "delete", "delete"),
            ("get-procurement-draft", "get", "get"),
            ("post-procurement-draft", "draft", "post"),
        }


class TestJsonSchema:
    def test_procurement_answers_describe_visible_field_write_allowlist(self):
        spec = json.loads(AGENT_TOOL_SPEC.read_text())
        answers = spec["components"]["schemas"]["DraftProcurementRequestRequestBody"][
            "properties"
        ]["answers"]

        assert (
            "draft_state.fields visibility-based write allowlist"
            in answers["description"]
        )
        assert "fields_to_answer is the prioritized subset" in answers["description"]

    def test_bundled_procurement_change_request_draft_input(self):
        spec = json.loads(AGENT_TOOL_SPEC.read_text())
        schemas = spec["components"]["schemas"]
        draft_properties = schemas["DraftProcurementRequestRequestBody"]["properties"]

        assert "existing_spend_request_uuid" in draft_properties
        assert "change_request_answers" in draft_properties
        assert "clear_change_request_field_ids" in draft_properties
        assert "uuid" in schemas["ProcurementLineItemDataRequestBody"]["properties"]

    def test_bundled_procurement_change_request_draft_state(self):
        spec = json.loads(AGENT_TOOL_SPEC.read_text())
        schemas = spec["components"]["schemas"]
        draft_state = schemas["ProcurementDraftState"]["properties"]
        assert "change_request_state" in draft_state

    @pytest.mark.parametrize(
        "response_schema_name",
        [
            "ProcurementDraftSummary",
            "ProcurementSubmittedRequest",
            "UnifiedRequestDetailsOutput",
        ],
    )
    def test_bundled_procurement_change_request_response(self, response_schema_name):
        spec = json.loads(AGENT_TOOL_SPEC.read_text())
        response_properties = spec["components"]["schemas"][response_schema_name][
            "properties"
        ]

        assert "original_request" in response_properties
        assert "change_request_diff" in response_properties
        assert response_properties["original_request"]["allOf"] == [
            {"$ref": "#/components/schemas/OriginalProcurementRequest"}
        ]

    @pytest.mark.parametrize(
        "diff_schema_name",
        [
            "ProcurementChangeRequestAccountingFieldDiff",
            "ProcurementChangeRequestFilesFieldDiff",
            "ProcurementChangeRequestLineItemFieldDiff",
            "ProcurementChangeRequestLinkFieldDiff",
            "ProcurementChangeRequestTextFieldDiff",
        ],
    )
    def test_bundled_typed_change_request_field_diff_requires_discriminator(
        self, diff_schema_name
    ):
        spec = json.loads(AGENT_TOOL_SPEC.read_text())
        diff_schema = spec["components"]["schemas"][diff_schema_name]

        assert "field_type" in diff_schema["required"]
        assert "is_custom_field" in diff_schema["required"]

    def test_bundled_legacy_change_request_field_diff_metadata(self):
        spec = json.loads(AGENT_TOOL_SPEC.read_text())
        legacy_schema = spec["components"]["schemas"][
            "ProcurementChangeRequestLegacyFieldDiff"
        ]

        assert "is_custom_field" in legacy_schema["required"]
        assert "field_type" not in legacy_schema["properties"]

    def test_procurement_upload_returns_reusable_answer(self):
        spec = json.loads(AGENT_TOOL_SPEC.read_text())
        upload_result = spec["components"]["schemas"][
            "ProcurementUploadedFileResultJsonMode"
        ]

        assert "answer" in upload_result["properties"]

    def test_unified_request_search_schema_has_nested_filters(
        self, tool_map: dict[str, ToolDef]
    ):
        schema = tool_map["search-unified-requests"].json_schema
        assert schema is not None
        assert set(schema.properties) == {
            "filters",
            "limit",
            "page_cursor",
            "rationale",
        }

        filters = schema.properties["filters"]
        assert "search" in filters.properties
        assert "request_statuses" in filters.properties
        assert "unified_spend_request_types" in filters.properties

    def test_unified_request_search_schema_tracks_enum_arrays(
        self, tool_map: dict[str, ToolDef]
    ):
        filters = tool_map["search-unified-requests"].json_schema.properties["filters"]

        request_statuses = filters.properties["request_statuses"]
        assert request_statuses.array_item is not None
        assert request_statuses.array_item.enum_values == [
            "APPROVED",
            "DRAFT",
            "PENDING",
            "REJECTED",
        ]

        request_types = filters.properties["unified_spend_request_types"]
        assert request_types.array_item is not None
        assert "PURCHASE_ORDER" in request_types.array_item.enum_values

    def test_purchase_order_search_schema_preserves_distinct_status_enum(
        self, tool_map: dict[str, ToolDef]
    ):
        filters = tool_map["search-purchase-orders"].json_schema.properties["filters"]
        statuses = filters.properties["spend_request_statuses"]
        assert statuses.array_item is not None
        assert statuses.array_item.enum_values == [
            "APPROVED",
            "DRAFT",
            "REJECTED",
            "SUBMITTED",
        ]

    def test_nullable_is_preserved_through_all_of_enum_ref(
        self, tool_map: dict[str, ToolDef]
    ):
        schema = tool_map["get-funds"].json_schema
        assert schema is not None
        funds_to_retrieve = schema.properties["funds_to_retrieve"]
        assert funds_to_retrieve.nullable is True
        assert funds_to_retrieve.enum_values == ["ALL_FUNDS", "MY_FUNDS"]


class TestEdgeCases:
    def test_nested_required_properties_are_preserved(self):
        spec = {
            "paths": {
                "/developer/v1/agent-tools/create-widget": {
                    "post": {
                        "x-platforms": ["cli"],
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/CreateWidget"
                                    }
                                }
                            }
                        },
                    }
                }
            },
            "components": {
                "schemas": {
                    "CreateWidget": {
                        "type": "object",
                        "properties": {
                            "destinations": {
                                "type": "array",
                                "items": {"$ref": "#/components/schemas/Destination"},
                            }
                        },
                    },
                    "Destination": {
                        "type": "object",
                        "required": ["location"],
                        "properties": {
                            "location": {"$ref": "#/components/schemas/Location"}
                        },
                    },
                    "Location": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                }
            },
        }

        tool = parse_spec_dict(spec)[0]
        destination = tool.json_schema.properties["destinations"].array_item

        assert destination is not None
        assert destination.required_properties == {"location"}

    def test_parse_endpoint_derives_name_when_not_supplied(self):
        tool = _parse_endpoint(
            "/developer/v1/agent-tools/get-status",
            "get",
            {"summary": "Get status"},
            {},
        )

        assert tool is not None
        assert tool.name == "get-status"

    def test_empty_spec(self):
        assert parse_spec_dict({}) == []

    def test_spec_with_no_agent_tools(self):
        spec = {"paths": {"/developer/v1/users": {"get": {}}}}
        assert parse_spec_dict(spec, path_prefix="/developer/v1/agent-tools/") == []

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

    def test_skips_non_cli_tools(self):
        spec = {
            "paths": {
                "/developer/v1/agent-tools/cli-tool": {
                    "post": {
                        "summary": "CLI tool",
                        "x-platforms": ["cli", "mcp"],
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/CliReq"}
                                }
                            }
                        },
                    }
                },
                "/developer/v1/agent-tools/no-platform-tool": {
                    "post": {
                        "summary": "No platform tool",
                        "x-platforms": [],
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/NoPlatformReq"
                                    }
                                }
                            }
                        },
                    }
                },
                "/developer/v1/agent-tools/mcp-tool": {
                    "post": {
                        "summary": "MCP tool",
                        "x-platforms": ["mcp"],
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/McpReq"}
                                }
                            }
                        },
                    }
                },
            },
            "components": {
                "schemas": {
                    "CliReq": {"type": "object", "properties": {}},
                    "McpReq": {"type": "object", "properties": {}},
                    "NoPlatformReq": {"type": "object", "properties": {}},
                }
            },
        }

        tools = parse_spec_dict(spec)
        assert [tool.name for tool in tools] == ["cli-tool"]

    def test_missing_platform_metadata_defaults_to_visible(self):
        spec = {
            "paths": {
                "/developer/v1/agent-tools/test-tool": {
                    "post": {
                        "summary": "Test tool",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/TestReq"}
                                }
                            }
                        },
                    }
                }
            },
            "components": {
                "schemas": {"TestReq": {"type": "object", "properties": {}}}
            },
        }

        tools = parse_spec_dict(spec)
        assert [tool.name for tool in tools] == ["test-tool"]


# ── GET endpoint parsing ──


class TestGetEndpointParsing:
    def test_get_with_query_params(self):
        spec = {
            "paths": {
                "/developer/v1/agent-tools/get-status": {
                    "get": {
                        "summary": "Get status",
                        "x-alias": "status",
                        "tags": ["Agent Tool", "vendors"],
                        "security": [{"oauth2": ["vendors:read"]}],
                        "parameters": [
                            {
                                "in": "query",
                                "name": "batch_id",
                                "required": True,
                                "schema": {
                                    "type": "string",
                                    "description": "The batch ID",
                                    "title": "Batch Id",
                                },
                            },
                            {
                                "in": "query",
                                "name": "is_active",
                                "required": False,
                                "schema": {
                                    "type": "boolean",
                                    "default": None,
                                    "nullable": True,
                                    "description": "Filter by active status",
                                    "title": "Is Active",
                                },
                            },
                        ],
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/StatusResult"
                                        }
                                    }
                                },
                                "description": "Success",
                            }
                        },
                    }
                }
            },
            "components": {
                "schemas": {
                    "StatusResult": {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                    }
                }
            },
        }
        tools = parse_spec_dict(spec)
        assert len(tools) == 1
        tool = tools[0]
        assert tool.name == "get-status"
        assert tool.http_method == "get"
        assert tool.alias == "status"
        assert tool.category == "vendors"
        assert tool.required_scopes == ["vendors:read"]
        assert tool.response_schema_name == "StatusResult"
        assert len(tool.params) == 2
        # Required param first
        assert tool.params[0].name == "batch_id"
        assert tool.params[0].required is True
        assert tool.params[0].type is ParamType.STRING
        # Optional param second
        assert tool.params[1].name == "is_active"
        assert tool.params[1].required is False
        assert tool.params[1].type is ParamType.BOOL


class TestGenericOpenAPIParsing:
    def test_alias_selected_operations_share_the_main_parser(self):
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
                        "x-alias": "update",
                        "security": [{"oauth2": ["things:write"]}],
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "details": {
                                                "type": "object",
                                                "properties": {
                                                    "name": {"type": "string"}
                                                },
                                            }
                                        },
                                    }
                                }
                            }
                        },
                        "responses": {"204": {"description": "Updated"}},
                    },
                },
                "/developer/v1/things/documents": {
                    "post": {
                        "summary": "Upload a document",
                        "x-alias": "upload-document",
                        "requestBody": {
                            "content": {
                                "multipart/form-data": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["file"],
                                        "properties": {
                                            "file": {
                                                "type": "string",
                                                "format": "binary",
                                            },
                                            "purpose": {"type": "string"},
                                        },
                                    }
                                }
                            }
                        },
                        "responses": {"201": {"description": "Created"}},
                    }
                },
                "/developer/v1/internal": {
                    "get": {
                        "summary": "Not selected",
                        "responses": {"200": {"description": "OK"}},
                    }
                },
            }
        }

        tools = parse_spec_dict(
            spec,
            source="curated",
            category="things",
            path_prefix=None,
            require_alias=True,
            alias_as_name=True,
            synthesize_cli_tools=False,
        )
        tool_map = {tool.name: tool for tool in tools}

        assert set(tool_map) == {"update", "upload-document"}
        update = tool_map["update"]
        assert update.http_method == "patch"
        assert update.required_scopes == ["things:write"]
        assert update.source == "curated"
        assert update.category == "things"
        assert _find_param(update, "thing_id").location == "path"
        assert _find_param(update, "details").location == "body"

        upload = tool_map["upload-document"]
        assert upload.request_content_type == "multipart/form-data"
        assert _find_param(upload, "file").type is ParamType.FILE
        assert _find_param(upload, "file").location == "form"
        assert _find_param(upload, "purpose").location == "form"


class TestScopeExtraction:
    def test_extract_all_scopes_skips_non_cli_tools(self, tmp_path):
        spec = {
            "paths": {
                "/developer/v1/agent-tools/cli-tool": {
                    "post": {
                        "summary": "CLI tool",
                        "x-platforms": ["cli"],
                        "security": [{"oauth2": ["cli:read"]}],
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/CliReq"}
                                }
                            }
                        },
                    }
                },
                "/developer/v1/agent-tools/mcp-tool": {
                    "post": {
                        "summary": "MCP tool",
                        "x-platforms": ["mcp"],
                        "security": [{"oauth2": ["mcp:read"]}],
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/McpReq"}
                                }
                            }
                        },
                    }
                },
                "/developer/v1/agent-tools/legacy-tool": {
                    "post": {
                        "summary": "Legacy tool",
                        "security": [{"oauth2": ["legacy:read"]}],
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/LegacyReq"}
                                }
                            }
                        },
                    }
                },
            },
            "components": {
                "schemas": {
                    "CliReq": {"type": "object", "properties": {}},
                    "McpReq": {"type": "object", "properties": {}},
                    "LegacyReq": {"type": "object", "properties": {}},
                }
            },
        }
        spec_path = tmp_path / "agent-tool.json"
        spec_path.write_text(json.dumps(spec))

        assert extract_all_scopes(spec_path) == ["cli:read", "legacy:read"]


# ── Helpers ──


def _find_param(tool: ToolDef, name: str) -> ToolParam | None:
    for p in tool.params:
        if p.name == name:
            return p
    return None
