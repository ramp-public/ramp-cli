"""Tests for the ``ramp incorporation submit`` command.

Coverage goals:
- Happy path: request body is posted exactly without CLI-side SSN insertion.
- Env vars and stdin input are ignored; SSN entry belongs in the Ramp form.
- ``--dry-run`` flag → no POST and no SSN in output.
- Caller-supplied SSN in --json → rejected before any request is sent.
"""

from __future__ import annotations

import json
import re

import pytest
from click.testing import CliRunner

from ramp_cli.main import cli

_SSN_LAST_4_VALUE = "6789"
_FULL_SSN_VALUE = "123-45-6789"
_COMPACT_SSN_VALUE = "123456789"

_SINGLE_MEMBER_BODY = {
    "state": "DE",
    "naics_code": "541511",
    "members": [
        {
            "legal_first_name": "Jane",
            "legal_last_name": "Doe",
            "is_natural_person": True,
        }
    ],
}

_BODY_WITH_RP = {
    "state": "DE",
    "naics_code": "541511",
    "members": [{"legal_first_name": "Jane", "legal_last_name": "Doe"}],
    "responsible_party": {"legal_first_name": "Jane", "legal_last_name": "Doe"},
}

_LEAN_FORMATION_BODY = {
    "state": "DE",
    "naics_code": "513210",
    "description": "Synthetic nutrition tracking test business.",
    "name_options": [
        {"name": "Example Nutrition Fixture", "entity_type_ending": "LLC"},
        {"name": "Example Nutrition Labs", "entity_type_ending": "LLC"},
        {"name": "Example Nutrition AI", "entity_type_ending": "LLC"},
    ],
    "addresses": [
        {"provider": "ramp", "address_type": "registered_agent"},
        {
            "provider": "user",
            "address_type": "mailing",
            "line1": "123 Test Fixture Ave",
            "city": "Testville",
            "state": "CA",
            "postal_code": "90001",
            "country": "US",
            "phone": "+15550101000",
        },
    ],
}


def _contains_ssn(text: str) -> bool:
    """Return True if any SSN-like value appears in *text*."""
    return bool(re.search(r"\d{3}-\d{2}-\d{4}|\b\d{9}\b|\b\d{4}\b", text))


def test_submit__posts_body_without_cli_side_ssn_insertion(monkeypatch):
    captured: dict[str, object] = {}

    def fake_post(self, path: str, json_body: bytes) -> bytes:
        captured["path"] = path
        captured["body"] = json.loads(json_body)
        return b'{"formation_submission_status": "PENDING_REVIEW"}'

    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", fake_post)
    monkeypatch.setenv("RAMP_INCORPORATION_MEMBER_1_SSN_LAST_4", _SSN_LAST_4_VALUE)

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
    assert captured["path"] == "/developer/v1/incorporation/formation"
    assert captured["body"] == _SINGLE_MEMBER_BODY
    assert "ssn_last_4" not in captured["body"]["members"][0]
    assert not _contains_ssn(result.output)


def test_submit__does_not_prompt_for_ssn(monkeypatch):
    captured: dict[str, object] = {}

    def fake_post(self, path: str, json_body: bytes) -> bytes:
        captured["body"] = json.loads(json_body)
        return b'{"formation_submission_status": "PENDING_REVIEW"}'

    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", fake_post)
    monkeypatch.delenv("RAMP_INCORPORATION_MEMBER_1_SSN_LAST_4", raising=False)

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
        input=f"{_SSN_LAST_4_VALUE}\n",
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert captured["body"] == _SINGLE_MEMBER_BODY
    assert not _contains_ssn(result.output)


def test_submit__responsible_party_body_is_unchanged(monkeypatch):
    captured: dict[str, object] = {}

    def fake_post(self, path: str, json_body: bytes) -> bytes:
        captured["body"] = json.loads(json_body)
        return b'{"formation_submission_status": "PENDING_REVIEW"}'

    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", fake_post)
    monkeypatch.setenv(
        "RAMP_INCORPORATION_RESPONSIBLE_PARTY_SSN_LAST_4", _SSN_LAST_4_VALUE
    )

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

    assert result.exit_code == 0, result.output
    assert captured["body"] == _BODY_WITH_RP
    assert "ssn_last_4" not in captured["body"]["responsible_party"]
    assert "ssn_last_4" not in captured["body"]["members"][0]


