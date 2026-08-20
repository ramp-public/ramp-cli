"""The session-start sync must be cheap, quiet, rate-limited, and fail-open."""

from __future__ import annotations

import fcntl
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

import ramp_cli.commands.router as router_module
import ramp_cli.commands.router_sync as sync_module
import ramp_cli.main as main_module
from ramp_cli import __version__
from ramp_cli.config.settings import config_dir
from ramp_cli.main import cli

# Captured at import time, before the conftest fixture pins the resolver, so
# the resolution logic itself stays testable.
_REAL_RAMP_EXECUTABLE = sync_module.ramp_executable

_NOTICE = (
    "Ramp CLI v99.0.0 is available — run `ramp update` for the latest Router features."
)


def _write_cooldown(age_seconds: float = 0) -> Path:
    path = config_dir() / "router-sync-last"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def _cache_update(version: str = "99.0.0") -> None:
    (config_dir()).mkdir(parents=True, exist_ok=True)
    (config_dir() / "latest-version.txt").write_text(version)


def _record_spawns(monkeypatch) -> list[bool]:
    spawned: list[bool] = []
    monkeypatch.setattr(
        router_module, "spawn_detached_refresh", lambda: spawned.append(True)
    )
    return spawned


def _sync(*args: str):
    return CliRunner().invoke(cli, ["--human", "router", "sync", *args])


@pytest.mark.parametrize(
    "scenario",
    ["fast_path", "opt_out", "expiry_spawns", "current_install", "missing_client"],
)
def test_hook_mode_is_silent_on_every_quiet_path(monkeypatch, scenario):
    """Silence table: stdout and stderr stay empty; only side effects vary."""
    args = ["--hook", "--client", "codex"]
    expected_spawns = 0
    if scenario == "fast_path":
        _write_cooldown()
    elif scenario == "opt_out":
        monkeypatch.setenv(sync_module.SYNC_OPT_OUT_ENV, "1")
    elif scenario == "expiry_spawns":
        expected_spawns = 1
    elif scenario == "current_install":
        _write_cooldown()
        _cache_update()
        monkeypatch.setattr("ramp_cli.version_check.__version__", "99.0.0")
    elif scenario == "missing_client":
        _cache_update()
        args = ["--hook"]
        expected_spawns = 1
    spawned = _record_spawns(monkeypatch)

    result = _sync(*args)

    assert result.exit_code == 0
    assert result.output == ""
    assert result.stderr == ""
    assert len(spawned) == expected_spawns
    if scenario == "opt_out":
        # Opting out must not start a cooldown that would mask a later opt-in.
        assert not (config_dir() / "router-sync-last").exists()
    elif scenario == "expiry_spawns":
        assert (config_dir() / "router-sync-last").exists()
    elif scenario == "missing_client":
        # The host's output contract is unknown, so emitting is riskier than
        # staying silent; the withholding is only logged.
        assert "without --client" in (config_dir() / "router-sync.log").read_text()


def test_hook_mode_emits_nothing_for_claude_even_while_pending(monkeypatch):
    # Claude's only notice surface is the statusline, which reads the
    # pending-update file; the hook stays silent on every channel.
    _cache_update()
    spawned = _record_spawns(monkeypatch)

    expiry = _sync("--hook", "--client", "claude-code")
    fast_path = _sync("--hook", "--client", "claude-code")

    assert expiry.exit_code == fast_path.exit_code == 0
    assert expiry.output == fast_path.output == ""
    assert expiry.stderr == fast_path.stderr == ""
    assert spawned == [True]


def test_hook_mode_maintains_the_pending_update_notice_file(monkeypatch, tmp_path):
    notice_file = tmp_path / "state" / "update-notice.json"
    monkeypatch.setattr(
        "ramp_cli.version_check.update_notice_path", lambda: notice_file
    )
    _write_cooldown()
    _cache_update()
    _record_spawns(monkeypatch)

    pending = _sync("--hook", "--client", "claude-code")

    assert pending.exit_code == 0
    payload = json.loads(notice_file.read_text())
    # The statusline contract: at least latest_version, present only while
    # the install is older than the latest release.
    assert payload["latest_version"] == "99.0.0"

    # An upgrade — ramp update or out-of-band via brew — clears it on the
    # next observation that the install is current.
    monkeypatch.setattr("ramp_cli.version_check.__version__", "99.0.0")
    current = _sync("--hook", "--client", "claude-code")

    assert current.exit_code == 0
    assert not notice_file.exists()


def test_hook_mode_fails_open_and_logs_to_the_state_dir(monkeypatch):
    def explode():
        raise RuntimeError("network down")

    monkeypatch.setattr(router_module, "spawn_detached_refresh", explode)

    result = _sync("--hook", "--client", "claude-code")

    assert result.exit_code == 0
    assert result.output == ""
    log = (config_dir() / "router-sync.log").read_text()
    assert "session sync failed" in log
    assert "network down" in log


def test_sync_without_internal_mode_points_to_refresh():
    result = CliRunner().invoke(cli, ["--human", "router", "sync"])

    assert result.exit_code == 2
    assert "router refresh" in result.output


