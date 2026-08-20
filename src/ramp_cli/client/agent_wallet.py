"""Authenticated client for the Agent Wallet service."""

from __future__ import annotations

import json
import os
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from ramp_cli.client.transport import AuthenticatedRampTransport
from ramp_cli.errors import (
    ApiError,
    RampCLIError,
)

PAYMENT_PATH = "/developer/v1/agent-wallet/pay"
PAYMENT_HISTORY_PATH = "/developer/v1/agent-wallet/operations"
AGENT_WALLET_BASE_URL = "https://wallet.ramp.com"
AGENT_WALLET_PAYMENT_BASE_URL = "https://vault-wallet.ramp.com"


def _is_authentication_service_unavailable(error: ApiError) -> bool:
    return error.status_code == 503 and error.detail.rstrip(".").casefold() == (
        "authentication service unavailable"
    )


class AgentWalletClientError(RampCLIError):
    """Agent Wallet routing or response validation failed."""


class AgentWalletApiError(ApiError):
    """API failure enriched with Agent Wallet command context."""

    def __init__(self, error: ApiError) -> None:
        self.status_code = error.status_code
        self.body = error.body
        self.error_code = error.error_code

        detail = error.detail
        if _is_authentication_service_unavailable(error):
            detail = (
                "The authentication service is temporarily unavailable. "
                "Try again shortly."
            )

        self.detail = detail
        RampCLIError.__init__(self, f"Agent Wallet request failed: {detail}")


def agent_wallet_base_url() -> str:
    """Resolve the Agent Wallet service origin."""
    override = os.environ.get("RAMP_AGENT_WALLET_API_URL", "").strip()
    if override:
        parts = urlsplit(override)
        hostname = parts.hostname
        is_loopback = hostname == "localhost"
        if hostname and not is_loopback:
            try:
                is_loopback = ip_address(hostname).is_loopback
            except ValueError:
                pass
        if not hostname or (
            parts.scheme != "https" and not (parts.scheme == "http" and is_loopback)
        ):
            raise AgentWalletClientError(
                "RAMP_AGENT_WALLET_API_URL must use HTTPS, except for loopback HTTP."
            )
        return override.rstrip("/")
    return AGENT_WALLET_BASE_URL


def agent_wallet_payment_base_url() -> str:
    """Resolve the vault-proxied endpoint used for Agent Wallet payments."""
    service_base_url = agent_wallet_base_url()
    if service_base_url == AGENT_WALLET_BASE_URL:
        return AGENT_WALLET_PAYMENT_BASE_URL
    return service_base_url


class AgentWalletClient:
    """Client for public Agent Wallet payments."""

    def __init__(self, env: str, access_token: str | None = None) -> None:
        self.env = env
        self._transport = AuthenticatedRampTransport(env, access_token)

    @property
    def payment_url(self) -> str:
        return f"{agent_wallet_payment_base_url()}{PAYMENT_PATH}"

    def pay(self, body: dict[str, Any]) -> dict[str, Any]:
        payment_base_url = agent_wallet_payment_base_url()
        payload = self._request_json(
            "POST",
            f"{payment_base_url}{PAYMENT_PATH}",
            body=json.dumps(body).encode(),
            proxied=payment_base_url == AGENT_WALLET_PAYMENT_BASE_URL,
        )
        if not isinstance(payload, dict):
            raise AgentWalletClientError("Agent Wallet returned an invalid response")
        return payload

    def list_payments(self, limit: int) -> list[dict[str, Any]]:
        payments_url = f"{agent_wallet_base_url()}{PAYMENT_HISTORY_PATH}?limit={limit}"
        payload = self._request_json("GET", payments_url)
        if not isinstance(payload, list) or not all(
            isinstance(payment, dict) for payment in payload
        ):
            raise AgentWalletClientError("Agent Wallet returned an invalid response")
        return payload

    def cancel(self, payment_operation_id: UUID) -> dict[str, Any]:
        cancellation_url = (
            f"{agent_wallet_base_url()}{PAYMENT_HISTORY_PATH}/"
            f"{payment_operation_id}/cancel"
        )
        payload = self._request_json("POST", cancellation_url)
        if not isinstance(payload, dict):
            raise AgentWalletClientError("Agent Wallet returned an invalid response")
        return payload

    def _request_json(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        proxied: bool = False,
    ) -> Any:
        response = self._transport.request(method, url, body=body, proxied=proxied)
        try:
            payload = json.loads(response)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise AgentWalletClientError(
                "Agent Wallet returned an invalid response"
            ) from error
        return payload
