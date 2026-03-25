"""ramp config set/get/list/path commands."""

from __future__ import annotations

import os

import click

from ramp_cli.config import settings
from ramp_cli.config.constants import ENV_PRODUCTION, ENV_SANDBOX, client_id
from ramp_cli.output.formatter import print_agent_json, print_table, resolve_format


@click.group("config", help="Manage CLI configuration")
def config_group() -> None:
    pass


@config_group.command("set")
@click.argument("key")
@click.argument("value")
@click.pass_context
def config_set(ctx: click.Context, key: str, value: str) -> None:
    """Set a configuration value."""
    cfg = settings.load()

    if key == "environment":
        if value == "prod":
            value = "production"
        if value not in ("sandbox", "production"):
            raise click.BadParameter(
                f"Invalid environment {value!r} — use 'sandbox' or 'production'"
            )
        cfg.environment = value
    elif key == "format":
        if value == "auto":
            cfg.format = ""
        elif value not in ("json", "table"):
            raise click.BadParameter(
                f"Invalid format {value!r} — use 'json', 'table', or 'auto'"
            )
        else:
            cfg.format = value
    else:
        raise click.BadParameter(
            f"Unknown config key {key!r} — supported: environment, format"
        )

    settings.save(cfg)

    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
    if fmt == "json":
        print_agent_json({"key": key, "value": value}, pagination=None)
        return

    click.echo(f"Set {key} = {value}")


@config_group.command("get")
@click.argument("key")
@click.pass_context
def config_get(ctx: click.Context, key: str) -> None:
    """Get a configuration value."""
    env = ctx.obj["env"]
    cfg = settings.load()

    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])

    if key == "environment":
        display_value = cfg.environment or "sandbox (default)"
    elif key == "format":
        display_value = cfg.format or "(auto-detect)"
    elif key == "client_id":
        display_value = f"{client_id(env)} ({env})"
    else:
        raise click.BadParameter(f"Unknown config key {key!r}")

    if fmt == "json":
        print_agent_json({"key": key, "value": display_value}, pagination=None)
    else:
        click.echo(display_value)


@config_group.command("list")
@click.pass_context
def config_list(ctx: click.Context) -> None:
    """Show all configuration values with sources."""
    flag_env = ctx.obj.get("flag_env", "")
    flag_format = ctx.obj.get("format", "")
    cfg = settings.load()

    rows: list[tuple[str, str, str]] = []

    # environment
    val, src = _resolve_with_source(
        flag_env, "RAMP_ENVIRONMENT", cfg.environment, "sandbox"
    )
    rows.append(("environment", val, src))

    # format
    val, src = _resolve_with_source(flag_format, "", cfg.format, "(auto-detect)")
    rows.append(("format", val, src))

    # Client IDs (read-only, from constants)
    for e in (ENV_SANDBOX, ENV_PRODUCTION):
        rows.append((f"client_id ({e})", _mask(client_id(e)), "built-in"))

    rows.append(("config_path", str(settings.config_path()), "-"))

    headers = ["key", "value", "source"]
    table_rows = [{"key": r[0], "value": r[1], "source": r[2]} for r in rows]

    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
    if fmt == "json":
        print_agent_json(table_rows, pagination=None)
        return

    print_table(headers, table_rows)


@config_group.command("path")
@click.pass_context
def config_path_cmd(ctx: click.Context) -> None:
    """Show the config file path."""
    path = str(settings.config_path())
    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
    if fmt == "json":
        print_agent_json({"path": path}, pagination=None)
    else:
        click.echo(path)


def _mask(val: str) -> str:
    if len(val) <= 12:
        return "***"
    return val[:8] + "..." + val[-4:]


def _resolve_with_source(
    flag_value: str, env_var_name: str, config_value: str, default: str
) -> tuple[str, str]:
    if flag_value:
        return flag_value, "flag"
    if env_var_name:
        v = os.environ.get(env_var_name, "")
        if v:
            return v, f"env ({env_var_name})"
    if config_value:
        return config_value, "config"
    return default, "default"