def test_sync_is_due_only_after_the_cooldown():
    assert sync_module.sync_is_due()  # no state yet
    _write_cooldown()
    assert not sync_module.sync_is_due()
    _write_cooldown(age_seconds=sync_module.SYNC_COOLDOWN_SECONDS + 1)
    assert sync_module.sync_is_due()


@pytest.mark.parametrize("client", ["claude-code", "codex"])
def test_session_sync_hook_command_wraps_and_quotes_the_executable(monkeypatch, client):
    assert sync_module.session_sync_hook_command(client) == (
        "[ -x /opt/ramp-cli/bin/ramp ] && /opt/ramp-cli/bin/ramp "
        f"router sync --hook --client {client} || true"
    )

    monkeypatch.setattr(
        sync_module, "ramp_executable", lambda: Path("/Users/a user/.local/bin/ramp")
    )
    command = sync_module.session_sync_hook_command(client)
    assert "'/Users/a user/.local/bin/ramp'" in command
    assert command.endswith(f"router sync --hook --client {client} || true")


def test_session_sync_hook_command_is_skipped_when_it_cannot_run(monkeypatch):
    monkeypatch.setattr(sync_module, "ramp_executable", lambda: None)
    assert sync_module.session_sync_hook_command("codex") is None

    monkeypatch.setattr(sync_module.os, "name", "nt")
    assert sync_module.session_sync_hook_command("codex") is None


@pytest.mark.parametrize(
    "command,expected",
    [
        ("/usr/bin/ramp router sync --hook", True),
        ("nice /custom/ramp router sync --hook", True),
        ("RAMP_NO_SESSION_SYNC=1 /custom/ramp router sync --hook", True),
        ("env RAMP_NO_SESSION_SYNC=1 /custom/ramp router sync --hook", True),
        ("echo 'router sync --hook'", False),
        ("echo FOO=1 ramp router sync --hook", False),
        ("echo ramp router sync --hook", False),
        ("ramp router sync --hooked", False),
        ("ramp router sync --hook echo", False),
        ("# ramp router sync --hook", False),
    ],
)
def test_command_runs_session_sync_requires_an_invocation(command, expected):
    assert sync_module.command_runs_session_sync(command) is expected


def test_ramp_executable_resolution(monkeypatch, tmp_path):
    # The invoked entrypoint wins when it is a real executable named ramp.
    fake = tmp_path / "bin" / "ramp"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(fake)])
    assert _REAL_RAMP_EXECUTABLE() == fake

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["bin/ramp"])
    assert _REAL_RAMP_EXECUTABLE() == fake

    # Otherwise PATH lookup, then None.
    monkeypatch.setattr(sys, "argv", ["pytest"])
    monkeypatch.setattr(sync_module.shutil, "which", lambda _name: "/usr/bin/ramp")
    assert _REAL_RAMP_EXECUTABLE() == Path("/usr/bin/ramp")

    monkeypatch.setattr(sys, "argv", ["ramp"])
    assert _REAL_RAMP_EXECUTABLE() == Path("/usr/bin/ramp")

    monkeypatch.setattr(sync_module.shutil, "which", lambda _name: None)
    assert _REAL_RAMP_EXECUTABLE() is None


def test_spawn_detached_refresh_runs_the_resolved_cli_detached(monkeypatch):
    calls = {}

    def popen(command, **kwargs):
        calls["command"] = command
        calls["kwargs"] = kwargs

    monkeypatch.setattr(sync_module.subprocess, "Popen", popen)

    sync_module.spawn_detached_refresh()

    assert calls["command"] == [
        "/opt/ramp-cli/bin/ramp",
        "router",
        "sync",
        "--detached",
    ]
    assert calls["kwargs"]["start_new_session"] is True
    assert (config_dir() / "router-sync.log").exists()


def test_spawn_detached_refresh_reenters_the_interpreter_without_an_entrypoint(
    monkeypatch,
):
    calls = {}
    monkeypatch.setattr(sync_module, "ramp_executable", lambda: None)
    monkeypatch.setattr(
        sync_module.subprocess,
        "Popen",
        lambda command, **kwargs: calls.setdefault("command", command),
    )

    sync_module.spawn_detached_refresh()

    command = calls["command"]
    assert command[0] == sys.executable
    assert command[1] == "-c"
    assert command[-3:] == ["router", "sync", "--detached"]


