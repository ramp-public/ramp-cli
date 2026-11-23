"""Rate-limited session-start sync for installed Router artifacts.

Configure/refresh register a session-start sync in Claude Code, Codex, and
OpenCode that runs `ramp router sync --hook`, keeping each setup's model list
and default model current. The invocation is cheap and fail-open: the common
case is one stat() with zero network I/O, and an expired cooldown spawns a
models-only refresh detached so session start never waits. That refresh also
repairs missing CLI-owned OpenCode/Pi plugin paths after upgrades, without
resetting plugin options. Other settings are the user's between configures;
`ramp router refresh` (which
`ramp update` runs) is what repairs the rest of a setup.
"""

from __future__ import annotations

import contextlib
import ntpath
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ramp_cli import cost_script
from ramp_cli.config.settings import config_dir
from ramp_cli.install_record import npm_entrypoint
from ramp_cli.version_check import get_update_info

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None

# Session starts are frequent, so the interval trades freshness for staying
# invisible: at most half a day stale, one stat() in the common case.
SYNC_COOLDOWN_SECONDS = 12 * 60 * 60
# Opt-out; the hooks stay registered but do nothing.
SYNC_OPT_OUT_ENV = "RAMP_NO_SESSION_SYNC"
SYNC_HOOK_MARKER = "router sync --hook"
# Receipt sentinel: a user-authored hook already invokes the sync, so there
# is nothing managed to repair or remove.
SYNC_HOOK_USER_MANAGED = "user-managed"


def _cooldown_path() -> Path:
    # Same state area as the daily version check; the mtime is the record.
    return config_dir() / "router-sync-last"


def log_path() -> Path:
    return config_dir() / "router-sync.log"


def job_lock_dir() -> Path:
    """Where the per-user sync and schedule locks and the opt-out marker live.

    Not config_dir(): that follows XDG_CONFIG_HOME, and the scheduled job runs
    with the value it was installed with. An interactive run under another
    XDG_CONFIG_HOME would otherwise take a lock the running job is not holding
    and reload or remove the job mid-write, or leave an opt-out marker the job
    never reads.
    """
    return Path.home() / ".config" / "ramp"


def try_lock(lock_file) -> bool:
    """Take an exclusive lock on an open lock file without waiting.

    flock on POSIX; on Windows a byte-range lock on byte 0 (the files are never
    read or written, so the lock's mandatory semantics affect nothing). With
    neither available, the lock is not enforced.
    """
    if fcntl is not None:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True
    if msvcrt is not None:
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
    return True


def unlock(lock_file) -> None:
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:
        lock_file.seek(0)
        with contextlib.suppress(OSError):
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def sync_is_due() -> bool:
    """Report whether the cooldown has elapsed: one stat(), no network."""
    try:
        age = time.time() - _cooldown_path().stat().st_mtime
    except OSError:
        return True
    return age >= SYNC_COOLDOWN_SECONDS


def cooldown_due_at() -> float:
    """When the cooldown ends (now if it already has)."""
    try:
        return _cooldown_path().stat().st_mtime + SYNC_COOLDOWN_SECONDS
    except OSError:
        return time.time()


def touch_cooldown() -> bool:
    """Start a new cooldown interval; syncing too often is the safe failure."""
    try:
        path = _cooldown_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return True
    except OSError:
        return False


def claim_sync_slot() -> bool:
    """Atomically claim an expired cooldown; report whether this process won.

    Concurrent session starts can all see an expired cooldown, so dueness is
    re-checked and the mtime advanced under a non-blocking exclusive lock on a
    dedicated lock file — exactly one caller syncs.
    """
    try:
        lock_path = job_lock_dir() / "router-sync.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            if not try_lock(lock_file):
                # Another session start is already syncing.
                return False
            try:
                if not sync_is_due():
                    # A winner advanced the cooldown before we got the lock.
                    return False
                return touch_cooldown()
            finally:
                unlock(lock_file)
    except OSError as exc:
        # Fail closed: a state dir that can hold neither the lock nor the
        # cooldown would otherwise spawn a network refresh on every single
        # session start, forever.
        log_failure(f"could not claim the sync cooldown: {exc!r}")
        return False


@contextmanager
def sync_lock(wait: float = 0) -> Iterator[bool]:
    """Hold the sync lock for the whole block; yield False if another run has it.

    Lock-only counterpart of claim_sync_slot (same file, no cooldown check),
    for the scheduled job, whose refresh must not overlap another sync.
    ``wait`` seconds are spent retrying before giving up, for callers that
    must not act while a refresh is writing.
    """
    try:
        lock_path = job_lock_dir() / "router-sync.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a+b")
    except OSError as exc:
        log_failure(f"could not open the sync lock: {exc!r}")
        yield False
        return
    with lock_file:
        deadline = time.monotonic() + wait
        while not try_lock(lock_file):
            if time.monotonic() >= deadline:
                yield False
                return
            time.sleep(0.25)
        try:
            yield True
        finally:
            unlock(lock_file)


