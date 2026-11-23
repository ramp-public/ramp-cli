"""Tests for writing Cursor's local OpenAI override."""

import base64
import json
import sqlite3
from contextlib import closing

import pytest
from click.testing import CliRunner

from ramp_cli.commands import cursor
from ramp_cli.commands import router as router_module
from ramp_cli.main import cli
from tests.test_router_cursor import _mock_cursor_setup
from tests.test_router_cursor import _router_model as router_module_model

# Captured before conftest replaces them for every test.
_REAL_RUNNING_PIDS = cursor.running_pids
_REAL_RUNS_INSIDE_CURSOR = cursor.runs_inside_cursor
_REAL_REOPEN = cursor.reopen

CIPHERTEXT = '{"type":"Buffer","data":[1]}'
# The fallback reasons, shared with the other Cursor setup tests.
BY_HAND = "Or set it up by hand: ramp router cursor"
OPEN = (
    "Cursor wasn't set up because it's open. Quit Cursor (Cmd+Q), then run "
    "'ramp router configure cursor' again."
)
INSIDE = (
    "Cursor wasn't set up because this is running in Cursor's terminal. Run "
    "'ramp router configure cursor' from Terminal or iTerm."
)
UNKNOWN = (
    "Cursor wasn't set up because Ramp couldn't check whether Cursor is running. "
    "Quit Cursor yourself if it's open, then run 'ramp router configure cursor' "
    "again."
)
KEYCHAIN = (
    "Cursor wasn't set up because macOS didn't allow access to \"Cursor Safe "
    "Storage\". Run 'ramp router configure cursor' again and click Allow."
)
SETTINGS_ERROR = (
    "Cursor wasn't set up because of an error: Cursor's settings could not be "
    "read. Run 'ramp router configure cursor' again."
)
PROMPTS: list[bool] = []


@pytest.fixture
def cursor_db(monkeypatch):
    monkeypatch.setattr(cursor, "running_pids", lambda: [])
    PROMPTS.clear()

    def encrypt(key, prompt=True):
        PROMPTS.append(prompt)
        return CIPHERTEXT

    monkeypatch.setattr(cursor, "encrypt_with_cursor", encrypt)
    path = cursor.state_db_path()
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as db, db:
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
    with closing(sqlite3.connect(path)) as db, db:
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
    with closing(sqlite3.connect(cursor_db)) as db, db:
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


@pytest.mark.parametrize(
    ("starts_during", "error"),
    [("encrypt_with_cursor", "Quit Cursor"), ("_write", "opened while setup wrote")],
    ids=["keychain-read", "write"],
)
def test_configure_checks_for_cursor_after_the_keychain_and_the_write(
    cursor_db, monkeypatch, starts_during, error
):
    # The keychain dialog can stay open for minutes, and Cursor can be opened
    # meanwhile, or while the settings are written.
    before = _settings(cursor_db)
    running = []
    monkeypatch.setattr(cursor, "running_pids", lambda: list(running))
    step = getattr(cursor, starts_during)

    def cursor_starts(*args, **kwargs):
        result = step(*args, **kwargs)
        running.append(4242)
        return result

    monkeypatch.setattr(cursor, starts_during, cursor_starts)
    with pytest.raises(cursor.CursorSettingsError, match=error):
        cursor.configure("router-key", "https://router.test/v1", ["gpt-5.6-sol"])
    if starts_during == "encrypt_with_cursor":
        assert _settings(cursor_db) == before


def test_configure_names_the_models_it_added(cursor_db):
    receipt = cursor.configure(
        "router-key", "https://router.test/v1", ["gpt-5.6-sol", "ramp-opus-5-5"]
    )
    # gpt-5.6-sol was already there.
    assert receipt["added_now"] == ["ramp-opus-5-5"]
    assert (
        cursor.configure(
            "router-key", "https://router.test/v1", ["gpt-5.6-sol", "ramp-opus-5-5"]
        )["added_now"]
        == []
    )


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
    assert "Cursor: added ramp-opus-5." in result.output
    assert "Cursor: set up. Open Cursor" in result.output
    assert "models discovered" not in result.output
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


def test_windows_writes_settings_without_a_permission_note(
    cursor_db, monkeypatch, tmp_path
):
    _mock_cursor_setup(monkeypatch, aliases={})
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(router_module, "_can_prompt", lambda ctx, fmt: True)
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "cursor", "--api-key", "router-secret"],
    )
    assert result.exit_code == 0, result.output
    assert "Cursor: set up. Open Cursor, and in a new chat on This PC" in result.output
    assert "This Mac" not in result.output
    assert "Cursor Safe Storage" not in result.output
    assert "Override OpenAI Base URL" not in result.output
    assert _rows(cursor_db)[cursor._OPENAI_KEY_SECRET] == CIPHERTEXT


