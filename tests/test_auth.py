"""Tests for auth token storage and PKCE generation."""

from __future__ import annotations

import json
import sys
import threading
import time
from contextlib import nullcontext
from urllib.parse import parse_qs, urlparse

import click
import httpx
import pytest
from click.testing import CliRunner

from ramp_cli.auth import oauth as oauth_module
from ramp_cli.auth import refresh as refresh_helper
from ramp_cli.auth import store
from ramp_cli.auth.oauth import (
    OAuthTokenError,
    TokenResponse,
    _callback_html,
    _generate_challenge,
    _generate_verifier,
)
from ramp_cli.commands import auth as auth_command_module
from ramp_cli.config import profiles, settings
from ramp_cli.errors import RefreshFailedError
from ramp_cli.main import BoxHelpFormatter, cli, main
from ramp_cli.onboarding import record_first_login


def _clear_agent_client_env(monkeypatch):
    for key in ("CLAUDECODE", "OPENCODE", "CODEX_SANDBOX", "RAMP_CLIENT_NAME"):
        monkeypatch.delenv(key, raising=False)


def test_pkce_verifier_length():
    v = _generate_verifier()
    assert len(v) >= 43  # base64url of 32 bytes


def test_pkce_challenge_deterministic():
    v = _generate_verifier()
    c1 = _generate_challenge(v)
    c2 = _generate_challenge(v)
    assert c1 == c2


def test_pkce_challenge_differs_for_different_verifiers():
    v1 = _generate_verifier()
    v2 = _generate_verifier()
    assert _generate_challenge(v1) != _generate_challenge(v2)


def test_auth_url_uses_explicit_authorization_level():
    url = oauth_module._build_auth_url(
        "sandbox",
        "http://localhost:19817/callback",
        "state",
        "challenge",
        "applications:read",
        auth_level="business",
    )

    assert parse_qs(urlparse(url).query)["auth_level"] == ["business"]


def test_auth_url_includes_allowlisted_agent_client_hint(monkeypatch):
    _clear_agent_client_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")

    url = oauth_module._build_auth_url(
        "sandbox",
        "http://localhost:19817/callback",
        "state",
        "challenge",
        "applications:read",
        auth_level="business",
    )
    query = parse_qs(urlparse(url).query)

    assert query["agent_client_hint"] == ["claude_code"]
    assert query["client_id"] == [oauth_module.client_id("sandbox")]
    assert query["redirect_uri"] == ["http://localhost:19817/callback"]
    assert query["scope"] == ["applications:read"]


def test_auth_url_includes_codex_agent_client_hint(monkeypatch):
    _clear_agent_client_env(monkeypatch)
    monkeypatch.setenv("CODEX_SANDBOX", "1")

    url = oauth_module._build_auth_url(
        "sandbox",
        "http://localhost:19817/callback",
        "state",
        "challenge",
        "applications:read",
    )

    assert parse_qs(urlparse(url).query)["agent_client_hint"] == ["codex"]


def test_auth_url_codex_hint_wins_when_opencode_is_also_present(monkeypatch):
    _clear_agent_client_env(monkeypatch)
    monkeypatch.setenv("OPENCODE", "1")
    monkeypatch.setenv("CODEX_SANDBOX", "1")

    url = oauth_module._build_auth_url(
        "sandbox",
        "http://localhost:19817/callback",
        "state",
        "challenge",
        "applications:read",
    )

    assert parse_qs(urlparse(url).query)["agent_client_hint"] == ["codex"]


def test_auth_url_includes_opencode_agent_client_hint(monkeypatch):
    _clear_agent_client_env(monkeypatch)
    monkeypatch.setenv("OPENCODE", "1")

    url = oauth_module._build_auth_url(
        "sandbox",
        "http://localhost:19817/callback",
        "state",
        "challenge",
        "applications:read",
    )

    assert parse_qs(urlparse(url).query)["agent_client_hint"] == ["opencode"]


def test_auth_url_omits_unknown_or_untrusted_agent_client_hint(monkeypatch):
    _clear_agent_client_env(monkeypatch)
    monkeypatch.setenv("RAMP_CLIENT_NAME", "claude_code")
    monkeypatch.setenv("CODEX_REVIEW", "1")

    url = oauth_module._build_auth_url(
        "sandbox",
        "http://localhost:19817/callback",
        "state",
        "challenge",
        "applications:read",
    )

    assert "agent_client_hint" not in parse_qs(urlparse(url).query)


def test_open_browser_git_bash_uses_win32_branch_with_full_url(monkeypatch):
    """Git Bash runs a native win32 Python, so the win32 branch must handle it.

    The URL must reach the launcher intact — cmd.exe-style `start` would split
    an unquoted authorize URL at every `&`.
    """
    calls = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            calls.append((args, kwargs))

    url = (
        "https://app.ramp.com/v1/authorize?response_type=code"
        "&client_id=abc123&scope=transactions%3Aread&state=xyz"
        "&code_challenge=ccc&code_challenge_method=S256"
    )
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("MSYSTEM", "MINGW64")
    monkeypatch.setattr(oauth_module.subprocess, "Popen", FakePopen)

    assert oauth_module._open_browser(url) is True
    args, _kwargs = calls[0]
    assert args == ["rundll32", "url.dll,FileProtocolHandler", url]
    assert "cmd.exe" not in args


def test_token_save_and_load(isolated_config):
    store.save_tokens("sandbox", "access123", "refresh456")
    access, refresh = store.get_tokens("sandbox")
    assert access == "access123"
    assert refresh == "refresh456"


def test_token_state_stores_expiry_metadata(isolated_config):
    store.save_tokens(
        "sandbox",
        "access123",
        "refresh456",
        access_token_expires_in=300,
        refresh_token_expires_in=604800,
        issued_at=100,
    )

    state = store.get_token_state("sandbox")
    assert state.access_token == "access123"
    assert state.refresh_token == "refresh456"
    assert state.access_token_issued_at == 100
    assert state.access_token_expires_in == 300
    assert state.refresh_token_issued_at == 100
    assert state.refresh_token_expires_in == 604800


