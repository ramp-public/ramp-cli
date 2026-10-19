"""Fetch business memberships for the authenticated user."""

from __future__ import annotations

import json
from typing import Any

import httpx

from ramp_cli.auth import store
from ramp_cli.auth.environment import extra_auth_headers
from ramp_cli.config.constants import api_url

_MEMBERSHIPS_PATH = "/developer/v1/agent-tools/get-simplified-user-detail"
_TOKEN_INFO_PATH = "/developer/v1/token/info"


def fetch_token_info(env: str) -> dict[str, Any]:
    """Return token info for the current session, or {} if unavailable."""
    if not store.has_tokens(env):
        return {}
    access_token, _ = store.get_tokens(env)
    try:
        with httpx.Client(timeout=5.0) as http:
            resp = http.get(
                api_url(env, _TOKEN_INFO_PATH),
                headers={
                    "Authorization": f"Bearer {access_token}",
                    **extra_auth_headers(env),
                },
            )
            resp.raise_for_status()
            data = json.loads(resp.content)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def fetch_business_memberships(env: str) -> list[dict[str, Any]]:
    """Return user records across businesses, or [] if the call fails."""
    if not store.has_tokens(env):
        return []
    access_token, _ = store.get_tokens(env)
    body = json.dumps(
        {"rationale": "List Ramp business memberships for the CLI auth command"}
    ).encode()
    try:
        with httpx.Client(timeout=10.0) as http:
            resp = http.post(
                api_url(env, _MEMBERSHIPS_PATH),
                content=body,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    **extra_auth_headers(env),
                },
            )
            resp.raise_for_status()
            data = json.loads(resp.content)
    except Exception:
        return []

    users = data.get("users") if isinstance(data, dict) else None
    if not isinstance(users, list):
        return []
    return [u for u in users if isinstance(u, dict)]


def summarize_memberships(
    memberships: list[dict[str, Any]], active_business_id: str
) -> list[dict[str, Any]]:
    """Normalize membership rows for CLI / agent JSON output."""
    rows: list[dict[str, Any]] = []
    for user in memberships:
        business_id = str(user.get("business_id") or "")
        first = str(user.get("first_name") or "")
        last = str(user.get("last_name") or "")
        rows.append(
            {
                "business_id": business_id,
                "user_id": str(user.get("id") or ""),
                "email": str(user.get("email") or ""),
                "name": f"{first} {last}".strip(),
                "department": (user.get("department") or {}).get("name")
                if isinstance(user.get("department"), dict)
                else None,
                "location_name": user.get("location_name"),
                "is_active_session": bool(
                    active_business_id and business_id == active_business_id
                ),
            }
        )
    return rows
