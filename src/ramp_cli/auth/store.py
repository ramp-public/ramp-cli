"""Token persistence — plaintext config file storage with strict permissions."""

from __future__ import annotations

import time
from dataclasses import dataclass

from ramp_cli.config import settings

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


def get_tokens(env: str) -> tuple[str, str]:
    """Return (access_token, refresh_token) for the environment."""
    state = get_token_state(env)
    return state.access_token, state.refresh_token


def get_token_state(env: str) -> TokenState:
    cfg = settings.load()
    ec: settings.EnvConfig = getattr(cfg, env, settings.EnvConfig())
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
) -> None:
    state = _build_token_state(
        access_token,
        refresh_token,
        access_token_expires_in,
        refresh_token_expires_in,
        issued_at,
    )

    cfg = settings.load()
    ec: settings.EnvConfig = getattr(cfg, env)
    ec.access_token = state.access_token
    ec.refresh_token = state.refresh_token
    ec.access_token_issued_at = state.access_token_issued_at
    ec.access_token_expires_in = state.access_token_expires_in
    ec.refresh_token_issued_at = state.refresh_token_issued_at
    ec.refresh_token_expires_in = state.refresh_token_expires_in
    settings.save(cfg)


def clear_tokens(env: str) -> None:
    cfg = settings.load()
    ec: settings.EnvConfig = getattr(cfg, env)
    ec.access_token = ""
    ec.refresh_token = ""
    ec.access_token_issued_at = 0
    ec.access_token_expires_in = 0
    ec.refresh_token_issued_at = 0
    ec.refresh_token_expires_in = 0
    settings.save(cfg)


def has_tokens(env: str) -> bool:
    access, refresh = get_tokens(env)
    return bool(access or refresh)


def is_authenticated(env: str, now: int | None = None) -> bool:
    return get_token_state(env).is_authenticated(now)


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
