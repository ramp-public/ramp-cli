"""Opt-in routing for Agent Card credentials through a payment-vault proxy."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from urllib.parse import urlsplit

import click

VAULT_PROXY_ENABLED_ENV = "RAMP_VAULT_PROXY_ENABLED"
VAULT_PROXY_URL_ENV = "RAMP_VAULT_PROXY_URL"
VAULT_PROXY_HEADERS_ENV = "RAMP_VAULT_PROXY_HEADERS"

_FALSEY_FLAG_VALUES = frozenset({"", "0", "false", "no", "off"})
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


def vault_proxy_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether the explicit vault-proxy routing flag is enabled."""
    env = environ if environ is not None else os.environ
    return (env.get(VAULT_PROXY_ENABLED_ENV) or "").strip().lower() not in (
        _FALSEY_FLAG_VALUES
    )


def vault_proxy_url(environ: Mapping[str, str] | None = None) -> str | None:
    """Return a validated HTTPS proxy base URL, or ``None`` when unset."""
    env = environ if environ is not None else os.environ
    raw = (env.get(VAULT_PROXY_URL_ENV) or "").strip().rstrip("/")
    if not raw:
        return None

    parts = urlsplit(raw)
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise click.BadParameter(
            "must be an HTTPS base URL without credentials, query, or fragment",
            param_hint=f"'{VAULT_PROXY_URL_ENV}'",
        )
    return raw


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
    path: str, environ: Mapping[str, str] | None = None
) -> str | None:
    """Return the proxy target when enabled, otherwise use the normal Ramp API."""
    if not vault_proxy_enabled(environ):
        return None
    base = vault_proxy_url(environ)
    if base is None:
        raise click.UsageError(
            f"{VAULT_PROXY_ENABLED_ENV} is enabled but {VAULT_PROXY_URL_ENV} is unset"
        )
    if not path.startswith("/"):
        raise click.UsageError("vault-proxied API paths must start with '/'")
    return base + path
