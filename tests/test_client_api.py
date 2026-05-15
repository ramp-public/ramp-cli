"""Tests for API client auth behavior."""

from __future__ import annotations

import pytest

from ramp_cli.auth.store import TokenState
from ramp_cli.client.api import (
    RampClient,
    infer_client_name,
    user_agent_string,
)
from ramp_cli.errors import (
    AuthRequiredError,
    EnvironmentAuthRequiredError,
    RefreshFailedError,
)


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


def test_request__sends_extra_auth_header(monkeypatch):
    client = RampClient("sandbox")
    captured = {}

    class FakeHTTP:
        def request(self, method, url, headers, content=None):
            captured["headers"] = headers
            return b"ok"

    monkeypatch.setattr(
        "ramp_cli.client.api.extra_auth_headers",
        lambda env: {"X-Extra-Auth": f"{env}-token"},
    )

    result = client._request(FakeHTTP(), "GET", "https://example.test", "access")

    assert result == b"ok"
    assert captured["headers"]["X-Extra-Auth"] == "sandbox-token"


def test_request__user_agent_includes_client_comment_when_sentinel_set(monkeypatch):
    client = RampClient("sandbox")
    captured = {}

    class FakeHTTP:
        def request(self, method, url, headers, content=None):
            captured["headers"] = headers
            return b"ok"

    monkeypatch.setenv("RAMP_CLIENT_NAME", "test-harness/1.0")

    result = client._request(FakeHTTP(), "GET", "https://example.test", "access")

    assert result == b"ok"
    ua = captured["headers"]["User-Agent"]
    assert ua.startswith("ramp-cli/")
    assert ua.endswith("(test-harness/1.0)")


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        pytest.param({}, None, id="empty"),
        pytest.param({"RAMP_CLIENT_NAME": "wrapper/9"}, "wrapper/9", id="override"),
        pytest.param({"CLAUDECODE": "1"}, "claude-code", id="claudecode_sentinel"),
        pytest.param({"OPENCODE": "1"}, "opencode", id="opencode_sentinel"),
        pytest.param({"CODEX_SANDBOX": "1"}, "codex", id="codex_sandbox_sentinel"),
        pytest.param(
            {"RAMP_CLIENT_NAME": "wrapper/9", "CLAUDECODE": "1"},
            "wrapper/9",
            id="override_beats_sentinel",
        ),
        # False positives the previous prefix-matching / terminal heuristics produced.
        pytest.param({"TERM_PROGRAM": "iTerm.app"}, None, id="terminal_is_not_agent"),
        pytest.param({"SHELL": "/bin/zsh"}, None, id="shell_is_not_agent"),
        pytest.param(
            {"CODEX_REVIEW": "1"}, None, id="unrelated_codex_prefix_is_not_agent"
        ),
        pytest.param(
            {"VSCODE_PID": "12345"}, None, id="unrelated_vscode_prefix_is_not_agent"
        ),
    ],
)
def test_infer_client_name(env, expected):
    assert infer_client_name(env) == expected


def test_infer_client_name__sanitizes_unsafe_chars():
    # CRLF and spaces are replaced so they can't smuggle header content.
    assert (
        infer_client_name({"RAMP_CLIENT_NAME": "bad\r\nclient name"})
        == "bad-client-name"
    )


def test_user_agent_string__no_client_comment_when_unknown():
    assert user_agent_string({}).startswith("ramp-cli/")
    assert "(" not in user_agent_string({})


def test_user_agent_string__includes_client_comment_when_known():
    ua = user_agent_string({"CLAUDECODE": "1"})
    assert ua.startswith("ramp-cli/")
    assert ua.endswith("(claude-code)")


def test_request__requires_environment_auth(monkeypatch):
    client = RampClient("sandbox")

    monkeypatch.setattr(
        "ramp_cli.client.api.missing_required_environment_auth", lambda env: True
    )
    monkeypatch.setattr(
        "ramp_cli.client.api.environment_auth_required_message",
        lambda env: f"{env} requires extra auth",
    )

    with pytest.raises(EnvironmentAuthRequiredError) as exc_info:
        client._do_request("GET", "https://example.test/developer/v1/users/me")

    assert "sandbox requires extra auth" in str(exc_info.value)
