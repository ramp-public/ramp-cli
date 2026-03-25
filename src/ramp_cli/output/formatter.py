"""JSON/table output and TTY detection."""

from __future__ import annotations

import json
import sys
from typing import Any

import click


def is_tty() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


# ── Quiet mode state ──
_quiet: bool = False


def set_quiet(q: bool) -> None:
    """Set global quiet mode. Called once from cli() in main.py."""
    global _quiet
    _quiet = q


def is_quiet() -> bool:
    """Check if quiet mode is active."""
    return _quiet


# ── ISO 4217 currency exponents for minor-unit conversion ──
ISO_4217_EXPONENTS: dict[str, int] = {
    # Exponent 0 (no minor unit)
    "BIF": 0,
    "CLP": 0,
    "DJF": 0,
    "GNF": 0,
    "ISK": 0,
    "JPY": 0,
    "KMF": 0,
    "KRW": 0,
    "PYG": 0,
    "RWF": 0,
    "UGX": 0,
    "VND": 0,
    "VUV": 0,
    "XAF": 0,
    "XOF": 0,
    "XPF": 0,
    # Exponent 3
    "BHD": 3,
    "IQD": 3,
    "JOD": 3,
    "KWD": 3,
    "LYD": 3,
    "OMR": 3,
    "TND": 3,
    # All other currencies default to exponent 2 (USD, EUR, GBP, etc.)
}
_DEFAULT_EXPONENT = 2


def canonical_to_display(amount: int, currency_code: str) -> str:
    """Convert API minor-unit integer to display string.

    Examples:
        canonical_to_display(104999, "USD") -> "1,049.99"
        canonical_to_display(1000, "JPY") -> "1,000"
        canonical_to_display(1234567, "BHD") -> "1,234.567"
    """
    code = currency_code.upper() if currency_code else ""
    exp = ISO_4217_EXPONENTS.get(code, _DEFAULT_EXPONENT)
    value = amount / (10**exp)
    return f"{value:,.{exp}f}"


SUPPORTED_FORMATS = {"json", "table"}


def resolve_format(flag_value: str | None, config_default: str) -> str:
    if flag_value:
        if flag_value.lower() not in SUPPORTED_FORMATS:
            raise click.BadParameter(
                f"unsupported format '{flag_value}'. Choose from: {', '.join(sorted(SUPPORTED_FORMATS))}",
                param_hint="'-o'",
            )
        return flag_value.lower()
    if config_default:
        lower = config_default.lower()
        if lower in SUPPORTED_FORMATS:
            return lower
    return "table" if is_tty() else "json"


