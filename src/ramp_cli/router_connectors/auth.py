from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import httpx

from ramp_cli.auth.refresh import _refresh_lock
from ramp_cli.client.router_user import RouterUserClient, _atomic_write
from ramp_cli.config import settings
from ramp_cli.errors import ApiError, RampCLIError
from ramp_cli.router_connectors import harnesses

CLIENT_ID = "9d07c47e-9eb4-4ca2-a9e4-f51bb2ab871b"
RESOURCES = {
    "https://app.router.com": "https://api.router.com/v1/mcp",
    "https://internal.router.com": "https://internal-api.router.com/v1/mcp",
    "https://qa.router.com": "https://qa-router-api.ramp.com/v1/mcp",
    "https://qa-internal.router.com": "https://qa-internal-api.router.com/v1/mcp",
}
IDENTITY_FIELDS = (
    "resource",
    "client_id",
    "user_id",
    "business_uuid",
    "core_user_uuid",
    "session_id",
)
UPGRADE = "Connector authorization required. Run 'ramp router login --connectors', then explicitly enable this harness again."


def resource_for_origin(origin: str) -> str:
    resource = RESOURCES.get(origin)
    if resource is None:
        raise RampCLIError(
            "Connectors require a supported Router deployment; no credentials were sent."
        )
    return resource


def authorization(client: RouterUserClient) -> dict:
    state = client._load()
    value = state.get("connector_authorization")
    if not state.get("refresh_token") or int(time.time()) >= state.get(
        "issued_at", 0
    ) + state.get("refresh_token_expires_in", 0):
        raise RampCLIError(
            "Sign in with 'ramp router login --connectors' first.", code=4
        )
    return validate_authorization(client.origin, value)


def validate_authorization(origin: str, value: object) -> dict:
    if not isinstance(value, dict) or any(
        not isinstance(value.get(k), str) or not value[k] for k in IDENTITY_FIELDS
    ):
        raise RampCLIError(UPGRADE, code=4)
    if (
        value["resource"] != resource_for_origin(origin)
        or value["client_id"] != CLIENT_ID
        or value.get("scope") != "connectors:execute"
    ):
        raise RampCLIError(
            "Stored connector authorization does not match this deployment/client.",
            code=4,
        )
    return {key: value[key] for key in IDENTITY_FIELDS}


def _directory() -> Path:
    return settings.config_dir() / "router-connectors"


def _binding_path(binding: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", binding):
        raise RampCLIError("Invalid connector binding identifier.")
    return _directory() / f"binding-{binding}.json"


def load_binding(binding: str) -> dict:
    try:
        value = json.loads(_binding_path(binding).read_text())
        if (
            not isinstance(value, dict)
            or value["origin"] not in RESOURCES
            or not isinstance(value["authorization"], dict)
        ):
            raise ValueError
        auth = value["authorization"]
        if (
            set(auth) != set(IDENTITY_FIELDS)
            or any(not isinstance(auth[k], str) or not auth[k] for k in IDENTITY_FIELDS)
            or auth["resource"] != RESOURCES[value["origin"]]
            or auth["client_id"] != CLIENT_ID
        ):
            raise ValueError
        if value.get("profile") is not None and not isinstance(value["profile"], str):
            raise ValueError
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RampCLIError(
            "Connector binding is missing or invalid; explicitly enable the harness again.",
            code=4,
        ) from exc


def create_binding(client: RouterUserClient) -> str:
    auth = authorization(client)
    data = {"origin": client.origin, "profile": client.profile, "authorization": auth}
    # All harnesses for a single device authorization share one access-token cache.
    binding = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:32]
    issue(client, auth, binding)
    _atomic_write(_binding_path(binding), data)
    return binding