def test_concurrent_session_starts_yield_exactly_one_syncer():
    _write_cooldown(age_seconds=sync_module.SYNC_COOLDOWN_SECONDS + 1)
    barrier = threading.Barrier(4)
    winners = []

    def race():
        barrier.wait()
        # The hook path's sequence: an unlocked dueness check, then the claim.
        if sync_module.sync_is_due() and sync_module.claim_sync_slot():
            winners.append(True)

    threads = [threading.Thread(target=race) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert winners == [True]


def test_claim_sync_slot_loses_while_another_process_holds_the_lock():
    _write_cooldown(age_seconds=sync_module.SYNC_COOLDOWN_SECONDS + 1)
    lock_path = config_dir() / "router-sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        try:
            assert not sync_module.claim_sync_slot()
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    # The holder never advanced the cooldown, so a later start still syncs.
    assert sync_module.claim_sync_slot()


def test_claim_sync_slot_requires_a_persisted_cooldown(monkeypatch):
    monkeypatch.setattr(sync_module, "touch_cooldown", lambda: False)

    assert not sync_module.claim_sync_slot()


def test_hook_invocation_never_starts_the_passive_update_check(monkeypatch):
    checks = []
    monkeypatch.setattr(main_module, "check_for_update", lambda: checks.append(True))
    monkeypatch.setattr(router_module, "spawn_detached_refresh", lambda: None)

    # No cooldown and no version cache: the worst case for accidental network.
    monkeypatch.setattr(
        sys,
        "argv",
        ["ramp", "--human", "router", "sync", "--hook", "--client", "codex"],
    )
    main_module.main()
    assert checks == []

    # The fast path stays zero-network too.
    monkeypatch.setattr(sys, "argv", ["ramp", "--quiet", "router", "sync", "--hook"])
    main_module.main()
    assert checks == []

    # Every other invocation keeps the passive check.
    monkeypatch.setattr(sys, "argv", ["ramp", "--human", "router", "sync"])
    with pytest.raises(SystemExit):
        main_module.main()
    assert checks == [True]


def test_two_back_to_back_session_starts_both_emit_the_notice(monkeypatch):
    # The user-visible regression: the refresh cooldown must never suppress
    # the warning. Refresh cadence and notice cadence are independent.
    _cache_update()
    spawned = _record_spawns(monkeypatch)

    first = _sync("--hook", "--client", "codex")
    second = _sync("--hook", "--client", "codex")

    assert first.exit_code == second.exit_code == 0
    assert json.loads(first.stdout) == {"systemMessage": _NOTICE}
    # The second run is the zero-network fast path and still warns.
    assert json.loads(second.stdout) == {"systemMessage": _NOTICE}
    assert first.stderr == second.stderr == ""
    # Only the first invocation crossed the cooldown and spawned a refresh.
    assert spawned == [True]


def test_the_codex_notice_names_a_newer_server_recommended_version(monkeypatch):
    _write_cooldown()
    _cache_update("99.0.0")
    (config_dir() / "server-recommended-version").write_text("100.0.0\n")

    result = _sync("--hook", "--client", "codex")

    assert result.exit_code == 0
    message = json.loads(result.stdout)["systemMessage"]
    assert "v100.0.0" in message


def test_every_invocation_records_the_installed_version(monkeypatch):
    monkeypatch.setattr(main_module, "check_for_update", lambda: None)
    monkeypatch.setattr(router_module, "spawn_detached_refresh", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["ramp", "--quiet", "router", "sync", "--hook", "--client", "codex"],
    )

    main_module.main()

    assert (config_dir() / "installed-version").read_text() == __version__


def test_a_lock_losing_invocation_still_emits_the_notice(monkeypatch):
    _write_cooldown(age_seconds=sync_module.SYNC_COOLDOWN_SECONDS + 1)
    _cache_update()
    spawned = _record_spawns(monkeypatch)
    lock_path = config_dir() / "router-sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        try:
            result = _sync("--hook", "--client", "codex")
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)

    # A concurrent session start owns the refresh; this one still warns.
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"systemMessage": _NOTICE}
    assert spawned == []


def test_the_detached_sync_refreshes_the_version_cache(monkeypatch):
    # The hook path never fetches, so the detached half must be what keeps
    # "latest" fresh — synchronously, not via the daemon thread that dies
    # with short-lived processes.
    monkeypatch.setattr("ramp_cli.version_check.latest_version", lambda: "99.0.0")

    result = _sync("--detached")

    assert result.exit_code == 0
    assert (config_dir() / "latest-version.txt").read_text() == "99.0.0"
    # The freshly cached pending state is reconciled into the notice file
    # in the same pass, and the refresh itself still ran.
    payload = json.loads((config_dir() / "update-notice.json").read_text())
    assert payload["latest_version"] == "99.0.0"
    assert "Ramp Router is not configured" in result.output


def test_the_detached_sync_respects_the_update_check_opt_out(monkeypatch):
    monkeypatch.setenv("RAMP_NO_UPDATE_CHECK", "1")
    fetches = []
    monkeypatch.setattr(
        "ramp_cli.version_check.latest_version", lambda: fetches.append(True)
    )

    result = _sync("--detached")

    assert result.exit_code == 0
    assert fetches == []
    assert "Ramp Router is not configured" in result.output


def test_an_unwritable_state_dir_fails_closed(monkeypatch, tmp_path):
    # If the claim cannot be persisted, syncing anyway would spawn a network
    # refresh on every session start forever; staying quiet is the safe side.
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    # A state dir nested under a regular file fails every write with OSError
    # even when the tests run as root, unlike a chmod'd directory.
    monkeypatch.setattr(sync_module, "config_dir", lambda: blocker / "ramp")
    spawned = _record_spawns(monkeypatch)

    result = _sync("--hook", "--client", "codex")

    assert result.exit_code == 0
    assert result.output == ""
    assert result.stderr == ""
    assert spawned == []
