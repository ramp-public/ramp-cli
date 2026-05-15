"""Fetch and cache the agent-tool OpenAPI spec from the public endpoint."""

from __future__ import annotations

import json
import logging
import time

import httpx

from ramp_cli.auth.environment import extra_auth_headers
from ramp_cli.config.constants import (
    agent_tool_spec_hash_url,
    agent_tool_spec_url,
    environment_cache_key,
)
from ramp_cli.specs import local_agent_tool_hash, local_agent_tool_spec
from ramp_cli.tools.registry import reload

log = logging.getLogger(__name__)

_COOLDOWN_SECONDS = 3600  # 1 hour


def fetch_spec(env: str, *, known_hash: str | None = None) -> int:
    """Fetch the full spec and cache it with its content hash.

    If *known_hash* is provided (e.g. from a prior ``/hash`` check),
    it is stored directly and no extra hash request is made.
    """
    cache_key = environment_cache_key(env)
    spec_path = local_agent_tool_spec(cache_key)
    hash_path = local_agent_tool_hash(cache_key)
    headers = extra_auth_headers(env)

    with httpx.Client(timeout=30.0) as client:
        resp = client.get(agent_tool_spec_url(env), headers=headers)
        resp.raise_for_status()
        spec = resp.json()

        if known_hash is None:
            hash_resp = client.get(agent_tool_spec_hash_url(env), headers=headers)
            hash_resp.raise_for_status()
            known_hash = hash_resp.json().get("content_hash", "")

    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec, indent=2) + "\n")
    hash_path.write_text(known_hash)

    return len([p for p in spec.get("paths", {}) if "agent-tools" in p])


def maybe_sync(env: str, *, force: bool = False) -> None:
    """Check the hash endpoint and refresh if the spec changed.

    The normal path uses a 1h cooldown. Callers that must validate user input
    against the latest schema can force a hash check before proceeding.
    """
    hash_path = local_agent_tool_hash(environment_cache_key(env))

    try:
        if not force and hash_path.exists():
            age = time.time() - hash_path.stat().st_mtime
            if age < _COOLDOWN_SECONDS:
                return
    except Exception:
        log.debug("spec sync: failed to check cooldown", exc_info=True)
        return

    try:
        headers = extra_auth_headers(env)
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(agent_tool_spec_hash_url(env), headers=headers)
            resp.raise_for_status()
            remote_hash = resp.json().get("content_hash", "")
    except Exception:
        log.debug("spec sync: hash check failed", exc_info=True)
        return

    try:
        local_hash = hash_path.read_text().strip() if hash_path.exists() else ""
    except Exception:
        local_hash = ""

    if remote_hash and remote_hash == local_hash:
        try:
            hash_path.touch()
        except Exception:
            log.debug("spec sync: failed to touch hash file", exc_info=True)
        return

    try:
        fetch_spec(env, known_hash=remote_hash or None)
        reload(env)
    except Exception:
        log.debug("spec sync: fetch failed", exc_info=True)
