"""ramp getting-started — interactive orientation for new users."""

from __future__ import annotations

import click

from ramp_cli.auth.store import get_granted_scopes, is_authenticated
from ramp_cli.onboarding import print_getting_started
from ramp_cli.output.formatter import print_agent_json, resolve_format
from ramp_cli.tools.registry import (
    CATEGORY_ALIAS_GROUPS,
    CATEGORY_REMAP,
    list_categories,
)


def _remap_categories(categories: dict[str, list]) -> dict[str, list]:
    """Apply the same category remapping that main.py uses for CLI groups.

    E.g. ``agent_cards`` is merged into ``funds`` so that the onboarding
    guide shows names that match invokable ``ramp <resource>`` groups.

    ``CATEGORY_ALIAS_GROUPS`` (e.g. ``cards``) are additionally surfaced as
    their own group so the guide mirrors the CLI, where those tools are
    reachable from both their remapped home and the alias group.
    """
    merged: dict[str, list] = {}
    for cat, tools in categories.items():
        target = CATEGORY_REMAP.get(cat, cat)
        merged.setdefault(target, []).extend(tools)
    for cat in CATEGORY_ALIAS_GROUPS:
        alias_tools = categories.get(cat)
        if alias_tools:
            merged.setdefault(cat, []).extend(alias_tools)
    return dict(sorted(merged.items()))


@click.command("getting-started", help="Show an orientation guide with sample prompts")
@click.pass_context
def getting_started_cmd(ctx: click.Context) -> None:
    """Print a getting-started guide tailored to the user's granted scopes
    and tool-usage history."""
    env: str = ctx.obj["env"]
    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
    is_json = fmt == "json"

    if not is_authenticated(env):
        if is_json:
            print_agent_json(
                {
                    "authenticated": False,
                    "message": "Log in first with: ramp auth login",
                    "environment": env,
                },
                pagination=None,
            )
        else:
            click.echo(
                "\n  You're not logged in yet.  Start here:\n\n"
                "    ramp auth login\n\n"
                "  Then run 'ramp getting-started' again.\n"
            )
        return

    scopes = get_granted_scopes(env)
    categories = _remap_categories(list_categories(env))
    print_getting_started(
        env=env,
        scopes=scopes,
        categories=categories,
        is_json=is_json,
    )
