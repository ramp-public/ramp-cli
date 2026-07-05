"""Tests for pre-EIN early-access annotation on ``ramp incorporation status``.

The pre-EIN flow lets a business onboard once the formation has been filed
(status SUBMITTED) but before the EIN is issued. ``status`` annotates the
Ramp-facing ``formation_submission_status`` with a ``pre_ein`` block describing
the access the business has and what unblocks full access.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from ramp_cli.commands.incorporation import _pre_ein_status_note
from ramp_cli.main import cli

# ---------------------------------------------------------------------------
# Pure helper: status -> pre-EIN note
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status, expected_access",
    [
        ("SUBMITTED", "LIMITED"),
        ("submitted", "LIMITED"),  # case-insensitive
        ("APPROVED", "FULL_PENDING_KYB"),
        ("REJECTED", "AT_RISK"),
    ],
    ids=["submitted", "submitted_lowercase", "approved", "rejected"],
)
def test_pre_ein_note_present_for_lifecycle_statuses(status, expected_access):
    note = _pre_ein_status_note(status)
    assert note is not None
    assert note["access"] == expected_access
    assert "summary" in note and "unblocks_full_access" in note


@pytest.mark.parametrize(
    "status",
    ["NOT_SUBMITTED", "PENDING_REVIEW", "COMPLETED", "FAILED", "UNKNOWN", "", None],
    ids=[
        "not_submitted",
        "pending_review",
        "completed",
        "failed",
        "unknown",
        "empty",
        "none",
    ],
)
def test_pre_ein_note_absent_for_non_pre_ein_statuses(status):
    assert _pre_ein_status_note(status) is None


# ---------------------------------------------------------------------------
# status command: annotation wiring
# ---------------------------------------------------------------------------


def _fake_get_factory(body: dict):
    def _fake_get(self, path: str, params=None) -> bytes:
        return json.dumps(body).encode()

    return _fake_get


@pytest.mark.parametrize(
    "status, expected_access, expected_ein",
    [
        ("SUBMITTED", "LIMITED", "pending"),
        ("APPROVED", "FULL_PENDING_KYB", "issued"),
        ("REJECTED", "AT_RISK", "not issued"),
    ],
    ids=["submitted", "approved", "rejected"],
)
def test_status_attaches_pre_ein_note_for_live_status_response(
    status, expected_access, expected_ein, monkeypatch
):
    """Live formation_submission_status values get pre_ein annotations."""
    monkeypatch.setattr(
        "ramp_cli.client.api.RampClient.get",
        _fake_get_factory({"formation_submission_status": status}),
    )
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--agent", "--env", "sandbox", "incorporation", "status"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"][0]
    assert payload["pre_ein"]["access"] == expected_access
    assert payload["pre_ein"]["ein"] == expected_ein


@pytest.mark.parametrize(
    "body",
    [
        {"formation_submission_status": "PENDING_REVIEW"},
        {"formation_status": "SUBMITTED"},
        {"status": "SUBMITTED"},
    ],
    ids=["pending_review", "old_formation_status_key", "old_status_key"],
)
def test_status_no_annotation_for_non_live_status_shape(body, monkeypatch):
    """Only the live formation_submission_status contract drives annotation."""
    monkeypatch.setattr(
        "ramp_cli.client.api.RampClient.get",
        _fake_get_factory(body),
    )
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--agent", "--env", "sandbox", "incorporation", "status"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"][0]
    assert "pre_ein" not in payload


def test_status_no_annotation_when_status_absent(monkeypatch):
    """A response with no recognizable formation status is passed through unchanged."""
    monkeypatch.setattr(
        "ramp_cli.client.api.RampClient.get",
        _fake_get_factory({"some_other_field": "value"}),
    )
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--agent", "--env", "sandbox", "incorporation", "status"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"][0]
    assert "pre_ein" not in payload
