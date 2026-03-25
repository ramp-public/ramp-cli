"""Tests for the applications command."""

from __future__ import annotations

import json

from click.testing import CliRunner

from ramp_cli.commands.applications import (
    APPLICATION_CREATED_MESSAGE,
    APPLICATION_EXAMPLE,
    _merge_all_of,
)
from ramp_cli.config.constants import application_signup_token, base_url
from ramp_cli.main import cli

# ── Fake OpenAPI spec for schema unit tests ──

_FAKE_SPEC = {
    "components": {
        "schemas": {
            "Address": {
                "type": "object",
                "properties": {
                    "apt_suite": {"type": "string"},
                    "city": {"type": "string"},
                    "country": {"type": "string"},
                    "postal_code": {"type": "string"},
                    "state": {"type": "string"},
                    "street_address": {"type": "string"},
                },
            },
            "Person": {
                "type": "object",
                "properties": {
                    "address": {"$ref": "#/components/schemas/Address"},
                    "birth_date": {"type": "string"},
                    "email": {"type": "string"},
                    "first_name": {"type": "string"},
                    "is_beneficial_owner": {"type": "boolean"},
                    "last_name": {"type": "string"},
                    "phone": {"type": "string"},
                    "ssn_last_4": {"type": "string"},
                    "title": {"type": "string"},
                },
            },
            "Incorporation": {
                "type": "object",
                "properties": {
                    "date_of_incorporation": {"type": "string"},
                    "ein_number": {"type": "string"},
                    "entity_type": {"type": "string"},
                    "state_of_incorporation": {"type": "string"},
                },
            },
            "Business": {
                "type": "object",
                "properties": {
                    "address": {"$ref": "#/components/schemas/Address"},
                    "business_description": {"type": "string"},
                    "business_name_dba": {"type": ["string", "null"]},
                    "business_name_legal": {"type": "string"},
                    "business_name_on_card": {"type": ["string", "null"]},
                    "business_website": {"type": "string"},
                    "incorporation": {"$ref": "#/components/schemas/Incorporation"},
                    "phone": {"type": "string"},
                },
            },
            "FinancialDetails": {
                "type": "object",
                "properties": {
                    "estimated_monthly_ap_spend_amount": {"type": "integer"},
                    "estimated_monthly_spend_amount": {"type": "integer"},
                },
            },
            "OAuthAuthorizeParams": {
                "type": "object",
                "properties": {
                    "redirect_uri": {"type": "string"},
                    "state": {"type": "string"},
                },
            },
            "ApplicationCreateRequest": {
                "type": "object",
                "description": "Create a new application",
                "properties": {
                    "applicant": {
                        "allOf": [{"$ref": "#/components/schemas/Person"}],
                        "description": "The person applying",
                    },
                    "beneficial_owners": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/Person"},
                    },
                    "business": {"$ref": "#/components/schemas/Business"},
                    "controlling_officer": {"$ref": "#/components/schemas/Person"},
                    "financial_details": {
                        "$ref": "#/components/schemas/FinancialDetails"
                    },
                    "oauth_authorize_params": {
                        "$ref": "#/components/schemas/OAuthAuthorizeParams"
                    },
                },
                "example": {"applicant": {"first_name": "Jane"}},
            },
        },
    },
    "paths": {
        "/developer/v1/applications": {
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "$ref": "#/components/schemas/ApplicationCreateRequest"
                            }
                        }
                    }
                }
            }
        }
    },
}


def test_applications_create__prints_success_message(monkeypatch):

    captured: dict[str, object] = {}

    def fake_post(self, path: str, json_body: bytes) -> bytes:
        captured["path"] = path
        captured["body"] = json.loads(json_body)
        return b'{"ignored":"response"}'

    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", fake_post)

    runner = CliRunner()
    payload = {"applicant": {"email": "jane@example.com"}}

    result = runner.invoke(
        cli,
        ["--env", "sandbox", "applications", "create", "--json", json.dumps(payload)],
    )

    assert result.exit_code == 0
    assert APPLICATION_CREATED_MESSAGE in result.output
    assert captured["path"] == "/developer/v1/applications"
    assert captured["body"] == payload


def test_applications_create__prints_agent_json(monkeypatch):

    monkeypatch.setattr(
        "ramp_cli.client.api.RampClient.post",
        lambda self, path, json_body: b'{"ignored":"response"}',
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "applications",
            "create",
            "--json",
            '{"applicant":{"email":"jane@example.com"}}',
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload == {
        "schema_version": "1.0",
        "data": [{"message": APPLICATION_CREATED_MESSAGE}],
        "pagination": {"has_more": False, "next": None},
    }


def test_applications_create__rejects_invalid_json():

    runner = CliRunner()
    result = runner.invoke(
        cli, ["--env", "sandbox", "applications", "create", "--json", "not json"]
    )

    assert result.exit_code == 2
    assert "invalid JSON" in result.output


def test_applications_create__requires_json_object():

    runner = CliRunner()
    result = runner.invoke(
        cli, ["--env", "sandbox", "applications", "create", "--json", "[1, 2, 3]"]
    )

    assert result.exit_code == 2
    assert "application body must be a JSON object" in result.output


def test_applications_create__uses_dev_console_token(monkeypatch):

    captured_token: dict[str, object] = {}

    original_init = __import__(
        "ramp_cli.client.api", fromlist=["RampClient"]
    ).RampClient.__init__

    def spy_init(self, env, access_token=None):
        captured_token["access_token"] = access_token
        original_init(self, env, access_token=access_token)

    monkeypatch.setattr("ramp_cli.client.api.RampClient.__init__", spy_init)
    monkeypatch.setattr(
        "ramp_cli.client.api.RampClient.post",
        lambda self, path, json_body: b'{"ok":true}',
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--env",
            "sandbox",
            "applications",
            "create",
            "--json",
            '{"applicant":{"email":"test@example.com"}}',
        ],
    )

    assert result.exit_code == 0
    assert captured_token["access_token"] == application_signup_token("sandbox")


