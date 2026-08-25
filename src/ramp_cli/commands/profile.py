"""Select the credential profile used by Ramp CLI commands."""

from __future__ import annotations

import os

import click

from ramp_cli.auth import store
from ramp_cli.config import profiles
from ramp_cli.output.formatter import print_agent_json, resolve_format


@click.command("profile", help="Show, list, or switch credential profiles")
@click.argument("name", required=False, metavar="[human|agent|list]")
@click.pass_context
def profile_cmd(ctx: click.Context, name: str | None) -> None:
    """Show the active profile, list profiles, or switch to NAME."""
    if name == "list":
        _list_profiles(ctx)
        return

    if name is None:
        _show_current(ctx)
        return

    try:
        profiles.activate(name)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="'NAME'") from exc

    effective = os.environ.get("RAMP_PROFILE")
    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
    if fmt == "json":
        print_agent_json({"profile": name}, pagination=None)
        return
    if effective:
        click.echo(
            f"Active profile set to {name!r}. RAMP_PROFILE={effective!r} "
            "still overrides it.",
            err=True,
        )
    else:
        click.echo(f"Switched to profile {name!r}.")


def _show_current(ctx: click.Context) -> None:
    name = ctx.obj.get("profile")
    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
    if fmt == "json":
        print_agent_json({"profile": name}, pagination=None)
    elif name is None:
        click.echo("No active profile. Existing credentials remain in use.")
    else:
        click.echo(name)


def _list_profiles(ctx: click.Context) -> None:
    env = ctx.obj["env"]
    active = ctx.obj.get("profile")
    names = list(profiles.BUILTIN_PROFILES)
    records = list(
        {
            "profile": name,
            "active": active == name,
            "authenticated": store.is_authenticated(env, profile=name),
            "environment": env,
        }
        for name in sorted(names)
    )

    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
    if fmt == "json":
        print_agent_json(records, pagination=None)
        return

    for record in records:
        marker = "*" if record["active"] else " "
        status = "authenticated" if record["authenticated"] else "not authenticated"
        click.echo(f"{marker} {record['profile']:<24} {status}")
