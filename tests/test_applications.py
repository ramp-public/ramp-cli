"""Tests for the applications command."""

from __future__ import annotations

import json
import sys

import click
import httpx
import pytest
from click.testing import CliRunner

from ramp_cli.auth import store
from ramp_cli.auth.oauth import TokenResponse
from ramp_cli.commands.applications import (
    APPLICATION_CREATED_MESSAGE,
    APPLICATION_EXAMPLE,
    _merge_all_of,
    _render_handoff_result,
)
from ramp_cli.config.constants import application_signup_token, base_url
from ramp_cli.main import cli, main
from ramp_cli.version_check import _write_cache, emit_update_notice

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
                    "code_challenge": {"type": "string"},
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


class _FakePkceCallback:
    redirect_uri = "http://localhost:19817/callback"
    state = "state-123"
    code_challenge = "challenge-123"

    def __init__(self, code: str = "code-123") -> None:
        self.code = code
        self.shutdown_called = False

    def wait_for_code(self, timeout: int, *, timeout_message: str) -> str:
        return self.code

    def shutdown(self) -> None:
        self.shutdown_called = True


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


def test_applications_create__access_token_env_overrides_dev_console_token(monkeypatch):

    captured_token: dict[str, object] = {}

    original_init = __import__(
        "ramp_cli.client.api", fromlist=["RampClient"]
    ).RampClient.__init__

    def spy_init(self, env, access_token=None):
        captured_token["access_token"] = access_token
        original_init(self, env, access_token=access_token)

    monkeypatch.setenv("RAMP_ACCESS_TOKEN", "dev-deploy-token")
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
    assert captured_token["access_token"] == "dev-deploy-token"


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


def test_applications_create__wait_for_auth_injects_pkce_and_saves_tokens(
    isolated_config, monkeypatch
):
    captured: dict[str, object] = {}
    callback = _FakePkceCallback()

    def fake_post(self, path: str, json_body: bytes) -> bytes:
        captured["path"] = path
        captured["body"] = json.loads(json_body)
        return b'{"ignored":"response"}'

    def fake_exchange(env, callback_arg, code):
        captured["exchange"] = (env, callback_arg, code)
        return TokenResponse(
            access_token="access-token",
            refresh_token="refresh-token",
            expires_in=300,
            refresh_token_expires_in=86400,
            scope="applications:read applications:write",
            agent_key_uuid="agent-key-uuid",
        )

    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", fake_post)
    monkeypatch.setattr(
        "ramp_cli.commands.applications.start_pkce_callback", lambda: callback
    )
    monkeypatch.setattr(
        "ramp_cli.commands.applications.exchange_pkce_callback_code", fake_exchange
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
            "--wait_for_auth",
        ],
    )

    assert result.exit_code == 0
    assert captured["path"] == "/developer/v1/applications"
    assert captured["body"] == {
        "applicant": {"email": "jane@example.com"},
        "oauth_authorize_params": {
            "redirect_uri": "http://localhost:19817/callback",
            "state": "state-123",
            "code_challenge": "challenge-123",
        },
    }
    assert captured["exchange"] == ("sandbox", callback, "code-123")
    assert callback.shutdown_called is True
    assert store.get_tokens("sandbox") == ("access-token", "refresh-token")
    assert store.get_agent_key_uuid("sandbox") == "agent-key-uuid"
    payload = json.loads(result.output)
    assert payload["data"][0]["authenticated"] is True
    assert payload["data"][0]["fallback_command"] is None


def test_applications_create__wait_for_auth_overrides_user_oauth_params(
    isolated_config, monkeypatch
):
    captured: dict[str, object] = {}
    callback = _FakePkceCallback()

    def fake_post(self, path: str, json_body: bytes) -> bytes:
        captured["body"] = json.loads(json_body)
        return b"{}"

    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", fake_post)
    monkeypatch.setattr(
        "ramp_cli.commands.applications.start_pkce_callback", lambda: callback
    )
    monkeypatch.setattr(
        "ramp_cli.commands.applications.exchange_pkce_callback_code",
        lambda env, callback_arg, code: TokenResponse(access_token="access"),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--env",
            "sandbox",
            "applications",
            "create",
            "--json",
            json.dumps(
                {
                    "applicant": {"email": "jane@example.com"},
                    "oauth_authorize_params": {
                        "redirect_uri": "https://old.example/callback",
                        "state": "old-state",
                    },
                }
            ),
            "--wait_for_auth",
        ],
    )

    assert result.exit_code == 0
    assert captured["body"]["oauth_authorize_params"] == {
        "redirect_uri": "http://localhost:19817/callback",
        "state": "state-123",
        "code_challenge": "challenge-123",
    }


