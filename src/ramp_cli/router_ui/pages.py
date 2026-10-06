"""Base pages that every Router app tab builds on."""

from collections.abc import Callable
from typing import TYPE_CHECKING

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import (
    Horizontal,
    Vertical,
    VerticalScroll,
)
from textual.coordinate import Coordinate
from textual.geometry import Region
from textual.screen import Screen
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
    Tab,
    Tabs,
)

from ramp_cli.router_ui.animation import (
    BrandAnimation,
)
from ramp_cli.router_ui.service import (
    RouterService,
    safe_message,
)
from ramp_cli.router_ui.terminal import (
    fit_rows,
)
from ramp_cli.router_ui.widgets import (
    ARROW_BINDINGS,
    PAGES,
    ArrowNavigation,
    Button,
    CellMenu,
    CellTable,
    KeyHints,
    RouterFooter,
    RouterTable,
    RouterTabs,
    focus_first_action,
    title_case,
)

if TYPE_CHECKING:
    from ramp_cli.router_ui.app import RouterApp


class RouterPage(ArrowNavigation, Screen):
    page_title = "Router"
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "reload", "Reload"),
        *ARROW_BINDINGS,
    ]

    def __init__(self):
        super().__init__()
        self.ready = False
        # First loads hide the empty page at once; later ones wait briefly
        # so fast operations never blank content the user is looking at.
        self.content_shown = False
        self.loading_timer: Timer | None = None
        # A control to refocus once the page shows again after loading.
        self.pending_focus: Widget | None = None
        self.mounted_once = False

    @property
    def router_app(self) -> "RouterApp":
        return self.app

    @property
    def service(self) -> RouterService:
        return self.router_app.service

    def _on_screen_suspend(self):
        # Runs before Screen's own handler (Textual dispatches down the MRO).
        # While a screen is active, every style change reaches it, so its
        # styles are current as of this token (see RouterApp.style_token).
        self._style_token = self.router_app.style_token()

    def _on_screen_resume(self, event: events.ScreenResume):
        # Screen._on_screen_resume, which runs right after this with the same
        # event, restyles every node on each resume (~20 ms on a cached tab)
        # in case app-wide styles changed while the screen was away. Skip it
        # only when nothing app-wide changed since this screen was suspended;
        # widget class changes made meanwhile restyle their own nodes at once,
        # and size changes are refreshed separately via _refresh_layout.
        token = getattr(self, "_style_token", None)
        if token is not None and token == self.router_app.style_token():
            event.refresh_styles = False

    def fit_max_height(self):
        # Full-screen only: inline already has a fixed height from --height.
        if self.router_app.inline_height is not None:
            return
        # Tall portrait terminals stretch the chart; cap the box's aspect too.
        # After layout, so the tabs, actions, and footer count against the cap.
        self.call_after_refresh(self._apply_max_height)

    def _apply_max_height(self):
        frames = self.query("#frame")
        if not frames:
            # Left (or replaced, see account_changed) before layout ran.
            return
        frame = frames.first()
        rows = fit_rows(self.app.size.width)
        if rows is not None:
            chrome = sum(
                child.outer_size.height for child in self.children if child is not frame
            )
            rows = max(1, rows - chrome)
        frame.styles.max_height = rows

    def content(self) -> ComposeResult:
        yield Static("")

    def loading_style(self) -> str:
        """The class that shows this page's loading state."""
        return "loading"

    def nested(self) -> bool:
        return len(self.app.screen_stack) > 2

    def key_hints(self) -> str:
        """Shortcuts shown right-aligned beside the status line."""
        return self.hints("↑↓ move", "←→ switch", "↵ select")

    def hints(self, *keys: str) -> str:
        # ←→ is only advertised where it acts on the page; elsewhere it
        # silently switches tabs.
        if self.is_mounted and not self.horizontal_arrows():
            keys = tuple(key for key in keys if not key.startswith("←→"))
        elif self.is_mounted and isinstance(self.focused, Input):
            keys = tuple("←→ cursor" if k.startswith("←→") else k for k in keys)
        return "   ".join((*keys, f"esc {self.escape_hint()}"))

    def escape_hint(self) -> str:
        # Esc climbs: page content → the tab bar → Home → exit.
        from ramp_cli.router_ui.tabs.home import HomePage  # noqa: PLC0415

        if self.nested():
            return "back"
        if not self.is_mounted or not isinstance(self.focused, RouterTabs):
            return "menu"
        return "exit" if isinstance(self, HomePage) else "home"

    def on_descendant_focus(self, event):
        # A text box claims Left/Right only while its cursor can move.
        if isinstance(event.widget, Input) and not getattr(
            event.widget, "_hints_watched", False
        ):
            event.widget._hints_watched = True
            self.watch(
                event.widget,
                "selection",
                lambda _selection: self.refresh_key_hints(),
                init=False,
            )
        self.refresh_key_hints()

    def on_descendant_blur(self):
        self.refresh_key_hints()

    def refresh_key_hints(self):
        from ramp_cli.router_ui.tabs.home import HomePage  # noqa: PLC0415

        keys = self.query("#keys").first(KeyHints) if self.is_mounted else None
        if keys is not None:
            keys.update(self.key_hints())
        if isinstance(self, HomePage) and self.is_mounted:
            self.show_identity("")

    def arrow_unused(self, key: str):
        if self.nested() or self.app.busy:
            return
        tabs = self.query_one("#navigation", RouterTabs)
        tabs.action_next_tab() if key == "right" else tabs.action_previous_tab()

    def actions(self) -> ComposeResult:
        yield from ()

    @property
    def tab_section(self) -> str:
        from ramp_cli.router_ui.tabs.harnesses import (  # noqa: PLC0415
            DisconnectPage,
            HarnessesPage,
            HarnessModelPage,
            HarnessRoutingPage,
            LegacyPage,
        )
        from ramp_cli.router_ui.tabs.home import AccountPage  # noqa: PLC0415
        from ramp_cli.router_ui.tabs.keys import (  # noqa: PLC0415
            CreateKeyPage,
            KeyEditPage,
            KeyPage,
            KeysPage,
            SecretPage,
        )
        from ramp_cli.router_ui.tabs.strategies import StrategiesPage  # noqa: PLC0415

        if isinstance(self, (HarnessModelPage, HarnessRoutingPage)):
            return "harnesses"
        if isinstance(
            self, (KeysPage, KeyPage, CreateKeyPage, SecretPage, KeyEditPage)
        ):
            return "keys"
        if isinstance(self, (StrategiesPage, LegacyPage)):
            return "strategies"
        if isinstance(self, AccountPage):
            return "home"
        if isinstance(self, (HarnessesPage, DisconnectPage, ResultPage)):
            return "harnesses"
        return "home"

    def tab_pages(self) -> list[tuple[str, str]]:
        return list(PAGES)

    def compose(self) -> ComposeResult:
        from ramp_cli.router_ui.tabs.harnesses import HarnessesPage  # noqa: PLC0415
        from ramp_cli.router_ui.tabs.home import HomePage  # noqa: PLC0415

        with Horizontal(id="topbar"):
            yield RouterTabs(
                *(Tab(label, id=f"nav-{page}") for page, label in self.tab_pages()),
                active=f"nav-{self.tab_section}",
                id="navigation",
            )
            # Home shows its key hints up here instead of the email.
            corner = KeyHints if isinstance(self, HomePage) else Static
            yield corner("", id="identity", markup=False)
        with Vertical(id="frame"):
            yield Static("", id="activity", markup=False, expand=True)
            with Horizontal(id="status-row"):
                yield Static("", id="status", markup=False)
                yield KeyHints(self.key_hints(), id="keys", markup=False)
            body_container = (
                Vertical
                if isinstance(self, (CollectionPage, HarnessesPage))
                else VerticalScroll
            )
            with body_container(
                id="body", classes="table-body" if body_container is Vertical else ""
            ):
                yield from self.content()
        with Horizontal(id="page-actions", classes="actions"):
            yield from self.actions()
        yield RouterFooter()

    def on_mount(self):
        # Textual runs on_mount for every class in the MRO, and subclasses
        # also call super() to order their setup first; set up only once.
        if self.mounted_once:
            return
        self.mounted_once = True
        if self.router_app.inline_height is not None:
            self.styles.height = self.router_app.inline_height
        self.fit_max_height()
        # Padded so the line breaks around the title instead of touching it.
        self.query_one("#frame").border_title = f" {title_case(self.page_title)} "
        for label in self.query(Label):
            rendered = label.render()
            label.update(title_case(getattr(rendered, "plain", str(rendered))))
        for heading in self.query(".heading, .metric-label"):
            rendered = heading.render()
            heading.update(title_case(getattr(rendered, "plain", str(rendered))))
        for checkbox in self.query(Checkbox):
            checkbox.label = title_case(checkbox.label.plain)
        for selector in self.query(Select):
            selector.prompt = title_case(selector.prompt)
        self.lift_heading()
        self.show_identity(self.router_app.identity)
        self.router_app.load_page(self)
        self.query_one("#navigation", RouterTabs).focus()
        self.watch(self, "focused", self.keep_focus, init=False)

    def keep_focus(self, focused: Widget | None):
        # Disabling, hiding, or removing the focused widget leaves nothing
        # focused, and then no key reaches the UI. Fall back to the tabs,
        # which are never disabled, once any intended focus move settles.
        if focused is None:
            self.call_after_refresh(self.ensure_focus)

    def ensure_focus(self):
        if self.pending_focus is not None:
            self.focus_pending()
            return
        if self.is_mounted and self.focused is None and self.app.screen is self:
            self.action_focus_tabs()

    def restore_focus(self, widget: Widget):
        if widget.focusable and widget in self.focus_chain:
            self.pending_focus = None
            widget.focus()
            return
        # Just revealed after loading, it joins the focus chain only once
        # the page repaints. Until then the tabs hold focus as a fallback.
        self.pending_focus = widget
        fallback = self.focused
        self.call_after_refresh(self.focus_pending, fallback)

    def focus_pending(self, fallback: Widget | None = None):
        widget, self.pending_focus = self.pending_focus, None
        if (
            widget is None
            or not self.is_mounted
            or not widget.is_mounted
            # A result that moved focus on purpose keeps it.
            or self.focused not in (None, fallback)
        ):
            if self.focused is None:
                self.ensure_focus()
            return
        if widget.focusable and widget in self.focus_chain:
            widget.focus()
        elif self.focused is None:
            self.ensure_focus()

    def lift_heading(self):
        # A leading heading shares the key-hint row whenever no status shows.
        # on_mount can run once per class in the MRO; lift only once.
        row = self.query_one("#status-row")
        if row.has_class("has-heading"):
            return
        body = self.query_one("#body")
        heading = body.children[0] if body.children else None
        if heading is None or not heading.has_class("heading"):
            return
        rendered = heading.render()
        heading.remove()
        row.mount(
            Static(
                getattr(rendered, "plain", str(rendered)),
                classes="heading",
                markup=False,
            ),
            before="#status",
        )
        row.add_class("has-heading")

    def load(self):
        self.ready = True

    def message(self, message: str, *, error: bool = False):
        if self.is_mounted:
            status = self.query_one("#status", Static)
            status.set_class(error, "error")
            status.update(safe_message(message))
            self.query_one("#status-row").set_class(bool(message), "has-status")

    def show_identity(self, identity: str):
        from ramp_cli.router_ui.tabs.home import HomePage  # noqa: PLC0415

        if isinstance(self, HomePage):
            identity = self.key_hints()
        self.query_one("#identity", Static).update(identity)
        self.fit_identity()

    @on(events.Resize)
    def fit_identity(self):
        # Drop the email entirely when it would crowd the tabs.
        identity = self.query_one("#identity", Static)
        tabs = sum(len(label) + 2 for _page, label in self.tab_pages())
        needed = 2 + tabs + 2 + len(str(identity.content))
        identity.display = self.size.width >= needed

    @on(Tabs.TabActivated, "#navigation")
    def navigate(self, event: Tabs.TabActivated):
        event.stop()
        page = event.tab.id.removeprefix("nav-")
        if page == self.tab_section:
            return
        self.router_app.open(page)
        # A rejected switch must not show the wrong tab on the current view.
        if self.is_mounted:
            with event.tabs.prevent(Tabs.TabActivated):
                event.tabs.active = f"nav-{self.tab_section}"

    def action_focus_tabs(self):
        self.query_one("#navigation", RouterTabs).focus()

    def action_back(self):
        # Top-level tabs: Esc returns to the menu bar instead of leaving.
        tabs = self.query_one("#navigation", RouterTabs)
        if not self.nested() and self.focused is not tabs:
            tabs.focus()
        else:
            self.router_app.back()

    def action_reload(self):
        if not self.router_app.busy:
            # Reload is an explicit ask for fresh data, inside the cache TTL too.
            self.service.invalidate()
            self.router_app.load_page(self)

    def run(
        self,
        label: str,
        operation: Callable,
        complete: Callable | None = None,
        *,
        cancellable: bool = False,
        read: bool = False,
    ):
        """Run ``operation`` off the event loop; ``read`` marks it a fetch, so
        the account and staleness checks apply to its result."""
        self.router_app.run_operation(
            self, label, operation, complete, cancellable=cancellable, read=read
        )

    def run_and_reload(self, label: str, operation: Callable, note: str = ""):
        """Make a change, then reload the page, as one operation.

        Chaining a reload onto the change shows the loading state twice; run
        inside the page's own load instead, the change and the fresh read
        share one worker and one loading state.
        """
        app = self.router_app
        app.pending_write = (label, operation, note)
        try:
            self.load()
        finally:
            write, app.pending_write = app.pending_write, None
        if write is not None:
            # This page's load reads nothing, so the change runs by itself.
            self.run(label, operation, lambda _r: self.message(note or "Saved."))


