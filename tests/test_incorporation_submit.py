"""Tests for the ``ramp incorporation submit`` command (ADP-2789).

Coverage goals:
- Happy path: all env vars set → request body contains SSN values, stdout
  contains no SSN literal.
- Missing env var → structured error naming the var, exit non-zero, no SSN leak.
- ``--dry-run`` flag → still no SSN in output.
- Caller-supplied SSN in --json → rejected before any env-var read.
"""

from __future__ import annotations

import json
import re

import pytest
from click.testing import CliRunner

from ramp_cli.commands.incorporation import MissingSSNEnvVarError
from ramp_cli.main import cli

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SSN_VALUE = "123-45-6789"
_SSN2_VALUE = "987-65-4321"

_SINGLE_MEMBER_BODY = {
    "state": "DE",
    "naics_code": "541511",
    "members": [
        {
            "legal_first_name": "Zack",
            "legal_last_name": "Field",
            "is_natural_person": True,
        }
    ],
}

_MULTI_MEMBER_BODY = {
    "state": "DE",
    "naics_code": "541511",
    "members": [
        {"legal_first_name": "Zack", "legal_last_name": "Field"},
        {"legal_first_name": "Jane", "legal_last_name": "Doe"},
    ],
}

_BODY_WITH_RP = {
    "state": "DE",
    "naics_code": "541511",
    "members": [{"legal_first_name": "Zack", "legal_last_name": "Field"}],
    "responsible_party": {"legal_first_name": "Zack", "legal_last_name": "Field"},
}


def _contains_ssn(text: str) -> bool:
    """Return True if any SSN-like value appears in *text*."""
    # Matches NNN-NN-NNNN or NNNNNNNNN (9 consecutive digits)
    return bool(re.search(r"\d{3}-\d{2}-\d{4}|\b\d{9}\b", text))


# ---------------------------------------------------------------------------
# Happy path — env vars set → SSN in request body, NOT in output
# ---------------------------------------------------------------------------


def test_submit__ssn_slotted_into_request_body(monkeypatch):
    """All env vars set → request body contains real SSN values."""
    captured: dict[str, object] = {}

    def fake_post(self, path: str, json_body: bytes) -> bytes:
        captured["path"] = path
        captured["body"] = json.loads(json_body)
        return b'{"formation_submission_status": "PENDING_REVIEW"}'

    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", fake_post)
    monkeypatch.setenv("RAMP_INCORPORATION_SSN_MEMBER_1", _SSN_VALUE)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "incorporation",
            "submit",
            "--json",
            json.dumps(_SINGLE_MEMBER_BODY),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert captured["body"]["members"][0]["ssn"] == _SSN_VALUE
    assert captured["path"] == "/developer/v1/applications/incorporation/submit"


def test_submit__ssn_never_appears_in_stdout(monkeypatch):
    """Even though SSN is in the request body, it must never appear in stdout."""

    def fake_post(self, path: str, json_body: bytes) -> bytes:
        return b'{"formation_submission_status": "PENDING_REVIEW"}'

    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", fake_post)
    monkeypatch.setenv("RAMP_INCORPORATION_SSN_MEMBER_1", _SSN_VALUE)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "incorporation",
            "submit",
            "--json",
            json.dumps(_SINGLE_MEMBER_BODY),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert not _contains_ssn(result.output), (
        f"SSN value leaked into stdout:\n{result.output}"
    )


def test_submit__multi_member_ssns_slotted_correctly(monkeypatch):
    """Two members → RAMP_INCORPORATION_SSN_MEMBER_1 and _2 both slotted."""
    captured: dict[str, object] = {}

    def fake_post(self, path: str, json_body: bytes) -> bytes:
        captured["body"] = json.loads(json_body)
        return b'{"formation_submission_status": "PENDING_REVIEW"}'

    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", fake_post)
    monkeypatch.setenv("RAMP_INCORPORATION_SSN_MEMBER_1", _SSN_VALUE)
    monkeypatch.setenv("RAMP_INCORPORATION_SSN_MEMBER_2", _SSN2_VALUE)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "incorporation",
            "submit",
            "--json",
            json.dumps(_MULTI_MEMBER_BODY),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert captured["body"]["members"][0]["ssn"] == _SSN_VALUE
    assert captured["body"]["members"][1]["ssn"] == _SSN2_VALUE


def test_submit__responsible_party_ssn_slotted(monkeypatch):
    """responsible_party present → RAMP_INCORPORATION_SSN_RESPONSIBLE_PARTY slotted."""
    captured: dict[str, object] = {}

    def fake_post(self, path: str, json_body: bytes) -> bytes:
        captured["body"] = json.loads(json_body)
        return b'{"formation_submission_status": "PENDING_REVIEW"}'

    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", fake_post)
    monkeypatch.setenv("RAMP_INCORPORATION_SSN_MEMBER_1", _SSN_VALUE)
    monkeypatch.setenv("RAMP_INCORPORATION_SSN_RESPONSIBLE_PARTY", _SSN_VALUE)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "incorporation",
            "submit",
            "--json",
            json.dumps(_BODY_WITH_RP),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert captured["body"]["responsible_party"]["ssn"] == _SSN_VALUE
    assert captured["body"]["members"][0]["ssn"] == _SSN_VALUE


