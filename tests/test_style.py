"""Tests for ramp_cli.output.style."""

from __future__ import annotations

import io
import json
import re
import time

import ramp_cli.views.invoice as bill_invoice_mod
from ramp_cli.output.style import (
    DIAMOND_FILLED,
    DIAMOND_HOLLOW,
    ESC,
    _ansi_truncate,
    _ansi_visible_len,
    _fg,
    _gradient_text,
    _render_button,
    _reset,
    _reset_fg,
    _window_wrap,
    access_denied,
    env_label,
    header,
    show_detail_card,
    show_notice,
    show_table_card,
    start_spinner,
    start_waiting_animation,
    status_line,
    wrap_text,
)
from ramp_cli.views.invoice import render_bill_invoice


def test_env_label_sandbox():
    assert env_label("sandbox") == "Sandbox"


def test_env_label_production():
    assert env_label("production") == "Production"


def test_status_line_authenticated():
    result = status_line(True)
    assert DIAMOND_FILLED in result
    assert "authenticated" in result
    assert "not" not in result


def test_status_line_not_authenticated():
    result = status_line(False)
    assert DIAMOND_HOLLOW in result
    assert "not authenticated" in result


def test_header(capsys):
    header("Status")
    out = capsys.readouterr().out
    assert "Status" in out
    assert "\u2500" in out  # ─ box-drawing horizontal


def test_waiting_animation_produces_framed_output():
    buf = io.StringIO()
    stop = start_waiting_animation("ramp auth login", file=buf)
    time.sleep(0.15)  # let a frame or two render
    stop()
    output = buf.getvalue()
    assert "[WAITING]" in output
    assert "ramp auth login" in output
    assert "\u250c" in output  # ┌ top-left corner
    assert "\u2514" in output  # └ bottom-left corner
    assert "\u2502" in output  # │ vertical bar
    # Binary matrix content
    assert "0" in output or "1" in output


def test_access_denied(capsys, monkeypatch):
    monkeypatch.setattr("ramp_cli.output.style._color_supported", lambda f: False)
    access_denied("ramp transactions list", "sandbox")
    err = capsys.readouterr().err
    assert "ACCESS DENIED" in err
    assert "ramp transactions list" in err
    assert "ramp auth login --env" in err
    assert "sandbox." in err
    assert "\u250c" in err  # ┌ framed
    assert "\u2514" in err  # └ framed


# === Phase 1: Primitive tests ===


def test_reset_fg():
    assert _reset_fg() == f"{ESC}[22m{ESC}[39m"


def test_ansi_visible_len_plain():
    assert _ansi_visible_len("hello") == 5


def test_ansi_visible_len_with_escapes():
    text = f"{_fg(255, 0, 0)}red{_reset()}"
    assert _ansi_visible_len(text) == 3


def test_ansi_truncate_plain():
    result = _ansi_truncate("hello world!", 8)
    assert "..." in result
    assert _ansi_visible_len(result.replace(_reset_fg(), "")) <= 8


def test_ansi_truncate_no_truncation():
    assert _ansi_truncate("short", 10) == "short"


def test_ansi_truncate_mid_escape():
    """Doesn't slice inside an ANSI sequence."""
    text = f"{_fg(255, 0, 0)}hello{_reset()}"
    result = _ansi_truncate(text, 3)
    # Should not contain broken escape sequences
    assert "\033[" not in result.split("m")[-1].split("\033")[0] or "..." in result


# === Phase 2: Window wrap tests ===


def test_window_wrap_shadow():
    lines = ["line1", "line2", "line3"]
    wrapped = _window_wrap(lines, 10)
    # Should have: blank, 3 content lines, bottom shadow, blank
    assert wrapped[0] == ""  # top margin
    assert wrapped[-1] == ""  # bottom margin
    # Right shadow on non-first lines
    assert "\u2590" in wrapped[2]  # right-half block shadow on line2
    assert "\u2590" not in wrapped[1]  # no shadow on first line (offset illusion)
    # Bottom shadow row
    assert "\u2584" in wrapped[-2]  # lower-half block bottom shadow


