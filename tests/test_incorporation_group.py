"""Tests for the full ``ramp incorporation`` subcommand group.

Smoke tests per subcommand (stubbed HTTP) + help-text snapshot tests.
The SSN-specific submit tests live in test_incorporation_submit.py.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from ramp_cli.main import cli

# ---------------------------------------------------------------------------
# Help-text snapshot tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["incorporation", "--help"],
        ["incorporation", "states", "--help"],
        ["incorporation", "industries", "--help"],
        ["incorporation", "industries", "search", "--help"],
        ["incorporation", "countries", "--help"],
        ["incorporation", "applicant", "--help"],
        ["incorporation", "applicant", "create", "--help"],
        ["incorporation", "applicant", "get", "--help"],
        ["incorporation", "submit", "--help"],
        ["incorporation", "status", "--help"],
        ["incorporation", "documents", "--help"],
    ],
    ids=[
        "group_help",
        "states_help",
        "industries_help",
        "industries_search_help",
        "countries_help",
        "applicant_help",
        "applicant_create_help",
        "applicant_get_help",
        "submit_help",
        "status_help",
        "documents_help",
    ],
)
def test_incorporation_help_text_exits_zero(args):
    """Every incorporation subcommand's --help must exit 0."""
    runner = CliRunner()
    result = runner.invoke(cli, args, catch_exceptions=False)
    assert result.exit_code == 0, f"--help for {args} failed:\n{result.output}"


def test_incorporation_group_lists_all_commands():
    """``ramp incorporation --help`` must mention all 7 top-level commands."""
    runner = CliRunner()
    result = runner.invoke(cli, ["incorporation", "--help"], catch_exceptions=False)
    assert result.exit_code == 0
    for cmd in (
        "states",
        "industries",
        "countries",
        "applicant",
        "submit",
        "status",
        "documents",
    ):
        assert cmd in result.output, (
            f"'{cmd}' missing from incorporation help:\n{result.output}"
        )


def test_incorporation_submit_help_documents_form_only_ssn_entry():
    """submit --help must direct SSN entry to the Ramp form only."""
    runner = CliRunner()
    result = runner.invoke(
        cli, ["incorporation", "submit", "--help"], catch_exceptions=False
    )
    assert result.exit_code == 0
    compact_output = "".join(result.output.split())
    assert "nevercollectsSSN" in compact_output
    assert "Rampapplicationform" in compact_output
    assert "RAMP_INCORPORATION" not in compact_output
    assert "securelyprompts" not in compact_output


# ---------------------------------------------------------------------------
# Smoke tests per subcommand (stubbed GET/POST)
# ---------------------------------------------------------------------------


def _fake_get(self, path: str, params=None) -> bytes:
    return json.dumps({"path": path, "params": params or {}}).encode()


def _fake_post(self, path: str, json_body: bytes) -> bytes:
    body = json.loads(json_body)
    return json.dumps({"path": path, "body": body}).encode()


@pytest.mark.parametrize(
    "args, expected_path_fragment",
    [
        (
            ["incorporation", "states"],
            "/developer/v1/incorporation/states",
        ),
        (
            ["incorporation", "countries"],
            "/developer/v1/incorporation/countries",
        ),
        (
            ["incorporation", "applicant", "get"],
            "/developer/v1/incorporation/applicant",
        ),
        (
            ["incorporation", "status"],
            "/developer/v1/incorporation/company-status",
        ),
        (
            ["incorporation", "documents"],
            "/developer/v1/incorporation/documents",
        ),
    ],
    ids=["states", "countries", "applicant_get", "status", "documents"],
)
def test_get_commands_call_correct_path(args, expected_path_fragment, monkeypatch):
    """GET commands must hit the expected path stub."""
    monkeypatch.setattr("ramp_cli.client.api.RampClient.get", _fake_get)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["--agent", "--env", "sandbox"] + args, catch_exceptions=False
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["data"][0]["path"] == expected_path_fragment


def test_industries_search_passes_query_param(monkeypatch):
    """industries search must pass --q as Core's search query param."""
    monkeypatch.setattr("ramp_cli.client.api.RampClient.get", _fake_get)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "incorporation",
            "industries",
            "search",
            "--q",
            "saas restaurant",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["data"][0]["path"] == "/developer/v1/incorporation/industries"
    assert data["data"][0]["params"].get("search") == "saas restaurant"


def test_industries_search_requires_query():
    """industries search without --q must exit non-zero."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--env", "sandbox", "incorporation", "industries", "search"],
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    assert "--q" in result.output or "q" in result.output.lower()


def test_applicant_create_posts_country(monkeypatch):
    """applicant create --country-of-residence posts body with the country."""
    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", _fake_post)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "incorporation",
            "applicant",
            "create",
            "--country-of-residence",
            "US",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["data"][0]["path"] == "/developer/v1/incorporation/applicant"
    assert data["data"][0]["body"]["country_of_residence"] == "US"


def test_applicant_create_accepts_raw_json(monkeypatch):
    """applicant create --json '<body>' posts the raw body."""
    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", _fake_post)

    raw_body = {"country_of_residence": "GB", "extra": "field"}

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "incorporation",
            "applicant",
            "create",
            "--json",
            json.dumps(raw_body),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["data"][0]["body"]["country_of_residence"] == "GB"
    assert data["data"][0]["body"]["extra"] == "field"
