"""Shared formatting utilities for CLI output views."""

from __future__ import annotations

from datetime import datetime
from typing import Any

_CURRENCY_SYMBOLS: dict[str, str] = {
    "USD": "$",
    "CAD": "CA$",
    "GBP": "£",
    "EUR": "€",
    "JPY": "¥",
    "CNY": "¥",
    "KRW": "₩",
    "INR": "₹",
    "BRL": "R$",
    "MXN": "MX$",
    "AUD": "A$",
    "NZD": "NZ$",
    "CHF": "CHF ",
    "SEK": "kr",
    "NOK": "kr",
    "DKK": "kr",
    "PLN": "zł",
    "ZAR": "R",
    "TRY": "₺",
    "ILS": "₪",
    "THB": "฿",
    "SGD": "S$",
    "HKD": "HK$",
    "TWD": "NT$",
}


def currency_symbol(code: str) -> str:
    """Return currency symbol for a code, falling back to the code itself."""
    return _CURRENCY_SYMBOLS.get(code, f"{code} ")


def fmt_amount(amount: Any, currency: str = "USD") -> str:
    """Format amount — int (cents), float (dollars), dict, or string."""
    sym = currency_symbol(currency)
    if isinstance(amount, int):
        return f"{sym}{amount / 100:,.2f}"
    if isinstance(amount, float):
        return f"{sym}{amount:,.2f}"
    if isinstance(amount, dict):
        val = amount.get("amount", 0) or 0
        cur = amount.get("currency_code", "USD")
        sym = currency_symbol(cur)
        if isinstance(val, str):
            try:
                val = float(val)
            except (ValueError, TypeError):
                return f"{sym}{val}"
        formatted = f"{val / 100:,.2f}" if isinstance(val, int) else f"{val:,.2f}"
        return f"{sym}{formatted}"
    if isinstance(amount, str):
        try:
            return f"{sym}{float(amount):,.2f}"
        except ValueError:
            return amount
    return f"{sym}0.00"


def fmt_date(iso: str | None, default: str = "") -> str:
    """Format ISO date string to human-readable form."""
    if not iso:
        return default
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y")
    except (ValueError, AttributeError):
        return iso