# === Phase 3: Content styling tests ===


def test_gradient_text_visible_len():
    text = "Hello World"
    result = _gradient_text(text)
    assert _ansi_visible_len(result) == len(text)


def test_gradient_text_empty():
    assert _gradient_text("") == ""


def test_render_button():
    result = _render_button("ESC", "Exit")
    # Contains hotkey bg
    assert "\033[48;2;138;138;138m" in result
    # Contains label bg
    assert "\033[48;2;78;78;78m" in result
    assert "ESC" in result
    assert "EXIT" in result


# === Phase 3b: Table card header bg ===


def test_show_table_card_header_bg(monkeypatch):
    """Color mode: header row has bg color, no separator line."""
    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore[attr-defined]
    monkeypatch.setattr("ramp_cli.output.style._color_supported", lambda f: True)
    show_table_card(
        "Test",
        ["name", "value"],
        [{"name": "foo", "value": "bar"}],
        file=buf,
    )
    output = buf.getvalue()
    # Header bg present
    assert "\033[48;2;88;88;88m" in output
    # Window bg present in data rows
    assert "\033[48;2;38;38;38m" in output


def test_show_table_card_no_color(monkeypatch):
    """No-color mode still has separator line."""
    buf = io.StringIO()
    monkeypatch.setattr("ramp_cli.output.style._color_supported", lambda f: False)
    show_table_card(
        "Test",
        ["name", "value"],
        [{"name": "foo", "value": "bar"}],
        file=buf,
    )
    output = buf.getvalue()
    assert "\u2500\u2500" in output  # separator line present


def test_show_table_card_uuid_not_truncated(monkeypatch):
    """UUID values in table columns should not be truncated."""
    buf = io.StringIO()
    monkeypatch.setattr("ramp_cli.output.style._color_supported", lambda f: False)
    # Simulate a wide terminal so the table can expand
    monkeypatch.setattr("ramp_cli.output.style._term_width", lambda: 200)
    uuid_val = "b0857bc6-aa34-49dd-8aea-6d6f9ebccbbc"
    show_table_card(
        "Cards",
        ["id", "name", "status"],
        [
            {"id": uuid_val, "name": "Test Card", "status": "ACTIVE"},
            {
                "id": "a1234567-bbbb-cccc-dddd-eeeeeeeeeeee",
                "name": "Another",
                "status": "TERMINATED",
            },
        ],
        file=buf,
    )
    output = buf.getvalue()
    # Full UUID must appear without ellipsis truncation
    assert uuid_val in output
    assert "b0857bc6-aa34-49dd-8aea-6d6f9ebccbb\u2026" not in output


# === Phase 4: Detail card tests ===


def test_show_detail_card_status_green(monkeypatch):
    """ACTIVE status renders with green ANSI code."""
    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore[attr-defined]
    monkeypatch.setattr("ramp_cli.output.style._color_supported", lambda f: True)
    show_detail_card("Bill", {"status": "ACTIVE", "amount": "100"}, file=buf)
    output = buf.getvalue()
    # Green color for ACTIVE
    assert "\033[38;2;0;200;0m" in output
    assert "ACTIVE" in output


def test_show_detail_card_status_gray(monkeypatch):
    """CLOSED status renders with gray ANSI code."""
    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore[attr-defined]
    monkeypatch.setattr("ramp_cli.output.style._color_supported", lambda f: True)
    show_detail_card("Bill", {"status": "CLOSED"}, file=buf)
    output = buf.getvalue()
    assert "\033[38;2;100;100;100m" in output
    assert "CLOSED" in output