def test_applications_create__wait_for_auth_timeout_returns_fallback(
    monkeypatch,
):
    class TimeoutCallback(_FakePkceCallback):
        def wait_for_code(self, timeout: int, *, timeout_message: str) -> str:
            raise click.ClickException(timeout_message)

    callback = TimeoutCallback()

    monkeypatch.setattr(
        "ramp_cli.client.api.RampClient.post",
        lambda self, path, json_body: b"{}",
    )
    monkeypatch.setattr(
        "ramp_cli.commands.applications.start_pkce_callback", lambda: callback
    )

    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "applications",
            "create",
            "--json",
            '{"applicant":{"email":"jane@example.com"}}',
            "--wait_for_auth",
            "--auth_timeout",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert callback.shutdown_called is True
    payload = json.loads(result.output)
    data = payload["data"][0]
    assert data["authenticated"] is False
    assert data["auth_error"] == "OAuth redirect timed out after 1 seconds"
    assert data["fallback_command"] == (
        "ramp --env sandbox auth login --auth-level business "
        "--scope applications:read --scope applications:write "
        "--scope bank_accounts:read "
        "--scope incorporation:read --scope incorporation:write"
    )


def test_applications_create__wait_for_auth_interrupt_returns_invite_link(
    monkeypatch,
):
    class InterruptedCallback(_FakePkceCallback):
        def wait_for_code(self, timeout: int, *, timeout_message: str) -> str:
            raise KeyboardInterrupt

    callback = InterruptedCallback()

    monkeypatch.setattr(
        "ramp_cli.client.api.RampClient.post",
        lambda self, path, json_body: json.dumps(
            {"invite_link": "https://app.ramp.com/invite/test-token"}
        ).encode(),
    )
    monkeypatch.setattr(
        "ramp_cli.commands.applications.start_pkce_callback", lambda: callback
    )

    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "applications",
            "create",
            "--json",
            '{"applicant":{"email":"jane@example.com"}}',
            "--wait_for_auth",
        ],
    )

    emit_update_notice(agent_mode=True)

    assert result.exit_code == 130
    assert callback.shutdown_called is True
    payload = json.loads(result.output)
    data = payload["data"][0]
    assert data["authenticated"] is False
    assert data["interrupted"] is True
    assert data["auth_error"] == "Interrupted by user"
    assert data["invite_link"] == "https://app.ramp.com/invite/test-token"
    assert data["fallback_command"].startswith("ramp --env sandbox auth login")


