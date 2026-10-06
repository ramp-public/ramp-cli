"""User-level Router commands; inference-key configure is unchanged."""

from __future__ import annotations

import json
import re
import shutil
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import uuid4

import click
import httpx
import questionary

from ramp_cli.client.router_user import (
    DEFAULT_ORIGIN,
    RouterUserClient,
    selected_origin,
)
from ramp_cli.errors import ApiError, RampCLIError
from ramp_cli.output.formatter import (
    print_agent_json,
    print_json,
    print_table,
    resolve_format,
    truncate,
)
from ramp_cli.output.style import _WIDTH_MAX, _WIDTH_MIN, show_detail_card
from ramp_cli.router_ui import launch as router_ui

if TYPE_CHECKING:
    from typing import TypeAlias

    # A picker style, or a zero-argument factory for one so callers can defer
    # building it until a prompt is actually shown.
    PickerStyle: TypeAlias = questionary.Style | Callable[[], questionary.Style] | None


def resolve_picker_style(style: PickerStyle) -> questionary.Style | None:
    return style() if callable(style) else style


_ESCAPE_DELAY_SECONDS = 0.05


def _ask(question):
    """Ask a questionary prompt where Esc means back/cancel (returns None).

    questionary only cancels on Ctrl-C; the Router pickers advertise Esc, so
    bind it the same way the existing agent picker does.
    """
    application = getattr(question, "application", None)
    if application is not None:
        from prompt_toolkit.key_binding import (  # noqa: PLC0415
            KeyBindings,
            merge_key_bindings,
        )
        from prompt_toolkit.keys import Keys  # noqa: PLC0415

        escape = KeyBindings()

        @escape.add(Keys.Escape, eager=True)
        def _cancel(event):
            event.app.exit(result=None)

        # Text and confirm prompts expose merged (read-only) bindings.
        application.key_bindings = merge_key_bindings(
            [application.key_bindings, escape]
        )
        # prompt_toolkit waits 0.5s after Esc in case an arrow-key sequence
        # follows. Terminals send those sequences in one burst, so 50ms keeps
        # arrows working while Esc feels immediate.
        application.ttimeoutlen = _ESCAPE_DELAY_SECONDS
    return question.ask()


def _client(ctx: click.Context, ui_url: str | None = None) -> RouterUserClient:
    return RouterUserClient(
        selected_origin(ui_url, profile=ctx.obj.get("profile")),
        profile=ctx.obj.get("profile"),
        no_input=ctx.obj.get("no_input", False),
        interactive=not ctx.obj.get("agent_mode")
        and sys.stdin.isatty()
        and sys.stdout.isatty(),
    )


def _single_line(value: object) -> str:
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "", str(value))


def _money(value: object) -> str:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            raise InvalidOperation
        return f"${amount:,.2f}"
    except (InvalidOperation, ValueError, TypeError):
        return "Unavailable"


def _credits(billing: object, origin: str | None = None) -> str:
    """Remaining credit as the workspace shows it."""
    if not isinstance(billing, dict):
        return "Unavailable"
    if billing.get("ramp_employee_session_unlimited") is True:
        return "Unlimited"
    wallet = (
        billing.get("business_wallet")
        if billing.get("billed_to_business")
        else billing.get("snapshot")
    )
    if isinstance(wallet, dict):
        return _money(wallet.get("remaining_credit_usd"))
    # Routers other than production (internal.router.com among them) don't
    # provision billing, so an empty balance there means nobody is billed.
    if origin is not None and origin.rstrip("/") != DEFAULT_ORIGIN:
        return "Not billed"
    return "Unavailable"


def _menu_footer(ctx: click.Context) -> str:
    account, credits = "Not Logged In", None
    try:
        client = _client(ctx)
        state = client.status()
        if state["authenticated"] and not state["access_token_expired"]:
            # Saved refresh credentials identify a candidate account, not a
            # live session. Verify without browser recovery or token rotation
            # before displaying personal account information in help.
            client.access_token(check_session=True, recover=False, timeout=2)
            user = state.get("user") or {}
            account = _single_line(user.get("email") or user.get("id") or "Signed in")
            credits = "Unavailable"
            credits = _credits(
                client.get("/client/billing", timeout=2, refresh=False),
                client.origin,
            )
    except ApiError as error:
        if error.status_code in (401, 403):
            account, credits = "Not Logged In", None
    except RampCLIError:
        account, credits = "Not Logged In", None
    except (httpx.HTTPError, ValueError, KeyError, TypeError, OSError):
        pass
    width = max(_WIDTH_MIN, min(shutil.get_terminal_size((80, 24)).columns, _WIDTH_MAX))
    left = f"Credits: {credits}" if credits is not None else ""
    right = truncate(f"Account: {account}", max(1, width - len(left) - 6))
    return f"{left}{' ' * max(2, width - len(left) - len(right))}{right}\n"