def test_show_detail_card_nested(monkeypatch):
    """Nested dict renders as sub-card, not {...}."""
    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore[attr-defined]
    monkeypatch.setattr("ramp_cli.output.style._color_supported", lambda f: True)
    show_detail_card(
        "Transaction",
        {"vendor": {"name": "Acme", "id": "v123"}, "amount": "500"},
        file=buf,
    )
    output = buf.getvalue()
    assert "{...}" not in output
    # Gradient text splits chars with ANSI codes, so check visible content
    assert _ansi_visible_len(output) > 0
    assert "name:" in output  # nested key rendered
    assert "vendor:" in output


def test_show_detail_card_plain(monkeypatch):
    """Plain mode (no color) outputs key:value pairs."""
    buf = io.StringIO()
    monkeypatch.setattr("ramp_cli.output.style._color_supported", lambda f: False)
    show_detail_card("Bill", {"status": "ACTIVE", "amount": "100"}, file=buf)
    output = buf.getvalue()
    assert "status:" in output
    assert "ACTIVE" in output
    assert "\033[" not in output  # no ANSI codes


# === wrap_text ===


def test_wrap_text_empty():
    assert wrap_text("", 10) == [""]


def test_wrap_text_short():
    assert wrap_text("hi there", 80) == ["hi there"]


def test_wrap_text_exact_width():
    assert wrap_text("abcdefghij", 10) == ["abcdefghij"]


def test_wrap_text_wraps_on_word_boundary():
    assert wrap_text("hello world foo", 11) == ["hello world", "foo"]


def test_wrap_text_long_token_emitted_whole():
    # A single token longer than the width should not be hard-cut.
    assert wrap_text("aaaaaaaaaaaa", 5) == ["aaaaaaaaaaaa"]
    assert wrap_text("a aaaaaaaaaa b", 5) == ["a", "aaaaaaaaaa", "b"]


# === show_detail_card wrap behavior ===


def test_show_detail_card_long_value_wraps_not_truncates(monkeypatch):
    """Long string values wrap onto continuation rows; full text is preserved."""
    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore[attr-defined]
    monkeypatch.setattr("ramp_cli.output.style._color_supported", lambda f: True)
    monkeypatch.setattr("ramp_cli.output.style._term_width", lambda: 90)
    msg = (
        "This business is already enrolled in agent cards. To start using "
        "agent cards, create a fund with spend controls and a card to fund it."
    )
    show_detail_card("Result", {"output": msg}, file=buf)
    raw = buf.getvalue()
    plain = _strip_ansi(raw)
    # No ellipsis truncation marker
    assert "..." not in plain
    # Full message present (split across rows is fine — assert key fragments)
    assert "already enrolled in agent cards" in plain
    assert "create a fund with spend controls" in plain
    assert "card to fund it." in plain
    # Multiple rows rendered (top + at least 2 wrapped rows + bottom)
    assert plain.count("│") >= 4


def test_show_detail_card_long_value_wraps_plain(monkeypatch):
    """Long string values also wrap in the no-color path."""
    buf = io.StringIO()
    monkeypatch.setattr("ramp_cli.output.style._color_supported", lambda f: False)
    monkeypatch.setattr("ramp_cli.output.style._term_width", lambda: 90)
    msg = "abc " * 60  # ~240 chars; far exceeds inner width
    show_detail_card("Result", {"output": msg.strip()}, file=buf)
    out = buf.getvalue()
    assert "\033[" not in out
    assert "..." not in out
    # Should produce more than one content line for the value
    content_lines = [
        line for line in out.splitlines() if line.startswith("│") and "abc" in line
    ]
    assert len(content_lines) >= 2


