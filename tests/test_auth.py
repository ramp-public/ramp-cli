"""Tests for auth token storage and PKCE generation."""

from __future__ import annotations

import json
import sys
from contextlib import nullcontext

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
from ramp_cli.errors import RefreshFailedError
from ramp_cli.main import cli, main


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


def test_try_refresh__raises_on_transient_refresh_failure(isolated_config, monkeypatch):
    store.save_tokens("sandbox", "access-old", "refresh-old")

    def fail_refresh(env: str, refresh_token: str) -> TokenResponse:
        raise OAuthTokenError("temporarily_unavailable", "retry later")

    monkeypatch.setattr(refresh_helper, "refresh_tokens", fail_refresh)

    with pytest.raises(RefreshFailedError):
        refresh_helper.try_refresh("sandbox")


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
        import click as _click

        from ramp_cli.main import BoxHelpFormatter

        flag_during_show: list[bool] = []
        original_show = _click.UsageError.show

        def spy_show(self, file=None):
            flag_during_show.append(BoxHelpFormatter._suppress_wave)
            original_show(self, file)

        monkeypatch.setattr(_click.UsageError, "show", spy_show)
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
        from ramp_cli.main import BoxHelpFormatter

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
        assert "\u25c6" in html  # ◆ filled diamond
        assert "Authenticated" in html
        assert "close this window" in html
        assert "#10b981" in html  # green accent

    def test_error_page_contains_key_elements(self):
        html = _callback_html(
            success=False,
            title="Authentication failed",
            message="You can close this window.",
            detail="access_denied — user cancelled",
        )
        assert "<!DOCTYPE html>" in html
        assert "\u2715" in html  # ✕ symbol
        assert "Authentication failed" in html
        assert "access_denied" in html
        assert "#ef4444" in html  # red accent

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

    def test_uses_env_specific_cached_spec(self, tmp_path, monkeypatch):
        """If an env-specific cached spec exists, scopes are extracted from it."""
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

        scopes = oauth_module._resolve_scopes("production")
        # Bundled spec has standard scopes
        assert "business:read" in scopes


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
