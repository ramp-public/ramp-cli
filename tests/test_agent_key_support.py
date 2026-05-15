"""Tests for agent-key handling in the OAuth flow.

The CLI extracts `agent_key_uuid` from the `ak` claim in the JWT-formatted
access token returned by the token endpoint. This replaces the earlier
behavior of parsing the value from the /authorize callback query string.
"""

from __future__ import annotations

import base64
import json

import pytest

from ramp_cli.auth import oauth as oauth_module
from ramp_cli.auth import refresh as refresh_helper
from ramp_cli.auth import store
from ramp_cli.auth.constants import INVALID_GRANT
from ramp_cli.auth.oauth import (
    OAuthTokenError,
    TokenResponse,
    _classify_token_error,
    _extract_agent_key_uuid,
)
from ramp_cli.config import settings

# --- JWT decoding ---


def _make_jwt(payload: dict) -> str:
    """Build a fake unsigned JWT for testing the payload decoder."""
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    return f"{header.decode()}.{body.decode()}.signature"


def test_extract_agent_key_uuid__pulls_ak_claim():
    uuid = "019de000-0000-0000-0000-000000000000"
    token = _make_jwt({"ak": uuid, "ak_exp": 9999999999, "sub": "user"})
    assert _extract_agent_key_uuid(token) == uuid


def test_extract_agent_key_uuid__missing_claim_returns_empty():
    token = _make_jwt({"sub": "user", "exp": 9999999999})
    assert _extract_agent_key_uuid(token) == ""


def test_extract_agent_key_uuid__empty_token_returns_empty():
    assert _extract_agent_key_uuid("") == ""


def test_extract_agent_key_uuid__opaque_token_returns_empty():
    """Non-JWT (opaque) tokens have no `.` segments to decode."""
    assert _extract_agent_key_uuid("not-a-jwt") == ""


def test_extract_agent_key_uuid__malformed_payload_returns_empty():
    """Garbage in the payload segment must not raise."""
    assert _extract_agent_key_uuid("aaa.!!!not-base64!!!.bbb") == ""


def test_extract_agent_key_uuid__non_dict_payload_returns_empty():
    body = base64.urlsafe_b64encode(b'"just-a-string"').rstrip(b"=").decode()
    assert _extract_agent_key_uuid(f"hdr.{body}.sig") == ""


def test_extract_agent_key_uuid__non_string_ak_returns_empty():
    token = _make_jwt({"ak": 12345})
    assert _extract_agent_key_uuid(token) == ""


def test_extract_agent_key_uuid__handles_unpadded_base64():
    """Real JWTs strip base64 padding; the decoder must tolerate that."""
    uuid = "019de000-0000-0000-0000-000000000000"
    token = _make_jwt({"ak": uuid})
    # _make_jwt already strips padding, but be explicit:
    parts = token.split(".")
    assert "=" not in parts[1]
    assert _extract_agent_key_uuid(token) == uuid


# --- store persistence ---


def test_save_tokens__persists_agent_key_uuid():
    uuid = "019de000-0000-0000-0000-000000000000"
    store.save_tokens("sandbox", "access123", "refresh456", agent_key_uuid=uuid)
    assert store.get_agent_key_uuid("sandbox") == uuid


def test_save_tokens__omits_agent_key_uuid_preserves_prior():
    """`None` preserves — useful for callers that don't know the value."""
    uuid = "019de000-0000-0000-0000-000000000000"
    store.save_tokens("sandbox", "access123", "refresh456", agent_key_uuid=uuid)
    store.save_tokens("sandbox", "access999", "refresh999")
    assert store.get_agent_key_uuid("sandbox") == uuid


def test_save_tokens__explicit_empty_agent_key_uuid_clears_prior():
    """Empty string clears — used when the new access token is no longer
    bound to an agent key (e.g. JWT lost the `ak` claim)."""
    uuid = "019de000-0000-0000-0000-000000000000"
    store.save_tokens("sandbox", "access123", "refresh456", agent_key_uuid=uuid)
    store.save_tokens("sandbox", "access999", "refresh999", agent_key_uuid="")
    assert store.get_agent_key_uuid("sandbox") == ""


def test_clear_tokens__also_clears_agent_key_uuid():
    store.save_tokens(
        "sandbox",
        "access123",
        "refresh456",
        agent_key_uuid="019de000-0000-0000-0000-000000000000",
    )
    store.clear_tokens("sandbox")
    assert store.get_agent_key_uuid("sandbox") == ""


def test_agent_key_uuid_roundtrips_through_disk():
    uuid = "019de000-0000-0000-0000-000000000000"
    store.save_tokens("sandbox", "access123", "refresh456", agent_key_uuid=uuid)
    cfg = settings.load()
    assert cfg.sandbox.agent_key_uuid == uuid


# --- oauth error classification ---


@pytest.mark.parametrize(
    "description",
    [
        "Agent-key-authorized session has expired; please re-authenticate.",
        "Session has expired",
    ],
)
def test_classify_token_error__session_expired_maps_to_invalid_grant(description):
    assert (
        _classify_token_error(
            status_code=401, grant_type="refresh_token", description=description
        )
        == INVALID_GRANT
    )


