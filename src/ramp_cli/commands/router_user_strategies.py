"""User-level Router routing profiles over the same endpoints Router web uses.

Profiles are what the web calls strategies: a named set of routing settings
and the keys that route by it. Every key always belongs to exactly one profile,
so removing a key from a profile moves it back to the account default.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import click
import httpx
import questionary

from ramp_cli.client.router_user import RouterUserClient
from ramp_cli.commands.router_user import (
    _all_keys,
    _ask,
    _client,
    _human,
    _interactive_keys,
    _output,
    resolve_picker_style,
)
from ramp_cli.errors import ApiError, RampCLIError
from ramp_cli.output.formatter import print_agent_json, print_json, print_table
from ramp_cli.output.style import show_detail_card
from ramp_cli.router_ui import launch as router_ui

if TYPE_CHECKING:
    from ramp_cli.commands.router_user import PickerStyle

# Settings the CLI edits, labelled like the existing strategy toggles. Shadow
# models and provider preference stay web-only: shadowing needs the content
# recording consent flow that only the web app presents.
PROFILE_SETTINGS = (
    ("cost_efficient_routing_enabled", "Cost-efficient routing"),
    ("switchyard_routing_enabled", "NVIDIA NeMo Switchyard"),
    ("auto_routing_enabled", "Jev Auto Routing"),
)
# Switchyard and auto routing are mutually exclusive on the server.
_EXCLUSIVE = {
    "switchyard_routing_enabled": "auto_routing_enabled",
    "auto_routing_enabled": "switchyard_routing_enabled",
}
_NAME_MAX = 64
_ASSIGN_BATCH = 100
_BACK = "__profiles_back__"
_CREATE = "__profiles_create__"
_SAVE = "__profiles_save__"
_DELETE = "__profiles_delete__"
_NAME = "__profiles_name__"
_KEYS = "__profiles_keys__"
AUTO_ROUTING_CHOICES = {"on": True, "off": False, "account": None}


@dataclass
class ProfileDraft:
    """Unsaved edits to one profile; nothing is sent until save."""

    profile_id: str | None
    is_default: bool
    name: str
    settings: dict[str, bool | None]
    keys: set[str]
    original_name: str = ""
    original_settings: dict[str, bool | None] = field(default_factory=dict)
    original_keys: set[str] = field(default_factory=set)

    @classmethod
    def new(cls) -> "ProfileDraft":
        return cls(
            profile_id=None,
            is_default=False,
            name="",
            settings={
                "cost_efficient_routing_enabled": False,
                "switchyard_routing_enabled": False,
                "auto_routing_enabled": None,
            },
            keys=set(),
        )

    @classmethod
    def from_profile(cls, profile: dict) -> "ProfileDraft":
        settings = {
            "cost_efficient_routing_enabled": bool(
                profile.get("cost_efficient_routing_enabled")
            ),
            "switchyard_routing_enabled": bool(
                profile.get("switchyard_routing_enabled")
            ),
            "auto_routing_enabled": profile.get("auto_routing_enabled"),
        }
        keys = set(profile.get("assigned_api_key_ids") or [])
        name = str(profile.get("name") or "")
        return cls(
            profile_id=str(profile["id"]),
            is_default=bool(profile.get("is_default")),
            name=name,
            settings=dict(settings),
            keys=set(keys),
            original_name=name,
            original_settings=dict(settings),
            original_keys=set(keys),
        )

    def set_setting(self, field_name: str, value: bool | None) -> None:
        self.settings[field_name] = value
        other = _EXCLUSIVE.get(field_name)
        if value and other and self.settings.get(other):
            self.settings[other] = False

    def rebase(self, profile: dict) -> None:
        """Refresh the saved baseline without discarding the user's desired edits."""
        added, removed = self.added_keys, self.removed_keys
        saved = self.from_profile(profile)
        # Carry only the user's membership edits onto the fresh server state.
        # Untouched keys may have been moved by another command while editing.
        self.keys = (saved.keys | added) - removed
        self.profile_id = saved.profile_id
        self.is_default = saved.is_default
        self.original_name = saved.name
        self.original_settings = saved.settings
        self.original_keys = saved.keys

    @property
    def added_keys(self) -> set[str]:
        return self.keys - self.original_keys

    @property
    def removed_keys(self) -> set[str]:
        return self.original_keys - self.keys

    def patch_body(self) -> dict:
        body: dict = {}
        if self.profile_id is not None and self.name != self.original_name:
            body["name"] = self.name
        for field_name, _title in PROFILE_SETTINGS:
            if self.settings[field_name] != self.original_settings.get(field_name):
                body[field_name] = self.settings[field_name]
        return body

    def create_body(self) -> dict:
        body: dict = {"name": self.name}
        for field_name, _title in PROFILE_SETTINGS:
            if self.settings[field_name] is not None:
                body[field_name] = self.settings[field_name]
        return body

    def changes(self) -> list[str]:
        if self.profile_id is None:
            return ["New strategy"] if self.name else []
        described = []
        if self.name != self.original_name:
            described.append("name")
        described += [
            title
            for field_name, title in PROFILE_SETTINGS
            if self.settings[field_name] != self.original_settings.get(field_name)
        ]
        if self.added_keys or self.removed_keys:
            described.append("API keys")
        return described