def test_token_clear(isolated_config):
    store.save_tokens("sandbox", "access123", "refresh456")
    store.clear_tokens("sandbox")
    access, refresh = store.get_tokens("sandbox")
    assert access == ""
    assert refresh == ""


def test_has_tokens(isolated_config):
    assert store.has_tokens("sandbox") is False
    store.save_tokens("sandbox", "tok", "")
    assert store.has_tokens("sandbox") is True


def test_has_tokens_with_refresh_only(isolated_config):
    store.save_tokens("sandbox", "", "refresh-only")
    assert store.has_tokens("sandbox") is True


def test_is_authenticated_false_when_both_tokens_expired(isolated_config):
    store.save_tokens(
        "sandbox",
        "access123",
        "refresh456",
        access_token_expires_in=10,
        refresh_token_expires_in=20,
        issued_at=100,
    )

    assert store.is_authenticated("sandbox", now=200) is False


def test_is_authenticated_true_with_expired_access_and_valid_refresh(isolated_config):
    store.save_tokens(
        "sandbox",
        "access123",
        "refresh456",
        access_token_expires_in=10,
        refresh_token_expires_in=200,
        issued_at=100,
    )

    assert store.is_authenticated("sandbox", now=150) is True


def test_separate_environments(isolated_config):
    store.save_tokens("sandbox", "sandbox-token", "")
    store.save_tokens("production", "prod-token", "")

    a1, _ = store.get_tokens("sandbox")
    a2, _ = store.get_tokens("production")
    assert a1 == "sandbox-token"
    assert a2 == "prod-token"


