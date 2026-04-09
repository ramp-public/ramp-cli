"""Build Click commands from ToolDef structures.

Each ToolDef is converted into a Click command that calls the
corresponding agent-tool endpoint. Simple params become typed CLI
flags; complex nested params require the --json escape hatch.
"""

import json
import sys
import threading
import time
from typing import Any

import click

from ramp_cli.auth.store import get_granted_scopes
from ramp_cli.client.api import RampClient
from ramp_cli.config.constants import base_url
from ramp_cli.output.formatter import (
    extract_headers,
    format_value,
    is_quiet,
    print_agent_json,
    print_json,
    print_table,
    resolve_format,
)
from ramp_cli.output.paginator import ToolPaginator
from ramp_cli.output.style import show_detail_card, show_table_card
from ramp_cli.specs.sync import maybe_sync
from ramp_cli.tools.parser import ParamType, ToolDef, ToolParam

_SPINNER_CHARS = "░▒▓█▓▒"

_ID_SUFFIXES = ("_id", "_uuid")


def is_id_param(param: ToolParam) -> bool:
    """True if this param is a resource identifier that should be positional."""
    if not param.required or param.is_complex or param.type is not ParamType.STRING:
        return False
    name = param.name
    return name == "id" or any(name.endswith(s) for s in _ID_SUFFIXES)


def _build_argument(param: ToolParam) -> click.Argument:
    """Convert an ID ToolParam into a positional Click Argument.

    Arguments are optional at the Click level so that --json can bypass
    them. Validation happens in _build_body when --json is not used.
    """
    return click.Argument(
        [param.flag.replace("-", "_")],
        required=False,
        default=None,
        type=str,
    )


def build_tool_command(tool: ToolDef) -> click.Command:
    """Convert a ToolDef into a Click command."""
    params: list[click.Parameter] = []

    # Positional arguments come first (required ID params)
    for p in tool.params:
        if not p.is_complex and is_id_param(p):
            params.append(_build_argument(p))

    # Then options (everything else)
    for p in tool.params:
        if not p.is_complex and not is_id_param(p):
            params.append(_build_option(p))
    params.append(
        click.Option(
            ["--json", "json_body"],
            default=None,
            help="Raw JSON request body (bypasses flag validation)",
        )
    )
    params.append(
        click.Option(
            ["--dry_run", "-n"],
            is_flag=True,
            default=False,
            help="Print request without sending",
        )
    )

    @click.pass_context
    def callback(ctx: click.Context, **kwargs: Any) -> None:
        _execute_tool(ctx, tool, kwargs)

    help_text = tool.summary
    if any(p.is_complex for p in tool.params):
        help_text += " (use --json for complex fields)"

    return click.Command(
        name=tool.name,
        callback=callback,
        help=help_text,
        short_help=tool.summary,
        params=params,
    )


def _build_option(param: ToolParam) -> click.Option:
    """Convert a ToolParam into a Click Option based on its ParamType."""
    flag = f"--{param.flag}"
    kwargs: dict[str, Any] = {"help": param.description}
    decls = [flag]

    match param.type:
        case ParamType.BOOL:
            # Tri-state bools: unset (omit), true, or false.
            decls = [f"{flag}/--no-{param.flag}"]
            kwargs.update(default=None, show_default=False)
        case ParamType.INT:
            kwargs.update(type=int, default=None)
        case ParamType.ENUM:
            values_hint = ", ".join(param.enum_values)
            kwargs.update(
                type=str,
                default=None,
                help=f"{param.description} (values: {values_hint})",
            )
        case ParamType.ENUM_ARRAY:
            hint = (
                f" (comma-separated: {','.join(param.enum_values[:3])}...)"
                if (param.enum_values and len(param.enum_values) > 3)
                else ""
            )
            kwargs.update(default=None, help=f"{param.description}{hint}")
        case ParamType.ARRAY:
            kwargs.update(default=None, help=f"{param.description} (JSON array string)")
        case _:
            kwargs.update(type=str, default=None)

    # Click derives kwarg names from flags; add a secondary name if they differ
    kwarg_name = param.flag.replace("-", "_")
    if kwarg_name != param.flag:
        decls.append(kwarg_name)

    return click.Option(decls, **kwargs)


