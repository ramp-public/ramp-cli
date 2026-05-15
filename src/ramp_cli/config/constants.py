"""URLs, client IDs, scopes, and version constants."""

from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ramp_cli.config.environment_types import (
    EnvironmentAuthRequirement,
    EnvironmentDefinition,
)

ENV_SANDBOX = "sandbox"
ENV_PRODUCTION = "production"

SANDBOX_BASE_URL = "https://demo-api.ramp.com"
PRODUCTION_BASE_URL = "https://api.ramp.com"

SANDBOX_AUTH_URL = "https://demo.ramp.com/v1/authorize"
PRODUCTION_AUTH_URL = "https://app.ramp.com/v1/authorize"

SANDBOX_TOKEN_URL = "https://demo-api.ramp.com/developer/v1/token/pkce"
PRODUCTION_TOKEN_URL = "https://api.ramp.com/developer/v1/token/pkce"

# Per-environment OAuth client IDs (public client, PKCE flow).
SANDBOX_CLIENT_ID = "ramp_id_Q0xnopBQxMjvXzmA04GkhA9LQqbT3XwYdrHoJRTI"
PRODUCTION_CLIENT_ID = "ramp_id_6pKvd0IR3d8Kuzp82SV6YgpVCZOlz68Px6s3wVsr"

# Bootstrap tokens that can only used for creating applications.
SANDBOX_APPLICATION_SIGNUP_TOKEN = (
    "ramp_business_tok_29qMQpAGD1ZlLRJSYB9t6mOLEZZww1Lbgp2pJAIw0G"
)
PRODUCTION_APPLICATION_SIGNUP_TOKEN = (
    "ramp_business_tok_LzWMhIyksTncPyD951JLTlkbe906cbEso9VwtvaXig"
)

PREFERRED_CALLBACK_PORT = 19817


PUBLIC_ENVIRONMENTS: dict[str, EnvironmentDefinition] = {
    ENV_SANDBOX: EnvironmentDefinition(
        name=ENV_SANDBOX,
        base_url=SANDBOX_BASE_URL,
        auth_url=SANDBOX_AUTH_URL,
        token_url=SANDBOX_TOKEN_URL,
        client_id=SANDBOX_CLIENT_ID,
        application_signup_token=SANDBOX_APPLICATION_SIGNUP_TOKEN,
        description="Operate in demo.ramp.com",
        aliases=("demo",),
    ),
    ENV_PRODUCTION: EnvironmentDefinition(
        name=ENV_PRODUCTION,
        base_url=PRODUCTION_BASE_URL,
        auth_url=PRODUCTION_AUTH_URL,
        token_url=PRODUCTION_TOKEN_URL,
        client_id=PRODUCTION_CLIENT_ID,
        application_signup_token=PRODUCTION_APPLICATION_SIGNUP_TOKEN,
        description="Operate in app.ramp.com",
        aliases=("prod",),
    ),
}


try:
    from ramp_cli.config.local_environments import build_local_environments
except ModuleNotFoundError as e:
    if e.name != "ramp_cli.config.local_environments":
        raise
    LOCAL_ENVIRONMENTS = ()
else:
    LOCAL_ENVIRONMENTS = build_local_environments(
        client_id=SANDBOX_CLIENT_ID,
        application_signup_token=SANDBOX_APPLICATION_SIGNUP_TOKEN,
    )


ENVIRONMENTS: dict[str, EnvironmentDefinition] = {
    **PUBLIC_ENVIRONMENTS,
    **{env.name: env for env in LOCAL_ENVIRONMENTS},
}


def environment_names() -> tuple[str, ...]:
    return tuple(ENVIRONMENTS)


def environment_choices() -> tuple[str, ...]:
    choices: set[str] = set()
    for env in ENVIRONMENTS.values():
        choices.add(env.name)
        choices.update(env.aliases)
    return tuple(sorted(choices))


def environment_help() -> str:
    return ", ".join(environment_choices())


def environment_usage() -> str:
    return "|".join(sorted(environment_names()))


def default_environment() -> str:
    for definition in ENVIRONMENTS.values():
        if definition.default_when is not None and definition.default_when():
            return definition.name
    return ""


def iter_environments() -> tuple[EnvironmentDefinition, ...]:
    return tuple(ENVIRONMENTS.values())


def env_auth_requirement(env: str) -> EnvironmentAuthRequirement | None:
    return _env_definition(env).auth_requirement


def normalize_env(env: str) -> str:
    lower = env.strip().lower()
    for definition in ENVIRONMENTS.values():
        if lower == definition.name or lower in definition.aliases:
            return definition.name
    raise ValueError(f"invalid environment {env!r}. Choose from: {environment_help()}")


def _env_definition(env: str) -> EnvironmentDefinition:
    return ENVIRONMENTS[normalize_env(env)]


def append_query_params(url: str, params: dict[str, str]) -> str:
    filtered = {key: value for key, value in params.items() if value}
    if not filtered:
        return url

    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend(filtered.items())
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


# Scopes needed by existing DevAPI resource commands. These are merged with
# scopes extracted from the agent-tool spec at login time in oauth.py.
DEVAPI_SCOPES = [
    "business:read",
    "cashbacks:read",
    "departments:read",
    "departments:write",
    "entities:read",
    "item_receipts:read",
    "limits:write",
    "locations:read",
    "locations:write",
    "merchants:read",
    "purchase_orders:read",
    "purchase_orders:write",
    "receipts:read",
    "reimbursements:read",
    "reimbursements:write",
    "spend_programs:read",
    "spend_programs:write",
    "statements:read",
    "transfers:read",
    "users:read",
    "users:write",
    "vendors:read",
    "vendors:write",
]


def base_url(env: str) -> str:
    override = os.environ.get("RAMP_API_URL")
    if override:
        return override.rstrip("/")
    definition = _env_definition(env)
    if definition.base_url_override is not None:
        env_override = definition.base_url_override().strip()
        if env_override:
            return env_override.rstrip("/")
    return definition.base_url


def environment_cache_key(env: str) -> str:
    definition = _env_definition(env)
    if definition.spec_cache_key is not None:
        key = definition.spec_cache_key().strip()
        if key:
            return key
    return normalize_env(env)


def api_url(env: str, path: str, params: dict[str, str] | None = None) -> str:
    return append_query_params(base_url(env) + path, dict(params or {}))


def auth_url(env: str) -> str:
    return _env_definition(env).auth_url


def auth_setup_url(env: str) -> str:
    definition = _env_definition(env)
    if definition.auth_setup_url is None:
        return ""
    return definition.auth_setup_url().strip()


def token_url(env: str) -> str:
    definition = _env_definition(env)
    if definition.base_url_override is not None:
        override = definition.base_url_override().strip()
        if override:
            return f"{override.rstrip('/')}/developer/v1/token/pkce"
    return definition.token_url


def client_id(env: str) -> str:
    return _env_definition(env).client_id


def application_signup_token(env: str) -> str:
    return _env_definition(env).application_signup_token


def agent_tool_spec_url(env: str) -> str:
    return api_url(env, "/v1/public/agent-tools/spec/")


def agent_tool_spec_hash_url(env: str) -> str:
    return api_url(env, "/v1/public/agent-tools/spec/hash")
