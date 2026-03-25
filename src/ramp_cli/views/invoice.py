"""Render a bill as a cohesive invoice document inside terminal window chrome."""

from __future__ import annotations

import json
import shutil
import sys
from typing import Any

from ramp_cli.output.style import (
    _HEADER_BG,
    _MARGIN,
    _SHADOW_W,
    _WIDTH_MAX,
    _WIDTH_MIN,
    BOX_V,
    _bg,
    _bold,
    _color_supported,
    _fg,
    _frame_bottom,
    _frame_row,
    _frame_row_ansi,
    _frame_top,
    _frame_top_ansi,
    _gradient_text,
    _reset,
    _window_wrap,
)
from ramp_cli.output.utils import fmt_amount, fmt_date

_fmt_amount = fmt_amount


def _fmt_date(iso: str | None) -> str:
    return fmt_date(iso, default="—")


def render_bill_invoice(body: bytes) -> bool:
    """Render bill as styled invoice. Returns True if rendered, False to fallback."""
    if not (hasattr(sys.stdout, "isatty") and sys.stdout.isatty()):
        return False

    use_color = _color_supported(sys.stdout)
    data: dict[str, Any] = json.loads(body)

    term_cols = shutil.get_terminal_size((80, 24)).columns
    available = term_cols - _MARGIN - _SHADOW_W
    width = min(max(_WIDTH_MIN, available), _WIDTH_MAX)
    inner = width - 4  # content area between │ ... │

    lines: list[str] = []

    # --- 1. Title bar ---
    invoice_num = data.get("invoice_number", "")
    title_text = (
        f"Viewing: INVOICE #{invoice_num}" if invoice_num else "Viewing: INVOICE"
    )
    if use_color:
        title_ansi = f"{_bold()}{title_text}{_reset()}"
        lines.append(_frame_top_ansi(title_ansi, len(title_text), width))
    else:
        lines.append(_frame_top(title_text, width, use_color=False))

    # --- 2. Vendor block ---
    vendor = data.get("vendor", {}) or {}
    vendor_name = vendor.get("name") or vendor.get("remote_name") or "Unknown Vendor"
    if use_color:
        vn = f"{_bold()}{_fg(255, 255, 255)}{vendor_name}{_reset()}"
        lines.append(_frame_row_ansi(vn, len(vendor_name), width))
    else:
        lines.append(_frame_row(vendor_name, width))

    vendor_type = vendor.get("type", "")
    if vendor_type:
        lines.append(_frame_row(vendor_type.replace("_", " ").title(), width))
    remote_code = vendor.get("remote_code", "")
    if remote_code:
        lines.append(_frame_row(f"Code: {remote_code}", width))

    # --- 3. Separator ---
    lines.append(_frame_row("", width))

    # --- 4. Billed To ---
    owner = data.get("bill_owner", {}) or {}
    owner_name = f"{owner.get('first_name', '')} {owner.get('last_name', '')}".strip()
    if use_color:
        label = f"{_bold()}{_fg(255, 255, 255)}Billed To:{_reset()}"
        lines.append(_frame_row_ansi(label, len("Billed To:"), width))
    else:
        lines.append(_frame_row("Billed To:", width))
    if owner_name:
        lines.append(_frame_row(f"  {owner_name}", width))

    # --- 5. Separator ---
    lines.append(_frame_row("", width))

    # --- 6. Order details ---
    if invoice_num:
        lines.append(_frame_row(f"Order No.: {invoice_num}", width))
    issued = _fmt_date(data.get("issued_at"))
    lines.append(_frame_row(f"Purchase Date: {issued}", width))

    # --- 7. Separator ---
    lines.append(_frame_row("", width))

    # --- 8. Invoice due ---
    due = _fmt_date(data.get("due_at"))
    if use_color:
        due_label = f"{_fg(200, 200, 200)}Invoice due: {_bold()}{_fg(255, 255, 255)}{due}{_reset()}"
        due_visible = len(f"Invoice due: {due}")
        lines.append(_frame_row_ansi(due_label, due_visible, width))
    else:
        lines.append(_frame_row(f"Invoice due: {due}", width))

    # --- 9. Separator ---
    lines.append(_frame_row("", width))

    # --- 10. Line items table ---
    inv_items = data.get("inventory_line_items") or []
    simple_items = data.get("line_items") or []

    has_inventory = bool(inv_items)
    has_simple = bool(simple_items)

    if has_inventory or has_simple:
        _render_line_items_table(
            lines, inv_items, simple_items, inner, width, use_color
        )

    # --- 11. Summary rows ---
    _render_summary(lines, data, inv_items, simple_items, inner, width, use_color)

    # --- 12. Blank space ---
    lines.append(_frame_row("", width))

    # --- 13. "Whats next?" ---
    if use_color:
        wn_text = _gradient_text(
            "Whats next?", start=(180, 180, 180), end=(100, 100, 100)
        )
        lines.append(_frame_row_ansi(wn_text, len("Whats next?"), width))
    else:
        lines.append(_frame_row("Whats next?", width))

    # --- 14. Bottom border ---
    lines.append(_frame_bottom(width))

    # Wrap with window chrome
    wrapped = _window_wrap(lines, width)

    for line in wrapped:
        sys.stdout.write(line + "\n")
    sys.stdout.flush()
    return True