def test_applications_create__dry_run_prints_request_without_sending(monkeypatch):

    def fail_post(self, path: str, json_body: bytes) -> bytes:
        raise AssertionError("dry-run should not send the request")

    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", fail_post)

    runner = CliRunner()
    request_body = {"applicant": {"email": "jane@example.com"}}
    result = runner.invoke(
        cli,
        [
            "--human",
            "--env",
            "sandbox",
            "applications",
            "create",
            "--json",
            json.dumps(request_body),
            "--dry_run",
        ],
    )

    assert result.exit_code == 0
    assert (
        f"DRY RUN: POST {base_url('sandbox')}/developer/v1/applications"
        in result.output
    )
    assert json.dumps(request_body, indent=2) in result.output


def test_applications_create__example_prints_full_payload():

    runner = CliRunner()
    result = runner.invoke(cli, ["--human", "applications", "create", "--example"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload == APPLICATION_EXAMPLE


def test_applications_create__example_agent_mode_wraps_in_envelope():

    runner = CliRunner()
    result = runner.invoke(cli, ["--agent", "applications", "create", "--example"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload == {
        "schema_version": "1.0",
        "data": [APPLICATION_EXAMPLE],
        "pagination": {"has_more": False, "next": None},
    }


def test_applications_create__missing_json_shows_usage_error():

    runner = CliRunner()
    result = runner.invoke(cli, ["--env", "sandbox", "applications", "create"])

    assert result.exit_code != 0
    assert "--json" in result.output
    assert "--example" in result.output


def test_applications_create__dry_run_prints_agent_json(monkeypatch):

    def fail_post(self, path: str, json_body: bytes) -> bytes:
        raise AssertionError("dry-run should not send the request")

    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", fail_post)

    runner = CliRunner()
    request_body = {"applicant": {"email": "jane@example.com"}}
    result = runner.invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "applications",
            "create",
            "--json",
            json.dumps(request_body),
            "--dry_run",
        ],
    )

    assert result.exit_code == 0
    assert "DRY RUN:" not in result.output
    payload = json.loads(result.output)
    assert payload == {
        "schema_version": "1.0",
        "data": [
            {
                "dry_run": True,
                "method": "POST",
                "url": f"{base_url('sandbox')}/developer/v1/applications",
                "body": request_body,
            }
        ],
        "pagination": {"has_more": False, "next": None},
    }


# ── Schema subcommand tests ──


class _FakeResponse:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _patch_httpx_get(monkeypatch, spec=None, exc=None):
    """Monkeypatch httpx.get to return a fake spec or raise."""

    def fake_get(url, **kwargs):
        if exc:
            raise exc
        return _FakeResponse(spec or _FAKE_SPEC)

    monkeypatch.setattr("httpx.get", fake_get)


def test_applications_schema__prints_resolved_schema(monkeypatch):

    _patch_httpx_get(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(cli, ["--human", "applications", "schema"])

    assert result.exit_code == 0
    schema = json.loads(result.output)
    assert schema["type"] == "object"
    assert schema["description"] == "Create a new application"
    # $ref should be resolved inline
    assert "$ref" not in json.dumps(schema)
    # Nested address should be resolved
    assert (
        "city"
        in schema["properties"]["applicant"]["properties"]["address"]["properties"]
    )
    # Array items should be resolved
    assert (
        "first_name" in schema["properties"]["beneficial_owners"]["items"]["properties"]
    )
    # Top-level example should be stripped
    assert "example" not in schema


def test_applications_schema__agent_mode_wraps_in_envelope(monkeypatch):

    _patch_httpx_get(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(cli, ["--agent", "applications", "schema"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "1.0"
    assert payload["pagination"] == {"has_more": False, "next": None}
    data = payload["data"][0]
    assert data["type"] == "object"
    assert "$ref" not in json.dumps(data)


def test_applications_schema__handles_fetch_failure(monkeypatch):

    _patch_httpx_get(monkeypatch, exc=ConnectionError("network down"))

    runner = CliRunner()
    result = runner.invoke(cli, ["--human", "applications", "schema"])

    assert result.exit_code == 1
    assert "Failed to fetch schema" in result.output


def test_merge_all_of__deep_merges_properties():
    """allOf with overlapping properties dicts should union, not overwrite."""
    schema = {
        "allOf": [
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "required": ["a"],
            },
            {
                "type": "object",
                "properties": {"b": {"type": "integer"}},
                "required": ["b"],
            },
        ],
    }
    resolved = _merge_all_of(schema)
    assert "a" in resolved["properties"]
    assert "b" in resolved["properties"]
    assert resolved["required"] == ["a", "b"]
