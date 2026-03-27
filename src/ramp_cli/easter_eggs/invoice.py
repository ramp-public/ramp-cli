"""ramp invoice <bill_id> — cohesive invoice detail view."""

import json

import click

from ramp_cli.client.api import RampClient
from ramp_cli.output.formatter import print_agent_json
from ramp_cli.views.invoice import render_bill_invoice


@click.command(
    "invoice", hidden=True, help="Render a bill as a styled invoice document"
)
@click.argument("bill_id")
@click.pass_context
def invoice_cmd(ctx: click.Context, bill_id: str) -> None:
    client = RampClient(ctx.obj["env"])
    body = client.get(f"/developer/v1/bills/{bill_id}")

    if not render_bill_invoice(body):
        print_agent_json(json.loads(body), pagination=None)
