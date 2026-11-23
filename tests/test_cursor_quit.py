"""configure cursor offering to quit an open Cursor (tester report #2)."""

import json
import shutil
import signal
import sqlite3
import subprocess
import sys
from contextlib import closing

import click
import pytest
from click.testing import CliRunner

from ramp_cli.commands import cursor
from ramp_cli.commands import router as router_module
from ramp_cli.main import cli
from tests.test_cursor_settings import (  # noqa: F401
    _REAL_REOPEN,
    _REAL_RUNNING_PIDS,
    _REAL_RUNS_INSIDE_CURSOR,
    BY_HAND,
    INSIDE,
    KEYCHAIN,
    SETTINGS_ERROR,
    UNKNOWN,
    _settings,
    cursor_db,
    mac_setup,
)

CONFIGURE = ["router", "configure", "cursor", "--api-key", "router-secret"]
PROMPT = (
    "Cursor is open. Setup needs to quit it, then reopens it with your open "
    "files. Chats still running in Cursor will stop. Quit Cursor now? [Y/n]"
)
QUITTING = (
    "Cursor: quitting. If macOS asks to let your terminal control Cursor, click "
    "OK. If Cursor asks you to confirm, confirm."
)
FORCE_RISK = (
    "Force quit it? Unsaved changes and chats still running in Cursor may be "
    "lost. [y/N]"
)
FORCE_PROMPTS = {
    cursor.QUIT_TIMEOUT: (
        "Cursor is still open after 30 seconds. It may be waiting for you to "
        f"answer a dialog. {FORCE_RISK}"
    ),
    cursor.QUIT_DENIED: f"macOS didn't let Ramp quit Cursor. {FORCE_RISK}",
    cursor.QUIT_FAILED: f"Cursor didn't quit. {FORCE_RISK}",
}
RERUN = "run 'ramp router configure cursor' again"
DECLINED = (
    "Cursor wasn't set up because it's still open. When you're ready, run "
    "'ramp router configure cursor' again and let it quit Cursor."
)
NOT_FOUND = "Cursor wasn't set up because its settings weren't found."
NIGHTLY = "Cursor wasn't set up because only Cursor Nightly was found"
BLOCKED = "Your Cursor team blocks personal API keys"
QUIT_FAILURES = {
    cursor.QUIT_DENIED: (
        "Cursor wasn't set up because macOS didn't let Ramp quit it. Quit Cursor "
        f"(Cmd+Q) and {RERUN}. To let setup quit it next time, allow your "
        "terminal in System Settings › Privacy & Security › Automation."
    ),
    cursor.QUIT_TIMEOUT: (
        "Cursor wasn't set up because it didn't quit in time. Quit Cursor "
        f"(Cmd+Q), then {RERUN}."
    ),
    cursor.QUIT_FAILED: (
        f"Cursor wasn't set up because it didn't quit. Quit Cursor (Cmd+Q), then "
        f"{RERUN}."
    ),
}
FORCE_FAILED = (
    "Cursor wasn't set up because it didn't quit, even when forced. Quit "
    f"Cursor (Cmd+Q), then {RERUN}."
)


class FakeCursor:
    """A Cursor that is open until asked to quit, recording what setup did."""

    def __init__(
        self,
        monkeypatch,
        *,
        outcome=cursor.QUIT_DONE,
        reopens=True,
        inside=False,
        forced=True,
    ):
        self.running = True
        self.calls = []
        monkeypatch.setattr(cursor, "running_pids", self.pids)
        monkeypatch.setattr(
            cursor, "wait_until_quit", lambda *a, **k: pytest.fail("waited")
        )
        monkeypatch.setattr(cursor, "runs_inside_cursor", lambda: inside)

        def request_quit(*args, **kwargs):
            self.calls.append("quit")
            if outcome == cursor.QUIT_DONE:
                self.running = False
            return outcome

        def reopen():
            self.calls.append("reopen")
            self.running = reopens
            return reopens

        def force_quit(*args, **kwargs):
            self.calls.append("force")
            if forced:
                self.running = False
                return cursor.QUIT_FORCED
            return cursor.QUIT_FORCE_FAILED

        def unlock(prompt=True):
            self.calls.append("keychain")

        monkeypatch.setattr(cursor, "request_quit", request_quit)
        monkeypatch.setattr(cursor, "force_quit", force_quit)
        monkeypatch.setattr(cursor, "reopen", reopen)
        monkeypatch.setattr(cursor, "unlock", unlock)

    def pids(self):
        return [4242] if self.running else []