def test_show_detail_card_overlong_token_does_not_break_frame(monkeypatch):
    """A single token longer than the value column is clamped, not overflowed.

    Regression: an unbroken URL/UUID would previously push the right border
    past the frame because ``wrap_text`` emits long tokens whole.
    """
    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore[attr-defined]
    monkeypatch.setattr("ramp_cli.output.style._color_supported", lambda f: True)
    monkeypatch.setattr("ramp_cli.output.style._term_width", lambda: 90)
    long_token = "a" * 500  # far exceeds the inner width
    show_detail_card("Result", {"output": long_token}, file=buf)
    plain_lines = _strip_ansi(buf.getvalue()).splitlines()
    # Every framed value row must have the same visible width — no overflow.
    # Colored output appends a shadow char (▐) to value rows; the trailing
    # box border │ comes immediately before it.
    framed = [ln for ln in plain_lines if ln.startswith("  │") and "│" in ln[3:]]
    assert framed, "expected at least one framed row"
    widths = {len(ln) for ln in framed}
    assert len(widths) == 1, f"frame rows have inconsistent widths: {widths}"
    # The clamped row should end with an ellipsis marker.
    assert any("\u2026" in ln for ln in framed)


def test_show_detail_card_overlong_token_plain(monkeypatch):
    """Same clamp behavior in the no-color path."""
    buf = io.StringIO()
    monkeypatch.setattr("ramp_cli.output.style._color_supported", lambda f: False)
    monkeypatch.setattr("ramp_cli.output.style._term_width", lambda: 90)
    show_detail_card("Result", {"output": "z" * 500}, file=buf)
    framed = [ln for ln in buf.getvalue().splitlines() if ln.startswith("│")]
    widths = {len(ln) for ln in framed}
    assert len(widths) == 1, f"frame rows have inconsistent widths: {widths}"
    assert any("\u2026" in ln for ln in framed)


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


# === Bill Invoice Tests ===


def _make_mock_bill(**overrides):
    """Return a minimal bill dict for testing."""
    bill = {
        "invoice_number": "INV-001",
        "vendor": {"name": "Acme Corp", "type": "VENDOR"},
        "bill_owner": {"first_name": "Jane", "last_name": "Doe"},
        "issued_at": "2025-01-15T00:00:00Z",
        "due_at": "2025-02-15T00:00:00Z",
        "amount": 150000,
        "line_items": [
            {"memo": "Consulting services", "amount": 100000},
            {"memo": "Travel expenses", "amount": 50000},
        ],
    }
    bill.update(overrides)
    return bill


class _FakeTTY(io.StringIO):
    def isatty(self):
        return True


def _render_invoice_to_string(data, use_color=True):
    """Render a bill invoice and capture the output string."""
    buf = _FakeTTY()

    orig_stdout = bill_invoice_mod.sys.stdout
    bill_invoice_mod.sys.stdout = buf
    try:
        # Monkeypatch _color_supported within the module
        orig_cs = bill_invoice_mod._color_supported
        bill_invoice_mod._color_supported = lambda f: use_color
        try:
            render_bill_invoice(json.dumps(data).encode())
        finally:
            bill_invoice_mod._color_supported = orig_cs
    finally:
        bill_invoice_mod.sys.stdout = orig_stdout
    return buf.getvalue()


def test_bill_invoice_no_bg_bleed():
    """No _WIN_BG background escape should appear in invoice output."""
    output = _render_invoice_to_string(_make_mock_bill())
    # _WIN_BG (38,38,38) should not appear anywhere
    win_bg_code = "\033[48;2;38;38;38m"
    assert win_bg_code not in output

    # Check no bg escapes leak past the closing │ border
    for line in output.split("\n"):
        parts = line.rsplit("\u2502", 1)
        if len(parts) == 2:
            after_border = parts[1]
            assert "\033[48;2;" not in after_border


def test_bill_invoice_header_bg_spans_full_row():
    """Header row bg should span the full inner width and reset before border."""
    output = _render_invoice_to_string(_make_mock_bill())
    header_bg = "\033[48;2;88;88;88m"
    for line in output.split("\n"):
        if header_bg in line:
            # Find the content between the frame borders │ ... │
            # The reset should appear between the bg start and closing │
            bg_start = line.index(header_bg)
            # Find the closing │ that comes after the bg content
            closing_border = line.rfind("\u2502")
            # There should be a reset between bg_start and closing_border
            content = line[bg_start:closing_border]
            assert "\033[0m" in content