# ---------------------------------------------------------------------------
# Missing env var → structured error, exit non-zero, no SSN leak
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "set_env_vars, missing_var, use_body_with_rp",
    [
        ({}, "RAMP_INCORPORATION_SSN_MEMBER_1", False),
        (
            {"RAMP_INCORPORATION_SSN_MEMBER_1": _SSN_VALUE},
            "RAMP_INCORPORATION_SSN_RESPONSIBLE_PARTY",
            True,
        ),
    ],
    ids=["no_env_vars_set", "missing_responsible_party_var"],
)
def test_submit__missing_env_var_exits_nonzero_and_names_var(
    set_env_vars, missing_var, use_body_with_rp, monkeypatch
):
    """Missing required env var → exit 1, error message names the missing var."""
    for k, v in set_env_vars.items():
        monkeypatch.setenv(k, v)
    # Ensure the missing var is not present
    monkeypatch.delenv(missing_var, raising=False)

    body = _BODY_WITH_RP if use_body_with_rp else _SINGLE_MEMBER_BODY

    # catch_exceptions=True so CliRunner captures the exception rather than
    # re-raising.  We inspect result.exception directly (CliRunner places the
    # caught exception there when standalone_mode=False bypasses main()'s
    # error handler).
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "incorporation",
            "submit",
            "--json",
            json.dumps(body),
        ],
        catch_exceptions=True,
    )

    assert result.exit_code != 0 or isinstance(result.exception, MissingSSNEnvVarError)
    # The exception message must name the missing variable
    exc = result.exception
    assert exc is not None
    assert missing_var in str(exc), (
        f"Expected '{missing_var}' in exception message but got: {exc!s}"
    )
    # No SSN value in any output or exception text
    assert not _contains_ssn(str(exc)), f"SSN value leaked in exception:\n{exc!s}"


def test_submit__missing_member_2_env_var_names_correct_var(monkeypatch):
    """With 2 members, missing MEMBER_2 → error names MEMBER_2 not MEMBER_1."""
    monkeypatch.setenv("RAMP_INCORPORATION_SSN_MEMBER_1", _SSN_VALUE)
    monkeypatch.delenv("RAMP_INCORPORATION_SSN_MEMBER_2", raising=False)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "incorporation",
            "submit",
            "--json",
            json.dumps(_MULTI_MEMBER_BODY),
        ],
        catch_exceptions=True,
    )

    assert isinstance(result.exception, MissingSSNEnvVarError)
    assert "RAMP_INCORPORATION_SSN_MEMBER_2" in str(result.exception)


# ---------------------------------------------------------------------------
# --dry-run flag → no POST, SSNs redacted in output
# ---------------------------------------------------------------------------


def test_submit__dry_run_does_not_call_post(monkeypatch):
    """--dry-run must not trigger an HTTP POST."""
    monkeypatch.setenv("RAMP_INCORPORATION_SSN_MEMBER_1", _SSN_VALUE)

    called = {"post": False}

    def fail_post(self, path: str, json_body: bytes) -> bytes:
        called["post"] = True
        raise AssertionError("POST should not be called in dry-run mode")

    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", fail_post)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "incorporation",
            "submit",
            "--json",
            json.dumps(_SINGLE_MEMBER_BODY),
            "--dry-run",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert not called["post"]
    assert not _contains_ssn(result.output), (
        f"SSN value leaked in dry-run output:\n{result.output}"
    )


@pytest.mark.parametrize(
    "dry_run_flag",
    ["--dry-run", "--dry_run", "-n"],
    ids=["hyphen", "underscore", "short"],
)
def test_submit__dry_run_flag_aliases_all_work(dry_run_flag, monkeypatch):
    """All three dry-run spellings (hyphen, underscore, -n) must be accepted.

    The rest of the CLI uses --dry_run; this command must accept both so
    agents copy-pasting from other commands don't hit a "no such option" error.
    """
    monkeypatch.setenv("RAMP_INCORPORATION_SSN_MEMBER_1", _SSN_VALUE)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "incorporation",
            "submit",
            "--json",
            json.dumps(_SINGLE_MEMBER_BODY),
            dry_run_flag,
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, f"flag {dry_run_flag} failed: {result.output}"
    payload = json.loads(result.output)
    assert payload["data"][0]["dry_run"] is True


def test_submit__dry_run_shows_redacted_body(monkeypatch):
    """--dry-run output uses <redacted> for SSN slots."""
    monkeypatch.setenv("RAMP_INCORPORATION_SSN_MEMBER_1", _SSN_VALUE)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "incorporation",
            "submit",
            "--json",
            json.dumps(_SINGLE_MEMBER_BODY),
            "--dry-run",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["data"][0]["dry_run"] is True
    body = payload["data"][0]["body"]
    assert body["members"][0]["ssn"] == "<redacted>"
    assert not _contains_ssn(result.output)


