"""OAuth 2.0 client credentials flow for standalone agents."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import httpx

from ramp_cli.auth.environment import extra_auth_headers
from ramp_cli.auth.oauth import OAuthTokenError, TokenResponse
from ramp_cli.config.constants import api_url

TOKEN_PATH = "/developer/v1/token"


def login(
    env: str,
    *,
    client_id: str,
    client_secret: str,
    scopes: tuple[str, ...] = (),
) -> TokenResponse:
    """Exchange standalone-agent client credentials for an access token."""
    data = {"grant_type": "client_credentials"}
    if scopes:
        data["scope"] = " ".join(dict.fromkeys(scopes))

    response = httpx.post(
        api_url(env, TOKEN_PATH),
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            **extra_auth_headers(env),
        },
        auth=httpx.BasicAuth(client_id, client_secret),
    )
    body = _parse_response(response)
    _raise_for_error(response, body)

    access_token = body.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthTokenError(
            "token_request_failed",
            "Token response did not include a valid access token.",
        )

    expires_in = body.get("expires_in")
    if (
        isinstance(expires_in, bool)
        or not isinstance(expires_in, int)
        or expires_in <= 0
    ):
        raise OAuthTokenError(
            "token_request_failed",
            "Token response did not include a positive expires_in value.",
        )

    token_type = body.get("token_type")
    scope = body.get("scope")
    return TokenResponse(
        access_token=access_token,
        refresh_token="",
        token_type=token_type if isinstance(token_type, str) else "",
        expires_in=expires_in,
        refresh_token_expires_in=0,
        scope=scope if isinstance(scope, str) else "",
        agent_key_uuid="",
    )


def _parse_response(response: httpx.Response) -> dict[str, object]:
    try:
        body: object = response.json()
    except Exception as exc:
        description = response.text.strip() or f"HTTP {response.status_code}"
        raise OAuthTokenError("token_request_failed", description) from exc

    if not isinstance(body, dict) or not all(isinstance(key, str) for key in body):
        raise OAuthTokenError("token_request_failed", str(body))
    return cast(dict[str, object], body)


def _raise_for_error(
    response: httpx.Response,
    body: Mapping[str, object],
) -> None:
    if not response.is_error and "error" not in body and "access_token" in body:
        return

    error = body.get("error")
    error_code = error if isinstance(error, str) and error else "token_request_failed"
    raise OAuthTokenError(error_code, _error_description(response, body))


def _error_description(
    response: httpx.Response,
    body: Mapping[str, object],
) -> str:
    for container in (body, body.get("error"), body.get("error_v2")):
        if not isinstance(container, Mapping):
            continue
        for key in ("error_description", "message"):
            description = container.get(key)
            if description:
                return str(description)
    return response.text.strip() or str(body)