_WORKSPACE_PARAMS = ("inline", "inline_height", "theme_mode")


class RouterMenuGroup(click.Group):
    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        # Old paths remain callable without cluttering the visible menu.
        return super().get_command(ctx, cmd_name) or getattr(
            self, "_legacy_commands", {}
        ).get(cmd_name)

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        # Without the automatic workspace, these only matter to `router ui`.
        hidden = [
            param
            for param in self.params
            if param.name in _WORKSPACE_PARAMS and not param.hidden
        ]
        if hidden and not router_ui.auto_launch():
            for param in hidden:
                param.hidden = True
        else:
            hidden = []
        try:
            super().format_help(ctx, formatter)
        finally:
            for param in hidden:
                param.hidden = False
        if ctx.obj and not ctx.obj.get("agent_mode") and not ctx.obj.get("quiet"):
            formatter.write_paragraph()
            formatter.write(_menu_footer(ctx))


def _human(ctx: click.Context) -> bool:
    return (
        not ctx.obj.get("agent_mode")
        and resolve_format(ctx.obj.get("format"), ctx.obj.get("config_format", ""))
        == "table"
    )


def _router_host(origin: object) -> str:
    parts = urlsplit(str(origin or ""))
    return parts.netloc or str(origin or "")


def _output_session(ctx: click.Context, data: dict, *, title: str) -> None:
    """Human-friendly account card; machine formats keep the raw payload."""
    if not _human(ctx):
        _output(ctx, data)
        return
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    signed_in = bool(data.get("authenticated"))
    fields = {
        "Account": _single_line(user.get("email") or "Not signed in")
        if signed_in
        else "Not signed in",
        "Router": _single_line(_router_host(data.get("origin"))),
        "Status": "Signed in" if signed_in else "Signed out",
    }
    show_detail_card(title, fields, plain=True)
    if not signed_in:
        click.echo("Run `ramp router login` to sign in.")


def _output(ctx: click.Context, data: dict) -> None:
    fmt = resolve_format(ctx.obj.get("format"), ctx.obj.get("config_format", ""))
    if ctx.obj.get("agent_mode"):
        print_agent_json(data)
    elif fmt == "table":
        for key, value in data.items():
            click.echo(f"{key}: {value}")
    else:
        print_json(data)


def _output_keys(ctx: click.Context, payload: dict, *, offset: int) -> None:
    """Keep the interactive list compact without changing machine contracts."""
    fmt = resolve_format(ctx.obj.get("format"), ctx.obj.get("config_format", ""))
    if ctx.obj.get("agent_mode") or fmt != "table":
        _output(ctx, payload)
        return
    keys = payload.get("data", [])
    count = len(keys)
    label = "API key" if count == 1 else "API keys"
    if offset or payload.get("has_more"):
        click.echo(f"Showing {count} {label} (offset {offset})")
    else:
        click.echo(f"{count} {label}")
    if count:
        print_table(
            ["Name", "Enabled", "ID"],
            [
                {
                    "Name": key.get("name") or "(unnamed)",
                    "Enabled": "Yes" if key.get("enabled") is True else "No",
                    "ID": key["id"],
                }
                for key in keys
            ],
        )
    if payload.get("has_more"):
        next_offset = payload.get("next_offset")
        if not isinstance(next_offset, int):
            next_offset = offset + count
        click.echo(
            f"More keys available. Use --offset {next_offset} for the next page."
        )


def _all_keys(client: RouterUserClient) -> list[dict]:
    keys: list[dict] = []
    offset = 0
    for _ in range(100):
        page = client.get("/client/api-keys", params={"limit": 100, "offset": offset})
        keys.extend(page["data"])
        if not page.get("has_more"):
            return keys
        next_offset = page.get("next_offset")
        if not isinstance(next_offset, int) or next_offset <= offset:
            raise RampCLIError("Router returned invalid API-key pagination.")
        offset = next_offset
    raise RampCLIError(
        "Too many API-key pages; use 'keys list --offset' to page explicitly."
    )


def _interactive_keys(ctx: click.Context) -> bool:
    return (
        not ctx.obj.get("no_input")
        and not ctx.obj.get("agent_mode")
        and resolve_format(ctx.obj.get("format"), ctx.obj.get("config_format", ""))
        == "table"
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )


_PICKER_BACK = "__router_keys_back__"
_PICKER_DONE = "__router_keys_done__"
_PICKER_CREATE = "__router_keys_create__"
_PICKER_RENAME = "__router_keys_rename__"
_PICKER_STRATEGY = "__router_keys_strategy__"
_PICKER_LOCK = "__router_keys_lock__"
_PICKER_UNLOCK = "__router_keys_unlock__"
_KEY_NAME_MAX = 32


