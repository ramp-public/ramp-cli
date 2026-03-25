"""Parse the agent-tool OpenAPI spec into ToolDef structures.

The spec contains all
agent-tool endpoints under /developer/v1/agent-tools/. Each endpoint
has a Pydantic-derived request body schema that we convert into typed
ToolParam definitions for CLI flag generation.
"""

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

_AGENT_TOOLS_PREFIX = "/developer/v1/agent-tools/"
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})


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

    @property
    def display_name(self) -> str:
        """Human-friendly command name, e.g. 'transactions list'."""
        if self.category and self.alias:
            return f"{self.category} {self.alias}"
        return self.alias or self.name


def _extract_category(tags: list[str]) -> str:
    """Extract category from spec tags. Core provides it as the second tag."""
    for tag in tags:
        if tag != "Agent Tool":
            return tag
    return ""


def parse_spec(spec_path: Path) -> list[ToolDef]:
    """Parse an agent-tool OpenAPI spec file into a sorted list of ToolDefs."""
    with open(spec_path) as f:
        return parse_spec_dict(json.load(f))


def parse_spec_dict(spec: dict) -> list[ToolDef]:
    """Parse an agent-tool OpenAPI spec dict into a sorted list of ToolDefs."""
    schemas = spec.get("components", {}).get("schemas", {})
    tools: list[ToolDef] = []

    for path, path_def in spec.get("paths", {}).items():
        if not path.startswith(_AGENT_TOOLS_PREFIX):
            continue
        for method, method_def in path_def.items():
            # Skip OpenAPI extension keys like "x-source-details"
            if method.startswith("x-") or method not in _HTTP_METHODS:
                continue
            tool = _parse_endpoint(path, method, method_def, schemas)
            if tool is not None:
                tools.append(tool)

    return sorted(tools, key=lambda t: t.name)


def _parse_endpoint(
    path: str, method: str, method_def: dict, schemas: dict
) -> ToolDef | None:
    request_ref = _deep_get(
        method_def, "requestBody", "content", "application/json", "schema", "$ref"
    )
    if not request_ref:
        return None

    schema_name = request_ref.split("/")[-1]
    schema_def = schemas.get(schema_name, {})
    summary = method_def.get("summary", "")
    response_ref = _deep_get(
        method_def, "responses", "200", "content", "application/json", "schema", "$ref"
    )

    return ToolDef(
        name=path.split("/")[-1],
        path=path,
        http_method=method,
        summary=summary,
        description=schema_def.get("description", summary),
        category=_extract_category(method_def.get("tags", [])),
        alias=method_def.get("x-alias", ""),
        params=_parse_params(schema_def, schemas),
        required_scopes=_extract_scopes(method_def),
        request_schema_name=schema_name,
        response_schema_name=response_ref.split("/")[-1] if response_ref else "",
    )


def _extract_scopes(method_def: dict) -> list[str]:
    scopes: list[str] = []
    for sec_req in method_def.get("security", []):
        if "oauth2" in sec_req:
            scopes.extend(sec_req["oauth2"])
    return scopes


def _parse_params(schema_def: dict, schemas: dict) -> list[ToolParam]:
    """Convert schema properties into a sorted list of ToolParams (required first)."""
    required_names = set(schema_def.get("required", []))
    params: list[ToolParam] = []

    for name, prop in schema_def.get("properties", {}).items():
        param = _classify_property(name, prop, schemas)
        param.required = name in required_names
        params.append(param)

    return sorted(params, key=lambda p: (not p.required, p.name))


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

    if "enum" in ref_schema:
        return ToolParam(
            name=name,
            flag=name,
            description=desc,
            type=ParamType.ENUM,
            default=default,
            enum_values=ref_schema["enum"],
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
    """Extract all unique OAuth scopes required by agent tools in the spec."""
    with open(spec_path) as f:
        spec = json.load(f)

    scopes: set[str] = set()
    for path, path_def in spec.get("paths", {}).items():
        if not path.startswith(_AGENT_TOOLS_PREFIX):
            continue
        for method, method_def in path_def.items():
            if method.startswith("x-") or method not in _HTTP_METHODS:
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
