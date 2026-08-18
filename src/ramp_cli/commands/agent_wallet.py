"""Direct Agent Wallet service commands."""

from __future__ import annotations

import json
import os
from typing import Any
from uuid import UUID, uuid4

import click

from ramp_cli.auth.agent_wallet import save_api_key
from ramp_cli.client.agent_wallet import (
    AgentWalletApiError,
    AgentWalletClient,
    AgentWalletClientError,
)
from ramp_cli.commands.agent_wallet_validation import (
    build_structured_card_body,
    validate_card_json_body,
)
from ramp_cli.errors import ApiError
from ramp_cli.output.formatter import (
    canonical_to_display,
    print_agent_json,
    print_json,
    resolve_format,
)
from ramp_cli.tools.commands import GeneratedToolGroup, build_tool_command
from ramp_cli.tools.parser import ToolDef

CONFIGURE_KEY_ENV = "RAMP_AGENT_WALLET_CONFIGURE_API_KEY"


def _build_policy_group(tool: ToolDef) -> click.Group:
    group = click.Group(
        "policy",
        help="Publish or update an Agent Wallet policy.",
        no_args_is_help=True,
    )

    for name, help_text in (
        ("publish", "Publish a policy and make it active."),
        ("update", "Update the active policy by publishing a new version."),
    ):
        command = build_tool_command(
            tool,
            group_name="agent-wallet",
            command_name="policy",
        )
        command.help = help_text
        command.short_help = help_text
        group.add_command(command, name)

    return group


class _AgentWalletGroup(GeneratedToolGroup):
    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except ApiError as error:
            raise AgentWalletApiError(error) from error

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        if cmd_name == "policy":
            for command_name, tool in self._generated_commands(ctx):
                if command_name == cmd_name:
                    return _build_policy_group(tool)
        return super().get_command(ctx, cmd_name)


def _parse_json_body(raw_json: str) -> dict[str, Any]:
    try:
        body = json.loads(raw_json)
    except json.JSONDecodeError as error:
        raise click.BadParameter(
            f"invalid JSON: {error}", param_hint="'--json'"
        ) from error
    if not isinstance(body, dict):
        raise click.BadParameter(
            "payment body must be a JSON object", param_hint="'--json'"
        )
    return body


def _render(payload: Any, ctx: click.Context) -> None:
    if resolve_format(ctx.obj["format"], ctx.obj["config_format"]) == "json":
        print_agent_json(payload, pagination=None)
    else:
        print_json(payload)


def _submit_payment(
    ctx: click.Context,
    payment_id: UUID,
    body: dict[str, Any],
    payment_command: str,
    display_amount: str | None = None,
) -> None:
    click.echo(f"Payment ID: {payment_id}", err=True)
    is_human = resolve_format(ctx.obj["format"], ctx.obj["config_format"]) != "json"
    if is_human:
        click.echo(
            "To retry safely, reuse this payment ID:\n"
            f"  ramp agent-wallet pay {payment_command} --payment-id {payment_id} ...\n\n"
            "If the request times out or credentials are unavailable, first run:\n"
            "  ramp agent-wallet list --limit 10\n"
            "before creating a new payment.",
            err=True,
        )
    if display_amount and is_human:
        click.echo(f"Amount: {display_amount}", err=True)
    result = AgentWalletClient().pay(body)
    if result.get("payment_operation_id") != str(payment_id):
        raise AgentWalletClientError("Agent Wallet returned an invalid response")
    _render(result, ctx)


@click.group(
    "agent-wallet",
    cls=_AgentWalletGroup,
    tool_category="agent-wallet",
    help="Manage Agent Wallet policy and payments",
)
def agent_wallet_group() -> None:
    pass