class TestAuthLogout:
    def test_logout_not_logged_in(self, isolated_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["auth", "logout"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "No credentials stored" in result.output

    def test_logout_agent_json(self, isolated_config):
        store.save_tokens("sandbox", "tok", "")
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--agent", "--env", "sandbox", "auth", "logout"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["schema_version"] == "1.0"
        assert "Logged out" in data["data"][0]["message"]

    def test_agent_logout_clears_only_selected_agent_environment(self, isolated_config):
        store.save_tokens(
            "sandbox",
            "standalone-access",
            "",
            granted_scopes="transactions:read",
            profile=profiles.AGENT_PROFILE,
        )
        store.save_tokens(
            "production",
            "production-access",
            "",
            profile=profiles.AGENT_PROFILE,
        )
        store.save_tokens(
            "sandbox",
            "human-access",
            "human-refresh",
            profile=profiles.HUMAN_PROFILE,
        )

        result = CliRunner().invoke(
            cli,
            ["--agent", "--env", "sandbox", "agent", "logout"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert not store.has_tokens("sandbox", profile=profiles.AGENT_PROFILE)
        assert not store.get_granted_scopes("sandbox", profile=profiles.AGENT_PROFILE)
        assert store.get_tokens("production", profile=profiles.AGENT_PROFILE) == (
            "production-access",
            "",
        )
        assert store.get_tokens("sandbox", profile=profiles.HUMAN_PROFILE) == (
            "human-access",
            "human-refresh",
        )


def test_status_reports_expired_tokens_as_unauthenticated(isolated_config):
    store.save_tokens(
        "sandbox",
        "access123",
        "refresh456",
        access_token_expires_in=10,
        refresh_token_expires_in=20,
        issued_at=100,
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["--agent", "auth", "status"], catch_exceptions=False)

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["data"][0]["sandbox"]["authenticated"] is False


def test_status_reports_valid_refresh_token_as_authenticated(
    isolated_config, monkeypatch
):
    monkeypatch.setattr(store.time, "time", lambda: 150)
    store.save_tokens(
        "sandbox",
        "",
        "refresh456",
        refresh_token_expires_in=200,
        issued_at=100,
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["--agent", "auth", "status"], catch_exceptions=False)

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["data"][0]["sandbox"]["authenticated"] is True


def test_try_refresh__rotates_refresh_token(isolated_config, monkeypatch):
    store.save_tokens("sandbox", "access-old", "refresh-old")

    def fake_refresh_tokens(env: str, refresh_token: str) -> TokenResponse:
        assert env == "sandbox"
        assert refresh_token == "refresh-old"
        return TokenResponse(access_token="access-new", refresh_token="refresh-new")

    monkeypatch.setattr(refresh_helper, "refresh_tokens", fake_refresh_tokens)

    assert refresh_helper.try_refresh("sandbox") == "access-new"
    assert store.get_tokens("sandbox") == ("access-new", "refresh-new")


@pytest.mark.parametrize(
    "refresh_outcome",
    [
        pytest.param("success", id="successful-stale-refresh"),
        pytest.param("invalid_grant", id="stale-invalid-grant"),
    ],
)
def test_try_refresh__preserves_concurrent_profile_login(
    isolated_config, monkeypatch, refresh_outcome
):
    store.save_tokens("sandbox", "access-old", "refresh-old", profile="human")

    def concurrent_login(env: str, refresh_token: str) -> TokenResponse:
        store.save_tokens("sandbox", "login-access", "login-refresh", profile="human")
        if refresh_outcome == "invalid_grant":
            raise OAuthTokenError("invalid_grant", "refresh token expired")
        return TokenResponse(access_token="stale-access", refresh_token="stale-refresh")

    monkeypatch.setattr(refresh_helper, "refresh_tokens", concurrent_login)

    assert refresh_helper.try_refresh("sandbox", profile="human") == "login-access"
    assert store.get_tokens("sandbox", profile="human") == (
        "login-access",
        "login-refresh",
    )


def test_try_refresh__rotates_only_selected_profile(isolated_config, monkeypatch):
    store.save_tokens("sandbox", "human-access", "human-refresh", profile="human")
    store.save_tokens("sandbox", "agent-access", "agent-refresh", profile="agent")

    def fake_refresh_tokens(env: str, refresh_token: str) -> TokenResponse:
        assert env == "sandbox"
        assert refresh_token == "agent-refresh"
        return TokenResponse(
            access_token="agent-access-new", refresh_token="agent-refresh-new"
        )

    monkeypatch.setattr(refresh_helper, "refresh_tokens", fake_refresh_tokens)

    assert refresh_helper.try_refresh("sandbox", profile="agent") == (
        "agent-access-new"
    )
    assert store.get_tokens("sandbox", profile="agent") == (
        "agent-access-new",
        "agent-refresh-new",
    )
    assert store.get_tokens("sandbox", profile="human") == (
        "human-access",
        "human-refresh",
    )


def test_try_refresh__uses_newly_rotated_tokens_from_other_process(
    isolated_config, monkeypatch
):
    tokens = iter(
        [
            ("access-old", "refresh-old"),
            ("access-new", "refresh-new"),
        ]
    )

    def fail_refresh(env: str, refresh_token: str) -> TokenResponse:
        raise AssertionError("refresh should not be retried")

    monkeypatch.setattr(store, "get_tokens", lambda env: next(tokens))
    monkeypatch.setattr(refresh_helper, "_refresh_lock", lambda env: nullcontext())
    monkeypatch.setattr(refresh_helper, "refresh_tokens", fail_refresh)

    assert refresh_helper.try_refresh("sandbox") == "access-new"


def test_try_refresh__windows_lock_serializes_refresh_rotation(
    isolated_config, monkeypatch
):
    store.save_tokens("sandbox", "access-old", "refresh-old")
    original_get_tokens = store.get_tokens
    initial_reads = 0
    initial_reads_lock = threading.Lock()

    class FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 2

        def __init__(self):
            self._lock = threading.Lock()
            self.lock_held = False

        def locking(self, fd: int, mode: int, nbytes: int) -> None:
            if mode == self.LK_LOCK:
                self._lock.acquire()
                self.lock_held = True
            else:
                self.lock_held = False
                self._lock.release()

    fake_msvcrt = FakeMsvcrt()

    def tracked_get_tokens(env: str):
        nonlocal initial_reads
        if not fake_msvcrt.lock_held:
            with initial_reads_lock:
                initial_reads += 1
        return original_get_tokens(env)

    refresh_calls = 0

    def fake_refresh_tokens(env: str, refresh_token: str) -> TokenResponse:
        nonlocal refresh_calls
        refresh_calls += 1
        assert refresh_token == "refresh-old"
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with initial_reads_lock:
                if initial_reads >= 2:
                    break
            time.sleep(0.01)
        return TokenResponse(access_token="access-new", refresh_token="refresh-new")

    monkeypatch.setattr(refresh_helper, "fcntl", None)
    monkeypatch.setattr(refresh_helper, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(store, "get_tokens", tracked_get_tokens)
    monkeypatch.setattr(refresh_helper, "refresh_tokens", fake_refresh_tokens)

    results: list[str | None] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(refresh_helper.try_refresh("sandbox"))
        )
        for _ in range(2)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert results == ["access-new", "access-new"]
    assert refresh_calls == 1
    assert store.get_tokens("sandbox") == ("access-new", "refresh-new")


def test_try_refresh__clears_tokens_without_replacement_refresh_token(
    isolated_config, monkeypatch
):
    store.save_tokens("sandbox", "access-old", "refresh-old")
    monkeypatch.setattr(
        refresh_helper,
        "refresh_tokens",
        lambda env, refresh_token: TokenResponse(access_token="access-new"),
    )

    assert refresh_helper.try_refresh("sandbox") is None
    assert store.get_tokens("sandbox") == ("", "")


def test_try_refresh__clears_tokens_on_invalid_grant(isolated_config, monkeypatch):
    store.save_tokens("sandbox", "access-old", "refresh-old")

    def fail_refresh(env: str, refresh_token: str) -> TokenResponse:
        raise OAuthTokenError("invalid_grant", "refresh token expired")

    monkeypatch.setattr(refresh_helper, "refresh_tokens", fail_refresh)

    assert refresh_helper.try_refresh("sandbox") is None
    assert store.get_tokens("sandbox") == ("", "")


def test_refresh_tokens__classifies_ramp_refresh_not_found_as_invalid_grant(
    monkeypatch,
):
    class FakeResponse:
        status_code = 401
        is_error = True
        text = (
            '{"error_v2":{"additional_info":{},"notes":"","error_id":"abc123",'
            '"error_code":"DEVELOPER_7002","message":"Refresh token with given '
            'refresh_token not found"},"error":{"message":"Refresh token with '
            'given refresh_token not found","details":{}}}'
        )

        @staticmethod
        def json():
            return {
                "error_v2": {
                    "additional_info": {},
                    "notes": "",
                    "error_id": "abc123",
                    "error_code": "DEVELOPER_7002",
                    "message": "Refresh token with given refresh_token not found",
                },
                "error": {
                    "message": "Refresh token with given refresh_token not found",
                    "details": {},
                },
            }

    monkeypatch.setattr(
        oauth_module, "_do_token_request", lambda env, url, data: FakeResponse()
    )

    with pytest.raises(OAuthTokenError) as exc_info:
        oauth_module.refresh_tokens("sandbox", "refresh-old")

    assert exc_info.value.error == "invalid_grant"


def test_token_request__sends_extra_auth_header(monkeypatch):
    captured = {}

    def fake_post(url, data, headers):
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers

        class FakeResponse:
            status_code = 200
            is_error = False

            @staticmethod
            def json():
                return {"access_token": "access"}

        return FakeResponse()

    monkeypatch.setattr(
        oauth_module,
        "extra_auth_headers",
        lambda env: {"X-Extra-Auth": f"{env}-token"},
    )
    monkeypatch.setattr(oauth_module.httpx, "post", fake_post)

    oauth_module._do_token_request("sandbox", "https://example.test/token", {})

    assert captured["headers"]["X-Extra-Auth"] == "sandbox-token"
    assert captured["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert captured["data"]["client_id"]


def test_try_refresh__raises_on_transient_refresh_failure(isolated_config, monkeypatch):
    store.save_tokens("sandbox", "access-old", "refresh-old")

    def fail_refresh(env: str, refresh_token: str) -> TokenResponse:
        raise OAuthTokenError("temporarily_unavailable", "retry later")

    monkeypatch.setattr(refresh_helper, "refresh_tokens", fail_refresh)

    with pytest.raises(RefreshFailedError):
        refresh_helper.try_refresh("sandbox")


def test_try_refresh__access_only_credentials_do_not_exchange(
    isolated_config, monkeypatch
):
    store.save_tokens("sandbox", "standalone-access", "")
    monkeypatch.setattr(
        refresh_helper,
        "refresh_tokens",
        lambda env, refresh_token: pytest.fail(
            "access-only credentials cannot use a refresh-token exchange"
        ),
    )

    assert refresh_helper.try_refresh("sandbox") is None


class TestUsageErrorDisplay:
    """Verify that UsageErrors show the usage box but not the strip-wave banner."""

    def test_extra_arg_rejected(self, isolated_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["auth", "login", "login"])
        assert result.exit_code != 0

    def test_usage_error_suppresses_wave_during_show(
        self, isolated_config, monkeypatch
    ):
        """main() sets _suppress_wave=True while e.show() runs, then resets it."""
        flag_during_show: list[bool] = []
        original_show = click.UsageError.show

        def spy_show(self, file=None):
            flag_during_show.append(BoxHelpFormatter._suppress_wave)
            original_show(self, file)

        monkeypatch.setattr(click.UsageError, "show", spy_show)
        # --human forces the non-agent path so e.show() is called
        # (without it, _is_agent_mode() returns True in CI/pytest
        # because stdout is not a TTY)
        monkeypatch.setattr(sys, "argv", ["ramp", "--human", "auth", "login", "login"])

        with pytest.raises(SystemExit):
            main()

        # Flag should have been True when show() ran
        assert flag_during_show == [True]
        # Flag should be cleaned up afterwards
        assert BoxHelpFormatter._suppress_wave is False

    def test_suppress_wave_flag_prevents_wave(self, isolated_config):
        """BoxHelpFormatter._suppress_wave=True prevents the wave in getvalue()."""
        BoxHelpFormatter._suppress_wave = True
        try:
            fmt = BoxHelpFormatter()
            fmt.write("test content\n")
            result = fmt.getvalue()
            assert "test content" in result
            # Wave banner chars should be absent
            assert "\u2599\u2580\u2596" not in result
        finally:
            BoxHelpFormatter._suppress_wave = False


class TestCallbackHtml:
    def test_success_page_contains_key_elements(self):
        html = _callback_html(
            success=True,
            title="Authenticated",
            message="You can close this window and return to your terminal.",
        )
        assert "<!DOCTYPE html>" in html
        assert "ramp-cli" in html
        assert "Authenticated" in html
        assert "close this window" in html
        assert "#e4f222" in html  # Ramp brand yellow on the heading

    def test_error_page_contains_key_elements(self):
        html = _callback_html(
            success=False,
            title="Authentication failed",
            message="You can close this window.",
            detail="access_denied — user cancelled",
        )
        assert "<!DOCTYPE html>" in html
        assert "Authentication failed" in html
        assert "access_denied" in html
        assert "#ef4444" in html  # red accent
        assert "#e4f222" not in html

    def test_page_is_rendered_in_the_router_dark_palette(self):
        # The browser lands here straight from Router, so a white card would
        # flash bright in the middle of an otherwise dark hand-off.
        html = _callback_html(success=True, title="OK", message="msg")

        assert 'content="dark"' in html
        assert "background:#090909" in html
        assert "#fff" not in html
        assert "#fafafa" not in html

    def test_detail_block_rendered_when_provided(self):
        html = _callback_html(
            success=False,
            title="Error",
            message="msg",
            detail="something went wrong",
        )
        assert 'class="d"' in html
        assert "something went wrong" in html

    def test_detail_block_absent_when_empty(self):
        html = _callback_html(success=True, title="OK", message="msg")
        assert 'class="d"' not in html

    def test_html_escapes_user_content(self):
        html = _callback_html(
            success=False,
            title="<script>alert(1)</script>",
            message='"><img src=x onerror=alert(1)>',
            detail="<b>bold</b>",
        )
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "<b>bold</b>" not in html
        assert "&lt;b&gt;" in html


class TestResolveScopes:
    """Verify _resolve_scopes uses env-specific cached spec when available."""

    def test_does_not_inject_agent_wallet_scope(self, monkeypatch):
        monkeypatch.setattr(oauth_module, "configured_scopes", lambda: "")
        monkeypatch.setattr(oauth_module, "_resolve_scope_spec_paths", lambda env: ())

        assert oauth_module._resolve_scopes("sandbox") == ""

    def test_uses_env_specific_cached_spec(self, tmp_path, monkeypatch):
        """If an env-specific cached spec exists, its scopes are included."""
        # Create a cached spec with a custom scope
        spec = {
            "paths": {
                "/developer/v1/agent-tools/custom-tool": {
                    "post": {
                        "operationId": "custom-tool",
                        "summary": "Custom",
                        "description": "Custom tool",
                        "security": [{"oauth2": ["custom:special"]}],
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Req"}
                                }
                            }
                        },
                    }
                }
            },
            "components": {"schemas": {"Req": {"type": "object", "properties": {}}}},
        }
        spec_file = tmp_path / "agent-tool-sandbox.json"
        spec_file.write_text(json.dumps(spec))

        monkeypatch.setattr(
            oauth_module,
            "_resolve_spec_path",
            lambda env: tmp_path / f"agent-tool-{env}.json",
        )
        monkeypatch.setattr(
            oauth_module,
            "configured_scopes",
            lambda: "",
        )

        scopes = oauth_module._resolve_scopes("sandbox")
        assert "custom:special" in scopes

    def test_bundled_spec_scopes_survive_stale_cached_spec(self, tmp_path, monkeypatch):
        """A stale cached spec must not hide scopes shipped with the CLI."""
        bundled_spec = {
            "paths": {
                "/developer/v1/agent-tools/get-attention-feed": {
                    "post": {
                        "operationId": "get-attention-feed",
                        "summary": "Get attention feed",
                        "description": "Get attention feed",
                        "security": [{"oauth2": ["tasks:read"]}],
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Req"}
                                }
                            }
                        },
                    }
                }
            },
            "components": {"schemas": {"Req": {"type": "object", "properties": {}}}},
        }
        stale_cached_spec = {
            "paths": {
                "/developer/v1/agent-tools/get-users": {
                    "post": {
                        "operationId": "get-users",
                        "summary": "Get users",
                        "description": "Get users",
                        "security": [{"oauth2": ["users:read"]}],
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Req"}
                                }
                            }
                        },
                    }
                }
            },
            "components": {"schemas": {"Req": {"type": "object", "properties": {}}}},
        }
        bundled_file = tmp_path / "bundled-agent-tool.json"
        cached_file = tmp_path / "agent-tool-production.json"
        bundled_file.write_text(json.dumps(bundled_spec))
        cached_file.write_text(json.dumps(stale_cached_spec))

        monkeypatch.setattr(oauth_module, "AGENT_TOOL_SPEC", bundled_file)
        monkeypatch.setattr(
            oauth_module,
            "_resolve_spec_path",
            lambda env: cached_file,
        )
        monkeypatch.setattr(
            oauth_module,
            "local_agent_tool_hash",
            lambda cache_key: tmp_path / "missing-hash.txt",
        )
        monkeypatch.setattr(
            oauth_module,
            "configured_scopes",
            lambda: "",
        )

        scopes = oauth_module._resolve_scopes("production").split()
        assert "tasks:read" in scopes
        assert "users:read" in scopes

    def test_fresh_cached_spec_is_authoritative(self, tmp_path, monkeypatch):
        """A freshly synced cache reflects the current env contract."""
        bundled_spec = {
            "paths": {
                "/developer/v1/agent-tools/get-attention-feed": {
                    "post": {
                        "operationId": "get-attention-feed",
                        "summary": "Get attention feed",
                        "description": "Get attention feed",
                        "security": [{"oauth2": ["tasks:read"]}],
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Req"}
                                }
                            }
                        },
                    }
                }
            },
            "components": {"schemas": {"Req": {"type": "object", "properties": {}}}},
        }
        fresh_cached_spec = {
            "paths": {
                "/developer/v1/agent-tools/get-users": {
                    "post": {
                        "operationId": "get-users",
                        "summary": "Get users",
                        "description": "Get users",
                        "security": [{"oauth2": ["users:read"]}],
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Req"}
                                }
                            }
                        },
                    }
                }
            },
            "components": {"schemas": {"Req": {"type": "object", "properties": {}}}},
        }
        bundled_file = tmp_path / "bundled-agent-tool.json"
        cached_file = tmp_path / "agent-tool-production.json"
        hash_file = tmp_path / "agent-tool-production-hash.txt"
        bundled_file.write_text(json.dumps(bundled_spec))
        cached_file.write_text(json.dumps(fresh_cached_spec))
        hash_file.write_text("fresh")

        monkeypatch.setattr(oauth_module, "AGENT_TOOL_SPEC", bundled_file)
        monkeypatch.setattr(
            oauth_module,
            "_resolve_spec_path",
            lambda env: cached_file,
        )
        monkeypatch.setattr(
            oauth_module,
            "local_agent_tool_hash",
            lambda cache_key: hash_file,
        )
        monkeypatch.setattr(
            oauth_module,
            "environment_cache_key",
            lambda env: env,
        )
        monkeypatch.setattr(
            oauth_module,
            "configured_scopes",
            lambda: "",
        )

        scopes = oauth_module._resolve_scopes("production").split()
        assert "tasks:read" not in scopes
        assert "users:read" in scopes

    def test_falls_back_to_bundled_spec(self, tmp_path, monkeypatch):
        """Without a cached spec, scopes come from the bundled spec."""
        monkeypatch.setattr(
            oauth_module,
            "_resolve_spec_path",
            lambda env: tmp_path / f"agent-tool-{env}.json",  # won't exist
        )
        monkeypatch.setattr(
            oauth_module,
            "configured_scopes",
            lambda: "",
        )

        scopes = oauth_module._resolve_scopes("production").split()
        assert "applications:read" in scopes
        assert "users:read" in scopes
        assert "business:read" not in scopes
        assert "incorporation:read" not in scopes
        assert "incorporation:write" not in scopes

    def test_explicit_login_scopes_override_discovery(self, monkeypatch):
        monkeypatch.setattr(
            oauth_module,
            "_resolve_scopes",
            lambda env: pytest.fail("scope discovery should not run"),
        )

        scopes = oauth_module._resolve_login_scopes(
            "production",
            ("applications:read", "applications:write", "applications:read"),
        )

        assert scopes == "applications:read applications:write"


