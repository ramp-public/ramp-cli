"""The Harnesses tab and its connection pages."""

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING
from uuid import uuid4

import click
from rich.cells import cell_len
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import (
    Horizontal,
    Vertical,
    VerticalScroll,
)
from textual.coordinate import Coordinate
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import (
    Checkbox,
    DataTable,
    Input,
    Label,
    Select,
    SelectionList,
    Static,
)

from ramp_cli.commands import router
from ramp_cli.router_ui.dialogs import (
    Notice,
)
from ramp_cli.router_ui.pages import (
    CellEditing,
    FormPage,
    ResultPage,
    RouterPage,
)
from ramp_cli.router_ui.service import (
    KeyHandoffFailed,
    RouterService,
    safe_message,
    safe_text,
)
from ramp_cli.router_ui.widgets import (
    ARROW_BINDINGS,
    ArrowNavigation,
    Button,
    CellTable,
    InlineModal,
    PaletteText,
    RouterTable,
    arrow_row,
    centred_width,
    title_case,
)

if TYPE_CHECKING:
    from ramp_cli.router_ui.app import RouterApp


SUBAGENT_PREFIX = "subagent:"
# The Default Model column, the one cell a subagent row can change.
MODEL_COLUMN = 2


PRICE_NOTE = "Prices are USD per 1M tokens."


def _rate(value: str | None) -> str:
    try:
        amount = Decimal(value or "")
    except InvalidOperation:
        return "—"
    # Router writes 0 for a rate its catalog doesn't state.
    if not amount.is_finite() or amount <= 0:
        return "—"
    # Cents, unless the rate is finer than a cent.
    exact = f"{amount.normalize():f}"
    cents = f"{amount:.2f}"
    return "$" + (cents if Decimal(cents) == amount else exact)


PRICE_HEADERS = ("Model", "In", "Out", "Cached")


def priced(
    choices: list[tuple[str, str]], prices: dict[str, tuple]
) -> tuple[list[tuple[str, str]], str]:
    """Choices with their prices in aligned columns, and the header over them.

    Unpriced choices keep their label, and with no prices there's no header.
    """
    rates = {
        value: [_rate(rate) for rate in prices[value]]
        for _label, value in choices
        if value in prices
    }
    if not rates:
        return choices, ""
    name = max(cell_len(label) for label in [PRICE_HEADERS[0], *dict(choices)])
    widths = [
        max(len(header), *(len(row[index]) for row in rates.values()))
        for index, header in enumerate(PRICE_HEADERS[1:])
    ]

    def line(first: str, cells) -> str:
        columns = "".join(f"  {cell:>{width}}" for cell, width in zip(cells, widths))
        return first + " " * (name - cell_len(first)) + columns

    labelled = [
        (line(label, rates[value]) if value in rates else label, value)
        for label, value in choices
    ]
    return labelled, line(PRICE_HEADERS[0], PRICE_HEADERS[1:])


def organization_labelled(data: dict) -> list[tuple[str, str]]:
    """Model choices, marking the one the workspace admin suggests."""
    suggested = data.get("organization_default")
    return [
        (safe_text(label) + (" · Org default" if value == suggested else ""), value)
        for label, value in data["models"]
    ]


def organization_note(data: dict) -> str:
    """Point out the admin's suggestion when it isn't already the default."""
    suggested = data.get("organization_default")
    if not suggested or suggested == data.get("model"):
        return ""
    label = next(
        (label for label, value in data["models"] if value == suggested), suggested
    )
    return f"Your organization suggests {safe_text(label)}."


# Claude Desktop is restarted by setup itself, and Cursor applies the values
# pasted into its own settings at once; every other harness reads its Router
# configuration when it starts.
_APPLIES_WITHOUT_RESTART = ("cowork", "cursor")


def restart_notice(clients, *, connected: bool) -> Notice | None:
    """Tell the user which harnesses to restart, or None when none need it."""
    names = [
        safe_text(router.AGENT_NAMES.get(client, client))
        for client in clients
        if client not in _APPLIES_WITHOUT_RESTART
    ]
    if not names:
        return None
    them = "it" if len(names) == 1 else "them"
    action = "start" if connected else "stop"
    return Notice(
        "Restart to apply changes",
        f"Restart {router._human_join(names)} so {them} {action} using Ramp "
        "Router. Sessions that are already running keep their previous settings.",
    )


def current_first(choices: list[tuple[str, str]], current: str | None) -> list:
    """The current model on top, the rest in their order (newest first)."""
    return sorted(choices, key=lambda choice: choice[1] != current)


