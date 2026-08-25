"""Rate-limited session-start sync for installed Router artifacts.

Configure/refresh register a session-start sync in Claude Code, Codex, and
OpenCode that runs `ramp router sync --hook`, keeping the locally installed
artifacts (Codex catalog snapshot and instructions, Claude statusline, default
model ids, plugin paths) from going stale. The invocation is cheap and
fail-open: the common case is one stat() with zero network I/O, and an expired
cooldown spawns `ramp router refresh` detached so session start never waits.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ramp_cli.config.settings import config_dir
from ramp_cli.version_check import get_update_info

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

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


def sync_is_due() -> bool:
    """Report whether the cooldown has elapsed: one stat(), no network."""
    try:
        age = time.time() - _cooldown_path().stat().st_mtime
    except OSError:
        return True
    return age >= SYNC_COOLDOWN_SECONDS


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
    dedicated lock file — exactly one caller syncs. POSIX-only locking, like
    the hooks that call this.
    """
    try:
        lock_path = config_dir() / "router-sync.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            if fcntl is not None:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    # Another session start is already syncing.
                    return False
            try:
                if not sync_is_due():
                    # A winner advanced the cooldown before we got the lock.
                    return False
                return touch_cooldown()
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        # Fail closed: a state dir that can hold neither the lock nor the
        # cooldown would otherwise spawn a network refresh on every single
        # session start, forever.
        log_failure(f"could not claim the sync cooldown: {exc!r}")
        return False


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


def ramp_executable() -> Path | None:
    """The absolute path to the ramp entrypoint this process runs as, or None.

    argv[0] names the exact install being run; PATH lookup is the fallback.
    Symlinks are kept — an installer-managed link is the stable name.
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
    resolved = shutil.which("ramp")
    return Path(os.path.abspath(resolved)) if resolved else None


def session_sync_hook_command(client: str) -> str | None:
    """The SessionStart command written into one agent's hook config, or None.

    POSIX-only, like the cost scripts. The `[ -x ]` guard and `|| true` make
    a moved binary or failing sync a silent no-op at session start. --client
    names the hosting agent so hook mode can emit that agent's structured
    output instead of plain stdout, which both agents inject into the model's
    context.
    """
    if os.name == "nt":
        return None
    executable = ramp_executable()
    if executable is None:
        return None
    quoted = shlex.quote(str(executable))
    return f"[ -x {quoted} ] && {quoted} {SYNC_HOOK_MARKER} --client {client} || true"


def command_runs_session_sync(command: str) -> bool:
    """Return whether a shell command actually invokes `ramp router sync --hook`."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
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
    if len(tokens) < 4 or Path(tokens[0]).name != "ramp":
        return False
    if tokens[1:4] != ["router", "sync", "--hook"]:
        return False
    return tokens[4:] in (
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
    executable = ramp_executable()
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
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )


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
