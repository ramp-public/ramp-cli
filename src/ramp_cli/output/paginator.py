"""Interactive paginated table for list commands in human mode."""

from __future__ import annotations

import shutil
import sys
from typing import Callable, TextIO

from ramp_cli.output.style import (
    ESC,
    _color_supported,
    _fg,
    _hide_cursor,
    _read_key,
    _render_button,
    _reset,
    _show_cursor,
    show_table_card,
)

# Non-data lines consumed by the table card + footer: blank above, frame top,
# header, frame bottom, shadow bar, blank below, footer gap, footer buttons,
# plus 1 for the cursor line after the trailing newline = 9.
_TABLE_CHROME_LINES = 9
_MIN_VISIBLE_ROWS = 1


class _LineCounter:
    """Thin wrapper around a TextIO that counts newlines written through it.

    Delegates all attribute access (including ``isatty``) to the underlying
    stream so that color-detection in ``show_table_card`` sees the real TTY.
    """

    def __init__(self, inner: TextIO) -> None:
        self._inner = inner
        self.line_count = 0

    def write(self, data: str) -> int:
        self.line_count += data.count("\n")
        return self._inner.write(data)

    def flush(self) -> None:
        self._inner.flush()

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class ToolPaginator:
    """Interactive paginated table with server-side cursor pagination.

    Renders using `show_table_card()` for the table itself. Adds keyboard
    navigation (left/right for pages, up/down for row selection, Enter to
    open detail). Pages are fetched lazily and cached for instant backward
    navigation.
    """

    def __init__(
        self,
        title: str,
        headers: list[str],
        initial_rows: list[dict[str, str]],
        next_cursor: str | None,
        fetch_next_page: Callable[[str], tuple[list[dict], str | None]],
        file: TextIO | None = None,
    ) -> None:
        self._title = title
        self._headers = headers
        self._file = file or sys.stdout
        self._use_color = _color_supported(self._file)

        # Page cache: list of rows per page
        self._pages: list[list[dict[str, str]]] = [initial_rows]
        self._cursors: list[str | None] = [next_cursor]
        self._fetch = fetch_next_page

        self._page_idx = 0
        self._selected = 0
        self._viewport_start = 0  # index of first visible row
        self._last_line_count = 0

    def run(self) -> dict | None:
        """Run the interactive paging loop.

        Returns the selected row dict if Enter is pressed, or None on ESC/q.
        """
        if not sys.stdin.isatty():
            show_table_card(self._title, self._headers, self._pages[0], file=self._file)
            return None

        self._write(_hide_cursor())
        try:
            self._render()
            while True:
                key = _read_key()
                if key in ("esc", "q"):
                    return None
                elif key == "enter":
                    page = self._pages[self._page_idx]
                    if page and 0 <= self._selected < len(page):
                        return page[self._selected]
                    return None
                elif key == "right":
                    self._next_page()
                elif key == "left":
                    self._prev_page()
                elif key == "down":
                    page = self._pages[self._page_idx]
                    if self._selected < len(page) - 1:
                        self._selected += 1
                        self._render()
                elif key == "up":
                    if self._selected > 0:
                        self._selected -= 1
                        self._render()
        finally:
            self._write(_show_cursor())

    def _write(self, data: str) -> None:
        self._file.write(data)
        self._file.flush()

    def _next_page(self) -> None:
        """Navigate to the next page, fetching from API if needed."""
        if self._page_idx + 1 < len(self._pages):
            self._page_idx += 1
            self._selected = 0
            self._viewport_start = 0
            self._render()
            return

        cursor = self._cursors[self._page_idx]
        if not cursor:
            return

        self._render_loading()

        raw_items, new_cursor = self._fetch(cursor)
        if not raw_items:
            self._cursors[self._page_idx] = None
            self._render()
            return

        self._pages.append(raw_items)
        self._cursors.append(new_cursor)
        self._page_idx += 1
        self._selected = 0
        self._viewport_start = 0
        self._render()

    def _prev_page(self) -> None:
        """Navigate to the previous (cached) page."""
        if self._page_idx > 0:
            self._page_idx -= 1
            self._selected = 0
            self._viewport_start = 0
            self._render()

    def _max_visible_rows(self) -> int:
        """How many data rows fit on screen given current terminal height."""
        term_h = shutil.get_terminal_size((80, 24)).lines
        return max(_MIN_VISIBLE_ROWS, term_h - _TABLE_CHROME_LINES)

    def _clamp_viewport(self, page: list[dict]) -> None:
        """Ensure selected row is visible and viewport is within bounds."""
        cap = self._max_visible_rows()
        # Scroll viewport so selected row is visible.
        if self._selected < self._viewport_start:
            self._viewport_start = self._selected
        elif self._selected >= self._viewport_start + cap:
            self._viewport_start = self._selected - cap + 1
        # Don't let viewport extend past end of page.
        max_start = max(0, len(page) - cap)
        self._viewport_start = min(self._viewport_start, max_start)

    def _render(self) -> None:
        """Render the current page using show_table_card + footer."""
        page = self._pages[self._page_idx]
        has_prev = self._page_idx > 0
        has_next = bool(self._cursors[self._page_idx]) or (
            self._page_idx + 1 < len(self._pages)
        )

        # Compute viewport slice so the table fits the terminal height.
        self._clamp_viewport(page)
        cap = self._max_visible_rows()
        vp_start = self._viewport_start
        vp_end = min(vp_start + cap, len(page))
        visible_rows = page[vp_start:vp_end]
        # Translate selected index to viewport-relative index.
        vis_selected = self._selected - vp_start

        # Build title with page + scroll position info.
        page_title = f"{self._title} [Page {self._page_idx + 1}]"
        if len(page) > cap:
            page_title += f"  {vp_start + 1}\u2013{vp_end} of {len(page)}"

        if self._last_line_count > 0:
            # Return to column 0 (in case _render_loading left cursor mid-line),
            # move cursor up by the number of previously rendered lines, then
            # clear from cursor to end of screen.  This replaces the old
            # ESC[s / ESC[u (save/restore cursor) approach which breaks when
            # the table content scrolls past the saved cursor position.
            self._write(f"\r{ESC}[{self._last_line_count}A{ESC}[J")
        else:
            # First render: clear screen and home cursor so pre-existing
            # output (e.g. summary line) doesn't eat into the table's
            # available height.
            self._write(f"{ESC}[H{ESC}[J")

        # Wrap the output file to count newlines while preserving TTY
        # color detection (StringIO buffering would lose isatty()).
        counter = _LineCounter(self._file)
        show_table_card(
            page_title,
            self._headers,
            visible_rows,
            file=counter,
            selected_row=vis_selected,
        )

        # Footer — add scroll arrows when rows are clipped.
        scroll_hint = ""
        if vp_start > 0:
            scroll_hint += f" ▲ {vp_start} more"
        if vp_end < len(page):
            scroll_hint += f" ▼ {len(page) - vp_end} more"
        footer = "\n" + self._build_footer(has_prev, has_next, scroll_hint)
        counter.write(footer)
        counter.flush()

        self._last_line_count = counter.line_count

    def _render_loading(self) -> None:
        """Show a loading indicator in the footer area."""
        if self._use_color:
            msg = f"\r{_fg(228, 242, 33)}  ▓ Loading next page...{_reset()}"
        else:
            msg = "\r  Loading next page..."
        self._write(msg)

    def _build_footer(
        self, has_prev: bool, has_next: bool, scroll_hint: str = ""
    ) -> str:
        """Build the keyboard hint footer."""
        buttons = [("ESC", "Exit"), ("↑↓", "Select")]
        if has_prev:
            buttons.append(("←", "Prev"))
        if has_next:
            buttons.append(("→", "Next"))
        buttons.append(("↵", "Open"))

        if self._use_color:
            bar = "  ".join(_render_button(k, v) for k, v in buttons)
            if scroll_hint:
                bar += f"  {_fg(120, 120, 120)}{scroll_hint}{_reset()}"
            return bar + "\n"
        bar = "  ".join(f"[{k}] {v}" for k, v in buttons)
        if scroll_hint:
            bar += f"  {scroll_hint}"
        return bar + "\n"
