"""Shared widgets, constants, and keyboard helpers for the Router app."""

import re
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from functools import lru_cache

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events, on
from textual.actions import SkipAction
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import (
    Horizontal,
    ScrollableContainer,
    Vertical,
)
from textual.content import Content
from textual.coordinate import Coordinate
from textual.geometry import Region
from textual.message import Message
from textual.screen import ModalScreen, Screen, ScreenResultType
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import (
    Button as TextualButton,
)
from textual.widgets import (
    DataTable,
    Footer,
    Input,
    OptionList,
    Select,
    SelectionList,
    Static,
    Tabs,
)
from textual.widgets.selection_list import Selection

from ramp_cli import __version__
from ramp_cli.router_ui import border
from ramp_cli.router_ui.animation import (
    screen_shows,
)
from ramp_cli.router_ui.palette import (
    Palette,
)
from ramp_cli.router_ui.terminal import (
    draws_block_glyphs,
)

border.register()

PAGES = (
    ("home", "Home"),
    ("harnesses", "Harnesses"),
    ("strategies", "Strategies"),
    ("keys", "API Keys"),
)
REFRESHING = "Refreshing Data"
TAB_PAGES = frozenset(page for page, _label in PAGES)
# Frames at least this wide show the metric cards as 3×2 instead of 2×2.
WIDE_METRICS = 120
# How long new usage data takes to grow its bars up from the baseline.
CHART_GROW_SECONDS = 0.3
# Fewer chart rows than this and Home shows only the metric cells.
MIN_CHART_ROWS = 8


