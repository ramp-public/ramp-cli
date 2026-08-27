"""Standalone agent identity commands."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import click

from ramp_cli.client.api import RampClient
from ramp_cli.commands.auth import login_standalone_agent, logout_profile
from ramp_cli.config import profiles
from ramp_cli.config.constants import api_url
from ramp_cli.output.formatter import print_agent_json, print_json, resolve_format
from ramp_cli.output.style import env_label
from ramp_cli.tools.commands import GeneratedToolGroup

CLIENT_ID_ENV_VAR = "RAMP_CLIENT_ID"
AGENTS_PATH = "/developer/v1/agents"


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


def _parse_json_body(raw_json: str) -> dict[str, Any]:
    try:
        body = json.loads(raw_json)
    except json.JSONDecodeError as error:
        raise click.BadParameter(
            f"invalid JSON: {error}", param_hint="'--json'"
        ) from error
    if not isinstance(body, dict):
        raise click.BadParameter(
            "agent body must be a JSON object", param_hint="'--json'"
        )
    return body


def _render_created_agent(response: bytes, ctx: click.Context) -> None:
    try:
        data = json.loads(response)
    except (json.JSONDecodeError, ValueError):
        data = {"raw": response.decode("utf-8", errors="replace")}

    if resolve_format(ctx.obj["format"], ctx.obj["config_format"]) == "json":
        print_agent_json(data, pagination=None)
        return

    print_json(data)
    if isinstance(data, dict) and data.get("client_secret"):
        click.echo(
            "\nThe client secret is shown only once. Store it now:\n"
            f"  export {CLIENT_ID_ENV_VAR}={data.get('client_id', '')}\n"
            "  export RAMP_CLIENT_SECRET=<client_secret above>\n"
            "Then authenticate as the agent with 'ramp agent login'.",
            err=True,
        )


@agent_group.command("create", short_help="Create a standalone agent")
@click.option("--name", default=None, help="Display name of the standalone agent")
@click.option(
    "--role-id",
    type=click.UUID,
    default=None,
    help=(
        "Existing custom role ID to assign. "
        "To assign several roles, pass role_ids in --json."
    ),
)
@click.option(
    "--description",
    default=None,
    help="Description of the standalone agent (max 300 characters)",
)
@click.option(
    "--owner-id",
    type=click.UUID,
    default=None,
    help="Owning user ID. Defaults to the authorizing user.",
)
@click.option(
    "--json",
    "json_body",
    default=None,
    help="Raw request body as JSON (merged over the individual flags)",
)
@click.option(
    "--dry_run",
    "--dry-run",
    "-n",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print request without sending",
)
@click.pass_context
def agent_create(
    ctx: click.Context,
    name: str | None,
    role_id: UUID | None,
    description: str | None,
    owner_id: UUID | None,
    json_body: str | None,
    dry_run: bool,
) -> None:
    """Create a standalone agent in the authenticated business.

    Requires one existing custom role. The Developer API takes a role_ids
    array, so --role-id is sent as an array of one; use --json to assign
    more than one role. The response includes show-once OAuth client
    credentials for the new agent.

    \b
    Example:
        ramp agent create --name "Procurement Agent" \\
            --role-id 7c322160-2871-4382-b026-92597ce3ed19

    Endpoint: POST /developer/v1/agents
    """
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if role_id is not None:
        body["role_ids"] = [str(role_id)]
    if description is not None:
        body["description"] = description
    if owner_id is not None:
        body["owner_id"] = str(owner_id)
    if json_body:
        body.update(_parse_json_body(json_body))

    if not body.get("name"):
        raise click.UsageError("Pass --name or provide name in --json.")
    if not body.get("role_ids"):
        raise click.UsageError("Pass --role-id or provide role_ids in --json.")

    env = ctx.obj["env"]
    if dry_run:
        url = api_url(env, AGENTS_PATH)
        if resolve_format(ctx.obj["format"], ctx.obj["config_format"]) == "json":
            print_agent_json(
                {"dry_run": True, "method": "POST", "url": url, "body": body},
                pagination=None,
            )
        else:
            click.echo(f"DRY RUN: POST {url}", err=True)
            print_json(body)
        return

    client = RampClient(env, profile=ctx.obj["profile"])
    response = client.post(AGENTS_PATH, json.dumps(body).encode())
    _render_created_agent(response, ctx)
