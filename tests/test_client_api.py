"""Tests for API client auth behavior."""

from __future__ import annotations

import httpx
import pytest

from ramp_cli import __version__ as VERSION
from ramp_cli.auth.store import TokenState
from ramp_cli.client.api import (
    RampClient,
    infer_client_name,
    user_agent_string,
)
from ramp_cli.client.headers import agent_headers
from ramp_cli.client.transport import AuthenticatedRampTransport
from ramp_cli.errors import (
    EXIT_AUTH_REQUIRED,
    ApiError,
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
    client = AuthenticatedRampTransport("sandbox")

    monkeypatch.setattr(
        "ramp_cli.client.transport.store.get_token_state",
        lambda env: TokenState(refresh_token="refresh-only"),
    )
    monkeypatch.setattr(
        "ramp_cli.client.transport.try_refresh", lambda env: "access-new"
    )

    assert client._get_access_token() == "access-new"


def test_get_access_token__loads_and_refreshes_selected_profile(monkeypatch):
    client = AuthenticatedRampTransport("sandbox", profile="agent")
    observed = {}

    def get_state(env: str, *, profile: str):
        observed["state"] = (env, profile)
        return TokenState(refresh_token="agent-refresh")

    def refresh(env: str, *, profile: str):
        observed["refresh"] = (env, profile)
        return "agent-access"

    monkeypatch.setattr("ramp_cli.client.transport.store.get_token_state", get_state)
    monkeypatch.setattr("ramp_cli.client.transport.try_refresh", refresh)

    assert client._get_access_token() == "agent-access"
    assert observed == {
        "state": ("sandbox", "agent"),
        "refresh": ("sandbox", "agent"),
    }


def test_get_access_token__raises_auth_required_without_local_credentials(monkeypatch):
    client = AuthenticatedRampTransport("sandbox")
    monkeypatch.setattr(
        "ramp_cli.client.transport.store.get_token_state",
        lambda env: TokenState(),
    )

    with pytest.raises(AuthRequiredError) as exc_info:
        client._get_access_token()

    assert exc_info.value.code == EXIT_AUTH_REQUIRED
    assert "ramp --env sandbox auth login" in str(exc_info.value)


def test_get_access_token__agent_error_explains_how_to_reauthenticate(monkeypatch):
    client = AuthenticatedRampTransport("sandbox", profile="agent")
    monkeypatch.setattr(
        "ramp_cli.client.transport.store.get_token_state",
        lambda env, *, profile: TokenState(),
    )

    with pytest.raises(AuthRequiredError) as exc_info:
        client._get_access_token()

    assert "ramp --env sandbox agent login --client-id <agent-client-id>" in str(
        exc_info.value
    )


def test_get_access_token__raises_when_refresh_fails(monkeypatch):
    client = AuthenticatedRampTransport("sandbox")

    monkeypatch.setattr(
        "ramp_cli.client.transport.store.get_token_state",
        lambda env: TokenState(refresh_token="refresh-only"),
    )
    monkeypatch.setattr("ramp_cli.client.transport.try_refresh", lambda env: None)

    with pytest.raises(AuthRequiredError):
        client._get_access_token()


def test_get_access_token__expired_access_only_token_does_not_refresh(monkeypatch):
    client = AuthenticatedRampTransport("sandbox")
    monkeypatch.setattr(
        "ramp_cli.client.transport.store.get_token_state",
        lambda env: TokenState(
            access_token="standalone-access",
            access_token_issued_at=100,
            access_token_expires_in=300,
        ),
    )
    monkeypatch.setattr("ramp_cli.client.transport.time.time", lambda: 500)
    monkeypatch.setattr(
        "ramp_cli.client.transport.try_refresh",
        lambda env: pytest.fail("access-only credentials cannot be refreshed"),
    )

    with pytest.raises(AuthRequiredError):
        client._get_access_token()


def test_get_access_token__refreshes_proactively_when_expiring_soon(monkeypatch):
    client = AuthenticatedRampTransport("sandbox")

    monkeypatch.setattr(
        "ramp_cli.client.transport.store.get_token_state",
        lambda env: TokenState(
            access_token="access-old",
            refresh_token="refresh-old",
            access_token_issued_at=100,
            access_token_expires_in=300,
        ),
    )
    monkeypatch.setattr("ramp_cli.client.transport.time.time", lambda: 380)
    monkeypatch.setattr(
        "ramp_cli.client.transport.try_refresh", lambda env: "access-new"
    )

    assert client._get_access_token() == "access-new"


def test_get_access_token__uses_current_token_when_proactive_refresh_fails(
    monkeypatch,
):
    client = AuthenticatedRampTransport("sandbox")

    monkeypatch.setattr(
        "ramp_cli.client.transport.store.get_token_state",
        lambda env: TokenState(
            access_token="access-old",
            refresh_token="refresh-old",
            access_token_issued_at=100,
            access_token_expires_in=300,
        ),
    )
    monkeypatch.setattr("ramp_cli.client.transport.time.time", lambda: 380)

    def fail_refresh(env: str) -> str:
        raise RefreshFailedError("temporarily unavailable")

    monkeypatch.setattr("ramp_cli.client.transport.try_refresh", fail_refresh)

    assert client._get_access_token() == "access-old"


def test_request__sends_extra_auth_header(monkeypatch):
    client = AuthenticatedRampTransport("sandbox")
    captured = {}

    class FakeHTTP:
        def request(self, method, url, headers, content=None):
            captured["headers"] = headers
            return b"ok"

    monkeypatch.setattr(
        "ramp_cli.client.transport.extra_auth_headers",
        lambda env: {"X-Extra-Auth": f"{env}-token"},
    )

    result = client._request(FakeHTTP(), "GET", "https://example.test", "access")

    assert result == b"ok"
    assert captured["headers"]["X-Extra-Auth"] == "sandbox-token"


def test_request__sends_operation_headers(monkeypatch):
    client = AuthenticatedRampTransport("sandbox")
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
    client = AuthenticatedRampTransport("sandbox")
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
    client = AuthenticatedRampTransport("sandbox")
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
    client = AuthenticatedRampTransport("sandbox")
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
    client = AuthenticatedRampTransport("sandbox")

    monkeypatch.setattr(
        "ramp_cli.client.transport.missing_required_environment_auth",
        lambda env: True,
    )
    monkeypatch.setattr(
        "ramp_cli.client.transport.environment_auth_required_message",
        lambda env: f"{env} requires extra auth",
    )

    with pytest.raises(EnvironmentAuthRequiredError) as exc_info:
        client.request("GET", "https://example.test/developer/v1/users/me")

    assert "sandbox requires extra auth" in str(exc_info.value)


def test_request__refreshes_and_replays_after_401(monkeypatch):
    client = AuthenticatedRampTransport("sandbox")
    attempted_tokens = []

    def handler(request):
        attempted_tokens.append(request.headers["Authorization"])
        status_code = 401 if len(attempted_tokens) == 1 else 200
        return httpx.Response(status_code, content=b"ok")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("ramp_cli.client.transport.httpx.Client", lambda **kwargs: http)
    monkeypatch.setattr(
        "ramp_cli.client.transport.store.get_token_state",
        lambda env: TokenState(access_token="access-old"),
    )
    monkeypatch.setattr(
        "ramp_cli.client.transport.try_refresh", lambda env: "access-new"
    )

    assert client.request("GET", "https://example.test/things") == b"ok"
    assert attempted_tokens == ["Bearer access-old", "Bearer access-new"]


def test_request__proxied_401_recovers_via_refresh_then_retry(monkeypatch):
    # A Core bearer that expires just before Core validates it is forwarded back
    # through the proxy as a 401; the normal refresh-and-retry must still recover.
    client = AuthenticatedRampTransport("sandbox")
    attempted_tokens = []

    def handler(request):
        attempted_tokens.append(request.headers["Authorization"])
        status_code = 401 if len(attempted_tokens) == 1 else 200
        return httpx.Response(status_code, content=b"ok")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("ramp_cli.client.transport.httpx.Client", lambda **kwargs: http)
    monkeypatch.setattr(
        "ramp_cli.client.transport.store.get_token_state",
        lambda env: TokenState(access_token="access-old"),
    )
    monkeypatch.setattr(
        "ramp_cli.client.transport.try_refresh", lambda env: "access-new"
    )

    assert (
        client.request("POST", "https://proxy.example.com/creds", proxied=True) == b"ok"
    )
    assert attempted_tokens == ["Bearer access-old", "Bearer access-new"]


def test_request__proxied_401_surfaces_api_error_after_failed_recovery(monkeypatch):
    # A residual 401 (e.g. a bad proxy-auth key) must surface the actual response
    # body as ApiError rather than the misleading `ramp auth login` path.
    client = AuthenticatedRampTransport("sandbox")
    attempted_tokens = []

    def handler(request):
        attempted_tokens.append(request.headers["Authorization"])
        return httpx.Response(401, content=b"proxy auth rejected")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("ramp_cli.client.transport.httpx.Client", lambda **kwargs: http)
    monkeypatch.setattr(
        "ramp_cli.client.transport.store.get_token_state",
        lambda env: TokenState(access_token="access-old"),
    )
    monkeypatch.setattr(
        "ramp_cli.client.transport.try_refresh", lambda env: "access-new"
    )

    with pytest.raises(ApiError) as exc_info:
        client.request("POST", "https://proxy.example.com/creds", proxied=True)

    assert exc_info.value.status_code == 401
    assert "proxy auth rejected" in exc_info.value.body
    # Refresh-and-retry was attempted before surfacing the proxy failure.
    assert attempted_tokens == ["Bearer access-old", "Bearer access-new"]


def test_request__proxied_401_without_refresh_token_surfaces_api_error(monkeypatch):
    # No refresh token available: a proxied 401 must still surface the response
    # body via ApiError rather than AuthRequiredError.
    client = AuthenticatedRampTransport("sandbox")

    def handler(request):
        return httpx.Response(401, content=b"proxy auth rejected")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("ramp_cli.client.transport.httpx.Client", lambda **kwargs: http)
    monkeypatch.setattr(
        "ramp_cli.client.transport.store.get_token_state",
        lambda env: TokenState(access_token="access-old"),
    )
    monkeypatch.setattr("ramp_cli.client.transport.try_refresh", lambda env: None)

    with pytest.raises(ApiError) as exc_info:
        client.request("POST", "https://proxy.example.com/creds", proxied=True)

    assert exc_info.value.status_code == 401
    assert "proxy auth rejected" in exc_info.value.body


def test_request__does_not_refresh_static_token_after_401(monkeypatch):
    client = AuthenticatedRampTransport("sandbox", access_token="access-static")
    attempted_tokens = []

    def handler(request):
        attempted_tokens.append(request.headers["Authorization"])
        return httpx.Response(401)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("ramp_cli.client.transport.httpx.Client", lambda **kwargs: http)

    def fail_if_called(env):
        raise AssertionError("static tokens must not be refreshed")

    monkeypatch.setattr("ramp_cli.client.transport.try_refresh", fail_if_called)

    with pytest.raises(AuthRequiredError):
        client.request("GET", "https://example.test/things")

    assert attempted_tokens == ["Bearer access-static"]


def test_request_multipart__refreshes_and_replays_after_401(monkeypatch):
    client = AuthenticatedRampTransport("sandbox")
    attempted_tokens = []

    def handler(request):
        attempted_tokens.append(request.headers["Authorization"])
        status_code = 401 if len(attempted_tokens) == 1 else 200
        return httpx.Response(status_code, content=b"ok")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("ramp_cli.client.transport.httpx.Client", lambda **kwargs: http)
    monkeypatch.setattr(
        "ramp_cli.client.transport.store.get_token_state",
        lambda env: TokenState(access_token="access-old"),
    )
    monkeypatch.setattr(
        "ramp_cli.client.transport.try_refresh", lambda env: "access-new"
    )

    result = client.request_multipart(
        "POST",
        "https://example.test/uploads",
        data={"purpose": "receipt"},
        files={"file": ("receipt.txt", b"contents")},
    )

    assert result == b"ok"
    assert attempted_tokens == ["Bearer access-old", "Bearer access-new"]


def test_post__resolves_core_base_url_at_request_time(monkeypatch):
    client = RampClient("sandbox")
    captured = {}

    def fake_request(method, url, body=None, request_headers=None):
        captured.update(method=method, url=url, body=body)
        return b"ok"

    monkeypatch.setattr(client._transport, "request", fake_request)
    monkeypatch.setenv("RAMP_API_URL", "https://core.example.test/")

    assert client.post("/developer/v1/things", b"{}") == b"ok"
    assert captured == {
        "method": "POST",
        "url": "https://core.example.test/developer/v1/things",
        "body": b"{}",
    }


def test_post_url__allows_https_proxy_and_forwards_headers(monkeypatch):
    client = RampClient("production")
    captured = {}

    def fake_request(method, url, body=None, request_headers=None, proxied=False):
        captured.update(
            method=method,
            url=url,
            body=body,
            request_headers=request_headers,
            proxied=proxied,
        )
        return b"ok"

    monkeypatch.setattr(client._transport, "request", fake_request)

    assert (
        client.post_url(
            "https://proxy.example.com/developer/v1/agent-tools/x",
            b"{}",
            headers={"BT-API-KEY": "secret"},
        )
        == b"ok"
    )
    assert captured == {
        "method": "POST",
        "url": "https://proxy.example.com/developer/v1/agent-tools/x",
        "body": b"{}",
        "request_headers": {"BT-API-KEY": "secret"},
        "proxied": True,
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://proxy.example.com/developer/v1/agent-tools/x",
        "https://user@proxy.example.com/developer/v1/agent-tools/x",
        "https://proxy.example.com/developer/v1/agent-tools/x#fragment",
    ],
)
def test_post_url__rejects_unsafe_proxy_url(url):
    client = RampClient("production")

    with pytest.raises(UnsafeRequestUrlError):
        client.post_url(url, b"{}")


def test_get_url__allows_active_api_origin(monkeypatch):
    client = RampClient("production")
    captured = {}

    def fake_request(method, url, body=None):
        captured.update(method=method, url=url)
        return b"ok"

    monkeypatch.setattr(client._transport, "request", fake_request)

    assert (
        client.get_url("https://api.ramp.com:443/developer/v1/things?start=2") == b"ok"
    )
    assert captured == {
        "method": "GET",
        "url": "https://api.ramp.com:443/developer/v1/things?start=2",
    }


def test_get_url__forwards_request_headers(monkeypatch):
    client = RampClient("production")
    captured = {}

    def fake_request(method, url, body=None, request_headers=None):
        captured.update(method=method, url=url, request_headers=request_headers)
        return b"ok"

    monkeypatch.setattr(client._transport, "request", fake_request)

    assert (
        client.get_url(
            "https://api.ramp.com/developer/v1/things?start=2",
            headers={"X-Ramp-Agent-Mode": "agent"},
        )
        == b"ok"
    )
    assert captured == {
        "method": "GET",
        "url": "https://api.ramp.com/developer/v1/things?start=2",
        "request_headers": {"X-Ramp-Agent-Mode": "agent"},
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
