"""Routing for Agent Card credentials through Ramp's payment-vault proxy."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

import click

VAULT_PROXY_HEADERS_ENV = "RAMP_VAULT_PROXY_HEADERS"
PRODUCTION_VAULT_PROXY_URL = "https://vault-api.ramp.com"

_PROTECTED_HEADER_NAMES = frozenset(
    {
        "accept",
        "authorization",
        "bt-trace-id",
        "content-length",
        "content-type",
        "cookie",
        "host",
        "proxy-authorization",
        "transfer-encoding",
        "user-agent",
        "x-encrypted-by",
        "x-external-session-id",
        "x-idempotency-key",
        "x-ramp-agent-mode",
        "x-rampy-auth",
    }
)


def vault_proxy_headers(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Parse optional proxy-auth headers without allowing request impersonation."""
    env = environ if environ is not None else os.environ
    raw = (env.get(VAULT_PROXY_HEADERS_ENV) or "").strip()
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise click.BadParameter(
            f"invalid JSON: {error}", param_hint=f"'{VAULT_PROXY_HEADERS_ENV}'"
        ) from error

    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in parsed.items()
    ):
        raise click.BadParameter(
            "must be a JSON object mapping header names to string values",
            param_hint=f"'{VAULT_PROXY_HEADERS_ENV}'",
        )

    protected = sorted(
        key for key in parsed if key.strip().lower() in _PROTECTED_HEADER_NAMES
    )
    if protected:
        raise click.BadParameter(
            "cannot override Ramp authentication, request metadata, or "
            f"vault-attestation headers: {', '.join(protected)}",
            param_hint=f"'{VAULT_PROXY_HEADERS_ENV}'",
        )
    return parsed


def vault_proxy_target(
    path: str,
    *,
    environment: str,
) -> str | None:
    """Return the production proxy target, or ``None`` for direct environments."""
    if environment != "production":
        return None
    if not path.startswith("/"):
        raise click.UsageError("vault-proxied API paths must start with '/'")
    return PRODUCTION_VAULT_PROXY_URL + path
