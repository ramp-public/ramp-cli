"""Build Click commands from ToolDef structures.

Each ToolDef is converted into a Click command that calls the
corresponding agent-tool endpoint. Simple params become typed CLI
flags; complex nested params require the --json escape hatch.
"""

import json
import mimetypes
import os
import sys
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse
from uuid import uuid4

import click

from ramp_cli.client.api import RampClient
from ramp_cli.client.session import get_session_id
from ramp_cli.client.vault import vault_proxy_headers, vault_proxy_target
from ramp_cli.config.constants import api_url, append_query_params
from ramp_cli.config.settings import configured_scopes, resolve_environment
from ramp_cli.errors import ApiError
from ramp_cli.onboarding import maybe_show_category_tip
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
from ramp_cli.tools.availability import fetch_availability
from ramp_cli.tools.parser import JsonSchema, ParamType, ToolDef, ToolParam
from ramp_cli.tools.registry import (
    CATEGORY_ALIAS_GROUPS,
    CATEGORY_REMAP,
    get_tool,
    list_tool_defs,
)

_SPINNER_CHARS = "░▒▓█▓▒"

_ID_SUFFIXES = ("_id", "_uuid")
_IDEMPOTENCY_HEADER = "X-Idempotency-Key"
_TOOL_CALL_MODE_HEADER = "X-Ramp-Agent-Mode"
_HUMAN_RATIONALE = "User initiated this request through the Ramp CLI."
_APPLICATION_PROGRESS_PATH = "/developer/v1/applications/progress"
_VAULT_PROXY_TOOL = "get-agent-card-creds"
_OPTION_SUGGESTION_CUTOFF = 0.6
_OPTION_SUGGESTION_MARGIN = 0.1

CLI_RESOURCE_CATEGORIES = frozenset(
    {
        "accounting",
        "agent",
        "ai-spend",
        "bills",
        "business",
        "cards",
        "funds",
        "procurement_requests",
        "purchase_orders",
        "receipts",
        "reimbursements",
        "requests",
        "tasks",
        "transactions",
        "travel",
        "treasury",
        "users",
        "vendors",
    }
)

_APPLICATION_WAIT_ACTION_ALIASES: dict[str, str] = {
    "idv": "identity_verification",
    "identity": "identity_verification",
    "identity_verification": "identity_verification",
    "complete_identity_verification": "identity_verification",
    "onfido": "identity_verification",
    "phone": "phone_verification",
    "phone_verification": "phone_verification",
    "verify_phone": "phone_verification",
    "verify_email_or_phone": "VERIFY_EMAIL_OR_PHONE",
}

# Common flag aliases for agent/Unix ergonomics.
# Maps API param names to additional CLI flag declarations.
# Agents guess ``--limit`` when the spec says ``page_size`` and vice-versa;
# making both names work removes the need for ``--help`` discovery.
_PARAM_ALIASES: dict[str, list[str]] = {
    "page_size": ["--limit"],
    "limit": ["--page_size"],
}


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    """Transport-ready request derived from a tool and its input values."""

    method: str
    path: str
    query: dict[str, Any]
    body: dict[str, Any]
    form: dict[str, Any]
    files: dict[str, Path]
    headers: dict[str, str]
    content_type: str

    def display_body(self) -> dict[str, Any]:
        if self.content_type != "multipart/form-data":
            return self.body
        return {
            **self.form,
            **{name: str(path) for name, path in self.files.items()},
        }


@dataclass(frozen=True, slots=True)
class WaitConfig:
    actions: tuple[str, ...]
    interval_seconds: int
    timeout_seconds: int


def resolve_tool_command_names(
    group_name: str,
    tools: list[ToolDef],
    granted_scopes: set[str] | None = None,
) -> list[tuple[str, ToolDef]]:
    """Resolve the public command name for each tool in a CLI group."""
    alias_tools: dict[str, list[ToolDef]] = {}
    for tool in tools:
        preferred = tool.alias or tool.name
        alias_tools.setdefault(preferred, []).append(tool)

    resolved: list[tuple[str, ToolDef]] = []
    for tool in tools:
        preferred = tool.alias or tool.name
        peers = alias_tools[preferred]
        if len(peers) == 1:
            command_name = preferred
        else:
            authorized_peers = (
                [
                    peer
                    for peer in peers
                    if not peer.required_scopes
                    or set(peer.required_scopes) <= granted_scopes
                ]
                if granted_scopes
                else []
            )
            native_peers = [peer for peer in peers if peer.category == group_name]
            preferred_peer = (
                authorized_peers[0]
                if len(authorized_peers) == 1
                else native_peers[0]
                if len(native_peers) == 1
                else None
            )
            command_name = preferred if tool is preferred_peer else tool.name
        resolved.append((command_name, tool))
    return resolved