def setting_value(field_name: str, value: bool | None) -> str:
    if value is None:
        return "Account default"
    return "On" if value else "Off"


def profile_summary(profile: dict) -> str:
    enabled = [
        title for field_name, title in PROFILE_SETTINGS if profile.get(field_name)
    ]
    return ", ".join(enabled) if enabled else "No strategies on"


def api_error_message(error: ApiError) -> str:
    try:
        parsed = json.loads(error.body)
        message = (parsed.get("error") or {}).get("message") or parsed.get("detail")
        if isinstance(message, str):
            return message
    except (ValueError, AttributeError):
        pass
    return str(error)


def _profiles(client: RouterUserClient) -> list[dict]:
    try:
        return client.get("/client/routing-profiles").get("data") or []
    except ApiError as error:
        if error.status_code == 403:
            raise RampCLIError(
                "Strategies aren't available for your Router account yet."
            ) from None
        raise


def _match(profiles: list[dict], reference: str, *, param: str) -> dict:
    wanted = reference.strip()
    for profile in profiles:
        if str(profile.get("id")) == wanted:
            return profile
    matches = [
        p for p in profiles if str(p.get("name") or "").casefold() == wanted.casefold()
    ]
    if len(matches) == 1:
        return matches[0]
    names = ", ".join(str(p.get("name")) for p in profiles) or "none"
    raise click.BadParameter(
        f"No strategy named '{wanted}'. Available: {names}.", param_hint=param
    )


def _default_profile(profiles: list[dict]) -> dict:
    default = next((p for p in profiles if p.get("is_default")), None)
    if default is None:
        raise RampCLIError("Router did not return a default strategy.")
    return default


def _validate_name(value: str) -> str | bool:
    name = value.strip()
    if not name:
        return "Enter a name."
    if len(name) > _NAME_MAX:
        return f"Use {_NAME_MAX} characters or fewer."
    if name.casefold() == "account default":
        return "That name is reserved for your default strategy."
    return True


def plan_requests(draft: ProfileDraft, default_id: str | None) -> list[dict]:
    """Every request a save sends, in order; also the --dry-run output."""
    requests: list[dict] = []
    target = draft.profile_id or "{new_profile_id}"
    if draft.profile_id is None:
        requests.append(
            {
                "method": "POST",
                "path": "/client/routing-profiles",
                "body": draft.create_body(),
            }
        )
    elif body := draft.patch_body():
        requests.append(
            {
                "method": "PATCH",
                "path": f"/client/routing-profiles/{target}",
                "body": body,
            }
        )
    added = sorted(draft.added_keys)
    for start in range(0, len(added), _ASSIGN_BATCH):
        requests.append(
            {
                "method": "POST",
                "path": f"/client/routing-profiles/{target}/assignments",
                "body": {"api_key_ids": added[start : start + _ASSIGN_BATCH]},
            }
        )
    removed = sorted(draft.removed_keys)
    if removed and default_id is None:
        raise RampCLIError("Router did not return a default strategy.")
    for start in range(0, len(removed), _ASSIGN_BATCH):
        requests.append(
            {
                "method": "POST",
                "path": f"/client/routing-profiles/{default_id}/assignments",
                "body": {"api_key_ids": removed[start : start + _ASSIGN_BATCH]},
            }
        )
    return requests