class HarnessTable(CellTable):
    """Claude Code's subagent rows sit under it and open only from its cells.

    Row mode steps over them. Enter on Claude Code, then Up/Down, reaches them.
    """

    # Set while a reload puts the cursor back where it was, subagent rows too.
    restoring = False

    def row_id(self, row: int) -> str:
        return str(self.ordered_rows[row].key.value)

    def is_subagent(self, row: int) -> bool:
        return 0 <= row < self.row_count and self.row_id(row).startswith(
            SUBAGENT_PREFIX
        )

    def skipped(self, row: int, origin: int) -> bool:
        """Whether the cursor passes over ``row`` on its way from ``origin``."""
        if self.restoring or not self.is_subagent(row):
            return False
        if self.cursor_type != "cell" or not 0 <= origin < self.row_count:
            return True
        return not (self.is_subagent(origin) or self.row_id(origin) == "claude-code")

    def row_reachable(self, back: bool) -> bool:
        step = -1 if back else 1
        row = self.cursor_row + step
        while self.skipped(row, self.cursor_row):
            row += step
        return 0 <= row < self.row_count

    def cell_locked(self, coordinate: Coordinate) -> bool:
        return self.is_subagent(coordinate.row) and coordinate.column != MODEL_COLUMN

    def validate_cursor_coordinate(self, value: Coordinate) -> Coordinate:
        value = super().validate_cursor_coordinate(value)
        current = self.cursor_row
        if value.row != current and self.skipped(value.row, current):
            step = 1 if value.row > current else -1
            row = value.row
            while self.skipped(row, current):
                row += step
            if not 0 <= row < self.row_count:
                return self.cursor_coordinate
            value = super().validate_cursor_coordinate(Coordinate(row, value.column))
        # A subagent row has only its model to move to.
        if self.cursor_type == "cell" and self.cell_locked(value):
            return self.cursor_coordinate
        return value

    def action_leave_cells(self):
        # Leaving from a subagent row lands on Claude Code, which owns it.
        row = self.cursor_row
        while self.is_subagent(row):
            row -= 1
        if row != self.cursor_row:
            self.cursor_coordinate = Coordinate(row, self.cursor_column)
        super().action_leave_cells()


