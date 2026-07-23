"""Tests for the effective agent-tool availability overlay."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from ramp_cli.errors import ApiError
from ramp_cli.tools.availability import ToolAvailability, fetch_availability
from ramp_cli.tools.parser import ToolDef


def _tool(path: str = "/developer/v1/agent-tools/get-funds", method: str = "post"):
    return ToolDef(
        name="get-funds",
        path=path,
        http_method=method,
        summary="List funds",
        description="List funds",
        category="funds",
    )


def _payload() -> dict:
    return {
        "content_hash": "sha256:abc",
        "requested_tools": None,
        "tools": [
            {
                "tool": "get-funds",
                "method": "POST",
                "available": True,
                "unavailable_reasons": None,
                "missing_scopes": None,
            },
            {
                "tool": "get-transactions",
                "method": "GET",
                "available": False,
                "unavailable_reasons": ["missing_scopes", "disabled_for_business"],
                "missing_scopes": ["transactions:read"],
            },
        ],
    }


@pytest.fixture()
def availability_enabled(monkeypatch):
    monkeypatch.delenv("RAMP_NO_TOOL_AVAILABILITY", raising=False)
    monkeypatch.setattr(
        "ramp_cli.tools.availability.store.is_authenticated", lambda env: True
    )


def _mock_client(monkeypatch, **kwargs) -> MagicMock:
    client = MagicMock()
    client.get = MagicMock(**kwargs)
    monkeypatch.setattr("ramp_cli.tools.availability.RampClient", lambda env: client)
    return client


class TestFetchAvailability:
    def test_parses_response_and_joins_on_tool_defs(
        self, availability_enabled, monkeypatch
    ):
        _mock_client(monkeypatch, return_value=json.dumps(_payload()).encode())

        snapshot = fetch_availability("production")

        assert snapshot is not None
        assert snapshot.content_hash == "sha256:abc"
        # Joins on the path segment under /agent-tools/ plus the (case-
        # insensitive) HTTP method.
        entry = snapshot.lookup(_tool(method="post"))
        assert entry is not None and entry.available

        blocked = snapshot.lookup(
            _tool("/developer/v1/agent-tools/get-transactions", "get")
        )
        assert blocked is not None and not blocked.available
        assert blocked.unavailable_reasons == (
            "missing_scopes",
            "disabled_for_business",
        )
        assert blocked.missing_scopes == ("transactions:read",)

    def test_lookup_misses_are_none(self, availability_enabled, monkeypatch):
        _mock_client(monkeypatch, return_value=json.dumps(_payload()).encode())

        snapshot = fetch_availability("production")

        # Wrong method for a known tool.
        assert snapshot.lookup(_tool(method="delete")) is None
        # Tool the server did not report.
        assert snapshot.lookup(_tool("/developer/v1/agent-tools/unknown")) is None
        # Non-agent-tools command (e.g. synthesized or hand-written path).
        assert snapshot.lookup(_tool("/developer/v1/applications/progress")) is None

    @pytest.mark.parametrize(
        "client_kwargs",
        [
            {"side_effect": ApiError(403, "forbidden")},
            {"return_value": b"not json"},
        ],
    )
    def test_fails_open_on_any_problem(
        self, availability_enabled, monkeypatch, client_kwargs
    ):
        _mock_client(monkeypatch, **client_kwargs)

        assert fetch_availability("production") is None

    def test_skips_fetch_without_credentials_or_with_kill_switch(self, monkeypatch):
        client = _mock_client(monkeypatch, return_value=json.dumps(_payload()).encode())
        monkeypatch.delenv("RAMP_ACCESS_TOKEN", raising=False)
        monkeypatch.setattr(
            "ramp_cli.tools.availability.store.is_authenticated", lambda env: False
        )

        # Kill switch wins even when authenticated.
        monkeypatch.setenv("RAMP_NO_TOOL_AVAILABILITY", "1")
        monkeypatch.setenv("RAMP_ACCESS_TOKEN", "static-token")
        assert fetch_availability("production") is None

        # No credential at all: skip without a request.
        monkeypatch.delenv("RAMP_NO_TOOL_AVAILABILITY")
        monkeypatch.delenv("RAMP_ACCESS_TOKEN")
        assert fetch_availability("production") is None
        client.get.assert_not_called()

        # A static token counts as a credential.
        monkeypatch.setenv("RAMP_ACCESS_TOKEN", "static-token")
        assert fetch_availability("production") is not None


def test_describe_renders_reasons():
    entry = ToolAvailability(
        available=False,
        unavailable_reasons=(
            "missing_scopes",
            "disabled_for_business",
            "some_future_reason",
        ),
        missing_scopes=("transactions:read", "limits:read"),
    )
    assert entry.describe() == (
        "missing scope(s): transactions:read, limits:read; "
        "not enabled for your business; some future reason"
    )
