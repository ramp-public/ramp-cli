"""Tests for OAuth scope persistence, tool discovery, and API errors."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import click
import pytest
from click.testing import CliRunner

from ramp_cli.auth import oauth as oauth_module
from ramp_cli.auth import store
from ramp_cli.config import settings
from ramp_cli.errors import ApiError
from ramp_cli.main import cli
from ramp_cli.tools import registry
from ramp_cli.tools.availability import AvailabilitySnapshot, ToolAvailability
from ramp_cli.tools.commands import _scope_error_hint
from ramp_cli.tools.parser import ToolDef


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

    def test_external_token_makes_stored_scopes_unknown(
        self, isolated_config, monkeypatch
    ):
        store.save_tokens(
            "production", "stored", "refresh", granted_scopes="cards:read_agentic"
        )
        monkeypatch.setenv("RAMP_ACCESS_TOKEN", "external")

        assert store.get_known_granted_scopes("production") is None

    def test_replacing_token_can_clear_stored_scopes(self, isolated_config):
        store.save_tokens(
            "production", "old", "refresh", granted_scopes="cards:read_agentic"
        )

        store.save_tokens(
            "production",
            "replacement",
            "",
            clear_granted_scopes=True,
        )

        assert store.get_granted_scopes("production") == set()
        assert store.get_known_granted_scopes("production") is None


class TestScopeIndependentDiscovery:
    """Tool discovery does not depend on locally persisted token scopes."""

    def _make_tool(self, name: str, scopes: list[str] | None = None) -> ToolDef:
        return ToolDef(
            name=name,
            path=f"/developer/v1/agent-tools/{name}",
            http_method="post",
            summary=name,
            description=name,
            required_scopes=scopes or [],
        )

    def test_all_tools_shown_when_no_scopes_stored(self, isolated_config, monkeypatch):
        tools = [
            self._make_tool("tool-a", ["a:read"]),
            self._make_tool("tool-b", ["b:write"]),
        ]
        monkeypatch.setattr(registry, "list_tool_defs", lambda env: tools)

        categories = registry.list_categories("production")

        assert [tool.name for tool in categories["general"]] == [
            "tool-a",
            "tool-b",
        ]

    def test_tools_with_missing_scopes_remain_visible(
        self, isolated_config, monkeypatch
    ):
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
        monkeypatch.setattr(registry, "list_tool_defs", lambda env: tools)

        categories = registry.list_categories("production")

        assert [tool.name for tool in categories["general"]] == [
            "tool-a",
            "tool-b",
            "tool-c",
        ]


class TestServerScopeErrors:
    """Core authorizes tool calls and the CLI contextualizes scope errors."""

    @staticmethod
    def _tool() -> ToolDef:
        return ToolDef(
            name="get-attention-feed",
            path="/developer/v1/agent-tools/get-attention-feed",
            http_method="post",
            summary="Get attention feed",
            description="Get attention feed",
            category="tasks",
            alias="list",
            params=[],
            required_scopes=["tasks:read", "users:read"],
        )

    @staticmethod
    def _scope_error() -> ApiError:
        return ApiError(
            403,
            json.dumps(
                {
                    "error_v2": {
                        "error_code": "DEVELOPER_7100",
                        "message": "Insufficient scope",
                    }
                }
            ),
        )

    def test_core_scope_error_has_contextual_recovery_guidance(
        self, isolated_config, monkeypatch
    ):
        store.save_tokens(
            "sandbox",
            "tok",
            "",
            granted_scopes="users:read",
        )
        client = MagicMock()
        client.post.side_effect = self._scope_error()
        tool = self._tool()
        monkeypatch.setattr("ramp_cli.main.maybe_sync", lambda env: None)
        monkeypatch.setattr("ramp_cli.tools.commands.maybe_sync", lambda env: None)
        monkeypatch.setattr(
            "ramp_cli.main.list_categories", lambda env: {"tasks": [tool]}
        )
        monkeypatch.setattr("ramp_cli.tools.commands.RampClient", lambda env: client)

        result = CliRunner().invoke(cli, ["--env", "sandbox", "tasks", "list"])

        assert result.exit_code != 0
        client.post.assert_called_once()
        message = str(result.exception)
        assert "DEVELOPER_7100" not in message
        assert "get-attention-feed" in message
        assert "insufficient OAuth scope" in message
        assert "tasks:read" not in message
        assert "users:read" not in message
        assert "ramp --env sandbox tools refresh" in message
        assert "ramp --env sandbox auth login" in message

    def _invoke_with_availability(self, monkeypatch, error, snapshot):
        store.save_tokens("sandbox", "tok", "", granted_scopes="users:read")
        client = MagicMock()
        client.post.side_effect = error
        tool = self._tool()
        monkeypatch.setattr("ramp_cli.main.maybe_sync", lambda env: None)
        monkeypatch.setattr("ramp_cli.tools.commands.maybe_sync", lambda env: None)
        monkeypatch.setattr(
            "ramp_cli.main.list_categories", lambda env: {"tasks": [tool]}
        )
        monkeypatch.setattr("ramp_cli.tools.commands.RampClient", lambda env: client)
        monkeypatch.setattr(
            "ramp_cli.tools.commands.fetch_availability",
            lambda env: snapshot,
        )
        return CliRunner().invoke(cli, ["--env", "sandbox", "tasks", "list"])

    def test_generic_error_gets_availability_hint_when_tool_unavailable(
        self, isolated_config, monkeypatch
    ):
        snapshot = AvailabilitySnapshot(
            content_hash="sha256:abc",
            entries={
                ("get-attention-feed", "POST"): ToolAvailability(
                    available=False,
                    unavailable_reasons=("disabled_for_business",),
                )
            },
        )

        result = self._invoke_with_availability(
            monkeypatch, ApiError(403, "Forbidden"), snapshot
        )

        assert result.exit_code != 0
        message = str(result.exception)
        assert "currently unavailable" in message
        assert "not enabled for your business" in message

    @pytest.mark.parametrize(
        "snapshot",
        [
            # No availability data at all.
            None,
            # The tool is available, so the 4xx is about something else.
            AvailabilitySnapshot(
                content_hash="sha256:abc",
                entries={
                    ("get-attention-feed", "POST"): ToolAvailability(available=True)
                },
            ),
        ],
    )
    def test_generic_error_is_unchanged_without_unavailable_entry(
        self, isolated_config, monkeypatch, snapshot
    ):
        result = self._invoke_with_availability(
            monkeypatch, ApiError(403, "Forbidden"), snapshot
        )

        assert result.exit_code != 0
        message = str(result.exception)
        assert "currently unavailable" not in message
        assert "Forbidden" in message

    def test_dry_run_skips_scope_check(self, isolated_config):
        store.save_tokens(
            "production",
            "tok",
            "",
            granted_scopes="users:read",
        )
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "get-transactions",
                "--dry_run",
                "--rationale",
                "test",
                "--transactions_to_retrieve",
                "my_transactions",
                "--json",
                '{"filters": {"filters": []}}',
            ],
        )
        assert result.exit_code == 0
        assert "dry_run" in result.output

    def test_unknown_grants_do_not_claim_exact_missing_scopes(self, isolated_config):
        message = _scope_error_hint(self._tool(), "sandbox")

        assert "insufficient OAuth scope" in message
        assert "tasks:read" not in message

    def test_custom_scope_override_is_called_out(self, isolated_config, monkeypatch):
        cfg = settings.load()
        cfg.scopes = "users:read"
        settings.save(cfg)
        client = MagicMock()
        client.post.side_effect = self._scope_error()
        tool = self._tool()
        monkeypatch.setattr("ramp_cli.main.maybe_sync", lambda env: None)
        monkeypatch.setattr("ramp_cli.tools.commands.maybe_sync", lambda env: None)
        monkeypatch.setattr(
            "ramp_cli.main.list_categories", lambda env: {"tasks": [tool]}
        )
        monkeypatch.setattr("ramp_cli.tools.commands.RampClient", lambda env: client)

        result = CliRunner().invoke(cli, ["--env", "sandbox", "tasks", "list"])

        assert result.exit_code != 0
        message = str(result.exception)
        assert "top-level custom `scopes` override" in message
        assert "cached tool definition: tasks:read" in message

    def test_custom_scope_override_superset_is_not_blamed(self, isolated_config):
        cfg = settings.load()
        cfg.scopes = "tasks:read users:read"
        settings.save(cfg)

        message = _scope_error_hint(self._tool(), "sandbox")

        assert "custom `scopes` override" not in message

    def test_external_token_gets_external_token_recovery(
        self, isolated_config, monkeypatch
    ):
        cfg = settings.load()
        cfg.scopes = "users:read"
        settings.save(cfg)
        monkeypatch.setenv("RAMP_ACCESS_TOKEN", "external")

        message = _scope_error_hint(self._tool(), "sandbox")

        assert "`RAMP_ACCESS_TOKEN` has insufficient OAuth scope" in message
        assert "overrides credentials saved by `ramp auth login`" in message
        assert "unset RAMP_ACCESS_TOKEN" in message
        assert "custom `scopes` override" not in message

    def test_stale_cached_scope_is_not_reported_as_missing(self, isolated_config):
        error = ApiError(
            403,
            json.dumps(
                {
                    "error_v2": {
                        "error_code": "DEVELOPER_7100",
                        "message": "Missing newly_required:read",
                    }
                }
            ),
            contextual_hint=_scope_error_hint(self._tool(), "sandbox"),
        )

        message = str(error)
        assert "Missing newly_required:read" in message
        assert "tasks:read" not in message

    def test_non_scope_api_error_is_unchanged(self, isolated_config, monkeypatch):
        error = ApiError(
            403,
            json.dumps(
                {
                    "error_v2": {
                        "error_code": "CUSTOMER_7004",
                        "message": "Business not authorized",
                    }
                }
            ),
        )
        client = MagicMock()
        client.post.side_effect = error
        tool = self._tool()
        monkeypatch.setattr("ramp_cli.main.maybe_sync", lambda env: None)
        monkeypatch.setattr("ramp_cli.tools.commands.maybe_sync", lambda env: None)
        monkeypatch.setattr(
            "ramp_cli.main.list_categories", lambda env: {"tasks": [tool]}
        )
        monkeypatch.setattr("ramp_cli.tools.commands.RampClient", lambda env: client)

        result = CliRunner().invoke(cli, ["--env", "sandbox", "tasks", "list"])

        assert result.exit_code != 0
        message = str(result.exception)
        assert "Business not authorized" in message
        assert "tools refresh" not in message
        assert "auth login" not in message

    def test_non_403_scope_code_does_not_get_auth_guidance(
        self, isolated_config, monkeypatch
    ):
        error = ApiError(
            404,
            json.dumps(
                {
                    "error_v2": {
                        "error_code": "DEVELOPER_7100",
                        "message": "Resource not found",
                    }
                }
            ),
        )

        result = self._invoke_with_availability(monkeypatch, error, None)

        assert result.exit_code != 0
        message = str(result.exception)
        assert "Resource not found" in message
        assert "OAuth scope" not in message
        assert "auth login" not in message


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
        # The bundled spec still supplies scopes when the env cache fails.
        assert "users:read" in output
        assert "business:read" not in output
