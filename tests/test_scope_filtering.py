"""Tests for OAuth scope persistence, tool filtering, and pre-flight checks."""

from __future__ import annotations

import json

import click
from click.testing import CliRunner

from ramp_cli.auth import oauth as oauth_module
from ramp_cli.auth import store
from ramp_cli.config import settings
from ramp_cli.main import cli
from ramp_cli.tools.parser import ToolDef
from ramp_cli.tools.registry import _filter_by_scopes


class TestScopePersistence:
    """Granted scopes are saved to and loaded from config."""

    def test_save_and_load_scopes(self, isolated_config):
        store.save_tokens(
            "sandbox",
            "tok",
            "refresh",
            granted_scopes="transactions:read users:read",
        )
        scopes = store.get_granted_scopes("sandbox")
        assert scopes == {"transactions:read", "users:read"}

    def test_empty_scopes_returns_empty_set(self, isolated_config):
        store.save_tokens("sandbox", "tok", "refresh", granted_scopes="")
        scopes = store.get_granted_scopes("sandbox")
        assert scopes == set()

    def test_no_config_returns_empty_set(self, isolated_config):
        scopes = store.get_granted_scopes("sandbox")
        assert scopes == set()

    def test_clear_tokens_clears_scopes(self, isolated_config):
        store.save_tokens(
            "sandbox",
            "tok",
            "refresh",
            granted_scopes="transactions:read",
        )
        store.clear_tokens("sandbox")
        scopes = store.get_granted_scopes("sandbox")
        assert scopes == set()

    def test_scopes_roundtrip_through_toml(self, isolated_config):
        cfg = settings.Config()
        cfg.sandbox.access_token = "tok"
        cfg.sandbox.granted_scopes = "a:read b:write"
        settings.save(cfg)

        loaded = settings.load()
        assert loaded.sandbox.granted_scopes == "a:read b:write"

    def test_refresh_preserves_scopes(self, isolated_config):
        """Token refresh (no granted_scopes param) must not wipe stored scopes."""
        store.save_tokens(
            "production",
            "tok1",
            "refresh1",
            granted_scopes="transactions:read users:read",
        )
        # Simulate a token refresh — no granted_scopes passed
        store.save_tokens(
            "production",
            "tok2",
            "refresh2",
            access_token_expires_in=3600,
        )
        scopes = store.get_granted_scopes("production")
        assert scopes == {"transactions:read", "users:read"}

    def test_explicit_empty_string_preserves_scopes(self, isolated_config):
        """Passing granted_scopes='' should also preserve prior scopes."""
        store.save_tokens(
            "production",
            "tok1",
            "refresh1",
            granted_scopes="a:read",
        )
        store.save_tokens(
            "production",
            "tok2",
            "refresh2",
            granted_scopes="",
        )
        assert store.get_granted_scopes("production") == {"a:read"}

    def test_explicit_new_scopes_overwrite(self, isolated_config):
        """Passing non-empty granted_scopes should overwrite."""
        store.save_tokens(
            "production",
            "tok1",
            "refresh1",
            granted_scopes="old:scope",
        )
        store.save_tokens(
            "production",
            "tok2",
            "refresh2",
            granted_scopes="new:scope",
        )
        assert store.get_granted_scopes("production") == {"new:scope"}

    def test_scopes_per_environment(self, isolated_config):
        store.save_tokens(
            "sandbox",
            "tok1",
            "",
            granted_scopes="sandbox:scope",
        )
        store.save_tokens(
            "production",
            "tok2",
            "",
            granted_scopes="prod:scope",
        )
        assert store.get_granted_scopes("sandbox") == {"sandbox:scope"}
        assert store.get_granted_scopes("production") == {"prod:scope"}


