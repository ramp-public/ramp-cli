"""Named credential profile persistence."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import tomllib
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

import tomli_w

from ramp_cli.config import settings
from ramp_cli.config.constants import normalize_env

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX fallback
    msvcrt = None


PROFILE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
RESERVED_PROFILE_NAMES = frozenset({"default", "list"})
HUMAN_PROFILE = "human"
AGENT_PROFILE = "agent"
BUILTIN_PROFILES = (HUMAN_PROFILE, AGENT_PROFILE)


def validate_name(name: str) -> str:
    if name in RESERVED_PROFILE_NAMES or not PROFILE_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "profile names must be 1-64 lowercase letters, numbers, hyphens, or "
            "underscores, must start with a letter or number, and cannot be "
            "'default' or 'list'"
        )
    return name


def validate_builtin_name(name: str) -> str:
    if name not in BUILTIN_PROFILES:
        raise ValueError("profile must be 'human' or 'agent'")
    return name


def profiles_path() -> Path:
    return settings.config_dir() / "profiles.toml"


def resolve_profile(flag_value: str = "") -> str | None:
    env_value = os.environ.get("RAMP_PROFILE", "")
    if env_value:
        env_profile = validate_builtin_name(env_value)
        if flag_value and validate_builtin_name(flag_value) != env_profile:
            raise ValueError(
                f"--profile {flag_value!r} conflicts with "
                f"RAMP_PROFILE={env_profile!r}; unset RAMP_PROFILE to override it"
            )
        return env_profile
    if flag_value:
        return validate_builtin_name(flag_value)
    configured = settings.load().profile
    return validate_builtin_name(configured) if configured else None


def activate(name: str) -> None:
    validate_builtin_name(name)
    if not has_profile(name):
        raise ValueError(f"profile {name!r} does not exist")
    config = settings.load()
    config.profile = name
    settings.save(config)


def activate_default() -> None:
    config = settings.load()
    config.profile = ""
    settings.save(config)


def list_profiles() -> list[str]:
    return sorted(_load().keys())


def has_profile(name: str) -> bool:
    validate_name(name)
    return name in _load()


def get_env_config(name: str, env: str) -> settings.EnvConfig:
    validate_name(name)
    section = _load().get(name, {}).get(normalize_env(env), {})
    return _env_config_from_raw(section)


def update_env_config(
    name: str,
    env: str,
    update: Callable[[settings.EnvConfig], None],
    *,
    expected_refresh_token: str | None = None,
) -> bool:
    validate_name(name)
    env = normalize_env(env)
    with _profiles_lock():
        raw = _load()
        env_config = _env_config_from_raw(raw.get(name, {}).get(env, {}))
        if (
            expected_refresh_token is not None
            and env_config.refresh_token != expected_refresh_token
        ):
            return False
        update(env_config)
        section = _env_config_to_raw(env_config)
        if section:
            raw.setdefault(name, {})[env] = section
        else:
            profile = raw.get(name, {})
            profile.pop(env, None)
            if not profile:
                raw.pop(name, None)
        _save(raw)
        return True


def _load() -> dict[str, dict[str, dict]]:
    path = profiles_path()
    if not path.exists():
        return {}
    raw = tomllib.loads(path.read_text())
    profiles = raw.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("profiles.toml must contain a [profiles] table")
    if os.name != "nt":
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            print(
                f"WARNING: profiles file {path} has permissions {mode:04o} "
                f"(should be 0600)\n  Fix: chmod 600 {path}",
                file=sys.stderr,
            )
    return profiles


def _save(profiles: dict[str, dict[str, dict]]) -> None:
    directory = settings.config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".profiles.", suffix=".toml", dir=directory)
    try:
        with os.fdopen(fd, "wb") as file:
            tomli_w.dump({"profiles": profiles}, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_name, profiles_path())
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _env_config_from_raw(section: dict) -> settings.EnvConfig:
    return settings.EnvConfig(
        access_token=section.get("access_token", ""),
        refresh_token=section.get("refresh_token", ""),
        access_token_issued_at=section.get("access_token_issued_at", 0),
        access_token_expires_in=section.get("access_token_expires_in", 0),
        refresh_token_issued_at=section.get("refresh_token_issued_at", 0),
        refresh_token_expires_in=section.get("refresh_token_expires_in", 0),
        granted_scopes=section.get("granted_scopes", ""),
        agent_key_uuid=section.get("agent_key_uuid", ""),
    )


def _env_config_to_raw(env_config: settings.EnvConfig) -> dict:
    return {key: value for key, value in vars(env_config).items() if value}


@contextmanager
def _profiles_lock():
    path = settings.config_dir() / ".profiles.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:
            lock_file.seek(0, 2)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