def _routing_profiles(client: RouterUserClient) -> list[dict] | None:
    """The caller's routing profiles, or None outside the routing-profile rollout."""
    try:
        return client.get("/client/routing-profiles").get("data") or []
    except ApiError as error:
        if error.status_code == 403:
            return None
        raise


def _profile_strategy_summary(profile: dict) -> str:
    enabled = [
        label
        for label, field in (
            ("Flex", "cost_efficient_routing_enabled"),
            ("Switchyard", "switchyard_routing_enabled"),
            ("Jev Auto Routing", "auto_routing_enabled"),
        )
        if profile.get(field)
    ]
    if profile.get("auto_routing_enabled") is None:
        enabled.append("Jev Auto Routing: account default")
    return ", ".join(enabled) if enabled else "No strategies"


def _profile_label(profile: dict) -> str:
    name = _single_line(profile.get("name") or "(unnamed)")
    if profile.get("is_default"):
        name += " (default)"
    return f"{name} · {_profile_strategy_summary(profile)}"


def _match_profile(profiles: list[dict], reference: str) -> dict:
    wanted = reference.strip()
    for profile in profiles:
        if str(profile.get("id")) == wanted:
            return profile
    matches = [
        profile
        for profile in profiles
        if str(profile.get("name") or "").casefold() == wanted.casefold()
    ]
    if len(matches) == 1:
        return matches[0]
    names = ", ".join(_single_line(p.get("name")) for p in profiles) or "none"
    raise click.BadParameter(
        f"No routing profile named '{wanted}'. Available: {names}.",
        param_hint="--routing-profile",
    )


def _validate_key_name(value: str) -> str | bool:
    name = value.strip()
    if not name:
        return "Enter a name for the key."
    if len(name) > _KEY_NAME_MAX:
        return f"Use {_KEY_NAME_MAX} characters or fewer."
    return True


def create_named_key(
    client: RouterUserClient, name: str, profile: dict | None, request_id: str
) -> dict:
    """One shared write operation, independent of prompts and output."""
    valid = _validate_key_name(name)
    if valid is not True:
        raise click.BadParameter(str(valid), param_hint="--name")
    body = {"name": name.strip()}
    if profile is not None:
        body["routing_profile_id"] = str(profile["id"])
    return client.post("/client/api-keys", json=body, idempotency_key=request_id)


def _create_key(
    ctx: click.Context,
    client: RouterUserClient,
    *,
    name: str | None,
    routing_profile: str | None,
    show_key: bool,
    dry_run: bool,
    copy_to_clipboard: Callable[[str], bool],
    picker_style: PickerStyle,
    picker_pointer: str,
) -> dict | None:
    interactive = _interactive_keys(ctx)
    if name is None:
        if not interactive:
            raise click.UsageError("Provide --name when prompts are unavailable.")
        name = _ask(
            questionary.text(
                "Key name",
                validate=_validate_key_name,
                style=resolve_picker_style(picker_style),
            )
        )
        if name is None:
            return None
    valid = _validate_key_name(name)
    if valid is not True:
        raise click.BadParameter(str(valid), param_hint="--name")
    name = name.strip()

    profiles = _routing_profiles(client)
    profile: dict | None = None
    if routing_profile is not None:
        if profiles is None:
            raise click.UsageError(
                "Routing profiles aren't available for your account yet; "
                "omit --routing-profile to use your account's strategies."
            )
        profile = _match_profile(profiles, routing_profile)
    elif interactive and profiles:
        default = next((p for p in profiles if p.get("is_default")), profiles[0])
        choices = [
            questionary.Choice(_profile_label(p), value=str(p.get("id")))
            for p in profiles
        ]
        chosen = _ask(
            questionary.select(
                "Routing strategy",
                choices=choices,
                default=next(c for c in choices if c.value == str(default.get("id"))),
                style=resolve_picker_style(picker_style),
                pointer=picker_pointer,
                instruction="(↑/↓ choose, enter select, Esc cancel)",
            )
        )
        if chosen is None:
            return None
        profile = next(p for p in profiles if str(p.get("id")) == chosen)
    elif profiles:
        profile = next((p for p in profiles if p.get("is_default")), None)

    body: dict = {"name": name}
    if profile is not None:
        body["routing_profile_id"] = str(profile.get("id"))
    if dry_run:
        _output(ctx, {"method": "POST", "path": "/client/api-keys", "body": body})
        return None

    created = create_named_key(client, name, profile, str(uuid4()))
    secret = created.get("secret")
    on_clipboard = bool(secret) and not show_key and copy_to_clipboard(secret)
    # A secret reaches the terminal only by consent: --show-key, or a yes
    # when the clipboard copy fails in an interactive session.
    reveal = bool(secret) and (
        show_key
        or (
            not on_clipboard
            and interactive
            and bool(
                _ask(
                    questionary.confirm(
                        "Couldn't copy the new key to the clipboard. "
                        "Show it in this terminal? It won't be shown again.",
                        default=False,
                        style=resolve_picker_style(picker_style),
                    )
                )
            )
        )
    )
    withheld = bool(secret) and not on_clipboard and not reveal
    key_id = created.get("id")
    withheld_hint = (
        "Couldn't copy the new key to the clipboard, and it wasn't shown. "
        f"Lock it with 'ramp router keys lock {key_id}' and create another "
        "with --show-key."
    )
    strategy = (
        _single_line(profile.get("name")) if profile is not None else "Account defaults"
    )
    if (
        ctx.obj.get("agent_mode")
        or resolve_format(ctx.obj.get("format"), ctx.obj.get("config_format", ""))
        != "table"
    ):
        payload = {key: value for key, value in created.items() if key != "secret"}
        payload["routing_strategy"] = strategy
        payload["api_key_on_clipboard"] = on_clipboard
        if reveal:
            payload["secret"] = secret
        elif withheld:
            payload["secret_withheld"] = withheld_hint
        _output(ctx, payload)
        return created
    click.echo(
        f"Created {_single_line(created.get('name') or name)} · {created.get('id')}"
    )
    click.echo(f"Routing strategy: {strategy}")
    if on_clipboard:
        click.echo("Your new API key is on the clipboard. It won't be shown again.")
    elif reveal:
        click.echo(f"API key: {secret}")
        click.echo("Store it now; it won't be shown again.")
    elif withheld:
        click.echo(withheld_hint)
    return created


