"""Token persistence — plaintext config file storage with strict permissions."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from ramp_cli.config import profiles, settings

ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 30


@dataclass
class TokenState:
    access_token: str = ""
    refresh_token: str = ""
    access_token_issued_at: int = 0
    access_token_expires_in: int = 0
    refresh_token_issued_at: int = 0
    refresh_token_expires_in: int = 0

    def access_token_is_expired(self, now: int | None = None) -> bool:
        if not self.access_token:
            return True
        if self.access_token_issued_at <= 0 or self.access_token_expires_in <= 0:
            return False
        if now is None:
            now = int(time.time())
        return now >= self.access_token_issued_at + self.access_token_expires_in

    def access_token_is_expiring_soon(self, now: int | None = None) -> bool:
        if (
            not self.access_token
            or self.access_token_issued_at <= 0
            or self.access_token_expires_in <= 0
        ):
            return False
        if now is None:
            now = int(time.time())
        refresh_at = self.access_token_issued_at + max(
            self.access_token_expires_in - ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
            0,
        )
        return now >= refresh_at

    def refresh_token_is_expired(self, now: int | None = None) -> bool:
        if not self.refresh_token:
            return True
        if self.refresh_token_issued_at <= 0 or self.refresh_token_expires_in <= 0:
            return False
        if now is None:
            now = int(time.time())
        return now >= self.refresh_token_issued_at + self.refresh_token_expires_in

    def is_authenticated(self, now: int | None = None) -> bool:
        if self.access_token and not self.access_token_is_expired(now):
            return True
        if self.refresh_token and not self.refresh_token_is_expired(now):
            return True
        return False


# --- Public API ---


def get_tokens(env: str, *, profile: str | None = None) -> tuple[str, str]:
    """Return (access_token, refresh_token) for the environment."""
    state = get_token_state(env, profile=profile)
    return state.access_token, state.refresh_token


def get_token_state(env: str, *, profile: str | None = None) -> TokenState:
    if profile is None:
        cfg = settings.load()
        ec = settings.get_env_config(cfg, env)
    else:
        ec = profiles.get_env_config(profile, env)
    return TokenState(
        access_token=ec.access_token,
        refresh_token=ec.refresh_token,
        access_token_issued_at=ec.access_token_issued_at,
        access_token_expires_in=ec.access_token_expires_in,
        refresh_token_issued_at=ec.refresh_token_issued_at,
        refresh_token_expires_in=ec.refresh_token_expires_in,
    )


def save_tokens(
    env: str,
    access_token: str,
    refresh_token: str,
    access_token_expires_in: int = 0,
    refresh_token_expires_in: int = 0,
    issued_at: int | None = None,
    granted_scopes: str | None = None,
    agent_key_uuid: str | None = None,
    clear_granted_scopes: bool = False,
    profile: str | None = None,
    expected_refresh_token: str | None = None,
) -> bool:
    state = _build_token_state(
        access_token,
        refresh_token,
        access_token_expires_in,
        refresh_token_expires_in,
        issued_at,
    )

    def update(ec: settings.EnvConfig) -> None:
        ec.access_token = state.access_token
        ec.refresh_token = state.refresh_token
        ec.access_token_issued_at = state.access_token_issued_at
        ec.access_token_expires_in = state.access_token_expires_in
        ec.refresh_token_issued_at = state.refresh_token_issued_at
        ec.refresh_token_expires_in = state.refresh_token_expires_in
        # Token refresh responses often omit scope, so preserve grants by default.
        # Credential replacement callers explicitly clear them when scope is unknown.
        if clear_granted_scopes:
            ec.granted_scopes = ""
        elif granted_scopes:
            ec.granted_scopes = granted_scopes
        # Refresh callers omit the UUID to preserve it. Credential replacement
        # callers pass a value explicitly, including "" to clear it.
        if agent_key_uuid is not None:
            ec.agent_key_uuid = agent_key_uuid

    if profile is not None:
        return profiles.update_env_config(
            profile,
            env,
            update,
            expected_refresh_token=expected_refresh_token,
        )
    cfg = settings.load()
    update(settings.ensure_env_config(cfg, env))
    settings.save(cfg)
    return True


def clear_tokens(
    env: str,
    *,
    profile: str | None = None,
    expected_refresh_token: str | None = None,
) -> bool:
    def clear(ec: settings.EnvConfig) -> None:
        ec.access_token = ""
        ec.refresh_token = ""
        ec.access_token_issued_at = 0
        ec.access_token_expires_in = 0
        ec.refresh_token_issued_at = 0
        ec.refresh_token_expires_in = 0
        ec.granted_scopes = ""
        ec.agent_key_uuid = ""

    if profile is not None:
        return profiles.update_env_config(
            profile,
            env,
            clear,
            expected_refresh_token=expected_refresh_token,
        )
    cfg = settings.load()
    clear(settings.ensure_env_config(cfg, env))
    settings.save(cfg)
    return True


def get_agent_key_uuid(env: str, *, profile: str | None = None) -> str:
    if profile is None:
        cfg = settings.load()
        ec = settings.get_env_config(cfg, env)
    else:
        ec = profiles.get_env_config(profile, env)
    return ec.agent_key_uuid


def get_granted_scopes(env: str, *, profile: str | None = None) -> set[str]:
    """Return the set of OAuth scopes granted to the current token."""
    if profile is None:
        cfg = settings.load()
        ec = settings.get_env_config(cfg, env)
    else:
        ec = profiles.get_env_config(profile, env)
    if not ec.granted_scopes:
        return set()
    return set(ec.granted_scopes.split())


def get_known_granted_scopes(
    env: str, *, profile: str | None = None
) -> set[str] | None:
    """Return grants only when they are known to match the active credential."""
    if os.environ.get("RAMP_ACCESS_TOKEN"):
        return None
    scopes = get_granted_scopes(env, profile=profile)
    return scopes or None


def has_tokens(env: str, *, profile: str | None = None) -> bool:
    access, refresh = get_tokens(env, profile=profile)
    return bool(access or refresh)


def is_authenticated(
    env: str, now: int | None = None, *, profile: str | None = None
) -> bool:
    return get_token_state(env, profile=profile).is_authenticated(now)


# --- Helpers ---


def _build_token_state(
    access_token: str,
    refresh_token: str,
    access_token_expires_in: int,
    refresh_token_expires_in: int,
    issued_at: int | None,
) -> TokenState:
    if issued_at is None and (
        access_token_expires_in > 0 or refresh_token_expires_in > 0
    ):
        issued_at = int(time.time())
    access_token_issued_at = (
        issued_at if access_token and access_token_expires_in > 0 else 0
    )
    refresh_token_issued_at = (
        issued_at if refresh_token and refresh_token_expires_in > 0 else 0
    )
    return TokenState(
        access_token=access_token,
        refresh_token=refresh_token,
        access_token_issued_at=access_token_issued_at or 0,
        access_token_expires_in=access_token_expires_in,
        refresh_token_issued_at=refresh_token_issued_at or 0,
        refresh_token_expires_in=refresh_token_expires_in,
    )
