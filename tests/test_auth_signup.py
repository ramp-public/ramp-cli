"""Tests for `ramp auth signup`."""

from __future__ import annotations

import json

import httpx
import pytest
from click.testing import CliRunner

from ramp_cli.auth import store
from ramp_cli.commands import auth as auth_command_module
from ramp_cli.errors import ApiError, RampCLIError
from ramp_cli.main import cli


@pytest.fixture
def captured(monkeypatch):
    calls: list[httpx.Request] = []
    responses: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses.pop(0)

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(auth_command_module.httpx, "Client", fake_client)
    return calls, responses


def _invoke(args):
    return CliRunner().invoke(cli, args, catch_exceptions=False)


def test_signup_agent_mode_returns_next_steps(captured):
    calls, responses = captured
    store.save_tokens("sandbox", "stored-access-token", "stored-refresh-token")
    responses.append(
        httpx.Response(202, json={"email": "dev@acme.com", "status": "submitted"})
    )

    result = _invoke(
        ["--env", "sandbox", "--agent", "auth", "signup", "--email", "dev@acme.com"]
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"][0]
    assert data["email"] == "dev@acme.com"
    assert data["status"] == "submitted"
    assert data["environment"] == "sandbox"
    steps = [step["step"] for step in data["next_steps"]]
    assert steps == ["accept_invite", "login"]
    assert data["next_steps"][1]["command"] == "ramp --env sandbox auth login"
    assert "dev@acme.com" in data["next_steps"][0]["description"]

    (request,) = calls
    assert request.method == "POST"
    assert str(request.url) == (
        "https://demo-api.ramp.com/v1/public/developer-experience/signup"
    )
    assert json.loads(request.content) == {"email": "dev@acme.com"}
    assert "authorization" not in request.headers


def test_signup_human_output_lists_next_steps(captured):
    _, responses = captured
    responses.append(
        httpx.Response(202, json={"email": "dev@acme.com", "status": "submitted"})
    )

    result = _invoke(
        ["--env", "sandbox", "--human", "auth", "signup", "--email", "dev@acme.com"]
    )

    assert result.exit_code == 0, result.output
    assert "Signup submitted for dev@acme.com" in result.output
    assert "Check dev@acme.com for the Ramp Developer Console invite" in result.output
    assert "ramp --env sandbox auth login" in result.output


def test_signup_surfaces_server_rejection(captured):
    _, responses = captured
    responses.append(
        httpx.Response(
            400,
            json={
                "error_v2": {
                    "error_code": "DEVELOPER_EXPERIENCE_7009",
                    "message": "Please use a work email address.",
                }
            },
        )
    )

    with pytest.raises(ApiError) as exc_info:
        CliRunner().invoke(
            cli,
            ["--env", "sandbox", "auth", "signup", "--email", "someone@gmail.com"],
            catch_exceptions=False,
            standalone_mode=False,
        )

    assert exc_info.value.status_code == 400
    assert "Please use a work email address." in exc_info.value.detail


def test_signup_sends_organization_name(captured):
    calls, responses = captured
    responses.append(
        httpx.Response(202, json={"email": "dev@acme.com", "status": "submitted"})
    )

    result = _invoke(
        [
            "--env",
            "sandbox",
            "--agent",
            "auth",
            "signup",
            "--email",
            "dev@acme.com",
            "--org",
            "Acme",
        ]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(calls[0].content) == {
        "email": "dev@acme.com",
        "organization_name": "Acme",
    }


@pytest.mark.parametrize(
    ("status_code", "body", "message"),
    [
        (
            403,
            {"error_v2": {"error_code": "4013", "message": "Forbidden"}},
            "Developer Console signup isn't available yet.",
        ),
        (429, {}, "Too many signup attempts. Try again in an hour."),
    ],
)
def test_signup_friendly_errors(captured, status_code, body, message):
    _, responses = captured
    responses.append(httpx.Response(status_code, json=body))

    with pytest.raises(RampCLIError, match=message):
        CliRunner().invoke(
            cli,
            ["--env", "sandbox", "auth", "signup", "--email", "dev@acme.com"],
            catch_exceptions=False,
            standalone_mode=False,
        )


@pytest.mark.parametrize("email", ["not-an-email", "two@@acme.com"])
def test_signup_rejects_malformed_email_without_request(captured, email):
    calls, _ = captured

    result = CliRunner().invoke(
        cli, ["--env", "sandbox", "--human", "auth", "signup", "--email", email]
    )

    assert result.exit_code == 2
    assert "not a valid email address" in result.output
    assert calls == []


def test_signup_dry_run_sends_nothing(captured):
    calls, _ = captured

    result = _invoke(
        [
            "--env",
            "sandbox",
            "--agent",
            "auth",
            "signup",
            "--email",
            "dev@acme.com",
            "--dry_run",
        ]
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"][0]
    assert data == {
        "dry_run": True,
        "method": "POST",
        "url": "https://demo-api.ramp.com/v1/public/developer-experience/signup",
        "body": {"email": "dev@acme.com"},
    }
    assert calls == []