class TestAuthLoginSpecRefresh:
    """Verify CLI login refreshes tool specs before requesting OAuth scopes."""

    def _patch_success_ui(self, monkeypatch):
        monkeypatch.setattr(auth_command_module, "show_nyc", lambda duration=5.0: None)
        monkeypatch.setattr(auth_command_module, "show_status_box", lambda envs: None)
        monkeypatch.setattr(auth_command_module, "is_first_login", lambda: False)
        monkeypatch.setattr(auth_command_module, "record_first_login", lambda: None)

    def test_login_refreshes_spec_before_oauth(self, isolated_config, monkeypatch):
        self._patch_success_ui(monkeypatch)
        calls: list[tuple[str, str]] = []

        def fake_fetch_spec(env: str) -> int:
            calls.append(("fetch", env))
            return 1

        def fake_login(env: str, opts: oauth_module.LoginOptions) -> TokenResponse:
            calls.append(("login", env))
            return TokenResponse(
                access_token="access",
                refresh_token="refresh",
                scope="tasks:read",
            )

        monkeypatch.setattr(auth_command_module, "fetch_spec", fake_fetch_spec)
        monkeypatch.setattr(auth_command_module, "do_login", fake_login)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--human", "--env", "sandbox", "auth", "login"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert calls == [("fetch", "sandbox"), ("login", "sandbox")]
        assert store.get_granted_scopes("sandbox", profile="human") == {"tasks:read"}

    def test_login_continues_if_spec_refresh_fails(self, isolated_config, monkeypatch):
        self._patch_success_ui(monkeypatch)

        def fail_fetch_spec(env: str) -> int:
            raise RuntimeError("network down")

        monkeypatch.setattr(auth_command_module, "fetch_spec", fail_fetch_spec)
        monkeypatch.setattr(
            auth_command_module,
            "do_login",
            lambda env, opts: TokenResponse(
                access_token="access",
                refresh_token="refresh",
                scope="users:read",
            ),
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--human", "--env", "sandbox", "auth", "login"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "Could not refresh tool definitions before login" in result.output
        assert store.get_granted_scopes("sandbox", profile="human") == {"users:read"}

    def test_login_passes_explicit_oauth_options(self, isolated_config, monkeypatch):
        self._patch_success_ui(monkeypatch)
        requested_scopes = (
            "applications:read",
            "applications:write",
            "bank_accounts:read",
        )

        monkeypatch.setattr(auth_command_module, "fetch_spec", lambda env: 1)

        def fake_login(env: str, opts: oauth_module.LoginOptions) -> TokenResponse:
            assert env == "sandbox"
            assert opts.scopes == requested_scopes
            assert opts.auth_level == "business"
            return TokenResponse(
                access_token="access",
                refresh_token="refresh",
                scope=" ".join(requested_scopes),
            )

        monkeypatch.setattr(auth_command_module, "do_login", fake_login)

        result = CliRunner().invoke(
            cli,
            [
                "--human",
                "--env",
                "sandbox",
                "auth",
                "login",
                "--scope",
                "applications:read",
                "--scope",
                "applications:write",
                "--scope",
                "bank_accounts:read",
                "--auth-level",
                "business",
            ],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert store.get_granted_scopes("sandbox", profile="human") == set(
            requested_scopes
        )

    def test_refresh_skips_when_custom_scopes_configured(
        self, isolated_config, monkeypatch
    ):
        monkeypatch.setattr(
            auth_command_module.settings,
            "configured_scopes",
            lambda: "users:read",
        )
        monkeypatch.setattr(
            auth_command_module,
            "fetch_spec",
            lambda env: pytest.fail("fetch_spec should not be called"),
        )

        auth_command_module._refresh_tool_spec_before_login("sandbox")


class TestStandaloneAgentLogin:
    def test_agent_login_reads_credentials_from_environment(
        self, isolated_config, monkeypatch
    ):
        monkeypatch.setenv("RAMP_CLIENT_ID", "agent-client")
        monkeypatch.setenv("RAMP_CLIENT_SECRET", "agent-secret")
        monkeypatch.setattr(
            "ramp_cli.tools.commands.maybe_sync",
            lambda env: pytest.fail("agent login must not refresh the tool spec"),
        )

        def fake_client_credentials_login(
            env: str,
            *,
            client_id: str,
            client_secret: str,
            scopes: tuple[str, ...],
        ) -> TokenResponse:
            assert env == "sandbox"
            assert client_id == "agent-client"
            assert client_secret == "agent-secret"
            assert scopes == ()
            return TokenResponse(
                access_token="standalone-access",
                expires_in=604800,
                scope="transactions:read",
            )

        monkeypatch.setattr(
            auth_command_module,
            "do_client_credentials_login",
            fake_client_credentials_login,
        )

        result = CliRunner().invoke(
            cli,
            ["--agent", "--env", "sandbox", "agent", "login"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert store.get_tokens("sandbox", profile="agent") == (
            "standalone-access",
            "",
        )
        assert settings.load().profile == "agent"

    def test_agent_login_requires_client_id(self, isolated_config, monkeypatch):
        monkeypatch.delenv("RAMP_CLIENT_ID", raising=False)
        monkeypatch.setenv("RAMP_CLIENT_SECRET", "agent-secret")

        result = CliRunner().invoke(cli, ["--human", "agent", "login"])

        assert result.exit_code == 2
        assert "Pass --client-id or set RAMP_CLIENT_ID." in result.output

    def test_env_secret_selects_client_credentials_and_replaces_stale_state(
        self, isolated_config, monkeypatch
    ):
        store.save_tokens(
            "sandbox",
            "old-access",
            "old-refresh",
            granted_scopes="old:scope",
            agent_key_uuid="old-agent-key",
            profile="agent",
        )
        monkeypatch.setenv("RAMP_CLIENT_SECRET", "secret-from-env")
        monkeypatch.setattr(
            auth_command_module,
            "fetch_spec",
            lambda env: pytest.fail("standalone login must not refresh the spec"),
        )
        monkeypatch.setattr(
            auth_command_module,
            "do_login",
            lambda env, opts: pytest.fail("standalone login must not use PKCE"),
        )

        def fake_client_credentials_login(
            env: str,
            *,
            client_id: str,
            client_secret: str,
            scopes: tuple[str, ...],
        ) -> TokenResponse:
            assert env == "sandbox"
            assert client_id == "agent-client"
            assert client_secret == "secret-from-env"
            assert scopes == ()
            return TokenResponse(
                access_token="standalone-access",
                refresh_token="unexpected-refresh",
                expires_in=604800,
                refresh_token_expires_in=86400,
                scope="transactions:read users:read",
                agent_key_uuid="unexpected-agent-key",
            )

        monkeypatch.setattr(
            auth_command_module,
            "do_client_credentials_login",
            fake_client_credentials_login,
        )

        result = CliRunner().invoke(
            cli,
            [
                "--agent",
                "--env",
                "sandbox",
                "auth",
                "login",
                "--client-id",
                "agent-client",
            ],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["data"][0] == {
            "message": "Authenticated standalone agent for Sandbox.",
            "environment": "sandbox",
            "profile": "agent",
            "scopes": ["transactions:read", "users:read"],
            "expires_in": 604800,
        }
        state = store.get_token_state("sandbox", profile="agent")
        assert state.access_token == "standalone-access"
        assert state.refresh_token == ""
        assert state.access_token_expires_in == 604800
        assert state.refresh_token_expires_in == 0
        assert store.get_granted_scopes("sandbox", profile="agent") == {
            "transactions:read",
            "users:read",
        }
        assert store.get_agent_key_uuid("sandbox", profile="agent") == ""
        assert settings.load().profile == "agent"

    def test_flag_secret_wins_over_env_and_warns(self, isolated_config, monkeypatch):
        monkeypatch.setenv("RAMP_CLIENT_SECRET", "secret-from-env")

        def fake_client_credentials_login(
            env: str,
            *,
            client_id: str,
            client_secret: str,
            scopes: tuple[str, ...],
        ) -> TokenResponse:
            assert client_secret == "secret-from-flag"
            return TokenResponse(
                access_token="standalone-access",
                expires_in=604800,
                scope="transactions:read",
            )

        monkeypatch.setattr(
            auth_command_module,
            "do_client_credentials_login",
            fake_client_credentials_login,
        )
        monkeypatch.setattr(
            auth_command_module,
            "show_nyc",
            lambda duration: pytest.fail("standalone login must not show onboarding"),
        )

        result = CliRunner().invoke(
            cli,
            [
                "--human",
                "auth",
                "login",
                "--client-id",
                "agent-client",
                "--client-secret",
                "secret-from-flag",
                "--scope",
                "transactions:read",
            ],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "Warning: --client-secret overrides RAMP_CLIENT_SECRET." in result.output
        assert "\u2713 Standalone agent authenticated" in result.output
        assert "  Environment  " not in result.output
        assert "default environment" not in result.output
        assert "ramp env production" not in result.output
        assert "secret-from-env" not in result.output
        assert "secret-from-flag" not in result.output

    def test_env_secret_alone_does_not_change_pkce_flow(
        self, isolated_config, monkeypatch
    ):
        monkeypatch.setenv("RAMP_CLIENT_SECRET", "standalone-secret")
        monkeypatch.setattr(auth_command_module, "fetch_spec", lambda env: 1)
        monkeypatch.setattr(auth_command_module, "show_nyc", lambda duration: None)
        monkeypatch.setattr(auth_command_module, "show_status_box", lambda envs: None)
        monkeypatch.setattr(auth_command_module, "is_first_login", lambda: False)
        monkeypatch.setattr(auth_command_module, "record_first_login", lambda: None)

        def fake_login(env: str, opts: oauth_module.LoginOptions) -> TokenResponse:
            return TokenResponse(
                access_token="human-access",
                refresh_token="human-refresh",
                scope="users:read",
            )

        monkeypatch.setattr(auth_command_module, "do_login", fake_login)
        monkeypatch.setattr(
            auth_command_module,
            "do_client_credentials_login",
            lambda *args, **kwargs: pytest.fail(
                "environment secret alone must not select client credentials"
            ),
        )

        result = CliRunner().invoke(
            cli,
            ["--human", "--env", "sandbox", "auth", "login"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert store.get_tokens("sandbox", profile="human") == (
            "human-access",
            "human-refresh",
        )
        assert settings.load().profile == "human"

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            pytest.param(
                ["--client-secret", "secret"],
                "--client-secret requires --client-id",
                id="secret-without-client-id",
            ),
            pytest.param(
                ["--client-id", "agent-client"],
                "--client-id requires --client-secret or RAMP_CLIENT_SECRET",
                id="missing-secret",
            ),
            pytest.param(
                [
                    "--client-id",
                    "agent-client",
                    "--client-secret",
                    "secret",
                    "--no_browser",
                ],
                "--no_browser cannot be used with --client-id",
                id="no-browser",
            ),
            pytest.param(
                [
                    "--client-id",
                    "agent-client",
                    "--client-secret",
                    "secret",
                    "--auth-level",
                    "user",
                ],
                "--auth-level cannot be used with --client-id",
                id="auth-level",
            ),
            pytest.param(
                [
                    "--token_stdin",
                    "--client-id",
                    "agent-client",
                    "--client-secret",
                    "secret",
                ],
                "--token_stdin cannot be used with --client-id or --client-secret",
                id="token-stdin",
            ),
        ],
    )
    def test_rejects_invalid_option_combinations(
        self, isolated_config, monkeypatch, args: list[str], message: str
    ):
        monkeypatch.delenv("RAMP_CLIENT_SECRET", raising=False)

        result = CliRunner().invoke(cli, ["--human", "auth", "login", *args])

        assert result.exit_code == 2
        assert message in result.output


class TestPostLoginEnvHint:
    """Verify the default-env hint is shown after login."""

    def test_login_token_stdin_shows_env_hint(self, isolated_config):
        """Human-mode --token_stdin login prints the ramp env hint."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--human", "auth", "login", "--token_stdin", "--env", "production"],
            input="new-token\n",
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "ramp env production" in result.output
        assert settings.load().profile == "human"
        assert store.get_tokens("production", profile="human") == (
            "new-token",
            "",
        )
        assert store.get_tokens("production") == ("", "")

    def test_login_token_stdin_agent_mode_no_hint(self, isolated_config):
        """Agent mode outputs JSON — no hint line."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--agent", "auth", "login", "--token_stdin", "--env", "production"],
            input="new-token\n",
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "Token saved" in data["data"][0]["message"]
        # JSON output should not contain the hint
        assert "ramp env" not in result.output

    def test_login_token_stdin_clears_stale_granted_scopes(self, isolated_config):
        store.save_tokens(
            "production",
            "old-token",
            "refresh",
            granted_scopes="cards:read_agentic",
            profile="human",
        )

        result = CliRunner().invoke(
            cli,
            ["--agent", "auth", "login", "--token_stdin", "--env", "production"],
            input="replacement-token\n",
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert store.get_granted_scopes("production", profile="human") == set()

    def test_login_token_stdin_rejects_oauth_options(self, isolated_config):
        result = CliRunner().invoke(
            cli,
            [
                "--human",
                "auth",
                "login",
                "--token_stdin",
                "--scope",
                "applications:read",
                "--auth-level",
                "business",
            ],
            input="new-token\n",
        )

        assert result.exit_code == 2
        assert "--scope and --auth-level cannot be used with --token_stdin" in (
            result.output
        )

    def test_repeat_oauth_login_recommends_funds_enroll(
        self, isolated_config, monkeypatch
    ):
        record_first_login()

        monkeypatch.setattr("ramp_cli.commands.auth.show_nyc", lambda duration: None)
        monkeypatch.setattr(
            "ramp_cli.commands.auth.do_login",
            lambda env, opts: TokenResponse(
                access_token="access", refresh_token="refresh", scope="business:read"
            ),
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--human", "--env", "production", "auth", "login", "--no_browser"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "Next step:  ramp funds enroll" in result.output
        assert "Explore all commands:  ramp --help" in result.output
        assert "ramp env production" in result.output


class TestEnvironmentAuthPreflight:
    def test_login_requires_environment_auth_human(
        self, isolated_config, monkeypatch, capsys
    ):
        monkeypatch.setattr("ramp_cli.main.check_for_update", lambda: None)
        monkeypatch.setattr("ramp_cli.main.emit_update_notice", lambda agent: None)
        monkeypatch.setattr(
            "ramp_cli.commands.auth.missing_required_environment_auth",
            lambda env: True,
        )
        monkeypatch.setattr(
            "ramp_cli.commands.auth.environment_auth_required_message",
            lambda env: f"{env} requires extra auth. Ask the user for a token.",
        )
        monkeypatch.setattr(
            sys, "argv", ["ramp", "--human", "--env", "sandbox", "auth", "login"]
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        captured = capsys.readouterr()
        assert exc_info.value.code == 4
        assert "sandbox requires extra auth" in captured.err
        assert "Ask the user for a token" in captured.err

    def test_login_requires_environment_auth_agent(
        self, isolated_config, monkeypatch, capsys
    ):
        monkeypatch.setattr("ramp_cli.main.check_for_update", lambda: None)
        monkeypatch.setattr("ramp_cli.main.emit_update_notice", lambda agent: None)
        monkeypatch.setattr(
            "ramp_cli.commands.auth.missing_required_environment_auth",
            lambda env: True,
        )
        monkeypatch.setattr(
            "ramp_cli.commands.auth.environment_auth_required_message",
            lambda env: f"{env} requires extra auth. Agents should request it.",
        )
        monkeypatch.setattr(
            sys, "argv", ["ramp", "--agent", "--env", "sandbox", "auth", "login"]
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert exc_info.value.code == 4
        assert data["error"]["code"] == 4
        assert "sandbox requires extra auth" in data["error"]["message"]


class TestOAuthLoginFailures:
    """OAuth login failures raise click.ClickException so
    main() renders them as `Error: <message>` instead of leaking
    `Error: RuntimeError: internal error`."""

    def test_timeout_raises_click_exception(self, monkeypatch):
        class TimeoutCallback:
            redirect_uri = "http://localhost:19817/callback"
            state = "state-123"
            code_challenge = "challenge-123"

            def wait_for_code(self, timeout, *, timeout_message):
                raise click.ClickException(timeout_message)

            def shutdown(self):
                pass

        monkeypatch.setattr(
            oauth_module, "start_pkce_callback", lambda: TimeoutCallback()
        )
        monkeypatch.setattr(oauth_module, "_open_browser", lambda url: True)

        with pytest.raises(click.ClickException) as exc_info:
            oauth_module.login("sandbox", oauth_module.LoginOptions(no_browser=True))
        assert "timed out" in str(exc_info.value).lower()

    def test_token_exchange_failure_renders_clean_error(
        self, isolated_config, monkeypatch
    ):
        monkeypatch.setattr(auth_command_module, "fetch_spec", lambda env: None)

        def fake_login(env: str, opts: oauth_module.LoginOptions) -> TokenResponse:
            raise OAuthTokenError(
                "token_request_failed",
                "The provided authorization grant or refresh token is invalid.",
            )

        monkeypatch.setattr(auth_command_module, "do_login", fake_login)

        result = CliRunner().invoke(
            cli,
            ["--human", "--env", "sandbox", "auth", "login"],
            catch_exceptions=False,
        )

        assert result.exit_code == 1
        assert "token_request_failed" in result.output
        assert "authorization grant or refresh token is invalid" in result.output
        assert "internal error" not in result.output

    def test_token_transport_failure_renders_clean_error(
        self, isolated_config, monkeypatch
    ):
        monkeypatch.setattr(auth_command_module, "fetch_spec", lambda env: None)

        def fake_login(env: str, opts: oauth_module.LoginOptions) -> TokenResponse:
            raise httpx.ConnectError("connection failed")

        monkeypatch.setattr(auth_command_module, "do_login", fake_login)

        result = CliRunner().invoke(
            cli,
            ["--human", "--env", "sandbox", "auth", "login"],
            catch_exceptions=False,
        )

        assert result.exit_code == 1
        assert "Token request failed" in result.output
        assert "connection failed" in result.output
        assert "internal error" not in result.output
