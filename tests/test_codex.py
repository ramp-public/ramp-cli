"""Tests for ramp codex compaction (Codex config.toml compaction settings)."""

from __future__ import annotations

import contextlib
import json
import os
import tomllib
from pathlib import Path

from click.testing import CliRunner

import ramp_cli.commands.codex as codex_module
import ramp_cli.commands.router as router_module
from ramp_cli.main import cli

EXISTING_CONFIG = """\
# my codex config
model = "gpt-5"
approval_policy = "never"

[model_providers.foo]
name = "foo"  # keep me

[profiles.original]
model = "gpt-4.1"
"""


def _config_path() -> Path:
    return Path(os.environ["CODEX_HOME"]) / "config.toml"


def _write_config(content: str) -> Path:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _agent_payload(result) -> dict:
    envelope = json.loads(result.output)
    assert envelope["schema_version"] == "1.0"
    return envelope["data"][0]


class TestShow:
    def test_show_when_nothing_configured(self):
        result = CliRunner().invoke(cli, ["codex", "compaction", "--agent"])
        assert result.exit_code == 0, result.output
        payload = _agent_payload(result)
        assert payload == {
            "path": str(_config_path()),
            "model_auto_compact_token_limit": None,
            "model_auto_compact_token_limit_scope": None,
            "model_context_window": None,
        }

    def test_show_human_reads_existing_values(self):
        _write_config("model_auto_compact_token_limit = 150000\n" + EXISTING_CONFIG)
        result = CliRunner().invoke(cli, ["codex", "compaction", "--human"])
        assert result.exit_code == 0, result.output
        assert f"Codex config: {_config_path()}" in result.output
        assert "model_auto_compact_token_limit = 150000" in result.output
        assert "model_auto_compact_token_limit_scope = (not set)" in result.output
        assert "model_context_window = (not set)" in result.output

    def test_show_ignores_table_with_same_name(self):
        _write_config("[model_auto_compact_token_limit]\nvalue = 1\n")
        result = CliRunner().invoke(cli, ["codex", "compaction", "--agent"])
        assert result.exit_code == 0, result.output
        assert _agent_payload(result)["model_auto_compact_token_limit"] is None


