"""The Strategies tab."""

import httpx
from textual import on
from textual.coordinate import Coordinate
from textual.widgets import (
    DataTable,
    Static,
)
from textual.widgets.data_table import CellDoesNotExist

from ramp_cli.commands.router_user_strategies import (
    AUTO_ROUTING_CHOICES,
    PROFILE_SETTINGS,
    ProfileDraft,
    _validate_name,
    setting_value,
)
from ramp_cli.errors import ApiError, RampCLIError
from ramp_cli.router_ui.pages import (
    CellEditing,
    CollectionPage,
)
from ramp_cli.router_ui.service import (
    safe_text,
)
from ramp_cli.router_ui.widgets import (
    Button,
    CellChecklist,
    CellInput,
    CellTable,
)


def parse_strategy_name(text: str) -> str:
    valid = _validate_name(text)
    if valid is not True:
        raise ValueError(str(valid))
    return text.strip()


def key_count(count: int) -> str:
    return f"{count} {'key' if count == 1 else 'keys'}"


class StrategyTable(CellTable):
    """A strategy's name opens rename and delete, except the default's."""

    first_cell_column = 0

    def cell_locked(self, coordinate: Coordinate) -> bool:
        if coordinate.column != 0:
            return False
        try:
            key = str(self.coordinate_to_cell_key(coordinate).row_key.value)
        except CellDoesNotExist:
            return False
        entries = getattr(self.screen, "entries", None) or []
        item = next((p for p in entries if str(p.get("id")) == key), None)
        return item is not None and bool(item.get("is_default"))


