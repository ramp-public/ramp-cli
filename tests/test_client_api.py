"""Tests for API client auth behavior."""

from __future__ import annotations

import pytest

from ramp_cli.auth.store import TokenState
from ramp_cli.client.api import RampClient
from ramp_cli.errors import AuthRequiredError, RefreshFailedError


def test_get_access_token__refreshes_when_only_refresh_token_exists(monkeypatch):
    client = RampClient("sandbox")

    monkeypatch.setattr(
        "ramp_cli.client.api.store.get_token_state",
        lambda env: TokenState(refresh_token="refresh-only"),
    )
    monkeypatch.setattr("ramp_cli.client.api.try_refresh", lambda env: "access-new")

    assert client._get_access_token() == "access-new"


def test_get_access_token__raises_when_refresh_fails(monkeypatch):
    client = RampClient("sandbox")

    monkeypatch.setattr(
        "ramp_cli.client.api.store.get_token_state",
        lambda env: TokenState(refresh_token="refresh-only"),
    )
    monkeypatch.setattr("ramp_cli.client.api.try_refresh", lambda env: None)

    with pytest.raises(AuthRequiredError):
        client._get_access_token()


def test_get_access_token__refreshes_proactively_when_expiring_soon(monkeypatch):
    client = RampClient("sandbox")

    monkeypatch.setattr(
        "ramp_cli.client.api.store.get_token_state",
        lambda env: TokenState(
            access_token="access-old",
            refresh_token="refresh-old",
            access_token_issued_at=100,
            access_token_expires_in=300,
        ),
    )
    monkeypatch.setattr("ramp_cli.client.api.time.time", lambda: 380)
    monkeypatch.setattr("ramp_cli.client.api.try_refresh", lambda env: "access-new")

    assert client._get_access_token() == "access-new"


def test_get_access_token__uses_current_token_when_proactive_refresh_fails(
    monkeypatch,
):
    client = RampClient("sandbox")

    monkeypatch.setattr(
        "ramp_cli.client.api.store.get_token_state",
        lambda env: TokenState(
            access_token="access-old",
            refresh_token="refresh-old",
            access_token_issued_at=100,
            access_token_expires_in=300,
        ),
    )
    monkeypatch.setattr("ramp_cli.client.api.time.time", lambda: 380)

    def fail_refresh(env: str) -> str:
        raise RefreshFailedError("temporarily unavailable")

    monkeypatch.setattr("ramp_cli.client.api.try_refresh", fail_refresh)

    assert client._get_access_token() == "access-old"