def resolve_cli_tool_groups(tools: list[ToolDef]) -> dict[str, list[ToolDef]]:
    """Group tools under the resource names exposed by the root CLI."""
    source_categories: dict[str, list[ToolDef]] = {}
    for tool in tools:
        source_categories.setdefault(tool.category or "general", []).append(tool)

    merged: dict[str, list[ToolDef]] = {}
    for category, category_tools in source_categories.items():
        target = CATEGORY_REMAP.get(category, category)
        merged.setdefault(target, []).extend(category_tools)

    groups: dict[str, list[ToolDef]] = {}
    general_tools: list[ToolDef] = []
    for category, category_tools in merged.items():
        if len(category_tools) > 1 or category in CLI_RESOURCE_CATEGORIES:
            groups[category] = category_tools
        else:
            general_tools.extend(category_tools)

    for category in CATEGORY_ALIAS_GROUPS:
        if alias_tools := source_categories.get(category):
            groups.setdefault(category, list(alias_tools))

    if general_tools:
        groups.setdefault("general", []).extend(general_tools)
    return dict(sorted(groups.items()))


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


class GeneratedToolCommand(click.Command):
    """A generated command with conservative unknown-option guidance."""

    @staticmethod
    def _normalize_option(option: str) -> str:
        return option.removeprefix("--").replace("-", "_").casefold()

    def _suggest_option(self, ctx: click.Context, unknown: str) -> str | None:
        candidates: dict[str, str] = {}
        for param in self.get_params(ctx):
            if not isinstance(param, click.Option) or param.hidden:
                continue
            for option in (*param.opts, *param.secondary_opts):
                if option.startswith("--"):
                    candidates.setdefault(self._normalize_option(option), option)

        normalized = self._normalize_option(unknown)
        ranked = sorted(
            (
                (SequenceMatcher(None, normalized, candidate).ratio(), option)
                for candidate, option in candidates.items()
            ),
            reverse=True,
        )
        if not ranked or ranked[0][0] < _OPTION_SUGGESTION_CUTOFF:
            return None
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < _OPTION_SUGGESTION_MARGIN:
            return None
        return ranked[0][1]

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        try:
            return super().parse_args(ctx, args)
        except click.NoSuchOption as error:
            message = error.message
            if suggestion := self._suggest_option(ctx, error.option_name):
                message += f" Did you mean '{suggestion}'?"
            message += f"\nRun: {ctx.command_path} --help"
            raise click.NoSuchOption(
                error.option_name,
                message=message,
                ctx=ctx,
            ) from error


