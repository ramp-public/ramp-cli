"""Render a unified receipt view for transactions, receipts, reimbursements, and purchase orders."""

from __future__ import annotations

import json
import shutil
import sys
from typing import Any

from ramp_cli.output.style import (
    _HEADER_BG,
    _MARGIN,
    _SHADOW_W,
    _STATUS_GRAY,
    _STATUS_GREEN,
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
    _reset_fg,
    _window_wrap,
)
from ramp_cli.output.utils import currency_symbol, fmt_amount, fmt_date

_fmt_amount = fmt_amount
_fmt_date = fmt_date
_currency_symbol = currency_symbol


def render_receipt_view(
    body: bytes, resource_type: str, use_color: bool | None = None
) -> bool:
    """Render unified receipt view. Returns True if rendered, False to fallback.

    resource_type: transaction | receipt | reimbursement | purchase-order
    """
    if not (hasattr(sys.stdout, "isatty") and sys.stdout.isatty()):
        return False

    if use_color is None:
        use_color = _color_supported(sys.stdout)

    try:
        data: dict[str, Any] = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False

    term_cols = shutil.get_terminal_size((80, 24)).columns
    available = term_cols - _MARGIN - _SHADOW_W
    width = min(max(_WIDTH_MIN, available), _WIDTH_MAX)
    inner = width - 4  # content area between │ ... │

    lines: list[str] = []

    # --- Title bar ---
    merchant = _extract_merchant(data, resource_type)
    type_label = resource_type.upper().replace("-", " ")
    title_text = (
        f"Viewing: {type_label} — {merchant}" if merchant else f"Viewing: {type_label}"
    )
    if use_color:
        title_ansi = f"{_bold()}{title_text}{_reset()}"
        lines.append(_frame_top_ansi(title_ansi, len(title_text), width))
    else:
        lines.append(_frame_top(title_text, width, use_color=False))

    lines.append(_frame_row("", width))

    # --- Section 1: Summary ---
    summary = _extract_summary(data, resource_type)
    _render_kv_section(lines, summary, inner, width, use_color)

    # --- Section 2: Line Items (conditional) ---
    line_items_data = _detect_line_items(data, resource_type)
    if line_items_data is not None:
        lines.append(_frame_row("", width))
        _append_section_header(lines, "Line Items", inner, width, use_color)
        lines.append(_frame_row("", width))
        _render_line_items(lines, line_items_data, inner, width, use_color)

    # --- Section 3: Status ---
    status = _extract_status(data, resource_type)
    if status:
        lines.append(_frame_row("", width))
        _append_section_header(lines, "Status", inner, width, use_color)
        lines.append(_frame_row("", width))
        _render_kv_section(lines, status, inner, width, use_color, status_coloring=True)

    lines.append(_frame_row("", width))
    lines.append(_frame_bottom(width))

    wrapped = _window_wrap(lines, width)
    for line in wrapped:
        sys.stdout.write(line + "\n")
    sys.stdout.flush()
    return True


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------


def _extract_merchant(data: dict, resource_type: str) -> str:
    if resource_type == "transaction":
        return data.get("merchant_name", "") or ""
    if resource_type == "receipt":
        return data.get("merchant_name", "") or ""
    if resource_type == "reimbursement":
        return data.get("merchant", "") or ""
    if resource_type == "purchase-order":
        return data.get("name", "") or ""
    return ""


