"""Passive version-update detection with daily caching.

Checks the GitHub releases API at most once per day. When a newer version
is available, provides warning strings for both human and agent output modes.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import httpx

from ramp_cli import __version__
from ramp_cli.config.settings import config_dir

_PUBLIC_REPO = "ramp-public/ramp-cli"
_COOLDOWN_SECONDS = 86400  # 24 hours
_SUPPRESS_NEXT_UPDATE_NOTICE = False


def parse_version(v: str) -> tuple[int, ...]:
    """Parse a semver string like '0.1.3' into a comparable tuple."""
    return tuple(int(x) for x in v.split("."))


def latest_version() -> str | None:
    """Fetch the latest release version from the public GitHub API."""
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"https://api.github.com/repos/{_PUBLIC_REPO}/releases/latest",
                headers={"Accept": "application/vnd.github+json"},
                follow_redirects=True,
            )
            if resp.status_code == 200:
                tag = resp.json().get("tag_name", "")
                if tag:
                    return tag.lstrip("v")
    except Exception:
        pass
    return None


def _cache_path() -> Path:
    return config_dir() / "latest-version.txt"


def installed_version_path() -> Path:
    override = os.environ.get("RAMP_INSTALLED_VERSION_FILE")
    return Path(override) if override else config_dir() / "installed-version"


def record_installed_version() -> None:
    """Keep the installed-version file naming this binary's own version.

    Contract with the Router-served Claude statusline: it compares the
    server-reported recommended CLI version against this plain-text file,
    with zero subprocess calls. Runs on every CLI invocation but writes only
    when the content differs — the fast path is one read — so it self-heals
    right after `ramp update`: the new binary rewrites it on its first run.
    """
    try:
        path = installed_version_path()
        try:
            if path.read_text() == __version__:
                return
        except OSError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(__version__)
        tmp.replace(path)  # atomic on POSIX
    except OSError:
        pass


def server_recommended_version_path() -> Path:
    override = os.environ.get("RAMP_SERVER_RECOMMENDED_VERSION_FILE")
    return Path(override) if override else config_dir() / "server-recommended-version"


def _read_server_recommended() -> str | None:
    """The CLI version the Router server last recommended, or None.

    The Router-served Codex cost hook records the session-usage endpoint's
    `recommended_cli_version` here after each turn. This side only ever reads
    the file — never the network — and fails open: a missing file is treated
    as absent.
    """
    # The opt-out's observed contract has always been "no update nudges" (it
    # starves the cache); the Router-served cost hook writes this file
    # out-of-band, so it must not resurrect nudges for opted-out users.
    if os.environ.get("RAMP_NO_UPDATE_CHECK"):
        return None
    try:
        return server_recommended_version_path().read_text().strip()
    except OSError:
        return None


def _read_cache() -> str | None:
    """Read cached latest version, or None if cache doesn't exist."""
    path = _cache_path()
    if not path.exists():
        return None
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _write_cache(version: str) -> None:
    """Write latest version to cache file (atomic via rename)."""
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(version)
        tmp.replace(path)  # atomic on POSIX
    except OSError:
        pass


def _cooldown_expired() -> bool:
    """Return True if enough time has passed since last check."""
    path = _cache_path()
    if not path.exists():
        return True
    try:
        age = time.time() - path.stat().st_mtime
        return age >= _COOLDOWN_SECONDS
    except OSError:
        return True


def _do_check() -> None:
    """The passive check's background-thread body."""
    refresh_version_cache()


def refresh_version_cache() -> None:
    """Fetch and cache the latest release version, synchronously.

    The session-start hook never touches the network, and the passive check's
    daemon thread dies with its process — often before the fetch completes in
    short-lived commands. A machine where ramp only ever runs through hooks
    would therefore keep a permanently stale "latest" and the update notice
    could silently never fire. The detached background sync calls this, where
    blocking on the network is fine.
    """
    if os.environ.get("RAMP_NO_UPDATE_CHECK"):
        return
    version = latest_version()
    if version:
        _write_cache(version)
    sync_update_notice_file()


def check_for_update() -> None:
    """Kick off a background version check if cooldown has expired."""
    if os.environ.get("RAMP_NO_UPDATE_CHECK"):
        return
    # Cached-state reconcile on every start, so the statusline's pending file
    # clears right after any upgrade — `ramp update` or out-of-band via brew.
    sync_update_notice_file()
    if not _cooldown_expired():
        return
    t = threading.Thread(target=_do_check, daemon=True)
    t.start()


def get_update_info() -> dict[str, str] | None:
    """Return update info if a newer version is known, else None.

    "Newer" is judged against the max of the two local sources: the cached
    GitHub latest release and the server-recommended version the Codex cost
    hook records. Both are plain reads — never a fetch.

    Returns {"current": "0.1.3", "latest": "0.1.4"} or None.
    """
    candidates: list[tuple[tuple[int, ...], str]] = []
    for candidate in (_read_cache(), _read_server_recommended()):
        if not candidate:
            continue
        try:
            candidates.append((parse_version(candidate), candidate))
        except (ValueError, TypeError):
            continue
    if not candidates:
        return None
    parsed_latest, latest = max(candidates)
    try:
        if parsed_latest > parse_version(__version__):
            return {"current": __version__, "latest": latest}
    except (ValueError, TypeError):
        pass
    return None


def update_notice_path() -> Path:
    return config_dir() / "update-notice.json"


def sync_update_notice_file() -> None:
    """Keep the pending-update notice file in step with the cached state.

    Contract with the Claude statusline, which renders a persistent nudge
    from it: the file exists exactly while the install is older than the
    latest cached release, holds at least {"latest_version": ...}, and is
    removed the moment the install is current. Reconciled from cached state
    only — never the network — and fail-open on both sides.
    """
    try:
        path = update_notice_path()
        info = get_update_info()
        if info is None:
            path.unlink(missing_ok=True)
            return
        rendered = (
            json.dumps(
                {
                    "schema_version": 1,
                    "latest_version": info["latest"],
                    "current_version": info["current"],
                },
                indent=2,
            )
            + "\n"
        )
        try:
            if path.read_text() == rendered:
                return
        except OSError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(rendered)
        tmp.replace(path)  # atomic on POSIX
    except OSError:
        pass


def get_update_warning() -> str | None:
    """Return a human-readable warning if an update is available, else None."""
    info = get_update_info()
    if not info:
        return None
    return (
        f"\u26a0  Update available: v{info['current']} \u2192 v{info['latest']}"
        f" \u2014 run `ramp update` to upgrade"
    )


def suppress_next_update_notice() -> None:
    """Skip the next passive update notice emitted at CLI shutdown."""
    global _SUPPRESS_NEXT_UPDATE_NOTICE
    _SUPPRESS_NEXT_UPDATE_NOTICE = True


def emit_update_notice(agent_mode: bool) -> None:
    """Print update notice to stderr if an update is available.

    In human mode, prints a styled warning.
    In agent mode, prints a JSON object so agents can detect and act on it.
    """
    global _SUPPRESS_NEXT_UPDATE_NOTICE
    if _SUPPRESS_NEXT_UPDATE_NOTICE:
        _SUPPRESS_NEXT_UPDATE_NOTICE = False
        return

    if agent_mode:
        info = get_update_info()
        if not info:
            return
        notice = {
            "update_available": {
                "current": info["current"],
                "latest": info["latest"],
                "command": "ramp update",
            }
        }
        print(json.dumps(notice), file=sys.stderr)
    else:
        warning = get_update_warning()
        if warning:
            print(f"\n{warning}", file=sys.stderr)