def _key_strategies(client: RouterUserClient, key_id: str) -> dict:
    """Resolve a key's effective strategies from the endpoints Router web uses.

    Inside the routing-profile rollout a key follows its assigned profile;
    otherwise, or while unassigned, it follows the account-wide defaults.
    """
    defaults: dict | None = None
    try:
        profiles = client.get("/client/routing-profiles").get("data") or []
    except ApiError as error:
        if error.status_code != 403:
            raise
        profiles = []
    profile = next(
        (
            entry
            for entry in profiles
            if key_id in (entry.get("assigned_api_key_ids") or [])
        ),
        None,
    )
    if profile is not None:
        auto_routing = profile.get("auto_routing_enabled")
        if auto_routing is None:
            defaults = client.get("/admin/me/experiment-settings")
            auto_routing = bool(defaults.get("auto_routing_enabled"))
        return {
            "source": "routing_profile",
            "routing_profile": {
                "id": profile.get("id"),
                "name": profile.get("name"),
                "is_default": bool(profile.get("is_default")),
            },
            "allow_flex_tier_default": bool(
                profile.get("cost_efficient_routing_enabled")
            ),
            "switchyard_routing_enabled": bool(
                profile.get("switchyard_routing_enabled")
            ),
            "auto_routing_enabled": auto_routing,
            "shadow_models_enabled": profile.get("shadow_models_enabled"),
        }
    defaults = defaults or client.get("/admin/me/experiment-settings")
    return {
        "source": "account_defaults",
        "routing_profile": None,
        "allow_flex_tier_default": bool(defaults.get("allow_flex_tier_default")),
        "switchyard_routing_enabled": bool(defaults.get("switchyard_routing_enabled")),
        "auto_routing_enabled": bool(defaults.get("auto_routing_enabled")),
        "shadow_models_enabled": defaults.get("shadow_models_enabled"),
    }


def _owned_key(client: RouterUserClient, key_id: str) -> dict:
    if not key_id or any(char in key_id for char in "/?#\\") or key_id in (".", ".."):
        raise click.BadParameter("Invalid API key ID", param_hint="key_id")
    key = next((entry for entry in _all_keys(client) if entry["id"] == key_id), None)
    if key is None:
        raise click.BadParameter(
            "API key not found in your account.", param_hint="key_id"
        )
    return key


def key_mutation_request(
    client: RouterUserClient,
    key: dict,
    action: str,
    *,
    name: str | None = None,
    routing_profile: str | None = None,
) -> tuple[str, str, dict]:
    """Validate and plan a key mutation for both front ends."""
    path = f"/client/api-keys/{key['id']}"
    method, body = "POST", {}
    if action == "rename":
        valid = _validate_key_name(name or "")
        if valid is not True:
            raise click.BadParameter(str(valid), param_hint="--name")
        method, body = "PATCH", {"name": name.strip()}
    elif action == "set-strategy":
        profiles = _routing_profiles(client)
        if not profiles:
            raise click.UsageError(
                "Routing profiles aren't available for your account."
            )
        profile = _match_profile(profiles, routing_profile or "")
        path = f"/client/routing-profiles/{profile['id']}/assignments"
        body = {"api_key_ids": [key["id"]]}
    elif action in ("lock", "unlock"):
        path += f"/{action}"
    else:
        raise click.BadParameter("Unknown API-key action.")
    return method, path, body