def _extract_summary(data: dict, resource_type: str) -> list[tuple[str, str]]:
    """Return ordered list of (label, value) for the summary section."""
    rows: list[tuple[str, str]] = []

    def _add(label: str, value: Any) -> None:
        if value:
            rows.append((label, str(value)))

    if resource_type == "transaction":
        _add("Transaction ID", data.get("id"))
        _add("Merchant", data.get("merchant_name"))
        _add("Amount", _fmt_amount(data.get("amount")))
        _add("Date", _fmt_date(data.get("user_transaction_time")))
        holder = data.get("card_holder") or {}
        holder_name = (
            f"{holder.get('first_name', '')} {holder.get('last_name', '')}".strip()
        )
        _add("Card Holder", holder_name)
        _add("Category", data.get("sk_category_name"))
        _add("Department", holder.get("department_name"))
        _add("Location", holder.get("location_name"))
        _add("Memo", data.get("memo"))
        orig = data.get("original_transaction_amount")
        if orig:
            _add("Original Amount", _fmt_amount(orig))
        _add("Trip", data.get("trip_name"))

    elif resource_type == "receipt":
        _add("Receipt ID", data.get("id"))
        _add("Merchant", data.get("merchant_name"))
        _add("Amount", _fmt_amount(data.get("amount")))
        _add("Date", _fmt_date(data.get("date")))
        _add("Card Holder", data.get("card_holder"))
        _add("Memo", data.get("memo"))

    elif resource_type == "reimbursement":
        _add("Reimbursement ID", data.get("id"))
        _add("Merchant", data.get("merchant"))
        _add("Amount", _fmt_amount(data.get("amount")))
        _add("Date", _fmt_date(data.get("transaction_date")))
        _add("User", data.get("user_full_name"))
        _add("Memo", data.get("memo"))
        orig = data.get("original_reimbursement_amount")
        if orig:
            _add("Original Amount", _fmt_amount(orig))
        rtype = data.get("type", "")
        direction = data.get("direction", "")
        if rtype:
            _add("Type", f"{rtype} {direction}".strip() if direction else rtype)
        eg = data.get("expense_group") or {}
        _add("Expense Group", eg.get("name"))

    elif resource_type == "purchase-order":
        _add("PO Number", data.get("purchase_order_number") or data.get("id"))
        _add("Name", data.get("name"))
        _add("Amount", _fmt_amount(data.get("amount")))
        _add("Date", _fmt_date(data.get("created_at")))
        _add("Memo", data.get("memo"))
        _add("Billing Status", data.get("billing_status"))
        start = _fmt_date(data.get("spend_start_date"))
        end = _fmt_date(data.get("spend_end_date"))
        if start and end:
            _add("Spend Period", f"{start} → {end}")
        elif start:
            _add("Spend Period", f"{start} →")

    return rows


def _detect_line_items(data: dict, resource_type: str) -> dict | None:
    """Return line items info dict or None if no items to display.

    Returns {"style": "5col"|"3col", "items": [...], "total": ..., "subtotal": ...}
    """
    if resource_type == "transaction":
        # Prefer merchant_data.receipt for rich 5-col
        receipt = (data.get("merchant_data") or {}).get("receipt") or {}
        receipt_items = receipt.get("items") or []
        if receipt_items and any(
            it.get("description") or it.get("unit_cost") for it in receipt_items
        ):
            items = []
            subtotal = 0
            for it in receipt_items:
                desc = it.get("description", "")
                unit = it.get("unit_cost")
                qty = it.get("quantity")
                total = it.get("total")
                items.append(
                    {
                        "description": desc,
                        "unit_rate": unit,
                        "quantity": qty,
                        "amount": total,
                    }
                )
                if total is not None:
                    try:
                        subtotal += float(total)
                    except (ValueError, TypeError):
                        pass
            return {
                "style": "5col",
                "items": items,
                "subtotal": subtotal,
                "total": data.get("amount"),
            }

        # Fallback: line_items with amount
        li = data.get("line_items") or []
        if li:
            return _build_3col(li, data.get("amount"))
        return None

    if resource_type == "reimbursement":
        li = data.get("line_items") or []
        if li:
            return _build_3col(li, data.get("amount"))
        return None

    if resource_type == "purchase-order":
        li = data.get("line_items") or []
        if li and any(it.get("description") or it.get("unit_price") for it in li):
            items = []
            subtotal = 0
            for it in li:
                desc = it.get("description", "")
                unit = it.get("unit_price")
                qty = it.get("quantity")
                amt = it.get("amount")
                items.append(
                    {
                        "description": desc,
                        "unit_rate": unit,
                        "quantity": qty,
                        "amount": amt,
                    }
                )
                if amt is not None:
                    subtotal += _to_cents(amt) / 100  # convert cents to dollars
            return {
                "style": "5col",
                "items": items,
                "subtotal": subtotal,
                "total": data.get("amount"),
            }
        if li:
            return _build_3col(li, data.get("amount"))
        return None

    # receipt type — typically no line items
    return None


