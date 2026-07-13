"""Tests for ramp auth businesses command."""

from __future__ import annotations

import json
from unittest.mock import patch

from click.testing import CliRunner

from ramp_cli.auth import store
from ramp_cli.main import cli


def _invoke(args):
    runner = CliRunner()
    return runner.invoke(cli, args, catch_exceptions=False)


class TestAuthBusinesses:
    def test_requires_authentication(self, isolated_config):
        result = CliRunner().invoke(cli, ["auth", "businesses"])
        assert result.exit_code != 0
        assert "Not authenticated" in result.output

    @patch("ramp_cli.commands.auth.fetch_business_memberships")
    @patch("ramp_cli.commands.auth.fetch_token_info")
    def test_agent_json_lists_memberships(
        self, mock_token_info, mock_memberships, isolated_config
    ):
        store.save_tokens(
            "production",
            "tok",
            "refresh",
            access_token_expires_in=9999,
            refresh_token_expires_in=99999,
            granted_scopes="users:read",
        )
        mock_token_info.return_value = {"business_id": "biz-a", "user_id": "user-a"}
        mock_memberships.return_value = [
            {
                "business_id": "biz-a",
                "id": "user-a",
                "email": "you@example.com",
                "first_name": "Ada",
                "last_name": "Lovelace",
                "department": {"name": "Engineering"},
                "location_name": "NYC",
            },
            {
                "business_id": "biz-b",
                "id": "user-b",
                "email": "you@example.com",
                "first_name": "Ada",
                "last_name": "Lovelace",
                "department": None,
                "location_name": None,
            },
        ]

        result = _invoke(["--agent", "--env", "production", "auth", "businesses"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        payload = data["data"][0]
        assert payload["membership_count"] == 2
        assert payload["active_business_id"] == "biz-a"
        assert payload["memberships"][0]["is_active_session"] is True
        assert payload["memberships"][1]["is_active_session"] is False
        assert "switch_hint" in payload

    @patch("ramp_cli.commands.auth.fetch_business_memberships")
    @patch("ramp_cli.commands.auth.fetch_token_info")
    def test_human_output_shows_switch_hint(
        self, mock_token_info, mock_memberships, isolated_config
    ):
        store.save_tokens(
            "production",
            "tok",
            "refresh",
            access_token_expires_in=9999,
            refresh_token_expires_in=99999,
            granted_scopes="users:read",
        )
        mock_token_info.return_value = {"business_id": "biz-a"}
        mock_memberships.return_value = [
            {
                "business_id": "biz-a",
                "id": "u1",
                "email": "a@example.com",
                "first_name": "A",
                "last_name": "One",
                "department": None,
                "location_name": None,
            },
            {
                "business_id": "biz-b",
                "id": "u2",
                "email": "b@example.com",
                "first_name": "B",
                "last_name": "Two",
                "department": None,
                "location_name": None,
            },
        ]

        result = _invoke(["--human", "--env", "production", "auth", "businesses"])
        assert result.exit_code == 0
        assert "active session" in result.output
        assert "ramp auth login" in result.output