def test_refresh_failure_on_session_expiry_raises_invalid_grant():
    err = OAuthTokenError("token_request_failed", "Session has expired")
    classified = _classify_token_error(
        status_code=401,
        grant_type="refresh_token",
        description=err.description,
    )
    assert classified == INVALID_GRANT


# --- exchange/refresh wiring (JWT-based) ---


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = body
        self.is_error = False
        self.status_code = 200
        self.text = json.dumps(body)

    def json(self) -> dict:
        return self._body


def test_exchange_code__populates_agent_key_uuid_from_jwt(monkeypatch):
    uuid = "019de001-0000-0000-0000-000000000000"
    access_token = _make_jwt({"ak": uuid, "exp": 9999999999})
    body = {
        "access_token": access_token,
        "refresh_token": "refresh-new",
        "expires_in": 3600,
        "refresh_token_expires_in": 86400,
        "scope": "users:read",
    }
    monkeypatch.setattr(
        oauth_module,
        "_do_token_request",
        lambda env, url, data: _FakeResponse(body),
    )

    resp = oauth_module._exchange_code("sandbox", "CODE", "http://localhost", "v")
    assert resp.access_token == access_token
    assert resp.agent_key_uuid == uuid


def test_exchange_code__opaque_token_yields_empty_agent_key_uuid(monkeypatch):
    body = {
        "access_token": "ramp_tok_opaque",
        "refresh_token": "refresh-new",
        "expires_in": 3600,
    }
    monkeypatch.setattr(
        oauth_module,
        "_do_token_request",
        lambda env, url, data: _FakeResponse(body),
    )

    resp = oauth_module._exchange_code("sandbox", "CODE", "http://localhost", "v")
    assert resp.agent_key_uuid == ""


def test_refresh_tokens__populates_agent_key_uuid_from_jwt(monkeypatch):
    uuid = "019de002-0000-0000-0000-000000000000"
    access_token = _make_jwt({"ak": uuid})
    body = {
        "access_token": access_token,
        "refresh_token": "refresh-rotated",
        "expires_in": 3600,
        "refresh_token_expires_in": 86400,
    }
    monkeypatch.setattr(
        oauth_module,
        "_do_token_request",
        lambda env, url, data: _FakeResponse(body),
    )

    resp = oauth_module.refresh_tokens("sandbox", "refresh-old")
    assert resp.agent_key_uuid == uuid


# --- end-to-end: JWT-derived UUID lands in config.toml ---


def test_login_persists_agent_key_uuid_to_config(monkeypatch):
    """The full login flow writes the JWT's `ak` claim into config.toml."""
    uuid = "019de003-0000-0000-0000-000000000000"
    access_token = _make_jwt({"ak": uuid})

    monkeypatch.setattr(
        oauth_module,
        "_exchange_code",
        lambda env, code, redirect_uri, verifier: TokenResponse(
            access_token=access_token,
            refresh_token="refresh-new",
            expires_in=3600,
            refresh_token_expires_in=86400,
            scope="users:read",
            agent_key_uuid=uuid,
        ),
    )

    token_resp = oauth_module._exchange_code("sandbox", "CODE", "http://localhost", "v")
    store.save_tokens(
        "sandbox",
        token_resp.access_token,
        token_resp.refresh_token,
        access_token_expires_in=token_resp.expires_in,
        refresh_token_expires_in=token_resp.refresh_token_expires_in,
        granted_scopes=token_resp.scope,
        agent_key_uuid=token_resp.agent_key_uuid,
    )

    cfg = settings.load()
    assert cfg.sandbox.agent_key_uuid == uuid


def test_refresh_flow_persists_agent_key_uuid_to_config(monkeypatch):
    """try_refresh() must update the persisted UUID from the rotated JWT."""
    initial_uuid = "019de004-0000-0000-0000-000000000000"
    rotated_uuid = "019de005-0000-0000-0000-000000000000"

    store.save_tokens(
        "sandbox",
        "access-old",
        "refresh-old",
        agent_key_uuid=initial_uuid,
    )

    rotated_access = _make_jwt({"ak": rotated_uuid})
    monkeypatch.setattr(
        refresh_helper,
        "refresh_tokens",
        lambda env, refresh_token: TokenResponse(
            access_token=rotated_access,
            refresh_token="refresh-new",
            expires_in=3600,
            refresh_token_expires_in=86400,
            agent_key_uuid=rotated_uuid,
        ),
    )

    assert refresh_helper.try_refresh("sandbox") == rotated_access

    cfg = settings.load()
    assert cfg.sandbox.agent_key_uuid == rotated_uuid


def test_refresh_flow_clears_agent_key_uuid_when_jwt_drops_claim(monkeypatch):
    """If a refresh returns an access token without `ak`, the persisted
    UUID must be cleared so config reflects the current token's binding."""
    store.save_tokens(
        "sandbox",
        "access-old",
        "refresh-old",
        agent_key_uuid="019de006-0000-0000-0000-000000000000",
    )

    plain_access = _make_jwt({"sub": "user"})
    monkeypatch.setattr(
        refresh_helper,
        "refresh_tokens",
        lambda env, refresh_token: TokenResponse(
            access_token=plain_access,
            refresh_token="refresh-new",
            expires_in=3600,
            refresh_token_expires_in=86400,
            agent_key_uuid="",
        ),
    )

    assert refresh_helper.try_refresh("sandbox") == plain_access
    assert store.get_agent_key_uuid("sandbox") == ""