def _build_3col(li: list[dict], total: Any) -> dict:
    subtotal = 0
    items = []
    for it in li:
        cat = it.get("category") or it.get("memo") or it.get("description") or ""
        amt = it.get("amount")
        items.append({"category": cat, "amount": amt})
        if amt is not None:
            subtotal += _to_cents(amt) / 100  # convert cents to dollars
    return {"style": "3col", "items": items, "subtotal": subtotal, "total": total}


def _to_cents(val: Any) -> int:
    """Normalize an amount value to cents (int)."""
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val * 100)
    if isinstance(val, dict):
        return _to_cents(val.get("amount", 0))
    return 0


def _extract_status(data: dict, resource_type: str) -> list[tuple[str, str]]:
    """Return ordered list of (label, value) for the status section."""
    rows: list[tuple[str, str]] = []

    def _add(label: str, value: Any) -> None:
        if value:
            rows.append((label, str(value)))

    if resource_type == "transaction":
        _add("State", data.get("state"))
        _add("Settlement Date", _fmt_date(data.get("settlement_date")))
        _add("Synced At", _fmt_date(data.get("synced_at")))
        loc = data.get("merchant_data") or {}
        city = loc.get("city", "")
        state = loc.get("state", "")
        country = loc.get("country", "")
        location = ", ".join(p for p in [city, state, country] if p)
        _add("Merchant Location", location)

    elif resource_type == "receipt":
        _add("State", data.get("state"))
        _add("Transaction ID", data.get("transaction_id"))
        _add("Receipt URL", data.get("receipt_url"))
        _add("Uploaded At", _fmt_date(data.get("created_at")))

    elif resource_type == "reimbursement":
        _add("State", data.get("state"))
        _add("Synced At", _fmt_date(data.get("synced_at")))
        _add("Submitted At", _fmt_date(data.get("submitted_at")))
        _add("User Email", data.get("user_email"))

    elif resource_type == "purchase-order":
        _add("Status", data.get("billing_status"))
        _add("Receipt Status", data.get("receipt_status"))
        bill_ids = data.get("bill_ids") or []
        if bill_ids:
            _add("Linked Bills", str(len(bill_ids)))
        txn_ids = data.get("transaction_ids") or []
        if txn_ids:
            _add("Linked Transactions", str(len(txn_ids)))

    return rows


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _render_kv_section(
    lines: list[str],
    rows: list[tuple[str, str]],
    inner: int,
    width: int,
    use_color: bool,
    status_coloring: bool = False,
) -> None:
    """Render key-value rows inside the frame."""
    if not rows:
        return
    label_w = max(len(label) for label, _ in rows) + 2  # +2 for spacing

    for label, value in rows:
        is_status = status_coloring and label.lower() in ("state", "status")
        if use_color:
            label_part = f"{_bold()}{label + ':':<{label_w}}{_reset_fg()}"
            if is_status and value.upper() in _STATUS_GREEN:
                val_part = f"{_fg(0, 200, 0)}{value}{_reset_fg()}"
            elif is_status and value.upper() in _STATUS_GRAY:
                val_part = f"{_fg(100, 100, 100)}{value}{_reset_fg()}"
            else:
                val_part = _gradient_text(value)
            row_ansi = f"{label_part} {val_part}"
            visible = label_w + 1 + len(value)
            lines.append(_frame_row_ansi(row_ansi, visible, width))
        else:
            row_text = f"{label + ':':<{label_w}} {value}"
            lines.append(_frame_row(row_text, width))