class TestSet:
    def test_set_all_values_preserves_existing_config(self):
        path = _write_config(EXISTING_CONFIG)
        result = CliRunner().invoke(
            cli,
            [
                "codex",
                "compaction",
                "--limit",
                "150000",
                "--scope",
                "total",
                "--context-window",
                "272000",
                "--agent",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = _agent_payload(result)
        assert payload["model_auto_compact_token_limit"] == 150000
        assert payload["model_auto_compact_token_limit_scope"] == "total"
        assert payload["model_context_window"] == 272000
        assert payload["updated"] is True
        assert payload["dry_run"] is False
        assert "warnings" not in payload

        content = path.read_text()
        data = tomllib.loads(content)
        assert data["model_auto_compact_token_limit"] == 150000
        assert data["model_auto_compact_token_limit_scope"] == "total"
        assert data["model_context_window"] == 272000
        # Everything that was already there survives, comments included.
        assert data["model"] == "gpt-5"
        assert data["approval_policy"] == "never"
        assert data["model_providers"]["foo"]["name"] == "foo"
        assert data["profiles"]["original"]["model"] == "gpt-4.1"
        assert "# my codex config" in content
        assert "# keep me" in content
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_set_creates_config_when_missing(self):
        path = _config_path()
        assert not path.exists()
        result = CliRunner().invoke(
            cli, ["codex", "compaction", "--limit", "100000", "--human"]
        )
        assert result.exit_code == 0, result.output
        assert f"Updated {path}" in result.output
        assert tomllib.loads(path.read_text()) == {
            "model_auto_compact_token_limit": 100000
        }

    def test_set_one_key_keeps_the_others(self):
        _write_config(
            "model_auto_compact_token_limit = 100000\n"
            'model_auto_compact_token_limit_scope = "total"\n'
        )
        result = CliRunner().invoke(
            cli, ["codex", "compaction", "--limit", "120000", "--agent"]
        )
        assert result.exit_code == 0, result.output
        data = tomllib.loads(_config_path().read_text())
        assert data == {
            "model_auto_compact_token_limit": 120000,
            "model_auto_compact_token_limit_scope": "total",
        }

    def test_set_replaces_existing_value_without_duplicates(self):
        _write_config("model_auto_compact_token_limit = 100000\n")
        result = CliRunner().invoke(
            cli, ["codex", "compaction", "--limit", "120000", "--agent"]
        )
        assert result.exit_code == 0, result.output
        content = _config_path().read_text()
        assert content.count("model_auto_compact_token_limit") == 1

    def test_set_same_values_reports_no_changes(self):
        path = _write_config("model_auto_compact_token_limit = 100000\n")
        before = path.read_text()
        result = CliRunner().invoke(
            cli, ["codex", "compaction", "--limit", "100000", "--human"]
        )
        assert result.exit_code == 0, result.output
        assert f"No changes needed in {path}" in result.output
        assert path.read_text() == before

    def test_scope_value_is_validated(self):
        result = CliRunner().invoke(
            cli, ["codex", "compaction", "--scope", "everything"]
        )
        assert result.exit_code != 0
        assert "Invalid value for '--scope'" in result.output

    def test_limit_must_be_positive(self):
        result = CliRunner().invoke(cli, ["codex", "compaction", "--limit", "0"])
        assert result.exit_code != 0
        assert "Invalid value for '--limit'" in result.output

    def test_invalid_existing_toml_is_reported(self):
        _write_config("model = [broken\n")
        result = CliRunner().invoke(cli, ["codex", "compaction", "--limit", "100000"])
        assert result.exit_code != 0
        assert "Could not read Codex config" in result.output


class TestSurprisingLayouts:
    def test_quoted_key_is_replaced_without_duplicates(self):
        _write_config('"model_auto_compact_token_limit" = 100000\n' + EXISTING_CONFIG)
        result = CliRunner().invoke(
            cli, ["codex", "compaction", "--limit", "120000", "--agent"]
        )
        assert result.exit_code == 0, result.output
        content = _config_path().read_text()
        assert content.count("model_auto_compact_token_limit") == 1
        assert tomllib.loads(content)["model_auto_compact_token_limit"] == 120000

    def test_quoted_key_is_cleared(self):
        _write_config("'model_auto_compact_token_limit' = 100000\n" + EXISTING_CONFIG)
        result = CliRunner().invoke(cli, ["codex", "compaction", "--clear", "--agent"])
        assert result.exit_code == 0, result.output
        data = tomllib.loads(_config_path().read_text())
        assert "model_auto_compact_token_limit" not in data
        assert data["model"] == "gpt-5"

    def test_setting_a_key_defined_as_a_table_is_rejected(self):
        path = _write_config(EXISTING_CONFIG + "[model_context_window]\nvalue = 1\n")
        before = path.read_text()
        result = CliRunner().invoke(
            cli, ["codex", "compaction", "--context-window", "272000"]
        )
        assert result.exit_code != 0
        assert "defines model_context_window as a table" in result.output
        assert path.read_text() == before

    def test_other_keys_can_be_set_around_a_colliding_table(self):
        path = _write_config("[model_context_window]\nvalue = 1\n")
        result = CliRunner().invoke(
            cli, ["codex", "compaction", "--limit", "100000", "--agent"]
        )
        assert result.exit_code == 0, result.output
        data = tomllib.loads(path.read_text())
        assert data["model_auto_compact_token_limit"] == 100000
        assert data["model_context_window"] == {"value": 1}

    def test_multiline_string_that_mimics_toml_is_refused_not_corrupted(self):
        path = _write_config(
            'notes = """\n[profiles.fake]\nmodel_auto_compact_token_limit = 999\n"""\n'
        )
        before = path.read_text()
        result = CliRunner().invoke(cli, ["codex", "compaction", "--limit", "100000"])
        assert result.exit_code != 0
        assert "Could not update Codex config" in result.output
        assert path.read_text() == before

    def test_plain_multiline_string_is_preserved(self):
        path = _write_config('notes = """\nhello\nworld\n"""\n' + EXISTING_CONFIG)
        result = CliRunner().invoke(
            cli, ["codex", "compaction", "--limit", "100000", "--agent"]
        )
        assert result.exit_code == 0, result.output
        data = tomllib.loads(path.read_text())
        assert data["notes"] == "hello\nworld\n"
        assert data["model_auto_compact_token_limit"] == 100000

    def test_write_rereads_config_under_the_lock(self, monkeypatch):
        """A concurrent rewrite between snapshot and lock is not lost."""
        path = _write_config("model = 'gpt-5'\n")
        original_locked = codex_module.codex_config_lock

        @contextlib.contextmanager
        def racing_locked(lock_path):
            with original_locked(lock_path):
                # Simulates another invocation winning the race: it rewrote
                # the file after this run's initial snapshot.
                path.write_text('model = "gpt-6"\nmodel_context_window = 5000\n')
                yield

        monkeypatch.setattr(codex_module, "codex_config_lock", racing_locked)
        result = CliRunner().invoke(
            cli, ["codex", "compaction", "--limit", "100000", "--agent"]
        )
        assert result.exit_code == 0, result.output
        data = tomllib.loads(path.read_text())
        assert data == {
            "model": "gpt-6",
            "model_context_window": 5000,
            "model_auto_compact_token_limit": 100000,
        }

    def test_compaction_shares_the_router_config_lock(self):
        """Every config.toml writer must serialize on the same lock."""
        assert codex_module.codex_config_lock is router_module.codex_config_lock
        result = CliRunner().invoke(
            cli, ["codex", "compaction", "--limit", "100000", "--agent"]
        )
        assert result.exit_code == 0, result.output
        assert (_config_path().parent / ".ramp-codex-config.lock").exists()


class TestWarnings:
    def test_limit_near_context_window_warns(self):
        result = CliRunner().invoke(
            cli,
            [
                "codex",
                "compaction",
                "--limit",
                "260000",
                "--context-window",
                "272000",
                "--agent",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = _agent_payload(result)
        assert any("~90%" in warning for warning in payload["warnings"])
        # The write still happens; Codex is the one that clamps.
        data = tomllib.loads(_config_path().read_text())
        assert data["model_auto_compact_token_limit"] == 260000

    def test_limit_near_existing_context_window_warns(self):
        _write_config("model_context_window = 100000\n")
        result = CliRunner().invoke(
            cli, ["codex", "compaction", "--limit", "95000", "--human"]
        )
        assert result.exit_code == 0, result.output
        assert "~90%" in result.stderr

    def test_scope_without_limit_warns(self):
        result = CliRunner().invoke(
            cli, ["codex", "compaction", "--scope", "body_after_prefix", "--agent"]
        )
        assert result.exit_code == 0, result.output
        payload = _agent_payload(result)
        assert any("has no effect" in warning for warning in payload["warnings"])


class TestClear:
    def test_clear_removes_only_compaction_keys(self):
        path = _write_config(
            "model_auto_compact_token_limit = 150000\n"
            'model_auto_compact_token_limit_scope = "body_after_prefix"\n'
            "model_context_window = 272000\n" + EXISTING_CONFIG
        )
        result = CliRunner().invoke(cli, ["codex", "compaction", "--clear", "--agent"])
        assert result.exit_code == 0, result.output
        payload = _agent_payload(result)
        assert payload["updated"] is True
        content = path.read_text()
        data = tomllib.loads(content)
        assert "model_auto_compact_token_limit" not in data
        assert "model_auto_compact_token_limit_scope" not in data
        assert "model_context_window" not in data
        assert data["model"] == "gpt-5"
        assert data["model_providers"]["foo"]["name"] == "foo"
        assert "# my codex config" in content

    def test_clear_when_nothing_set_is_a_no_op(self):
        path = _write_config(EXISTING_CONFIG)
        before = path.read_text()
        result = CliRunner().invoke(cli, ["codex", "compaction", "--clear", "--agent"])
        assert result.exit_code == 0, result.output
        assert _agent_payload(result)["updated"] is False
        assert path.read_text() == before

    def test_clear_rejects_set_options(self):
        result = CliRunner().invoke(
            cli, ["codex", "compaction", "--clear", "--limit", "100000"]
        )
        assert result.exit_code != 0
        assert "--clear cannot be combined" in result.output


class TestDryRun:
    def test_dry_run_does_not_write(self):
        path = _write_config(EXISTING_CONFIG)
        before = path.read_text()
        result = CliRunner().invoke(
            cli, ["codex", "compaction", "--limit", "150000", "--dry-run", "--agent"]
        )
        assert result.exit_code == 0, result.output
        payload = _agent_payload(result)
        assert payload["dry_run"] is True
        assert payload["model_auto_compact_token_limit"] == 150000
        assert path.read_text() == before

    def test_dry_run_human_output(self):
        result = CliRunner().invoke(
            cli, ["codex", "compaction", "--limit", "150000", "--dry-run", "--human"]
        )
        assert result.exit_code == 0, result.output
        assert f"Would update {_config_path()}" in result.output
        assert not _config_path().exists()

    def test_dry_run_without_changes_is_a_usage_error(self):
        result = CliRunner().invoke(cli, ["codex", "compaction", "--dry-run"])
        assert result.exit_code != 0
        assert "--dry-run requires a change" in result.output