@agent_wallet_group.command(
    "configure",
    help="Configure this CLI with a standalone agent's Agent Wallet API key",
)
@click.option(
    "--api-key",
    metavar="KEY",
    envvar=CONFIGURE_KEY_ENV,
    help=(
        "Agent Wallet API key. Prompts securely when omitted. "
        f"Set {CONFIGURE_KEY_ENV} to keep it out of shell history."
    ),
)
@click.pass_context
def configure(ctx: click.Context, api_key: str | None) -> None:
    if api_key is None:
        if ctx.obj["no_input"]:
            raise click.UsageError(
                f"Pass --api-key or set {CONFIGURE_KEY_ENV} in non-interactive mode."
            )
        api_key = click.prompt("Agent Wallet API key", hide_input=True)

    api_key = api_key.strip()
    os.environ.pop(CONFIGURE_KEY_ENV, None)
    if not api_key:
        raise click.UsageError("The Agent Wallet API key cannot be empty.")

    save_api_key(api_key)
    payload = {"configured": True}
    if resolve_format(ctx.obj["format"], ctx.obj["config_format"]) == "json":
        print_agent_json(payload, pagination=None)
    else:
        click.echo("Configured Agent Wallet.")


@agent_wallet_group.command("list", short_help="List recent payments")
@click.option(
    "--limit",
    type=click.IntRange(min=1, max=100),
    default=100,
    show_default=True,
    help="Maximum number of payments to return",
)
@click.pass_context
def list_payments(ctx: click.Context, limit: int) -> None:
    """List recent Agent Wallet payments."""
    payments = AgentWalletClient().list_payments(limit)
    _render(payments, ctx)


@agent_wallet_group.command("cancel", short_help="Cancel a payment")
@click.argument("payment_id", type=click.UUID)
@click.pass_context
def cancel_payment(ctx: click.Context, payment_id: UUID) -> None:
    """Disable future authority for a payment."""
    result = AgentWalletClient().cancel(payment_id)
    if result.get("payment_operation_id") != str(payment_id):
        raise AgentWalletClientError("Agent Wallet returned an invalid response")
    _render(result, ctx)


@agent_wallet_group.group(
    "pay",
    short_help="Submit a payment to Agent Wallet",
)
def pay_group() -> None:
    """Submit a payment using a method-specific body."""


@pay_group.command("cards", short_help="Submit a virtual card payment")
@click.option(
    "--payment-id",
    type=click.UUID,
    default=None,
    help="Reuse a CLI-generated payment ID when retrying a payment",
)
@click.option("--merchant-name", default=None, help="Merchant display name")
@click.option("--merchant-url", default=None, help="Merchant HTTP or HTTPS URL")
@click.option(
    "--merchant-country-code",
    default=None,
    help="Two-letter merchant country code",
)
@click.option(
    "--amount",
    default=None,
    metavar="USD",
    help="Payment amount in US dollars, e.g. 10.35",
)
@click.option(
    "--expires-at",
    default=None,
    help="Future ISO 8601 credential expiration timestamp",
)
@click.pass_context
def pay_cards(
    ctx: click.Context,
    payment_id: UUID | None,
    merchant_name: str | None,
    merchant_url: str | None,
    merchant_country_code: str | None,
    amount: str | None,
    expires_at: str | None,
) -> None:
    """Submit a virtual card payment."""
    payment_id = payment_id or uuid4()
    body = build_structured_card_body(
        payment_id,
        merchant_name,
        merchant_url,
        merchant_country_code,
        amount,
        expires_at,
    )
    request_amount = body["method_request"]["amount"]
    _submit_payment(
        ctx,
        payment_id,
        body,
        "cards",
        display_amount=(
            f"{request_amount['currency']} "
            f"{canonical_to_display(request_amount['value'], request_amount['currency'])}"
        ),
    )


@pay_group.command("json", short_help="Submit a raw JSON payment body")
@click.option(
    "--payment-id",
    type=click.UUID,
    default=None,
    help="Reuse a CLI-generated payment ID when retrying a payment",
)
@click.option(
    "--json",
    "json_body",
    required=True,
    help="Raw payment body; the CLI adds its generated payment ID",
)
@click.pass_context
def pay_json(
    ctx: click.Context,
    payment_id: UUID | None,
    json_body: str,
) -> None:
    """Submit a payment body as JSON."""
    payment_id = payment_id or uuid4()
    body = _parse_json_body(json_body)
    if "payment_operation_id" in body:
        raise click.UsageError(
            "The CLI generates the payment ID; omit payment_operation_id from --json."
        )
    if "method" not in body:
        raise click.UsageError("--json must include method.")
    body = {"payment_operation_id": str(payment_id), **body}
    body = validate_card_json_body(body)
    _submit_payment(ctx, payment_id, body, "json")
