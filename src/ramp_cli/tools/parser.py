"""Parse OpenAPI operations exposed to the CLI into ToolDef structures."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

import jsonref

_AGENT_TOOLS_PREFIX = "/developer/v1/agent-tools/"
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})
_CLI_PLATFORM = "cli"


class ParamType(StrEnum):
    """Determines how a parameter is represented as a CLI flag.

    STRING/INT/BOOL  → simple Click option
    ENUM             → click.Choice with allowed values
    ENUM_ARRAY       → comma-separated string of allowed values
    ARRAY            → JSON string (simple items) or --json (complex items)
    OBJECT           → always requires --json escape hatch
    """

    STRING = "string"
    INT = "int"
    BOOL = "bool"
    ENUM = "enum"
    ENUM_ARRAY = "enum_array"
    ARRAY = "array"
    OBJECT = "object"
    FILE = "file"


# Maps OpenAPI type strings to ParamType for simple scalar properties.
_SIMPLE_TYPE_MAP: dict[str, ParamType] = {
    "string": ParamType.STRING,
    "integer": ParamType.INT,
    "number": ParamType.INT,
    "boolean": ParamType.BOOL,
}


@dataclass(slots=True)
class ToolParam:
    """A single parameter for an agent tool command."""

    name: str
    flag: str  # CLI flag name — matches the API property name (snake_case)
    description: str
    type: ParamType
    required: bool = False
    default: Any = None
    enum_values: list[str] | None = None
    is_complex: bool = False  # True when the param needs --json rather than a flag
    location: str = "body"  # path, query, body, or form


@dataclass(slots=True)
class JsonSchema:
    """Small schema model used to validate raw --json bodies."""

    properties: dict[str, JsonSchema] = field(default_factory=dict)
    enum_values: list[str] | None = None
    array_item: JsonSchema | None = None
    additional_properties_allowed: bool = True
    nullable: bool = False


@dataclass(slots=True)
class ToolDef:
    """An agent tool parsed from the OpenAPI spec."""

    name: str  # kebab-case endpoint name, e.g. "get-funds"
    path: str  # full API path, e.g. "/developer/v1/agent-tools/get-funds"
    http_method: str
    summary: str  # one-line summary from OpenAPI
    description: str  # full description from the request body schema
    category: str = ""  # from the second tag in the spec (set by core)
    alias: str = ""  # human-friendly CLI name from x-alias (e.g. "list")
    params: list[ToolParam] = field(default_factory=list)
    required_scopes: list[str] = field(default_factory=list)
    request_schema_name: str = ""
    response_schema_name: str = ""
    json_schema: JsonSchema | None = None
    request_content_type: str = "application/json"
    source: str = "agent-tools"

    @property
    def display_name(self) -> str:
        """Human-friendly command name, e.g. 'transactions list'."""
        if self.category and self.alias:
            return f"{self.category} {self.alias}"
        return self.alias or self.name


def _extract_category(tags: list[str]) -> str:
    """Extract the CLI category from the final operation tag."""
    return tags[-1] if tags else ""


def _operation_name(path: str, method_def: dict) -> str:
    """Return a stable internal name while preserving legacy tool identities."""
    if path.startswith(_AGENT_TOOLS_PREFIX):
        return path.rsplit("/", 1)[-1]
    return method_def.get("operationId") or path.rsplit("/", 1)[-1]


def parse_spec(
    spec_path: Path,
    *,
    source: str = "agent-tools",
    category: str = "",
    path_prefix: str | None = None,
    require_alias: bool = False,
    alias_as_name: bool = False,
    synthesize_cli_tools: bool = True,
) -> list[ToolDef]:
    """Parse selected OpenAPI operations into sorted ToolDefs."""
    with open(spec_path) as f:
        return parse_spec_dict(
            json.load(f),
            source=source,
            category=category,
            path_prefix=path_prefix,
            require_alias=require_alias,
            alias_as_name=alias_as_name,
            synthesize_cli_tools=synthesize_cli_tools,
        )


def load_component_schema(spec_path: Path, schema_name: str) -> dict[str, Any]:
    """Load a fully dereferenced component schema from an OpenAPI document."""
    with open(spec_path) as f:
        spec = jsonref.replace_refs(json.load(f), proxies=False)

    schemas = spec.get("components", {}).get("schemas", {})
    try:
        return schemas[schema_name]
    except KeyError as exc:
        raise KeyError(f"Unknown OpenAPI schema component '{schema_name}'.") from exc


def parse_spec_dict(
    spec: dict,
    *,
    source: str = "agent-tools",
    category: str = "",
    path_prefix: str | None = None,
    require_alias: bool = False,
    alias_as_name: bool = False,
    synthesize_cli_tools: bool = True,
) -> list[ToolDef]:
    """Parse selected OpenAPI operations into sorted ToolDefs."""
    schemas = spec.get("components", {}).get("schemas", {})
    tools: list[ToolDef] = []

    for path, path_def in spec.get("paths", {}).items():
        if path_prefix is not None and not path.startswith(path_prefix):
            continue
        path_params = path_def.get("parameters", [])
        for method, method_def in path_def.items():
            # Skip OpenAPI extension keys like "x-source-details"
            if method.startswith("x-") or method not in _HTTP_METHODS:
                continue
            if not isinstance(method_def, dict) or not _supports_cli(method_def):
                continue
            alias = method_def.get("x-alias")
            if require_alias and not alias:
                continue
            tool = _parse_endpoint(
                path,
                method,
                method_def,
                schemas,
                path_params=path_params,
                name=alias if alias_as_name else None,
                category=category or None,
                source=source,
            )
            if tool is not None:
                tools.append(tool)

    if synthesize_cli_tools:
        tools.extend(_synthesize_cli_tools(tools))

    return sorted(tools, key=lambda t: t.name)


def _synthesize_cli_tools(tools: list[ToolDef]) -> list[ToolDef]:
    """Add small CLI-only wrappers for existing tools when UX needs differ."""
    # If the spec already has a real list-bills endpoint, skip the synthetic
    # wrapper — it was only needed before core provided its own tool.
    if any(tool.name == "list-bills" for tool in tools):
        return []

    search_bills = next((tool for tool in tools if tool.name == "search-bills"), None)
    if search_bills is None:
        return []

    return [
        replace(
            search_bills,
            name="list-bills",
            summary="List bills without requiring a search query",
            description=(
                "List bills with optional filters. If no query is provided, "
                "returns the default bill enumeration."
            ),
            alias="list",
            params=[
                replace(
                    param,
                    description="Optional bill search query. Leave unset to list bills.",
                    required=True,
                    default="",
                )
                if param.name == "query"
                else param
                for param in search_bills.params
            ],
        )
    ]


def _parse_endpoint(
    path: str,
    method: str,
    method_def: dict,
    schemas: dict,
    *,
    path_params: list[dict] | None = None,
    name: str | None = None,
    category: str | None = None,
    source: str = "agent-tools",
) -> ToolDef | None:
    summary = method_def.get("summary", "")
    tool_name = name or _operation_name(path, method_def)
    response_ref = _response_ref(method_def)
    all_parameters = [*(path_params or []), *method_def.get("parameters", [])]
    operation_params = _parse_operation_params(all_parameters, schemas)

    request_content_type, request_schema = _request_schema(method_def)
    request_ref = request_schema.get("$ref", "") if request_schema else ""
    if request_ref:
        schema_name = request_ref.split("/")[-1]
        schema_def = schemas.get(schema_name, {})
        body_location = (
            "form" if request_content_type == "multipart/form-data" else "body"
        )
        body_params = _parse_params(schema_def, schemas, location=body_location)
        return ToolDef(
            name=tool_name,
            path=path,
            http_method=method,
            summary=summary,
            description=schema_def.get("description", summary),
            category=category or _extract_category(method_def.get("tags", [])),
            alias=method_def.get("x-alias", ""),
            params=_sort_params([*operation_params, *body_params]),
            required_scopes=_extract_scopes(method_def),
            request_schema_name=schema_name,
            response_schema_name=response_ref.split("/")[-1] if response_ref else "",
            json_schema=_parse_input_json_schema(
                schema_def,
                all_parameters,
                schemas,
            ),
            request_content_type=request_content_type,
            source=source,
        )

    if request_schema:
        body_location = (
            "form" if request_content_type == "multipart/form-data" else "body"
        )
        body_params = _parse_params(request_schema, schemas, location=body_location)
        return ToolDef(
            name=tool_name,
            path=path,
            http_method=method,
            summary=summary,
            description=request_schema.get("description", summary),
            category=category or _extract_category(method_def.get("tags", [])),
            alias=method_def.get("x-alias", ""),
            params=_sort_params([*operation_params, *body_params]),
            required_scopes=_extract_scopes(method_def),
            request_schema_name="",
            response_schema_name=response_ref.split("/")[-1] if response_ref else "",
            json_schema=_parse_input_json_schema(
                request_schema,
                all_parameters,
                schemas,
            ),
            request_content_type=request_content_type,
            source=source,
        )

    return ToolDef(
        name=tool_name,
        path=path,
        http_method=method,
        summary=summary,
        description=method_def.get("description", summary),
        category=category or _extract_category(method_def.get("tags", [])),
        alias=method_def.get("x-alias", ""),
        params=_sort_params(operation_params),
        required_scopes=_extract_scopes(method_def),
        response_schema_name=response_ref.split("/")[-1] if response_ref else "",
        json_schema=_parse_operation_json_schema(all_parameters, schemas),
        source=source,
    )


def _request_schema(method_def: dict) -> tuple[str, dict]:
    content = method_def.get("requestBody", {}).get("content", {})
    for content_type in ("application/json", "multipart/form-data"):
        schema = content.get(content_type, {}).get("schema")
        if isinstance(schema, dict):
            return content_type, schema
    return "application/json", {}


def _response_ref(method_def: dict) -> str:
    for status, response in method_def.get("responses", {}).items():
        if not str(status).startswith("2"):
            continue
        ref = _deep_get(response, "content", "application/json", "schema", "$ref")
        if ref:
            return ref
    return ""


def _extract_scopes(method_def: dict) -> list[str]:
    scopes: list[str] = []
    for sec_req in method_def.get("security", []):
        if "oauth2" in sec_req:
            scopes.extend(sec_req["oauth2"])
    return scopes


def _supports_cli(method_def: dict) -> bool:
    """Return True when the endpoint is exposed to the CLI platform.

    `x-platforms` is optional in older specs, so missing metadata defaults to
    visible for backwards compatibility.
    """

    platforms = method_def.get("x-platforms")
    if platforms is None:
        return True
    if isinstance(platforms, str):
        return platforms == _CLI_PLATFORM
    return _CLI_PLATFORM in platforms


def _parse_params(
    schema_def: dict, schemas: dict, *, location: str = "body"
) -> list[ToolParam]:
    """Convert schema properties into a sorted list of ToolParams (required first)."""
    required_names = set(schema_def.get("required", []))
    params: list[ToolParam] = []

    for name, prop in schema_def.get("properties", {}).items():
        param = _classify_property(name, prop, schemas)
        param.required = name in required_names
        param.location = location
        params.append(param)

    return _sort_params(params)


def _parse_operation_params(parameters: list[dict], schemas: dict) -> list[ToolParam]:
    """Convert OpenAPI path and query parameters into ToolParams."""
    params: list[ToolParam] = []
    for p in parameters:
        location = p.get("in")
        if location not in {"path", "query"}:
            continue
        name = p["name"]
        schema = p.get("schema", {})
        param = _classify_property(name, schema, schemas)
        param.required = p.get("required", False)
        param.location = location
        param.description = p.get("description", "") or param.description
        params.append(param)
    return _sort_params(params)


def _sort_params(params: list[ToolParam]) -> list[ToolParam]:
    return sorted(params, key=lambda p: (not p.required, p.name))


def _parse_input_json_schema(
    request_schema: dict,
    parameters: list[dict],
    schemas: dict,
) -> JsonSchema:
    """Build the raw --json schema across body, path, and query inputs."""
    result = _parse_json_schema(request_schema, schemas)
    operation_schema = _parse_operation_json_schema(parameters, schemas)
    result.properties.update(operation_schema.properties)
    return result


def _parse_operation_json_schema(
    parameters: list[dict],
    schemas: dict,
) -> JsonSchema:
    """Build an object schema for path and query operation parameters."""
    properties: dict[str, JsonSchema] = {}
    for p in parameters:
        if p.get("in") not in {"path", "query"}:
            continue
        properties[p["name"]] = _parse_json_schema(p.get("schema", {}), schemas)
    return JsonSchema(properties=properties)


def _parse_json_schema(
    schema: dict, schemas: dict, seen_refs: frozenset[str] = frozenset()
) -> JsonSchema:
    """Extract object property and enum metadata from an OpenAPI schema."""
    result = JsonSchema(nullable=schema.get("nullable") is True)

    if not isinstance(schema, dict):
        return JsonSchema()

    if "$ref" in schema:
        ref_name = schema["$ref"].split("/")[-1]
        if ref_name in seen_refs:
            return result
        ref_schema = _parse_json_schema(
            schemas.get(ref_name, {}), schemas, seen_refs | frozenset({ref_name})
        )
        ref_schema.nullable = ref_schema.nullable or result.nullable
        return ref_schema

    for sub_schema in schema.get("allOf", []):
        _merge_json_schema(result, _parse_json_schema(sub_schema, schemas, seen_refs))

    if "enum" in schema:
        result.enum_values = schema["enum"]

    result.additional_properties_allowed = (
        result.additional_properties_allowed
        and schema.get("additionalProperties") is not False
    )

    if schema.get("type") == "array":
        result.array_item = _parse_json_schema(schema.get("items", {}), schemas)

    for name, prop in schema.get("properties", {}).items():
        result.properties[name] = _parse_json_schema(prop, schemas, seen_refs)

    return result


def _merge_json_schema(target: JsonSchema, source: JsonSchema) -> None:
    """Merge allOf fragments into a single shallow validation schema."""
    target.properties.update(source.properties)
    target.additional_properties_allowed = (
        target.additional_properties_allowed and source.additional_properties_allowed
    )
    if target.enum_values is None:
        target.enum_values = source.enum_values
    if target.array_item is None:
        target.array_item = source.array_item
    target.nullable = target.nullable or source.nullable


def _classify_property(name: str, prop: dict, schemas: dict) -> ToolParam:
    """Classify a schema property into a ParamType.

    OpenAPI schemas use several patterns to represent types:
      - Simple types: {"type": "string"} → ParamType.STRING
      - Enums via allOf: {"allOf": [{"$ref": "..."}]} where ref has "enum" → ParamType.ENUM
      - Nested objects via allOf: same pattern but ref has "properties" → ParamType.OBJECT
      - Arrays: {"type": "array", "items": {...}} → depends on item type
    """
    desc = prop.get("description", "") or prop.get("title", "")
    default = prop.get("default")

    if prop.get("type") == "string" and prop.get("format") == "binary":
        return ToolParam(
            name=name,
            flag=name,
            description=desc,
            type=ParamType.FILE,
            default=default,
        )

    if "allOf" in prop:
        for sub in prop["allOf"]:
            if "$ref" in sub:
                return _resolve_ref(name, sub["$ref"], schemas, desc, default)
        return ToolParam(
            name=name,
            flag=name,
            description=desc,
            type=ParamType.OBJECT,
            default=default,
            is_complex=True,
        )

    if "$ref" in prop:
        return _resolve_ref(name, prop["$ref"], schemas, desc, default)

    if prop.get("type") == "array":
        return _classify_array(name, prop.get("items", {}), schemas, desc, default)

    # Inline enums (enum values directly on the property, not via $ref)
    if "enum" in prop:
        return ToolParam(
            name=name,
            flag=name,
            description=desc,
            type=ParamType.ENUM,
            default=default,
            enum_values=prop["enum"],
        )

    return ToolParam(
        name=name,
        flag=name,
        description=desc,
        type=_SIMPLE_TYPE_MAP.get(prop.get("type", "string"), ParamType.STRING),
        default=default,
    )


def _resolve_ref(
    name: str, ref: str, schemas: dict, desc: str, default: Any
) -> ToolParam:
    """Resolve a $ref to either an enum param or a complex object param."""
    ref_schema = schemas.get(ref.split("/")[-1], {})

    if ref_schema.get("type") == "string" and ref_schema.get("format") == "binary":
        return ToolParam(
            name=name,
            flag=name,
            description=desc,
            type=ParamType.FILE,
            default=default,
        )

    if "enum" in ref_schema:
        return ToolParam(
            name=name,
            flag=name,
            description=desc,
            type=ParamType.ENUM,
            default=default,
            enum_values=ref_schema["enum"],
        )

    if ref_schema.get("type") in _SIMPLE_TYPE_MAP:
        return ToolParam(
            name=name,
            flag=name,
            description=desc,
            type=_SIMPLE_TYPE_MAP[ref_schema["type"]],
            default=default,
        )

    return ToolParam(
        name=name,
        flag=name,
        description=desc,
        type=ParamType.OBJECT,
        default=default,
        is_complex=True,
    )


def _classify_array(
    name: str, items: dict, schemas: dict, desc: str, default: Any
) -> ToolParam:
    """Classify an array property by its item type."""
    if "$ref" in items:
        ref_schema = schemas.get(items["$ref"].split("/")[-1], {})

        if "enum" in ref_schema:
            return ToolParam(
                name=name,
                flag=name,
                description=desc,
                type=ParamType.ENUM_ARRAY,
                default=default,
                enum_values=ref_schema["enum"],
            )

        if ref_schema.get("properties") or ref_schema.get("type") == "object":
            return ToolParam(
                name=name,
                flag=name,
                description=desc,
                type=ParamType.ARRAY,
                default=default,
                is_complex=True,
            )

    is_simple = items.get("type") in ("string", "integer", "boolean")
    return ToolParam(
        name=name,
        flag=name,
        description=desc,
        type=ParamType.ARRAY,
        default=default,
        is_complex=not is_simple,
    )


def extract_all_scopes(spec_path: Path) -> list[str]:
    """Extract scopes from all CLI-enabled operations in an OpenAPI spec."""
    with open(spec_path) as f:
        spec = json.load(f)

    scopes: set[str] = set()
    for path, path_def in spec.get("paths", {}).items():
        for method, method_def in path_def.items():
            if method.startswith("x-") or method not in _HTTP_METHODS:
                continue
            if not isinstance(method_def, dict):
                continue
            if not _supports_cli(method_def):
                continue
            scopes.update(_extract_scopes(method_def))

    return sorted(scopes)


def _deep_get(d: dict, *keys: str) -> str:
    """Walk nested dicts by key sequence, returning '' if any key is missing."""
    for key in keys:
        if not isinstance(d, dict):
            return ""
        d = d.get(key, {})
    return d if isinstance(d, str) else ""