class HarnessesPage(CellEditing, RouterPage):
    page_title = "Coding harnesses"
    cell_table_id = "harnesses"

    def __init__(self, connect: dict | None = None):
        super().__init__()
        self.entries = {}
        # Connect form options to open over the table once it first loads.
        self.pending_connect = connect

    def content(self) -> ComposeResult:
        yield Static("Connect Router", classes="heading")
        yield HarnessTable(id="harnesses", cursor_type="row")

    def actions(self) -> ComposeResult:
        yield Button("Connect", id="connection", disabled=True)
        yield Button("Refresh tools", id="refresh")

    def on_mount(self):
        self.query_one("#harnesses", DataTable).add_columns(
            "Harness", "Router", "Default Model", "Routing Strategy", "API Key"
        )
        super().on_mount()

    def load(self):
        self.run("Checking harnesses", self.service.harnesses, self.loaded)

    def loaded(self, entries):
        table = self.query_one("#harnesses", DataTable)
        if self.pending_connect is not None:
            options, self.pending_connect = self.pending_connect, None
            self.call_after_refresh(
                lambda: self.app.push_screen(ConnectPage(**options))
            )
        selected = self.selected_harness()
        column = table.cursor_column
        self.entries = {str(item["id"]): item for item in entries}
        table.clear()
        palette = self.router_app.palette
        for item in entries:
            cells = [
                safe_text(item["name"]),
                item.get("status")
                or ("Connected" if item["configured"] else "Not Connected"),
                safe_text(item.get("default_model") or "—"),
                safe_text(item.get("routing_strategy") or "—"),
                safe_text(item.get("api_key") or "—"),
            ]
            if item["configured"]:
                cells[1] = PaletteText(cells[1], palette)
            elif not item["detected"]:
                # Supported but absent here: listed last, dimmed, connect-only.
                cells[1] = "Not Detected"
                cells = [PaletteText(cell, palette, "muted") for cell in cells]
            table.add_row(*cells, key=item["id"])
            # Connected Claude Code lists its subagent tiers' models under it.
            for tier, model in (item.get("subagents") or {}).items():
                table.add_row(
                    PaletteText(f"  ↳ {tier.title()} subagent", palette, "muted"),
                    "",
                    safe_text(model) if model else "Not set",
                    "",
                    "",
                    key=f"{SUBAGENT_PREFIX}{tier}",
                )
        if selected in table.rows:
            table.restoring = True
            try:
                table.move_cursor(row=table.get_row_index(selected), column=column)
            finally:
                table.restoring = False
        self.update_connection_button()
        self.call_after_refresh(self.layout_columns)
        self.message("")

    def layout_columns(self):
        if self.router_app.live(self):
            self.query_one("#harnesses", RouterTable).spread_columns()

    def selected_harness(self):
        table = self.query_one("#harnesses", DataTable)
        if table.row_count:
            return str(
                table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            )
        return None

    def update_connection_button(self):
        entry = self.entries.get(self.selected_harness())
        button = self.query_one("#connection", Button)
        button.disabled = entry is None or self.router_app.busy
        button.label = "Disconnect" if entry and entry["configured"] else "Connect"

    @on(DataTable.RowHighlighted, "#harnesses")
    @on(DataTable.CellHighlighted, "#harnesses")
    def highlight_harness(self):
        self.update_connection_button()

    def key_hints(self) -> str:
        table = self.query("#harnesses").first(CellTable) if self.is_mounted else None
        if table is not None and table.cursor_type == "cell":
            return "↑↓←→ move   ↵ change   esc rows"
        return self.hints("↑↓ move", "←→ switch", "↵ edit cells")

    @on(CellTable.ModeChanged, "#harnesses")
    def cell_mode_changed(self):
        self.query_one("#keys", Static).update(self.key_hints())
        table = self.query_one("#harnesses", CellTable)
        client = self.selected_harness()
        entry = self.entries.get(client)
        # Connecting is the only thing a detected, unconnected harness can do,
        # so Enter goes straight to offering it.
        if (
            table.cursor_type == "cell"
            and entry is not None
            and entry["detected"]
            and not entry["configured"]
            and not self.router_app.busy
        ):
            self.edit_connection(client, entry, table.cursor_coordinate)

    @on(DataTable.CellSelected, "#harnesses")
    def edit_cell(self, event: DataTable.CellSelected):
        event.stop()
        client = str(event.cell_key.row_key.value)
        if client.startswith(SUBAGENT_PREFIX):
            if event.coordinate.column == MODEL_COLUMN and not self.router_app.busy:
                self.edit_subagent(
                    client.removeprefix(SUBAGENT_PREFIX), event.coordinate
                )
            return
        entry = self.entries.get(client)
        if entry is None or self.router_app.busy:
            return
        editor = {
            1: self.edit_connection,
            2: self.edit_model,
            3: self.edit_strategy,
            # A harness changes keys by connecting again with another one.
            4: lambda client, entry, _coordinate: self.connect_harness(client, entry),
        }.get(event.coordinate.column)
        if editor:
            editor(client, entry, event.coordinate)

    def harness_label(self, client: str, entry: dict) -> str:
        return safe_text(router.AGENT_NAMES.get(client) or entry["name"])

    def connect_harness(self, client: str, entry: dict):
        name = self.harness_label(client, entry)
        self.router_app.push(ConnectPage(clients=(client,), key_name=name))

    def reload_with(self, note: str):
        def loaded(entries):
            self.loaded(entries)
            self.message(note)

        self.run("Checking harnesses", self.service.harnesses, loaded, read=True)

    def explain_cursor_disconnect(self):
        # Cursor's settings sync through its account, so only Cursor can
        # change them; say exactly which toggle to turn off.
        self.message(
            "To disconnect Cursor, open Cursor Settings (Cmd/Ctrl+Shift+J) → Models → API Keys and turn off “Override OpenAI Base URL”, then refresh."
        )

    def disconnect(self, client: str, name: str):
        def operation():
            result = self.service.command(["configure", "disconnect", client])
            notice = restart_notice((client,), connected=False)
            if notice is not None:
                # Shown as soon as the change lands; the table reloads below
                # it, and a failed reload still says the change went through.
                self.app.call_from_thread(self.app.push_screen, notice)
            return result

        self.run_and_reload(f"Disconnecting {name}", operation, f"Disconnected {name}.")

    def edit_connection(self, client: str, entry: dict, coordinate: Coordinate):
        name = self.harness_label(client, entry)

        def chosen(value: str):
            if value == "back":
                return
            if value == "connected":
                self.connect_harness(client, entry)
                return
            if client == "cursor":
                self.explain_cursor_disconnect()
                return
            self.router_app.confirm(
                f"Disconnect {name} and restore its previous settings? API keys are not revoked.",
                lambda: self.disconnect(client, name),
                "Disconnect",
            )

        # Offer only the action that changes the state, focused, and a way out.
        action = (
            ("Disconnect?", "disconnected")
            if entry["configured"]
            else ("Connect Now?", "connected")
        )
        self.pick(coordinate, [action, ("Back", "back")], None, chosen)

    def edit_model(self, client: str, entry: dict, coordinate: Coordinate):
        name = self.harness_label(client, entry)
        if not entry["configured"]:
            self.message(f"Connect {name} to Router first.")
            return
        if client == "cursor":
            self.message("Pick Router models in Cursor's chat model picker.")
            return

        def loaded(data):
            self.message("")
            if not data["models"]:
                self.message(
                    data["model_note"] or "No models are available to this key."
                )
                return

            def chosen(model: str):
                self.router_app.confirm(
                    f"Set {name}'s default model to {safe_text(model)}? Existing chats may keep their current model.",
                    lambda: self.run_and_reload(
                        "Saving default model",
                        lambda: self.service.save_harness_model(
                            client, model, data["connection_token"]
                        ),
                        f"{name} now defaults to {safe_text(model)}.",
                    ),
                    "Save model",
                    focus_accept=True,
                )

            choices, header = priced(
                organization_labelled(data),
                data.get("prices") or {},
            )
            self.message(
                " ".join(
                    note
                    for note in (PRICE_NOTE if header else "", organization_note(data))
                    if note
                )
            )
            self.pick(
                coordinate,
                current_first(choices, data["model"]),
                data["model"],
                chosen,
                header,
            )

        self.run("Loading models", lambda: self.service.harness_editor(client), loaded)

    def edit_subagent(self, tier: str, coordinate: Coordinate):
        label = f"{tier.title()} subagents"

        def loaded(data):
            self.message("")
            current = data["tiers"].get(tier) or ""
            choices = [(safe_text(name), value) for name, value in data["models"]]
            if current and current not in {value for _, value in choices}:
                choices.append((safe_text(current) + " (no longer served)", current))
            # Empty clears the override, so Claude Code picks the tier's model.
            choices.append(("Claude Code default", ""))
            choices, header = priced(choices, data.get("prices") or {})
            self.message(PRICE_NOTE if header else "")

            def chosen(model: str):
                note = (
                    f"{label} now use {safe_text(model)}."
                    if model
                    else f"{label} now use Claude Code's default."
                )
                self.run_and_reload(
                    "Saving subagent model",
                    lambda: self.service.save_subagent(tier, model or None),
                    note,
                )

            self.pick(
                coordinate, current_first(choices, current), current, chosen, header
            )

        self.run("Loading models", self.service.subagents, loaded)

    def edit_strategy(self, client: str, entry: dict, coordinate: Coordinate):
        name = self.harness_label(client, entry)
        if not entry["configured"]:
            self.message(f"Connect {name} to Router first.")
            return
        if client == "cursor":
            self.message(
                "Cursor uses the strategy of the API key pasted into it. Change it in API keys."
            )
            return

        def loaded(data):
            self.message("")
            profiles = data["profiles"]
            if not profiles:
                self.message("This key's account has no routing strategies yet.")
                return
            current = next((p["name"] for p in profiles if p["is_current"]), None)
            others = ", ".join(
                router.AGENT_NAMES.get(other, other) for other in data["shared_with"]
            )

            def chosen(profile: str):
                shared = (
                    f" {others} use the same API key and will switch too."
                    if others
                    else ""
                )
                self.router_app.confirm(
                    f"Switch {name} to the “{safe_text(profile)}” routing strategy?{shared}",
                    lambda: self.run_and_reload(
                        "Saving routing strategy",
                        lambda: self.service.select_harness_profile(
                            client, profile, data["connection_token"]
                        ),
                        f"{name} now uses {safe_text(profile)}.",
                    ),
                    "Switch strategy",
                )

            self.pick(
                coordinate,
                [
                    (
                        safe_text(p["name"])
                        + (" · default" if p.get("is_default") else ""),
                        p["name"],
                    )
                    for p in profiles
                ],
                current,
                chosen,
            )

        self.run(
            "Loading routing strategies",
            lambda: self.service.harness_profiles(client),
            loaded,
        )

    @on(Button.Pressed, "#connection")
    def change_connection(self):
        selected = self.selected_harness()
        entry = self.entries.get(selected)
        if entry and entry["configured"] and selected == "cursor":
            self.explain_cursor_disconnect()
        elif entry and entry["configured"]:
            self.router_app.push(DisconnectPage(clients=(selected,)))
        elif entry:
            self.connect_harness(selected, entry)

    @on(Button.Pressed, "#refresh")
    def refresh_harnesses(self):
        # The same list `ramp router refresh` walks, so the dialog names
        # exactly what will be rewritten.
        clients = router.configured_router_clients()
        if not clients:
            self.app.push_screen(
                Notice("Nothing to refresh", "No connected harnesses to refresh.")
            )
            return
        labels = [safe_text(router.AGENT_NAMES.get(c, c)) for c in clients]
        what = "It gets" if len(labels) == 1 else "For each one, it gets"
        if len(labels) == 1:
            title, accept = f"Refresh {labels[0]}?", f"Refresh {labels[0]}"
        else:
            count = f"{len(labels)} connected harnesses"
            title, accept = f"Refresh {count}?", f"Refresh {len(labels)} Harnesses"
        message = "\n".join(
            [
                title,
                "",
                "This will update:",
                *(f"• {label}" for label in labels),
                "",
                f"{what} the latest model list from Router and "
                "rewrites the Router URL and model settings in its config file. "
                "API keys stay the same, and nothing gets disconnected.",
            ]
        )
        self.router_app.confirm(
            message,
            lambda: self.run(
                "Refreshing harnesses",
                lambda: self.service.command(["refresh"]),
                lambda result: self.router_app.push(ResultPage("refresh", result)),
            ),
            accept,
            focus_accept=True,
        )