def build_tool_command(
    tool: ToolDef,
    *,
    group_name: str | None = None,
    command_name: str | None = None,
) -> click.Command:
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
    has_json_input = bool(
        tool.params or (tool.json_schema and tool.json_schema.properties)
    )
    if has_json_input:
        params.append(
            click.Option(
                ["--json", "json_body"],
                default=None,
                help=_json_option_help(tool, group_name, command_name),
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
    if _supports_application_progress_wait(tool):
        params.extend(_build_application_progress_wait_options())

    @click.pass_context
    def callback(ctx: click.Context, **kwargs: Any) -> None:
        _execute_tool(ctx, tool, kwargs)

    help_text = tool.summary
    if any(p.is_complex for p in tool.params):
        help_text += " (use --json for complex fields)"

    return GeneratedToolCommand(
        name=tool.name,
        callback=callback,
        help=help_text,
        short_help=tool.summary,
        params=params,
    )


class GeneratedToolGroup(click.Group):
    """A hand-written Click group augmented by one generated tool category."""

    def __init__(self, *args: Any, tool_category: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.tool_category = tool_category

    @staticmethod
    def _resolve_env(ctx: click.Context) -> str:
        root = ctx.find_root()
        flag_env = root.params.get("flag_env") or ""
        try:
            return resolve_environment(flag_env)
        except ValueError:
            # Root option validation reports the invalid value after command
            # discovery, so use the default spec while Click resolves commands.
            return "production"

    def _generated_tools(self, ctx: click.Context) -> list[ToolDef]:
        root = ctx.find_root()
        env = self._resolve_env(ctx)
        if not getattr(root, "_ramp_synced", False):
            maybe_sync(env)
            root._ramp_synced = True  # type: ignore[attr-defined]
        return [
            tool for tool in list_tool_defs(env) if tool.category == self.tool_category
        ]

    def _generated_commands(self, ctx: click.Context) -> list[tuple[str, ToolDef]]:
        return resolve_tool_command_names(
            self.tool_category,
            self._generated_tools(ctx),
        )

    def list_commands(self, ctx: click.Context) -> list[str]:
        static = set(super().list_commands(ctx))
        generated = {name for name, _tool in self._generated_commands(ctx)}
        return sorted(static | generated)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        if command := super().get_command(ctx, cmd_name):
            return command

        for command_name, tool in self._generated_commands(ctx):
            if cmd_name == command_name:
                return build_tool_command(
                    tool,
                    group_name=self.tool_category,
                    command_name=command_name,
                )
        return None


def _json_option_help(
    tool: ToolDef,
    group_name: str | None = None,
    command_name: str | None = None,
) -> str:
    """Describe when raw JSON is useful for this operation."""
    complex_fields = [param.name for param in tool.params if param.is_complex]
    if not complex_fields:
        return "Raw JSON input (validated against the tool schema)"
    group_name = group_name or CATEGORY_REMAP.get(tool.category, tool.category)
    command_name = command_name or tool.alias or tool.name
    schema_hint = (
        f" Run 'ramp tools schema {group_name} {command_name}' for the full structure."
        if group_name
        else ""
    )
    return (
        f"Raw JSON input; required for complex fields: {', '.join(complex_fields)}."
        f"{schema_hint}"
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
        case ParamType.FILE:
            kwargs.update(
                type=click.Path(
                    exists=True,
                    dir_okay=False,
                    readable=True,
                    path_type=Path,
                ),
                default=None,
            )
        case _:
            kwargs.update(type=str, default=None)

    # Click derives kwarg names from flags; add a secondary name if they differ
    kwarg_name = param.flag.replace("-", "_")
    if kwarg_name != param.flag:
        decls.append(kwarg_name)

    # Inject common aliases (e.g. --limit for --page_size) so agents and
    # users familiar with Unix conventions don't need to discover via --help.
    for alias in _PARAM_ALIASES.get(param.name, []):
        if alias not in decls:
            decls.append(alias)

    return click.Option(decls, **kwargs)


def _supports_application_progress_wait(tool: ToolDef) -> bool:
    return tool.http_method == "get" and tool.path == _APPLICATION_PROGRESS_PATH


def _build_application_progress_wait_options() -> list[click.Option]:
    return [
        click.Option(
            ["--wait_for_action", "--wait-for-action"],
            multiple=True,
            metavar="ACTION",
            help=(
                "Poll until the named required action is no longer present. "
                "Accepts phone_verification, identity_verification, or an exact "
                "required action/applicant_action enum. May be repeated."
            ),
        ),
        click.Option(
            ["--wait_for_phone_verification", "--wait-for-phone-verification"],
            is_flag=True,
            default=False,
            help="Poll until phone verification is complete.",
        ),
        click.Option(
            ["--wait_for_identity_verification", "--wait-for-identity-verification"],
            is_flag=True,
            default=False,
            help="Poll until identity verification is complete.",
        ),
        click.Option(
            ["--wait_interval", "--wait-interval"],
            type=click.IntRange(1),
            default=15,
            show_default=True,
            help="Seconds between progress polling attempts.",
        ),
        click.Option(
            ["--wait_timeout", "--wait-timeout"],
            type=click.IntRange(1),
            default=900,
            show_default=True,
            help="Maximum seconds to wait for required actions to complete.",
        ),
    ]


def _execute_tool(ctx: click.Context, tool: ToolDef, kwargs: dict[str, Any]) -> None:
    """Build the JSON body, optionally dry-run, then call the agent-tool endpoint."""
    env: str = ctx.obj["env"]
    fmt: str | None = ctx.obj["format"]
    config_format: str = ctx.obj["config_format"]
    json_body_raw: str | None = kwargs.get("json_body")
    dry_run: bool = kwargs.get("dry_run", False)
    wait_config = _application_progress_wait_config(tool, kwargs)
    synced_for_json_validation = False
    is_agent_mode = ctx.obj.get("agent_mode", False)
    kwargs = _with_human_rationale(tool, kwargs, is_agent_mode=is_agent_mode)

    if dry_run and wait_config is not None:
        raise click.UsageError(
            "application progress wait flags cannot be used with --dry_run"
        )

    if json_body_raw:
        try:
            raw_values = json.loads(json_body_raw)
        except json.JSONDecodeError as e:
            raise click.BadParameter(f"invalid JSON: {e}", param_hint="'--json'")
        if not isinstance(raw_values, dict):
            raise click.BadParameter(
                "JSON body must be an object", param_hint="'--json'"
            )
        if not dry_run:
            tool = _sync_tool_for_json_validation(env, tool)
            synced_for_json_validation = True
        values = _build_body(tool, kwargs, allow_missing=True)
        validation_values = {
            name: value
            for name, value in values.items()
            if tool.json_schema is None or name in tool.json_schema.properties
        }
        validation_values.update(raw_values)
        _validate_json_body(tool, validation_values)
        body = {
            param.name: values[param.name]
            for param in tool.params
            if param.location == "body" and param.name in values
        }
        body.update(raw_values)
        for param in tool.params:
            if param.location == "body" or param.name not in body:
                continue
            value = body.pop(param.name)
            if param.type is ParamType.FILE:
                if value is None:
                    continue
                value = _validate_file_path(value, param.flag)
            values[param.name] = value
        _validate_required_non_body_params(tool, values)
    else:
        values = _build_body(tool, kwargs)
        body = {
            param.name: values[param.name]
            for param in tool.params
            if param.location == "body" and param.name in values
        }

    request = _prepare_request(
        tool,
        values,
        body,
        mode="agent" if is_agent_mode else "human",
    )
    query = {name: str(value) for name, value in request.query.items()}
    request_path = append_query_params(request.path, query)
    proxy_url: str | None = None
    proxy_headers: dict[str, str] = {}
    if tool.name == _VAULT_PROXY_TOOL:
        proxy_url = vault_proxy_target(request_path)
        if proxy_url:
            if request.method != "post":
                raise click.UsageError(
                    "Agent Card credential proxy routing requires a POST operation"
                )
            proxy_headers = vault_proxy_headers()

    if dry_run:
        resolved = resolve_format(fmt, config_format)
        target_url = proxy_url or api_url(env, request.path, query)
        if resolved == "json":
            payload = {
                "dry_run": True,
                "method": request.method.upper(),
                "url": target_url,
                "body": request.display_body(),
            }
            if request.headers:
                payload["headers"] = request.headers
            if proxy_url:
                payload["via_vault_proxy"] = True
                payload["proxy_headers"] = {key: "<redacted>" for key in proxy_headers}
            print_agent_json(
                payload,
                pagination=None,
            )
        else:
            click.echo(
                f"DRY RUN: {request.method.upper()} {target_url}",
                err=True,
            )
            if proxy_url:
                click.echo("  (routed through payment-vault proxy)", err=True)
            print_json(request.display_body())
        return

    # Refresh cached spec in the background for the *next* invocation. Non-dry-run
    # --json bodies already forced a sync before schema validation above.
    if not synced_for_json_validation:
        maybe_sync(env)

    resolved = resolve_format(fmt, config_format)
    is_human = resolved != "json" and sys.stdout.isatty() and not is_quiet()

    stop_spinner = _start_spinner(tool.display_name) if is_human else None
    t0 = time.monotonic()

    profile = ctx.obj.get("profile")
    client = RampClient(env, profile=profile) if profile else RampClient(env)
    try:
        if wait_config is None:
            resp_bytes = _execute_prepared_request(
                client,
                request,
                post_url=proxy_url,
                extra_headers=proxy_headers,
            )
            data = json.loads(resp_bytes) if resp_bytes.strip() else {}
        else:
            data = _execute_waiting_application_progress(
                client,
                request,
                wait_config,
                is_human=is_human,
                started_at=t0,
            )
    except ApiError as error:
        if error.status_code == 403 and error.error_code == "DEVELOPER_7100":
            raise ApiError(
                error.status_code,
                error.body,
                contextual_hint=_scope_error_hint(tool, env, profile),
            ) from error
        hint = _availability_error_hint(tool, env, error, profile)
        if hint:
            raise ApiError(
                error.status_code, error.body, contextual_hint=hint
            ) from error
        raise

    elapsed = time.monotonic() - t0
    if stop_spinner:
        stop_spinner()

    # Record category usage and show a contextual tip on first invocation.
    # This must happen before any early-return path (e.g. interactive table)
    # so usage is always tracked and the tip is always shown.
    # Use the remapped category so tracking matches invokable CLI groups
    # (e.g. spec category "cards" → CLI group "funds").
    if is_human and tool.category:
        display_cat = CATEGORY_REMAP.get(tool.category, tool.category)
        maybe_show_category_tip(display_cat)

    if resolved == "json":
        print_agent_json(
            data,
            pagination=_extract_pagination(
                data,
                cursor_param=_declared_cursor_param(tool),
            ),
        )
    else:
        wide: bool = ctx.obj.get("wide", False)
        _print_summary(tool.display_name, data, elapsed)

        no_input: bool = ctx.obj.get("no_input", False)
        if is_human and sys.stdin.isatty() and not no_input:
            if _try_interactive_table(tool, data, request, client, wide=wide):
                return

        _render_human(data, wide=wide, category=tool.category)


def _availability_error_hint(
    tool: ToolDef, env: str, error: ApiError, profile: str | None = None
) -> str | None:
    """Explain a rejected tool call using the effective-availability endpoint."""
    if not 400 <= error.status_code < 500:
        return None
    snapshot = (
        fetch_availability(env, profile=profile) if profile else fetch_availability(env)
    )
    entry = snapshot.lookup(tool) if snapshot else None
    if entry is None or entry.available:
        return None
    return (
        f"This tool is currently unavailable for your credentials: {entry.describe()}."
    )


def _scope_error_hint(tool: ToolDef, env: str, profile: str | None = None) -> str:
    if os.environ.get("RAMP_ACCESS_TOKEN"):
        return (
            f"The `{tool.name}` command was rejected by Core because "
            "`RAMP_ACCESS_TOKEN` has insufficient OAuth scope.\n\n"
            "  This environment variable overrides credentials saved by "
            "`ramp auth login`. Replace it with an appropriately scoped token, "
            "or unset it before re-authorizing:\n\n"
            "    unset RAMP_ACCESS_TOKEN\n"
            f"    ramp --env {env} tools refresh\n"
            f"    ramp --env {env} auth login"
        )

    required = set(tool.required_scopes)
    override_guidance = ""
    configured = set(configured_scopes().split())
    missing_from_override = sorted(required - configured) if configured else []
    if missing_from_override:
        override_guidance = (
            "\n  Your top-level custom `scopes` override omits scopes declared by "
            "the cached tool definition: "
            f"{', '.join(missing_from_override)}. Update or remove the override "
            "before logging in.\n"
        )

    login_command = f"ramp --env {env} auth login"
    if profile == "agent":
        login_command += " --client-id <agent-client-id>"
    return (
        f"The `{tool.name}` command was rejected by Core because the active "
        "credential has insufficient OAuth scope.\n"
        f"{override_guidance}\n"
        f"  Refresh the tool definitions and re-authorize for {env}:\n\n"
        f"    ramp --env {env} tools refresh\n"
        f"    {login_command}"
    )


def _application_progress_wait_config(
    tool: ToolDef,
    kwargs: dict[str, Any],
) -> WaitConfig | None:
    if not _supports_application_progress_wait(tool):
        return None

    actions = [
        _normalize_application_wait_action(action)
        for action in kwargs.get("wait_for_action", ())
    ]
    if kwargs.get("wait_for_phone_verification"):
        actions.append("phone_verification")
    if kwargs.get("wait_for_identity_verification"):
        actions.append("identity_verification")

    deduped_actions = tuple(dict.fromkeys(action for action in actions if action))
    if not deduped_actions:
        return None

    return WaitConfig(
        actions=deduped_actions,
        interval_seconds=kwargs["wait_interval"],
        timeout_seconds=kwargs["wait_timeout"],
    )


def _normalize_application_wait_action(action: str) -> str:
    key = action.strip().lower().replace("-", "_")
    return _APPLICATION_WAIT_ACTION_ALIASES.get(key, action.strip())


def _execute_waiting_application_progress(
    client: RampClient,
    request: PreparedRequest,
    wait_config: WaitConfig,
    *,
    is_human: bool,
    started_at: float,
) -> dict[str, Any]:
    deadline = started_at + wait_config.timeout_seconds
    if is_human:
        actions = ", ".join(wait_config.actions)
        click.echo(f"Waiting for application actions to complete: {actions}", err=True)

    while True:
        resp_bytes = _execute_prepared_request(client, request)
        data = json.loads(resp_bytes) if resp_bytes.strip() else {}
        pending_actions = _matching_application_required_actions(
            data,
            wait_config.actions,
        )
        if not pending_actions:
            return data

        now = time.monotonic()
        if now >= deadline:
            pending = ", ".join(
                _summarize_required_action(action) for action in pending_actions
            )
            raise click.ClickException(
                "Timed out waiting for application actions to complete after "
                f"{wait_config.timeout_seconds} seconds: {pending}"
            )

        time.sleep(min(wait_config.interval_seconds, max(0.0, deadline - now)))


def _matching_application_required_actions(
    data: dict[str, Any],
    targets: tuple[str, ...],
) -> list[dict[str, Any]]:
    required_actions = data.get("required_actions")
    if not isinstance(required_actions, list):
        return []

    return [
        action
        for action in required_actions
        if isinstance(action, dict)
        and any(
            _application_required_action_matches(action, target) for target in targets
        )
    ]


def _application_required_action_matches(
    action: dict[str, Any],
    target: str,
) -> bool:
    if target == "identity_verification":
        return action.get("applicant_action") == "COMPLETE_IDENTITY_VERIFICATION"

    if target == "phone_verification":
        has_phone_scope = (
            action.get("page_key") is not None or action.get("section_key") is not None
        )
        return action.get("applicant_action") == "VERIFY_EMAIL_OR_PHONE" and (
            not has_phone_scope
            or action.get("page_key") == "phone_verification"
            or action.get("section_key") == "phone_verification"
        )

    enum_target = target.upper().replace("-", "_")
    key_target = target.lower().replace("-", "_")
    return any(
        action.get(field) == enum_target for field in ("type", "applicant_action")
    ) or any(action.get(field) == key_target for field in ("page_key", "section_key"))


def _summarize_required_action(action: dict[str, Any]) -> str:
    return "/".join(
        str(action[field])
        for field in ("type", "applicant_action", "page_key", "section_key")
        if action.get(field)
    )


def _prepare_request(
    tool: ToolDef,
    values: dict[str, Any],
    body: dict[str, Any],
    *,
    mode: str,
) -> PreparedRequest:
    path_values = _values_for_location(tool, values, "path")
    query_values = {
        name: value
        for name, value in _values_for_location(tool, values, "query").items()
        if value is not None
    }
    form_values = {
        param.name: values[param.name]
        for param in tool.params
        if param.location == "form"
        and param.type is not ParamType.FILE
        and param.name in values
        and values[param.name] is not None
    }
    file_values = {
        param.name: Path(values[param.name])
        for param in tool.params
        if param.location == "form"
        and param.type is ParamType.FILE
        and param.name in values
        and values[param.name] is not None
    }
    header_values = {
        name: str(value)
        for name, value in _values_for_location(tool, values, "header").items()
        if value is not None
    }
    resolved_path = _resolve_path(tool.path, path_values)
    if _declares_idempotency_key(tool) and _IDEMPOTENCY_HEADER not in header_values:
        header_values[_IDEMPOTENCY_HEADER] = _default_idempotency_key(tool)
    header_values[_TOOL_CALL_MODE_HEADER] = mode
    return PreparedRequest(
        method=tool.http_method,
        path=resolved_path,
        query=query_values,
        body=body,
        form=form_values,
        files=file_values,
        headers=header_values,
        content_type=tool.request_content_type,
    )


def _with_human_rationale(
    tool: ToolDef,
    kwargs: dict[str, Any],
    *,
    is_agent_mode: bool,
) -> dict[str, Any]:
    """Supply the API-required rationale for interactive CLI calls."""
    if is_agent_mode:
        return kwargs

    rationale = next(
        (
            param
            for param in tool.params
            if param.name == "rationale" and param.required and not param.is_complex
        ),
        None,
    )
    if rationale is None:
        return kwargs

    key = rationale.flag.replace("-", "_")
    if kwargs.get(key) is not None:
        return kwargs
    return {**kwargs, key: _HUMAN_RATIONALE}


def _declares_idempotency_key(tool: ToolDef) -> bool:
    return any(
        param.location == "header" and param.name.lower() == _IDEMPOTENCY_HEADER.lower()
        for param in tool.params
    )


def _default_idempotency_key(tool: ToolDef) -> str:
    nonce = str(uuid4())
    prefix = f"{get_session_id()}:{tool.name}:"
    return f"{prefix[: 255 - len(nonce)]}{nonce}"


def _execute_prepared_request(
    client: RampClient,
    request: PreparedRequest,
    *,
    post_url: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    params = {name: str(value) for name, value in request.query.items()}
    path = append_query_params(request.path, params)
    headers = {**request.headers, **(extra_headers or {})}
    headers_kw = {"headers": headers} if headers else {}

    if request.content_type == "multipart/form-data":
        with ExitStack() as stack:
            files = {}
            for name, file_path in request.files.items():
                file_obj = stack.enter_context(file_path.open("rb"))
                content_type = (
                    mimetypes.guess_type(file_path.name)[0]
                    or "application/octet-stream"
                )
                files[name] = (file_path.name, file_obj, content_type)
            data = {name: str(value) for name, value in request.form.items()}
            if request.method == "post":
                return client.post_multipart(path, data, files, **headers_kw)
            return client.request_multipart(
                request.method, path, data, files, **headers_kw
            )

    if request.method == "get":
        return client.get(request.path, params, **headers_kw)
    payload = json.dumps(request.body).encode() if request.body else None
    if request.method == "patch":
        return client.patch(path, payload or b"{}", **headers_kw)
    if request.method == "put":
        return client.put(path, payload or b"{}", **headers_kw)
    if request.method == "delete":
        return client.delete(path, payload, **headers_kw)
    if post_url is not None:
        return client.post_url(post_url, payload or b"{}", **headers_kw)
    return client.post(path, payload or b"{}", **headers_kw)


def _values_for_location(
    tool: ToolDef,
    values: dict[str, Any],
    location: str,
) -> dict[str, Any]:
    return {
        param.name: values[param.name]
        for param in tool.params
        if param.location == location and param.name in values
    }


def _validate_file_path(value: Any, flag: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise click.BadParameter(
            f"file path for --{flag} must be a string",
            param_hint="'--json'",
        )
    path = Path(value)
    if not path.is_file():
        raise click.BadParameter(
            f"file does not exist: {path}",
            param_hint="'--json'",
        )
    return path


def _build_body(
    tool: ToolDef,
    kwargs: dict[str, Any],
    *,
    allow_missing: bool = False,
) -> dict[str, Any]:
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
            if _has_auto_default(param):
                continue
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

    if missing and not allow_missing:
        _raise_missing_params(tool, missing)

    return body


def _has_auto_default(param: ToolParam) -> bool:
    return (
        param.location == "header" and param.name.lower() == _IDEMPOTENCY_HEADER.lower()
    )


def _validate_required_non_body_params(
    tool: ToolDef,
    values: dict[str, Any],
) -> None:
    missing: list[str] = []
    for param in tool.params:
        if not param.required or param.location not in {"path", "query", "header"}:
            continue
        if values.get(param.name) is not None or _has_auto_default(param):
            continue
        if is_id_param(param):
            missing.append(param.flag.upper())
        else:
            missing.append(f"--{param.flag}")
    if missing:
        _raise_missing_params(tool, missing)


def _raise_missing_params(tool: ToolDef, missing: list[str]) -> None:
    example_parts = [f"ramp {tool.display_name}"]
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


def _resolve_path(path: str, values: dict[str, Any]) -> str:
    for name, value in values.items():
        path = path.replace("{" + name + "}", quote(str(value), safe=""))
    return path


def _validate_json_body(tool: ToolDef, body: Any) -> dict[str, Any]:
    """Validate raw --json bodies against the parsed OpenAPI schema."""
    if not isinstance(body, dict):
        raise click.BadParameter("JSON body must be an object", param_hint="'--json'")

    if tool.json_schema is not None:
        _validate_json_object(
            tool.json_schema,
            body,
            validate_required=tool.http_method != "patch",
        )

    return body


def _sync_tool_for_json_validation(env: str, tool: ToolDef) -> ToolDef:
    """Refresh the local spec before validating raw --json schema fields."""
    maybe_sync(env, force=True)
    return get_tool(tool.name, env) or tool


def _validate_json_object(
    schema: JsonSchema,
    body: dict[str, Any],
    path: tuple[str, ...] = (),
    *,
    validate_required: bool = True,
) -> None:
    if validate_required:
        for key in sorted(schema.required_properties - body.keys()):
            raise click.BadParameter(
                f"missing required JSON field '{'.'.join(path + (key,))}'",
                param_hint="'--json'",
            )

    if schema.properties and not schema.additional_properties_allowed:
        allowed = set(schema.properties)
        for key in body:
            if key not in allowed:
                raise click.BadParameter(
                    _unknown_field_message(path + (key,), schema.properties),
                    param_hint="'--json'",
                )

    for key, value in body.items():
        child_schema = schema.properties.get(key)
        if child_schema is None:
            continue
        _validate_json_value(child_schema, value, path + (key,))


def _validate_json_value(schema: JsonSchema, value: Any, path: tuple[str, ...]) -> None:
    if value is None and schema.nullable:
        return

    if schema.enum_values:
        if value not in schema.enum_values:
            raise click.BadParameter(
                _invalid_enum_message(path, value, schema.enum_values),
                param_hint="'--json'",
            )
        return

    if schema.properties and isinstance(value, dict):
        _validate_json_object(schema, value, path)
        return

    if schema.array_item is not None and isinstance(value, list):
        item_schema = schema.array_item
        for index, item in enumerate(value):
            item_path = (*path[:-1], f"{path[-1]}[{index}]")
            _validate_json_value(item_schema, item, item_path)


def _unknown_field_message(
    field_path: tuple[str, ...], properties: dict[str, JsonSchema]
) -> str:
    field = ".".join(field_path)
    valid = ", ".join(sorted(properties))
    parent = ".".join(field_path[:-1])
    if parent:
        return f"unknown JSON field '{field}'. Valid fields at '{parent}': {valid}"
    return f"unknown JSON field '{field}'. Valid fields: {valid}"


def _invalid_enum_message(
    path: tuple[str, ...], value: str, enum_values: list[str]
) -> str:
    return (
        f"invalid enum value '{value}' at '{'.'.join(path)}'. "
        f"Choose from: {', '.join(enum_values)}"
    )


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
    request: PreparedRequest,
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

    next_cursor = _pagination_next(data)
    cursor_param = _detect_cursor_param(tool)
    headers = extract_headers(list_items[0], wide=wide, category=tool.category)

    def fetch_next(cursor: str) -> tuple[list[dict[str, str]], str | None]:
        if cursor.startswith(("https://", "http://")):
            resp = client.get_url(cursor, headers=request.headers)
        else:
            next_request = _with_request_value(tool, request, cursor_param, cursor)
            resp = _execute_prepared_request(client, next_request)
        page_data = json.loads(resp)
        _k, items = _extract_list_field(page_data)
        return _format_rows(items, headers, wide=wide), _pagination_next(page_data)

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


def _with_request_value(
    tool: ToolDef,
    request: PreparedRequest,
    name: str,
    value: Any,
) -> PreparedRequest:
    param = next((param for param in tool.params if param.name == name), None)
    location = param.location if param is not None else "body"
    field_name = {
        "query": "query",
        "form": "form",
        "body": "body",
    }.get(location, "body")
    updated = dict(getattr(request, field_name))
    updated[name] = value
    return replace(request, **{field_name: updated})


_CURSOR_PARAM_NAMES = ("next_page_cursor", "page_cursor", "cursor", "start")


def _detect_cursor_param(tool: ToolDef) -> str:
    """Detect which input param name this tool uses for cursor pagination."""
    return _declared_cursor_param(tool) or "next_page_cursor"


def _declared_cursor_param(tool: ToolDef) -> str | None:
    param_names = {p.name for p in tool.params}
    for name in _CURSOR_PARAM_NAMES:
        if name in param_names:
            return name
    return None


_PAGINATION_KEYS = ("next_page_cursor", "page_cursor", "cursor", "next")


def _extract_pagination(
    data: Any,
    *,
    cursor_param: str | None = None,
) -> dict | None:
    """Extract pagination info from an API response for the agent JSON envelope.

    Full next-page URLs are reduced to the value accepted by the tool's cursor
    parameter when one is known. Interactive pagination still uses the full URL.
    """
    if not isinstance(data, dict):
        return None
    for key in _PAGINATION_KEYS:
        value = data.get(key)
        if value is not None:
            return {
                "next_cursor": _agent_cursor_value(value, cursor_param=cursor_param)
            }
    page = data.get("page")
    if isinstance(page, dict) and page.get("next") is not None:
        return {
            "next_cursor": _agent_cursor_value(
                page["next"],
                cursor_param=cursor_param,
            )
        }
    return None


def _agent_cursor_value(value: Any, *, cursor_param: str | None) -> Any:
    if (
        cursor_param is None
        or not isinstance(value, str)
        or not value.startswith(("https://", "http://"))
    ):
        return value

    values = parse_qs(urlparse(value).query).get(cursor_param)
    return values[0] if values else value


def _pagination_next(data: Any) -> str | None:
    pagination = _extract_pagination(data)
    if pagination is None:
        return None
    value = pagination.get("next_cursor")
    return str(value) if value else None


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