def _execute_tool(ctx: click.Context, tool: ToolDef, kwargs: dict[str, Any]) -> None:
    """Build the JSON body, optionally dry-run, then call the agent-tool endpoint."""
    env: str = ctx.obj["env"]
    fmt: str | None = ctx.obj["format"]
    config_format: str = ctx.obj["config_format"]
    json_body_raw: str | None = kwargs.get("json_body")
    dry_run: bool = kwargs.get("dry_run", False)

    if json_body_raw:
        try:
            body = json.loads(json_body_raw)
        except json.JSONDecodeError as e:
            raise click.BadParameter(f"invalid JSON: {e}", param_hint="'--json'")
    else:
        body = _build_body(tool, kwargs)

    is_get = tool.http_method == "get"
    method_label = "GET" if is_get else "POST"

    if dry_run:
        resolved = resolve_format(fmt, config_format)
        if resolved == "json":
            print_agent_json(
                {
                    "dry_run": True,
                    "method": method_label,
                    "url": f"{base_url(env)}{tool.path}",
                    "body": body,
                },
                pagination=None,
            )
        else:
            click.echo(f"DRY RUN: {method_label} {base_url(env)}{tool.path}", err=True)
            print_json(body)
        return

    # Pre-flight scope check: fail fast with a clear message instead of
    # sending a doomed request that returns an opaque 403.
    if tool.required_scopes:
        granted = get_granted_scopes(env)
        if granted:  # only check if we have scope info persisted
            missing = set(tool.required_scopes) - granted
            if missing:
                missing_str = ", ".join(sorted(missing))
                raise click.ClickException(
                    f"Your token is missing the required scope: {missing_str}\n\n"
                    f"  To fix this, log in again to get a fresh token:\n\n"
                    f"    ramp auth login\n\n"
                    f"  This will request all the scopes needed for your tools."
                )

    # Refresh cached spec in the background for the *next* invocation.
    # The current command is already resolved, so this only updates the cache.
    maybe_sync(env)

    resolved = resolve_format(fmt, config_format)
    is_human = resolved != "json" and sys.stdout.isatty() and not is_quiet()

    stop_spinner = _start_spinner(tool.display_name) if is_human else None
    t0 = time.monotonic()

    client = RampClient(env)
    if is_get:
        params = {k: str(v) for k, v in body.items() if v is not None}
        resp_bytes = client.get(tool.path, params)
    else:
        resp_bytes = client.post(tool.path, json.dumps(body).encode())
    data = json.loads(resp_bytes)

    elapsed = time.monotonic() - t0
    if stop_spinner:
        stop_spinner()

    if resolved == "json":
        print_agent_json(data, pagination=_extract_pagination(data))
    else:
        wide: bool = ctx.obj.get("wide", False)
        _print_summary(tool.display_name, data, elapsed)

        no_input: bool = ctx.obj.get("no_input", False)
        if is_human and sys.stdin.isatty() and not no_input:
            if _try_interactive_table(tool, data, body, client, wide=wide):
                return

        _render_human(data, wide=wide, category=tool.category)