@pytest.fixture
def mac(mac_setup):  # noqa: F811
    """configure cursor on macOS against a fixture Cursor database."""
    return mac_setup


@pytest.fixture
def state_db(cursor_db):  # noqa: F811
    """A fixture Cursor database, with Cursor closed."""
    return cursor_db


@pytest.fixture
def interactive(monkeypatch):
    """A person at a terminal: Click's input stands in for their answers."""
    monkeypatch.setattr(
        router_module,
        "_can_prompt",
        lambda ctx, fmt: fmt != "json" and not ctx.obj["no_input"],
    )


def _run(*args, input=None):
    return CliRunner().invoke(cli, ["--human", *args, *CONFIGURE], input=input)


def _no_key(monkeypatch):
    monkeypatch.setattr(router_module, "_pick_stored_router_api_key", lambda: None)
    monkeypatch.setattr(
        router_module,
        "acquire_router_api_key",
        lambda *a, **k: pytest.fail("a key was created for nothing"),
    )


def _signed_in_run(input):
    return CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "cursor", "--no-browser"],
        input=input,
    )


def _configure_with_codex(mac, monkeypatch, input):
    configured = []

    def configure_client(item, api_key, models, **kwargs):
        configured.append(item)
        return mac.parent / f"{item}.json", "gpt-5.6-sol", False

    monkeypatch.setattr(router_module, "_configure_client", configure_client)
    monkeypatch.setattr(router_module, "_pick_claude_models", lambda *a: "compact")
    copies = []
    monkeypatch.setattr(router_module, "_copy_to_clipboard", copies.append)
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "cursor", "codex", "--api-key", "k"],
        input=input,
    )
    return result, configured, copies


def _unknown(monkeypatch, fake, db):
    # pgrep can't answer, so whether this runs inside Cursor isn't known either.
    monkeypatch.setattr(cursor, "running_pids", lambda: None)
    monkeypatch.setattr(
        cursor, "runs_inside_cursor", lambda: pytest.fail("checked inside Cursor")
    )


def _missing_settings(monkeypatch, fake, db):
    db.unlink()


def _nightly_with_cursor_closed(monkeypatch, fake, db):
    fake.running = False
    db.unlink()
    monkeypatch.setattr(
        cursor, "other_build_user_dirs", lambda: [("Cursor Nightly", cursor.user_dir())]
    )


def _keys_blocked(monkeypatch, fake, db):
    monkeypatch.setattr(cursor, "personal_keys_blocked", lambda user=None: True)


def _keychain_denied(monkeypatch, fake, db):
    def deny(prompt=True):
        fake.calls.append("keychain")
        raise cursor.CursorKeychainError("denied")

    monkeypatch.setattr(cursor, "unlock", deny)


def _keychain_closed(monkeypatch, fake, db):
    fake.running = False
    _keychain_denied(monkeypatch, fake, db)


# Each way a Cursor setup can't finish once Cursor is open: (id, FakeCursor
# options, extra setup, answers, the reason given, what setup did to Cursor,
# whether the quit was offered at all).
CANT_FINISH = [
    ("declined", {}, None, "n\n", DECLINED, [], True),
    ("inside-cursor", {"inside": True}, None, "y\n", INSIDE, [], False),
    ("state-unknown", {}, _unknown, "y\n", UNKNOWN, [], False),
    ("settings-missing", {}, _missing_settings, "y\n", NOT_FOUND, [], False),
    ("only-nightly", {}, _nightly_with_cursor_closed, "y\n", NIGHTLY, [], False),
    ("keys-blocked", {}, _keys_blocked, "y\n", BLOCKED, [], False),
    # Asked after the user agreed to quit, but before quitting.
    ("keychain-denied", {}, _keychain_denied, "y\n", KEYCHAIN, ["keychain"], True),
    ("keychain-closed", {}, _keychain_closed, "y\n", KEYCHAIN, ["keychain"], False),
    *[
        # Yes to quitting, then no to forcing it: nothing to reopen.
        (
            f"quit-{outcome}",
            {"outcome": outcome},
            None,
            "y\nn\n",
            message,
            ["keychain", "quit"],
            True,
        )
        for outcome, message in QUIT_FAILURES.items()
    ],
    (
        "force-quit-failed",
        {"outcome": cursor.QUIT_TIMEOUT, "forced": False},
        None,
        "y\ny\n",
        FORCE_FAILED,
        ["keychain", "quit", "force"],
        True,
    ),
]


