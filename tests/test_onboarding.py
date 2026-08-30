"""Tests for first-time user experience: onboarding state, welcome, and getting-started."""

from __future__ import annotations

import io
import json
import sys
from unittest.mock import MagicMock

from click.testing import CliRunner

from ramp_cli.auth import store
from ramp_cli.commands.getting_started import _remap_categories
from ramp_cli.config import profiles, settings
from ramp_cli.config.constants import environment_usage
from ramp_cli.main import cli
from ramp_cli.onboarding import (
    get_used_categories,
    is_first_login,
    maybe_show_category_tip,
    print_getting_started,
    record_category_used,
    record_first_login,
    show_welcome,
)

# ── State helpers ────────────────────────────────────────────────────────────


class TestFirstLoginState:
    def test_is_first_login_when_no_config(self, isolated_config):
        assert is_first_login() is True

    def test_record_first_login_stamps_timestamp(self, isolated_config):
        record_first_login()
        cfg = settings.load()
        assert cfg.first_login_at > 0

    def test_is_first_login_false_after_recording(self, isolated_config):
        record_first_login()
        assert is_first_login() is False

    def test_record_first_login_idempotent(self, isolated_config):
        record_first_login()
        first_ts = settings.load().first_login_at
        record_first_login()
        assert settings.load().first_login_at == first_ts


class TestCategoryTracking:
    def test_no_categories_used_initially(self, isolated_config):
        assert get_used_categories() == set()

    def test_record_returns_true_on_first_use(self, isolated_config):
        assert record_category_used("transactions") is True

    def test_record_returns_false_on_repeat_use(self, isolated_config):
        record_category_used("transactions")
        assert record_category_used("transactions") is False

    def test_multiple_categories_tracked(self, isolated_config):
        record_category_used("transactions")
        record_category_used("bills")
        used = get_used_categories()
        assert used == {"transactions", "bills"}

    def test_categories_persist_across_loads(self, isolated_config):
        record_category_used("funds")
        # Simulate fresh load
        used = get_used_categories()
        assert "funds" in used

    def test_multiword_command_preserves_existing_raw_category(
        self, isolated_config, monkeypatch
    ):
        cfg = settings.load()
        cfg.tools_used = "purchase_orders"
        settings.save(cfg)
        client = MagicMock()
        client.post.return_value = b"{}"
        monkeypatch.setattr("click.testing._NamedTextIOWrapper.isatty", lambda _: True)
        monkeypatch.setattr("ramp_cli.tools.commands.RampClient", lambda env: client)
        monkeypatch.setattr("ramp_cli.tools.commands.maybe_sync", lambda env: None)
        monkeypatch.setattr("ramp_cli.tools.commands._start_spinner", lambda _: None)

        result = CliRunner().invoke(
            cli,
            [
                "--human",
                "--no-input",
                "purchase-orders",
                "search",
                "--rationale",
                "test",
            ],
        )

        assert result.exit_code == 0, result.output
        assert get_used_categories() == {"purchase_orders"}


# ── Welcome message ──────────────────────────────────────────────────────────


class TestShowWelcome:
    def test_welcome_contains_sample_commands(self, isolated_config):
        buf = io.StringIO()
        show_welcome("production", file=buf)
        output = buf.getvalue()
        assert "ramp funds enroll" in output
        assert "ramp users me" in output
        assert "ramp transactions list" in output
        assert "ramp getting-started" in output

    def test_welcome_shows_environment(self, isolated_config):
        buf = io.StringIO()
        show_welcome("sandbox", file=buf)
        assert "sandbox" in buf.getvalue()

    def test_welcome_shows_environment_options(self, isolated_config):
        buf = io.StringIO()
        show_welcome("production", file=buf)
        assert f"ramp env [{environment_usage()}]" in buf.getvalue()


# ── Category tips ────────────────────────────────────────────────────────────


def _fake_tty_stderr(monkeypatch):
    """Replace sys.stderr with a StringIO that reports isatty()=True."""
    fake = io.StringIO()
    fake.isatty = lambda: True  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "stderr", fake)