def save_draft(
    client: RouterUserClient, draft: ProfileDraft, default_id: str | None
) -> dict:
    """Send the draft: settings first, then key moves. Returns the saved profile."""
    if draft.is_default and draft.removed_keys:
        raise click.UsageError(
            "Keys leave the default strategy by joining another strategy."
        )
    removed = sorted(draft.removed_keys)
    if removed and default_id is None:
        raise RampCLIError("Router did not return a default strategy.")
    profile: dict | None = None
    try:
        if draft.profile_id is None:
            profile = client.post("/client/routing-profiles", json=draft.create_body())
            # A later assignment failure must never turn a retry into another
            # create. Retain the ID as soon as creation is confirmed.
            draft.profile_id = str(profile["id"])
        elif body := draft.patch_body():
            profile = client.patch(
                f"/client/routing-profiles/{draft.profile_id}", json=body
            )
        added = sorted(draft.added_keys)
        for start in range(0, len(added), _ASSIGN_BATCH):
            profile = client.post(
                f"/client/routing-profiles/{draft.profile_id}/assignments",
                json={"api_key_ids": added[start : start + _ASSIGN_BATCH]},
            )
        for start in range(0, len(removed), _ASSIGN_BATCH):
            client.post(
                f"/client/routing-profiles/{default_id}/assignments",
                json={"api_key_ids": removed[start : start + _ASSIGN_BATCH]},
            )
        if removed or profile is None:
            profile = client.get(f"/client/routing-profiles/{draft.profile_id}")
    except (ApiError, httpx.TransportError):
        if draft.profile_id is not None:
            try:
                draft.rebase(client.get(f"/client/routing-profiles/{draft.profile_id}"))
            except (ApiError, RampCLIError, httpx.HTTPError) as error:
                raise RampCLIError(
                    "Some strategy changes may already be saved, but Router's "
                    "current state could not be reloaded. Editing stopped; "
                    "reopen the strategy and review it before saving again."
                ) from error
        raise
    draft.rebase(profile)
    return profile


# --- Interactive editor ----------------------------------------------------


def _key_label(key: dict, owner: str | None) -> str:
    name = str(key.get("name") or "(unnamed)")
    status = "" if key.get("enabled") else " · Disabled"
    suffix = f" · now on {owner}" if owner else ""
    return f"{name} · {key['id']}{status}{suffix}"


def _other_owner(
    owners: dict[str, tuple[str, str]], key_id: str, profile_id: str | None
) -> str | None:
    """The strategy a key would move away from, if it's not this one."""
    owner = owners.get(key_id)
    return owner[1] if owner and owner[0] != profile_id else None


def _pick_keys(
    draft: ProfileDraft,
    keys: list[dict],
    owners: dict[str, tuple[str, str]],
    *,
    style: questionary.Style | None,
    pointer: str,
) -> None:
    if not keys:
        click.echo("You don't have any API keys yet.")
        return
    selected = _ask(
        questionary.checkbox(
            f"API keys using {draft.name or 'this strategy'}",
            choices=[
                questionary.Choice(
                    _key_label(key, _other_owner(owners, key["id"], draft.profile_id)),
                    value=key["id"],
                    checked=key["id"] in draft.keys,
                )
                for key in keys
            ],
            style=style,
            pointer=pointer,
            instruction="(space to toggle, enter to confirm, Esc to cancel)",
        )
    )
    if selected is None:
        return
    chosen = set(selected)
    if draft.is_default and not draft.original_keys <= chosen:
        click.echo(
            "Keys leave the default strategy by joining another strategy; "
            "kept them selected."
        )
        chosen |= draft.original_keys
    draft.keys = chosen