class StrategiesPage(CellEditing, CollectionPage):
    page_title = "strategies"
    columns = ("Name", "Cost-efficient", "Switchyard", "Jev Auto Routing", "API keys")
    table_class = StrategyTable
    cell_table_id = "table"
    # On/Off cells, by column, and the strategy setting each one changes.
    CELL_SETTINGS = {
        1: "cost_efficient_routing_enabled",
        2: "switchyard_routing_enabled",
        3: "auto_routing_enabled",
    }

    def __init__(self, target: str | None = None, intent: str = ""):
        # `ramp router strategies create/edit/delete` open here: "create"
        # asks for a name, "edit" selects the target, "delete" confirms it.
        super().__init__()
        self.target = target
        self.intent = intent
        self.auto_default: bool | None = None
        # None when the keys couldn't be listed; the page still works without.
        self.keys: list[dict] | None = []
        # A just-created strategy, selected once the reload lists it.
        self.select_name: str | None = None

    def key_hints(self) -> str:
        table = self.query("#table").first(CellTable) if self.is_mounted else None
        if table is not None and table.cursor_type == "cell":
            return "↑↓←→ move   ↵ change   esc rows"
        return self.hints("↑↓ move", "←→ buttons", "↵ edit cells")

    @on(CellTable.ModeChanged, "#table")
    def cell_mode_changed(self):
        self.query_one("#keys", Static).update(self.key_hints())

    @on(DataTable.CellSelected, "#table")
    def edit_cell(self, event: DataTable.CellSelected):
        event.stop()
        key = str(event.cell_key.row_key.value)
        profile = next((p for p in self.entries if str(p["id"]) == key), None)
        if profile is None or self.router_app.busy:
            return
        editor = {
            0: self.edit_name,
            1: self.edit_setting,
            2: self.edit_setting,
            3: self.edit_setting,
            4: self.edit_keys,
        }.get(event.coordinate.column)
        if editor:
            editor(profile, event.coordinate)

    def strategy_label(self, profile: dict) -> str:
        return safe_text(profile.get("name"))

    def edit_setting(self, profile: dict, coordinate: Coordinate):
        setting = self.CELL_SETTINGS[coordinate.column]
        value = profile.get(setting)
        if setting == "auto_routing_enabled":
            choices = [
                (inherited_label(self.auto_default), "account"),
                ("On", "on"),
                ("Off", "off"),
            ]
            current = "account" if value is None else "on" if value else "off"
        else:
            choices = [("On", "on"), ("Off", "off")]
            current = "on" if value else "off"

        def chosen(choice: str):
            draft = ProfileDraft.from_profile(profile)
            draft.set_setting(setting, AUTO_ROUTING_CHOICES[choice])
            label = {"on": "On", "off": "Off", "account": "Default"}[choice]
            title = dict(PROFILE_SETTINGS)[setting]
            self.confirm_save(
                profile,
                draft,
                setting,
                f"Set {title} to {label} for {self.strategy_label(profile)}?",
            )

        self.pick(coordinate, choices, current, chosen)

    def confirm_save(
        self, profile: dict, draft: ProfileDraft, setting: str, message: str
    ):
        if any(
            draft.settings[other] != draft.original_settings[other]
            for other in draft.settings
            if other != setting
        ):
            # Switchyard and Jev Auto Routing can't both be on.
            message += " This turns off the other one, which can't run alongside it."
        self.confirm_draft(profile, draft, message)

    def confirm_draft(self, profile: dict, draft: ProfileDraft, message: str):
        name = self.strategy_label(profile)
        self.router_app.confirm(
            message,
            lambda: self.run_and_reload(
                "Saving strategy",
                lambda: self.service.save_profile(draft),
                f"Saved {name}.",
            ),
            "Save",
            focus_accept=True,
        )

    def edit_keys(self, profile: dict, coordinate: Coordinate):
        if self.keys is None:
            self.message(
                "Couldn't list your API keys. Refresh to try again.", error=True
            )
            return
        if not self.keys:
            self.message("You don't have any API keys yet.")
            return
        draft = ProfileDraft.from_profile(profile)
        name = self.strategy_label(profile)
        owners = {
            str(key_id): (str(p["id"]), safe_text(p.get("name")))
            for p in self.entries
            for key_id in p.get("assigned_api_key_ids") or []
        }
        choices = []
        for key in self.keys:
            key_id = str(key["id"])
            label = safe_text(key.get("name") or key_id)
            if key.get("enabled") is False:
                label += " · Disabled"
            owner = owners.get(key_id)
            if owner and owner[0] != str(profile["id"]):
                label += f" · on {owner[1]}"
            # Keys leave the default strategy only by joining another one.
            locked = draft.is_default and key_id in draft.keys
            choices.append((label, key_id, key_id in draft.keys, locked))

        def done(chosen: set[str] | None):
            if chosen is None or chosen == draft.keys:
                return
            added, removed = chosen - draft.keys, draft.keys - chosen
            draft.keys = set(chosen)
            moves = []
            if added:
                moves.append(f"move {key_count(len(added))} to {name}")
            if removed:
                moves.append(
                    f"return {key_count(len(removed))} to your default strategy"
                )
            message = " and ".join(moves)
            self.confirm_draft(profile, draft, f"{message[0].upper()}{message[1:]}?")

        def show():
            if self.is_mounted and self.app.screen is self:
                self.app.push_screen(
                    CellChecklist(
                        choices, self.cell_anchor(coordinate), f"API keys using {name}"
                    ),
                    done,
                )

        self.call_after_refresh(show)

    def edit_name(self, profile: dict, coordinate: Coordinate):
        def chosen(value: str):
            if value == "rename":
                self.rename_strategy(profile, coordinate)
            elif value == "delete":
                self.delete_strategy(profile)

        self.pick(
            coordinate,
            [
                ("Rename Strategy?", "rename"),
                ("Delete Strategy?", "delete"),
                ("Back", "back"),
            ],
            None,
            chosen,
        )

    def rename_strategy(self, profile: dict, coordinate: Coordinate):
        name = self.strategy_label(profile)
        current = str(profile.get("name") or "")

        def chosen(new_name: str):
            if new_name == current.strip():
                return
            draft = ProfileDraft.from_profile(profile)
            draft.name = new_name
            self.router_app.confirm(
                f"Rename {name} to “{safe_text(new_name)}”?",
                lambda: self.run_and_reload(
                    "Renaming strategy",
                    lambda: self.service.save_profile(draft),
                    f"Renamed {name} to {safe_text(new_name)}.",
                ),
                "Rename",
            )

        def show():
            if self.is_mounted and self.app.screen is self:
                self.app.push_screen(
                    CellInput(
                        current,
                        self.cell_anchor(coordinate),
                        parse_strategy_name,
                        placeholder="Strategy name",
                        submit="Rename",
                    ),
                    lambda result: result and chosen(result[0]),
                )

        # After the menu closes, so the popover opens over the page itself.
        self.call_after_refresh(show)

    def delete_strategy(self, profile: dict):
        name = self.strategy_label(profile)
        moved = len(profile.get("assigned_api_key_ids") or [])
        # Router moves a deleted strategy's keys to the default one.
        note = (
            f" Its {key_count(moved)} will move to your default strategy."
            if moved
            else ""
        )
        self.router_app.confirm(
            f"Delete {name}?{note}",
            lambda: self.run_and_reload(
                "Deleting strategy",
                lambda: self.service.delete_profile(str(profile["id"])),
                f"Deleted {name}.",
            ),
            "Delete",
        )

    def load(self):
        def keys():
            try:
                return self.service.keys()
            except Exception:
                return None

        def loaded(result):
            entries, self.auto_default, self.keys = result
            self.loaded(entries)
            self.select_created()
            self.open_target()

        self.run(
            "Loading strategies",
            lambda: (
                self.service.profiles(),
                self.service.auto_routing_default(),
                keys(),
            ),
            loaded,
        )

    def select_row(self, key: str):
        table = self.query_one("#table", DataTable)
        if key in table.rows:
            table.move_cursor(row=table.get_row_index(key))

    def select_created(self):
        name, self.select_name = self.select_name, None
        if name is None:
            return
        created = next((p for p in self.entries if p.get("name") == name), None)
        if created is not None:
            self.select_row(str(created["id"]))

    def open_target(self):
        target, intent = self.target, self.intent
        self.target, self.intent = None, ""
        if intent == "create":
            self.call_after_refresh(self.create)
            return
        if target is None:
            return
        # As the CLI resolves it: an exact ID first, then one unambiguous
        # name, so a strategy named like another's ID never stands in for it.
        wanted = target.strip()
        profile = next((p for p in self.entries if str(p["id"]) == wanted), None)
        if profile is None:
            named = [
                p
                for p in self.entries
                if str(p.get("name") or "").casefold() == wanted.casefold()
            ]
            profile = named[0] if len(named) == 1 else None
        if profile is None:
            self.message(f"No strategy named '{safe_text(target)}'.", error=True)
            return
        self.select_row(str(profile["id"]))
        self.query_one("#table", DataTable).focus()
        if intent != "delete":
            return
        if profile.get("is_default"):
            self.message("Your default strategy can't be deleted.", error=True)
        else:
            self.call_after_refresh(lambda: self.delete_strategy(profile))

    def row(self, item):
        name = safe_text(item.get("name"))
        auto = item.get("auto_routing_enabled")
        return (
            f"{name} (default)" if item.get("is_default") else name,
            "On" if item.get("cost_efficient_routing_enabled") else "Off",
            "On" if item.get("switchyard_routing_enabled") else "Off",
            inherited_label(self.auto_default)
            if auto is None
            else setting_value("auto_routing_enabled", auto),
            str(len(item.get("assigned_api_key_ids") or [])),
        )

    @on(Button.Pressed, "#create")
    def create(self):
        if not self.is_mounted or self.app.screen is not self:
            return

        def chosen(result: tuple | None):
            if not result:
                return
            draft = ProfileDraft.new()
            draft.name = result[0]
            self.select_name = draft.name
            self.run_and_reload(
                "Creating strategy",
                lambda: self.create_profile(draft),
                f"Created {safe_text(draft.name)}. Change its settings in its row.",
            )

        self.app.push_screen(
            CellInput(
                "",
                self.query_one("#create", Button).region,
                parse_strategy_name,
                placeholder="Strategy name",
                submit="Create",
            ),
            chosen,
        )

    def create_profile(self, draft: ProfileDraft):
        try:
            return self.service.save_profile(draft)
        except (ApiError, httpx.TransportError) as error:
            if isinstance(error, ApiError) and error.status_code < 500:
                raise
            # It may have been created anyway; a blind retry could make two.
            raise RampCLIError(
                "Strategy creation could not be confirmed. Check your strategies "
                "before creating another; this request may already have saved."
            ) from error

    @on(Button.Pressed, "#legacy")
    def legacy(self):
        from ramp_cli.router_ui.tabs.harnesses import LegacyPage  # noqa: PLC0415

        self.router_app.push(LegacyPage())


def inherited_label(value: bool | None) -> str:
    """A strategy setting left to the account, showing what that resolves to."""
    return "Default" if value is None else f"Default ({'On' if value else 'Off'})"