def update_notice() -> str | None:
    """One short line while a newer CLI release is pending, else None.

    Read from the version-check cache; never its own request.
    """
    info = get_update_info()
    if not info:
        return None
    return (
        f"Ramp CLI v{info['latest']} is available — run `ramp update` "
        "for the latest Router features."
    )


def _windows_executable(invoked: Path) -> Path | None:
    """The ramp .exe that argv[0] names on Windows, or None.

    The installer's ramp.cmd starts ramp-windows-amd64.exe, and a pip or uv
    install has Scripts\\ramp.exe (argv[0] may omit the suffix). Background jobs
    need the .exe: one started through a .cmd runs cmd.exe, and the agent
    binary ships beside the .exe.
    """
    candidate = invoked if invoked.suffix else invoked.with_suffix(".exe")
    if (
        candidate.suffix.lower() == ".exe"
        and candidate.stem.lower() in ("ramp", "ramp-windows-amd64")
        and candidate.is_file()
    ):
        return candidate
    return None


def ramp_executable() -> Path | None:
    """The absolute path to the ramp entrypoint this process runs as, or None.

    argv[0] names the exact install being run; an npm-managed install's
    stable entrypoint is next, since npx can put a temporary shim first on
    PATH; PATH lookup is the fallback. Symlinks are kept — an
    installer-managed link is the stable name.
    """
    invoked_text = sys.argv[0] or ""
    invoked = Path(invoked_text)
    path_qualified = (
        invoked.is_absolute()
        or os.sep in invoked_text
        or (os.altsep is not None and os.altsep in invoked_text)
    )
    if invoked.name == "ramp" and path_qualified:
        candidate = Path(os.path.abspath(invoked))
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    if os.name == "nt" and path_qualified:
        candidate = _windows_executable(Path(os.path.abspath(invoked)))
        if candidate is not None:
            return candidate
    entrypoint = npm_entrypoint()
    if entrypoint is not None:
        return entrypoint
    resolved = shutil.which("ramp")
    return Path(os.path.abspath(resolved)) if resolved else None


# The hook event a sync command declares when it is not the session start.
# Codex's Stop hook reruns the same command after every turn so the Codex
# default model is reset for the next session; the flag keeps that run from
# emitting the session-start envelope on a channel with other semantics.
SYNC_HOOK_STOP_EVENT = "stop"


def session_sync_hook_command(client: str, event: str | None = None) -> str | None:
    """The SessionStart command written into one agent's hook config, or None.

    The `[ -x ]` guard and `|| true` make a moved binary or failing sync a
    silent no-op at session start; on Windows the guard file does the same
    (see ``WINDOWS_HOOK_GUARD``). --client names the hosting agent so hook
    mode can emit that agent's structured output instead of plain stdout,
    which both agents inject into the model's context. ``event`` names a
    non-session-start hook event (see ``SYNC_HOOK_STOP_EVENT``) the same
    command is registered for.
    """
    suffix = f" --event {event}" if event else ""
    if os.name == "nt":
        return _windows_session_sync_hook_command(client, suffix)
    executable = ramp_executable()
    if executable is None:
        return None
    quoted = shlex.quote(str(executable))
    return (
        f"[ -x {quoted} ] && {quoted} {SYNC_HOOK_MARKER} --client {client}{suffix}"
        " || true"
    )


# On Windows the hook starts with this batch file, which runs the rest of
# the command only while that ramp exists and always exits 0: the POSIX
# command's `[ -x ]` and `|| true`, which no single line can spell for Git
# Bash, PowerShell and cmd.exe at once, but which all three run as a program.
# Constant, so a ramp that moves only changes the hook command. Kept in the
# state folder with the sync's lock and cooldown, which uninstalling ramp
# leaves in place, so hooks left behind stay silent.
WINDOWS_HOOK_GUARD = "router-sync-hook.cmd"
_WINDOWS_HOOK_GUARD_SCRIPT = (
    "@echo off\r\n"
    "rem Written by ramp for its agent hooks: runs ramp only while it is\r\n"
    "rem installed, and never fails the agent's session.\r\n"
    'if exist "%~f1" call "%~f1" %2 %3 %4 %5 %6 %7 %8 %9\r\n'
    "exit /b 0\r\n"
)


def _windows_session_sync_hook_command(client: str, suffix: str) -> str | None:
    """The Windows form, as bare words; see cost_script.

    Claude Code runs hooks through Git Bash or PowerShell, so its paths use
    forward slashes like the status line; Codex runs them through cmd.exe or
    PowerShell, so its paths keep backslashes like the cost hook.
    """
    executable = cost_script.windows_executable()
    guard = _windows_hook_guard()
    if executable is None or guard is None:
        return None
    native = client != "claude-code"
    words = [
        cost_script.bare_path(str(guard), native=native),
        cost_script.bare_path(executable, native=native),
    ]
    if not all(words):
        return None
    return f"{' '.join(words)} {SYNC_HOOK_MARKER} --client {client}{suffix}"  # type: ignore[arg-type]


