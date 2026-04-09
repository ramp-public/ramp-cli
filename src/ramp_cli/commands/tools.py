"""Manage agent-tool specifications."""

from __future__ import annotations

import click
import httpx

from ramp_cli.output.formatter import print_agent_json, resolve_format
from ramp_cli.output.help import BoxHelpFormatter
from ramp_cli.specs.sync import fetch_spec, maybe_sync
from ramp_cli.tools.registry import list_categories, reload


@click.group("tools", help="Manage agent-tool specifications")
def tools_group() -> None:
    pass


@tools_group.command("refresh", help="Fetch the latest agent-tool spec from the server")
@click.pass_context
def tools_refresh(ctx: click.Context) -> None:
    env: str = ctx.obj["env"]
    try:
        count = fetch_spec(env)
        reload(env)
    except httpx.HTTPStatusError as e:
        raise click.ClickException(
            f"Failed to fetch spec: server returned {e.response.status_code}"
        )
    except httpx.HTTPError as e:
        raise click.ClickException(f"Failed to fetch spec: {e}")
    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
    if fmt == "json":
        print_agent_json(
            {"refreshed": True, "env": env, "tool_count": count},
            pagination=None,
        )
    else:
        click.echo(f"Refreshed agent-tool spec for {env} ({count} tools)")


@tools_group.command("list", help="List all available agent tools")
@click.pass_context
def tools_list(ctx: click.Context) -> None:
    env: str = ctx.obj["env"]
    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])

    # Opportunistically sync before listing so the user sees the latest tools.
    maybe_sync(env)

    categories = list_categories(env)

    if fmt == "json":
        print_agent_json(
            [
                {"name": t.name, "category": cat, "description": t.description}
                for cat, tools in sorted(categories.items())
                for t in tools
            ],
            pagination=None,
        )
        return

    BoxHelpFormatter._suppress_wave = True
    try:
        formatter = BoxHelpFormatter()
        total = 0
        for cat, tools in sorted(categories.items()):
            dl_rows = [(t.alias or t.name, t.description or "") for t in tools]
            total += len(dl_rows)
            with formatter.section(cat.replace("_", " ").title()):
                formatter.write_dl(dl_rows)
        click.echo(formatter.getvalue(), nl=False)
    finally:
        BoxHelpFormatter._suppress_wave = False
    click.echo(f"\n  {total} tools across {len(categories)} categories")