def _mutate_key(
    ctx: click.Context,
    client: RouterUserClient,
    key: dict,
    action: str,
    *,
    name: str | None = None,
    routing_profile: str | None = None,
    dry_run: bool = False,
) -> None:
    """Change only the selected key; leave shared profile settings untouched."""
    method, path, body = key_mutation_request(
        client, key, action, name=name, routing_profile=routing_profile
    )
    if dry_run:
        _output(ctx, {"method": method, "path": path, "body": body})
        return
    if method == "PATCH":
        result = client.patch(path, json=body)
    else:
        result = client.post(path, json=body)
    if not _human(ctx):
        _output(ctx, result)
        return
    messages = {
        "rename": f"Renamed API key to {_single_line(name)}.",
        "set-strategy": "Updated this key's routing strategy.",
        "lock": "Locked API key. It can no longer make requests.",
        "unlock": "Unlocked API key.",
    }
    click.echo(messages[action])


def _manage_key(
    ctx: click.Context,
    client: RouterUserClient,
    key: dict,
    days: int,
    *,
    style: questionary.Style | None,
    pointer: str,
) -> bool:
    """Return True to go back to the key list; cancellation never writes."""
    while True:
        _output_key_details(ctx, _key_details(client, key, days))
        after = _ask(
            questionary.select(
                "What next?",
                choices=[
                    questionary.Choice("Rename key", value=_PICKER_RENAME),
                    questionary.Choice(
                        "Change routing strategy", value=_PICKER_STRATEGY
                    ),
                    questionary.Choice("Lock key", value=_PICKER_LOCK)
                    if key.get("enabled")
                    else questionary.Choice("Unlock key", value=_PICKER_UNLOCK),
                    questionary.Choice("Back to keys", value=_PICKER_BACK),
                    questionary.Choice("Done", value=_PICKER_DONE),
                ],
                default=_PICKER_BACK,
                style=style,
                pointer=pointer,
                instruction="(↑/↓ choose, enter select, Esc exit)",
            )
        )
        if after == _PICKER_BACK:
            return True
        if after is None or after == _PICKER_DONE:
            return False
        if after == _PICKER_RENAME:
            name = _ask(
                questionary.text(
                    "Key name",
                    default=key.get("name") or "",
                    validate=_validate_key_name,
                    style=style,
                )
            )
            if name is None:
                continue
            _mutate_key(ctx, client, key, "rename", name=name)
        elif after == _PICKER_STRATEGY:
            profiles = _routing_profiles(client)
            if not profiles:
                click.echo("Routing profiles aren't available for your account.")
                continue
            current = next(
                (
                    p
                    for p in profiles
                    if key["id"] in (p.get("assigned_api_key_ids") or [])
                ),
                next((p for p in profiles if p.get("is_default")), profiles[0]),
            )
            selected = _ask(
                questionary.select(
                    "Routing strategy",
                    choices=[
                        questionary.Choice(_profile_label(p), value=str(p["id"]))
                        for p in profiles
                    ],
                    default=str(current["id"]),
                    style=style,
                    pointer=pointer,
                    instruction="(↑/↓ choose, enter select, Esc cancel)",
                )
            )
            if selected is None:
                continue
            _mutate_key(ctx, client, key, "set-strategy", routing_profile=selected)
        elif after in (_PICKER_LOCK, _PICKER_UNLOCK):
            action = "lock" if after == _PICKER_LOCK else "unlock"
            if (
                _ask(
                    questionary.confirm(
                        f"{action.title()} {_single_line(key.get('name') or key['id'])}?"
                        + (
                            " This will stop requests using this key."
                            if action == "lock"
                            else ""
                        ),
                        default=False,
                        style=style,
                    )
                )
                is not True
            ):
                continue
            _mutate_key(ctx, client, key, action)
        else:
            return False
        key = _owned_key(client, key["id"])
        click.echo()


def _key_details(client: RouterUserClient, key: dict, days: int) -> dict:
    result = {"key": key, "days": days, "strategies": None, "usage": None}
    try:
        result["strategies"] = _key_strategies(client, key["id"])
    except (ApiError, httpx.HTTPError) as error:
        if isinstance(error, ApiError) and error.status_code in (401, 403, 404):
            raise
        result["strategies_error"] = "Strategy settings are unavailable."
    end = datetime.now(timezone.utc)
    try:
        result["usage"] = client.get(
            "/client/usage/dashboard",
            params={
                "start_at": (end - timedelta(days=days)).isoformat(),
                "end_at": end.isoformat(),
                "group_by": "model",
                "filters": json.dumps({"key": [key["id"]]}),
                "include_summary_stats": "true",
            },
        )
    except (ApiError, httpx.HTTPError) as error:
        if isinstance(error, ApiError) and error.status_code in (401, 403):
            raise
        result["usage_error"] = "Usage analytics are unavailable for this environment."
    return result


