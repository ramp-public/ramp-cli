"""Tests for payment-vault proxy configuration."""

from __future__ import annotations

import click
import pytest

from ramp_cli.client.vault import (
    VAULT_PROXY_ENABLED_ENV,
    VAULT_PROXY_HEADERS_ENV,
    VAULT_PROXY_URL_ENV,
    vault_proxy_enabled,
    vault_proxy_headers,
    vault_proxy_target,
    vault_proxy_url,
)

_ON = {VAULT_PROXY_ENABLED_ENV: "1"}


@pytest.mark.parametrize("value", ["", "0", "false", "FALSE", "no", "off"])
def test_vault_proxy_disabled_for_falsey_values(value: str) -> None:
    assert vault_proxy_enabled({VAULT_PROXY_ENABLED_ENV: value}) is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_vault_proxy_enabled_for_truthy_values(value: str) -> None:
    assert vault_proxy_enabled({VAULT_PROXY_ENABLED_ENV: value}) is True


def test_vault_proxy_url_requires_https() -> None:
    with pytest.raises(click.BadParameter):
        vault_proxy_url({VAULT_PROXY_URL_ENV: "http://proxy.example.com"})


@pytest.mark.parametrize(
    "url",
    [
        "https://user@proxy.example.com",
        "https://proxy.example.com?destination=ramp",
        "https://proxy.example.com#fragment",
    ],
)
def test_vault_proxy_url_rejects_unsafe_components(url: str) -> None:
    with pytest.raises(click.BadParameter):
        vault_proxy_url({VAULT_PROXY_URL_ENV: url})


def test_vault_proxy_target_is_direct_when_disabled() -> None:
    env = {VAULT_PROXY_URL_ENV: "https://proxy.example.com"}
    assert vault_proxy_target("/developer/v1/agent-tools/x", env) is None


def test_vault_proxy_target_requires_url_when_enabled() -> None:
    with pytest.raises(click.UsageError):
        vault_proxy_target("/developer/v1/agent-tools/x", _ON)


def test_vault_proxy_target_appends_api_path() -> None:
    env = {**_ON, VAULT_PROXY_URL_ENV: "https://proxy.example.com/"}
    assert vault_proxy_target("/developer/v1/agent-tools/x", env) == (
        "https://proxy.example.com/developer/v1/agent-tools/x"
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
