"""Tests for API client auth behavior."""

from __future__ import annotations

import pytest

from ramp_cli import __version__ as VERSION
from ramp_cli.auth.store import TokenState
from ramp_cli.client.api import (
    RampClient,
    infer_client_name,
    user_agent_string,
)
from ramp_cli.client.headers import agent_headers
from ramp_cli.errors import (
    EXIT_AUTH_REQUIRED,
    AuthRequiredError,
    EnvironmentAuthRequiredError,
    RefreshFailedError,
    UnsafeRequestUrlError,
)


@pytest.fixture(autouse=True)
def clear_agent_client_env(monkeypatch):
    for key in ("CLAUDECODE", "OPENCODE", "CODEX_SANDBOX", "RAMP_CLIENT_NAME"):
        monkeypatch.delenv(key, raising=False)


def test_get_access_token__refreshes_when_only_refresh_token_exists(monkeypatch):
    client = RampClient("sandbox")

    monkeypatch.setattr(
        "ramp_cli.client.api.store.get_token_state",
        lambda env: TokenState(refresh_token="refresh-only"),
    )
    monkeypatch.setattr("ramp_cli.client.api.try_refresh", lambda env: "access-new")

    assert client._get_access_token() == "access-new"


def test_get_access_token__raises_auth_required_without_local_credentials(monkeypatch):
    client = RampClient("sandbox")
    monkeypatch.setattr(
        "ramp_cli.client.api.store.get_token_state",
        lambda env: TokenState(),
    )

    with pytest.raises(AuthRequiredError) as exc_info:
        client._get_access_token()

    assert exc_info.value.code == EXIT_AUTH_REQUIRED
    assert "ramp --env sandbox auth login" in str(exc_info.value)


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


def test_request__sends_operation_headers(monkeypatch):
    client = RampClient("sandbox")
    captured = {}

    class FakeHTTP:
        def request(self, method, url, headers, content=None):
            captured["headers"] = headers
            return b"ok"

    result = client._request(
        FakeHTTP(),
        "POST",
        "https://example.test",
        "access",
        request_headers={"X-Idempotency-Key": "idem-123"},
    )

    assert result == b"ok"
    assert captured["headers"]["X-Idempotency-Key"] == "idem-123"


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


def test_request__sends_typed_agent_headers(monkeypatch):
    client = RampClient("sandbox")
    captured = {}

    class FakeHTTP:
        def request(self, method, url, headers, content=None):
            captured["headers"] = headers
            return b"ok"

    monkeypatch.setenv("CODEX_SANDBOX", "1")
    monkeypatch.setattr("ramp_cli.client.headers.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "ramp_cli.client.headers.platform.mac_ver",
        lambda: ("15.5", ("", "", ""), ""),
    )
    monkeypatch.setattr("ramp_cli.client.headers.platform.machine", lambda: "arm64")

    client._request(FakeHTTP(), "GET", "https://example.test", "access")

    headers = captured["headers"]
    assert headers["X-Ramp-Agent-Runtime-Name"] == "ramp-cli"
    assert headers["X-Ramp-Agent-Runtime-Version"] == VERSION
    assert headers["X-Ramp-Agent-Harness-Name"] == "codex"
    assert "X-Ramp-Agent-Harness-Version" not in headers
    assert headers["X-Ramp-Agent-Device-OS"] == "macos"
    assert headers["X-Ramp-Agent-Device-OS-Version"] == "15.5"
    assert headers["X-Ramp-Agent-Device-Architecture"] == "arm64"
    assert headers["X-Ramp-Agent-Device-Type"] == "desktop"
    assert headers["X-External-Session-Id"]


def test_request__does_not_treat_legacy_client_override_as_typed_harness(
    monkeypatch,
):
    client = RampClient("sandbox")
    captured = {}

    class FakeHTTP:
        def request(self, method, url, headers, content=None):
            captured["headers"] = headers
            return b"ok"

    monkeypatch.setenv("RAMP_CLIENT_NAME", "wrapper/9")

    client._request(FakeHTTP(), "GET", "https://example.test", "access")

    assert captured["headers"]["User-Agent"].endswith("(wrapper/9)")
    assert "X-Ramp-Agent-Harness-Name" not in captured["headers"]


def test_agent_headers__omit_unknown_optional_metadata(monkeypatch):
    monkeypatch.setattr("ramp_cli.client.headers.platform.system", lambda: "Other")
    monkeypatch.setattr("ramp_cli.client.headers.platform.machine", lambda: "Other")

    headers = agent_headers(None)

    assert headers["X-Ramp-Agent-Device-Type"] == "unknown"
    assert "X-Ramp-Agent-Harness-Name" not in headers
    assert "X-Ramp-Agent-Harness-Version" not in headers
    assert "X-Ramp-Agent-Device-OS" not in headers
    assert "X-Ramp-Agent-Device-OS-Version" not in headers
    assert "X-Ramp-Agent-Device-Architecture" not in headers


def test_agent_headers__linux_device_type_is_unknown(monkeypatch):
    monkeypatch.setattr("ramp_cli.client.headers.platform.system", lambda: "Linux")
    monkeypatch.setattr("ramp_cli.client.headers.platform.release", lambda: "6.8.0")
    monkeypatch.setattr("ramp_cli.client.headers.platform.machine", lambda: "x86_64")

    headers = agent_headers(None)

    assert headers["X-Ramp-Agent-Device-Type"] == "unknown"
    assert headers["X-Ramp-Agent-Device-OS"] == "linux"
    assert headers["X-Ramp-Agent-Device-Architecture"] == "amd64"


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


def test_get_url__allows_active_api_origin(monkeypatch):
    client = RampClient("production")
    captured = {}

    def fake_request(method, url, body=None):
        captured.update(method=method, url=url)
        return b"ok"

    monkeypatch.setattr(client, "_do_request", fake_request)

    assert (
        client.get_url("https://api.ramp.com:443/developer/v1/things?start=2") == b"ok"
    )
    assert captured == {
        "method": "GET",
        "url": "https://api.ramp.com:443/developer/v1/things?start=2",
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/developer/v1/things?start=2",
        "http://api.ramp.com/developer/v1/things?start=2",
        "https://user@api.ramp.com/developer/v1/things?start=2",
    ],
)
def test_get_url__rejects_untrusted_origin(url):
    client = RampClient("production")

    with pytest.raises(UnsafeRequestUrlError):
        client.get_url(url)