# ---------------------------------------------------------------------------
# Caller-supplied SSN in --json → rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body_with_ssn",
    [
        {"members": [{"ssn": "123-45-6789", "legal_first_name": "Zack"}]},
        {"members": [{"SSN": "123-45-6789"}]},
        {"responsible_party": {"ssn": "123-45-6789"}},
        # Snake-case variants — `_` is a word char so `\bssn\b` alone misses these
        {"members": [{"member_ssn": "123-45-6789"}]},
        {"members": [{"ssn_number": "123-45-6789"}]},
        {"members": [{"applicant_ssn_value": "123-45-6789"}]},
        {"responsible_party": {"social_security_number": "123-45-6789"}},
        {"responsible_party": {"SOCIAL_SECURITY": "123-45-6789"}},
        # camelCase / PascalCase variants — the key term scan must split these
        # on case boundaries.  Values here are deliberately NOT SSN-shaped so we
        # exercise *key* detection in isolation from value-shape detection.
        {"members": [{"memberSSN": "set-via-env"}]},
        {"members": [{"applicantSsnValue": "set-via-env"}]},
        {"members": [{"memberSsn": "set-via-env"}]},
        {"responsible_party": {"socialSecurityNumber": "set-via-env"}},
        # Concatenated-lowercase prefix (no separator, no case boundary).
        {"members": [{"ssnnumber": "set-via-env"}]},
    ],
    ids=[
        "member_ssn_lowercase",
        "member_ssn_uppercase",
        "responsible_party_ssn",
        "member_snake_member_ssn",
        "member_snake_ssn_number",
        "member_snake_applicant_ssn_value",
        "responsible_party_social_security_number",
        "responsible_party_social_security_upper",
        "member_camel_memberSSN",
        "member_camel_applicantSsnValue",
        "member_camel_memberSsn",
        "responsible_party_camel_socialSecurityNumber",
        "member_concat_ssnnumber",
    ],
)
def test_submit__rejects_ssn_in_json_body(body_with_ssn, monkeypatch):
    """Any SSN-like key inside --json must be rejected before env-var read."""
    monkeypatch.setenv("RAMP_INCORPORATION_SSN_MEMBER_1", _SSN_VALUE)
    monkeypatch.setenv("RAMP_INCORPORATION_SSN_RESPONSIBLE_PARTY", _SSN_VALUE)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "incorporation",
            "submit",
            "--json",
            json.dumps(body_with_ssn),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code != 0
    assert "SSN" in result.output or "ssn" in result.output.lower()
    # Even in rejection, no real SSN value should appear
    assert not _contains_ssn(result.output)


@pytest.mark.parametrize(
    "sneaky_value",
    ["123-45-6789", "123456789"],
    ids=["dashed", "nine_digits"],
)
def test_submit__rejects_ssn_shaped_value_under_unexpected_key(
    sneaky_value, monkeypatch
):
    """jchoi feedback: check SSN-related terms and SSN-shaped *values* separately.

    Even if a value is smuggled under an innocuous key name that the key term
    scan would not flag, an SSN-shaped value must be rejected before any env
    read or POST — and the rejection must never echo the value.
    """
    monkeypatch.setenv("RAMP_INCORPORATION_SSN_MEMBER_1", _SSN_VALUE)

    called = {"post": False}

    def fail_post(self, path: str, json_body: bytes) -> bytes:
        called["post"] = True
        raise AssertionError("POST must not run when an SSN-shaped value is present")

    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", fail_post)

    sneaky_body = {
        "state": "DE",
        "naics_code": "541511",
        "members": [
            {
                "legal_first_name": "Zack",
                "legal_last_name": "Field",
                "tax_id_freeform": sneaky_value,  # not flagged by key name
            }
        ],
    }

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "incorporation",
            "submit",
            "--json",
            json.dumps(sneaky_body),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code != 0
    assert not called["post"]
    assert "SSN" in result.output or "ssn" in result.output.lower()
    # Rejection must not echo the offending value
    assert not _contains_ssn(result.output), (
        f"SSN-shaped value leaked into rejection output:\n{result.output}"
    )


def test_submit__benign_key_containing_ssn_substring_is_accepted(monkeypatch):
    """Keys that merely *contain* the substring 'ssn' (e.g. 'className') must not
    be rejected — term detection matches whole tokens, not substrings.
    """
    captured: dict[str, object] = {}

    def fake_post(self, path: str, json_body: bytes) -> bytes:
        captured["body"] = json.loads(json_body)
        return b'{"formation_submission_status": "PENDING_REVIEW"}'

    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", fake_post)
    monkeypatch.setenv("RAMP_INCORPORATION_SSN_MEMBER_1", _SSN_VALUE)

    body = {
        "state": "DE",
        "naics_code": "541511",
        "className": "premium",  # contains "ssn" as a substring, not a token
        "members": [{"legal_first_name": "Zack", "legal_last_name": "Field"}],
    }

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "incorporation",
            "submit",
            "--json",
            json.dumps(body),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert captured["body"]["className"] == "premium"
    assert captured["body"]["members"][0]["ssn"] == _SSN_VALUE