@pytest.mark.parametrize(
    ("options", "setup", "answers", "message", "calls", "asked"),
    [case[1:] for case in CANT_FINISH],
    ids=[case[0] for case in CANT_FINISH],
)
def test_a_cursor_only_setup_that_cant_finish_stops_before_sign_in(
    mac, monkeypatch, interactive, options, setup, answers, message, calls, asked
):
    fake = FakeCursor(monkeypatch, **options)
    if setup:
        setup(monkeypatch, fake, mac)
    before = _settings(mac) if mac.exists() else None
    _no_key(monkeypatch)
    result = _signed_in_run(answers)
    assert result.exit_code == router_module.CURSOR_NOT_SET_UP_EXIT_CODE, result.output
    assert message in result.output
    assert result.output.splitlines()[-1] == BY_HAND
    assert (PROMPT in result.output) is asked
    # The user's own answer, or a quit that didn't happen, isn't an error, and
    # a macOS terminal gets no paste-in steps.
    assert "Error:" not in result.output
    assert "Override OpenAI Base URL" not in result.output
    assert fake.calls == calls
    if before is not None:
        assert _settings(mac) == before


# One stop before quitting, one after asking, one after a failed force quit.
ALONGSIDE = ("declined", "keychain-denied", "force-quit-failed")


@pytest.mark.parametrize(
    ("options", "setup", "answers", "message", "calls"),
    [case[1:6] for case in CANT_FINISH if case[0] in ALONGSIDE],
    ids=[case[0] for case in CANT_FINISH if case[0] in ALONGSIDE],
)
def test_other_agents_still_finish_when_cursor_cant(
    mac, monkeypatch, interactive, options, setup, answers, message, calls
):
    fake = FakeCursor(monkeypatch, **options)
    if setup:
        setup(monkeypatch, fake, mac)
    before = _settings(mac)
    result, configured, copies = _configure_with_codex(mac, monkeypatch, answers)
    assert result.exit_code == 0, result.output
    assert configured == ["codex"]
    # The same sentence as a Cursor-only run, then the way to do it by hand.
    lines = result.output.splitlines()
    assert lines[lines.index(message) + 1] == BY_HAND
    assert "Override OpenAI Base URL" not in result.output
    assert copies == []
    assert fake.calls == calls
    assert _settings(mac) == before


def test_accepting_quits_cursor_before_sign_in_sets_it_up_and_reopens_it(
    mac, monkeypatch, interactive
):
    fake = FakeCursor(monkeypatch)
    monkeypatch.setattr(router_module, "_pick_stored_router_api_key", lambda: None)

    def sign_in(*args, **kwargs):
        fake.calls.append("sign-in")
        return "router-secret"

    monkeypatch.setattr(router_module, "acquire_router_api_key", sign_in)
    result = _signed_in_run("y\n")
    assert result.exit_code == 0, result.output
    assert PROMPT in result.output
    assert QUITTING in result.output
    assert "Enter your Mac password if asked, then click Allow." in result.output
    assert "Force quit it?" not in result.output
    assert (
        "Cursor: set up and reopened. In a new chat on This Mac, pick gpt-5.6-sol."
    ) in result.output.splitlines()
    assert fake.calls == ["keychain", "quit", "sign-in", "reopen"]
    assert _settings(mac)["openAIBaseUrl"] == "https://api.router.com/v1"


@pytest.mark.parametrize(
    "flags", [["--no-input"], ["--agent"], []], ids=["no-input", "json", "no-tty"]
)
def test_unattended_runs_never_quit_cursor(mac, monkeypatch, flags):
    fake = FakeCursor(monkeypatch, outcome=cursor.QUIT_TIMEOUT)
    result = CliRunner().invoke(cli, [*flags, *CONFIGURE], input="y\ny\n")
    assert result.exit_code == 0, result.output
    assert PROMPT not in result.output
    assert "Force quit it?" not in result.output
    assert fake.calls == []
    # They keep the paste-in steps, with the reason.
    assert "Cursor wasn't set up because it's open." in result.output
    assert "Override OpenAI Base URL" in result.output


