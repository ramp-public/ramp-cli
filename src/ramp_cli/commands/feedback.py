"""ramp feedback command — submit feedback about the Ramp Developer API or CLI."""

from __future__ import annotations

import json

import click
import httpx

from ramp_cli import __version__ as VERSION
from ramp_cli.auth import store
from ramp_cli.auth.environment import extra_auth_headers
from ramp_cli.config.constants import PRODUCTION_BASE_URL, api_url
from ramp_cli.output.formatter import print_agent_json


def _fetch_user_context(env: str) -> tuple[str, str]:
    """Return (business_id, user_id) if authenticated, else ("", "").

    Calls ``GET /developer/v1/token/info`` which returns both
    ``business_id`` and ``user_id`` for the current token.  This endpoint
    requires any valid Bearer token (no specific OAuth scope) and is the
    same one the MCP server uses for session context.
    """
    try:
        if not store.has_tokens(env):
            return "", ""
        access_token, _ = store.get_tokens(env)
    except Exception:
        # Malformed or unreadable config — enrichment is best-effort.
        return "", ""

    try:
        with httpx.Client(timeout=3.0) as http:
            resp = http.get(
                api_url(env, "/developer/v1/token/info"),
                headers={
                    "Authorization": f"Bearer {access_token}",
                    **extra_auth_headers(env),
                },
            )
            resp.raise_for_status()
            data = json.loads(resp.content)
        return data.get("business_id", ""), data.get("user_id", "")
    except Exception:
        return "", ""


@click.command("feedback", help="Submit feedback about the CLI")
@click.argument("text")
@click.pass_context
def feedback_cmd(ctx: click.Context, text: str) -> None:
    """Submit feedback about the Ramp Developer API or CLI."""
    text = text.strip()
    if len(text) < 10:
        raise click.ClickException("Feedback must be at least 10 characters.")
    if len(text) > 1000:
        raise click.ClickException("Feedback must be at most 1000 characters.")

    agent_mode = ctx.obj.get("agent_mode", False)
    env = ctx.obj.get("env", "production")

    # Build context header.
    # Avoid brackets, pipes, and special chars that trigger Cloudflare WAF.
    context_parts = [
        f"Ramp CLI v{VERSION}",
        f"agent={str(agent_mode).lower()}",
        f"env={env}",
    ]

    # Fetch identifying info when authenticated (short timeout — optional).
    biz_id, user_id = _fetch_user_context(env)
    if biz_id:
        context_parts.append(f"biz={biz_id}")

    header = "(" + ", ".join(context_parts) + ")"
    enriched = f"{header} {text}"

    # Submit via public endpoint (always production, no auth).
    # Explicit headers required to pass Cloudflare WAF.
    url = PRODUCTION_BASE_URL + "/v1/public/api-feedback/llm"
    headers = {
        "User-Agent": f"ramp-cli/{VERSION}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    # Build query params — always include feedback and source, optionally
    # include business_id and user_id as structured params so they appear
    # as indexed fields in Datadog rather than buried in freetext.
    params: dict[str, str] = {"feedback": enriched, "source": "RAMP_CLI"}
    if biz_id:
        params["business_id"] = biz_id
    if user_id:
        params["user_id"] = user_id

    try:
        with httpx.Client(timeout=15.0) as http:
            resp = http.get(
                url,
                params=params,
                headers=headers,
            )
            resp.raise_for_status()
    except httpx.TimeoutException:
        raise click.ClickException("Request timed out. Please try again.")
    except httpx.HTTPStatusError as e:
        raise click.ClickException(
            f"Server returned {e.response.status_code}. Please try again."
        )
    except httpx.HTTPError:
        raise click.ClickException(
            "Network error. Please check your connection and try again."
        )

    if agent_mode:
        print_agent_json(
            {"message": "Feedback submitted successfully"}, pagination=None
        )
    elif not ctx.obj.get("quiet", False):
        click.echo("Feedback submitted. Thank you!")
