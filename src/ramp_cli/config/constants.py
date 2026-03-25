"""URLs, client IDs, scopes, and version constants."""

from __future__ import annotations

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
    if env == ENV_PRODUCTION:
        return PRODUCTION_BASE_URL
    return SANDBOX_BASE_URL


def auth_url(env: str) -> str:
    if env == ENV_PRODUCTION:
        return PRODUCTION_AUTH_URL
    return SANDBOX_AUTH_URL


def token_url(env: str) -> str:
    if env == ENV_PRODUCTION:
        return PRODUCTION_TOKEN_URL
    return SANDBOX_TOKEN_URL


def client_id(env: str) -> str:
    if env == ENV_PRODUCTION:
        return PRODUCTION_CLIENT_ID
    return SANDBOX_CLIENT_ID


def application_signup_token(env: str) -> str:
    if env == ENV_PRODUCTION:
        return PRODUCTION_APPLICATION_SIGNUP_TOKEN
    return SANDBOX_APPLICATION_SIGNUP_TOKEN


def agent_tool_spec_url(env: str) -> str:
    return base_url(env) + "/v1/public/agent-tools/spec/"


def agent_tool_spec_hash_url(env: str) -> str:
    return base_url(env) + "/v1/public/agent-tools/spec/hash"
