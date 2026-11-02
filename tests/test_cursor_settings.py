"""Tests for writing Cursor's local OpenAI override."""

import json
import sqlite3

import pytest
from click.testing import CliRunner

from ramp_cli.commands import cursor
from ramp_cli.commands import router as router_module
from ramp_cli.main import cli
from tests.test_router_cursor import _mock_cursor_setup

CIPHERTEXT = '{"type":"Buffer","data":[1]}'
PROMPTS: list[bool] = []


@pytest.fixture
def cursor_db(monkeypatch):
    monkeypatch.setattr(cursor, "is_running", lambda: False)
    PROMPTS.clear()

    def encrypt(key, prompt=True):
        PROMPTS.append(prompt)
        return CIPHERTEXT

    monkeypatch.setattr(cursor, "encrypt_with_cursor", encrypt)
    path = cursor.state_db_path()
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
        db.execute(
            "INSERT INTO ItemTable VALUES (?, ?)",
            (
                cursor._SETTINGS_KEY,
                json.dumps(
                    {
                        "openAIBaseUrl": "https://api.openai.com/v1",
                        "aiSettings": {"userAddedModels": ["mine", "gpt-5.6-sol"]},
                    }
                ),
            ),
        )
    return path


def _rows(path):
    with sqlite3.connect(path) as db:
        return dict(db.execute("SELECT key, value FROM ItemTable"))


def _settings(path):
    return json.loads(_rows(path)[cursor._SETTINGS_KEY])


def test_reconfigure_then_unconfigure_restores_the_original_settings(cursor_db):
    cursor.configure("router-key", "https://router.test/v1/", ["gpt-5.6-sol"])
    settings = _settings(cursor_db)
    assert settings["openAIBaseUrl"] == "https://router.test/v1"
    assert settings["useOpenAIKey"] is True
    assert _rows(cursor_db)[cursor._OPENAI_KEY_SECRET] == CIPHERTEXT

    # Signing in to another Cursor account turns the key off; configure again.
    settings["useOpenAIKey"] = False
    with sqlite3.connect(cursor_db) as db:
        db.execute(
            "UPDATE ItemTable SET value = ? WHERE key = ?",
            (json.dumps(settings), cursor._SETTINGS_KEY),
        )
    cursor.configure(
        "router-key", "https://router.test/v1", ["gpt-5.6-sol", "ramp-sonnet-5-5"]
    )
    assert _settings(cursor_db)["aiSettings"]["userAddedModels"] == [
        "mine",
        "gpt-5.6-sol",
        "ramp-sonnet-5-5",
    ]

    assert cursor.unconfigure() is True
    settings = _settings(cursor_db)
    assert settings["openAIBaseUrl"] == "https://api.openai.com/v1"
    assert "useOpenAIKey" not in settings
    assert settings["aiSettings"]["userAddedModels"] == ["mine", "gpt-5.6-sol"]
    assert cursor._OPENAI_KEY_SECRET not in _rows(cursor_db)
    assert cursor.unconfigure() is False


def test_configure_refuses_while_cursor_is_running(cursor_db, monkeypatch):
    monkeypatch.setattr(cursor, "is_running", lambda: True)
    with pytest.raises(cursor.CursorSettingsError, match="Quit Cursor"):
        cursor.configure("router-key", "https://router.test/v1", ["gpt-5.6-sol"])


def test_claude_models_are_listed_by_their_router_alias():
    names = cursor.model_names_to_add(
        ["claude-opus-5", "gpt-5.6-sol", "claude-sonnet-5-5"],
        {"ramp-sonnet-5-5": "claude-sonnet-5-5"},
    )
    assert names == ["gpt-5.6-sol", "ramp-sonnet-5-5"]


