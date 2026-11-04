"""Per-user Router preferences shared by the CLI and the harness plugins.

The Pi and OpenCode plugins read this file directly, so the path and key are a
contract with their ``harnessWidgetEnabled`` readers.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ramp_cli.commands import claude_code, router_sync

SETTINGS_FILENAME = "router-settings.json"
HARNESS_WIDGET_KEY = "harness_widget_enabled"


def settings_path() -> Path:
    return router_sync.job_lock_dir() / SETTINGS_FILENAME


def _read() -> dict:
    try:
        settings = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return settings if isinstance(settings, dict) else {}


def harness_widget_enabled() -> bool:
    """Only a literal false disables, so a damaged file fails open."""
    return _read().get(HARNESS_WIDGET_KEY) is not False


def set_harness_widget_enabled(enabled: bool) -> None:
    path = settings_path()
    with claude_code.advisory_lock(path.parent / f".{SETTINGS_FILENAME}.lock"):
        content = json.dumps({**_read(), HARNESS_WIDGET_KEY: enabled}, indent=2) + "\n"
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{SETTINGS_FILENAME}.", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