def test_applications_create__main_interrupt_exits_130_with_invite_link(
    monkeypatch, capsys
):
    class InterruptedCallback(_FakePkceCallback):
        def wait_for_code(self, timeout: int, *, timeout_message: str) -> str:
            raise KeyboardInterrupt

    callback = InterruptedCallback()

    monkeypatch.setattr("ramp_cli.main.check_for_update", lambda: None)
    _write_cache("999.0.0")
    monkeypatch.setattr(
        "ramp_cli.client.api.RampClient.post",
        lambda self, path, json_body: json.dumps(
            {"invite_link": "https://app.ramp.com/invite/test-token"}
        ).encode(),
    )
    monkeypatch.setattr(
        "ramp_cli.commands.applications.start_pkce_callback", lambda: callback
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ramp",
            "--agent",
            "--env",
            "sandbox",
            "applications",
            "create",
            "--json",
            '{"applicant":{"email":"jane@example.com"}}',
            "--wait_for_auth",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    data = payload["data"][0]
    assert exc_info.value.code == 130
    assert callback.shutdown_called is True
    assert "update_available" not in payload
    assert captured.err == ""
    assert data["interrupted"] is True
    assert data["invite_link"] == "https://app.ramp.com/invite/test-token"


def test_applications_create__interrupted_handoff_falls_back_to_stderr_on_broken_pipe(
    monkeypatch, capsys
):
    redirected_stdout = False

    def raise_broken_pipe(*args, **kwargs):
        raise BrokenPipeError

    def redirect_stdout_to_devnull():
        nonlocal redirected_stdout
        redirected_stdout = True

    monkeypatch.setattr(
        "ramp_cli.commands.applications.print_agent_json", raise_broken_pipe
    )
    monkeypatch.setattr(
        "ramp_cli.commands.applications._redirect_stdout_to_devnull",
        redirect_stdout_to_devnull,
    )

    _render_handoff_result(
        "sandbox",
        False,
        "Interrupted by user",
        "json",
        "table",
        invite_link="https://app.ramp.com/invite/test-token",
        interrupted=True,
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    data = payload["data"][0]
    assert data["interrupted"] is True
    assert data["invite_link"] == "https://app.ramp.com/invite/test-token"
    assert redirected_stdout is True


def test_applications_create__wait_for_auth_exchange_error_returns_fallback(
    monkeypatch,
):
    callback = _FakePkceCallback()

    def fail_exchange(env, callback_arg, code):
        raise httpx.ConnectError("token endpoint unavailable")

    monkeypatch.setattr(
        "ramp_cli.client.api.RampClient.post",
        lambda self, path, json_body: b"{}",
    )
    monkeypatch.setattr(
        "ramp_cli.commands.applications.start_pkce_callback", lambda: callback
    )
    monkeypatch.setattr(
        "ramp_cli.commands.applications.exchange_pkce_callback_code",
        fail_exchange,
    )

    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "applications",
            "create",
            "--json",
            '{"applicant":{"email":"jane@example.com"}}',
            "--wait_for_auth",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)["data"][0]
    assert data["authenticated"] is False
    assert data["auth_error"] == "token endpoint unavailable"
    assert data["fallback_command"].startswith("ramp --env sandbox auth login")


def test_applications_create__wait_for_auth_persistence_error_returns_fallback(
    monkeypatch,
):
    callback = _FakePkceCallback()

    def fail_save_tokens(*args, **kwargs):
        raise OSError("could not write credentials")

    monkeypatch.setattr(
        "ramp_cli.client.api.RampClient.post",
        lambda self, path, json_body: b"{}",
    )
    monkeypatch.setattr(
        "ramp_cli.commands.applications.start_pkce_callback", lambda: callback
    )
    monkeypatch.setattr(
        "ramp_cli.commands.applications.exchange_pkce_callback_code",
        lambda env, callback_arg, code: TokenResponse(
            access_token="access-token",
            refresh_token="refresh-token",
        ),
    )
    monkeypatch.setattr(store, "save_tokens", fail_save_tokens)

    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "applications",
            "create",
            "--json",
            '{"applicant":{"email":"jane@example.com"}}',
            "--wait_for_auth",
        ],
    )

    assert result.exit_code == 0
    assert callback.shutdown_called is True
    data = json.loads(result.output)["data"][0]
    assert data["authenticated"] is False
    assert data["auth_error"] == "could not write credentials"
    assert data["fallback_command"].startswith("ramp --env sandbox auth login")


def test_applications_create__dry_run_rejects_auth_wait_without_listener(
    monkeypatch,
):
    def fail_start():
        raise AssertionError("dry-run must not open a callback listener")

    monkeypatch.setattr(
        "ramp_cli.commands.applications.start_pkce_callback", fail_start
    )

    result = CliRunner().invoke(
        cli,
        [
            "--env",
            "sandbox",
            "applications",
            "create",
            "--json",
            '{"applicant":{"email":"jane@example.com"}}',
            "--dry_run",
            "--wait_for_auth",
        ],
    )

    assert result.exit_code == 2
    assert "--wait_for_auth cannot be used with --dry_run" in result.output


def test_applications_create__wait_for_auth_rejects_non_object_oauth_params(
    monkeypatch,
):
    callback = _FakePkceCallback()

    def fail_post(self, path: str, json_body: bytes) -> bytes:
        raise AssertionError("invalid body should not be sent")

    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", fail_post)
    monkeypatch.setattr(
        "ramp_cli.commands.applications.start_pkce_callback", lambda: callback
    )

    result = CliRunner().invoke(
        cli,
        [
            "--env",
            "sandbox",
            "applications",
            "create",
            "--json",
            '{"applicant":{"email":"jane@example.com"},"oauth_authorize_params":[]}',
            "--wait_for_auth",
        ],
    )

    assert result.exit_code == 2
    assert "oauth_authorize_params must be a JSON object" in result.output
    assert callback.shutdown_called is True


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
