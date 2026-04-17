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
    """Fetch latest version and write to cache. Runs in background thread."""
    version = latest_version()
    if version:
        _write_cache(version)


def check_for_update() -> None:
    """Kick off a background version check if cooldown has expired."""
    if os.environ.get("RAMP_NO_UPDATE_CHECK"):
        return
    if not _cooldown_expired():
        return
    t = threading.Thread(target=_do_check, daemon=True)
    t.start()


def get_update_info() -> dict[str, str] | None:
    """Return update info if a newer version is cached, else None.

    Returns {"current": "0.1.3", "latest": "0.1.4"} or None.
    """
    cached = _read_cache()
    if not cached:
        return None
    try:
        if parse_version(cached) > parse_version(__version__):
            return {"current": __version__, "latest": cached}
    except (ValueError, TypeError):
        pass
    return None


def get_update_warning() -> str | None:
    """Return a human-readable warning if an update is available, else None."""
    info = get_update_info()
    if not info:
        return None
    return (
        f"\u26a0  Update available: v{info['current']} \u2192 v{info['latest']}"
        f" \u2014 run `ramp update` to upgrade"
    )


def emit_update_notice(agent_mode: bool) -> None:
    """Print update notice to stderr if an update is available.

    In human mode, prints a styled warning.
    In agent mode, prints a JSON object so agents can detect and act on it.
    """
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
