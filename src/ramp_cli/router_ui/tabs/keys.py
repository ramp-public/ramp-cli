"""The API Keys tab and its key pages."""

from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

import click
from rich.cells import cell_len
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.coordinate import Coordinate
from textual.widgets import (
    DataTable,
    Input,
    Label,
    Select,
    Static,
)
from textual.widgets.data_table import CellDoesNotExist

from ramp_cli.commands import router
from ramp_cli.commands.router_user import (
    _match_profile,
    _money,
    _validate_key_name,
)
from ramp_cli.router_ui.dialogs import (
    ChoiceDialog,
    Confirm,
)
from ramp_cli.router_ui.pages import (
    CellEditing,
    CollectionPage,
    FormPage,
    RouterPage,
)
from ramp_cli.router_ui.service import (
    safe_text,
)
from ramp_cli.router_ui.widgets import (
    Button,
    CellInput,
    CellTable,
    PaletteText,
    RouterTable,
    parse_spend_cap,
)


class KeyTable(CellTable):
    """The key's name opens rename and delete, so the cursor can reach it."""

    first_cell_column = 0
    spend_cap_column = 3

    def cell_locked(self, coordinate: Coordinate) -> bool:
        # Organization keys' caps are set by workspace admins only.
        if coordinate.column != self.spend_cap_column:
            return False
        try:
            key = str(self.coordinate_to_cell_key(coordinate).row_key.value)
        except CellDoesNotExist:
            return False
        entries = getattr(self.screen, "entries", None) or []
        item = next((k for k in entries if str(k.get("id")) == key), None)
        return item is not None and not item.get("spend_cap_editable")


def harness_labels(clients) -> str:
    from ramp_cli.router_ui.tabs.harnesses import ConnectPage  # noqa: PLC0415

    return ", ".join(ConnectPage.label(client) for client in clients)


def parse_key_name(text: str) -> str:
    valid = _validate_key_name(text)
    if valid is not True:
        raise ValueError(str(valid))
    return text.strip()


