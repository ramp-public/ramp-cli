"""Tests for direct Agent Wallet commands."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from click.testing import CliRunner

from ramp_cli.auth import agent_wallet as agent_wallet_store
from ramp_cli.auth import refresh as refresh_helper
from ramp_cli.auth import store
from ramp_cli.auth.oauth import TokenResponse
from ramp_cli.client.agent_wallet import (
    AgentWalletAuthRequiredError,
    AgentWalletClient,
    AgentWalletClientError,
)
from ramp_cli.client.transport import BearerTokenTransport
from ramp_cli.errors import ApiError
from ramp_cli.main import cli


def _card_args() -> list[str]:
    return [
        "--merchant-name",
        "Acme",
        "--merchant-url",
        "https://acme.example/checkout",
        "--merchant-country-code",
        "us",
        "--amount-value",
        "1250",
        "--amount-currency",
        "usd",
        "--expires-at",
        (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    ]


def test_configure__stores_and_uses_wallet_key(isolated_config, monkeypatch):
    captured = {}
    store.save_tokens("production", "general-cli-token", "")
    monkeypatch.setenv("RAMP_AGENT_WALLET_CONFIGURE_API_KEY", "wallet-key")

    def fake_request(transport, method, url, body=None, request_headers=None):
        captured["payment_key"] = transport._static_access_token
        return b"{}"

    monkeypatch.setattr(BearerTokenTransport, "request", fake_request)

    result = CliRunner().invoke(
        cli,
        ["--agent", "agent-wallet", "configure"],
    )
    assert result.exit_code == 0, result.output
    assert "wallet-key" not in result.output
    assert "RAMP_AGENT_WALLET_CONFIGURE_API_KEY" not in os.environ
    assert agent_wallet_store.get_api_key() == "wallet-key"
    if os.name != "nt":
        assert agent_wallet_store._api_key_path().stat().st_mode & 0o077 == 0
    assert store.get_tokens("production") == ("general-cli-token", "")

    AgentWalletClient().pay({})

    assert captured == {"payment_key": "wallet-key"}


def test_configure__prompts_for_api_key(isolated_config, monkeypatch):
    result = CliRunner().invoke(
        cli,
        ["--human", "agent-wallet", "configure"],
        input="wallet-key\n",
    )

    assert result.exit_code == 0, result.output
    assert "wallet-key" not in result.output
    assert agent_wallet_store.get_api_key() == "wallet-key"


def test_save_wallet_key__does_not_overwrite_concurrent_production_token_rotation(
    isolated_config,
    monkeypatch,
):
    store.save_tokens("production", "access-old", "refresh-old")
    write_started = threading.Event()
    allow_wallet_write = threading.Event()
    original_atomic_write = agent_wallet_store._atomic_write

    def delayed_wallet_write(path, value):
        write_started.set()
        allow_wallet_write.wait(timeout=2)
        original_atomic_write(path, value)

    def rotate_production_token(env: str, token: str) -> TokenResponse:
        assert env == "production"
        assert token == "refresh-old"
        return TokenResponse(
            access_token="access-new",
            refresh_token="refresh-new",
        )

    monkeypatch.setattr(agent_wallet_store, "_atomic_write", delayed_wallet_write)
    monkeypatch.setattr(
        refresh_helper,
        "refresh_tokens",
        rotate_production_token,
    )
    configure_thread = threading.Thread(
        target=agent_wallet_store.save_api_key,
        args=("wallet-key",),
        daemon=True,
    )
    configure_thread.start()
    assert write_started.wait(timeout=2)

    assert refresh_helper.try_refresh("production") == "access-new"
    allow_wallet_write.set()

    configure_thread.join(timeout=2)
    assert not configure_thread.is_alive()
    assert store.get_tokens("production") == ("access-new", "refresh-new")
    assert agent_wallet_store.get_api_key() == "wallet-key"


def test_list__returns_recent_payments(monkeypatch):
    operation_id = uuid4()
    payments = [
        {
            "payment_operation_id": str(operation_id),
            "method": "vic",
            "amount": "12.50",
            "currency": "USD",
            "decision": "allow",
            "created_at": "2026-08-11T20:00:00Z",
        }
    ]
    captured = {}

    def fake_list_payments(client, limit):
        captured["limit"] = limit
        return payments

    monkeypatch.setattr(AgentWalletClient, "list_payments", fake_list_payments)

    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "agent-wallet",
            "list",
            "--limit",
            "25",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {"limit": 25}
    assert json.loads(result.output) == {
        "schema_version": "1.0",
        "data": payments,
        "pagination": None,
    }


def test_list__requests_server_side_payment_history(monkeypatch):
    monkeypatch.setenv("RAMP_AGENT_WALLET_API_KEY", "wallet-key")
    captured = {}

    def fake_request(transport, method, url, body=None, request_headers=None):
        captured.update(method=method, url=url, body=body)
        return b"[]"

    monkeypatch.setattr(BearerTokenTransport, "request", fake_request)

    assert AgentWalletClient().list_payments(10) == []
    assert captured == {
        "method": "GET",
        "url": (
            "https://wallet.ramp.com/developer/v1/agent-wallet/operations?limit=10"
        ),
        "body": None,
    }


@pytest.mark.parametrize("response", [b"{}", b"[1]"])
def test_list__rejects_invalid_success_response(monkeypatch, response):
    monkeypatch.setenv("RAMP_AGENT_WALLET_API_KEY", "wallet-key")
    monkeypatch.setattr(
        BearerTokenTransport,
        "request",
        lambda *args, **kwargs: response,
    )

    with pytest.raises(AgentWalletClientError, match="invalid response"):
        AgentWalletClient().list_payments(10)


def test_cancel__posts_operation_to_production_wallet(monkeypatch):
    operation_id = uuid4()
    response = {
        "payment_operation_id": str(operation_id),
        "method": "vic",
        "new_authority_disabled_at": "2026-08-12T12:00:00Z",
        "remaining_authority": "absent",
    }
    captured = {}

    def fake_request(transport, method, url, body=None, request_headers=None):
        captured.update(method=method, url=url, body=body)
        return json.dumps(response).encode()

    monkeypatch.setenv("RAMP_AGENT_WALLET_API_KEY", "wallet-key")
    monkeypatch.setattr(BearerTokenTransport, "request", fake_request)

    result = CliRunner().invoke(
        cli,
        ["--agent", "agent-wallet", "cancel", str(operation_id)],
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "method": "POST",
        "url": (
            "https://wallet.ramp.com/developer/v1/agent-wallet/operations/"
            f"{operation_id}/cancel"
        ),
        "body": None,
    }
    assert json.loads(result.output) == {
        "schema_version": "1.0",
        "data": [response],
        "pagination": None,
    }


def test_cancel__rejects_mismatched_response_operation_id(monkeypatch):
    monkeypatch.setattr(
        AgentWalletClient,
        "cancel",
        lambda client, operation_id: {"payment_operation_id": str(uuid4())},
    )

    result = CliRunner().invoke(
        cli,
        ["--human", "agent-wallet", "cancel", str(uuid4())],
    )

    assert isinstance(result.exception, AgentWalletClientError)
    assert str(result.exception) == "Agent Wallet returned an invalid response"


def test_pay__posts_structured_request_to_production_wallet(monkeypatch):
    captured = {}

    def fake_pay(client, body):
        captured["body"] = body
        return {
            "payment_operation_id": body["payment_operation_id"],
            "status": "declined",
        }

    monkeypatch.setattr(AgentWalletClient, "pay", fake_pay)

    result = CliRunner().invoke(
        cli,
        ["--human", "agent-wallet", "pay", "cards"] + _card_args(),
    )

    assert result.exit_code == 0, result.output
    operation_id = UUID(captured["body"]["payment_operation_id"])
    assert captured == {
        "body": {
            "payment_operation_id": str(operation_id),
            "method": "vic",
            "method_request": {
                "merchant": {
                    "name": "Acme",
                    "url": "https://acme.example/checkout",
                    "country_code": "US",
                },
                "amount": {"value": 1250, "currency": "USD"},
                "expires_at": captured["body"]["method_request"]["expires_at"],
            },
        },
    }
    assert json.loads(result.stdout) == {
        "payment_operation_id": str(operation_id),
        "status": "declined",
    }


def test_pay__base_url_override_wins(monkeypatch):
    captured = {}
    monkeypatch.setenv("RAMP_AGENT_WALLET_API_URL", "https://wallet.example.test/")
    monkeypatch.setenv("RAMP_AGENT_WALLET_API_KEY", "wallet-key")
    client = AgentWalletClient()

    def fake_request(transport, method, url, body=None, request_headers=None):
        captured["url"] = url
        return b"{}"

    monkeypatch.setattr(BearerTokenTransport, "request", fake_request)

    client.pay({})

    assert captured["url"] == (
        "https://wallet.example.test/developer/v1/agent-wallet/pay"
    )


def test_pay__rejects_non_tls_remote_base_url_override(monkeypatch):
    monkeypatch.setenv("RAMP_AGENT_WALLET_API_URL", "http://wallet.example.test")
    client = AgentWalletClient()

    with pytest.raises(AgentWalletClientError) as exc_info:
        client.pay({})

    assert "must use HTTPS" in str(exc_info.value)


def test_pay__allows_loopback_http_base_url_override(monkeypatch):
    monkeypatch.setenv("RAMP_AGENT_WALLET_API_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("RAMP_AGENT_WALLET_API_KEY", "wallet-key")
    client = AgentWalletClient()
    captured = {}

    def fake_request(transport, method, url, body=None, request_headers=None):
        captured["url"] = url
        return b"{}"

    monkeypatch.setattr(BearerTokenTransport, "request", fake_request)

    client.pay({})

    assert captured["url"] == "http://127.0.0.1:8080/developer/v1/agent-wallet/pay"


def test_pay__uses_production_wallet_url():
    assert AgentWalletClient().payment_url == (
        "https://wallet.ramp.com/developer/v1/agent-wallet/pay"
    )


def test_pay__accepts_complete_json_body(monkeypatch):
    body = {
        "method": "vic",
        "future_core_field": {"preserve": True},
        "method_request": {
            "merchant": {
                "name": "Acme",
                "url": "https://acme.example/checkout",
                "country_code": "us",
            },
            "amount": {"value": 1250, "currency": "usd"},
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat(),
        },
    }
    captured = {}

    def fake_pay(client, request_body):
        captured["body"] = request_body
        return {"payment_operation_id": request_body["payment_operation_id"]}

    monkeypatch.setattr(AgentWalletClient, "pay", fake_pay)

    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "agent-wallet",
            "pay",
            "json",
            "--json",
            json.dumps(body),
        ],
    )

    assert result.exit_code == 0, result.output
    operation_id = UUID(captured["body"]["payment_operation_id"])
    assert captured["body"] == {"payment_operation_id": str(operation_id), **body}


def test_pay__rejects_non_vic_json():
    body = {
        "method": "mpp_stripe_spt",
        "method_request": {},
    }
    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "agent-wallet",
            "pay",
            "json",
            "--json",
            json.dumps(body),
        ],
    )

    assert result.exit_code != 0
    assert "Invalid value for '--json'" in result.output


def test_pay__rejects_caller_supplied_json_payment_id():
    body = {
        "payment_operation_id": str(uuid4()),
        "method": "vic",
        "method_request": {},
    }
    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "agent-wallet",
            "pay",
            "json",
            "--json",
            json.dumps(body),
        ],
    )

    assert result.exit_code != 0
    assert "CLI generates the payment ID" in result.output


def test_pay__requires_method_for_json_input():
    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "agent-wallet",
            "pay",
            "json",
            "--json",
            "{}",
        ],
    )

    assert result.exit_code != 0
    assert "--json must include method" in result.output


def test_pay__separates_method_specific_options():
    cards_help = CliRunner().invoke(cli, ["agent-wallet", "pay", "cards", "--help"])
    json_help = CliRunner().invoke(cli, ["agent-wallet", "pay", "json", "--help"])

    assert cards_help.exit_code == 0, cards_help.output
    assert "--merchant-name" in cards_help.output
    assert "--json" not in cards_help.output
    assert json_help.exit_code == 0, json_help.output
    assert "--json" in json_help.output
    assert "--merchant-name" not in json_help.output


def test_cancel__uses_payment_language_in_help():
    group_help = CliRunner().invoke(cli, ["agent-wallet", "--help"])
    cancel_help = CliRunner().invoke(cli, ["agent-wallet", "cancel", "--help"])

    assert group_help.exit_code == 0, group_help.output
    assert "Cancel a payment" in group_help.output
    assert cancel_help.exit_code == 0, cancel_help.output
    assert "PAYMENT_ID" in cancel_help.output
    assert "PAYMENT_OPERATION_ID" not in cancel_help.output


@pytest.mark.parametrize("payment_type", ["cards", "json"])
def test_pay__uses_payment_id_option(payment_type):
    help_result = CliRunner().invoke(
        cli, ["agent-wallet", "pay", payment_type, "--help"]
    )

    assert help_result.exit_code == 0, help_result.output
    assert "--payment-id" in help_result.output
    assert "--payment-operation-id" not in help_result.output
    assert "payment ID" in help_result.output


def test_pay__does_not_expose_dry_run():
    for payment_type in ("cards", "json"):
        result = CliRunner().invoke(
            cli, ["agent-wallet", "pay", payment_type, "--help"]
        )

        assert result.exit_code == 0, result.output
        assert "--dry-run" not in result.output
        assert "--dry_run" not in result.output


def test_pay__reuses_explicit_payment_id(monkeypatch):
    operation_id = uuid4()
    captured = {}

    def fake_pay(client, body):
        captured["body"] = body
        return {"payment_operation_id": body["payment_operation_id"]}

    monkeypatch.setattr(AgentWalletClient, "pay", fake_pay)
    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "agent-wallet",
            "pay",
            "cards",
            "--payment-id",
            str(operation_id),
        ]
        + _card_args(),
    )

    assert result.exit_code == 0, result.output
    assert captured["body"]["payment_operation_id"] == str(operation_id)


def test_pay_json__reuses_explicit_payment_id(monkeypatch):
    payment_id = uuid4()
    captured = {}

    def fake_pay(client, body):
        captured["body"] = body
        return {"payment_operation_id": body["payment_operation_id"]}

    monkeypatch.setattr(AgentWalletClient, "pay", fake_pay)
    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "agent-wallet",
            "pay",
            "json",
            "--payment-id",
            str(payment_id),
            "--json",
            json.dumps({"method": "vic", "method_request": {}}),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["body"]["payment_operation_id"] == str(payment_id)


def test_pay__rejects_mismatched_response_operation_id(monkeypatch):
    monkeypatch.setattr(
        AgentWalletClient,
        "pay",
        lambda client, body: {"payment_operation_id": str(uuid4())},
    )

    result = CliRunner().invoke(
        cli,
        ["--human", "agent-wallet", "pay", "cards"] + _card_args(),
    )

    assert isinstance(result.exception, AgentWalletClientError)
    assert str(result.exception) == "Agent Wallet returned an invalid response"


def test_pay__preserves_agent_wallet_client_error(monkeypatch):
    captured = {}

    def fail_pay(client, body):
        captured["body"] = body
        raise AgentWalletClientError("wallet failed")

    monkeypatch.setattr(AgentWalletClient, "pay", fail_pay)

    result = CliRunner().invoke(
        cli,
        ["--human", "agent-wallet", "pay", "cards"] + _card_args(),
    )

    assert isinstance(result.exception, AgentWalletClientError)
    assert str(result.exception) == "wallet failed"
    operation_id = UUID(captured["body"]["payment_operation_id"])
    assert f"Payment ID: {operation_id}" in result.output


@pytest.mark.parametrize("response", [b"secret", b"[]", b"null"])
def test_pay__rejects_invalid_success_response(monkeypatch, response):
    monkeypatch.setenv("RAMP_AGENT_WALLET_API_KEY", "wallet-key")
    client = AgentWalletClient()
    monkeypatch.setattr(
        BearerTokenTransport,
        "request",
        lambda *args, **kwargs: response,
    )

    with pytest.raises(AgentWalletClientError) as exc_info:
        client.pay({})

    assert str(exc_info.value) == "Agent Wallet returned an invalid response"
    assert "secret" not in str(exc_info.value)


def test_pay__never_falls_back_to_general_cli_token(isolated_config):
    store.save_tokens("production", "general-cli-token", "")

    with pytest.raises(AgentWalletAuthRequiredError, match="agent-wallet configure"):
        AgentWalletClient().pay({})


def test_pay__wallet_authorization_failure_uses_wallet_guidance(monkeypatch):
    monkeypatch.setenv("RAMP_AGENT_WALLET_API_KEY", "invalid-wallet-key")
    monkeypatch.setattr(
        BearerTokenTransport,
        "request",
        lambda *args, **kwargs: (_ for _ in ()).throw(ApiError(403, "forbidden")),
    )

    with pytest.raises(AgentWalletAuthRequiredError, match="agent-wallet configure"):
        AgentWalletClient().pay({})
