"""Manages a short-lived external session ID for agent-tool API calls.

The session ID is a random UUID that resets after 5 minutes of inactivity
(no API calls). Every call to ``get_session_id()`` refreshes the timer.
"""

from __future__ import annotations

import time
import uuid

SESSION_TIMEOUT = 300  # 5 minutes in seconds

_session_id: str | None = None
_last_active: float = 0.0


def get_session_id() -> str:
    """Return the current session ID, creating or rotating as needed."""
    global _session_id, _last_active

    now = time.monotonic()
    if _session_id is None or (now - _last_active) >= SESSION_TIMEOUT:
        _session_id = str(uuid.uuid4())

    _last_active = now
    return _session_id


def reset() -> None:
    """Force-expire the current session (useful for testing)."""
    global _session_id, _last_active
    _session_id = None
    _last_active = 0.0
