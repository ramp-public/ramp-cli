"""Environment-specific authentication helpers."""

from __future__ import annotations

from ramp_cli.config.constants import env_auth_requirement, normalize_env


def extra_auth_headers(env: str) -> dict[str, str]:
    requirement = env_auth_requirement(env)
    if requirement is None:
        return {}
    return requirement.headers()


def missing_required_environment_auth(env: str) -> bool:
    requirement = env_auth_requirement(env)
    return requirement is not None and not requirement.is_satisfied()


def environment_auth_required_message(env: str) -> str:
    env = normalize_env(env)
    requirement = env_auth_requirement(env)
    if requirement is None:
        return f"No extra authentication is required for {env}."
    return requirement.message(env)
