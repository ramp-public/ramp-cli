"""Fetch and cache OpenAPI documents used for generated CLI tools."""

from __future__ import annotations

import json
import logging
import time

import httpx

from ramp_cli.auth.environment import extra_auth_headers
from ramp_cli.config.constants import environment_cache_key
from ramp_cli.specs import AGENT_TOOL_DEFINITION, GeneratedToolSpec
from ramp_cli.tools.registry import reload

log = logging.getLogger(__name__)

_COOLDOWN_SECONDS = 3600  # 1 hour


def fetch_spec(env: str, *, known_hash: str | None = None) -> int:
    """Fetch the full spec and cache it with its content hash.

    If *known_hash* is provided (e.g. from a prior ``/hash`` check),
    it is stored directly and no extra hash request is made.
    """
    spec = _fetch_spec(env, AGENT_TOOL_DEFINITION, known_hash=known_hash)
    return sum(
        1
        for path_item in spec.get("paths", {}).values()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
        and isinstance(operation, dict)
        and "cli" in operation.get("x-platforms", ["cli"])
    )


def _fetch_spec(
    env: str,
    definition: GeneratedToolSpec,
    *,
    known_hash: str | None,
) -> dict:
    cache_key = environment_cache_key(env)
    spec_path = definition.local_spec(cache_key)
    hash_path = definition.local_hash(cache_key)
    headers = extra_auth_headers(env)

    with httpx.Client(timeout=30.0) as client:
        resp = client.get(definition.spec_url(env), headers=headers)
        resp.raise_for_status()
        spec = resp.json()

        if known_hash is None:
            hash_resp = client.get(definition.hash_url(env), headers=headers)
            hash_resp.raise_for_status()
            known_hash = hash_resp.json().get("content_hash", "")

    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec, indent=2) + "\n")
    hash_path.write_text(known_hash)

    return spec


def maybe_sync(env: str, *, force: bool = False) -> None:
    """Check the hash endpoint and refresh if the spec changed.

    The normal path uses a 1h cooldown. Callers that must validate user input
    against the latest schema can force a hash check before proceeding.
    """
    definition = AGENT_TOOL_DEFINITION
    hash_path = definition.local_hash(environment_cache_key(env))

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
            resp = client.get(definition.hash_url(env), headers=headers)
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