def test_other_platforms_fall_back_without_probing(cursor_db, monkeypatch, tmp_path):
    _mock_cursor_setup(monkeypatch, aliases={})
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    monkeypatch.setattr("sys.platform", "linux")

    def probe():
        raise AssertionError("is_running must not run off macOS and Windows")

    monkeypatch.setattr(cursor, "is_running", probe)
    monkeypatch.setattr(cursor, "running_pids", probe)
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "cursor", "--api-key", "router-secret"],
    )
    assert result.exit_code == 0, result.output
    assert "Cursor: set up" not in result.output
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


def test_windows_encrypts_in_chromiums_v10_gcm_format(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(cursor, "_windows_safe_storage_key", lambda: b"k" * 32)
    monkeypatch.setattr(cursor.os, "urandom", lambda size: b"n" * size)
    calls = []

    def gcm(key, nonce, plaintext):
        calls.append((key, nonce, plaintext))
        return b"<ciphertext>" + b"t" * 16

    monkeypatch.setattr(cursor, "_aes_256_gcm_encrypt", gcm)

    stored = json.loads(cursor.encrypt_with_cursor("router-key"))

    assert calls == [(b"k" * 32, b"n" * 12, b"router-key")]
    assert stored["type"] == "Buffer"
    assert bytes(stored["data"]) == b"v10" + b"n" * 12 + b"<ciphertext>" + b"t" * 16


def test_windows_key_comes_from_local_state_through_dpapi(monkeypatch, tmp_path):
    user = tmp_path / "Cursor" / "User"
    user.mkdir(parents=True)
    monkeypatch.setenv(cursor.USER_DIR_ENV, str(user))
    wrapped = base64.b64encode(b"DPAPI" + b"<blob>").decode()
    (user.parent / "Local State").write_text(
        json.dumps({"os_crypt": {"encrypted_key": wrapped}}), encoding="utf-8"
    )
    unwrapped = []
    monkeypatch.setattr(
        cursor, "_dpapi_unprotect", lambda blob: unwrapped.append(blob) or b"k" * 32
    )

    assert cursor._windows_safe_storage_key() == b"k" * 32
    assert unwrapped == [b"<blob>"]


def test_windows_without_a_key_explains_how_to_get_one(monkeypatch, tmp_path):
    user = tmp_path / "Cursor" / "User"
    user.mkdir(parents=True)
    monkeypatch.setenv(cursor.USER_DIR_ENV, str(user))

    with pytest.raises(cursor.CursorSettingsError, match="Open Cursor once"):
        cursor._windows_safe_storage_key()


TASKLIST = r"C:\Windows\System32\tasklist.exe"


@pytest.mark.parametrize(
    ("returncode", "stdout", "pids"),
    [
        (0, b'"Cursor.exe","4242","Console","1","180,000 K"\r\n', [4242]),
        (
            0,
            b'"Cursor.exe","4242","Console","1","180,000 K"\r\n'
            b'"Cursor.exe","4243","Console","1","90,000 K"\r\n',
            [4242, 4243],
        ),
        (
            0,
            b"INFO: No tasks are running which match the specified criteria.\r\n",
            [],
        ),
        # Localized output in an OEM code page must not break the check.
        (
            0,
            b'"Cursor.exe","4242","Konsole","1","180.000 K"\r\nGr\x81\xe1e\r\n',
            [4242],
        ),
        (
            0,
            b"INFORMATION: Es werden keine Tasks mit den angegebenen \x84\x94\r\n",
            [],
        ),
        # An unreadable answer counts as running, so nothing is written.
        (1, b"", None),
        (0, b'"Cursor.exe","?","Console","1","180,000 K"\r\n', None),
    ],
)
def test_windows_running_check(monkeypatch, returncode, stdout, pids):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(cursor, "running_pids", _REAL_RUNNING_PIDS)
    monkeypatch.setenv("USERNAME", "someone")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        assert "text" not in kwargs
        return cursor.subprocess.CompletedProcess(command, returncode, stdout, b"")

    monkeypatch.setattr(cursor.subprocess, "run", run)

    assert cursor.running_pids() == pids
    assert cursor.is_running() is (pids != [])
    command, kwargs = seen[0]
    assert command[:3] == [TASKLIST, "/FI", "IMAGENAME eq Cursor.exe"]
    assert "USERNAME eq someone" in command
    # Never a console window, even when run without one.
    assert kwargs["creationflags"] == getattr(cursor.subprocess, "CREATE_NO_WINDOW", 0)


@pytest.mark.parametrize(
    ("system_root", "expected"),
    [
        (r"D:\Win", r"D:\Win\System32\tasklist.exe"),
        # Unset or empty: the default location, never a bare name that could
        # resolve from the current folder.
        (None, TASKLIST),
        ("", TASKLIST),
    ],
)
def test_windows_tasklist_runs_by_absolute_path(monkeypatch, system_root, expected):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(cursor, "running_pids", _REAL_RUNNING_PIDS)
    if system_root is None:
        monkeypatch.delenv("SystemRoot", raising=False)
    else:
        monkeypatch.setenv("SystemRoot", system_root)
    seen = []

    def run(command, **kwargs):
        seen.append(command)
        return cursor.subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(cursor.subprocess, "run", run)
    assert cursor.running_pids() == []
    assert seen[0][0] == expected


CONFIGURE = ["router", "configure", "cursor", "--api-key", "router-secret"]


def _configure(*root_flags):
    result = CliRunner().invoke(cli, [*root_flags, *CONFIGURE])
    assert result.exit_code == 0, result.output
    return result


def _configure_json():
    return json.loads(_configure("--agent").output)["data"][0]


@pytest.fixture
def mac_setup(cursor_db, monkeypatch, tmp_path):
    """configure cursor on macOS against the fixture database."""
    _mock_cursor_setup(monkeypatch, aliases={})
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    monkeypatch.setattr("sys.platform", "darwin")
    return cursor_db


def _running(monkeypatch, pids):
    monkeypatch.setattr(cursor, "running_pids", lambda: pids)
    monkeypatch.setattr(
        cursor, "wait_until_quit", lambda *a, **k: pytest.fail("waited for Cursor")
    )


def _missing(monkeypatch):
    cursor.state_db_path().unlink()


def _only_nightly(monkeypatch):
    _missing(monkeypatch)
    monkeypatch.setattr(
        cursor, "other_build_user_dirs", lambda: [("Cursor Nightly", cursor.user_dir())]
    )


def _keychain_denied(monkeypatch):
    def deny(key, prompt=True):
        raise cursor.CursorKeychainError("not available from the keychain")

    monkeypatch.setattr(cursor, "encrypt_with_cursor", deny)


def _unreadable(monkeypatch):
    with closing(sqlite3.connect(cursor.state_db_path())) as db, db:
        db.execute(
            "UPDATE ItemTable SET value = ? WHERE key = ?",
            ("not json", cursor._SETTINGS_KEY),
        )


def _inside_cursor(monkeypatch):
    _running(monkeypatch, [4242])
    monkeypatch.setattr(cursor, "runs_inside_cursor", lambda: True)


# How each fallback reason reads: (break setup, reason, message, extra JSON).
FALLBACKS = {
    "running": (
        lambda monkeypatch: _running(monkeypatch, [11, 12]),
        "cursor_running",
        OPEN,
        {"cursor_pids": [11, 12]},
    ),
    "inside-cursor": (
        _inside_cursor,
        "cursor_running",
        INSIDE,
        {"inside_cursor": True},
    ),
    "state-unknown": (
        lambda monkeypatch: _running(monkeypatch, None),
        "cursor_running",
        UNKNOWN,
        {"process_check_failed": True},
    ),
    "settings-not-found": (
        _missing,
        "settings_not_found",
        "Cursor wasn't set up because its settings weren't found. Open Cursor "
        "once, then run 'ramp router configure cursor' again. (Looked in "
        "{user_dir}. If you start Cursor with --user-data-dir, set "
        "RAMP_CURSOR_USER_DIR to that User folder.)",
        {"settings_path": "{db}"},
    ),
    "only-nightly": (
        _only_nightly,
        "settings_not_found",
        "Cursor wasn't set up because only Cursor Nightly was found, and "
        "automatic setup supports only the standard Cursor app. (Looked in "
        "{user_dir}.)",
        {},
    ),
    "keychain-denied": (
        _keychain_denied,
        "keychain_unavailable",
        KEYCHAIN,
        {},
    ),
    "settings-error": (
        _unreadable,
        "settings_error",
        SETTINGS_ERROR,
        {},
    ),
    "other-platform": (
        lambda monkeypatch: monkeypatch.setattr("sys.platform", "linux"),
        "unsupported_platform",
        "Cursor wasn't set up automatically because automatic setup works "
        "only on macOS and Windows for now.",
        {},
    ),
}


@pytest.mark.parametrize(
    ("break_setup", "reason", "message", "details"),
    list(FALLBACKS.values()),
    ids=list(FALLBACKS),
)
def test_a_fallback_says_why_before_the_paste_in_steps(
    mac_setup, monkeypatch, break_setup, reason, message, details
):
    db = str(mac_setup)
    message = message.format(user_dir=cursor.user_dir())
    break_setup(monkeypatch)
    before = _rows(mac_setup) if mac_setup.exists() else None
    result = _configure("--human")
    lines = result.output.splitlines()
    # The reason comes before the paste-in steps it explains.
    assert lines.index(message) < lines.index("Finish in Cursor:")
    assert "Cursor: set up" not in result.output
    assert "pkill" not in result.output
    if before is not None:
        assert _rows(mac_setup) == before

    payload = _configure_json()
    assert payload["settings_written"] is False
    assert payload["fallback_reason"] == reason
    assert payload["fallback_message"] == message
    for key, value in details.items():
        assert payload[key] == (
            value.format(db=db) if isinstance(value, str) else value
        )
    assert any("Override OpenAI Base URL" in s for s in payload["instructions"])


def test_wait_until_quit_rechecks_until_cursor_exits_or_time_runs_out(monkeypatch):
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(cursor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cursor.time, "sleep", sleep)
    checks = iter([[4242], [4242], []])
    monkeypatch.setattr(cursor, "running_pids", lambda: next(checks))
    assert cursor.wait_until_quit(timeout=30) == []
    assert clock[0] == pytest.approx(2 * cursor._QUIT_POLL_SECONDS)
    clock[0] = 0.0
    monkeypatch.setattr(cursor, "running_pids", lambda: [4242])
    assert cursor.wait_until_quit(timeout=3) == [4242]
    assert clock[0] == pytest.approx(3)


def _pgrep(monkeypatch, returncode=0, stdout="", error=None):
    monkeypatch.setattr(cursor, "running_pids", _REAL_RUNNING_PIDS)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if error is not None:
            raise error
        return cursor.subprocess.CompletedProcess(command, returncode, stdout, "")

    monkeypatch.setattr(cursor.subprocess, "run", run)
    return calls


PGREP_ARGV = {
    # macOS pgrep leaves out its own ancestors without -a, and Cursor is one
    # when this runs in Cursor's terminal. Linux pgrep's -a lists command lines.
    "darwin": ["pgrep", "-a", "-u", "{uid}", "-x", "Cursor"],
    "linux": ["pgrep", "-u", "{uid}", "-x", "Cursor"],
}


@pytest.mark.parametrize(
    ("platform", "pgrep", "pids"),
    [
        ("darwin", {"stdout": "4242\n4243\n"}, [4242, 4243]),
        ("linux", {"stdout": "4242\n4243\n"}, [4242, 4243]),
        # Only "matched nothing" means closed; anything else counts as running.
        ("darwin", {"returncode": 1}, []),
        ("darwin", {"returncode": 2}, None),
        ("darwin", {"returncode": 3}, None),
        ("darwin", {"error": FileNotFoundError("pgrep")}, None),
        ("darwin", {"error": cursor.subprocess.TimeoutExpired("pgrep", 2)}, None),
    ],
    ids=[
        "mac",
        "linux",
        "no-match",
        "usage-error",
        "internal-error",
        "missing",
        "timeout",
    ],
)
@pytest.mark.posix_only(
    reason="Cursor's process check runs pgrep -u <uid>; automatic Cursor setup "
    "is macOS-only"
)
def test_running_pids_reads_pgrep(cursor_db, monkeypatch, platform, pgrep, pids):
    monkeypatch.setattr("sys.platform", platform)
    monkeypatch.setattr(cursor.os, "getuid", lambda: 123, raising=False)
    calls = _pgrep(monkeypatch, **pgrep)
    assert cursor.running_pids() == pids
    uid = str(cursor.os.getuid())
    assert calls[0] == [part.format(uid=uid) for part in PGREP_ARGV[platform]]
    assert cursor.is_running() is (pids != [])
    if pids is None:
        # So configure refuses to write under it.
        before = _settings(cursor_db)
        with pytest.raises(cursor.CursorSettingsError, match="Quit Cursor"):
            cursor.configure("router-key", "https://router.test/v1", ["gpt-5.6-sol"])
        assert _settings(cursor_db) == before


def test_other_builds_are_found_beside_cursor(monkeypatch, tmp_path):
    monkeypatch.delenv(cursor.USER_DIR_ENV)
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(cursor.Path, "home", lambda: tmp_path)
    assert cursor.other_build_user_dirs() == []
    nightly = tmp_path / "Library" / "Application Support" / "Cursor Nightly" / "User"
    db = cursor.state_db_path(nightly)
    db.parent.mkdir(parents=True)
    db.touch()
    assert cursor.other_build_user_dirs() == [("Cursor Nightly", nightly)]
    # A redirected Cursor folder is the only one detection looks at.
    monkeypatch.setenv(cursor.USER_DIR_ENV, str(tmp_path / "elsewhere"))
    assert cursor.other_build_user_dirs() == []


SUGGESTION_MODELS = [
    "gpt-5.6-sol",
    router_module.DEFAULT_MODEL,
    "claude-opus-5-5",
    "claude-sonnet-5-5",
]


def _aliases_for(*names):
    return {f"ramp-{name.removeprefix('claude-')}": name for name in names}


@pytest.mark.parametrize(
    ("fallback", "suggested", "add_step"),
    [
        # Router's Claude fallback, under the alias setup created for it.
        ("claude-sonnet-5-5", "ramp-sonnet-5-5", "ramp-opus-5-5, ramp-sonnet-5-5"),
        # No fallback: the default, a direct model, added ahead of the aliases.
        (
            None,
            router_module.DEFAULT_MODEL,
            f"{router_module.DEFAULT_MODEL}, ramp-opus-5-5, ramp-sonnet-5-5",
        ),
    ],
    ids=["claude-fallback", "default"],
)
def test_every_path_suggests_a_model_cursor_has(
    mac_setup, monkeypatch, fallback, suggested, add_step
):
    _mock_cursor_setup(
        monkeypatch,
        models=[router_module_model(name) for name in SUGGESTION_MODELS],
        aliases=_aliases_for("claude-opus-5-5", "claude-sonnet-5-5"),
    )
    monkeypatch.setattr(
        "ramp_cli.admin_defaults.fallback_model", lambda *_args, **_kwargs: fallback
    )
    # Written: the suggestion is among the models setup added to Cursor.
    assert _configure_json()["suggested_model"] == suggested
    assert suggested in _settings(mac_setup)["aiSettings"]["userAddedModels"]
    # Paste-in steps, from configure and from ramp router cursor: the step that
    # adds models names the suggestion.
    _running(monkeypatch, [4242])
    guided = CliRunner().invoke(
        cli, ["--agent", "router", "cursor", "--api-key", "router-secret"]
    )
    assert guided.exit_code == 0, guided.output
    for payload in (_configure_json(), json.loads(guided.output)["data"][0]):
        assert payload["suggested_model"] == suggested
        assert f"Add these model names: {add_step}." in payload["instructions"]


@pytest.mark.parametrize(
    ("fallback", "aliases", "expected"),
    [
        # No alias for it: on to the default, as for any fallback Cursor
        # can't serve. An alias for another Claude model isn't a stand-in.
        ("claude-sonnet-5-5", ["claude-opus-5-5"], router_module.DEFAULT_MODEL),
        # A fallback Cursor serves directly, and one Router doesn't list.
        ("gpt-5.6-sol", ["claude-opus-5-5"], "gpt-5.6-sol"),
        ("gpt-9-sol", ["claude-opus-5-5"], router_module.DEFAULT_MODEL),
    ],
)
def test_suggestion_follows_routers_fallback(fallback, aliases, expected):
    models = [router_module_model(name) for name in SUGGESTION_MODELS]
    suggested = router_module._cursor_suggested_model(
        models, fallback, _aliases_for(*aliases)
    )
    assert suggested == expected


_REAL_UNLOCK = cursor.unlock


def test_the_keychain_is_asked_once_per_run_unless_refused(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    answers = iter([cursor.CursorKeychainError("denied"), b"cursor-safe-storage"])
    asked = []

    def password(prompt):
        asked.append(prompt)
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(cursor, "_keychain_password", password)
    # CommonCrypto exists only on macOS.
    monkeypatch.setattr(cursor, "_aes_128_cbc_encrypt", lambda key, iv, data: key)
    with pytest.raises(cursor.CursorKeychainError):
        _REAL_UNLOCK()
    # A refusal isn't kept: the next run asks again, then keeps the answer.
    _REAL_UNLOCK()
    first = cursor.encrypt_with_cursor("router-key")
    assert cursor.encrypt_with_cursor("router-key") == first
    assert asked == [True, True]
