"""Shared sign-in and API-key acquisition for Router configuration."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path
from uuid import uuid4

import click

from ramp_cli.auth.refresh import _refresh_lock
from ramp_cli.client.router_user import RouterUserClient, _atomic_write, _jwt_account
from ramp_cli.commands.router_user import _routing_profiles, create_named_key
from ramp_cli.config.settings import config_dir
from ramp_cli.errors import EXIT_AUTH_REQUIRED, RampCLIError

HARNESSES = ("claude-code", "codex", "opencode", "pi")


def sign_in(
    client: RouterUserClient, *, connectors: bool = False, no_browser: bool = False
) -> str | None:
    if connectors:
        client.login(connectors=True, no_browser=no_browser)
        return None
    connector_session = client.status().get("connectors_authorized") is True
    try:
        # no_input=True blocks implicit browser recovery, not silent rotation.
        return client.access_token(check_session=True)
    except RampCLIError as exc:
        if exc.code != EXIT_AUTH_REQUIRED:
            raise
        if connector_session:
            raise click.ClickException(
                "Your connector sign-in needs renewal. Renew it with 'router login --connectors' "
                "before setup; existing connector bindings were not changed."
            ) from None
    client.login(no_browser=no_browser)
    return client.access_token(check_session=True)


def _read_state(path: Path) -> dict:
    try:
        parent = path.parent.stat()
        if (
            path.parent.is_symlink()
            or parent.st_uid != os.getuid()
            or parent.st_mode & 0o022
        ):
            raise click.ClickException(
                "Router setup key directory must be owned by you and not writable by other users."
            )
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    except OSError:
        raise click.ClickException(
            "Could not securely read the Router setup key."
        ) from None
    with os.fdopen(fd, encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 16384
        ):
            raise click.ClickException(
                "Router setup key must be an owner-private regular file (600)."
            )
        try:
            state = json.load(handle)
        except (ValueError, UnicodeError):
            raise click.ClickException(
                "Could not read the Router setup key; no new key was created."
            ) from None
    if not isinstance(state, dict) or not isinstance(state.get("request_id"), str):
        raise click.ClickException(
            "Invalid Router setup key state; no new key was created."
        )
    return state


def _saved_key(client: RouterUserClient, path: Path) -> tuple[str, bool]:
    state = _read_state(path)
    secret = state.get("secret")
    if secret is not None:
        if (
            not isinstance(secret, str)
            or not secret
            or any(c.isspace() for c in secret)
        ):
            raise click.ClickException(
                "Invalid saved Router setup key; no new key was created."
            )
        created_at = state.get("created_at", 0)
        if not isinstance(created_at, int | float):
            raise click.ClickException("Invalid saved Router setup key timestamp.")
        return secret, time.time() - created_at < 300
    if not state:
        profiles = _routing_profiles(client) or []
        default = next((p for p in profiles if p.get("is_default")), None)
        state = {"request_id": str(uuid4()), "routing_profile": default}
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Durable idempotency identity precedes the write: retry a lost response
        # with the same key request instead of silently creating another key.
        _atomic_write(path, state)
    secret = create_key(client, state["request_id"], state.get("routing_profile"))
    state.update(secret=secret, created_at=time.time())
    _atomic_write(path, state)
    return secret, True


def create_key(
    client: RouterUserClient, request_id: str | None = None, profile: dict | None = None
) -> str:
    if request_id is None:
        profiles = _routing_profiles(client) or []
        profile = next((p for p in profiles if p.get("is_default")), None)
    created = create_named_key(client, "Ramp CLI", profile, request_id or str(uuid4()))
    secret = created.get("secret")
    if not isinstance(secret, str) or not secret or any(c.isspace() for c in secret):
        raise click.ClickException(
            "Router did not return a usable API key. Retry configuration."
        )
    return secret


def acquire_key(
    origin: str, *, profile: str | None, no_browser: bool
) -> tuple[str, bool]:
    client = RouterUserClient(origin, profile=profile, no_input=True)
    try:
        account = _jwt_account(sign_in(client, no_browser=no_browser))
        if account is None:
            raise click.ClickException("Router did not return an identifiable account.")
        client._request_account = account
        scope = hashlib.sha256(
            json.dumps([origin, profile, account]).encode()
        ).hexdigest()
        with _refresh_lock(f"router-setup-{scope}"):
            return _saved_key(client, config_dir() / "router-setup" / f"{scope}.json")
    finally:
        client.close()