def title_case(value: str) -> str:
    """Title Case for UI chrome, preserving acronyms and brand spellings."""
    title, separator, hint = value.partition(" · ")
    special = {
        "api": "API",
        "id": "ID",
        "cli": "CLI",
        "url": "URL",
        "json": "JSON",
        "nvidia": "NVIDIA",
        "nemo": "NeMo",
        "opencode": "OpenCode",
        "byok": "BYOK",
        "gpt": "GPT",
    }
    minor = {
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "for",
        "from",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
    first = True

    def word(match):
        nonlocal first
        text = match.group()
        lower = text.lower()
        result = special.get(
            lower, lower if not first and lower in minor else text.capitalize()
        )
        first = False
        return result

    return re.sub(r"[A-Za-z]+(?:['’]s)?", word, title) + separator + hint


class PaletteText(Text):
    """Semantic cell color which can be updated after terminal detection."""

    def __init__(self, value: str, palette: Palette, role: str = "accent"):
        super().__init__(value, style=palette.rich(role))
        self.role = role


@lru_cache(maxsize=64)
def button_styles(
    palette: Palette, focused: bool, filled: bool, disabled: bool
) -> tuple[Style, Style, Style]:
    """Fill, bold label, and outline styles for a Button state."""
    role = "accent_text" if filled else "disabled" if disabled else "foreground"
    fill = Style(
        color=palette.rich(role),
        bgcolor=palette.rich("accent" if filled else "background"),
    )
    rule = Style(
        color=palette.rich("accent" if focused else "button_border"),
        bgcolor=palette.rich("background"),
    )
    return fill, fill + Style(bold=True), rule


class Button(TextualButton):
    """Keep the outline stable and fill the entire interior on focus."""

    def validate_label(self, label) -> Content:
        parsed = super().validate_label(label)
        return Content.from_text(title_case(parsed.plain))

    def on_mount(self):
        self.label = title_case(self.label.plain)

    def render_line(self, y: int) -> Strip:
        width = self.content_size.width
        inner = max(0, width - 2)
        label = Text(self.label.plain)
        label.truncate(inner, overflow="ellipsis")
        padding = max(0, inner - label.cell_len)
        # A cell can't be split, so an odd gap would leave the label a cell left
        # of centre. Draw the outline one cell narrower instead and leave that
        # cell blank outside it (see centred_width for fixed-width buttons).
        trim = padding % 2
        inner -= trim
        padding -= trim
        palette = self.app.palette
        focused = self.has_focus
        filled = focused
        fill, bold_fill, rule = button_styles(palette, focused, filled, self.disabled)
        # Box-drawing lines run through the middle of their cells. Focused, the
        # outline becomes half and quarter blocks so the fill ends on that same
        # centre line: no gap inside the border and no overhang past it.
        if filled and draws_block_glyphs():
            top, bottom, edge = ("▗", "▄", "▖"), ("▝", "▀", "▘"), ("▐", "▌")
        elif filled:
            # Where blocks leave seams, plain cell backgrounds don't: the fill
            # covers the outline's whole box instead.
            top = bottom = edge = (" ", " ", " ")
            rule = fill
        else:
            top, bottom, edge = ("┌", "─", "┐"), ("└", "─", "┘"), ("│", "│")
        spare = Segment(" " * trim, rule)
        last = self.content_size.height - 1
        if y in (0, last):
            left, line, right = top if y == 0 else bottom
            return Strip([Segment(left + line * inner + right, rule), spare], width)
        return Strip(
            [
                Segment(edge[0], rule),
                Segment(" " * (padding // 2), fill),
                Segment(label.plain, bold_fill),
                Segment(" " * (padding // 2), fill),
                Segment(edge[1], rule),
                spare,
            ],
            width,
        )


def centred_width(label: str, width: int = 18) -> int:
    """The nearest width at or above `width` that centres `label` exactly."""
    # A cell can't be split, so the space beside a label only halves evenly
    # when the label and the button's interior share a parity.
    return width + (width - 2 - len(label)) % 2


class RouterTabs(Tabs):
    """One focus stop: Left/Right select tabs, Enter/Down enters content."""

    BINDINGS = [Binding("enter", "focus_content", "Open screen", show=False)]

    def action_previous_tab(self):
        if not getattr(self.screen, "holds_secret", False):
            super().action_previous_tab()

    def action_next_tab(self):
        if not getattr(self.screen, "holds_secret", False):
            super().action_next_tab()

    def action_focus_content(self):
        if self.app.busy:
            return
        # A list page opens on its table, not the filter box above it.
        tables = [t for t in self.screen.query("#body DataTable") if t.focusable]
        if tables:
            tables[0].focus()
            return
        self.screen.focus_next(
            "#body Button, #body Input, #body DataTable, #body Select, "
            "#body Checkbox, #body SelectionList, #page-actions Button"
        )

    # The email's underline continues this bar, so it tracks focus too.
    # While the tabs have focus, tables hide their cursor so the first row
    # doesn't look selected; hiding it keeps each cell's own color.
    def on_focus(self):
        self.screen.query("#identity").add_class("focused")
        for table in self.screen.query(DataTable):
            table.show_cursor = False

    def on_blur(self):
        self.screen.query("#identity").remove_class("focused")
        for table in self.screen.query(DataTable):
            table.show_cursor = True


def focus_first_action(screen) -> None:
    for button in screen.query("#page-actions Button"):
        if button.display and not button.disabled:
            button.focus()
            return


def _literal(cell):
    """Table cells show text as written: DataTable reads plain strings as markup,
    so an API value like "[red]Admin" would restyle itself or fail to render."""
    return Text(cell) if isinstance(cell, str) else cell


class RouterTable(DataTable):
    # Tables share leftover width evenly across columns unless a page lays
    # its columns out itself.
    spread = True

    def action_select_cursor(self):
        focus_first_action(self.screen)

    def on_resize(self):
        if not self.spread:
            return
        # The first spread runs before the next paint so columns never jump;
        # later resizes (a window drag) settle before the table is re-laid.
        if self._spread_key is None:
            self.call_after_refresh(self.spread_columns)
            return
        if self._spread_timer is not None:
            self._spread_timer.stop()
        self._spread_timer = self.set_timer(0.06, self.spread_columns)

    # Content edits bump a version so spreading can skip rescanning rows.
    _content_version = 0
    _spread_key: tuple[int, int] | None = None
    _spread_minimums: tuple[int, list[int]] | None = None
    _spread_timer: Timer | None = None

    def _content_changed(self):
        self._content_version += 1

    def add_column(self, *args, **kwargs):
        self._content_changed()
        return super().add_column(*args, **kwargs)

    def add_row(self, *cells, **kwargs):
        self._content_changed()
        return super().add_row(*map(_literal, cells), **kwargs)

    def update_cell(self, row_key, column_key, value, **kwargs):
        self._content_changed()
        return super().update_cell(row_key, column_key, _literal(value), **kwargs)

    def remove_row(self, *args, **kwargs):
        self._content_changed()
        return super().remove_row(*args, **kwargs)

    def remove_column(self, *args, **kwargs):
        self._content_changed()
        return super().remove_column(*args, **kwargs)

    def clear(self, *args, **kwargs):
        self._content_changed()
        return super().clear(*args, **kwargs)

    def spread_columns(self):
        if self._spread_timer is not None:
            self._spread_timer.stop()
            self._spread_timer = None
        if not self.is_mounted or not self.columns:
            return
        columns = list(self.columns.values())
        available = max(
            0,
            self.scrollable_content_region.width - 2 * self.cell_padding * len(columns),
        )
        key = (self._content_version, available)
        if key == self._spread_key:
            return
        self._spread_key = key
        if (
            self._spread_minimums is not None
            and self._spread_minimums[0] == self._content_version
        ):
            minimums = self._spread_minimums[1]
        else:
            rows = [self.get_row_at(index) for index in range(self.row_count)]
            minimums = [
                max(
                    [cell_len(column.label.plain)]
                    + [
                        cell_len(value.plain if isinstance(value, Text) else str(value))
                        for value in (row[index] for row in rows)
                    ]
                )
                for index, column in enumerate(columns)
            ]
            self._spread_minimums = (self._content_version, minimums)
        extra = max(0, available - sum(minimums))
        base, remainder = divmod(extra, len(columns))
        widths = [
            minimum + base + (index < remainder)
            for index, minimum in enumerate(minimums)
        ]
        self.set_column_widths(widths)

    def set_column_widths(self, widths: list[int]):
        columns = list(self.columns.values())
        if all(
            not column.auto_width and column.width == width
            for column, width in zip(columns, widths, strict=True)
        ):
            return
        for column, width in zip(columns, widths, strict=True):
            column.auto_width = False
            column.width = width
        # Textual 8 exposes Column metadata but no public width-update API.
        self._clear_caches()
        self._update_dimensions(self.rows)
        self.refresh(layout=True)


class CellTable(RouterTable):
    """Enter steps from a row into its editable cells; Esc steps back out.

    By default the first column names the row and is never editable, so the
    cell cursor stays to its right. Enter on a cell posts CellSelected.
    """

    BINDINGS = [Binding("escape", "leave_cells", "Rows", show=False)]
    # Leftmost column the cell cursor can reach; 0 makes the name editable too.
    first_cell_column = 1

    class ModeChanged(Message):
        def __init__(self, table: "CellTable"):
            super().__init__()
            self.table = table

        @property
        def control(self) -> "CellTable":
            return self.table

    def cell_locked(self, coordinate: Coordinate) -> bool:
        """A cell the cursor passes over, because it can't be changed."""
        return False

    def validate_cursor_coordinate(self, value: Coordinate) -> Coordinate:
        value = super().validate_cursor_coordinate(value)
        if self.cursor_type != "cell":
            return value
        first = self.first_cell_column
        if value.column < first < len(self.columns):
            value = Coordinate(value.row, first)
        if not self.cell_locked(value):
            return value
        # Keep going the way the cursor was heading, else the other way.
        step = -1 if value.column < self.cursor_coordinate.column else 1
        for direction in (step, -step):
            column = value.column + direction
            while max(first, 0) <= column < len(self.columns):
                candidate = Coordinate(value.row, column)
                if not self.cell_locked(candidate):
                    return candidate
                column += direction
        return value

    def check_action(self, action: str, parameters) -> bool | None:
        # In row mode Esc falls through to the page's own Back.
        if action == "leave_cells":
            return self.cursor_type == "cell"
        return super().check_action(action, parameters)

    def action_select_cursor(self):
        if not self.row_count:
            return
        if self.cursor_type == "row":
            self.enter_cells()
        else:
            DataTable.action_select_cursor(self)

    def enter_cells(self):
        # Cell mode first, so the move skips any locked cell.
        self.cursor_type = "cell"
        self.cursor_coordinate = Coordinate(self.cursor_row, self.first_cell_column)
        self.post_message(self.ModeChanged(self))

    def action_leave_cells(self):
        self.cursor_type = "row"
        self.post_message(self.ModeChanged(self))

    def on_blur(self):
        # Only focus moving to another control here leaves the cells. Loading
        # hides the table for a moment and drops focus to nothing; a modal
        # opened from a cell hands it back afterwards. Neither is leaving.
        focused = self.screen.focused
        if (
            self.cursor_type == "cell"
            and not self.screen.has_class("loading")
            and focused is not None
            and focused is not self
        ):
            self.action_leave_cells()


class InlineModal(ModalScreen[ScreenResultType]):
    """A modal that fills the --height region when the app runs inline."""

    def on_mount(self):
        # Textual also runs each subclass's on_mount; the new height only
        # takes effect at the next layout, so their setup order is unchanged.
        if self.app.inline_height is not None:
            self.styles.height = self.app.inline_height


class CellMenu(InlineModal[str | None]):
    """A dropdown under a table cell: Enter picks, Esc or a click outside cancels."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        choices: list[tuple[str, str]],
        current: str | None,
        anchor: Region,
        header: str = "",
    ):
        super().__init__()
        self.choices = choices
        self.current = current
        self.anchor = anchor
        self.header = header

    def compose(self) -> ComposeResult:
        menu = OptionList(
            *(Text(label) for label, _value in self.choices), id="cell-menu"
        )
        if not self.header:
            yield menu
            return
        # Column headers stay put while the choices scroll under them.
        with Vertical(id="cell-menu-box"):
            yield Static(Text(self.header), id="cell-menu-header")
            yield menu

    def on_mount(self):
        menu = self.query_one("#cell-menu", OptionList)
        size = self.size if self.size.height else self.app.size
        # Fit the longest choice (plus border and scrollbar), not the cell.
        labels = [label for label, _ in self.choices] + [self.header]
        width = max(12, 4 + max(cell_len(label) for label in labels))
        width = min(width, max(1, size.width))
        rows = min(len(self.choices), 10)
        height = rows + 2 + (1 if self.header else 0)
        x = max(0, min(self.anchor.x, size.width - width))
        y = self.anchor.bottom
        if y + height > size.height and self.anchor.y >= height:
            y = self.anchor.y - height
        frame = menu
        if self.header:
            frame = self.query_one("#cell-menu-box")
            menu.styles.max_height = rows
            if len(self.choices) > rows:
                # Clear the list's scrollbar so each header sits over its column.
                self.query_one("#cell-menu-header").styles.padding = (
                    0,
                    menu.styles.scrollbar_size_vertical,
                    0,
                    0,
                )
        frame.styles.width = width
        frame.styles.max_height = height
        frame.styles.offset = (x, y)
        values = [value for _label, value in self.choices]
        menu.highlighted = values.index(self.current) if self.current in values else 0
        menu.focus()

    @on(OptionList.OptionSelected, "#cell-menu")
    def chosen(self, event: OptionList.OptionSelected):
        event.stop()
        self.dismiss(self.choices[event.option_index][1])

    def on_click(self, event: events.Click):
        if event.widget is self:
            self.dismiss(None)

    def action_cancel(self):
        self.dismiss(None)


class CellChecklist(InlineModal[set[str] | None]):
    """A scrolling checklist under a table cell: Enter or Space toggles, Esc is done.

    Done dismisses with the checked values; nothing changes until then.
    """

    BINDINGS = [Binding("escape", "done", "Done")]

    def __init__(
        self,
        choices: list[tuple[str, str, bool, bool]],
        anchor: Region,
        title: str = "",
    ):
        # Each choice is (label, value, checked, locked).
        super().__init__()
        self.choices = choices
        self.anchor = anchor
        self.title_text = title

    def compose(self) -> ComposeResult:
        yield SelectionList[str](
            *(
                Selection(Text(label), value, checked, disabled=locked)
                for label, value, checked, locked in self.choices
            ),
            id="cell-checklist",
        )

    def on_mount(self):
        checklist = self.query_one("#cell-checklist", SelectionList)
        checklist.border_title = self.title_text
        checklist.border_subtitle = "↵ toggle · esc done"
        size = self.size if self.size.height else self.app.size
        # Labels, the checkbox, border, and padding; at least the subtitle.
        width = max(
            24,
            cell_len(self.title_text) + 4,
            8 + max((cell_len(label) for label, *_ in self.choices), default=0),
        )
        width = min(width, max(1, size.width))
        height = min(len(self.choices), 10) + 2
        x = max(0, min(self.anchor.x, size.width - width))
        y = self.anchor.bottom
        if y + height > size.height and self.anchor.y >= height:
            y = self.anchor.y - height
        checklist.styles.width = width
        checklist.styles.max_height = height
        checklist.styles.offset = (x, y)
        checklist.focus()

    def on_click(self, event: events.Click):
        if event.widget is self:
            self.action_done()

    def action_done(self):
        self.dismiss(set(self.query_one("#cell-checklist", SelectionList).selected))


ARROW_BINDINGS = [
    Binding(key, f"arrow('{key}')", show=False, priority=True)
    for key in ("up", "down", "left", "right")
]


def arrow_row(widget: Widget) -> Widget:
    """Controls side by side in one Horizontal share a row."""
    return widget.parent if isinstance(widget.parent, Horizontal) else widget


def scroll_area(widget: Widget | None) -> bool:
    """A scrolling container, as opposed to a control that scrolls itself."""
    return isinstance(widget, ScrollableContainer) and not isinstance(
        widget, ScrollView
    )


def arrow_owned(widget: Widget, key: str) -> bool:
    """Whether the focused widget uses this arrow itself, short of its edge."""
    vertical = key in ("up", "down")
    back = key in ("up", "left")
    if any(
        isinstance(node, Select) and node.expanded
        for node in (widget, *widget.ancestors)
    ):
        return True
    if isinstance(widget, Tabs):
        return not vertical
    if isinstance(widget, Input):
        if vertical:
            return False
        position = widget.cursor_position
        return position > 0 if back else position < len(widget.value)
    if isinstance(widget, OptionList) and vertical:
        index = widget.highlighted
        if index is None:
            return not back and bool(widget.option_count)
        return index > 0 if back else index < widget.option_count - 1
    if isinstance(widget, DataTable) and widget.cursor_type == "cell" and not vertical:
        return True
    if isinstance(widget, DataTable) and vertical:
        if not widget.row_count:
            return False
        # A table that skips rows says itself whether one is left to reach.
        reachable = getattr(widget, "row_reachable", None)
        if reachable is not None:
            return reachable(back)
        row = widget.cursor_row
        return row > 0 if back else row < widget.row_count - 1
    if isinstance(widget, ScrollableContainer):
        offset, limit = (
            (widget.scroll_y, widget.max_scroll_y)
            if vertical
            else (widget.scroll_x, widget.max_scroll_x)
        )
        return offset > 0 if back else offset < limit
    return False


class ArrowNavigation:
    """Arrow keys always reach a control, so no widget can swallow them.

    Up and Down move between rows of controls, Left and Right within a row.
    A widget that uses an arrow itself (a table or list cursor, an open menu,
    a text cursor, a scroll area) keeps it until it reaches its edge.
    """

    def action_arrow(self, key: str):
        focused = self.focused
        if focused is not None and arrow_owned(focused, key):
            raise SkipAction()
        target = self.arrow_target(focused, key)
        if target is not None:
            target.focus()
        elif key in ("left", "right"):
            self.arrow_unused(key)

    def arrow_unused(self, key: str):
        """Left or Right with nothing beside the focused control."""

    def horizontal_arrows(self) -> bool:
        """Whether Left/Right do something for the focused control."""
        focused = self.focused
        return focused is not None and any(
            arrow_owned(focused, key) or self.arrow_target(focused, key) is not None
            for key in ("left", "right")
        )

    def arrow_target(self, widget: Widget | None, key: str) -> Widget | None:
        chain = self.focus_chain
        controls = [w for w in chain if not scroll_area(w)]
        rows: list[list[Widget]] = []
        for control in controls:
            if rows and arrow_row(control) is arrow_row(rows[-1][0]):
                rows[-1].append(control)
            else:
                rows.append([control])
        if not rows:
            return None
        if widget not in controls:
            # Nothing focused, or a scroll area: it sits just before the
            # controls inside it, so Down enters them and Up leaves.
            later = chain[chain.index(widget) :] if widget in chain else []
            first = next((w for w in later if w in controls), None)
            row = next((i for i, r in enumerate(rows) if first in r), len(rows))
            if widget is None:
                return rows[0][0]
            if key == "down":
                return rows[row][0] if row < len(rows) else None
            return rows[row - 1][0] if key == "up" and row else None
        row = next(i for i, r in enumerate(rows) if widget in r)
        if key in ("left", "right"):
            cells = rows[row]
            if len(cells) < 2:
                return None
            step = 1 if key == "right" else -1
            return cells[(cells.index(widget) + step) % len(cells)]
        row += 1 if key == "down" else -1
        if not 0 <= row < len(rows):
            return None
        # Land on the control most directly above or below this one.
        return min(rows[row], key=lambda cell: abs(cell.region.x - widget.region.x))


def parse_spend_cap(text: str) -> str | None:
    """A cap the API accepts, canonical; blank is no cap. ValueError explains."""
    raw = text.strip().removeprefix("$").replace(",", "").strip()
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        value = None
    if value is None or not value.is_finite() or value <= 0:
        raise ValueError("Enter a spend cap greater than 0, or clear it for no cap.")
    value = value.normalize()
    exponent = value.as_tuple().exponent
    whole = value.adjusted() + 1
    if whole > 10 or (exponent < 0 and -exponent > 10):
        raise ValueError(
            "Enter a spend cap with at most 10 digits before and after the decimal point."
        )
    return format(value, "f")


class CellInput(ArrowNavigation, InlineModal[tuple | None]):
    """A popover under a table cell: a text field, Back, and Set (or `submit`).

    Set dismisses with `(parse(text),)`, so a parsed None still differs from
    Back's None. `parse` raises ValueError with a message to show instead.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel"), *ARROW_BINDINGS]

    def __init__(
        self,
        value: str,
        anchor: Region,
        parse: Callable[[str], object],
        placeholder: str = "",
        submit: str = "Set",
        *,
        title: str = "",
        width: int = 34,
        above: bool = False,
    ):
        super().__init__()
        self.value = value
        self.anchor = anchor
        self.parse = parse
        self.placeholder = placeholder
        self.submit_label = submit
        self.title = title
        self.min_width = width
        # Above the anchor, its right edge in line with the anchor's.
        self.above = above

    def compose(self) -> ComposeResult:
        with Vertical(id="cell-input"):
            yield Input(self.value, placeholder=self.placeholder, id="cell-value")
            yield Static("", id="cell-error", markup=False)
            with Horizontal(classes="actions"):
                yield Button("Back", id="cell-back")
                yield Button(self.submit_label, id="cell-set", variant="primary")

    def on_mount(self):
        box = self.query_one("#cell-input", Vertical)
        size = self.size if self.size.height else self.app.size
        width = min(max(self.min_width, self.anchor.width), max(1, size.width))
        # Field, buttons, and border; one more row while an error shows.
        height = 9
        x = self.anchor.right - width if self.above else self.anchor.x
        x = max(0, min(x, size.width - width))
        y = self.anchor.bottom
        if self.above or (y + height > size.height and self.anchor.y >= height):
            y = max(0, self.anchor.y - height)
        box.styles.width = width
        box.styles.offset = (x, y)
        if self.title:
            box.border_title = f" {self.title} "
        self.query_one("#cell-error").display = False
        field = self.query_one("#cell-value", Input)
        field.focus()
        field.action_end()

    @on(Input.Submitted, "#cell-value")
    @on(Button.Pressed, "#cell-set")
    def submit(self, event: Message):
        event.stop()
        try:
            value = self.parse(self.query_one("#cell-value", Input).value)
        except ValueError as error:
            message = self.query_one("#cell-error", Static)
            message.update(str(error))
            message.display = True
            return
        self.dismiss((value,))

    @on(Button.Pressed, "#cell-back")
    def back(self, event: Button.Pressed):
        event.stop()
        self.dismiss(None)

    def on_click(self, event: events.Click):
        if event.widget is self:
            self.dismiss(None)

    def action_cancel(self):
        self.dismiss(None)


class KeyHints(Static):
    """Shortcut hints: each key a touch brighter than the word after it."""

    COMPONENT_CLASSES = {"keys--key"}

    def render(self) -> Text:
        key = self.get_component_rich_style("keys--key")
        text = Text(no_wrap=True)
        for index, hint in enumerate(str(self.content).split("   ")):
            name, space, word = hint.partition(" ")
            text.append("   " if index else "")
            text.append(name, key)
            text.append(space + word)
        return text


class RouterFooter(Footer):
    """A Footer that recomposes only when the keys it would show change.

    Textual's Footer rebuilds all of its FooterKey widgets on every bindings
    change, which fires on each focus move and tab switch (~14 ms) even when
    the visible keys are the same. CSS for `Footer` still applies.
    """

    _shown: tuple | None = None

    def _signature(self) -> tuple:
        return (
            self.compact,
            self.show_command_palette,
            tuple(
                (binding, enabled, tooltip)
                for _node, binding, enabled, tooltip in (
                    self.screen.active_bindings.values()
                )
            ),
        )

    def compose(self) -> ComposeResult:
        self._shown = self._signature() if self._bindings_ready else None
        yield from super().compose()
        # Docked right, like Textual's own command-palette key.
        yield Static(f"ramp v{__version__}", id="footer-version", markup=False)

    def bindings_changed(self, screen: Screen) -> None:
        if (
            self._shown is not None
            and screen is self.screen
            and self._signature() == self._shown
        ):
            return
        super().bindings_changed(screen)


class MetricDivider(Static):
    """Horizontal rule that crosses the metric grid's vertical dividers."""

    def render(self) -> Text:
        width = self.size.width
        line = ["─"] * width
        row = self.screen.query(".metric-row").first()
        for card in row.query(".account-metric"):
            cross = card.region.right - 1 - self.region.x
            if card.display and not card.has_class("row-end") and 0 <= cross < width:
                line[cross] = "┼"
        return Text("".join(line))


class Marquee(Widget):
    """One line that slides to its end and back, only while it's wider than the widget."""

    # Ticks to hold at each end: about a second at 0.15s per tick.
    PAUSE = 7
    # Blank cells after the last name when scrolled fully right.
    END_PAD = 2

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.text = Text()
        self.shift = 0
        self.direction = 1
        self.hold = self.PAUSE
        self.timer: Timer | None = None

    def on_mount(self):
        self.timer = self.set_interval(0.15, self.step, pause=True)
        self.app.screen_change_signal.subscribe(self, lambda _screen: self.sync())

    def on_show(self):
        self.sync()

    def on_hide(self):
        if self.timer:
            self.timer.pause()

    def update(self, text: Text):
        self.text = text
        self.shift = 0
        self.direction = 1
        self.hold = self.PAUSE
        self.sync()

    def on_resize(self):
        self.shift = min(self.shift, self.max_shift)
        self.sync()

    @property
    def overflows(self) -> bool:
        return self.text.cell_len > self.size.width

    @property
    def max_shift(self) -> int:
        return max(0, self.text.cell_len + self.END_PAD - self.size.width)

    @property
    def scrolling(self) -> bool:
        return self.overflows and not getattr(self.app, "reduced_motion", False)

    def sync(self):
        # The timer runs only while the line overflows on a visible, current
        # screen. A width of 0 means not laid out yet; hidden ancestors send
        # Hide, and showing again sends Show and Resize. Toggling reduced
        # motion and terminal focus changes call sync.
        if self.timer:
            if not getattr(self.app, "app_focus", True):
                # Unfocused: hold the current offset and resume from it.
                self.timer.pause()
                self.refresh()
                return
            try:
                shown = (
                    self.visible
                    and self.size.width > 0
                    and screen_shows(self.screen)
                    and self.is_on_screen
                )
            except Exception:
                shown = False
            if shown and self.scrolling:
                self.timer.resume()
            else:
                self.timer.pause()
                if self.shift:
                    self.shift, self.direction, self.hold = 0, 1, self.PAUSE
        self.refresh()

    def step(self):
        if not self.scrolling:
            if self.shift:
                self.shift, self.direction, self.hold = 0, 1, self.PAUSE
                self.refresh()
            return
        if self.hold:
            self.hold -= 1
            return
        self.shift = max(0, min(self.max_shift, self.shift + self.direction))
        if self.shift in (0, self.max_shift):
            # Rest at either edge, then head back the other way.
            self.direction = -1 if self.shift else 1
            self.hold = self.PAUSE
        self.refresh()

    def render(self) -> Text:
        width = self.size.width
        if self.text.cell_len <= width:
            return self.text
        if not self.scrolling:
            # Reduced motion holds still, so the cut is marked instead.
            text = self.text.copy()
            text.truncate(width, overflow="ellipsis")
            return text
        text = Text.assemble(self.text, " " * self.END_PAD, no_wrap=True)
        return text[self.shift : self.shift + width]