class ConnectPage(ArrowNavigation, InlineModal):
    """Connect form drawn over the Harnesses table."""

    BINDINGS = [Binding("escape", "back", "Cancel"), *ARROW_BINDINGS]

    def __init__(
        self,
        clients: tuple[str, ...] = (),
        key_name: str | None = None,
        **options,
    ):
        super().__init__()
        self.clients = tuple(clients)
        self.options = options
        self.key_name = key_name or (
            " + ".join(self.label(client) for client in self.clients) or "Router"
        )
        # (label, secret) for each account key held on this machine.
        self.stored: list[tuple[str, str]] = []
        # One request per key name, so a retry can't create a second key.
        self.request_id = str(uuid4())
        self.requested_name: str | None = None
        self.baseline: tuple | None = None
        # What RouterApp.run_operation expects of the screen it runs on.
        self.content_shown = True
        self.loading_timer: Timer | None = None

    @property
    def router_app(self) -> "RouterApp":
        return self.app

    @property
    def service(self) -> RouterService:
        return self.router_app.service

    @staticmethod
    def label(client: str) -> str:
        return router.AGENT_NAMES.get(client) or (
            "Cursor" if client == "cursor" else client
        )

    @property
    def cursor(self) -> bool:
        return self.clients == ("cursor",)

    def asks_models(self) -> bool:
        # Only Claude Code has a model list to choose, and only on its own.
        return not self.clients or self.clients == ("claude-code",)

    def compose(self) -> ComposeResult:
        title = " + ".join(self.label(client) for client in self.clients)
        with Vertical(id="connect-dialog"):
            yield Static(
                f"Connect {title}" if title else "Connect harnesses",
                id="connect-title",
                markup=False,
            )
            with VerticalScroll(id="connect-body"):
                yield from self.content()
            with Vertical(id="connect-footer"):
                yield Static("", id="connect-status", markup=False)
                with Horizontal(id="connect-actions", classes="actions"):
                    yield Button("Back", id="connect-back")
                    yield Button("Advanced", id="advanced")
                    yield Static("", classes="spacer")
                    yield Button("Connect", id="save", variant="primary")

    def content(self) -> ComposeResult:
        if not self.clients:
            # Opened without a harness, e.g. from `ramp router configure`.
            yield Label("Harnesses")
            yield SelectionList[str](
                *[(label, name, False) for name, label in router.AGENT_NAMES.items()],
                id="clients",
            )
        yield Label("API key")
        yield Select(
            [("Create a new API key", "new")],
            value="new",
            allow_blank=False,
            id="key-source",
        )
        with Vertical(id="new-key"):
            yield Label("New key name")
            yield Input(self.key_name, id="key-name", max_length=128)
        if self.asks_models():
            yield Label("Model list")
            yield Select(
                [("Recommended models", "compact"), ("All models", "all")],
                value=self.options.get("claude_models") or "compact",
                allow_blank=False,
                id="model-view",
            )
        with Vertical(id="advanced-options"):
            yield Label("Or paste an API key · never shown in the command or result")
            self.secret_input = Input(
                self.options.get("api_key") or "", password=True, id="api-key"
            )
            yield self.secret_input
            if not self.cursor:
                yield Label("Or downloaded Router setup JSON")
                yield Input(
                    str(self.options.get("setup_file") or ""),
                    id="setup-file",
                    placeholder="/path/to/router-setup.json",
                )
            yield Label("Gateway and web app overrides")
            # Left empty, setup uses the Router chosen in Home's Settings.
            yield Input(
                self.options.get("base_url") or "",
                placeholder=router.router_base_url(),
                id="base-url",
            )
            yield Input(
                self.options.get("ui_url") or "",
                placeholder=router.router_ui_url(),
                id="ui-url",
            )

    def on_mount(self):
        for label in self.query(Label):
            rendered = label.render()
            label.update(title_case(getattr(rendered, "plain", str(rendered))))
        # Advanced values passed in from the command line stay visible.
        if any(
            self.options.get(name)
            for name in ("api_key", "setup_file", "base_url", "ui_url")
        ):
            self.toggle_advanced()
        self.query_one("#key-source", Select).focus()
        self.call_after_refresh(self.fit_body)
        # Once focus has landed, so loading gives it back to the dropdown.
        self.call_after_refresh(self.load)

    def on_resize(self, event: events.Resize):
        self.call_after_refresh(self.fit_body)

    def fit_body(self):
        # An auto-height scroll grows to its content, so cap it at whatever
        # the dialog has left after its title and footer; then it scrolls.
        if not self.is_mounted:
            return
        dialog = self.query_one("#connect-dialog")
        title = self.query_one("#connect-title")
        footer = self.query_one("#connect-footer")
        chrome = (
            dialog.styles.gutter.height
            + title.outer_size.height
            + title.styles.margin.bottom
            + footer.outer_size.height
        )
        limit = int(self.size.height * 0.9) - chrome
        self.query_one("#connect-body").styles.max_height = max(limit, 3)

    def load(self):
        def defaults():
            view = self.service.claude_model_view() if self.asks_models() else None
            return self.service.connect_keys(), view

        self.run("Reading local connections", defaults, self.loaded)

    def loaded(self, data):
        choices, view = data
        self.stored = choices
        source = self.query_one("#key-source", Select)
        source.set_options(
            [
                ("Create a new API key", "new"),
                *(
                    (Text(label), str(index))
                    for index, (label, *_) in enumerate(choices)
                ),
            ]
        )
        source.value = "new"
        if view and not self.options.get("claude_models"):
            self.query_one("#model-view", Select).value = view
        self.remember()
        if self.cursor:
            self.message(
                "Cursor is finished in its own settings. After you confirm, the key is copied to your clipboard and the exact steps are shown."
            )
            return
        self.message("Nothing changes until you confirm.")

    def run(self, label: str, operation: Callable, complete: Callable | None = None):
        self.router_app.run_operation(self, label, operation, complete)

    def message(self, message: str, *, error: bool = False):
        if self.is_mounted:
            status = self.query_one("#connect-status", Static)
            status.set_class(error, "error")
            status.update(safe_message(message))
            status.display = bool(message)
            self.call_after_refresh(self.fit_body)

    def progress(self, value: str):
        if self.is_mounted:
            status = self.query_one("#connect-status", Static)
            if status.content != value:
                status.set_class(False, "error")
                status.update(value)
                status.display = True

    def restore_focus(self, widget: Widget):
        # A re-enabled control rejoins the focus chain only after a refresh.
        def focus():
            if widget.is_mounted and widget.focusable and widget in self.focus_chain:
                widget.focus()

        self.call_after_refresh(focus)

    def values(self) -> tuple:
        return tuple(
            (widget.id, getattr(widget, "value", None))
            for widget in self.query("Input, Select")
        ) + tuple(
            (widget.id, tuple(widget.selected)) for widget in self.query(SelectionList)
        )

    def remember(self):
        self.baseline = self.values()

    def action_back(self):
        if self.router_app.busy:
            self.router_app.notice_busy()
        elif self.baseline is not None and self.values() != self.baseline:
            self.router_app.confirm(
                "Discard unsaved changes?", lambda: self.dismiss(None), "Discard"
            )
        else:
            self.dismiss(None)

    @on(Select.Changed, "#key-source")
    def key_source_changed(self, event: Select.Changed):
        self.query_one("#new-key").display = event.value == "new"

    @on(Button.Pressed, "#connect-back")
    def pressed_back(self):
        self.action_back()

    @on(Button.Pressed, "#advanced")
    def toggle_advanced(self):
        options = self.query_one("#advanced-options")
        options.toggle_class("open")
        shown = options.has_class("open")
        button = self.query_one("#advanced", Button)
        button.label = "Hide advanced" if shown else "Advanced"
        button.styles.width = centred_width(button.label.plain)

    def arrow_target(self, widget: Widget | None, key: str) -> Widget | None:
        target = super().arrow_target(widget, key)
        actions = self.query_one("#connect-actions")
        if (
            key == "down"
            and target is not None
            and arrow_row(target) is actions
            and (widget is None or arrow_row(widget) is not actions)
        ):
            # Moving down into the buttons lands on the main action.
            return self.query_one("#save", Button)
        return target

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted):
        event.stop()
        self.save()

    def field(self, selector: str) -> str:
        found = self.query(selector)
        return found.first(Input).value.strip() if found else ""

    def connected(self, clients: list[str], result: str):
        # Success needs no page of its own: the table below shows the change.
        table = self.app.screen_stack[-2]
        labels = ", ".join(self.label(name) for name in clients)
        self.dismiss(None)

        def reload(_closed=None):
            if isinstance(table, HarnessesPage) and table.is_mounted:
                table.reload_with(f"Connected {labels}.")

        notice = restart_notice(clients, connected=True)
        if "cursor" in clients:
            # Cursor still needs steps in its own settings; keep them in view.
            self.app.push_screen(Notice(f"Connected {labels}", result), reload)
        elif notice is not None:
            self.app.push_screen(notice, reload)
        else:
            reload()

    @on(Button.Pressed, "#save")
    def save(self):
        if self.router_app.busy:
            return
        clients = (
            list(self.clients)
            if self.clients
            else list(self.query_one("#clients", SelectionList).selected)
        )
        if not clients:
            self.message("Select at least one harness.", error=True)
            return
        options = {
            "setup_file": self.field("#setup-file"),
            "base_url": self.field("#base-url"),
            "ui_url": self.field("#ui-url"),
        }
        selectors = self.query("#model-view")
        # Rendered only when the form can honor it (see asks_models).
        if "claude-code" in clients and selectors:
            model_view = str(selectors.first(Select).value)
            if clients == ["claude-code"]:
                options["model_view"] = model_view
            elif model_view != self.service.claude_model_view():
                # Setup changes Claude Code's model list only when it's
                # connected alone; say so rather than drop the choice.
                self.message(
                    "Model list applies when Claude Code is connected by itself. "
                    "Connect it separately to change its model list.",
                    error=True,
                )
                return
        labels = ", ".join(self.label(name) for name in clients)
        pasted = self.field("#api-key")
        source = str(self.query_one("#key-source", Select).value)
        if options["setup_file"] or pasted or source != "new":
            if options["setup_file"]:
                secret, how = "", "the downloaded setup file"
            elif pasted:
                secret, how = pasted, "the pasted API key"
            else:
                label, secret = self.stored[int(source)]
                how = f"the key “{label}”"
            self.router_app.confirm(
                f"Connect {labels} with {how}? This updates their Router configuration and may install managed integrations.",
                lambda: self.run(
                    "Connecting harnesses",
                    lambda: self.service.connect(clients, secret=secret, **options),
                    lambda result: self.connected(clients, result),
                ),
                "Connect",
                focus_accept=True,
            )
            return
        key_name = self.field("#key-name")
        if not key_name:
            self.message("Name the new API key.", error=True)
            return
        if key_name != self.requested_name:
            self.request_id = str(uuid4())
            self.requested_name = key_name
        request_id = self.request_id
        self.router_app.confirm(
            f"Connect {labels} with a new API key named “{key_name}”? This updates their Router configuration and may install managed integrations.",
            lambda: self.run(
                "Creating key and connecting",
                lambda: self.connect_new(clients, key_name, request_id, options),
                lambda result: self.connected_new(clients, result),
            ),
            "Connect",
            focus_accept=True,
        )

    def connect_new(self, clients, key_name, request_id, options):
        try:
            return self.service.connect_with_new_key(
                clients, key_name, request_id, **options
            )
        except KeyHandoffFailed as failed:
            return failed

    def connected_new(self, clients, result):
        if isinstance(result, KeyHandoffFailed):
            from ramp_cli.router_ui.tabs.keys import SecretPage  # noqa: PLC0415

            # The new key is held nowhere else: show it masked, with Copy and
            # Reveal, rather than report a connection that never got it.
            self.router_app.push(SecretPage(result.key))
            self.router_app.screen.message(str(result), error=True)
            return
        self.connected(clients, result)

    def on_unmount(self):
        self.stored = []
        self.options.clear()
        self.secret_input.value = ""


