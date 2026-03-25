"""Tests for the interactive paginator — logic only, no raw terminal mode."""

import io
import os

from ramp_cli.output.paginator import ToolPaginator, _LineCounter
from ramp_cli.output.style import ESC
from ramp_cli.tools.commands import _detect_cursor_param, _extract_list_field
from ramp_cli.tools.parser import ParamType, ToolDef, ToolParam


class TestExtractListField:
    def test_extracts_first_list_of_dicts(self):
        data = {
            "total_count": 2,
            "transactions": [{"id": "a"}, {"id": "b"}],
        }
        key, items = _extract_list_field(data)
        assert key == "transactions"
        assert items == [{"id": "a"}, {"id": "b"}]

    def test_skips_non_dict_lists(self):
        data = {"tags": ["foo", "bar"], "items": [{"id": "a"}]}
        key, items = _extract_list_field(data)
        assert key == "items"
        assert items == [{"id": "a"}]

    def test_returns_none_for_no_lists(self):
        key, items = _extract_list_field({"status": "ok", "count": 0})
        assert key is None
        assert items == []

    def test_returns_none_for_non_dict(self):
        key, items = _extract_list_field("not a dict")
        assert key is None
        assert items == []

    def test_returns_none_for_empty_list(self):
        key, items = _extract_list_field({"results": []})
        assert key is None
        assert items == []


class TestDetectCursorParam:
    def _make_tool(self, param_names: list[str]) -> ToolDef:
        return ToolDef(
            name="test-tool",
            path="/test",
            http_method="post",
            summary="test",
            description="test",
            params=[
                ToolParam(
                    name=n,
                    flag=n,
                    description="",
                    type=ParamType.STRING,
                )
                for n in param_names
            ],
        )

    def test_prefers_next_page_cursor(self):
        tool = self._make_tool(["query", "next_page_cursor", "page_size"])
        assert _detect_cursor_param(tool) == "next_page_cursor"

    def test_falls_back_to_page_cursor(self):
        tool = self._make_tool(["query", "page_cursor", "limit"])
        assert _detect_cursor_param(tool) == "page_cursor"

    def test_falls_back_to_cursor(self):
        tool = self._make_tool(["cursor", "page_size", "status"])
        assert _detect_cursor_param(tool) == "cursor"

    def test_falls_back_to_start(self):
        tool = self._make_tool(["start", "page_size"])
        assert _detect_cursor_param(tool) == "start"

    def test_defaults_to_next_page_cursor(self):
        tool = self._make_tool(["query", "limit"])
        assert _detect_cursor_param(tool) == "next_page_cursor"


class TestPaginatorPageCaching:
    def test_pages_are_cached(self):
        """Navigating back should use cached pages, not re-fetch."""
        fetch_count = 0

        def mock_fetch(cursor):
            nonlocal fetch_count
            fetch_count += 1
            return [{"id": f"page2-{i}"} for i in range(3)], None

        paginator = ToolPaginator(
            title="Test",
            headers=["id"],
            initial_rows=[{"id": "page1-0"}, {"id": "page1-1"}],
            next_cursor="cursor-1",
            fetch_next_page=mock_fetch,
        )

        # Manually test cache behavior without running the interactive loop
        assert len(paginator._pages) == 1
        assert paginator._cursors == ["cursor-1"]

        # Simulate fetching next page
        paginator._next_page()
        assert len(paginator._pages) == 2
        assert fetch_count == 1

        # Go back
        paginator._prev_page()
        assert paginator._page_idx == 0

        # Go forward again — should NOT fetch
        paginator._next_page()
        assert fetch_count == 1  # still 1, used cache

    def test_no_fetch_when_no_cursor(self):
        """Should not try to fetch when there's no next cursor."""
        fetch_called = False

        def mock_fetch(cursor):
            nonlocal fetch_called
            fetch_called = True
            return [], None

        paginator = ToolPaginator(
            title="Test",
            headers=["id"],
            initial_rows=[{"id": "1"}],
            next_cursor=None,
            fetch_next_page=mock_fetch,
        )

        paginator._next_page()
        assert not fetch_called


class TestPaginatorRerender:
    """Re-renders should move cursor up and clear, not append new tables."""

    def test_second_render_moves_cursor_up(self):
        """After the first render, subsequent renders must use ESC[<n>A to
        move the cursor up by the number of previously rendered lines, then
        ESC[J to clear. This prevents the 'infinite scroll' duplication bug."""
        buf = io.StringIO()
        paginator = ToolPaginator(
            title="Test",
            headers=["id", "name"],
            initial_rows=[
                {"id": "1", "name": "Alice"},
                {"id": "2", "name": "Bob"},
            ],
            next_cursor=None,
            fetch_next_page=lambda c: ([], None),
            file=buf,
        )

        # First render — should NOT contain cursor-up sequences
        paginator._render()
        first_output = buf.getvalue()
        assert (
            f"{ESC}[" not in first_output or "A" not in first_output.split("[")[-1][:3]
        )

        # Second render — MUST move cursor up and clear
        buf.truncate(0)
        buf.seek(0)
        paginator._render()
        second_output = buf.getvalue()

        # Should start with \r (return to col 0) then ESC[<n>A (move up)
        assert second_output.startswith("\r")
        assert f"{ESC}[J" in second_output
        # Extract the cursor-up sequence: \rESC[<n>AESC[J
        parts = second_output.split(f"{ESC}[")
        assert any(p[0].isdigit() and "A" in p[:5] for p in parts[1:])

    def test_no_save_restore_cursor_sequences(self):
        """The paginator must NOT use ESC[s/ESC[u (save/restore cursor)
        as these break when content scrolls past the saved position."""
        buf = io.StringIO()
        paginator = ToolPaginator(
            title="Test",
            headers=["id"],
            initial_rows=[{"id": "1"}],
            next_cursor=None,
            fetch_next_page=lambda c: ([], None),
            file=buf,
        )

        paginator._render()
        paginator._render()
        output = buf.getvalue()

        assert f"{ESC}[s" not in output
        assert f"{ESC}[u" not in output

    def test_line_counter_preserves_isatty(self):
        """_LineCounter must delegate isatty() to the inner stream so that
        show_table_card renders with colors on a real TTY."""
        # Open /dev/tty or /dev/null as a proxy for TTY behavior
        # Use a real file to test __getattr__ delegation
        with open(os.devnull, "w") as f:
            counter = _LineCounter(f)
            # devnull is not a tty, but isatty() should be delegated
            assert counter.isatty() == f.isatty()

        # StringIO has no isatty by default that returns True,
        # but the delegation should still work
        buf = io.StringIO()
        counter = _LineCounter(buf)
        assert counter.isatty() == buf.isatty()

    def test_line_counter_counts_newlines(self):
        """_LineCounter must accurately count newlines written through it."""
        buf = io.StringIO()
        counter = _LineCounter(buf)
        counter.write("line1\nline2\nline3\n")
        assert counter.line_count == 3
        counter.write("no newline here")
        assert counter.line_count == 3
        counter.write("\n")
        assert counter.line_count == 4