def _edit_profile(
    client: RouterUserClient,
    draft: ProfileDraft,
    keys: list[dict],
    profiles: list[dict],
    *,
    style: questionary.Style | None,
    pointer: str,
) -> None:
    default_id = str(_default_profile(profiles)["id"]) if profiles else None
    owners = {
        key_id: (str(p["id"]), str(p.get("name") or ""))
        for p in profiles
        for key_id in p.get("assigned_api_key_ids") or []
    }
    if draft.profile_id is None:
        name = _ask(
            questionary.text("Strategy name", validate=_validate_name, style=style)
        )
        if name is None:
            return
        draft.name = name.strip()
    while True:
        pending = draft.changes()
        title = "New strategy" if draft.profile_id is None else draft.original_name
        choices: list = [
            questionary.Choice(
                f"Name: {draft.name}",
                value=_NAME,
                disabled="Default strategy can't be renamed"
                if draft.is_default
                else None,
            )
        ]
        choices += [
            questionary.Choice(
                f"{label}: {setting_value(field_name, draft.settings[field_name])}",
                value=field_name,
            )
            for field_name, label in PROFILE_SETTINGS
        ]
        count = len(draft.keys)
        choices.append(
            questionary.Choice(
                f"API keys: {count} {'key' if count == 1 else 'keys'}", value=_KEYS
            )
        )
        choices.append(questionary.Separator())
        choices.append(
            questionary.Choice(
                f"Save changes ({', '.join(pending)})" if pending else "Save changes",
                value=_SAVE,
                disabled=None if pending else "No changes yet",
            )
        )
        if draft.profile_id is not None and not draft.is_default:
            choices.append(questionary.Choice("Delete strategy", value=_DELETE))
        choices.append(questionary.Choice("Back", value=_BACK))
        action = _ask(
            questionary.select(
                f"Edit {title}",
                choices=choices,
                style=style,
                pointer=pointer,
                instruction="(↑/↓ choose, enter change, Esc back)",
            )
        )
        if action in (None, _BACK):
            if pending and not _ask(
                questionary.confirm(
                    "Discard unsaved changes?", default=False, style=style
                )
            ):
                continue
            return
        if action == _NAME:
            name = _ask(
                questionary.text(
                    "Strategy name",
                    default=draft.name,
                    validate=_validate_name,
                    style=style,
                )
            )
            if name is not None:
                draft.name = name.strip()
        elif action == "auto_routing_enabled":
            current = draft.settings[action]
            chosen = _ask(
                questionary.select(
                    "Jev Auto Routing",
                    choices=[
                        questionary.Choice("Account default", value="account"),
                        questionary.Choice("On", value="on"),
                        questionary.Choice("Off", value="off"),
                    ],
                    default={None: "account", True: "on", False: "off"}[current],
                    style=style,
                    pointer=pointer,
                )
            )
            if chosen is not None:
                draft.set_setting(action, AUTO_ROUTING_CHOICES[chosen])
        elif action in dict(PROFILE_SETTINGS):
            draft.set_setting(action, not draft.settings[action])
        elif action == _KEYS:
            _pick_keys(draft, keys, owners, style=style, pointer=pointer)
        elif action == _SAVE:
            try:
                saved = save_draft(client, draft, default_id)
            except ApiError as error:
                click.secho(f"Couldn't save: {api_error_message(error)}", fg="red")
                if draft.profile_id is not None:
                    click.echo(
                        "Some changes may already be saved. Reloaded the server "
                        "state; review the remaining changes before retrying."
                    )
                continue
            click.echo(f"Saved {saved.get('name') or draft.name}.")
            return
        elif action == _DELETE:
            moved = len(draft.original_keys)
            note = (
                f" Its {moved} {'key' if moved == 1 else 'keys'} will move to your "
                "default strategy."
                if moved
                else ""
            )
            if not _ask(
                questionary.confirm(
                    f"Delete {draft.original_name}?{note}", default=False, style=style
                )
            ):
                continue
            try:
                delete_profile(client, draft.profile_id, default_id, moved)
            except ApiError as error:
                click.secho(f"Couldn't delete: {api_error_message(error)}", fg="red")
                continue
            click.echo(f"Deleted {draft.original_name}.")
            return


def delete_profile(
    client: RouterUserClient, profile_id: str, default_id: str | None, assigned: int
) -> None:
    params = {"reassign_to": default_id} if assigned and default_id else None
    client.delete(f"/client/routing-profiles/{profile_id}", params=params)


