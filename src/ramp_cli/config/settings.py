"""TOML config load/save and environment resolution."""

from __future__ import annotations

import os
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

from .constants import (
    ENV_PRODUCTION,
    ENV_SANDBOX,
    default_environment,
    environment_names,
    normalize_env,
)


@dataclass
class EnvConfig:
    access_token: str = ""
    refresh_token: str = ""
    access_token_issued_at: int = 0
    access_token_expires_in: int = 0
    refresh_token_issued_at: int = 0
    refresh_token_expires_in: int = 0
    granted_scopes: str = ""
    # Bound agent key (UUID) when the agent-key flow is enabled.
    # Surfaced in `ramp auth status` so users can see which key is in use.
    agent_key_uuid: str = ""


@dataclass
class Config:
    environment: str = ""
    format: str = ""
    scopes: str = ""
    first_login_at: int = 0
    tools_used: str = ""  # space-separated category names
    sandbox: EnvConfig = field(default_factory=EnvConfig)
    production: EnvConfig = field(default_factory=EnvConfig)
    extra_envs: dict[str, EnvConfig] = field(default_factory=dict)


def config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "ramp"
    return Path.home() / ".config" / "ramp"


def config_path() -> Path:
    return config_dir() / "config.toml"


def load() -> Config:
    path = config_path()
    cfg = Config()
    if not path.exists():
        return cfg

    data = path.read_bytes()
    raw = tomllib.loads(data.decode())

    cfg.environment = raw.get("environment", "")
    cfg.format = raw.get("format", "")
    cfg.scopes = raw.get("scopes", "")
    cfg.first_login_at = raw.get("first_login_at", 0)
    cfg.tools_used = raw.get("tools_used", "")
    for env_name in _env_section_names():
        section = raw.get(env_name, {})
        ec = EnvConfig(
            access_token=section.get("access_token", ""),
            refresh_token=section.get("refresh_token", ""),
            access_token_issued_at=section.get("access_token_issued_at", 0),
            access_token_expires_in=section.get("access_token_expires_in", 0),
            refresh_token_issued_at=section.get("refresh_token_issued_at", 0),
            refresh_token_expires_in=section.get("refresh_token_expires_in", 0),
            granted_scopes=section.get("granted_scopes", ""),
            agent_key_uuid=section.get("agent_key_uuid", ""),
        )
        set_env_config(cfg, env_name, ec)

    # Warn on loose permissions
    try:
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            print(
                f"WARNING: config file {path} has permissions {mode:04o} (should be 0600)\n"
                f"  Fix: chmod 600 {path}",
                file=sys.stderr,
            )
    except OSError:
        pass

    return cfg


def save(cfg: Config) -> None:
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)

    raw: dict = {}
    if cfg.environment:
        raw["environment"] = cfg.environment
    if cfg.format:
        raw["format"] = cfg.format
    if cfg.scopes:
        raw["scopes"] = cfg.scopes
    if cfg.first_login_at:
        raw["first_login_at"] = cfg.first_login_at
    if cfg.tools_used:
        raw["tools_used"] = cfg.tools_used
    for env_name in _env_section_names():
        ec = get_env_config(cfg, env_name)
        section: dict = {}
        if ec.access_token:
            section["access_token"] = ec.access_token
        if ec.refresh_token:
            section["refresh_token"] = ec.refresh_token
        if ec.access_token_issued_at:
            section["access_token_issued_at"] = ec.access_token_issued_at
        if ec.access_token_expires_in:
            section["access_token_expires_in"] = ec.access_token_expires_in
        if ec.refresh_token_issued_at:
            section["refresh_token_issued_at"] = ec.refresh_token_issued_at
        if ec.refresh_token_expires_in:
            section["refresh_token_expires_in"] = ec.refresh_token_expires_in
        if ec.granted_scopes:
            section["granted_scopes"] = ec.granted_scopes
        if ec.agent_key_uuid:
            section["agent_key_uuid"] = ec.agent_key_uuid
        if section:
            raw[env_name] = section

    path = config_path()
    fd, tmp_name = tempfile.mkstemp(prefix=".config.", suffix=".toml", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            tomli_w.dump(raw, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def resolve_environment(flag_value: str = "") -> str:
    if flag_value:
        return normalize_env(flag_value)
    env_var = os.environ.get("RAMP_ENVIRONMENT", "")
    if env_var:
        return normalize_env(env_var)
    env_default = default_environment()
    if env_default:
        return normalize_env(env_default)
    cfg = load()
    if cfg.environment:
        return normalize_env(cfg.environment)
    return ENV_PRODUCTION


def configured_scopes() -> str:
    return load().scopes


def _env_section_names() -> tuple[str, ...]:
    return tuple(sorted(environment_names()))


def get_env_config(cfg: Config, env: str) -> EnvConfig:
    env = normalize_env(env)
    if env == ENV_SANDBOX:
        return cfg.sandbox
    if env == ENV_PRODUCTION:
        return cfg.production
    return cfg.extra_envs.get(env, EnvConfig())


def ensure_env_config(cfg: Config, env: str) -> EnvConfig:
    env = normalize_env(env)
    if env == ENV_SANDBOX:
        return cfg.sandbox
    if env == ENV_PRODUCTION:
        return cfg.production
    return cfg.extra_envs.setdefault(env, EnvConfig())


def set_env_config(cfg: Config, env: str, env_config: EnvConfig) -> None:
    env = normalize_env(env)
    if env == ENV_SANDBOX:
        cfg.sandbox = env_config
    elif env == ENV_PRODUCTION:
        cfg.production = env_config
    else:
        cfg.extra_envs[env] = env_config
