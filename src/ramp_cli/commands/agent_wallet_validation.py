"""Request construction and validation for Agent Wallet payments."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import click

_MAX_USD_CENTS = 10**22 - 1


def _usd_to_cents(amount: str) -> int:
    try:
        dollars = Decimal(amount)
    except InvalidOperation as error:
        raise click.BadParameter(
            "must be a decimal amount", param_hint="'--amount'"
        ) from error
    if not dollars.is_finite() or dollars <= 0:
        raise click.BadParameter("must be greater than zero", param_hint="'--amount'")
    numerator, denominator = dollars.as_integer_ratio()
    cents, fractional_cents = divmod(numerator * 100, denominator)
    if fractional_cents:
        raise click.BadParameter(
            "supports at most 2 decimal places", param_hint="'--amount'"
        )
    if cents > _MAX_USD_CENTS:
        max_dollars, max_cents = divmod(_MAX_USD_CENTS, 100)
        raise click.BadParameter(
            f"must not exceed USD {max_dollars}.{max_cents:02d}",
            param_hint="'--amount'",
        )
    return cents


def _validate_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise click.BadParameter(
            "must be an HTTP or HTTPS URL with a host",
            param_hint="'--merchant-url'",
        )
    return value


def _validate_expires_at(value: str) -> str:
    try:
        expires_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise click.BadParameter(
            "must be an ISO 8601 timestamp",
            param_hint="'--expires-at'",
        ) from error
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise click.BadParameter(
            "must include a timezone",
            param_hint="'--expires-at'",
        )
    if expires_at <= datetime.now(timezone.utc):
        raise click.BadParameter(
            "must be in the future",
            param_hint="'--expires-at'",
        )
    return expires_at.isoformat()


def build_structured_vic_body(
    payment_operation_id: UUID,
    merchant_name: str | None,
    merchant_url: str | None,
    merchant_country_code: str | None,
    amount: str | None,
    expires_at: str | None,
) -> dict[str, Any]:
    fields = {
        "--merchant-name": merchant_name,
        "--merchant-url": merchant_url,
        "--merchant-country-code": merchant_country_code,
        "--amount": amount,
        "--expires-at": expires_at,
    }
    missing = [name for name, value in fields.items() if value is None]
    if missing:
        raise click.UsageError("Missing structured input: " + ", ".join(missing) + ".")

    name = merchant_name.strip() if merchant_name else ""
    if not name:
        raise click.BadParameter("must not be empty", param_hint="'--merchant-name'")
    country = merchant_country_code.strip().upper() if merchant_country_code else ""
    if len(country) != 2 or not country.isalpha():
        raise click.BadParameter(
            "must be a two-letter country code",
            param_hint="'--merchant-country-code'",
        )
    amount_cents = _usd_to_cents(amount or "")
    return {
        "payment_operation_id": str(payment_operation_id),
        "method": "vic",
        "method_request": {
            "merchant": {
                "name": name,
                "url": _validate_url(merchant_url or ""),
                "country_code": country,
            },
            "amount": {"value": amount_cents, "currency": "USD"},
            "expires_at": _validate_expires_at(expires_at or ""),
        },
    }


def validate_vic_json_body(body: dict[str, Any]) -> dict[str, Any]:
    if body.get("method") != "vic":
        raise click.BadParameter("method must be 'vic'", param_hint="'--json'")
    return body
