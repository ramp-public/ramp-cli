"""Tests for standalone-agent OAuth client credentials exchange."""

from __future__ import annotations

import base64

import httpx
import pytest

from ramp_cli.auth import client_credentials
from ramp_cli.auth.oauth import OAuthTokenError


def _response(status_code: int, body: object) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=body,
        request=httpx.Request("POST", "https://example.test/developer/v1/token"),
    )


def test_login_uses_basic_auth_and_omits_unspecified_scope(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(
        url: str,
        *,
        data: dict[str, str],
        headers: dict[str, str],
        auth: httpx.Auth,
    ) -> httpx.Response:
        captured.update(url=url, data=data, headers=headers, auth=auth)
        return _response(
            200,
            {
                "access_token": "standalone-access",
                "refresh_token": "must-not-be-persisted",
                "expires_in": 604800,
                "scope": "transactions:read users:read",
                "token_type": "Bearer",
            },
        )

    monkeypatch.setenv("RAMP_API_URL", "https://standalone.example.test")
    monkeypatch.setattr(
        client_credentials,
        "extra_auth_headers",
        lambda env: {"X-Extra-Auth": f"{env}-token"},
    )
    monkeypatch.setattr(client_credentials.httpx, "post", fake_post)

    token = client_credentials.login(
        "sandbox",
        client_id="agent-client",
        client_secret="agent-secret",
    )

    assert captured["url"] == ("https://standalone.example.test/developer/v1/token")
    assert captured["data"] == {"grant_type": "client_credentials"}
    assert captured["headers"] == {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Extra-Auth": "sandbox-token",
    }
    auth = captured["auth"]
    assert isinstance(auth, httpx.BasicAuth)
    request = next(
        auth.sync_auth_flow(httpx.Request("POST", "https://example.test/token"))
    )
    expected = base64.b64encode(b"agent-client:agent-secret").decode("ascii")
    assert request.headers["Authorization"] == f"Basic {expected}"
    assert token.access_token == "standalone-access"
    assert token.refresh_token == ""
    assert token.refresh_token_expires_in == 0
    assert token.expires_in == 604800
    assert token.agent_key_uuid == ""


def test_login_sends_deduplicated_explicit_scopes(monkeypatch) -> None:
    captured_data: dict[str, str] = {}

    def fake_post(
        url: str,
        *,
        data: dict[str, str],
        headers: dict[str, str],
        auth: httpx.Auth,
    ) -> httpx.Response:
        captured_data.update(data)
        return _response(
            200,
            {
                "access_token": "standalone-access",
                "expires_in": 3600,
                "scope": data["scope"],
            },
        )

    monkeypatch.setattr(client_credentials.httpx, "post", fake_post)

    token = client_credentials.login(
        "sandbox",
        client_id="agent-client",
        client_secret="agent-secret",
        scopes=("transactions:read", "users:read", "transactions:read"),
    )

    assert captured_data == {
        "grant_type": "client_credentials",
        "scope": "transactions:read users:read",
    }
    assert token.scope == "transactions:read users:read"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"expires_in": 3600}, id="missing-access-token"),
        pytest.param(
            {"access_token": "standalone-access", "expires_in": 0},
            id="zero-expiry",
        ),
        pytest.param(
            {"access_token": "standalone-access", "expires_in": "3600"},
            id="non-integer-expiry",
        ),
    ],
)
def test_login_rejects_invalid_success_response(monkeypatch, body: object) -> None:
    monkeypatch.setattr(
        client_credentials.httpx,
        "post",
        lambda *args, **kwargs: _response(200, body),
    )

    with pytest.raises(OAuthTokenError) as exc_info:
        client_credentials.login(
            "sandbox",
            client_id="agent-client",
            client_secret="agent-secret",
        )

    assert exc_info.value.error == "token_request_failed"


def test_login_surfaces_nested_core_error(monkeypatch) -> None:
    monkeypatch.setattr(
        client_credentials.httpx,
        "post",
        lambda *args, **kwargs: _response(
            400,
            {
                "error_v2": {
                    "message": "Client is not authorized for standalone agent access"
                }
            },
        ),
    )

    with pytest.raises(OAuthTokenError) as exc_info:
        client_credentials.login(
            "sandbox",
            client_id="agent-client",
            client_secret="agent-secret",
        )

    assert "not authorized for standalone agent access" in str(exc_info.value)