def run_profiles_picker(
    client: RouterUserClient,
    all_keys: Callable[[RouterUserClient], list[dict]],
    *,
    style: questionary.Style | None,
    pointer: str,
) -> None:
    while True:
        profiles = _profiles(client)
        keys = all_keys(client)
        choice = _ask(
            questionary.select(
                "Choose a strategy",
                choices=[
                    questionary.Choice(
                        f"{p.get('name')}{' (default)' if p.get('is_default') else ''}"
                        f" · {profile_summary(p)}"
                        f" · {len(p.get('assigned_api_key_ids') or [])} keys",
                        value=str(p["id"]),
                    )
                    for p in profiles
                ]
                + [
                    questionary.Separator(),
                    questionary.Choice("Create a new strategy", value=_CREATE),
                    questionary.Choice("Back", value=_BACK),
                ],
                style=style,
                pointer=pointer,
                instruction="(↑/↓ choose, enter edit, Esc exit)",
            )
        )
        if choice in (None, _BACK):
            return
        draft = (
            ProfileDraft.new()
            if choice == _CREATE
            else ProfileDraft.from_profile(
                next(p for p in profiles if str(p["id"]) == choice)
            )
        )
        _edit_profile(client, draft, keys, profiles, style=style, pointer=pointer)
        click.echo()


def show_profiles(profiles: list[dict]) -> None:
    click.echo(f"{len(profiles)} {'strategy' if len(profiles) == 1 else 'strategies'}")
    if profiles:
        print_table(
            ["Name", "Strategies", "Keys", "ID"],
            [
                {
                    "Name": f"{p.get('name')}{' (default)' if p.get('is_default') else ''}",
                    "Strategies": profile_summary(p),
                    "Keys": str(len(p.get("assigned_api_key_ids") or [])),
                    "ID": str(p.get("id")),
                }
                for p in profiles
            ],
        )


def show_profile(profile: dict) -> None:
    fields = {
        "Name": str(profile.get("name") or ""),
        "Default": "Yes" if profile.get("is_default") else "No",
    }
    fields.update(
        {
            label: setting_value(field_name, profile.get(field_name))
            for field_name, label in PROFILE_SETTINGS
        }
    )
    fields["API keys"] = str(len(profile.get("assigned_api_key_ids") or []))
    show_detail_card("Strategy", fields, plain=True)


# --- Commands --------------------------------------------------------------


def _apply_flags(
    draft: ProfileDraft,
    *,
    cost_efficient: bool | None,
    switchyard: bool | None,
    auto_routing: str | None,
) -> None:
    if switchyard and auto_routing == "on":
        raise click.UsageError(
            "NVIDIA NeMo Switchyard and auto routing can't both be on."
        )
    if cost_efficient is not None:
        draft.set_setting("cost_efficient_routing_enabled", cost_efficient)
    if switchyard is not None:
        draft.set_setting("switchyard_routing_enabled", switchyard)
    if auto_routing is not None:
        draft.set_setting("auto_routing_enabled", AUTO_ROUTING_CHOICES[auto_routing])


def _owned_key_ids(
    all_keys: list[dict], requested: tuple[str, ...], param: str
) -> set[str]:
    owned = {key["id"] for key in all_keys}
    missing = [key_id for key_id in requested if key_id not in owned]
    if missing:
        raise click.BadParameter(
            f"Not one of your API keys: {', '.join(missing)}", param_hint=param
        )
    return set(requested)


def _setting_options(command: Callable) -> Callable:
    command = click.option(
        "--auto-routing",
        type=click.Choice(tuple(AUTO_ROUTING_CHOICES), case_sensitive=False),
        help="Jev Auto Routing: on, off, or follow your account default.",
    )(command)
    command = click.option(
        "--switchyard/--no-switchyard",
        default=None,
        help="Turn NVIDIA NeMo Switchyard on or off.",
    )(command)
    command = click.option(
        "--cost-efficient/--no-cost-efficient",
        default=None,
        help="Turn cost-efficient routing on or off.",
    )(command)
    return command


def output_profiles(ctx: click.Context, profiles: list[dict]) -> None:
    if _human(ctx):
        show_profiles(profiles)
    elif ctx.obj.get("agent_mode"):
        print_agent_json(profiles)
    else:
        print_json({"data": profiles})


def output_profile(
    ctx: click.Context, profile: dict, message: str | None = None
) -> None:
    if _human(ctx):
        if message:
            click.echo(message)
        show_profile(profile)
    else:
        _output(ctx, profile)


def dry_run_output(
    ctx: click.Context, draft: ProfileDraft, profiles: list[dict]
) -> None:
    default_id = str(_default_profile(profiles)["id"]) if profiles else None
    requests = plan_requests(draft, default_id)
    if ctx.obj.get("agent_mode"):
        print_agent_json(requests)
    else:
        print_json({"requests": requests})