def test_submit__accepts_lean_fa_sourced_payload(monkeypatch):
    """Submitted FA data can source owner/controller fields server-side."""
    captured: dict[str, object] = {}

    def fake_post(self, path: str, json_body: bytes) -> bytes:
        captured["path"] = path
        captured["body"] = json.loads(json_body)
        return b'{"formation_submission_status": "PENDING_REVIEW"}'

    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", fake_post)
    monkeypatch.setenv("RAMP_INCORPORATION_MEMBER_1_SSN_LAST_4", _SSN_LAST_4_VALUE)
    monkeypatch.setenv(
        "RAMP_INCORPORATION_RESPONSIBLE_PARTY_SSN_LAST_4", _SSN_LAST_4_VALUE
    )

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
            json.dumps(_LEAN_FORMATION_BODY),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert captured["path"] == "/developer/v1/incorporation/formation"
    assert captured["body"] == _LEAN_FORMATION_BODY
    assert "members" not in captured["body"]
    assert "responsible_party" not in captured["body"]
    assert not _contains_ssn(result.output)


def test_submit__dry_run_does_not_call_post(monkeypatch):
    called = {"post": False}

    def fail_post(self, path: str, json_body: bytes) -> bytes:
        called["post"] = True
        raise AssertionError("POST should not be called in dry-run mode")

    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", fail_post)
    monkeypatch.setenv("RAMP_INCORPORATION_MEMBER_1_SSN_LAST_4", _SSN_LAST_4_VALUE)

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
    assert not _contains_ssn(result.output)


@pytest.mark.parametrize(
    "dry_run_flag",
    ["--dry-run", "--dry_run", "-n"],
    ids=["hyphen", "underscore", "short"],
)
def test_submit__dry_run_flag_aliases_all_work(dry_run_flag, monkeypatch):
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


def test_submit__dry_run_shows_body_without_ssn(monkeypatch):
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
    assert body == _SINGLE_MEMBER_BODY
    assert "ssn_last_4" not in body["members"][0]
    assert not _contains_ssn(result.output)


@pytest.mark.parametrize(
    "body_with_ssn",
    [
        {"members": [{"ssn": _FULL_SSN_VALUE, "legal_first_name": "Jane"}]},
        {"members": [{"SSN": _FULL_SSN_VALUE}]},
        {"responsible_party": {"ssn": _FULL_SSN_VALUE}},
        {"members": [{"member_ssn": _FULL_SSN_VALUE}]},
        {"members": [{"ssn_number": _FULL_SSN_VALUE}]},
        {"members": [{"applicant_ssn_value": _FULL_SSN_VALUE}]},
        {"responsible_party": {"social_security_number": _FULL_SSN_VALUE}},
        {"responsible_party": {"SOCIAL_SECURITY": _FULL_SSN_VALUE}},
        {"members": [{"memberSSN": "must-use-form"}]},
        {"members": [{"applicantSsnValue": "must-use-form"}]},
        {"members": [{"memberSsn": "must-use-form"}]},
        {"responsible_party": {"socialSecurityNumber": "must-use-form"}},
        {"members": [{"ssnnumber": "must-use-form"}]},
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
    called = {"post": False}

    def fail_post(self, path: str, json_body: bytes) -> bytes:
        called["post"] = True
        raise AssertionError("POST must not run when SSN is present")

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
            json.dumps(body_with_ssn),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code != 0
    assert not called["post"]
    assert "SSN" in result.output or "ssn" in result.output.lower()
    assert "Ramp application form" in result.output
    assert not _contains_ssn(result.output)


@pytest.mark.parametrize(
    "sneaky_value",
    [_FULL_SSN_VALUE, _COMPACT_SSN_VALUE],
    ids=["dashed_full_ssn", "compact_full_ssn"],
)
def test_submit__rejects_ssn_shaped_value_under_unexpected_key(
    sneaky_value, monkeypatch
):
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
                "legal_first_name": "Jane",
                "legal_last_name": "Doe",
                "tax_id_freeform": sneaky_value,
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
    assert not _contains_ssn(result.output)


def test_submit__allows_four_digit_values_in_non_ssn_paths(monkeypatch):
    captured: dict[str, object] = {}

    def fake_post(self, path: str, json_body: bytes) -> bytes:
        captured["body"] = json.loads(json_body)
        return b'{"formation_submission_status": "PENDING_REVIEW"}'

    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", fake_post)

    body = {
        "state": "DE",
        "naics_code": "541511",
        "members": [
            {
                "legal_first_name": "Jane",
                "legal_last_name": "Doe",
                "address": {
                    "line1": "1 Market St",
                    "city": "Sydney",
                    "country": "AUS",
                    "postal_code": "2000",
                },
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
            json.dumps(body),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert captured["body"] == body
    assert captured["body"]["members"][0]["address"]["postal_code"] == "2000"


def test_submit__benign_key_containing_ssn_substring_is_accepted(monkeypatch):
    captured: dict[str, object] = {}

    def fake_post(self, path: str, json_body: bytes) -> bytes:
        captured["body"] = json.loads(json_body)
        return b'{"formation_submission_status": "PENDING_REVIEW"}'

    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", fake_post)

    body = {
        "state": "DE",
        "naics_code": "541511",
        "className": "premium",
        "members": [{"legal_first_name": "Jane", "legal_last_name": "Doe"}],
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
    assert captured["body"] == body
