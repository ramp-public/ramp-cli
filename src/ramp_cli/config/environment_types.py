"""Environment registry types."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class EnvironmentAuthRequirement:
    headers: Callable[[], dict[str, str]]
    is_satisfied: Callable[[], bool]
    message: Callable[[str], str]


@dataclass(frozen=True)
class EnvironmentDefinition:
    name: str
    base_url: str
    auth_url: str
    token_url: str
    client_id: str
    application_signup_token: str
    description: str
    aliases: tuple[str, ...] = ()
    default_when: Callable[[], bool] | None = None
    base_url_override: Callable[[], str] | None = None
    auth_setup_url: Callable[[], str] | None = None
    spec_cache_key: Callable[[], str] | None = None
    auth_requirement: EnvironmentAuthRequirement | None = None