class KeysPage(CellEditing, CollectionPage):
    page_title = "API keys"
    columns = (
        "Name",
        "Status",
        "Routing Profile",
        "Spend Cap",
        "Current Spend",
        "Created Date",
    )
    filter_placeholder = "Filter by Name or Routing Profile"
    table_class = KeyTable
    cell_table_id = "table"

    def __init__(self, days: int = 7):
        super().__init__()
        self.days = days

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
        item = next((k for k in self.entries if str(k["id"]) == key), None)
        if item is None or self.router_app.busy:
            return
        editor = {
            0: self.edit_name,
            1: self.edit_status,
            2: self.edit_profile,
            3: self.edit_spend_cap,
        }.get(event.coordinate.column)
        if editor:
            editor(item, event.coordinate)

    def key_label(self, item: dict) -> str:
        return safe_text(item.get("name") or item["id"])

    def edit_name(self, item: dict, coordinate: Coordinate):
        def chosen(value: str):
            if value == "rename":
                self.rename_key(item, coordinate)
            elif value == "delete":
                self.delete_key(item)

        self.pick(
            coordinate,
            [("Rename Key?", "rename"), ("Delete Key?", "delete"), ("Back", "back")],
            None,
            chosen,
        )

    def rename_key(self, item: dict, coordinate: Coordinate):
        name = self.key_label(item)
        current = str(item.get("name") or "")

        def chosen(new_name: str):
            if new_name == current.strip():
                return
            self.router_app.confirm(
                f"Rename {name} to “{safe_text(new_name)}”?",
                lambda: self.run_and_reload(
                    "Renaming API key",
                    lambda: self.service.mutate_key(
                        str(item["id"]), "rename", new_name
                    ),
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
                        parse_key_name,
                        placeholder="Key name",
                        submit="Rename",
                    ),
                    lambda result: result and chosen(result[0]),
                )

        # After the menu closes, so the popover opens over the page itself.
        self.call_after_refresh(show)

    def delete_key(self, item: dict):
        # Harnesses hold their key locally; find any on this one first, so
        # deleting it never silently breaks them.
        def check():
            return self.service.stored_keys(), self.service.cursor_connected()

        self.run(
            "Checking harnesses",
            check,
            lambda found: self.check_harnesses(item, *found),
        )

    def holds(self, secret: str, last_four: str) -> bool:
        return bool(last_four) and secret.endswith(last_four)

    def key_for(self, secret: str) -> str:
        """Name the account key a stored secret belongs to, if just one fits."""
        matches = [
            key
            for key in self.entries
            if self.holds(secret, str(key.get("last_four") or ""))
        ]
        if len(matches) == 1:
            return self.key_label(matches[0])
        return f"the key ending {safe_text(secret[-4:])}"

    def check_harnesses(
        self,
        item: dict,
        stored: list[tuple[str, tuple[str, ...]]],
        cursor: bool,
    ):
        name = self.key_label(item)
        last_four = str(item.get("last_four") or "")
        on_key = [
            client
            for secret, clients in stored
            if self.holds(secret, last_four)
            for client in clients
        ]
        others = [
            (secret, clients)
            for secret, clients in stored
            if not self.holds(secret, last_four)
        ]
        notes = []
        if not last_four:
            notes.append(
                "Router didn't return this key's last four characters, so connected harnesses couldn't be checked."
            )
        if cursor:
            notes.append(
                "Cursor is connected to Router and keeps its key in its own settings; if it uses this key, change it in Cursor Settings → Models → API Keys."
            )
        if not on_key:
            self.confirm_delete(item, notes)
            return
        labels = harness_labels(on_key)
        shared = sum(
            str(key.get("last_four") or "") == last_four for key in self.entries
        )
        uses = (
            f"{labels} use a key ending {safe_text(last_four)}, which may be {name}."
            if shared > 1
            else f"{labels} {'uses' if len(on_key) == 1 else 'use'} {name}."
        )

        def chosen(value: str | None):
            if value == "switch":
                self.switch_harnesses(item, on_key, others, notes)
            elif value == "disconnect":
                self.run(
                    f"Disconnecting {labels}",
                    lambda: self.service.command(["configure", "disconnect", *on_key]),
                    lambda _r: self.confirm_delete(
                        item, [f"Disconnected {labels}.", *notes], changed=True
                    ),
                )

        self.app.push_screen(
            ChoiceDialog(
                f"{name} is in use",
                f"{uses} Deleting the key stops them working immediately. Switch them to another key or disconnect them first.",
                [
                    ("Switch to another key", "switch"),
                    (f"Disconnect {labels}", "disconnect"),
                    ("Back", "back"),
                ],
            ),
            chosen,
        )

    def switch_harnesses(
        self,
        item: dict,
        clients: list[str],
        others: list[tuple[str, tuple[str, ...]]],
        notes: list[str],
    ):
        from ramp_cli.router_ui.tabs.harnesses import ConnectPage  # noqa: PLC0415

        labels = harness_labels(clients)
        new_name = " + ".join(ConnectPage.label(client) for client in clients)
        # One request for this choice, so a retried create can't add a second key.
        request_id = str(uuid4())
        choices = [
            (
                f"Use {self.key_for(secret)} · used by {harness_labels(used_by)}",
                str(index),
            )
            for index, (secret, used_by) in enumerate(others)
        ]
        choices += [(f"Create a new key “{new_name}”", "new"), ("Back", "back")]

        def switched(note: str):
            return lambda _r: self.confirm_delete(item, [note, *notes], changed=True)

        def chosen(value: str | None):
            if value == "new":
                self.run(
                    "Creating key and switching harnesses",
                    lambda: self.service.connect_with_new_key(
                        clients, new_name, request_id
                    ),
                    switched(f"Switched {labels} to a new key, “{new_name}”."),
                )
            elif value and value.isdigit():
                secret, _used_by = others[int(value)]
                self.run(
                    "Switching harnesses",
                    lambda: self.service.connect(clients, secret=secret),
                    switched(f"Switched {labels} to {self.key_for(secret)}."),
                )

        self.app.push_screen(
            ChoiceDialog(
                f"Switch {labels}",
                "Pick the key they should use instead. This updates their Router configuration.",
                choices,
            ),
            chosen,
        )

    def confirm_delete(self, item: dict, notes: list[str], *, changed: bool = False):
        name = self.key_label(item)
        message = " ".join(
            [
                *notes,
                f"Delete {name}? Any apps using it will stop working immediately. This cannot be undone.",
            ]
        )

        def answered(approved: bool):
            if approved:
                self.run_and_reload(
                    "Deleting API key",
                    lambda: self.service.delete_key(str(item["id"])),
                    f"Deleted {name}.",
                )
            elif changed:
                # Harnesses moved (maybe to a new key) even if the key stays.
                self.router_app.load_page(self, quiet=True)

        self.app.push_screen(Confirm(message, "Delete key"), answered)

    def edit_status(self, item: dict, coordinate: Coordinate):
        name = self.key_label(item)
        if not item.get("enabled"):
            if item.get("disabled_reason") == "workspace_admin_suspended":
                self.message(f"{name} was suspended by your organization's admin.")
                return
            if (
                item.get("spend_cap_locked")
                or item.get("disabled_reason") == "spend_cap"
            ):
                self.message(f"Raise or remove {name}'s spend cap before unlocking it.")
                return
        action = "lock" if item.get("enabled") else "unlock"
        note = (
            "It will stop accepting inference requests."
            if action == "lock"
            else "It will accept inference requests again."
        )

        def chosen(value: str):
            if value != action:
                return
            self.router_app.confirm(
                f"{action.title()} {name}? {note}",
                lambda: self.run_and_reload(
                    f"{action.title()}ing API key",
                    lambda: self.service.mutate_key(str(item["id"]), action),
                    f"{action.title()}ed {name}.",
                ),
                action.title(),
            )

        # Like a harness's Router cell: the one change, focused, and a way out.
        self.pick(
            coordinate,
            [(f"{action.title()} Key?", action), ("Back", "back")],
            None,
            chosen,
        )

    def edit_profile(self, item: dict, coordinate: Coordinate):
        name = self.key_label(item)

        def loaded(profiles):
            self.message(f"{self.query_one('#table', DataTable).row_count} shown")
            if not profiles:
                self.message("This account has no routing strategies yet.")
                return
            current = next(
                (
                    str(p["id"])
                    for p in profiles
                    if str(item["id"]) in map(str, p.get("assigned_api_key_ids") or [])
                ),
                None,
            )
            names = {str(p["id"]): safe_text(p.get("name")) for p in profiles}

            def chosen(profile_id: str):
                self.router_app.confirm(
                    f"Move {name} to the “{names[profile_id]}” routing strategy?",
                    lambda: self.run_and_reload(
                        "Saving routing strategy",
                        lambda: self.service.mutate_key(
                            str(item["id"]), "set-strategy", profile_id
                        ),
                        f"{name} now uses {names[profile_id]}.",
                    ),
                    "Move key",
                )

            self.pick(
                coordinate,
                [
                    (
                        names[str(p["id"])]
                        + (" · default" if p.get("is_default") else ""),
                        str(p["id"]),
                    )
                    for p in profiles
                ],
                current,
                chosen,
            )

        self.run(
            "Loading routing strategies", self.service.key_profiles, loaded, read=True
        )

    def edit_spend_cap(self, item: dict, coordinate: Coordinate):
        name = self.key_label(item)
        if not item.get("spend_cap_editable"):
            self.message(
                f"{name} belongs to your organization; only a workspace admin can change its spend cap."
            )
            return
        field = (
            "lifetime_spend_cap_usd"
            if "spend_cap_amount_usd" not in item and "lifetime_spend_cap_usd" in item
            else "spend_cap_amount_usd"
        )
        current = item.get(field)
        try:
            text = "" if current is None else parse_spend_cap(str(current)) or ""
        except ValueError:
            text = str(current)

        def chosen(amount: str | None):
            if amount == (text or None):
                return
            message = (
                f"Remove {name}'s spend cap?"
                if amount is None
                else f"Set {name}'s spend cap to {_money(amount)}?"
            )
            self.router_app.confirm(
                message,
                lambda: self.run_and_reload(
                    "Saving spend cap",
                    lambda: self.service.set_key_spend_cap(str(item["id"]), amount),
                    f"Removed {name}'s spend cap."
                    if amount is None
                    else f"{name}'s spend cap is now {_money(amount)}.",
                ),
                "Set cap",
            )

        def show():
            if self.is_mounted and self.app.screen is self:
                self.app.push_screen(
                    CellInput(
                        text,
                        self.cell_anchor(coordinate),
                        parse_spend_cap,
                        placeholder="No cap (USD)",
                    ),
                    lambda result: result and chosen(result[0]),
                )

        self.call_after_refresh(show)

    def load(self):
        self.run("Loading API keys", self.service.keys, self.loaded)

    def filter_text(self, item: dict) -> str:
        return f"{item.get('name', '')} {item.get('routing_profile_name', 'Account Defaults')}"

    def layout_columns(self):
        if not self.router_app.live(self):
            return
        table = self.query_one("#table", DataTable)
        if len(table.columns) != 6:
            return
        rows = [table.get_row_at(index) for index in range(table.row_count)]
        # Preserve readable money/date/status widths; long names and profiles
        # wrap rather than disappear. Below the minimum, the table scrolls.
        minimums = [14, 8, 15, 9, 13, 12]
        widths = list(minimums)
        for index in (1, 3, 4, 5):
            for row in rows:
                value = row[index]
                widths[index] = max(
                    widths[index],
                    cell_len(value.plain if isinstance(value, Text) else str(value)),
                )
        available = max(
            0, table.scrollable_content_region.width - 2 * table.cell_padding * 6
        )
        extra = max(0, available - sum(widths))
        weights = (2, 1, 3, 1, 1, 1)
        allocations = [extra * weight // sum(weights) for weight in weights]
        allocations[0] += extra - sum(allocations)
        widths = [base + bonus for base, bonus in zip(widths, allocations, strict=True)]
        table.set_column_widths(widths)

    def row(self, item):
        enabled = item.get("enabled")
        reason = item.get("disabled_reason")
        status = "Enabled" if enabled else "Locked"
        if item.get("spend_cap_locked") or reason == "spend_cap":
            status = "Cap Reached"
        elif reason in (
            "operator_suspended",
            "workspace_admin_suspended",
            "moderation",
            "admin",
        ):
            status = "Suspended"
        elif reason == "business_access_revoked":
            status = "Access Revoked"
        elif reason == "member_budget":
            status = "Budget Reached"
        if "spend_cap_amount_usd" in item:
            cap = (
                "No Cap"
                if item["spend_cap_amount_usd"] is None
                else _money(item["spend_cap_amount_usd"])
            )
        elif "lifetime_spend_cap_usd" in item:
            cap = (
                "No Cap"
                if item["lifetime_spend_cap_usd"] is None
                else _money(item["lifetime_spend_cap_usd"])
            )
        else:
            cap = "Unavailable"
        if cap not in ("No Cap", "Unavailable"):
            period = {
                "daily": "Day",
                "weekly": "Week",
                "monthly": "Month",
                "yearly": "Year",
            }.get(item.get("spend_cap_frequency"))
            if period:
                cap += f" / {period}"
        created = "Unavailable"
        if item.get("created_at"):
            try:
                value = datetime.fromisoformat(
                    str(item["created_at"]).replace("Z", "+00:00")
                )
                if value.tzinfo:
                    value = value.astimezone()
                # Unpadded day and hour (strftime's %-d isn't portable): Oct 4, 2026 3:23 PM.
                created = (
                    f"{value:%b} {value.day}, {value.year} "
                    f"{value.hour % 12 or 12}:{value:%M %p}"
                )
            except (ValueError, TypeError):
                pass
        return (
            safe_text(item.get("name") or "(unnamed)"),
            PaletteText(
                status,
                self.router_app.palette,
                "accent" if status == "Enabled" else "muted",
            ),
            safe_text(item.get("routing_profile_name") or "Account Defaults"),
            cap,
            _money(item.get("current_spend_usd")),
            created,
        )

    def open_entry(self, key):
        self.router_app.push(KeyPage(key, days=self.days))

    @on(Button.Pressed, "#create")
    def create(self):
        self.router_app.push(CreateKeyPage())


class KeyPage(RouterPage):
    page_title = "API key"

    def __init__(self, key_id: str, days: int = 7):
        super().__init__()
        self.key_id = key_id
        self.days = days
        self.key = {}

    def content(self) -> ComposeResult:
        yield Static("", id="key-summary", markup=False)
        yield Label("Usage window")
        # `keys --days` takes any window from 1 to 90; offer the one asked for.
        windows = sorted({7, 30, 90, self.days})
        yield Select(
            [(f"{days} day{'s' if days != 1 else ''}", days) for days in windows],
            value=self.days,
            allow_blank=False,
            id="days",
        )
        yield Static("", id="key-usage", markup=False)
        yield RouterTable(id="usage-models", cursor_type="row")

    def actions(self) -> ComposeResult:
        yield Button("Rename", id="rename")
        yield Button("Change strategy", id="assignment")
        yield Button("Lock", id="lock")

    def on_mount(self):
        self.query_one("#usage-models", DataTable).add_columns(
            "Model", "Requests", "Tokens", "Spend"
        )
        super().on_mount()

    def load(self):
        self.run(
            "Loading key details",
            lambda: self.service.key_details(self.key_id, self.days),
            self.loaded,
        )

    def loaded(self, result):
        self.key = result["key"]
        self.ready = True
        strategy = result.get("strategies") or {}
        profile = strategy.get("routing_profile") or {}
        self.query_one("#key-summary", Static).update(
            f"{safe_text(self.key.get('name') or '(unnamed)')}\n{safe_text(self.key_id)}\n"
            f"Status: {'Enabled' if self.key.get('enabled') else 'Locked'}\n"
            f"Strategy: {safe_text(profile.get('name') or 'Account defaults')}"
        )
        self.query_one("#lock", Button).label = (
            "Lock" if self.key.get("enabled") else "Unlock"
        )
        usage = result.get("usage") or {}
        summary = usage.get("summary") or {}
        self.query_one("#key-usage", Static).update(
            f"Lifetime spend: {_money(self.key.get('current_spend_usd'))}   "
            f"Spend cap: {_money(self.key.get('spend_cap_amount_usd'))}\n"
            f"Last {self.days} days: {summary.get('request_count', '—')} requests · "
            f"{summary.get('total_tokens', '—')} tokens · {_money(summary.get('spend_usd'))} spend\n"
            f"Savings: {_money(summary.get('cost_savings_usd'))} · Errors: {summary.get('error_count', '—')}\n\n"
            + " · ".join(
                f"{title}: {'On' if strategy.get(field) else 'Off'}"
                for title, field in (
                    ("Flex", "allow_flex_tier_default"),
                    ("Switchyard", "switchyard_routing_enabled"),
                    ("Jev Auto Routing", "auto_routing_enabled"),
                )
            )
        )
        table = self.query_one("#usage-models", DataTable)
        table.clear()
        for item in usage.get("model_stats") or []:
            table.add_row(
                safe_text(item.get("model_display_name") or item.get("model")),
                str(item.get("request_count", "—")),
                str(item.get("total_tokens", "—")),
                _money(item.get("spend_usd")),
            )
        table.display = bool(table.row_count)
        table.call_after_refresh(table.spread_columns)
        self.message(
            " · ".join(
                result.get(name, "")
                for name in ("usage_error", "strategies_error")
                if result.get(name)
            )
        )

    @on(Select.Changed, "#days")
    def days_changed(self, event: Select.Changed):
        if self.ready and event.value != self.days and not self.router_app.busy:
            self.days = int(event.value)
            self.load()

    @on(Button.Pressed, "#rename")
    def rename(self):
        self.router_app.push(KeyEditPage(self.key, "rename"))

    @on(Button.Pressed, "#assignment")
    def assignment(self):
        self.router_app.push(KeyEditPage(self.key, "set-strategy"))

    @on(Button.Pressed, "#lock")
    def lock_key(self):
        action = "lock" if self.key.get("enabled") else "unlock"
        note = (
            "It will stop accepting inference requests."
            if action == "lock"
            else "It will accept inference requests again."
        )
        self.router_app.confirm(
            f"{action.title()} {safe_text(self.key.get('name') or self.key_id)}? {note}",
            lambda: self.run_and_reload(
                action.title() + " API key",
                lambda: self.service.mutate_key(self.key_id, action),
            ),
            action.title(),
        )


class CreateKeyPage(FormPage):
    page_title = "create API key"

    def __init__(
        self,
        routing_profile: str | None = None,
        use_secret: Callable | None = None,
        key_name: str = "",
    ):
        super().__init__()
        self.request_id = str(uuid4())
        self.routing_profile = routing_profile
        self.use_secret = use_secret
        self.key_name = key_name
        self.submitted: tuple | None = None
        self.uncertain = False
        # Set when the --routing-profile given matches no strategy.
        self.profile_unresolved = False

    def content(self) -> ComposeResult:
        yield Label("Key name")
        yield Input(self.key_name, id="name", max_length=128)
        yield Label("Routing strategy")
        yield Select([], id="profile", prompt="Account defaults")

    def actions(self) -> ComposeResult:
        yield Button("Create key", id="save", variant="primary")

    def load(self):
        self.run("Loading strategies", self.service.key_profiles, self.loaded)

    def loaded(self, profiles):
        selector = self.query_one("#profile", Select)
        selector.set_options(
            [(Text(safe_text(p.get("name"))), str(p["id"])) for p in profiles]
        )
        notice = "The secret is available only when this key is created."
        default = next((p for p in profiles if p.get("is_default")), None)
        if self.routing_profile:
            # Resolved as 'keys create --routing-profile' does: an ID, then one
            # name in any case. An unknown one picks nothing, so a typo can't
            # quietly create the key on the account default instead.
            try:
                default = _match_profile(profiles, self.routing_profile)
            except click.BadParameter as error:
                default = None
                self.profile_unresolved = True
                notice = f"{error.format_message()} Choose a strategy."
        if default:
            selector.value = str(default["id"])
        self.remember()
        self.message(notice, error=self.profile_unresolved)

    @on(Button.Pressed, "#save")
    def save(self):
        profile = self.query_one("#profile", Select).value
        name = self.query_one("#name", Input).value
        if self.profile_unresolved and profile is Select.NULL:
            self.message(
                f"No routing strategy matches '{self.routing_profile}'. Choose one.",
                error=True,
            )
            return
        inputs = (name, profile)
        if self.uncertain and inputs != self.submitted:
            self.message(
                "The earlier create may have succeeded. Retry the same inputs to recover it safely, or leave and inspect your keys first.",
                error=True,
            )
            return
        self.submitted = inputs
        self.run(
            "Creating API key",
            lambda: self.service.create_key(
                name, None if profile is Select.NULL else str(profile), self.request_id
            ),
            lambda key: self.router_app.replace(
                SecretPage(key, use_secret=self.use_secret)
            ),
        )


class SecretPage(RouterPage):
    page_title = "API key created"

    def __init__(self, key: dict, *, use_secret: Callable | None = None):
        super().__init__()
        self.secret = str(key.pop("secret", ""))
        self.key = key
        self.use_secret = use_secret

    def content(self) -> ComposeResult:
        yield Static(
            f"Created {safe_text(self.key.get('name'))}\n{safe_text(self.key.get('id'))}",
            markup=False,
        )
        yield Static(
            "Copy and store this secret now. Leaving this screen clears it.",
            classes="muted",
        )
        self.secret_input = Input(
            self.secret, password=True, id="secret", disabled=True
        )
        yield self.secret_input

    def actions(self) -> ComposeResult:
        yield Button("Copy secret", id="copy", variant="primary")
        yield Button("Reveal", id="reveal")
        yield Button("Done", id="done")
        if self.use_secret:
            yield Button("Use for connection", id="use")

    @on(Button.Pressed, "#copy")
    def copy(self):
        self.run(
            "Copying secret",
            lambda: router._copy_to_clipboard(self.secret),
            lambda copied: self.message(
                "Copied to clipboard."
                if copied
                else "Clipboard unavailable; reveal and copy the secret manually."
            ),
        )

    @on(Button.Pressed, "#reveal")
    def reveal(self):
        field = self.query_one("#secret", Input)
        field.password = not field.password
        self.query_one("#reveal", Button).label = "Reveal" if field.password else "Hide"

    @on(Button.Pressed, "#done")
    def done(self):
        self.router_app.back()

    @on(Button.Pressed, "#use")
    def use(self):
        if self.use_secret:
            self.use_secret(self.secret)
            self.router_app.back()

    def on_unmount(self):
        self.secret = ""
        self.secret_input.value = ""
        self.use_secret = None


class KeyEditPage(FormPage):
    page_title = "edit API key"

    def __init__(self, key: dict, action: str):
        super().__init__()
        self.key = key
        self.action = action

    def content(self) -> ComposeResult:
        yield Static(safe_text(self.key.get("name") or self.key["id"]), markup=False)
        if self.action == "rename":
            yield Label("New name")
            yield Input(str(self.key.get("name") or ""), id="name")
        else:
            yield Label("Routing strategy")
            yield Select([], id="profile", prompt="Select a strategy")

    def actions(self) -> ComposeResult:
        yield Button("Save", id="save", variant="primary")

    def load(self):
        if self.action == "rename":
            self.remember()
        else:
            self.run("Loading strategies", self.service.key_profiles, self.loaded)

    def loaded(self, profiles):
        self.query_one("#profile", Select).set_options(
            [(Text(safe_text(p.get("name"))), str(p["id"])) for p in profiles]
        )
        self.remember()

    @on(Button.Pressed, "#save")
    def save(self):
        if self.action == "rename":
            value = self.query_one("#name", Input).value
        else:
            value = self.query_one("#profile", Select).value
            if value is Select.NULL:
                self.message("Select a strategy.", error=True)
                return
        self.run(
            "Saving API key",
            lambda: self.service.mutate_key(
                str(self.key["id"]), self.action, str(value)
            ),
            lambda _r: self.router_app.back(force=True),
        )
