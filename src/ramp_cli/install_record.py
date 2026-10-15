"""Detect installs managed by the @ramp/cli and @ramp/router npm packages."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

_OWNER = "@ramp/cli"
_DEFAULT_PACKAGE = "@ramp/router"


def _candidates() -> list[str]:
    return [sys.executable, sys.argv[0] if sys.argv else ""]


def _located_record() -> tuple[Path, dict[str, Any]] | None:
    for candidate in _candidates():
        if not candidate:
            continue
        try:
            directory = Path(candidate).resolve().parent
            record = json.loads(
                (directory / "install.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("owner") == _OWNER:
            return directory, record
    return None


def npm_install_record() -> dict[str, Any] | None:
    """Return the npm install record when this binary was installed by npm.

    The npm installer writes ``install.json`` next to the native executable.
    """
    located = _located_record()
    return located[1] if located else None


def npm_entrypoint() -> Path | None:
    """The stable ``ramp`` link the npm installer keeps beside the executable.

    The executable itself is architecture-named, and the npm shims on PATH may
    live in a temporary npx cache, so this is the path to persist in hooks.
    """
    located = _located_record()
    if located is None:
        return None
    entry = located[0] / "ramp"
    return entry if entry.is_file() and os.access(entry, os.X_OK) else None


def npm_update_command() -> list[str] | None:
    """The cache-independent npm updater installed beside this executable.

    Direct Windows invocation falls back to instructions: its running native
    executable cannot be replaced. The Node launcher has no such restriction.
    """
    if sys.platform == "win32":
        return None
    located = _located_record()
    node = shutil.which("node")
    if located is None or node is None:
        return None
    helper = located[0] / "npm-update.js"
    return [node, str(helper)] if helper.is_file() else None


def record_channel(record: dict[str, Any]) -> str:
    channel = record.get("channel")
    return channel if isinstance(channel, str) and channel else "public"


def record_package(record: dict[str, Any]) -> str:
    package = record.get("package")
    return package if package in ("@ramp/cli", "@ramp/router") else _DEFAULT_PACKAGE


def npm_update_lines(record: dict[str, Any]) -> list[str]:
    package = record_package(record)
    return [
        f"This ramp was installed by npm ({package}). Update it with npm:",
        f"  npm update -g {package}",
        f"  npx {package}@latest",
    ]