class DisconnectPage(FormPage):
    page_title = "disconnect harnesses"

    def __init__(self, clients: tuple[str, ...] = ()):
        super().__init__()
        self.clients = clients

    def content(self) -> ComposeResult:
        yield Static(
            "Restore the settings this CLI changed. API keys are not revoked.",
            classes="muted",
        )
        yield SelectionList[str](id="clients")

    def actions(self) -> ComposeResult:
        yield Button("Review and disconnect", id="save", variant="primary")

    def load(self):
        def receipts():
            clients = router._clients_with_a_receipt()
            if router.claude_cowork.state_path().exists() and "cowork" not in clients:
                clients = (*clients, "cowork")
            return clients

        self.run("Checking connected harnesses", receipts, self.loaded)

    def loaded(self, clients):
        selector = self.query_one("#clients", SelectionList)
        selector.add_options(
            [
                (router.AGENT_NAMES.get(name, name), name, name in self.clients)
                for name in clients
            ]
        )
        self.query_one("#save", Button).disabled = not clients
        self.remember()
        self.message(
            "Select the connections to remove."
            if clients
            else "No managed connections to remove."
        )

    @on(Button.Pressed, "#save")
    def save(self):
        clients = list(self.query_one("#clients", SelectionList).selected)
        if not clients:
            self.message("Select at least one harness.", error=True)
            return
        self.router_app.confirm(
            "Disconnect the selected harnesses and restore their previous settings?",
            lambda: self.run(
                "Disconnecting harnesses",
                lambda: self.service.command(["configure", "disconnect", *clients]),
                lambda result: self.disconnected(clients, result),
            ),
            "Disconnect",
        )

    def disconnected(self, clients: list[str], result: str):
        self.router_app.replace(ResultPage("disconnected", result))
        notice = restart_notice(clients, connected=False)
        if notice is not None:
            self.app.push_screen(notice)


