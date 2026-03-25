"""Interactive paginated table for list commands in human mode."""

from __future__ import annotations

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
        self._render()

    def _prev_page(self) -> None:
        """Navigate to the previous (cached) page."""
        if self._page_idx > 0:
            self._page_idx -= 1
            self._selected = 0
            self._render()

    def _render(self) -> None:
        """Render the current page using show_table_card + footer."""
        page = self._pages[self._page_idx]
        has_prev = self._page_idx > 0
        has_next = bool(self._cursors[self._page_idx]) or (
            self._page_idx + 1 < len(self._pages)
        )

        page_title = f"{self._title} [Page {self._page_idx + 1}]"

        if self._last_line_count > 0:
            # Return to column 0 (in case _render_loading left cursor mid-line),
            # move cursor up by the number of previously rendered lines, then
            # clear from cursor to end of screen.  This replaces the old
            # ESC[s / ESC[u (save/restore cursor) approach which breaks when
            # the table content scrolls past the saved cursor position.
            self._write(f"\r{ESC}[{self._last_line_count}A{ESC}[J")

        # Wrap the output file to count newlines while preserving TTY
        # color detection (StringIO buffering would lose isatty()).
        counter = _LineCounter(self._file)
        show_table_card(
            page_title,
            self._headers,
            page,
            file=counter,
            selected_row=self._selected,
        )

        # Footer
        footer = "\n" + self._build_footer(has_prev, has_next)
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

    def _build_footer(self, has_prev: bool, has_next: bool) -> str:
        """Build the keyboard hint footer."""
        buttons = [("ESC", "Exit"), ("↑↓", "Select")]
        if has_prev:
            buttons.append(("←", "Prev"))
        if has_next:
            buttons.append(("→", "Next"))
        buttons.append(("↵", "Open"))

        if self._use_color:
            return "  ".join(_render_button(k, v) for k, v in buttons) + "\n"
        return "  ".join(f"[{k}] {v}" for k, v in buttons) + "\n"
