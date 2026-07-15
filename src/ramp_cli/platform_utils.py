"""Small platform helpers for Windows shell compatibility."""

from __future__ import annotations

import os
import re
import sys

_MSYS_DRIVE_PATH = re.compile(r"^/([a-zA-Z])(?:/|$)")


def is_git_bash_windows() -> bool:
    """Return True when running from Git Bash/MSYS on Windows."""

    if not os.environ.get("MSYSTEM"):
        return False
    return sys.platform == "win32" or sys.platform.startswith("msys")


def normalize_msys_path(path: str) -> str:
    """Convert a Git Bash drive path like /c/Users/me to C:/Users/me."""

    if not is_git_bash_windows() or sys.platform != "win32":
        return path

    match = _MSYS_DRIVE_PATH.match(path)
    if not match:
        return path

    drive = match.group(1).upper()
    rest = path[3:] if len(path) > 2 else ""
    return f"{drive}:/{rest}"