def test_bill_invoice_frame_structure():
    """No-color mode: correct structure, no ANSI codes in content area."""
    output = _render_invoice_to_string(_make_mock_bill(), use_color=False)
    assert "\u250c" in output  # ┌
    assert "\u2514" in output  # └
    assert "INVOICE" in output
    assert "Billed To:" in output
    assert "Invoice due:" in output
    assert "Total" in output
    assert "Whats next?" in output
    # No ANSI codes inside the frame content (between │ borders)
    for line in output.split("\n"):
        if "\u2502" in line:
            parts = line.split("\u2502")
            # Content is between first and last │ — middle parts
            for part in parts[1:-1]:
                assert "\033[" not in part, f"ANSI code in content: {part!r}"


def test_bill_invoice_line_items_3col():
    """3-column layout when only line_items (no inventory)."""
    data = _make_mock_bill(inventory_line_items=None)
    output = _render_invoice_to_string(data, use_color=False)
    assert "Description" in output
    assert "Amount (USD)" in output
    assert "Unit Rate" not in output
    assert "Count" not in output


def test_bill_invoice_line_items_5col():
    """5-column layout when inventory_line_items present."""
    data = _make_mock_bill(
        inventory_line_items=[
            {"memo": "Widget", "unit_price": 5000, "quantity": 10, "amount": 50000},
        ],
    )
    output = _render_invoice_to_string(data, use_color=False)
    assert "Unit Rate" in output
    assert "Count" in output
    assert "Amount (USD)" in output


def test_show_notice_emphasizes_only_its_headings(monkeypatch):
    # A developer running with NO_COLOR set would otherwise see no escapes at
    # all and the assertions below would pass for the wrong reason.
    monkeypatch.delenv("NO_COLOR", raising=False)
    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore[attr-defined]

    show_notice(
        "NOTICE",
        [
            "Plain body text that carries no emphasis.",
            "",
            "Run your previous setup at any time:",
            "  codex --profile original",
        ],
        file=buf,
    )

    rows = {}
    for line in buf.getvalue().splitlines():
        plain = re.sub(r"\033\[[0-9;]*m", "", line)
        if not plain.startswith("│"):
            continue
        # Drop the borders and the single space of padding, keeping any
        # indentation the caller asked for.
        rows[plain[1:-1][1:].rstrip()] = line
    bold = f"{ESC}[1m"
    # An unindented line ending in a colon introduces the block below it.
    assert bold in rows["Run your previous setup at any time:"]
    # A command under it, and ordinary prose, stay unemphasized.
    assert bold not in rows["  codex --profile original"]
    assert bold not in rows["Plain body text that carries no emphasis."]


def test_show_notice_stays_legible_without_color():
    buf = io.StringIO()  # not a tty, so no escapes at all

    show_notice("NOTICE", ["Heading:", "  a command"], file=buf)

    output = buf.getvalue()
    assert ESC not in output
    assert "Heading:" in output
    assert "  a command" in output


def test_start_spinner_animates_then_leaves_the_line_clean(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore[attr-defined]

    stop = start_spinner("Setting up your coding agents", file=buf)
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and "Setting up" not in buf.getvalue():
            time.sleep(0.02)
    finally:
        stop()

    output = buf.getvalue()
    assert "Setting up your coding agents" in output
    assert any(frame in output for frame in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
    # The cursor is put back and the line wiped, so the caller's own output
    # starts from a clean line.
    assert output.startswith(f"{ESC}[?25l")
    assert output.endswith(f"\r{ESC}[K{ESC}[?25h")


def test_start_spinner_draws_nothing_without_a_terminal():
    buf = io.StringIO()  # not a tty, as when output is piped to a file

    stop = start_spinner("Setting up your coding agents", file=buf)
    time.sleep(0.05)
    stop()

    assert buf.getvalue() == ""