def test_configure_and_unconfigure_cursor_commands(cursor_db, monkeypatch, tmp_path):
    _mock_cursor_setup(monkeypatch, aliases={"ramp-opus-5": "claude-opus-5"})
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(router_module, "_can_prompt", lambda ctx, fmt: True)
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "cursor", "--api-key", "router-secret"],
    )
    assert result.exit_code == 0, result.output
    assert "Cursor Safe Storage" in result.output
    assert "Cursor: Router is set up" in result.output
    assert "clipboard" not in result.output
    assert _settings(cursor_db)["aiSettings"]["userAddedModels"] == [
        "mine",
        "gpt-5.6-sol",
        "ramp-opus-5",
    ]

    result = CliRunner().invoke(cli, ["--human", "router", "unconfigure", "cursor"])
    assert result.exit_code == 0, result.output
    assert "restored your previous settings for: Cursor" in result.output
    assert "undo it there" not in result.output
    assert _settings(cursor_db)["openAIBaseUrl"] == "https://api.openai.com/v1"


def test_unattended_configure_never_prompts(cursor_db, monkeypatch, tmp_path):
    _mock_cursor_setup(monkeypatch, aliases={})
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    monkeypatch.setattr("sys.platform", "darwin")
    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "--no-input",
            "router",
            "configure",
            "cursor",
            "--api-key",
            "router-secret",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Cursor Safe Storage" not in result.output
    assert PROMPTS == [False]


def test_json_configure_never_prompts(cursor_db, monkeypatch, tmp_path):
    _mock_cursor_setup(monkeypatch, aliases={})
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    monkeypatch.setattr("sys.platform", "darwin")
    result = CliRunner().invoke(
        cli,
        ["--agent", "router", "configure", "cursor", "--api-key", "router-secret"],
    )
    assert result.exit_code == 0, result.output
    assert "Cursor Safe Storage" not in result.output
    assert PROMPTS == [False]


def test_other_platforms_fall_back_without_probing(cursor_db, monkeypatch, tmp_path):
    _mock_cursor_setup(monkeypatch, aliases={})
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    monkeypatch.setattr("sys.platform", "win32")

    def probe():
        raise AssertionError("is_running must not run off macOS")

    monkeypatch.setattr(cursor, "is_running", probe)
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "cursor", "--api-key", "router-secret"],
    )
    assert result.exit_code == 0, result.output
    assert "Router is set up" not in result.output
    assert "Override OpenAI Base URL" in result.output


def test_a_failed_receipt_leaves_settings_untouched(cursor_db, monkeypatch):
    before = _settings(cursor_db)

    def fail(path, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(cursor.Path, "write_text", fail)
    with pytest.raises(cursor.CursorSettingsError, match="restore record"):
        cursor.configure("router-key", "https://router.test/v1", ["gpt-5.6-sol"])
    assert _settings(cursor_db) == before
    assert cursor._OPENAI_KEY_SECRET not in _rows(cursor_db)


def test_unconfigure_disables_only_models_configure_enabled(cursor_db):
    # "gpt-5.6-sol" was added by the user but left disabled.
    cursor.configure("router-key", "https://router.test/v1", ["gpt-5.6-sol"])
    assert "gpt-5.6-sol" in _settings(cursor_db)["aiSettings"]["modelOverrideEnabled"]
    assert cursor.unconfigure() is True
    ai_settings = _settings(cursor_db)["aiSettings"]
    assert ai_settings["userAddedModels"] == ["mine", "gpt-5.6-sol"]
    assert ai_settings["modelOverrideEnabled"] == []


def test_bare_unconfigure_restores_cursor(cursor_db, monkeypatch, tmp_path):
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    cursor.configure("router-key", "https://router.test/v1", ["gpt-5.6-sol"])
    result = CliRunner().invoke(cli, ["--human", "--no-input", "router", "unconfigure"])
    assert result.exit_code == 0, result.output
    assert "restored your previous settings for: Cursor" in result.output
    assert _settings(cursor_db)["openAIBaseUrl"] == "https://api.openai.com/v1"
    assert not cursor.has_receipt()