class LegacyPage(FormPage):
    page_title = "Account Routing Defaults"

    def __init__(self, api_key: str | None = None):
        super().__init__()
        self.stored = []
        self.payload = {}
        self.api_key = api_key

    def content(self) -> ComposeResult:
        yield Static(
            "Choose a connected harness. Routing changes apply to its key's owner and may affect other connections.",
            classes="muted",
        )
        yield Select([], id="stored", prompt="Choose a Connection")

        yield from self.routing_controls()

    def routing_controls(self) -> ComposeResult:
        yield Checkbox("Cost-efficient routing", id="cost")
        yield Checkbox("NVIDIA NeMo Switchyard", id="switchyard")
        yield Label("Switchyard efficient model · blank keeps current settings")
        yield Select([], id="model", prompt="Keep current")
        yield Label("Thinking effort · blank keeps current settings")
        yield Select([], id="effort", prompt="Keep current")
        yield Label("Sensitivity · blank keeps current settings")
        yield Select(
            [
                (label, name)
                for name, label in router.SWITCHYARD_SENSITIVITY_TITLES.items()
            ],
            id="sensitivity",
            prompt="Keep current",
        )
        yield Checkbox(
            "Reset Switchyard configuration to Router defaults", id="default-config"
        )

    def actions(self) -> ComposeResult:
        yield Button("Load settings", id="load")
        yield Button("Save defaults", id="save", variant="primary", disabled=True)

    def load(self):
        self.run("Reading connected keys", self.service.stored_keys, self.loaded_keys)

    def loaded_keys(self, choices):
        if self.api_key:
            choices = [(self.api_key, ("Provided API Key",)), *choices]
        self.stored = choices
        selector = self.query_one("#stored", Select)
        selector.set_options(
            [
                (
                    " + ".join(
                        router.AGENT_NAMES.get(client, client) for client in clients
                    ),
                    i,
                )
                for i, (_key, clients) in enumerate(choices)
            ]
        )
        if len(choices) == 1 or self.api_key:
            selector.value = 0
            self.call_after_refresh(self.load_settings)
        self.message(
            "Choose which connection's settings to manage."
            if choices
            else "Connect a harness first."
        )

    @on(Button.Pressed, "#load")
    def load_settings(self):
        index = self.query_one("#stored", Select).value
        if index is Select.NULL:
            self.message("Choose a connected key.", error=True)
            return
        self.run(
            "Loading routing defaults",
            lambda: self.service.legacy_settings_for(self.stored[int(index)][0]),
            self.loaded_settings,
        )

    def loaded_settings(self, payload):
        self.payload = payload
        self.query_one("#cost", Checkbox).value = payload["allow_flex_tier_default"]
        self.query_one("#switchyard", Checkbox).value = payload[
            "switchyard_routing_enabled"
        ]
        switchyard = payload.get("switchyard") or {}
        models = switchyard.get("efficient_models") or []
        self.query_one("#model", Select).set_options(
            [
                (
                    Text(safe_text(model.get("display_name") or model.get("id"))),
                    str(model.get("id")),
                )
                for model in models
            ]
        )
        self.query_one("#save", Button).disabled = False
        self.ready = True
        self.remember()
        self.message("Changes apply to this key owner's routing preferences.")

    @on(Select.Changed, "#stored")
    def stored_changed(self):
        self.query_one("#save", Button).disabled = True
        self.ready = False
        for name in ("model", "effort", "sensitivity"):
            self.query_one(f"#{name}", Select).value = Select.NULL
        self.query_one("#default-config", Checkbox).value = False

    @on(Select.Changed, "#model")
    def model_changed(self, event: Select.Changed):
        selected = next(
            (
                model
                for model in (self.payload.get("switchyard") or {}).get(
                    "efficient_models"
                )
                or []
                if model.get("id") == event.value
            ),
            None,
        )
        effort = self.query_one("#effort", Select)
        effort.set_options(
            [
                (Text(str(name)), str(name))
                for name in (selected or {}).get("efforts") or []
            ]
        )
        effort.value = Select.NULL

    @on(Button.Pressed, "#save")
    def save_pressed(self):
        # Textual runs every class's decorated handler, so subclasses
        # override `save` itself rather than redecorating it.
        self.save()

    def save(self):
        if not self.ready:
            return
        index = self.query_one("#stored", Select).value
        try:
            changes = self.routing_changes()
        except click.UsageError as error:
            self.message(str(error), error=True)
            return
        self.run(
            "Saving routing defaults",
            lambda: self.service.save_legacy_settings(
                self.stored[int(index)][0], changes
            ),
            self.loaded_settings,
        )

    def routing_changes(self) -> dict:
        changes = {
            "allow_flex_tier_default": self.query_one("#cost", Checkbox).value,
            "switchyard_routing_enabled": self.query_one("#switchyard", Checkbox).value,
        }
        config = {}
        for widget, field in (
            ("model", "efficient_model"),
            ("effort", "effort"),
            ("sensitivity", "sensitivity_config_name"),
        ):
            value = self.query_one(f"#{widget}", Select).value
            if value is not Select.NULL:
                config[field] = value
        reset = self.query_one("#default-config", Checkbox).value
        if reset and config:
            raise click.UsageError(
                "Reset to defaults cannot be combined with custom configuration."
            )
        if reset or config:
            if not changes["switchyard_routing_enabled"]:
                raise click.UsageError("Enable Switchyard to change its configuration.")
            changes["switchyard_config"] = None if reset else config
        return changes

    def on_unmount(self):
        self.stored = []
        self.api_key = None


