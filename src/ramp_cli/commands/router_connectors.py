from __future__ import annotations

import re

import click

from ramp_cli.auth import oauth
from ramp_cli.client.router_user import RouterUserClient, selected_origin
from ramp_cli.commands import router_credentials
from ramp_cli.commands.router_user import _output
from ramp_cli.errors import ApiError, RampCLIError
from ramp_cli.output.formatter import resolve_format
from ramp_cli.router_connectors import auth, harnesses

# The fields `connectors list` promises; the rest of Router's row is internal.
CATALOG_FIELDS = (
    "key",
    "title",
    "description",
    "logo_url",
    "availability",
    "release_channel",
    "connected",
    "internal_service",
    "company_setup_required",
)
_CONNECTOR_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _catalog_row(row: dict) -> dict:
    return {field: row.get(field) for field in CATALOG_FIELDS}


def _connector_path(key: str, suffix: str = "") -> str:
    if not _CONNECTOR_KEY.match(key):
        raise click.BadParameter("Expected a connector key such as 'slack'.")
    return f"/admin/connectors/{key}{suffix}"


def _not_found(key: str) -> RampCLIError:
    return RampCLIError(
        f"No connector '{key}' is available to you in Router. "
        "Run `ramp router connectors list` to see them."
    )


def catalog(client: RouterUserClient) -> dict:
    """The connectors this member can see in Router, and which they've connected.
    Router answers 404 when connectors aren't on for the account."""
    try:
        rows = client.get("/admin/connectors")
    except ApiError as error:
        if error.status_code != 404:
            raise
        rows, available = [], False
    else:
        available = True
    return {
        "contract_version": 1,
        "available": available,
        "connectors": [_catalog_row(row) for row in rows],
    }


def connect_url(client: RouterUserClient, key: str) -> str:
    """The provider's consent page. It returns to Router's web app (named by Origin),
    so the browser that opens it must be signed in to Router as the same person."""
    path = _connector_path(key, "/connect-url")
    try:
        body = client.post(path, json={}, headers={"Origin": client.origin})
    except ApiError as error:
        raise _not_found(key) if error.status_code == 404 else error from None
    return body["url"]


def disconnect(client: RouterUserClient, key: str) -> dict:
    path = _connector_path(key)
    try:
        return _catalog_row(client.delete(path))
    except ApiError as error:
        raise _not_found(key) if error.status_code == 404 else error from None


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
                "capabilities": [
                    "router-connectors-mcp-v1",
                    "router-connectors-catalog-v1",
                ],
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

    @connectors.command("list")
    @click.pass_context
    def list_connectors(ctx):
        """Connectors you can use in Router, and whether you've connected each."""
        client = _client(ctx)
        try:
            result = catalog(client)
        finally:
            client.close()
        if not _human(ctx):
            return _output(ctx, result)
        if not result["available"]:
            click.echo("Connectors aren't available for this Router account.")
        for row in result["connectors"]:
            state = "connected" if row["connected"] else row["availability"]
            click.echo(f"{row['key']}: {row['title']} ({state})")

    @connectors.command("connect")
    @click.argument("key")
    @click.option(
        "--no-browser", is_flag=True, help="Print the link instead of opening it."
    )
    @click.pass_context
    def connect(ctx, key, no_browser):
        """Connect a service (e.g. slack) in your browser.

        The browser must be signed in to Router as you: the service returns
        there to finish. Run `ramp router connectors list` afterwards.
        """
        client = _client(ctx)
        try:
            url = connect_url(client, key)
        finally:
            client.close()
        opened = not no_browser and oauth._open_browser(url)
        if not _human(ctx):
            return _output(
                ctx, {"contract_version": 1, "key": key, "url": url, "opened": opened}
            )
        click.echo(f"Opened {key} in your browser." if opened else url)

    @connectors.command("disconnect")
    @click.argument("key")
    @click.pass_context
    def disconnect_connector(ctx, key):
        """Remove your connection to a service, including in other Ramp products."""
        client = _client(ctx)
        try:
            row = disconnect(client, key)
        finally:
            client.close()
        _output(ctx, {"contract_version": 1, "connector": row})

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


def _human(ctx: click.Context) -> bool:
    if ctx.obj.get("agent_mode"):
        return False
    return (
        resolve_format(ctx.obj.get("format"), ctx.obj.get("config_format", ""))
        == "table"
    )


def _client(ctx: click.Context) -> RouterUserClient:
    profile = ctx.obj.get("profile")
    return RouterUserClient(
        selected_origin(profile=profile),
        profile=profile,
        no_input=True,
        passive_edge=True,
    )
