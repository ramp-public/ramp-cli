from __future__ import annotations

import click

from ramp_cli.client.router_user import RouterUserClient, selected_origin
from ramp_cli.commands import router_credentials
from ramp_cli.commands.router_user import _output
from ramp_cli.errors import RampCLIError
from ramp_cli.router_connectors import auth, harnesses


def _renew_other_harnesses(clients: tuple[str, ...], binding: str) -> None:
    target = auth.load_binding(binding)
    for harness in harnesses.HARNESSES:
        if harness in clients:
            continue
        try:
            registration = harnesses.status(harness)
            if registration["state"] != "enabled":
                continue
            previous = auth.load_binding(registration["binding_id"])
        except RampCLIError:
            # An unrelated malformed/custom configuration must not block setup.
            continue
        # Fresh consent replaces the shared device session. Renew previously
        # enabled registrations, but never retarget them to another account,
        # business, deployment, or credential profile after an account switch.
        if (
            previous["origin"] == target["origin"]
            and previous.get("profile") == target.get("profile")
            and all(
                previous["authorization"][field] == target["authorization"][field]
                for field in auth.IDENTITY_FIELDS
                if field != "session_id"
            )
        ):
            harnesses.enable(harness, binding)


def configure_harnesses(
    ctx: click.Context,
    clients: tuple[str, ...],
    *,
    base_url: str,
    api_key: str | None,
    no_browser: bool,
    reuse_existing_key: bool,
    can_prompt: bool,
) -> tuple[str, bool]:
    from ramp_cli.commands import router  # noqa: PLC0415

    origin = router._statusline_origin(base_url)
    auth.resource_for_origin(origin)
    if not router._same_router(base_url, router.router_base_url_for(origin)):
        raise click.UsageError(
            "--connectors requires --base-url and --ui-url to name the same Router deployment."
        )
    if api_key is None:
        if reuse_existing_key or not can_prompt:
            saved = router._stored_router_api_key_choices()
            if len(saved) > 1:
                raise click.UsageError(
                    "Saved Ramp Router keys disagree. Run 'ramp router configure "
                    "--connectors' interactively to choose a key, or supply --api-key."
                )
            if saved:
                if not reuse_existing_key:
                    raise click.UsageError(
                        "A saved Ramp Router key exists. Run interactively to choose "
                        "a key, or pass --reuse-existing-key or --api-key."
                    )
                api_key = saved[0][0]
        else:
            api_key = router._pick_stored_router_api_key()

    if ctx.obj.get("no_input"):
        raise click.UsageError(
            "Connector setup requires a fresh browser sign-in on every run; "
            "omit --no-input and --agent."
        )
    client = RouterUserClient(
        origin,
        profile=ctx.obj.get("profile"),
        # After explicit connector login, renewal must not silently replace it
        # with an ordinary login that drops connector authorization.
        no_input=True,
    )
    try:
        router_credentials.sign_in(client, connectors=True, no_browser=no_browser)
        binding = auth.create_binding(client)
        for harness in clients:
            harnesses.enable(harness, binding)
        _renew_other_harnesses(clients, binding)

        if api_key is not None:
            return api_key, False

        return router_credentials.create_key(client), True
    finally:
        client.close()


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