class HarnessModelPage(FormPage):
    def __init__(self, client: str):
        super().__init__()
        self.client = client
        self.page_title = f"{router.AGENT_NAMES[client]} settings"
        self.connection_token = ""

    def content(self) -> ComposeResult:
        yield Label("Default model")
        yield Select([], id="default-model", prompt="Choose a model")
        yield Static("", id="model-note", markup=False, classes="muted")
        yield Static(
            "Routing is managed separately through this connection's key.",
            classes="muted",
        )

    def actions(self) -> ComposeResult:
        yield Button("Save model", id="save", disabled=True)
        yield Button("Routing strategy", id="routing")
        yield Button("Back", id="editor-back")

    def load(self):
        self.run(
            "Loading harness settings",
            lambda: self.service.harness_editor(self.client),
            self.loaded,
        )

    def loaded(self, data):
        self.connection_token = data["connection_token"]
        selector = self.query_one("#default-model", Select)
        selector.set_options(
            [(Text(label), value) for label, value in organization_labelled(data)]
        )
        selector.disabled = not data["models"]
        selector.value = (
            data["model"]
            if data["model"] in {value for _, value in data["models"]}
            else Select.NULL
        )
        self.query_one("#save", Button).disabled = not data["models"]
        note = data["model_note"]
        if data["model"] and selector.value is Select.NULL:
            note = "The current default is not in this catalog. Choose an available model to replace it."
        note = " ".join(part for part in (note, organization_note(data)) if part)
        self.query_one("#model-note", Static).update(note)
        self.ready = True
        self.remember()
        self.message("")

    @on(Button.Pressed, "#save")
    def save(self):
        model = self.query_one("#default-model", Select).value
        if not self.ready or model is Select.NULL:
            self.message("Choose an available model.", error=True)
            return
        self.router_app.confirm(
            f"Set {router.AGENT_NAMES[self.client]}'s default model to {safe_text(model)}? Existing chats may keep their current model.",
            lambda: self.run_and_reload(
                "Saving default model",
                lambda: self.service.save_harness_model(
                    self.client, model, self.connection_token
                ),
            ),
            "Save model",
            focus_accept=True,
        )

    @on(Button.Pressed, "#routing")
    def routing(self):
        self.router_app.push(HarnessRoutingPage(self.client))

    @on(Button.Pressed, "#editor-back")
    def back(self):
        self.router_app.back()


