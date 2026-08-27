"""Tests for payment-vault proxy configuration."""

from __future__ import annotations

import click
import pytest

from ramp_cli.client.vault import (
    VAULT_PROXY_HEADERS_ENV,
    vault_proxy_headers,
    vault_proxy_target,
)


@pytest.mark.parametrize("environment", ["sandbox", "qa"])
def test_vault_proxy_target_is_direct_outside_production(environment: str) -> None:
    assert (
        vault_proxy_target(
            "/developer/v1/agent-tools/x",
            environment=environment,
        )
        is None
    )


def test_vault_proxy_target_uses_production_proxy() -> None:
    assert vault_proxy_target(
        "/developer/v1/agent-tools/x",
        environment="production",
    ) == ("https://vault-api.ramp.com/developer/v1/agent-tools/x")


def test_vault_proxy_target_requires_an_api_path() -> None:
    with pytest.raises(click.UsageError, match="must start"):
        vault_proxy_target(
            "developer/v1/agent-tools/x",
            environment="production",
        )


def test_vault_proxy_headers_parse_proxy_auth() -> None:
    env = {VAULT_PROXY_HEADERS_ENV: '{"BT-API-KEY": "key"}'}
    assert vault_proxy_headers(env) == {"BT-API-KEY": "key"}


@pytest.mark.parametrize(
    "header",
    [
        "Authorization",
        "authorization",
        "BT-TRACE-ID",
        "x-encrypted-by",
        "Host",
        "Cookie",
        "X-Rampy-Auth",
    ],
)
def test_vault_proxy_headers_cannot_override_protected_headers(header: str) -> None:
    env = {VAULT_PROXY_HEADERS_ENV: f'{{"{header}": "forged"}}'}
    with pytest.raises(click.BadParameter):
        vault_proxy_headers(env)


def test_vault_proxy_headers_require_string_mapping() -> None:
    with pytest.raises(click.BadParameter):
        vault_proxy_headers({VAULT_PROXY_HEADERS_ENV: '{"X-Api-Key": 1}'})