class CellEditing:
    """Table cells that open a dropdown under themselves (see CellTable)."""

    cell_table_id = ""

    def cell_anchor(self, coordinate: Coordinate) -> Region:
        table = self.query_one(f"#{self.cell_table_id}", CellTable)
        cell = table._get_cell_region(coordinate)
        return cell.translate(table.content_region.offset - table.scroll_offset)

    def pick(
        self,
        coordinate: Coordinate,
        choices: list[tuple[str, str]],
        current: str | None,
        chosen: Callable[[str], None],
        header: str = "",
    ):
        def picked(value: str | None):
            if value is not None and value != current:
                chosen(value)

        def show():
            # After a load the table may only now be laid out again.
            if self.is_mounted and self.app.screen is self:
                self.app.push_screen(
                    CellMenu(choices, current, self.cell_anchor(coordinate), header),
                    picked,
                )

        self.call_after_refresh(show)


class CollectionPage(RouterPage):
    columns: tuple[str, ...] = ()
    filter_placeholder = "Filter by Name or ID"
    table_class: type[RouterTable] = RouterTable

    def __init__(self):
        super().__init__()
        self.entries: list[dict] = []

    def content(self) -> ComposeResult:
        from ramp_cli.router_ui.tabs.keys import KeysPage  # noqa: PLC0415

        yield Input(placeholder=self.filter_placeholder, id="filter")
        table = self.table_class(id="table", cursor_type="row", zebra_stripes=False)
        # Keys weight Name and Routing Profile; the shared even spread would undo that.
        table.spread = not isinstance(self, KeysPage)
        yield table

    def key_hints(self) -> str:
        return self.hints("↑↓ rows", "←→ buttons", "↵ actions")

    def arrow_target(self, widget: Widget | None, key: str) -> Widget | None:
        target = super().arrow_target(widget, key)
        if key == "down" and isinstance(widget, Tabs) and target is not None:
            # Down from the tabs skips the filter; Up from the table reaches it.
            if target.id == "filter":
                return self.query_one("#table", DataTable)
        return target

    def actions(self) -> ComposeResult:
        # Strategies change in their cells, so they have nothing to open.
        from ramp_cli.router_ui.tabs.strategies import StrategiesPage  # noqa: PLC0415

        if not isinstance(self, StrategiesPage):
            yield Button("Open", id="open")
        yield Button("Create", id="create")
        if isinstance(self, StrategiesPage):
            yield Button("Change Defaults", id="legacy")

    def on_mount(self):
        self.query_one("#table", DataTable).add_columns(*self.columns)
        super().on_mount()

    def row(self, item: dict) -> tuple:
        return ()

    def loaded(self, entries):
        self.entries = entries
        self.ready = True
        self.populate()

    def populate(self):
        from ramp_cli.router_ui.tabs.keys import KeysPage  # noqa: PLC0415

        table = self.query_one("#table", DataTable)
        selected = self.selected_id()
        column = table.cursor_column
        table.clear()
        term = self.query_one("#filter", Input).value.casefold()
        for item in self.entries:
            if term and term not in self.filter_text(item).casefold():
                continue
            table.add_row(
                *self.row(item),
                key=str(item["id"]),
                height=None if isinstance(self, KeysPage) else 1,
            )
        if selected in table.rows:
            # Reloading after a cell edit keeps the cursor on that cell.
            table.move_cursor(row=table.get_row_index(selected), column=column)
        self.call_after_refresh(self.layout_columns)
        for button in self.query("#open").results(Button):
            button.disabled = not table.row_count
        self.message(
            f"{table.row_count} shown"
            if table.row_count
            else "No matches. Create one or change the filter."
        )

    def filter_text(self, item: dict) -> str:
        return f"{item.get('name', '')} {item['id']}"

    def on_resize(self):
        self.call_after_refresh(self.layout_columns)

    def layout_columns(self):
        if self.router_app.live(self):
            self.query_one("#table", RouterTable).spread_columns()

    def selected_id(self) -> str | None:
        table = self.query_one("#table", DataTable)
        if not table.row_count:
            return None
        return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    @on(Input.Changed, "#filter")
    def filter_changed(self):
        if self.ready:
            self.populate()

    @on(DataTable.RowSelected, "#table")
    def row_selected(self, event: DataTable.RowSelected):
        focus_first_action(self)

    @on(Button.Pressed, "#open")
    def open_selected(self):
        if key := self.selected_id():
            self.open_entry(key)

    def open_entry(self, key: str):
        pass


