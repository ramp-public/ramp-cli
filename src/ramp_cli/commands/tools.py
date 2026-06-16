"""Manage agent-tool specifications."""

from __future__ import annotations

import click
import httpx

from ramp_cli.config.constants import environment_cache_key
from ramp_cli.output.formatter import print_agent_json, print_json, resolve_format
from ramp_cli.output.help import BoxHelpFormatter
from ramp_cli.specs import AGENT_TOOL_DEFINITION
from ramp_cli.specs.sync import fetch_spec, maybe_sync
from ramp_cli.tools.commands import resolve_tool_command_names
from ramp_cli.tools.parser import load_component_schema
from ramp_cli.tools.registry import list_categories, list_tool_defs, reload


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
                {
                    "name": name,
                    "category": cat,
                    "description": tool.description,
                }
                for cat, tools in sorted(categories.items())
                for name, tool in resolve_tool_command_names(cat, tools)
            ],
            pagination=None,
        )
        return

    BoxHelpFormatter._suppress_wave = True
    try:
        formatter = BoxHelpFormatter()
        total = 0
        for cat, tools in sorted(categories.items()):
            dl_rows = [
                (name, tool.description or "")
                for name, tool in resolve_tool_command_names(cat, tools)
            ]
            total += len(dl_rows)
            with formatter.section(cat.replace("_", " ").title()):
                formatter.write_dl(dl_rows)
        click.echo(formatter.getvalue(), nl=False)
    finally:
        BoxHelpFormatter._suppress_wave = False
    click.echo(f"\n  {total} tools across {len(categories)} categories")


@tools_group.command("schema", help="Show a generated tool's request schema")
@click.argument("category")
@click.argument("tool_name")
@click.pass_context
def tools_schema(ctx: click.Context, category: str, tool_name: str) -> None:
    """Print the OpenAPI request schema for CATEGORY TOOL_NAME."""
    env: str = ctx.obj["env"]
    maybe_sync(env, force=True)

    category_tools = [
        candidate for candidate in list_tool_defs(env) if candidate.category == category
    ]
    named_tools = resolve_tool_command_names(category, category_tools)
    tool = next(
        (candidate for name, candidate in named_tools if name == tool_name),
        None,
    )
    if tool is None:
        alias_matches = [
            name for name, candidate in named_tools if candidate.alias == tool_name
        ]
        if len(alias_matches) > 1:
            choices = ", ".join(sorted(alias_matches))
            raise click.UsageError(
                f"Ambiguous generated tool '{category} {tool_name}'. "
                f"Use one of: {choices}."
            )
        raise click.UsageError(f"Unknown generated tool '{category} {tool_name}'.")
    if not tool.request_schema_name:
        raise click.UsageError(
            f"Tool '{category} {tool_name}' does not accept a request body."
        )

    spec_path = AGENT_TOOL_DEFINITION.resolve(environment_cache_key(env))
    schema = load_component_schema(spec_path, tool.request_schema_name)
    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
    if fmt == "json":
        print_agent_json(schema, pagination={"has_more": False, "next": None})
    else:
        print_json(schema)