def print_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def print_agent_json(data: Any, pagination: dict | None = None) -> None:
    envelope: dict[str, Any] = {"schema_version": "1.0"}
    if isinstance(data, list):
        envelope["data"] = data
    else:
        envelope["data"] = [data]
    envelope["pagination"] = pagination if pagination is not None else None
    json.dump(envelope, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def print_error_json(status_code: int, message: str) -> None:
    envelope = {
        "schema_version": "1.0",
        "error": {"code": status_code, "message": message},
        "data": [],
        "pagination": None,
    }
    json.dump(envelope, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def print_table(headers: list[str], rows: list[dict[str, str]]) -> None:
    # Compute column widths
    widths: dict[str, int] = {}
    for h in headers:
        widths[h] = len(h)
    for row in rows:
        for h in headers:
            widths[h] = max(widths[h], len(row.get(h, "")))

    # Header line
    parts = [h.upper().ljust(widths[h]) for h in headers]
    print("  ".join(parts))

    # Rows
    for row in rows:
        parts = [row.get(h, "").ljust(widths[h]) for h in headers]
        print("  ".join(parts))


def truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    if max_len <= 3:
        return s[:max_len]
    return s[: max_len - 3] + "..."


# Per-category display fields: ordered list of columns to show by default.
# When a category match exists, these replace the generic priority list.
# Fields not present in the response are silently skipped.
CATEGORY_DISPLAY_FIELDS: dict[str, list[str]] = {
    "transactions": [
        "amount",
        "merchant_name",
        "spent_by_user",
        "transaction_time",
        "merchant_category",
        "spend_allocation_name",
        "reason_or_justification",
        "transaction_uuid",
    ],
    "bills": [
        "vendor_name",
        "amount",
        "due_date",
        "payment_status",
        "approval_status",
        "invoice_number",
        "memo",
        "id",
    ],
    "cards": [
        "display_name",
        "last_four",
        "card_type",
        "is_physical",
        "id",
    ],
    "funds": [
        "name",
        "balance_info",
        "restrictions",
        "lock",
        "id",
    ],
    "reimbursements": [
        "amount",
        "merchant_name",
        "user_name",
        "created_at",
        "memo",
        "assessment",
        "reimbursement_uuid",
    ],
    "travel": [
        "airline",
        "flight_number",
        "departure_airport",
        "arrival_airport",
        "departure_time",
        "arrival_time",
        "status",
        "hotel_name",
        "city",
        "check_in_date",
        "check_out_date",
    ],
    "unified_requests": [
        "owner",
        "requester",
        "request_date",
        "details",
        "request_uuid",
    ],
    "users": [
        "name",
        "display_name",
        "email",
        "role",
        "department",
        "id",
    ],
}

_DEFAULT_PRIORITY = [
    "name",
    "display_name",
    "amount",
    "status",
    "state",
    "created_at",
    "updated_at",
    "user_id",
    "email",
]


def extract_headers(
    item: dict[str, Any], wide: bool = False, category: str | None = None
) -> list[str]:
    priority = CATEGORY_DISPLAY_FIELDS.get(category or "", _DEFAULT_PRIORITY)
    seen: set[str] = set()
    headers: list[str] = []

    for p in priority:
        if p in item:
            headers.append(p)
            seen.add(p)

    max_cols = len(item) if wide else 8

    for k in item:
        if k not in seen and len(headers) < max_cols:
            headers.append(k)
            seen.add(k)

    return headers


def is_canonical_amount(val: Any) -> bool:
    """Return True if val looks like a Ramp canonical amount object."""
    return (
        isinstance(val, dict)
        and isinstance(val.get("amount"), int)
        and isinstance(val.get("currency_code"), str)
    )


def _format_amount(val: dict) -> str:
    """Format a canonical amount dict to a readable string."""
    return f"{canonical_to_display(val['amount'], val['currency_code'])} {val['currency_code']}"


def _summarize_list(items: list, wide: bool = False) -> str:
    """Build a compact summary string for a list of values."""
    if len(items) == 0:
        return "(none)"

    # Simple scalars: join them
    if all(isinstance(i, (str, int, float)) for i in items):
        joined = ", ".join(str(i) for i in items)
        return truncate(joined, 80)

    # Dicts: try to extract informative keys
    if all(isinstance(i, dict) for i in items):
        _LABEL_KEYS = ("name", "label", "category_name", "value", "type", "id")
        parts: list[str] = []
        for item in items[:5]:
            # Find a type-like key and a name-like key
            type_val = item.get("type") or item.get("category_name")
            name_val = None
            for k in ("name", "label", "value", "category_name"):
                if k in item and item[k] and item[k] != type_val:
                    name_val = item[k]
                    break

            if type_val and name_val:
                parts.append(f"{type_val}: {name_val}")
            elif type_val:
                parts.append(str(type_val))
            elif name_val:
                parts.append(str(name_val))
            else:
                # Fall back to first string-valued field
                for k, fv in item.items():
                    if isinstance(fv, str) and fv:
                        parts.append(fv)
                        break
                else:
                    parts.append("{...}")

        summary = " · ".join(parts)
        if len(items) > 5:
            summary = f"{summary} (+{len(items) - 5} more)"
        if len(summary) > 80:
            return truncate(summary, 80)
        return summary

    return f"[{len(items)} items]"


def format_value(v: Any, wide: bool = False) -> str:
    if v is None:
        return ""
    trunc_len = 200 if wide else 50
    if isinstance(v, str):
        return truncate(v, trunc_len)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        if v == int(v):
            return str(int(v))
        return f"{v:.2f}"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, dict):
        # Canonical amount object: {amount: int, currency_code: str}
        if is_canonical_amount(v):
            return _format_amount(v)
        # One level of nesting: dict with a key whose value is a canonical amount
        for k in ("total", "amount"):
            nested = v.get(k)
            if is_canonical_amount(nested):
                return _format_amount(nested)
        return "{...}"
    if isinstance(v, list):
        return _summarize_list(v, wide=wide)
    return str(v)