class FormPage(RouterPage):
    def __init__(self):
        super().__init__()
        self.baseline: tuple | None = None

    def values(self) -> tuple:
        return tuple(
            (widget.id, getattr(widget, "value", None))
            for widget in self.query("Input, Select, Checkbox")
        ) + tuple(
            (widget.id, tuple(widget.selected)) for widget in self.query(SelectionList)
        )

    def remember(self):
        self.baseline = self.values()

    def action_back(self):
        if self.router_app.busy:
            self.router_app.back()
        elif self.baseline is not None and self.values() != self.baseline:
            self.router_app.confirm(
                "Discard unsaved changes?",
                lambda: self.router_app.back(force=True),
                "Discard",
            )
        else:
            self.router_app.back()

    def action_reload(self):
        # Never overwrite an unsaved form with a background refresh.
        self.message("Leave this form to reload. No changes are sent until Save.")


class ResultPage(RouterPage):
    def __init__(self, title: str, result: str):
        super().__init__()
        self.page_title = title
        self.result = result

    def content(self) -> ComposeResult:
        yield Static("Completed", classes="heading")
        yield BrandAnimation(id="success-animation")
        yield Static(self.result, markup=False, id="result")

    def actions(self) -> ComposeResult:
        yield Button("Back", id="done", variant="primary")

    def load(self):
        self.query_one("#success-animation", BrandAnimation).start(success=True)

    @on(Button.Pressed, "#done")
    def done(self):
        self.router_app.back()
