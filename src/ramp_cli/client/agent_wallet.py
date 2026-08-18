"""Authenticated client for the Agent Wallet service."""

from __future__ import annotations

import json
import os
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from ramp_cli.auth.agent_wallet import get_api_key
from ramp_cli.client.transport import BearerTokenTransport
from ramp_cli.errors import (
    EXIT_AUTH_REQUIRED,
    ApiError,
    RampCLIError,
)

PAYMENT_PATH = "/developer/v1/agent-wallet/pay"
PAYMENT_HISTORY_PATH = "/developer/v1/agent-wallet/operations"
AGENT_WALLET_API_KEY_ENV_VAR = "RAMP_AGENT_WALLET_API_KEY"
AGENT_WALLET_BASE_URL = "https://wallet.ramp.com"


class AgentWalletClientError(RampCLIError):
    """Agent Wallet routing or response validation failed."""


class AgentWalletApiError(ApiError):
    """API failure enriched with Agent Wallet command context."""

    def __init__(self, error: ApiError) -> None:
        self.status_code = error.status_code
        self.body = error.body
        self.error_code = error.error_code

        detail = error.detail
        if error.status_code == 503 and detail.rstrip(".").casefold() == (
            "authentication service unavailable"
        ):
            detail = (
                "The authentication service is temporarily unavailable. "
                "Try again shortly."
            )

        self.detail = detail
        RampCLIError.__init__(self, f"Agent Wallet request failed: {detail}")


class AgentWalletAuthRequiredError(AgentWalletClientError):
    def __init__(self) -> None:
        super().__init__(
            "Agent Wallet is not configured — run: ramp agent-wallet configure",
            code=EXIT_AUTH_REQUIRED,
        )


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


class AgentWalletClient:
    """Client for public Agent Wallet payments."""

    @property
    def payment_url(self) -> str:
        return f"{agent_wallet_base_url()}{PAYMENT_PATH}"

    def pay(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = self._request_json(
            "POST",
            self.payment_url,
            body=json.dumps(body).encode(),
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
    ) -> Any:
        api_key = os.environ.get(AGENT_WALLET_API_KEY_ENV_VAR, "").strip()
        api_key = api_key or get_api_key()
        if not api_key:
            raise AgentWalletAuthRequiredError()
        try:
            response = BearerTokenTransport(api_key).request(
                method,
                url,
                body=body,
            )
        except ApiError as error:
            if error.status_code in {401, 403}:
                raise AgentWalletAuthRequiredError() from error
            raise
        try:
            payload = json.loads(response)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise AgentWalletClientError(
                "Agent Wallet returned an invalid response"
            ) from error
        return payload
