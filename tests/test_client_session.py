"""Tests for the external session ID module."""

from __future__ import annotations

import time

import pytest

from ramp_cli.client import session


@pytest.fixture(autouse=True)
def _clean_session():
    session.reset()
    yield
    session.reset()


def test_returns_stable_id():
    sid1 = session.get_session_id()
    sid2 = session.get_session_id()
    assert sid1 == sid2


@pytest.mark.parametrize(
    "offset, should_rotate",
    [
        (session.SESSION_TIMEOUT - 1, False),
        (session.SESSION_TIMEOUT, True),
        (session.SESSION_TIMEOUT + 60, True),
    ],
    ids=["just-under", "exact-boundary", "well-past"],
)
def test_rotation_at_timeout_boundary(monkeypatch, offset, should_rotate):
    t = time.monotonic()
    monkeypatch.setattr("ramp_cli.client.session.time.monotonic", lambda: t)
    sid1 = session.get_session_id()

    monkeypatch.setattr("ramp_cli.client.session.time.monotonic", lambda: t + offset)
    sid2 = session.get_session_id()
    assert (sid1 != sid2) == should_rotate


def test_reset_clears_session():
    sid1 = session.get_session_id()
    session.reset()
    sid2 = session.get_session_id()
    assert sid1 != sid2