def _output_key_details(ctx: click.Context, result: dict) -> None:
    fmt = resolve_format(ctx.obj.get("format"), ctx.obj.get("config_format", ""))
    if ctx.obj.get("agent_mode") or fmt != "table":
        _output(ctx, result)
        return
    key, days = result["key"], result["days"]
    show_detail_card(
        _single_line(key.get("name") or "API key"),
        {
            "ID": key["id"],
            "Enabled": "Yes" if key.get("enabled") else "No",
            "Disabled reason": key.get("disabled_reason") or "—",
            "Created": key.get("created_at") or "—",
            "Expires": key.get("expires_at") or "Never",
            "Billing": key.get("billing_mode") or "—",
            "Lifetime spend": _money(key.get("current_spend_usd")),
            "Spend limit": "No limit"
            if "spend_cap_amount_usd" in key and key["spend_cap_amount_usd"] is None
            else _money(key.get("spend_cap_amount_usd")),
            "Limit frequency": key.get("spend_cap_frequency") or "—",
        },
        plain=True,
    )
    usage = result.get("usage")
    if isinstance(usage, dict) and isinstance(usage.get("summary"), dict):
        summary = usage["summary"]
        show_detail_card(
            f"Usage · last {days} days",
            {
                "Requests": f"{summary.get('request_count', 0):,}",
                "Tokens": f"{summary.get('total_tokens', 0):,}",
                "Spend": _money(summary.get("spend_usd")),
                "Savings": _money(summary.get("cost_savings_usd")),
                "Errors": summary.get("error_count")
                if summary.get("error_count") is not None
                else "Unavailable",
                "P95 latency (ms)": summary.get("p95_latency_ms")
                if summary.get("p95_latency_ms") is not None
                else "Unavailable",
            },
            plain=True,
        )
        models = usage.get("model_stats") or []
        if models:
            print_table(
                ["Model", "Requests", "Tokens", "Spend"],
                [
                    {
                        "Model": row.get("model_display_name") or row["model"],
                        "Requests": str(row.get("request_count", "—")),
                        "Tokens": str(row.get("total_tokens", "—")),
                        "Spend": _money(row.get("spend_usd")),
                    }
                    for row in models
                ],
            )
    else:
        click.echo(
            f"Usage · last {days} days: unavailable (analytics not configured or unreachable)."
        )
    strategies = result.get("strategies")
    if isinstance(strategies, dict):
        names = [
            ("Flex", "allow_flex_tier_default"),
            ("Switchyard", "switchyard_routing_enabled"),
            ("Jev Auto Routing", "auto_routing_enabled"),
        ]
        rows = {"Source": _strategy_source(strategies)}
        rows.update(
            {name: "On" if strategies.get(field) else "Off" for name, field in names}
        )
        show_detail_card("Strategies", rows, plain=True)
    else:
        click.echo("Strategies: unavailable.")


def _strategy_source(strategies: dict) -> str:
    profile = strategies.get("routing_profile")
    if isinstance(profile, dict) and profile.get("name"):
        return f"Profile: {_single_line(str(profile['name']))}"
    return "Account defaults"


