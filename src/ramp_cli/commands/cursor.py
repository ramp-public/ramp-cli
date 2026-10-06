"""Cursor detection for Ramp Router setup.

Cursor keeps its provider settings in its own account-synced UI, so Router
setup never writes them; ``ramp router configure cursor`` hands over a key and
the values to paste instead. What can be read safely is whether Cursor is on
this machine and where its "Override OpenAI Base URL" setting points, which
Cursor mirrors into a local SQLite state database.
"""

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

# Redirects Cursor's user-data directory. Tests set it so detection never
# reads a developer's real Cursor state, and the app bundle is then ignored.
USER_DIR_ENV = "RAMP_CURSOR_USER_DIR"
_APP_BUNDLE = Path("/Applications/Cursor.app")
# The row of Cursor's state database holding its application settings as JSON,
# the OpenAI base-URL override among them.
_SETTINGS_KEY = (
    "src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl"
    ".persistentStorage.applicationUser"
)


def user_dir() -> Path:
    """Locate Cursor's user-data directory for this platform."""
    configured = os.environ.get(USER_DIR_ENV)
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "Cursor" / "User"


def is_installed() -> bool:
    """Report whether Cursor appears to be present on this machine."""
    if user_dir().exists():
        return True
    if USER_DIR_ENV in os.environ:
        return False
    return _APP_BUNDLE.exists() or shutil.which("cursor") is not None


def override_base_url() -> str | None:
    """Return Cursor's OpenAI base-URL override, or None when unset or unreadable.

    The database is opened read-only: Cursor may be running and owns it.
    """
    path = user_dir() / "globalStorage" / "state.vscdb"
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1)
        try:
            row = connection.execute(
                "SELECT value FROM ItemTable WHERE key = ?", (_SETTINGS_KEY,)
            ).fetchone()
        finally:
            connection.close()
        settings = json.loads(row[0]) if row else {}
    except (sqlite3.Error, OSError, UnicodeError, ValueError, TypeError):
        return None
    value = settings.get("openAIBaseUrl") if isinstance(settings, dict) else None
    if not isinstance(value, str):
        return None
    return value.strip().rstrip("/") or None