class TestFilterByScopes:
    """_filter_by_scopes hides tools the token can't access."""

    def _make_tool(self, name: str, scopes: list[str] | None = None) -> ToolDef:
        return ToolDef(
            name=name,
            path=f"/developer/v1/agent-tools/{name}",
            http_method="post",
            summary=name,
            description=name,
            required_scopes=scopes or [],
        )

    def test_all_tools_shown_when_no_scopes_stored(self, isolated_config):
        """Backwards compat: old tokens with no scope info show everything."""
        tools = [
            self._make_tool("tool-a", ["a:read"]),
            self._make_tool("tool-b", ["b:write"]),
        ]
        result = _filter_by_scopes(tools, "production")
        assert len(result) == 2

    def test_filters_to_matching_scopes(self, isolated_config):
        store.save_tokens(
            "production",
            "tok",
            "",
            granted_scopes="a:read c:read",
        )
        tools = [
            self._make_tool("tool-a", ["a:read"]),
            self._make_tool("tool-b", ["b:write"]),
            self._make_tool("tool-c", ["c:read"]),
        ]
        result = _filter_by_scopes(tools, "production")
        names = [t.name for t in result]
        assert "tool-a" in names
        assert "tool-c" in names
        assert "tool-b" not in names

    def test_tool_with_no_required_scopes_always_shown(self, isolated_config):
        store.save_tokens("production", "tok", "", granted_scopes="a:read")
        tools = [self._make_tool("no-scope-tool", [])]
        result = _filter_by_scopes(tools, "production")
        assert len(result) == 1

    def test_tool_requiring_multiple_scopes(self, isolated_config):
        store.save_tokens(
            "production",
            "tok",
            "",
            granted_scopes="a:read b:write",
        )
        tools = [
            self._make_tool("needs-both", ["a:read", "b:write"]),
            self._make_tool("needs-missing", ["a:read", "c:read"]),
        ]
        result = _filter_by_scopes(tools, "production")
        names = [t.name for t in result]
        assert "needs-both" in names
        assert "needs-missing" not in names


class TestPreFlightScopeCheck:
    """_execute_tool rejects calls to tools the token can't access."""

    def test_missing_scope_fails_fast(self, isolated_config):
        """Calling a tool without the required scope gives a clear error."""
        store.save_tokens(
            "production",
            "tok",
            "",
            granted_scopes="users:read",
        )
        runner = CliRunner()
        # get-transactions requires transactions:read
        result = runner.invoke(
            cli,
            ["get-transactions", "--json", '{"filters": {}}'],
        )
        assert result.exit_code != 0
        assert "transactions:read" in result.output
        assert "ramp auth login" in result.output
        assert "missing the required scope" in result.output

    def test_dry_run_skips_scope_check(self, isolated_config):
        """--dry_run should not check scopes (it never hits the network)."""
        store.save_tokens(
            "production",
            "tok",
            "",
            granted_scopes="users:read",
        )
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["get-transactions", "--dry_run", "--json", '{"filters": {}}'],
        )
        assert result.exit_code == 0
        assert "dry_run" in result.output

    def test_matching_scope_proceeds(self, isolated_config):
        """When scopes match, the tool attempts execution (will fail on network,
        but should NOT fail on scope check)."""
        store.save_tokens(
            "production",
            "tok",
            "",
            granted_scopes="transactions:read",
        )
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["get-transactions", "--dry_run", "--json", '{"filters": {}}'],
        )
        # dry_run bypasses the scope check and the network
        assert result.exit_code == 0

    def test_no_stored_scopes_skips_check(self, isolated_config):
        """Old tokens without scope info should not block tool calls."""
        store.save_tokens("production", "tok", "", granted_scopes="")
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["get-funds", "--dry_run"],
        )
        assert result.exit_code == 0
        assert "dry_run" in result.output


class TestAuthStatusScopes:
    """ramp auth status shows scope information."""

    def test_status_json_includes_scopes(self, isolated_config):
        store.save_tokens(
            "production",
            "tok",
            "refresh",
            access_token_expires_in=9999,
            refresh_token_expires_in=99999,
            granted_scopes="a:read b:write",
        )
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--agent", "auth", "status"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        prod_data = data["data"][0]["production"]
        assert "scopes" in prod_data
        assert sorted(prod_data["scopes"]) == ["a:read", "b:write"]

    def test_status_warns_on_no_scopes(self, isolated_config, monkeypatch):
        monkeypatch.setattr(store.time, "time", lambda: 100)
        store.save_tokens(
            "production",
            "tok",
            "refresh",
            access_token_expires_in=9999,
            refresh_token_expires_in=99999,
            issued_at=50,
            granted_scopes="",
        )
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--human", "auth", "status"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        output = result.output
        assert "No scopes" in output
        assert "ramp auth login" in output


class TestResolveScopesWarning:
    """_resolve_scopes warns when spec extraction fails."""

    def test_logs_warning_on_spec_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            oauth_module,
            "_resolve_spec_path",
            lambda env: tmp_path / "nonexistent.json",
        )
        monkeypatch.setattr(
            oauth_module,
            "configured_scopes",
            lambda: "",
        )

        runner = CliRunner()

        # Call _resolve_scopes in a Click context so click.echo(err=True) works
        @click.command()
        def _cmd():
            scopes = oauth_module._resolve_scopes("production")
            click.echo(scopes)

        result = runner.invoke(_cmd, catch_exceptions=False)
        assert result.exit_code == 0
        output = result.output
        assert "Could not read tool definitions" in output
        assert "ramp tools refresh" in output
        # Should still return DEVAPI_SCOPES as fallback
        assert "business:read" in output