def _windows_hook_guard() -> Path | None:
    """Write the guard file if it is missing or stale; None if it can't be."""
    path = job_lock_dir() / WINDOWS_HOOK_GUARD
    try:
        if path.read_bytes() == _WINDOWS_HOOK_GUARD_SCRIPT.encode("ascii"):
            return path
    except OSError:
        pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_WINDOWS_HOOK_GUARD_SCRIPT.encode("ascii"))
    except OSError as exc:
        log_failure(f"could not write the Windows hook guard: {exc!r}")
        return None
    return path


# Windows runs the release binary under its own name, ramp-windows-amd64.exe.
_RAMP_PROGRAM = re.compile(r"ramp|ramp(-[a-z0-9]+)*\.exe", re.IGNORECASE)


def _is_ramp(word: str) -> bool:
    # ntpath splits on both separators, for the Windows forms of the hook.
    return _RAMP_PROGRAM.fullmatch(ntpath.basename(word)) is not None


def command_runs_session_sync(
    command: str, event: str | None = None, client: str | None = None
) -> bool:
    """Return whether a shell command actually invokes `ramp router sync --hook`.

    With ``event`` None this recognizes the session-start registration only;
    with an event name it recognizes the registration for that event. With
    ``client`` set, only a registration naming that client counts: a hook for
    another client (or for none) does not run that client's work.
    """
    # POSIX parsing first, so `My\ Tools/ramp` keeps its escaped space; then,
    # for the Codex form on Windows, whose backslashes are path separators.
    for posix in (True, False) if "\\" in command else (True,):
        try:
            tokens = shlex.split(command, posix=posix)
        except ValueError:
            continue
        if _tokens_run_session_sync(tokens, event, client):
            return True
    return False


def _tokens_run_session_sync(
    tokens: list[str], event: str | None, client: str | None
) -> bool:
    if tokens and ntpath.basename(tokens[0]).lower() == WINDOWS_HOOK_GUARD:
        tokens = tokens[1:]
    if tokens[-2:] == ["||", "true"]:
        tokens = tokens[:-2]
    if len(tokens) >= 6 and tokens[:2] == ["[", "-x"] and tokens[3:5] == ["]", "&&"]:
        if tokens[2] != tokens[5]:
            return False
        tokens = tokens[5:]
    if tokens and Path(tokens[0]).name == "env":
        tokens = tokens[1:]
    while tokens:
        name, separator, _value = tokens[0].partition("=")
        if not separator or not name.isidentifier():
            break
        tokens = tokens[1:]
    if tokens and tokens[0] == "nice":
        tokens = tokens[1:]
    if len(tokens) < 4 or not _is_ramp(tokens[0]):
        return False
    if tokens[1:4] != ["router", "sync", "--hook"]:
        return False
    tail = tokens[4:]
    if event is not None:
        if tail[-2:] != ["--event", event]:
            return False
        tail = tail[:-2]
    if client is not None:
        return tail == ["--client", client]
    return tail in (
        [],
        ["--client", "claude-code"],
        ["--client", "codex"],
        ["--client", "opencode"],
    )


def spawn_detached_refresh() -> None:
    """Run the background half of sync detached; the log keeps the latest run.

    The child runs `router sync --detached`: a synchronous version-cache
    refresh — the knowledge behind the update notice, which the hook path
    itself must never fetch — followed by `router refresh`.
    """
    executable = (
        cost_script.windows_executable() if os.name == "nt" else ramp_executable()
    )
    arguments = ["router", "sync", "--detached"]
    command = (
        [str(executable), *arguments]
        if executable is not None
        # No named entrypoint: re-enter through the running interpreter.
        # Dev installs only — a compiled binary always resolves through
        # ramp_executable()/argv[0]. click reads sys.argv[1:], which under
        # -c is the args after the program text.
        else [
            sys.executable,
            "-P",  # The hosting agent's project is not a trusted import path.
            "-c",
            "from ramp_cli.main import main; main()",
            *arguments,
        ]
    )
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")  # keep only the latest run
    # Append mode, so the child's writes interleave with (rather than
    # overwrite) anything log_failure adds while it runs.
    with path.open("a", encoding="utf-8") as log:
        if os.name != "nt":
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            return
        # No console window, and out of the agent's job object where it
        # allows that, so closing the agent doesn't end the refresh.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        for breakaway in (subprocess.CREATE_BREAKAWAY_FROM_JOB, 0):
            try:
                subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    creationflags=flags | breakaway,
                )
                return
            except PermissionError:
                # The job forbids breaking away; run inside it instead.
                if not breakaway:
                    raise


def log_failure(message: str) -> None:
    """Record a hook-path failure without touching the session's streams."""
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as log:
            timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            log.write(f"{timestamp} {message}\n")
    except OSError:
        pass
