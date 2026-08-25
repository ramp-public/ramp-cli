"""Silent token refresh helper."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from ramp_cli.auth import store
from ramp_cli.auth.constants import INVALID_GRANT
from ramp_cli.auth.oauth import OAuthTokenError, refresh_tokens
from ramp_cli.config import settings
from ramp_cli.errors import RefreshFailedError

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX fallback
    msvcrt = None


def try_refresh(env: str, *, profile: str | None = None) -> str | None:
    """Attempt to silently refresh tokens. Returns new access token or None."""
    _, refresh_token = _get_tokens(env, profile)
    if not refresh_token:
        return None
    current_refresh_token = refresh_token
    try:
        lock = _refresh_lock(env, profile) if profile else _refresh_lock(env)
        with lock:
            access_token, current_refresh_token = _get_tokens(env, profile)
            if not current_refresh_token:
                return None

            # Another CLI process may have already rotated this token family.
            if current_refresh_token != refresh_token and access_token:
                return access_token

            token_resp = refresh_tokens(env, current_refresh_token)
            if not token_resp.refresh_token:
                if _clear_tokens(env, profile, current_refresh_token):
                    return None
                return _get_tokens(env, profile)[0] or None

            if profile:
                saved = store.save_tokens(
                    env,
                    token_resp.access_token,
                    token_resp.refresh_token,
                    access_token_expires_in=token_resp.expires_in,
                    refresh_token_expires_in=token_resp.refresh_token_expires_in,
                    agent_key_uuid=token_resp.agent_key_uuid,
                    profile=profile,
                    expected_refresh_token=current_refresh_token,
                )
                if not saved:
                    return _get_tokens(env, profile)[0] or None
            else:
                store.save_tokens(
                    env,
                    token_resp.access_token,
                    token_resp.refresh_token,
                    access_token_expires_in=token_resp.expires_in,
                    refresh_token_expires_in=token_resp.refresh_token_expires_in,
                    agent_key_uuid=token_resp.agent_key_uuid,
                )
            return token_resp.access_token
    except OAuthTokenError as exc:
        if exc.error == INVALID_GRANT:
            if _clear_tokens(env, profile, current_refresh_token):
                return None
            return _get_tokens(env, profile)[0] or None
        raise RefreshFailedError(f"Token refresh failed: {exc}") from exc
    except Exception as exc:
        raise RefreshFailedError(f"Token refresh failed: {exc}") from exc


@contextmanager
def _refresh_lock(env: str, profile: str | None = None):
    lock_path = _refresh_lock_path(env, profile)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+b") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:
            _lock_with_msvcrt(lock_file)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                _unlock_with_msvcrt(lock_file)


def _refresh_lock_path(env: str, profile: str | None = None) -> Path:
    if profile is None:
        return settings.config_dir() / f".{env}.refresh.lock"
    return settings.config_dir() / f".{env}.{profile}.refresh.lock"


def _get_tokens(env: str, profile: str | None) -> tuple[str, str]:
    if profile:
        return store.get_tokens(env, profile=profile)
    return store.get_tokens(env)


def _clear_tokens(env: str, profile: str | None, expected_refresh_token: str) -> bool:
    if profile:
        return store.clear_tokens(
            env,
            profile=profile,
            expected_refresh_token=expected_refresh_token,
        )
    return store.clear_tokens(env)


def _lock_with_msvcrt(lock_file):
    lock_file.seek(0, 2)
    if lock_file.tell() == 0:
        lock_file.write(b"\0")
        lock_file.flush()
    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)


def _unlock_with_msvcrt(lock_file):
    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