class HarnessRoutingPage(LegacyPage):
    def __init__(self, client: str):
        super().__init__()
        self.client = client
        self.connection_token = ""
        self.page_title = f"{router.AGENT_NAMES[client]} routing"

    def content(self) -> ComposeResult:
        yield Static(
            "These settings apply to this key's owner and can affect other connected harnesses. Named profiles are managed in API keys.",
            classes="muted",
        )
        yield from self.routing_controls()

    def actions(self) -> ComposeResult:
        yield Button("Save routing", id="save", disabled=True)
        yield Button("Back", id="routing-back")

    def load(self):
        self.run(
            "Loading routing strategy",
            lambda: self.service.harness_routing(self.client),
            self.loaded,
        )

    def loaded(self, data):
        self.connection_token = data["connection_token"]
        self.loaded_settings(data["payload"])
        self.message("")

    def save(self):
        if not self.ready:
            return
        try:
            changes = self.routing_changes()
        except click.UsageError as error:
            self.message(str(error), error=True)
            return
        self.router_app.confirm(
            "Save these routing settings for the installed key's owner? Other harnesses using that owner's keys may also be affected.",
            lambda: self.run(
                "Saving routing strategy",
                lambda: self.service.save_harness_routing(
                    self.client, changes, self.connection_token
                ),
                self.loaded_settings,
            ),
            "Save routing",
        )

    @on(Button.Pressed, "#routing-back")
    def back(self):
        self.router_app.back()
