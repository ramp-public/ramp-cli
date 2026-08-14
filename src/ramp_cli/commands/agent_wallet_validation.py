"""Request construction and validation for Agent Wallet payments."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import click


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
    amount_value: int | None,
    amount_currency: str | None,
    expires_at: str | None,
) -> dict[str, Any]:
    fields = {
        "--merchant-name": merchant_name,
        "--merchant-url": merchant_url,
        "--merchant-country-code": merchant_country_code,
        "--amount-value": amount_value,
        "--amount-currency": amount_currency,
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
    currency = amount_currency.strip().upper() if amount_currency else ""
    if not currency:
        raise click.BadParameter("must not be empty", param_hint="'--amount-currency'")
    return {
        "payment_operation_id": str(payment_operation_id),
        "method": "vic",
        "method_request": {
            "merchant": {
                "name": name,
                "url": _validate_url(merchant_url or ""),
                "country_code": country,
            },
            "amount": {"value": amount_value, "currency": currency},
            "expires_at": _validate_expires_at(expires_at or ""),
        },
    }


def validate_vic_json_body(body: dict[str, Any]) -> dict[str, Any]:
    if body.get("method") != "vic":
        raise click.BadParameter("method must be 'vic'", param_hint="'--json'")
    return body
