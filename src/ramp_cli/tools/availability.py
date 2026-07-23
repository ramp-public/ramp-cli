"""Effective agent-tool availability from the Developer API.

Core's ``GET /developer/v1/agent-tools/availability`` endpoint collapses each
tool's rollout flag, required OAuth scopes, platform allowlist, and
authorization level into a per-tool answer for the calling token.  The CLI
uses it to *decorate* the spec-derived tool catalog — it never adds, hides,
or blocks tools.  ``available: true`` means the call would be admitted, not
that it is guaranteed to succeed.

Every failure path returns ``None`` (fail-open): unauthenticated, offline,
endpoint gated off, or malformed responses all leave the CLI behaving as if
this module did not exist.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from ramp_cli.auth import store
from ramp_cli.client.api import RampClient
from ramp_cli.tools.parser import ToolDef

logger = logging.getLogger(__name__)

AVAILABILITY_PATH = "/developer/v1/agent-tools/availability"
_AGENT_TOOLS_PREFIX = "/developer/v1/agent-tools/"

# Escape hatch: disables all availability lookups.
KILL_SWITCH_ENV_VAR = "RAMP_NO_TOOL_AVAILABILITY"

_REASON_TEXT: dict[str, str] = {
    "disabled_for_business": "not enabled for your business",
    "platform_not_allowed": "not available to CLI clients",
    "authorization_level_not_allowed": "not available to this token type",
}


@dataclass(frozen=True, slots=True)
class ToolAvailability:
    """Effective availability of one tool for the calling token."""

    available: bool
    unavailable_reasons: tuple[str, ...] = ()
    missing_scopes: tuple[str, ...] = ()

    def describe(self) -> str:
        """Human-readable reason summary, e.g. ``missing scope(s): limits:read``."""
        parts = []
        for reason in self.unavailable_reasons:
            if reason == "missing_scopes":
                scopes = ", ".join(self.missing_scopes) or "unknown"
                parts.append(f"missing scope(s): {scopes}")
            else:
                # Unknown future reasons render as-is for forward compatibility.
                parts.append(_REASON_TEXT.get(reason, reason.replace("_", " ")))
        return "; ".join(parts)


@dataclass(frozen=True, slots=True)
class AvailabilitySnapshot:
    """One availability response, joinable against spec-derived ToolDefs."""

    content_hash: str
    entries: dict[tuple[str, str], ToolAvailability]  # (tool segment, METHOD)

    def lookup(self, tool: ToolDef) -> ToolAvailability | None:
        """Availability for a ToolDef, or None when the server did not report it."""
        if not tool.path.startswith(_AGENT_TOOLS_PREFIX):
            return None
        segment = tool.path.removeprefix(_AGENT_TOOLS_PREFIX)
        return self.entries.get((segment, tool.http_method.upper()))


def fetch_availability(env: str) -> AvailabilitySnapshot | None:
    """Best-effort fetch of effective tool availability for the current token.

    Returns ``None`` when the kill switch is set, no credential is available,
    or the request fails for any reason.  Callers must treat ``None`` as
    "no availability data" and preserve existing behavior.
    """
    if os.environ.get(KILL_SWITCH_ENV_VAR):
        return None
    if not os.environ.get("RAMP_ACCESS_TOKEN") and not store.is_authenticated(env):
        return None
    try:
        payload = json.loads(RampClient(env).get(AVAILABILITY_PATH))
        entries = {
            (item["tool"], item["method"].upper()): ToolAvailability(
                available=bool(item["available"]),
                unavailable_reasons=tuple(item.get("unavailable_reasons") or ()),
                missing_scopes=tuple(item.get("missing_scopes") or ()),
            )
            for item in payload["tools"]
        }
        return AvailabilitySnapshot(
            content_hash=payload.get("content_hash") or "", entries=entries
        )
    except Exception as exc:  # fail-open by design
        logger.debug("agent-tool availability lookup failed: %s", exc)
        return None