def user_login_active(ctx: click.Context) -> bool:
    """Whether this CLI holds a Router user login (no network, never prompts)."""
    try:
        return bool(_client(ctx).status().get("authenticated"))
    except (RampCLIError, OSError, ValueError):
        return False


def use_account_strategies(ctx: click.Context) -> bool:
    """Read the Router login's strategies only when --account asks for them.

    Without it, every output mode keeps the stored-key contract, so a saved
    login never changes which account an existing command reads.
    """
    return bool(ctx.obj.get("strategies_account"))


def run_user_strategies(
    ctx: click.Context, *, style: questionary.Style | None, pointer: str
) -> None:
    """Bare 'ramp router strategies' for a signed-in user."""
    client = _client(ctx)
    if not _interactive_keys(ctx):
        output_profiles(ctx, _profiles(client))
        return
    run_profiles_picker(client, _all_keys, style=style, pointer=pointer)


def output_user_strategies(ctx: click.Context) -> None:
    output_profiles(ctx, _profiles(_client(ctx)))


def _reject_key_selectors(ctx: click.Context, command: str) -> None:
    """Account strategies belong to the Router login, never to a key or harness.

    'strategies --api-key KEY' and '--harness NAME' pick a key's account; acting
    on the saved login instead could change or delete another account's data.
    """
    state = ctx.obj or {}
    if state.get("strategies_api_key_from_flag") or state.get("strategies_base_url"):
        raise click.UsageError(
            f"'strategies {command}' works on your Router login's strategies and "
            "doesn't accept --api-key or --harness. Remove them, or use "
            "'strategies enable'/'disable' to change a key's own settings."
        )