def _render_line_items_table(
    lines: list[str],
    inv_items: list[dict],
    simple_items: list[dict],
    inner: int,
    width: int,
    use_color: bool,
) -> None:
    """Build line items table rows and append to lines."""
    has_inventory = bool(inv_items)

    if has_inventory:
        # 5-column: #, Desc., Unit Rate, Count, Amount (USD)
        col_num = 4
        col_amount = 14
        col_rate = 12
        col_count = 7
        col_desc = inner - col_num - col_rate - col_count - col_amount

        # Header row
        hdr = (
            f"{'#':<{col_num}}"
            f"{'Desc.':<{col_desc}}"
            f"{'Unit Rate':>{col_rate}}"
            f"{'Count':>{col_count}}"
            f"{'Amount (USD)':>{col_amount}}"
        )
        _append_header_row(lines, hdr, inner, width, use_color)

        # Inventory items
        for i, item in enumerate(inv_items, 1):
            memo = (item.get("memo") or "")[: col_desc - 1]
            unit_price = _fmt_amount(item.get("unit_price"))
            qty = str(item.get("quantity") or "")
            amount = _fmt_amount(item.get("amount"))

            row = (
                f"{str(i):<{col_num}}"
                f"{memo:<{col_desc}}"
                f"{unit_price:>{col_rate}}"
                f"{qty:>{col_count}}"
                f"{amount:>{col_amount}}"
            )
            lines.append(_frame_row(row, width))

        # If also has simple items, add separator
        if simple_items:
            lines.append(_frame_row("", width))
            offset = len(inv_items)
            for i, item in enumerate(simple_items, offset + 1):
                memo = (item.get("memo") or "")[: col_desc - 1]
                amount = _fmt_amount(item.get("amount"))
                row = (
                    f"{str(i):<{col_num}}"
                    f"{memo:<{col_desc}}"
                    f"{'':>{col_rate}}"
                    f"{'':>{col_count}}"
                    f"{amount:>{col_amount}}"
                )
                lines.append(_frame_row(row, width))
    else:
        # 3-column: #, Description, Amount (USD)
        col_num = 4
        col_amount = 14
        col_desc = inner - col_num - col_amount

        hdr = (
            f"{'#':<{col_num}}{'Description':<{col_desc}}{'Amount (USD)':>{col_amount}}"
        )
        _append_header_row(lines, hdr, inner, width, use_color)

        for i, item in enumerate(simple_items, 1):
            memo = (item.get("memo") or "")[: col_desc - 1]
            amount = _fmt_amount(item.get("amount"))
            row = f"{str(i):<{col_num}}{memo:<{col_desc}}{amount:>{col_amount}}"
            lines.append(_frame_row(row, width))


def _append_header_row(
    lines: list[str], hdr: str, inner: int, width: int, use_color: bool
) -> None:
    """Append a table header row with background highlighting."""
    if use_color:
        padded_hdr = hdr + " " * (inner - len(hdr))
        styled = (
            f"{_bg(*_HEADER_BG)}{_bold()}{_fg(255, 255, 255)}{padded_hdr}{_reset()}"
        )
        lines.append(f"{BOX_V} {styled} {BOX_V}")
    else:
        lines.append(_frame_row(hdr, width))


def _render_summary(
    lines: list[str],
    data: dict[str, Any],
    inv_items: list[dict],
    simple_items: list[dict],
    inner: int,
    width: int,
    use_color: bool,
) -> None:
    """Render summary rows (subtotal, discount, tax, total) right-aligned."""
    lines.append(_frame_row("", width))

    # Calculate subtotal from line items
    all_items = (inv_items or []) + (simple_items or [])
    subtotal_cents = 0
    for item in all_items:
        amt = item.get("amount", 0)
        if isinstance(amt, dict):
            amt = amt.get("amount", 0) or 0
        subtotal_cents += amt or 0

    total = data.get("amount", 0)
    if isinstance(total, dict):
        total = total.get("amount", 0) or 0

    label_width = 20
    amount_width = 14

    def _summary_row(label: str, value: int, bold_row: bool = False) -> None:
        formatted = _fmt_amount(value)
        pad_left = inner - label_width - amount_width
        if use_color and bold_row:
            row_text = (
                f"{' ' * pad_left}"
                f"{_bold()}{_fg(255, 255, 255)}{label:>{label_width}}"
                f"{formatted:>{amount_width}}{_reset()}"
            )
            lines.append(_frame_row_ansi(row_text, inner, width))
        else:
            row_text = (
                f"{' ' * pad_left}{label:>{label_width}}{formatted:>{amount_width}}"
            )
            lines.append(_frame_row(row_text, width))

    # Show subtotal if it differs from total (implies discount/tax)
    if subtotal_cents and subtotal_cents != total:
        _summary_row("Subtotal", subtotal_cents)
        diff = total - subtotal_cents
        if diff < 0:
            _summary_row("Discount", diff)
            _summary_row("Net Sales Total", total)
        elif diff > 0:
            _summary_row("Tax", diff)

    _summary_row("Total", total, bold_row=True)
