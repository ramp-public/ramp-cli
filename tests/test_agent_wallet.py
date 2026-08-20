"""Tests for direct Agent Wallet commands."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from click.testing import CliRunner

from ramp_cli.client.agent_wallet import (
    AgentWalletApiError,
    AgentWalletClient,
    AgentWalletClientError,
)
from ramp_cli.client.transport import AuthenticatedRampTransport
from ramp_cli.errors import ApiError
from ramp_cli.main import cli
from ramp_cli.specs import AGENT_TOOL_SPEC
from ramp_cli.tools.parser import ToolDef, parse_spec


def _card_args() -> list[str]:
    return [
        "--merchant-name",
        "Acme",
        "--merchant-url",
        "https://acme.example/checkout",
        "--merchant-country-code",
        "us",
        "--amount",
        "12.50",
        "--expires-at",
        (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    ]


POLICY_PATH = "/developer/v1/agent-wallet/agents/{agent_id}/policies"


def _use_bundled_spec(monkeypatch) -> list[ToolDef]:
    bundled_tools = parse_spec(AGENT_TOOL_SPEC)
    monkeypatch.setattr("ramp_cli.main.maybe_sync", lambda env: None)
    monkeypatch.setattr(
        "ramp_cli.tools.commands.maybe_sync", lambda env, **kwargs: None
    )
    monkeypatch.setattr(
        "ramp_cli.tools.commands.list_tool_defs", lambda env: bundled_tools
    )
    return bundled_tools


def _policy_body() -> dict:
    return {
        "configurations": [
            {"configuration_id": "default", "method": "merchant-authorization"}
        ],
        "constraints": {
            "currency": "USD",
            "ends_at": "2027-01-01T00:00:00Z",
            "max_amount": 5000,
            "max_amount_per_payment": 1000,
            "max_payments": 5,
            "starts_at": "2026-01-01T00:00:00Z",
        },
        "policy_version": "00000000-0000-0000-0000-000000000001",
        "schema_version": 1,
    }


def test_policy_actions_are_available_from_bundled_spec(monkeypatch) -> None:
    bundled_tools = _use_bundled_spec(monkeypatch)
    matches = [tool for tool in bundled_tools if tool.path == POLICY_PATH]

    assert len(matches) == 1
    assert (matches[0].alias, matches[0].category, matches[0].http_method) == (
        "policy",
        "agent-wallet",
        "post",
    )
    assert matches[0].required_scopes == ["agent_wallet_policy:write"]

    result = CliRunner().invoke(cli, ["agent-wallet", "policy", "--help"])

    assert result.exit_code == 0, result.output
    assert "update" in result.output
    assert "Update the active policy by publishing a new version." in result.output


@pytest.mark.parametrize("command", ["publish", "update"])
def test_policy_commands_use_existing_publication_endpoint(monkeypatch, command):
    _use_bundled_spec(monkeypatch)
    agent_id = "00000000-0000-0000-0000-000000000000"
    policy = _policy_body()

    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "agent-wallet",
            "policy",
            command,
            agent_id,
            "--json",
            json.dumps(policy),
            "--dry_run",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"][0] == {
        "dry_run": True,
        "method": "POST",
        "url": (
            f"https://api.ramp.com/developer/v1/agent-wallet/agents/{agent_id}/policies"
        ),
        "body": policy,
        "headers": {"X-Ramp-Agent-Mode": "agent"},
    }


def test_agent_wallet_authentication_service_error_is_retryable(monkeypatch):
    _use_bundled_spec(monkeypatch)

    def fail(*args, **kwargs):
        raise ApiError(503, '{"detail":"Authentication service unavailable"}')

    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", fail)

    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "agent-wallet",
            "policy",
            "update",
            "00000000-0000-0000-0000-000000000000",
            "--json",
            json.dumps(_policy_body()),
        ],
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, AgentWalletApiError)
    assert result.exception.status_code == 503
    assert str(result.exception) == (
        "Agent Wallet request failed: The authentication service is temporarily "
        "unavailable. Try again shortly."
    )
    assert "agent-wallet configure" not in result.output
    assert '{"detail"' not in result.output


def test_list__returns_recent_payments(monkeypatch):
    operation_id = uuid4()
    payments = [
        {
            "payment_operation_id": str(operation_id),
            "method": "card",
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
            "payments",
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


def test_list__shows_human_readable_empty_state(monkeypatch):
    monkeypatch.setattr(
        AgentWalletClient,
        "list_payments",
        lambda client, limit: [],
    )

    result = CliRunner().invoke(
        cli,
        ["--human", "agent-wallet", "payments", "list", "--limit", "1"],
    )

    assert result.exit_code == 0, result.output
    assert result.output == "No Agent Wallet payments found.\n"


def test_list__preserves_machine_readable_empty_state(monkeypatch):
    monkeypatch.setattr(
        AgentWalletClient,
        "list_payments",
        lambda client, limit: [],
    )

    result = CliRunner().invoke(
        cli,
        ["--agent", "agent-wallet", "payments", "list", "--limit", "1"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "schema_version": "1.0",
        "data": [],
        "pagination": None,
    }


def test_top_level_list__is_removed():
    result = CliRunner().invoke(cli, ["agent-wallet", "list"])

    assert result.exit_code == 2
    assert "No such command 'list'" in result.output


def test_payments__is_canonical_help_surface():
    group_help = CliRunner().invoke(cli, ["agent-wallet", "--help"])
    payments_help = CliRunner().invoke(cli, ["agent-wallet", "payments", "--help"])

    assert group_help.exit_code == 0, group_help.output
    assert payments_help.exit_code == 0, payments_help.output
    assert "  payments " in group_help.output
    assert "  list " not in group_help.output
    assert "  list " in payments_help.output
    assert "  cancel " in payments_help.output


def test_list__requests_server_side_payment_history(monkeypatch):
    captured = {}

    def fake_request(
        transport, method, url, body=None, request_headers=None, proxied=False
    ):
        captured.update(method=method, url=url, body=body)
        return b"[]"

    monkeypatch.setattr(AuthenticatedRampTransport, "request", fake_request)

    assert AgentWalletClient("production").list_payments(10) == []
    assert captured == {
        "method": "GET",
        "url": (
            "https://wallet.ramp.com/developer/v1/agent-wallet/operations?limit=10"
        ),
        "body": None,
    }


@pytest.mark.parametrize("response", [b"{}", b"[1]"])
def test_list__rejects_invalid_success_response(monkeypatch, response):
    monkeypatch.setattr(
        AuthenticatedRampTransport,
        "request",
        lambda *args, **kwargs: response,
    )

    with pytest.raises(AgentWalletClientError, match="invalid response"):
        AgentWalletClient("production").list_payments(10)


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(["payments", "cancel"], id="canonical"),
        pytest.param(["cancel"], id="legacy"),
    ],
)
def test_cancel__posts_operation_to_production_wallet(monkeypatch, command):
    operation_id = uuid4()
    response = {
        "payment_operation_id": str(operation_id),
        "method": "card",
        "new_authority_disabled_at": "2026-08-12T12:00:00Z",
        "remaining_authority": "absent",
    }
    captured = {}

    def fake_request(
        transport, method, url, body=None, request_headers=None, proxied=False
    ):
        captured.update(method=method, url=url, body=body)
        return json.dumps(response).encode()

    monkeypatch.setattr(AuthenticatedRampTransport, "request", fake_request)

    result = CliRunner().invoke(
        cli,
        ["--agent", "agent-wallet", *command, str(operation_id)],
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
            "method": "card",
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


def test_pay__displays_normalized_usd_amount(monkeypatch):
    captured = {}

    def fake_pay(client, body):
        captured["body"] = body
        return {"payment_operation_id": body["payment_operation_id"]}

    monkeypatch.setattr(AgentWalletClient, "pay", fake_pay)

    result = CliRunner().invoke(
        cli,
        ["--human", "agent-wallet", "pay", "cards"] + _card_args(),
    )

    assert result.exit_code == 0, result.output
    assert captured["body"]["method_request"]["amount"] == {
        "value": 1250,
        "currency": "USD",
    }
    assert "Amount: USD 12.50" in result.output


@pytest.mark.parametrize(
    ("amount", "message"),
    [
        ("10.351", "at most 2 decimal places"),
        ("0", "greater than zero"),
        ("not-a-number", "decimal amount"),
        (
            "123456789012345678901234567.89",
            "must not exceed USD 99999999999999999999.99",
        ),
    ],
)
def test_pay__rejects_invalid_usd_amounts(amount, message):
    args = _card_args()
    amount_index = args.index("--amount") + 1
    args[amount_index] = amount

    result = CliRunner().invoke(
        cli,
        ["--human", "agent-wallet", "pay", "cards"] + args,
    )

    assert result.exit_code != 0
    assert message in result.output


def test_pay__base_url_override_wins(monkeypatch):
    captured = {}
    monkeypatch.setenv("RAMP_AGENT_WALLET_API_URL", "https://wallet.example.test/")
    client = AgentWalletClient("production")

    def fake_request(
        transport, method, url, body=None, request_headers=None, proxied=False
    ):
        captured["url"] = url
        return b"{}"

    monkeypatch.setattr(AuthenticatedRampTransport, "request", fake_request)

    client.pay({})

    assert captured["url"] == (
        "https://wallet.example.test/developer/v1/agent-wallet/pay"
    )


def test_pay__rejects_non_tls_remote_base_url_override(monkeypatch):
    monkeypatch.setenv("RAMP_AGENT_WALLET_API_URL", "http://wallet.example.test")
    client = AgentWalletClient("production")

    with pytest.raises(AgentWalletClientError) as exc_info:
        client.pay({})

    assert "must use HTTPS" in str(exc_info.value)


def test_pay__allows_loopback_http_base_url_override(monkeypatch):
    monkeypatch.setenv("RAMP_AGENT_WALLET_API_URL", "http://127.0.0.1:8080")
    client = AgentWalletClient("production")
    captured = {}

    def fake_request(
        transport, method, url, body=None, request_headers=None, proxied=False
    ):
        captured["url"] = url
        return b"{}"

    monkeypatch.setattr(AuthenticatedRampTransport, "request", fake_request)

    client.pay({})

    assert captured["url"] == "http://127.0.0.1:8080/developer/v1/agent-wallet/pay"


def test_pay__uses_production_vault_proxy_url():
    assert AgentWalletClient("production").payment_url == (
        "https://vault-wallet.ramp.com/developer/v1/agent-wallet/pay"
    )


def test_pay__marks_vault_request_as_proxied(monkeypatch):
    captured = {}

    def fake_request(
        transport, method, url, body=None, request_headers=None, proxied=False
    ):
        captured["proxied"] = proxied
        return b"{}"

    monkeypatch.setattr(AuthenticatedRampTransport, "request", fake_request)

    AgentWalletClient("production").pay({})

    assert captured["proxied"]


def test_pay__does_not_mark_base_url_override_as_proxied(monkeypatch):
    monkeypatch.setenv("RAMP_AGENT_WALLET_API_URL", "https://wallet.example.test")
    captured = {}

    def fake_request(
        transport, method, url, body=None, request_headers=None, proxied=False
    ):
        captured["proxied"] = proxied
        return b"{}"

    monkeypatch.setattr(AuthenticatedRampTransport, "request", fake_request)

    AgentWalletClient("production").pay({})

    assert not captured["proxied"]


def test_pay__preserves_base_url_override(monkeypatch):
    monkeypatch.setenv("RAMP_AGENT_WALLET_API_URL", "https://demo-wallet.ramp.com")

    assert AgentWalletClient("production").payment_url == (
        "https://demo-wallet.ramp.com/developer/v1/agent-wallet/pay"
    )


def test_pay__accepts_complete_json_body(monkeypatch):
    body = {
        "method": "card",
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


def test_pay__rejects_non_card_json():
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
        "method": "card",
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
    assert "--amount USD" in cards_help.output
    assert "--amount-value" not in cards_help.output
    assert "--amount-currency" not in cards_help.output
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


@pytest.mark.parametrize(
    ("payment_command", "payment_args"),
    [
        ("cards", _card_args()),
        ("json", ["--json", json.dumps({"method": "card", "method_request": {}})]),
    ],
)
def test_pay__shows_safe_retry_guidance(monkeypatch, payment_command, payment_args):
    payment_id = uuid4()
    monkeypatch.setattr(
        AgentWalletClient,
        "pay",
        lambda client, body: {"payment_operation_id": body["payment_operation_id"]},
    )

    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "agent-wallet",
            "pay",
            payment_command,
            "--payment-id",
            str(payment_id),
            *payment_args,
        ],
    )

    assert result.exit_code == 0, result.output
    assert (
        f"ramp agent-wallet pay {payment_command} --payment-id {payment_id} ..."
        in result.output
    )
    assert "ramp agent-wallet payments list --limit 10" in result.output


def test_pay__omits_retry_guidance_from_agent_output(monkeypatch):
    monkeypatch.setattr(
        AgentWalletClient,
        "pay",
        lambda client, body: {"payment_operation_id": body["payment_operation_id"]},
    )

    result = CliRunner().invoke(
        cli,
        ["--agent", "agent-wallet", "pay", "cards", *_card_args()],
    )

    assert result.exit_code == 0, result.output
    assert "To retry safely" not in result.output
    assert "ramp agent-wallet list" not in result.output


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
            json.dumps({"method": "card", "method_request": {}}),
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
    assert f"--payment-id {operation_id}" in result.output
    assert "ramp agent-wallet payments list --limit 10" in result.output


@pytest.mark.parametrize("response", [b"secret", b"[]", b"null"])
def test_pay__rejects_invalid_success_response(monkeypatch, response):
    client = AgentWalletClient("production")
    monkeypatch.setattr(
        AuthenticatedRampTransport,
        "request",
        lambda *args, **kwargs: response,
    )

    with pytest.raises(AgentWalletClientError) as exc_info:
        client.pay({})

    assert str(exc_info.value) == "Agent Wallet returned an invalid response"
    assert "secret" not in str(exc_info.value)


def test_pay__surfaces_authorization_failures_as_api_errors(monkeypatch):
    monkeypatch.setattr(
        AuthenticatedRampTransport,
        "request",
        lambda *args, **kwargs: (_ for _ in ()).throw(ApiError(403, "forbidden")),
    )

    with pytest.raises(ApiError) as exc_info:
        AgentWalletClient("production").pay({})

    assert exc_info.value.status_code == 403


def test_pay__auth_service_outage_is_reported_as_retryable(monkeypatch):
    monkeypatch.setattr(
        AuthenticatedRampTransport,
        "request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ApiError(503, '{"detail":"Authentication service unavailable"}')
        ),
    )

    result = CliRunner().invoke(
        cli,
        ["--human", "agent-wallet", "pay", "cards", *_card_args()],
    )

    assert isinstance(result.exception, AgentWalletApiError)
    assert str(result.exception) == (
        "Agent Wallet request failed: The authentication service is temporarily "
        "unavailable. Try again shortly."
    )
    assert "agent-wallet configure" not in result.output
