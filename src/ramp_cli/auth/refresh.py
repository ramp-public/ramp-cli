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


def try_refresh(env: str) -> str | None:
    """Attempt to silently refresh tokens. Returns new access token or None."""
    _, refresh_token = store.get_tokens(env)
    if not refresh_token:
        return None
    try:
        with _refresh_lock(env):
            access_token, current_refresh_token = store.get_tokens(env)
            if not current_refresh_token:
                return None

            # Another CLI process may have already rotated this token family.
            if current_refresh_token != refresh_token and access_token:
                return access_token

            token_resp = refresh_tokens(env, current_refresh_token)
            if not token_resp.refresh_token:
                store.clear_tokens(env)
                return None

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
            store.clear_tokens(env)
            return None
        raise RefreshFailedError(f"Token refresh failed: {exc}") from exc
    except Exception as exc:
        raise RefreshFailedError(f"Token refresh failed: {exc}") from exc


@contextmanager
def _refresh_lock(env: str):
    lock_path = _refresh_lock_path(env)
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


def _refresh_lock_path(env: str) -> Path:
    return settings.config_dir() / f".{env}.refresh.lock"


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