def register_user_strategy_commands(
    group: click.Group,
    *,
    picker_style: PickerStyle = None,
    picker_pointer: str = "▶",
) -> None:
    """Add account-level create/show/edit/delete to 'ramp router strategies'."""

    @group.command("show", help="Show one routing strategy.")
    @click.argument("profile", metavar="STRATEGY")
    @click.pass_context
    def show(ctx, profile):
        _reject_key_selectors(ctx, "show")
        output_profile(ctx, _match(_profiles(_client(ctx)), profile, param="strategy"))

    @group.command("create", help="Create a routing strategy.")
    @click.option("--name", help=f"Strategy name (1-{_NAME_MAX} characters).")
    @_setting_options
    @click.option(
        "--key", "keys", multiple=True, help="API key ID to move onto it (repeatable)."
    )
    @click.option(
        "--dry-run", is_flag=True, help="Print the requests without sending them."
    )
    @click.pass_context
    def create(ctx, name, cost_efficient, switchyard, auto_routing, keys, dry_run):
        _reject_key_selectors(ctx, "create")
        if not any(
            (
                name,
                cost_efficient is not None,
                switchyard is not None,
                auto_routing is not None,
                keys,
                dry_run,
            )
        ) and router_ui.launch(ctx, "profile"):
            return
        client = _client(ctx)
        profiles = _profiles(client)
        flags_given = any(
            value is not None for value in (cost_efficient, switchyard, auto_routing)
        ) or bool(keys)
        if name is None and not flags_given and not dry_run and _interactive_keys(ctx):
            _edit_profile(
                client,
                ProfileDraft.new(),
                _all_keys(client),
                profiles,
                style=resolve_picker_style(picker_style),
                pointer=picker_pointer,
            )
            return
        if name is None:
            raise click.UsageError("Provide --name when prompts are unavailable.")
        valid = _validate_name(name)
        if valid is not True:
            raise click.BadParameter(str(valid), param_hint="--name")
        draft = ProfileDraft.new()
        draft.name = name.strip()
        _apply_flags(
            draft,
            cost_efficient=cost_efficient,
            switchyard=switchyard,
            auto_routing=auto_routing,
        )
        if keys:
            draft.keys = _owned_key_ids(_all_keys(client), keys, "--key")
        if dry_run:
            dry_run_output(ctx, draft, profiles)
            return
        default_id = str(_default_profile(profiles)["id"])
        output_profile(
            ctx, save_draft(client, draft, default_id), f"Created {draft.name}."
        )

    @group.command("edit", help="Change a routing strategy or which keys use it.")
    @click.argument("profile", metavar="STRATEGY")
    @click.option("--name", help="Rename the strategy.")
    @_setting_options
    @click.option(
        "--add-key", "add_keys", multiple=True, help="API key ID to move onto it."
    )
    @click.option(
        "--remove-key",
        "remove_keys",
        multiple=True,
        help="API key ID to move back to your default strategy.",
    )
    @click.option(
        "--dry-run", is_flag=True, help="Print the requests without sending them."
    )
    @click.pass_context
    def edit(
        ctx,
        profile,
        name,
        cost_efficient,
        switchyard,
        auto_routing,
        add_keys,
        remove_keys,
        dry_run,
    ):
        _reject_key_selectors(ctx, "edit")
        if (
            not any(
                value is not None
                for value in (name, cost_efficient, switchyard, auto_routing)
            )
            and not (add_keys or remove_keys or dry_run)
            and router_ui.launch(ctx, "profile", profile_id=profile)
        ):
            return
        client = _client(ctx)
        profiles = _profiles(client)
        draft = ProfileDraft.from_profile(_match(profiles, profile, param="strategy"))
        flags_given = any(
            value is not None
            for value in (name, cost_efficient, switchyard, auto_routing)
        ) or bool(add_keys or remove_keys)
        if not flags_given and not dry_run:
            if not _interactive_keys(ctx):
                raise click.UsageError(
                    "Pass at least one change, e.g. --cost-efficient."
                )
            _edit_profile(
                client,
                draft,
                _all_keys(client),
                profiles,
                style=resolve_picker_style(picker_style),
                pointer=picker_pointer,
            )
            return
        if name is not None:
            if draft.is_default:
                raise click.UsageError("The default strategy can't be renamed.")
            valid = _validate_name(name)
            if valid is not True:
                raise click.BadParameter(str(valid), param_hint="--name")
            draft.name = name.strip()
        _apply_flags(
            draft,
            cost_efficient=cost_efficient,
            switchyard=switchyard,
            auto_routing=auto_routing,
        )
        if add_keys or remove_keys:
            owned = _all_keys(client)
            draft.keys |= _owned_key_ids(owned, add_keys, "--add-key")
            removing = _owned_key_ids(owned, remove_keys, "--remove-key")
            if draft.is_default and removing & draft.keys:
                raise click.UsageError(
                    "Keys leave the default strategy by joining another strategy."
                )
            draft.keys -= removing
        if dry_run:
            dry_run_output(ctx, draft, profiles)
            return
        if not draft.changes():
            output_profile(
                ctx, _match(profiles, profile, param="strategy"), "No changes."
            )
            return
        default_id = str(_default_profile(profiles)["id"])
        output_profile(
            ctx, save_draft(client, draft, default_id), f"Saved {draft.name}."
        )

    @group.command("delete", help="Delete a strategy; its keys move to your default.")
    @click.argument("profile", metavar="STRATEGY")
    @click.option("--yes", is_flag=True, help="Delete without asking for confirmation.")
    @click.pass_context
    def delete(ctx, profile, yes):
        _reject_key_selectors(ctx, "delete")
        if not yes and router_ui.launch(
            ctx, "profile", profile_id=profile, delete_on_load=True
        ):
            return
        client = _client(ctx)
        profiles = _profiles(client)
        target = _match(profiles, profile, param="strategy")
        if target.get("is_default"):
            raise click.UsageError("The default strategy can't be deleted.")
        assigned = len(target.get("assigned_api_key_ids") or [])
        if not yes:
            if not _interactive_keys(ctx):
                raise click.UsageError("Pass --yes to delete without a prompt.")
            note = (
                f" Its {assigned} keys will move to your default strategy."
                if assigned
                else ""
            )
            if not _ask(
                questionary.confirm(
                    f"Delete {target.get('name')}?{note}",
                    default=False,
                    style=resolve_picker_style(picker_style),
                )
            ):
                click.echo("Nothing deleted.")
                return
        delete_profile(
            client, str(target["id"]), str(_default_profile(profiles)["id"]), assigned
        )
        if _human(ctx):
            click.echo(f"Deleted {target.get('name')}.")
        else:
            _output(ctx, {"deleted": True, "id": target["id"], "moved_keys": assigned})
