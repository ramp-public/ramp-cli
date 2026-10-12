"""Admin-configured defaults for coding-agent harnesses.

A Router workspace admin can choose the model each harness should start new
sessions with. Router resolves that choice against the workspace's model
policy and reports it to API-key callers as ``effective_model`` in
``GET {origin}/self-service/coding-agent-settings``: null when nothing is
set, the model is no longer allowed, or the key is personal; the harness is
missing entirely when Router does not support a default for it.

:func:`admin_default_model` is the read behind the Codex setup. Callers only
depend on its contract:

* It returns the model Router names as the default for a harness, or
  ``None`` when there is none. The value is Router's id; mapping it onto a
  harness's own naming is the caller's job.
* It never raises and never blocks for long: a Router that is down, slow,
  older than the endpoint, or lists only other harnesses all read as "no
  default". The admin default is a bonus on top of a working setup.
* It is cheap to call repeatedly: one process asks Router at most once per
  key and origin within :data:`CACHE_TTL_SECONDS`, so a configure that also
  reads the same settings for its Switchyard decision shares the response.

Only ``configure`` and ``refresh`` call it. The Codex SessionStart and Stop
hooks apply the value those runs cached in the setup receipt and stay off the
network.
"""

from __future__ import annotations

import time

import httpx

from ramp_cli import __version__
from ramp_cli.commands.claude_code import RAMP_CLI_VERSION_HEADER

# Reused for every read within this window, then refetched. Matches the
# OpenCode and Pi Router plugins, which apply the same setting per session.
CACHE_TTL_SECONDS = 60.0
DEFAULT_TIMEOUT_SECONDS = 2.0

# (api_key, origin) -> (fetched at, Router's ``harnesses`` object or None).
# Failures are cached too: within one run the Switchyard read and the admin
# default read must agree, and neither should wait on a Router that just
# timed out.
_cache: dict[tuple[str, str], tuple[float, dict | None]] = {}


def clear_cache() -> None:
    """Forget cached settings so the next read asks Router again.

    Called at the start of each ``configure`` and ``refresh`` so a command
    the user runs sees Router's current value; within the command, every
    harness and both readers share the one response.
    """
    _cache.clear()


def coding_agent_settings(
    api_key: str, origin: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> dict | None:
    """This key's per-harness coding-agent settings, or None when unavailable.

    ``origin`` is the control-plane host a setup already sends its
    session-usage calls to, so the saved key goes nowhere new. Fail-open:
    older Routers (404), revoked business access, network errors, and a
    malformed body all read as None. Callers must keep "Router said nothing"
    apart from "Router said no", since only the latter may move a selection.
    """
    if not api_key:
        return None
    origin = origin.rstrip("/")
    cache_key = (api_key, origin)
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached is not None and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    harnesses = _fetch_coding_agent_settings(api_key, origin, timeout)
    _cache[cache_key] = (now, harnesses)
    return harnesses


def _fetch_coding_agent_settings(
    api_key: str, origin: str, timeout: float
) -> dict | None:
    url = f"{origin}/self-service/coding-agent-settings"
    try:
        response = httpx.get(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                RAMP_CLI_VERSION_HEADER: __version__,
                "User-Agent": f"ramp-cli/{__version__}",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        harnesses = response.json()["harnesses"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
    return harnesses if isinstance(harnesses, dict) else None


def effective_model(harnesses: dict | None, client: str) -> str | None:
    """One harness's ``effective_model``, parsed permissively.

    A harness Router does not list, or lists without a usable model, has no
    default; so does a body of an unexpected shape.
    """
    entry = harnesses.get(client) if isinstance(harnesses, dict) else None
    if not isinstance(entry, dict):
        return None
    model = entry.get("effective_model")
    return model.strip() if isinstance(model, str) and model.strip() else None


def admin_default_model(client: str, api_key: str, origin: str) -> str | None:
    """Return the admin-configured default model id for ``client``.

    ``api_key`` and ``origin`` identify the harness's Router connection: the
    key the setup stores and the origin its session-usage calls go to.
    Returns ``None`` when no default is configured for that harness, or when
    Router could not be asked.
    """
    return effective_model(coding_agent_settings(api_key, origin), client)