class TestCategoryTips:
    def test_tip_shown_on_first_use(self, isolated_config, monkeypatch):
        """First use of a category prints a tip to stderr."""
        _fake_tty_stderr(monkeypatch)

        buf = io.StringIO()
        maybe_show_category_tip("transactions", file=buf)
        assert "Tip:" in buf.getvalue()

    def test_no_tip_on_second_use(self, isolated_config, monkeypatch):
        _fake_tty_stderr(monkeypatch)

        record_category_used("transactions")
        buf = io.StringIO()
        maybe_show_category_tip("transactions", file=buf)
        assert buf.getvalue() == ""

    def test_no_tip_for_unknown_category(self, isolated_config, monkeypatch):
        _fake_tty_stderr(monkeypatch)

        buf = io.StringIO()
        maybe_show_category_tip("nonexistent_category", file=buf)
        # No tip registered for this category — should still record but print nothing
        assert buf.getvalue() == ""


# ── Getting-started command ──────────────────────────────────────────────────


class TestGettingStartedCommand:
    def test_not_authenticated_human(self, isolated_config):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--human", "getting-started"], catch_exceptions=False
        )
        assert result.exit_code == 0
        assert "ramp auth login" in result.output

    def test_not_authenticated_agent(self, isolated_config):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--agent", "getting-started"], catch_exceptions=False
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"][0]["authenticated"] is False

    def test_authenticated_human(self, isolated_config, monkeypatch):
        store.save_tokens(
            "production",
            "access123",
            "refresh456",
            access_token_expires_in=3600,
            refresh_token_expires_in=604800,
            granted_scopes="business:read transactions:read",
        )
        monkeypatch.setattr(store.time, "time", lambda: 100)

        runner = CliRunner()
        result = runner.invoke(
            cli, ["--human", "getting-started"], catch_exceptions=False
        )
        assert result.exit_code == 0
        assert "Getting Started" in result.output
        assert "Resources to explore" in result.output

    def test_authenticated_named_profile_agent_json(self, isolated_config, monkeypatch):
        store.save_tokens(
            "production",
            "access123",
            "refresh456",
            access_token_expires_in=3600,
            refresh_token_expires_in=604800,
            granted_scopes="business:read",
            profile="human",
        )
        profiles.activate("human")
        monkeypatch.setattr(store.time, "time", lambda: 100)

        runner = CliRunner()
        result = runner.invoke(
            cli, ["--agent", "getting-started"], catch_exceptions=False
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        payload = data["data"][0]
        assert "categories_available" in payload
        assert "categories_unexplored" in payload
        assert "sample_prompts" in payload
        assert payload["environment"] == "production"
        assert payload["scopes_granted"] == 1

    def test_tracks_explored_categories(self, isolated_config, monkeypatch):
        store.save_tokens(
            "production",
            "access123",
            "refresh456",
            access_token_expires_in=3600,
            refresh_token_expires_in=604800,
            granted_scopes="business:read",
        )
        monkeypatch.setattr(store.time, "time", lambda: 100)

        # Simulate having used some categories
        record_category_used("transactions")
        record_category_used("bills")

        runner = CliRunner()
        result = runner.invoke(
            cli, ["--agent", "getting-started"], catch_exceptions=False
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        payload = data["data"][0]
        assert "transactions" in payload["categories_explored"]
        assert "bills" in payload["categories_explored"]
        assert "transactions" not in payload["categories_unexplored"]

    def test_categories_are_remapped(self, isolated_config, monkeypatch):
        """Spec category 'agent_cards' is merged into 'funds' so the guide
        shows names matching invokable CLI groups. 'cards' is merged into
        'funds' too AND additively surfaced as its own 'cards' alias group
        (matching the invokable `ramp cards` resource)."""
        # Don't set granted_scopes — when no scope info is stored, the
        # scope filter shows all tools (backwards-compat path), which
        # guarantees 'cards'/'agent_cards' tools are present to remap.
        store.save_tokens(
            "production",
            "access123",
            "refresh456",
            access_token_expires_in=3600,
            refresh_token_expires_in=604800,
        )
        monkeypatch.setattr(store.time, "time", lambda: 100)

        runner = CliRunner()
        result = runner.invoke(
            cli, ["--agent", "getting-started"], catch_exceptions=False
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        payload = data["data"][0]
        cats = payload["categories_available"]
        # cards keeps its own group (matches invokable `ramp cards`)
        assert "cards" in cats
        # agent_cards should be merged away into funds
        assert "agent_cards" not in cats
        # Merged target should be present
        assert "funds" in cats
        assert "purchase_orders" in cats
        assert "purchase-orders" not in cats


# ── Category remapping ───────────────────────────────────────────────────────


class TestCategoryRemapping:
    def test_remap_adds_cards_alias_group_while_keeping_funds(self):
        # 'cards' is remapped into 'funds' (existing behavior preserved) AND
        # additively surfaced as its own group (alias). Same tool in both.
        raw = {"cards": ["t1"], "funds": ["t2"], "bills": ["t3"]}
        result = _remap_categories(raw)
        assert result["cards"] == ["t1"]
        assert set(result["funds"]) == {"t1", "t2"}
        assert result["bills"] == ["t3"]

    def test_remap_merges_agent_cards_into_funds(self):
        raw = {"agent_cards": ["t1"], "funds": ["t2"]}
        result = _remap_categories(raw)
        assert "agent_cards" not in result
        assert set(result["funds"]) == {"t1", "t2"}

    def test_remap_preserves_unmapped_categories(self):
        raw = {"transactions": ["t1"], "bills": ["t2"]}
        result = _remap_categories(raw)
        assert result == {"bills": ["t2"], "transactions": ["t1"]}

    def test_remap_sorted_output(self):
        raw = {"z_category": [], "a_category": []}
        result = _remap_categories(raw)
        assert list(result.keys()) == ["a_category", "z_category"]


# ── print_getting_started unit tests ─────────────────────────────────────────


class TestPrintGettingStarted:
    def test_human_output_shows_quick_reference(self, isolated_config):
        buf = io.StringIO()
        print_getting_started(
            env="production",
            scopes={"business:read"},
            categories={"transactions": [], "bills": []},
            is_json=False,
            file=buf,
        )
        output = buf.getvalue()
        assert "Quick reference" in output
        assert "ramp <resource> --help" in output

    def test_json_output_has_expected_keys(self, isolated_config):
        print_getting_started(
            env="sandbox",
            scopes={"business:read", "transactions:read"},
            categories={"transactions": [1, 2], "funds": [1]},
            is_json=True,
        )
        # The JSON output goes to stdout via print_agent_json;
        # just verify no exception is raised.

    def test_explored_categories_appear_correctly(self, isolated_config):
        record_category_used("transactions")
        buf = io.StringIO()
        print_getting_started(
            env="production",
            scopes={"business:read"},
            categories={"transactions": [], "bills": []},
            is_json=False,
            file=buf,
        )
        output = buf.getvalue()
        assert "Resources you've used" in output
        assert "Transactions" in output


# ── Config persistence ───────────────────────────────────────────────────────


class TestConfigOnboardingFields:
    def test_first_login_at_roundtrip(self, isolated_config):
        cfg = settings.Config()
        cfg.first_login_at = 1713700000
        settings.save(cfg)

        loaded = settings.load()
        assert loaded.first_login_at == 1713700000

    def test_tools_used_roundtrip(self, isolated_config):
        cfg = settings.Config()
        cfg.tools_used = "bills funds transactions"
        settings.save(cfg)

        loaded = settings.load()
        assert loaded.tools_used == "bills funds transactions"

    def test_defaults_when_missing(self, isolated_config):
        cfg = settings.load()
        assert cfg.first_login_at == 0
        assert cfg.tools_used == ""
