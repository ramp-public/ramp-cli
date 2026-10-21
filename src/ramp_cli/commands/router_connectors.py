from __future__ import annotations

import click

from ramp_cli.client.router_user import RouterUserClient, selected_origin
from ramp_cli.commands.router_user import _output
from ramp_cli.errors import RampCLIError
from ramp_cli.router_connectors import auth, harnesses


def register(group: click.Group) -> None:
    @group.group(
        "connectors",
        short_help="Connector tools",
        help="Explicitly connect harnesses to Router connectors MCP.",
    )
    def connectors():
        pass

    @connectors.command("capabilities")
    @click.pass_context
    def capabilities(ctx):
        _output(
            ctx,
            {
                "contract_version": 1,
                "capabilities": ["router-connectors-mcp-v1"],
                "harnesses": list(harnesses.HARNESSES),
            },
        )

    @connectors.command("status")
    @click.pass_context
    def status(ctx):
        client = _client(ctx)
        try:
            _output(ctx, auth.status(client))
        finally:
            client.close()

    @connectors.command("enable")
    @click.option("--harness", required=True, type=click.Choice(harnesses.HARNESSES))
    @click.pass_context
    def enable(ctx, harness):
        client = _client(ctx)
        try:
            binding = auth.create_binding(client)
            result = harnesses.enable(harness, binding)
            _output(ctx, result)
        finally:
            client.close()

    @connectors.command("disable")
    @click.option("--harness", required=True, type=click.Choice(harnesses.HARNESSES))
    @click.pass_context
    def disable(ctx, harness):
        _output(ctx, harnesses.disable(harness))

    @connectors.command("bridge", hidden=True)
    @click.option("--binding", required=True)
    def bridge(binding):
        from ramp_cli.router_connectors.bridge import run  # noqa: PLC0415

        try:
            run(binding)
        except KeyboardInterrupt:
            raise SystemExit(130) from None
        except Exception as error:
            # The root CLI's automatic agent error envelope is not JSON-RPC.
            # Bridge startup/transport failures must never contaminate stdout.
            click.echo(
                "Router connectors bridge stopped. Check connector status and explicit Router authorization.",
                err=True,
            )
            raise SystemExit(
                error.code if isinstance(error, RampCLIError) else 1
            ) from None


def _client(ctx: click.Context) -> RouterUserClient:
    profile = ctx.obj.get("profile")
    return RouterUserClient(
        selected_origin(profile=profile),
        profile=profile,
        no_input=True,
        passive_edge=True,
    )