def status(client: RouterUserClient) -> dict:
    session = client.status()
    auth = None
    state = "signed_out"
    if session["authenticated"]:
        state = "authorization_required"
        try:
            auth = authorization(client)
            state = "authorized"
        except RampCLIError:
            pass
    registrations = []
    for harness in harnesses.HARNESSES:
        result = harnesses.status(harness)
        if result["state"] == "enabled":
            try:
                bound = load_binding(result["binding_id"])
                if bound != {
                    "origin": client.origin,
                    "profile": client.profile,
                    "authorization": auth,
                }:
                    result["state"] = "conflict"
            except RampCLIError:
                result["state"] = "conflict"
        registrations.append(result)
    return {
        "contract_version": 1,
        "authorization": state,
        "origin": client.origin,
        "identity": {"user_id": auth["user_id"], "business_id": auth["business_uuid"]}
        if auth
        else None,
        "harnesses": registrations,
    }


def issue(
    client: RouterUserClient,
    expected: dict,
    binding: str,
    *,
    rejected: str | None = None,
) -> str:
    # Lock ordering is always MCP cache -> CLI refresh. Never copy a refresh
    # secret into a binding or acquire these locks in the opposite order.
    with _refresh_lock("router-connectors-" + binding):
        cache_path = _directory() / f"token-{binding}.json"
        with _refresh_lock(client.namespace):
            if authorization(client) != expected:
                raise RampCLIError(
                    "Router authorization changed. Explicitly enable this harness again.",
                    code=4,
                )
        # Recover/rotate through the existing durable CLI refresh protocol only.
        # no_input prevents Router's reauthorization recovery from opening a browser.
        try:
            client.access_token()
        except (ApiError, httpx.HTTPError) as exc:
            raise RampCLIError(
                "Router account renewal failed; retry or explicitly sign in again."
            ) from exc
        with _refresh_lock(client.namespace):
            if authorization(client) != expected:
                raise RampCLIError(
                    "Router authorization changed. Explicitly enable this harness again.",
                    code=4,
                )
            try:
                cache = json.loads(cache_path.read_text())
                if (
                    cache.get("authorization") == expected
                    and time.time() < cache.get("expires_at", 0) - 30
                    and cache.get("access_token") != rejected
                ):
                    return cache["access_token"]
            except (OSError, ValueError, TypeError):
                pass
            state = client._load()
            if state.get("pending_refresh_token"):
                raise RampCLIError(
                    "Router credentials are being renewed; retry connector authorization."
                )
            try:
                response = client._pool().post(
                    client.origin + "/api/auth/cli/mcp-token",
                    json=expected,
                    headers={
                        **client._access_headers(),
                        "Authorization": "Bearer " + state["refresh_token"],
                    },
                    timeout=30,
                )
            except httpx.HTTPError as exc:
                raise RampCLIError(
                    "Cannot reach Router connector authorization; retry later."
                ) from exc
            if response.status_code != 200:
                # Never include upstream bodies: an auth failure can contain secrets.
                if response.status_code in (400, 401, 403):
                    raise RampCLIError(UPGRADE, code=4)
                raise RampCLIError(
                    "Router connector authorization is temporarily unavailable."
                )
            try:
                token = response.json()
                lifetime = token["expires_in"]
                access = token["access_token"]
                if (
                    any(token.get(k) != expected[k] for k in IDENTITY_FIELDS)
                    or token.get("scope") != "connectors:execute"
                    or token.get("token_type", "").lower() != "bearer"
                    or type(lifetime) is not int
                    or not 0 < lifetime <= 300
                    or not isinstance(access, str)
                    or not access
                    or not access.isascii()
                    or any(c.isspace() for c in access)
                ):
                    raise ValueError
            except (ValueError, TypeError, KeyError) as exc:
                raise RampCLIError(
                    "Router returned an invalid connector authorization response."
                ) from exc
            _atomic_write(
                cache_path,
                {
                    "authorization": expected,
                    "access_token": access,
                    "expires_at": time.time() + lifetime,
                },
            )
            return access