def register_router_user_commands(
    group: click.Group,
    *,
    picker_style: PickerStyle = None,
    picker_pointer: str = "▶",
    copy_to_clipboard: Callable[[str], bool] = lambda _text: False,
) -> None:
    @group.command(
        "login",
        help="Connect a Router web-user session (separate from inference keys).",
    )
    @click.option(
        "--ui-url", help="Router UI origin; HTTP is allowed only for localhost."
    )
    @click.option("--no-browser", is_flag=True)
    @click.option("--timeout", type=click.IntRange(1, 900), default=900, hidden=True)
    @click.pass_context
    def login(ctx, ui_url, no_browser, timeout):
        if ctx.obj.get("no_input"):
            raise click.UsageError(
                "User login requires browser interaction; remove --no-input."
            )
        if router_ui.launch(
            ctx,
            "account",
            origin=ui_url,
            sign_in=True,
            no_browser=no_browser,
            timeout=timeout,
        ):
            return
        _output_session(
            ctx,
            _client(ctx, ui_url).login(no_browser=no_browser, timeout=timeout),
            title="Signed in to Router",
        )

    @group.command(
        "status", help="Show Router user-login status without exposing credentials."
    )
    @click.pass_context
    def status(ctx):
        _output_session(ctx, _client(ctx).status(), title="Router account")

    @group.command(
        "usage",
        help="Show account spend, savings, tokens, latency, and credits, as the "
        "workspace home page does. Without --days, picks the longest of 7, 3, or "
        "1 days that your usage history fills.",
    )
    @click.option(
        "--days",
        type=click.Choice(["1", "3", "7", "30"]),
        default=None,
        help="Usage window in days, ending today.",
    )
    @click.option(
        "--by",
        "group_by",
        type=click.Choice(["model", "key"]),
        default="model",
        show_default=True,
        help="Break daily usage down by model or API key.",
    )
    @click.pass_context
    def usage(ctx, days, group_by):
        # The service imports router commands, so it can't load with this module.
        from ramp_cli.router_ui.service import RouterService  # noqa: PLC0415

        result = RouterService(dict(ctx.obj)).account(
            int(days) if days else None, group_by
        )
        if not result["session"].get("authenticated"):
            raise RampCLIError("Not signed in to Router. Run 'ramp router login'.")
        if "usage_error" in result:
            raise RampCLIError(result["usage_error"])
        data = {
            **result["usage"],
            "credits": _credits(result.get("billing"), result.get("origin")),
        }
        if not _human(ctx):
            _output(ctx, data)
            return
        summary = data["summary"]
        window = "today" if data["days"] == 1 else f"last {data['days']} days"
        latency = [
            f"{summary[f'{name}_latency_ms']:,.0f}ms {name}"
            for name in ("p50", "p95")
            if isinstance(summary.get(f"{name}_latency_ms"), int | float)
        ]
        show_detail_card(
            f"Router usage · {window}",
            {
                "Spent": _money(summary.get("spend_usd")),
                "Saved": _money(summary.get("cost_savings_usd")),
                "Tokens": f"{summary.get('total_tokens') or 0:,}",
                "Requests": f"{summary.get('request_count') or 0:,}",
                "Latency": " · ".join(latency) or "Unavailable",
                "Credits": data["credits"],
            },
            plain=True,
        )
        if data["models"]:
            print_table(
                ["Model", "Requests", "Tokens", "Spend"],
                [
                    {
                        "Model": model["label"],
                        "Requests": f"{model['request_count']:,}",
                        "Tokens": f"{model['total_tokens']:,}",
                        "Spend": _money(model["spend_usd"]),
                    }
                    for model in data["models"]
                ],
            )

    @group.command(
        "logout",
        help="Revoke this CLI's Router login; leave inference keys and the browser alone.",
    )
    @click.pass_context
    def logout(ctx):
        client = _client(ctx)
        client.logout()
        if not _human(ctx):
            _output(ctx, {"success": True})
            return
        show_detail_card(
            "Signed out of Router",
            {
                "Router": _single_line(_router_host(client.origin)),
                "Status": "Signed out",
            },
            plain=True,
        )
        click.echo("Your API keys and browser session are unchanged.")

    @group.group(
        "keys",
        invoke_without_command=True,
        no_args_is_help=False,
        help="Inspect and manage your API keys, usage, and routing strategies.",
    )
    @click.option(
        "--days",
        type=click.IntRange(1, 90),
        default=7,
        show_default=True,
        help="Usage window for the selected key.",
    )
    @click.pass_context
    def keys(ctx, days):
        if ctx.invoked_subcommand is not None:
            return
        if router_ui.launch(ctx, "keys", days=days):
            return
        client = _client(ctx)
        entries = _all_keys(client)
        if not _interactive_keys(ctx):
            _output_keys(ctx, {"data": entries, "has_more": False}, offset=0)
            return
        # The picker lists every key, so a table above it would only repeat it.
        # Reuse the existing Router picker's brand and keyboard treatment.
        # questionary substitutes a Choice's title when its value is None, so
        # navigation entries use explicit sentinels rather than None.
        while True:
            by_id = {key["id"]: key for key in entries}
            count = len(entries)
            selected = _ask(
                questionary.select(
                    f"Select an API key ({count} {'key' if count == 1 else 'keys'})"
                    if entries
                    else "You don't have any API keys yet",
                    choices=[
                        questionary.Choice(
                            f"{_single_line(key.get('name') or '(unnamed)')} · {key['id']} · {'Enabled' if key.get('enabled') else 'Disabled'}",
                            value=key["id"],
                        )
                        for key in entries
                    ]
                    + [
                        questionary.Choice("Create a new key", value=_PICKER_CREATE),
                        questionary.Choice("Back", value=_PICKER_BACK),
                    ],
                    style=resolve_picker_style(picker_style),
                    pointer=picker_pointer,
                    instruction="(↑/↓ choose, enter inspect, Esc exit)",
                )
            )
            if selected == _PICKER_CREATE:
                created = _create_key(
                    ctx,
                    client,
                    name=None,
                    routing_profile=None,
                    show_key=False,
                    dry_run=False,
                    copy_to_clipboard=copy_to_clipboard,
                    picker_style=resolve_picker_style(picker_style),
                    picker_pointer=picker_pointer,
                )
                if created is not None:
                    entries = _all_keys(client)
                click.echo()
                continue
            key = by_id.get(selected) if selected != _PICKER_BACK else None
            if key is None:
                return
            if not _manage_key(
                ctx,
                client,
                key,
                days,
                style=resolve_picker_style(picker_style),
                pointer=picker_pointer,
            ):
                return
            entries = _all_keys(client)
            click.echo()

    @keys.command("rename", help="Rename an API key without rotating its secret.")
    @click.argument("key_id")
    @click.option(
        "--name", required=True, help=f"Key name (1-{_KEY_NAME_MAX} characters)."
    )
    @click.option(
        "--dry-run", is_flag=True, help="Print the request without sending it."
    )
    @click.pass_context
    def rename_key(ctx, key_id, name, dry_run):
        client = _client(ctx)
        _mutate_key(
            ctx,
            client,
            _owned_key(client, key_id),
            "rename",
            name=name,
            dry_run=dry_run,
        )

    @keys.command(
        "set-strategy", help="Assign only this key to an existing routing profile."
    )
    @click.argument("key_id")
    @click.option(
        "--routing-profile", required=True, help="Routing profile name or ID."
    )
    @click.option(
        "--dry-run", is_flag=True, help="Print the request without sending it."
    )
    @click.pass_context
    def set_key_strategy(ctx, key_id, routing_profile, dry_run):
        client = _client(ctx)
        _mutate_key(
            ctx,
            client,
            _owned_key(client, key_id),
            "set-strategy",
            routing_profile=routing_profile,
            dry_run=dry_run,
        )

    def register_key_lock_command(action: str) -> None:
        @keys.command(action, help=f"{action.title()} an API key without deleting it.")
        @click.argument("key_id")
        @click.option(
            "--dry-run", is_flag=True, help="Print the request without sending it."
        )
        @click.pass_context
        def change_lock(ctx, key_id, dry_run):
            client = _client(ctx)
            _mutate_key(
                ctx, client, _owned_key(client, key_id), action, dry_run=dry_run
            )

    register_key_lock_command("lock")
    register_key_lock_command("unlock")

    @keys.command(
        "show", help="Inspect one of your keys, including usage and strategies."
    )
    @click.argument("key_id")
    @click.option("--days", type=click.IntRange(1, 90), default=7, show_default=True)
    @click.pass_context
    def show_key(ctx, key_id, days):
        client = _client(ctx)
        _output_key_details(ctx, _key_details(client, _owned_key(client, key_id), days))

    @keys.command("create", help="Create an API key and choose its routing strategy.")
    @click.option("--name", help=f"Key name (1-{_KEY_NAME_MAX} characters).")
    @click.option(
        "--routing-profile",
        help="Routing profile name or ID. Defaults to your default profile.",
    )
    @click.option(
        "--show-key",
        is_flag=True,
        help="Print the new API key instead of copying it to the clipboard.",
    )
    @click.option(
        "--dry-run", is_flag=True, help="Print the request without sending it."
    )
    @click.pass_context
    def create_key(ctx, name, routing_profile, show_key, dry_run):
        if (
            name is None
            and not dry_run
            and router_ui.launch(ctx, "create-key", routing_profile=routing_profile)
        ):
            return
        _create_key(
            ctx,
            _client(ctx),
            name=name,
            routing_profile=routing_profile,
            show_key=show_key,
            dry_run=dry_run,
            copy_to_clipboard=copy_to_clipboard,
            picker_style=resolve_picker_style(picker_style),
            picker_pointer=picker_pointer,
        )

    @keys.command("list")
    @click.option("--limit", type=click.IntRange(1, 100), default=50)
    @click.option("--offset", type=click.IntRange(0, 10000), default=0)
    @click.pass_context
    def list_keys(ctx, limit, offset):
        _output_keys(
            ctx,
            _client(ctx).get(
                "/client/api-keys", params={"limit": limit, "offset": offset}
            ),
            offset=offset,
        )

    @keys.command(
        "strategies", help="Read effective strategies for one of your API keys."
    )
    @click.argument("key_id")
    @click.pass_context
    def strategies(ctx, key_id):
        if (
            not key_id
            or any(char in key_id for char in "/?#\\")
            or key_id in (".", "..")
        ):
            raise click.BadParameter("Invalid API key ID")
        client = _client(ctx)
        _owned_key(client, key_id)
        _output(ctx, _key_strategies(client, key_id))
