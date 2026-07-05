"""Agent/client harness detection helpers."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

_CLIENT_OVERRIDE_ENV = "RAMP_CLIENT_NAME"

# Exact env-var sentinels for harnesses whose vendors commit to setting them.
_HARNESS_SENTINELS: tuple[tuple[str, str], ...] = (
    ("CLAUDECODE", "claude-code"),
    ("OPENCODE", "opencode"),
    ("CODEX_SANDBOX", "codex"),
)

_SAFE_COMMENT_RE = re.compile(r"[^A-Za-z0-9._/+:-]+")
_MAX_COMMENT_LEN = 64


def infer_client_name(environ: Mapping[str, str] | None = None) -> str | None:
    """Return a sanitized client-name string when we can identify the host
    harness, otherwise None.

    Detection is intentionally narrow: an explicit RAMP_CLIENT_NAME override or
    one of a small list of exact env-var sentinels that the harness vendor
    commits to setting. TERM_PROGRAM, SHELL, and env-var prefix matching are
    deliberately excluded — they produced false positives for plain human CLI
    use and for unrelated tools that share a vendor namespace.
    """
    env = environ if environ is not None else os.environ

    override = _sanitize_comment(env.get(_CLIENT_OVERRIDE_ENV))
    if override:
        return override

    return infer_harness_name(env)


def infer_harness_name(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the host harness name when an exact sentinel is present."""
    env = environ if environ is not None else os.environ
    return next((name for key, name in _HARNESS_SENTINELS if env.get(key)), None)


def _sanitize_comment(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = _SAFE_COMMENT_RE.sub("-", value.strip())[:_MAX_COMMENT_LEN].strip(" -")
    return cleaned or None
