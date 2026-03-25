"""ramp feedback command — submit feedback about the Ramp Developer API or CLI."""

from __future__ import annotations

import json

import click
import httpx

from ramp_cli import __version__ as VERSION
from ramp_cli.auth import store
from ramp_cli.config.constants import PRODUCTION_BASE_URL, base_url
from ramp_cli.output.formatter import print_agent_json


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

    # Try to enrich with business info if authenticated (short timeout — optional context)
    try:
        if store.has_tokens(env):
            access_token, _ = store.get_tokens(env)
            with httpx.Client(timeout=3.0) as http:
                resp = http.get(
                    f"{base_url(env)}/developer/v1/business",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                resp.raise_for_status()
                data = json.loads(resp.content)
            biz_id = data.get("id", "")
            if biz_id:
                context_parts.append(f"biz={biz_id}")
    except Exception:
        pass

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
    try:
        with httpx.Client(timeout=15.0) as http:
            resp = http.get(
                url,
                params={"feedback": enriched, "source": "RAMP_CLI"},
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