def _append_section_header(
    lines: list[str], title: str, inner: int, width: int, use_color: bool
) -> None:
    """Append a section divider header row with background highlighting."""
    if use_color:
        padded = title + " " * (inner - len(title))
        styled = f"{_bg(*_HEADER_BG)}{_bold()}{_fg(255, 255, 255)}{padded}{_reset()}"
        lines.append(f"{BOX_V} {styled} {BOX_V}")
    else:
        lines.append(_frame_row(title, width))


def _render_line_items(
    lines: list[str], data: dict, inner: int, width: int, use_color: bool
) -> None:
    """Render line items table (5-col or 3-col)."""
    items = data["items"]
    style = data["style"]

    if style == "5col":
        col_num = 4
        col_amount = 14
        col_rate = 12
        col_count = 7
        col_desc = inner - col_num - col_rate - col_count - col_amount

        hdr = (
            f"{'#':<{col_num}}"
            f"{'Desc.':<{col_desc}}"
            f"{'Unit Rate':>{col_rate}}"
            f"{'Count':>{col_count}}"
            f"{'Amount':>{col_amount}}"
        )
        _append_section_header(lines, hdr, inner, width, use_color)

        for i, item in enumerate(items, 1):
            desc = (item.get("description") or "")[: col_desc - 1]
            unit = _fmt_val(item.get("unit_rate"))
            qty = str(item.get("quantity") or "") if item.get("quantity") else ""
            amt = _fmt_val(item.get("amount"))
            row = (
                f"{str(i):<{col_num}}"
                f"{desc:<{col_desc}}"
                f"{unit:>{col_rate}}"
                f"{qty:>{col_count}}"
                f"{amt:>{col_amount}}"
            )
            lines.append(_frame_row(row, width))
    else:
        # 3-col
        col_num = 4
        col_amount = 14
        col_cat = inner - col_num - col_amount

        hdr = f"{'#':<{col_num}}{'Category':<{col_cat}}{'Amount':>{col_amount}}"
        _append_section_header(lines, hdr, inner, width, use_color)

        for i, item in enumerate(items, 1):
            cat = (item.get("category") or "")[: col_cat - 1]
            amt = _fmt_amount(item.get("amount"))
            row = f"{str(i):<{col_num}}{cat:<{col_cat}}{amt:>{col_amount}}"
            lines.append(_frame_row(row, width))

    # Summary rows
    _render_totals(lines, data, inner, width, use_color)


def _fmt_val(val: Any) -> str:
    """Format a line item value — could be numeric, string, or dict."""
    if val is None:
        return ""
    if isinstance(val, dict):
        return _fmt_amount(val)
    if isinstance(val, (int, float)):
        return _fmt_amount(val)
    return str(val)


def _render_totals(
    lines: list[str], data: dict, inner: int, width: int, use_color: bool
) -> None:
    """Render subtotal/total summary rows, right-aligned."""
    lines.append(_frame_row("", width))

    label_width = 20
    amount_width = 14

    def _summary_row(label: str, value: str, bold_row: bool = False) -> None:
        pad_left = inner - label_width - amount_width
        if use_color and bold_row:
            row_text = (
                f"{' ' * pad_left}"
                f"{_bold()}{_fg(255, 255, 255)}{label:>{label_width}}"
                f"{value:>{amount_width}}{_reset()}"
            )
            lines.append(_frame_row_ansi(row_text, inner, width))
        else:
            row_text = f"{' ' * pad_left}{label:>{label_width}}{value:>{amount_width}}"
            lines.append(_frame_row(row_text, width))

    total_val = data.get("total")
    subtotal = data.get("subtotal", 0)

    # Determine currency from total if it's a dict
    cur = "USD"
    if isinstance(total_val, dict):
        cur = total_val.get("currency_code", "USD")

    total_str = _fmt_amount(total_val, currency=cur)

    if subtotal:
        sym = _currency_symbol(cur)
        subtotal_str = (
            f"{sym}{subtotal:,.2f}"
            if isinstance(subtotal, (int, float))
            else str(subtotal)
        )
        _summary_row("Subtotal", subtotal_str)

    _summary_row("Total", total_str, bold_row=True)