def _build_body(tool: ToolDef, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Assemble a JSON request body from Click flag values.

    Skips complex params (they must be provided via --json) and unset
    optional params. Bools are only included when explicitly set
    so the API receives its own defaults for unset flags.
    """
    body: dict[str, Any] = {}
    missing: list[str] = []

    for param in tool.params:
        if param.is_complex:
            if param.required:
                missing.append(f"--json ({param.name})")
            continue

        val = kwargs.get(param.flag.replace("-", "_"))

        if param.type is ParamType.BOOL:
            if val is None:
                if param.required:
                    missing.append(f"--{param.flag}/--no-{param.flag}")
                continue
            body[param.name] = bool(val)
            continue

        if val is None:
            if param.required and param.default is not None:
                body[param.name] = param.default
                continue
            if param.required:
                label = param.flag.upper() if is_id_param(param) else f"--{param.flag}"
                missing.append(label)
            continue

        if param.type is ParamType.ENUM and param.enum_values:
            allowed = {v.lower() for v in param.enum_values}
            if isinstance(val, str) and val.lower() not in allowed:
                raise click.BadParameter(
                    f"invalid value '{val}'. Choose from: {', '.join(param.enum_values)}",
                    param_hint=f"'--{param.flag}'",
                )

        if param.type is ParamType.ENUM_ARRAY and isinstance(val, str):
            body[param.name] = [v.strip() for v in val.split(",")]
        elif param.type is ParamType.ARRAY and isinstance(val, str):
            try:
                body[param.name] = json.loads(val)
            except json.JSONDecodeError:
                raise click.BadParameter(
                    f"invalid JSON array for --{param.flag}",
                    param_hint=f"'--{param.flag}'",
                )
        else:
            body[param.name] = val

    if missing:
        example_parts = [f"ramp {tool.display_name}"]
        # Positional args first, then options
        for param in tool.params:
            if param.required and not param.is_complex and is_id_param(param):
                example_parts.append(f"<{param.flag}>")
        for param in tool.params:
            if param.required and not param.is_complex and not is_id_param(param):
                if param.type is ParamType.ENUM and param.enum_values:
                    example_parts.append(f"--{param.flag} {param.enum_values[0]}")
                else:
                    example_parts.append(f"--{param.flag} <value>")
        example = " ".join(example_parts)
        raise click.UsageError(
            f"Missing required flags: {', '.join(missing)}\n\n  Example: {example}"
        )

    return body


def _start_spinner(tool_name: str):
    """Show a lightweight spinner on stderr while the API call runs."""
    stop = threading.Event()

    def _spin() -> None:
        i = 0
        while not stop.is_set():
            char = _SPINNER_CHARS[i % len(_SPINNER_CHARS)]
            click.echo(f"\r  {char} {tool_name}...", nl=False, err=True)
            i += 1
            stop.wait(0.12)
        click.echo("\r" + " " * (len(tool_name) + 10) + "\r", nl=False, err=True)

    t = threading.Thread(target=_spin, daemon=True)
    t.start()

    def _stop() -> None:
        stop.set()
        t.join(timeout=1)

    return _stop


def _print_summary(tool_name: str, data: Any, elapsed: float) -> None:
    """Print a one-line summary of the tool response above the data."""
    parts = [f"  \u2713 {tool_name}"]

    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                parts.append(f"{len(v)} results")
                break
        total = data.get("total_count")
        if total is not None and total != len(
            next((v for v in data.values() if isinstance(v, list)), [])
        ):
            parts.append(f"{total} total")

    parts.append(f"{elapsed:.1f}s")
    click.echo(f"{parts[0]} — {', '.join(parts[1:])}")
    click.echo()


def _try_interactive_table(
    tool: ToolDef,
    data: Any,
    body: dict,
    client: "RampClient",
    wide: bool = False,
) -> bool:
    """Try to launch the interactive paginated table for list results.

    Returns True if the interactive table was shown, False if the data
    doesn't contain a list (caller should fall through to _render_human).
    """
    _key, list_items = _extract_list_field(data)
    if not list_items:
        return False

    next_cursor = data.get("next_page_cursor") if isinstance(data, dict) else None
    cursor_param = _detect_cursor_param(tool)
    headers = extract_headers(list_items[0], wide=wide, category=tool.category)

    def fetch_next(cursor: str) -> tuple[list[dict[str, str]], str | None]:
        fetch_body = dict(body)
        fetch_body[cursor_param] = cursor
        resp = client.post(tool.path, json.dumps(fetch_body).encode())
        page_data = json.loads(resp)
        _k, items = _extract_list_field(page_data)
        return _format_rows(items, headers, wide=wide), page_data.get(
            "next_page_cursor"
        )

    selected = ToolPaginator(
        title=tool.display_name,
        headers=headers,
        initial_rows=_format_rows(list_items, headers, wide=wide),
        next_cursor=next_cursor,
        fetch_next_page=fetch_next,
    ).run()
    if selected:
        show_detail_card("Selected", selected)
    return True


_CURSOR_PARAM_NAMES = ("next_page_cursor", "page_cursor", "cursor", "start")


def _detect_cursor_param(tool: ToolDef) -> str:
    """Detect which input param name this tool uses for cursor pagination."""
    param_names = {p.name for p in tool.params}
    for name in _CURSOR_PARAM_NAMES:
        if name in param_names:
            return name
    return "next_page_cursor"


_PAGINATION_KEYS = ("next_page_cursor", "page_cursor", "cursor", "next")


def _extract_pagination(data: Any) -> dict | None:
    """Extract pagination info from an API response for the agent JSON envelope.

    Looks for common cursor fields in the top-level response dict.
    Returns a dict like {"next_page_cursor": "tok_abc"} or None if no cursor.
    """
    if not isinstance(data, dict):
        return None
    for key in _PAGINATION_KEYS:
        value = data.get(key)
        if value is not None:
            return {"next_cursor": value}
    return None


def _extract_list_field(data: Any) -> tuple[str | None, list[dict]]:
    """Extract the primary list field name and items from a tool response.

    Returns (key_name, items) or (None, []) if no list field is found.
    Used by both the paginator path and the static table renderer.
    """
    if not isinstance(data, dict):
        return None, []
    for k, v in data.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return k, v
    return None, []


def _format_rows(
    items: list[dict], headers: list[str], wide: bool = False
) -> list[dict[str, str]]:
    """Format raw response items into display-ready row dicts."""
    return [
        {h: format_value(item.get(h), wide=wide) for h in headers} for item in items
    ]


def _render_human(data: Any, wide: bool = False, category: str | None = None) -> None:
    """Render a tool response for terminal display.

    Most agent tool responses contain a top-level list field (e.g. "funds",
    "cards", "bills") alongside metadata like "total_count". When we detect
    this pattern, render the list as a table and show metadata separately.
    """
    list_key, list_items = _extract_list_field(data)

    if list_key and list_items:
        # Show metadata fields (total_count, summary, etc.) above the table
        meta = {
            k: v for k, v in data.items() if k != list_key and not isinstance(v, list)
        }
        if meta:
            for k, v in meta.items():
                if v is not None:
                    click.echo(f"{k}: {format_value(v, wide=wide)}")
            click.echo()

        headers = extract_headers(list_items[0], wide=wide, category=category)
        rows = _format_rows(list_items, headers, wide=wide)

        if sys.stdout.isatty():
            show_table_card(f"{list_key.title()} [{len(list_items)}]", headers, rows)
        else:
            print_table(headers, rows)
    elif isinstance(data, dict) and sys.stdout.isatty():
        show_detail_card("Result", data)
    elif isinstance(data, dict):
        for k, v in data.items():
            click.echo(f"{k + ':':<25s} {format_value(v, wide=wide)}")
    else:
        click.echo(json.dumps(data, indent=2))