def test_mdm_runs_never_ask(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(router_module, "_can_prompt", lambda ctx, fmt: True)
    monkeypatch.setattr(
        cursor, "running_pids", lambda: pytest.fail("probed Cursor for MDM")
    )
    monkeypatch.setattr(click, "confirm", lambda *a, **k: pytest.fail("asked"))
    ctx = click.Context(click.Command("configure"), obj={"no_input": False})
    assert (
        router_module._offer_to_quit_cursor(ctx, "human", ("cursor",), unattended=True)
        == {}
    )


def test_linux_never_asks_and_keeps_the_paste_in_steps(mac, monkeypatch, interactive):
    monkeypatch.setattr("sys.platform", "linux")
    fake = FakeCursor(monkeypatch)
    result = _run(input="y\n")
    assert result.exit_code == 0, result.output
    assert PROMPT not in result.output
    # Nothing here uses macOS keys or names.
    assert "Cmd" not in result.output
    assert "Open Cursor Settings (Ctrl+Shift+J)" in result.output
    assert fake.calls == []


def test_a_cursor_opened_again_during_setup_is_left_alone(
    mac, monkeypatch, interactive
):
    fake = FakeCursor(monkeypatch)
    fetch = router_module._fetch_models

    def reopened_meanwhile(*args, **kwargs):
        fake.running = True
        return fetch(*args, **kwargs)

    monkeypatch.setattr(router_module, "_fetch_models", reopened_meanwhile)
    result = _run(input="y\n")
    # Setup quit Cursor and found it open again: no wait, nothing written.
    assert result.exit_code == 1, result.output
    assert (
        "Cursor wasn't set up because it was opened again during setup. Run "
        "'ramp router configure cursor' again and keep Cursor closed until it "
        "finishes."
    ) in result.output.splitlines()
    assert fake.calls == ["keychain", "quit"]


def test_a_cursor_that_cannot_be_reopened_says_so(mac, monkeypatch, interactive):
    fake = FakeCursor(monkeypatch, reopens=False)
    result = _run(input="y\n")
    assert result.exit_code == 0, result.output
    assert "Cursor: set up, but it couldn't be reopened." in result.output
    assert fake.calls == ["keychain", "quit", "reopen"]


FETCH = "ramp_cli.commands.router._fetch_models"
UNREADABLE = cursor.CursorSettingsError("Cursor's settings could not be read.")


@pytest.mark.parametrize(
    ("target", "error", "lines"),
    [
        # A run that says how it ended says Cursor was reopened.
        (
            "ramp_cli.commands.cursor.encrypt_with_cursor",
            UNREADABLE,
            [SETTINGS_ERROR, "Cursor: reopened.", BY_HAND],
        ),
        (FETCH, click.ClickException("Router is unreachable."), []),
        (FETCH, KeyboardInterrupt(), []),
    ],
    ids=["settings-error", "failed-setup", "ctrl-c"],
)
def test_cursor_is_reopened_however_the_run_ends(
    mac, monkeypatch, interactive, target, error, lines
):
    fake = FakeCursor(monkeypatch)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(target, fail)
    result = _run(input="y\n")
    assert result.exit_code != 0
    assert fake.calls == ["keychain", "quit", "reopen"]
    assert "\n".join(lines) in result.output


@pytest.mark.parametrize("outcome", list(FORCE_PROMPTS))
def test_a_failed_quit_offers_a_force_quit_that_sets_cursor_up(
    mac, monkeypatch, interactive, outcome
):
    fake = FakeCursor(monkeypatch, outcome=outcome)
    result = _run(input="y\ny\n")
    assert result.exit_code == 0, result.output
    assert FORCE_PROMPTS[outcome] in result.output
    assert "Cursor: force quitting." in result.output
    assert (
        "Cursor: force quit, set up, and reopened. In a new chat on This Mac, "
        "pick gpt-5.6-sol."
    ) in result.output.splitlines()
    assert fake.calls == ["keychain", "quit", "force", "reopen"]
    assert _settings(mac)["openAIBaseUrl"] == "https://api.router.com/v1"


def test_the_force_quit_prompt_defaults_to_no(mac, monkeypatch, interactive):
    fake = FakeCursor(monkeypatch, outcome=cursor.QUIT_TIMEOUT)
    before = _settings(mac)
    # Enter alone takes the default.
    result = _run(input="y\n\n")
    assert result.exit_code == 1, result.output
    assert FORCE_PROMPTS[cursor.QUIT_TIMEOUT] in result.output
    assert "force" not in fake.calls
    assert _settings(mac) == before


@pytest.mark.parametrize("when", ["before-asking", "while-asking"])
def test_a_cursor_that_quits_late_is_not_force_quit(
    mac, monkeypatch, interactive, when
):
    fake = FakeCursor(monkeypatch, outcome=cursor.QUIT_TIMEOUT)
    if when == "before-asking":
        quit_ = cursor.request_quit

        def late(*args, **kwargs):
            outcome = quit_(*args, **kwargs)
            fake.running = False
            return outcome

        monkeypatch.setattr(cursor, "request_quit", late)
    else:
        confirm = click.confirm

        def answer(text, **kwargs):
            answered = confirm(text, **kwargs)
            if "Force quit" in text:
                # The user answered Cursor's own dialog meanwhile.
                fake.running = False
            return answered

        monkeypatch.setattr(click, "confirm", answer)
    result = _run(input="y\nn\n")
    assert result.exit_code == 0, result.output
    assert "Cursor: set up and reopened." in result.output
    assert fake.calls == ["keychain", "quit", "reopen"]


def _osascript(monkeypatch, *, returncode=0, stderr="", timeout=False, error=None):
    monkeypatch.setattr("sys.platform", "darwin")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if error is not None:
            raise error
        if timeout:
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
        return subprocess.CompletedProcess(command, returncode, "", stderr)

    monkeypatch.setattr(cursor.subprocess, "run", run)
    return commands


QUIT_SCRIPT = (
    f'if application id "{cursor.APP_BUNDLE_ID}" is running then '
    f'tell application id "{cursor.APP_BUNDLE_ID}" to quit'
)


def _refused(stderr):
    return {"returncode": 1, "stderr": stderr}


@pytest.mark.parametrize(
    ("osascript", "remaining", "expected", "waited"),
    [
        # Only a refused Automation permission is an answer on its own.
        (_refused("Not authorized (-1743)"), [4242], cursor.QUIT_DENIED, False),
        # -128 comes back for quits that went ahead too: whether Cursor exits
        # decides. A cancel in Cursor's dialog leaves it open: a timeout.
        (_refused("User canceled. (-128)"), [], cursor.QUIT_DONE, True),
        (_refused("User canceled. (-128)"), [4242], cursor.QUIT_TIMEOUT, True),
        ({}, [], cursor.QUIT_DONE, True),
        # pgrep can't answer: never "Cursor quit".
        ({}, None, cursor.QUIT_TIMEOUT, True),
        # osascript itself ran out of time, or isn't there.
        ({"timeout": True}, [4242], cursor.QUIT_TIMEOUT, False),
        ({"error": FileNotFoundError("osascript")}, [4242], cursor.QUIT_FAILED, False),
    ],
)
def test_request_quit_outcomes(monkeypatch, osascript, remaining, expected, waited):
    commands = _osascript(monkeypatch, **osascript)
    waits = []
    monkeypatch.setattr(cursor, "running_pids", lambda: remaining)
    monkeypatch.setattr(
        cursor, "wait_until_quit", lambda timeout: waits.append(timeout) or remaining
    )
    assert cursor.request_quit() == expected
    assert waits == ([cursor.QUIT_CONFIRM_SECONDS] if waited else [])
    # It only ever asks a running Cursor to quit, by bundle id: no launch, no
    # kill, no force-quit.
    assert commands == [["osascript", "-e", QUIT_SCRIPT]]


def test_reopen_opens_cursor_by_bundle_id(monkeypatch):
    commands = _osascript(monkeypatch)
    assert _REAL_REOPEN() is True
    assert commands == [["open", "-b", cursor.APP_BUNDLE_ID]]


@pytest.mark.posix_only(reason="Cursor's process check runs pgrep -u <uid>")
def test_runs_inside_cursor_walks_parents_with_the_real_pgrep_argv(monkeypatch):
    # macOS pgrep without -a leaves out this process's ancestors, Cursor
    # among them, so the walk has nothing to find. With -a it does.
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(cursor, "running_pids", _REAL_RUNNING_PIDS)

    monkeypatch.setattr(cursor.os, "getuid", lambda: 123, raising=False)

    def pgrep(command, **kwargs):
        listed = "100\n" if "-a" in command else ""
        return subprocess.CompletedProcess(command, 0 if listed else 1, listed, "")

    monkeypatch.setattr(cursor.subprocess, "run", pgrep)
    monkeypatch.setattr(cursor.os, "getppid", lambda: 300)
    parents = {300: 200, 200: 100}
    monkeypatch.setattr(cursor, "_parent_pid", lambda pid: parents.get(pid, 1))
    assert _REAL_RUNS_INSIDE_CURSOR() is True
    # A chain that never reaches Cursor's pid isn't inside it.
    parents[200] = 1
    assert _REAL_RUNS_INSIDE_CURSOR() is False


class FakeProcesses:
    """pgrep's view of Cursor, and the signals sent to each pid."""

    def __init__(
        self, monkeypatch, pids, *, survives_term=(), survives_kill=(), denied=False
    ):
        monkeypatch.setattr("sys.platform", "darwin")
        monkeypatch.setattr(signal, "SIGKILL", KILL, raising=False)
        self.alive = set(pids)
        self.survives_term = set(survives_term)
        self.survives_kill = set(survives_kill)
        self.denied = denied
        self.sent = []
        monkeypatch.setattr(cursor, "running_pids", lambda: sorted(self.alive))
        monkeypatch.setattr(cursor.time, "sleep", lambda _: None)
        monkeypatch.setattr(cursor.os, "kill", self.kill)

    def kill(self, pid, sig):
        self.sent.append((pid, sig))
        if self.denied:
            raise PermissionError(pid)
        if pid not in self.alive:
            raise ProcessLookupError(pid)
        if sig == signal.SIGTERM and pid not in self.survives_term:
            self.alive.discard(pid)
        if sig == signal.SIGKILL and pid not in self.survives_kill:
            self.alive.discard(pid)


TERM, KILL = signal.SIGTERM, getattr(signal, "SIGKILL", 9)
_FORCE_QUIT_IS_POSIX_ONLY = pytest.mark.posix_only(
    reason="these force-quit Cursor with SIGTERM/SIGKILL; Windows uses taskkill"
)


@pytest.mark.parametrize(
    ("pids", "options", "expected", "sent"),
    [
        # A Cursor that quits on SIGTERM gets nothing more.
        ([4242], {}, cursor.QUIT_FORCED, [(4242, TERM)]),
        # SIGKILL only for what SIGTERM left.
        (
            [11, 12],
            {"survives_term": [12]},
            cursor.QUIT_FORCED,
            [(11, TERM), (12, TERM), (12, KILL)],
        ),
        (
            [11],
            {"survives_term": [11], "survives_kill": [11]},
            cursor.QUIT_FORCE_FAILED,
            [(11, TERM), (11, KILL)],
        ),
        ([11], {"denied": True}, cursor.QUIT_FORCE_FAILED, [(11, TERM)]),
        # Nothing was signalled, so it wasn't a force quit.
        ([], {}, cursor.QUIT_DONE, []),
    ],
    ids=["term", "term-then-kill", "survives", "not-allowed", "none-running"],
)
@_FORCE_QUIT_IS_POSIX_ONLY
def test_force_quit(monkeypatch, pids, options, expected, sent):
    processes = FakeProcesses(monkeypatch, pids, **options)
    assert cursor.force_quit(term_wait=0.01, kill_wait=0.01) == expected
    assert processes.sent == sent


@_FORCE_QUIT_IS_POSIX_ONLY
def test_force_quit_signals_only_the_cursor_pids_it_found(monkeypatch):
    processes = FakeProcesses(monkeypatch, [11], survives_term=[11])
    real_kill = processes.kill

    def kill(pid, sig):
        real_kill(pid, sig)
        # A Cursor started meanwhile (an update, say) is not one setup found.
        processes.alive.add(99)

    monkeypatch.setattr(cursor.os, "kill", kill)
    assert cursor.force_quit(term_wait=0.01, kill_wait=0.01) == cursor.QUIT_FORCE_FAILED
    assert {pid for pid, _ in processes.sent} == {11}
    # And when pgrep can't answer, nothing is signalled at all.
    processes.sent.clear()
    monkeypatch.setattr(cursor, "running_pids", lambda: None)
    assert cursor.force_quit(term_wait=0.01, kill_wait=0.01) == cursor.QUIT_FORCE_FAILED
    assert processes.sent == []


_KILLED_WRITER = """
import json, os, signal, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute("PRAGMA cache_size=1")
connection.execute("BEGIN")
for index in range(2000):
    connection.execute("INSERT INTO ItemTable VALUES (?, ?)", (f"x{index}", "y" * 500))
connection.execute(
    "UPDATE ItemTable SET value = ? WHERE key = ?",
    (json.dumps({"openAIBaseUrl": "https://half-written.test"}), sys.argv[2]),
)
os._exit(1)
"""


def _kill_cursor_mid_write(state_db):
    subprocess.run(
        [sys.executable, "-c", _KILLED_WRITER, str(state_db), cursor._SETTINGS_KEY],
        check=False,
    )
    assert state_db.with_name(state_db.name + "-journal").exists()


@pytest.mark.posix_only(reason="simulates a killed Cursor with SIGKILL")
def test_settings_left_mid_write_by_a_killed_cursor_are_recovered(
    state_db, monkeypatch
):
    # A killed writer leaves a hot journal that only a writable connection
    # can roll back; a read-only one fails, or reads what was rolled back.
    monkeypatch.setattr("sys.platform", "darwin")
    with closing(sqlite3.connect(state_db)) as db, db:
        db.execute(
            "INSERT INTO ItemTable VALUES (?, ?)",
            (cursor._ADMIN_SETTINGS_KEYS[0], json.dumps({"byokDisabled": True})),
        )
    _kill_cursor_mid_write(state_db)
    assert cursor.personal_keys_blocked() is True
    assert cursor.read_override_base_url() == (True, "https://api.openai.com/v1")
    _kill_cursor_mid_write(state_db)
    cursor.configure("router-key", "https://router.test/v1", ["gpt-5.6-sol"])
    settings = _settings(state_db)
    assert settings["openAIBaseUrl"] == "https://router.test/v1"
    # The half-written transaction is gone and the committed settings remain.
    assert settings["aiSettings"]["userAddedModels"] == ["mine", "gpt-5.6-sol"]
    with closing(sqlite3.connect(state_db)) as db, db:
        assert db.execute("SELECT count(*) FROM ItemTable").fetchone()[0] == 4


def test_settings_in_a_wal_left_by_a_killed_cursor_are_read(state_db, tmp_path):
    with closing(sqlite3.connect(state_db)) as db, db:
        db.execute("PRAGMA journal_mode=WAL")
    writer = sqlite3.connect(state_db)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    with writer:
        writer.execute(
            "UPDATE ItemTable SET value = ? WHERE key = ?",
            (
                json.dumps({"aiSettings": {"userAddedModels": ["only-in-the-wal"]}}),
                cursor._SETTINGS_KEY,
            ),
        )
    # Copy the files the way a killed Cursor leaves them: committed rows still
    # only in -wal, beside -shm.
    left = tmp_path / "left"
    left.mkdir()
    for suffix in ("", "-wal", "-shm"):
        shutil.copy(f"{state_db}{suffix}", left / f"state.vscdb{suffix}")
    writer.close()
    for suffix in ("", "-wal", "-shm"):
        shutil.copy(left / f"state.vscdb{suffix}", f"{state_db}{suffix}")
    cursor.configure("router-key", "https://router.test/v1", ["gpt-5.6-sol"])
    assert _settings(state_db)["aiSettings"]["userAddedModels"] == [
        "only-in-the-wal",
        "gpt-5.6-sol",
    ]


def test_a_running_cursor_is_read_without_writing(state_db, monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(cursor, "running_pids", lambda: [4242])
    modes = []
    connect = sqlite3.connect

    def recording(database, **kwargs):
        modes.append(database.rpartition("mode=")[2])
        return connect(database, **kwargs)

    monkeypatch.setattr(cursor.sqlite3, "connect", recording)
    cursor.personal_keys_blocked()
    cursor.read_override_base_url()
    assert modes == ["ro", "ro"]


@pytest.mark.parametrize(
    ("outcome", "answers", "calls", "message"),
    [
        (cursor.QUIT_DONE, "y\n", ["quit", "reopen"], "set up and reopened"),
        (
            cursor.QUIT_TIMEOUT,
            "y\ny\n",
            ["quit", "force", "reopen"],
            "force quit, set up",
        ),
        (cursor.QUIT_FAILED, "y\nn\n", ["quit"], "Close Cursor fully (File > Exit)"),
    ],
)
def test_windows_quit_offer(
    mac, monkeypatch, interactive, outcome, answers, calls, message
):
    monkeypatch.setattr("sys.platform", "win32")
    fake = FakeCursor(monkeypatch, outcome=outcome)
    result = _run(input=answers)
    assert result.exit_code == (1 if outcome == cursor.QUIT_FAILED else 0), (
        result.output
    )
    assert PROMPT in result.output
    assert message in result.output
    assert "macOS" not in result.output
    assert "Keychain" not in result.output
    assert fake.calls == calls


@pytest.mark.parametrize(
    ("force", "found", "remaining", "status", "expected"),
    [
        (False, [11, 12], [], 0, cursor.QUIT_DONE),
        (False, [11], [11], 0, cursor.QUIT_TIMEOUT),
        (False, [11], [11], 1, cursor.QUIT_FAILED),
        (False, None, None, 1, cursor.QUIT_FAILED),
        (True, [11, 12], [], 0, cursor.QUIT_FORCED),
        (True, [11], [11], 0, cursor.QUIT_FORCE_FAILED),
        (True, [11], [], 1, cursor.QUIT_DONE),
        (True, [], [], 0, cursor.QUIT_DONE),
    ],
)
def test_windows_taskkill(monkeypatch, force, found, remaining, status, expected):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("SystemRoot", r"D:\Win")
    monkeypatch.setattr(cursor, "running_pids", lambda: found)
    monkeypatch.setattr(cursor, "wait_until_quit", lambda timeout: remaining)
    monkeypatch.setattr(
        cursor, "_windows_image_path", lambda pid: r"D:\Apps\Cursor.exe"
    )
    monkeypatch.setattr(cursor, "_windows_cursor_image", None)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    commands = []

    def run(command, **kwargs):
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        commands.append(command)
        return subprocess.CompletedProcess(command, status)

    monkeypatch.setattr(subprocess, "run", run)
    assert (cursor.force_quit() if force else cursor.request_quit()) == expected
    switches = ["/F", "/T"] if force else []
    assert commands == [
        [
            r"D:\Win\System32\taskkill.exe",
            *switches,
            "/PID",
            str(pid),
            "/FI",
            "IMAGENAME eq Cursor.exe",
        ]
        for pid in found or []
    ]
    if found:
        assert cursor._windows_cursor_image == r"D:\Apps\Cursor.exe"


@pytest.mark.parametrize("cached", [None, r"D:\Apps\Cursor.exe"])
@pytest.mark.parametrize("launches", [True, False])
def test_windows_reopen(monkeypatch, cached, launches):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Local")
    monkeypatch.setattr(cursor, "_windows_cursor_image", cached)
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 8, raising=False)
    calls = []

    def launch(command, **kwargs):
        calls.append(command)
        assert kwargs["creationflags"] == subprocess.DETACHED_PROCESS
        if not launches:
            raise FileNotFoundError

    monkeypatch.setattr(subprocess, "Popen", launch)
    assert _REAL_REOPEN() is launches
    assert calls[0] == [cached or r"C:\Local\Programs\cursor\Cursor.exe"]
    if not launches:
        assert calls[-1] == [r"C:\Local\Programs\cursor\Cursor.exe"]


def test_windows_cursor_terminal_through_shell(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(cursor.os, "getppid", lambda: 22)
    monkeypatch.setattr(cursor, "running_pids", lambda: [11])
    monkeypatch.setattr(cursor, "_windows_parent_pids", lambda: {22: 17, 17: 11})
    assert _REAL_RUNS_INSIDE_CURSOR()
