"""Standalone agent identity commands."""

from __future__ import annotations

import click

from ramp_cli.commands.auth import login_standalone_agent, logout_profile
from ramp_cli.config import profiles
from ramp_cli.output.style import env_label
from ramp_cli.tools.commands import GeneratedToolGroup

CLIENT_ID_ENV_VAR = "RAMP_CLIENT_ID"


@click.group(
    "agent",
    cls=GeneratedToolGroup,
    tool_category="agent",
    help="Manage standalone agent identities",
)
def agent_group() -> None:
    pass


@agent_group.command("login", help="Authenticate a standalone agent")
@click.option(
    "--client-id",
    envvar=CLIENT_ID_ENV_VAR,
    help=f"OAuth client ID. Defaults to {CLIENT_ID_ENV_VAR}.",
)
@click.option(
    "--client-secret",
    help="OAuth client secret. Prefer the RAMP_CLIENT_SECRET environment variable.",
)
@click.option(
    "--scope",
    "scopes",
    multiple=True,
    help="Request only this OAuth scope. Repeat for additional scopes.",
)
@click.pass_context
def agent_login(
    ctx: click.Context,
    client_id: str | None,
    client_secret: str | None,
    scopes: tuple[str, ...],
) -> None:
    """Authenticate with OAuth client credentials."""
    if not client_id:
        raise click.UsageError(f"Pass --client-id or set {CLIENT_ID_ENV_VAR}.")

    env = ctx.obj["env"]
    login_standalone_agent(
        ctx,
        env=env,
        label=env_label(env),
        client_id=client_id,
        client_secret=client_secret,
        no_browser=False,
        scopes=scopes,
        auth_level="auto",
    )


@agent_group.command("logout", help="Log out the standalone agent")
@click.pass_context
def agent_logout(ctx: click.Context) -> None:
    """Clear stored standalone agent credentials."""
    logout_profile(ctx, profiles.AGENT_PROFILE)
