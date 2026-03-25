"""ramp auth login/logout/status commands."""

from __future__ import annotations

import sys

import click

from ramp_cli.auth import store
from ramp_cli.config.constants import ENV_PRODUCTION, ENV_SANDBOX, base_url
from ramp_cli.output.formatter import print_agent_json, resolve_format
from ramp_cli.output.style import env_label, show_status_box


@click.group("auth", help="Manage authentication")
def auth_group() -> None:
    pass


def _show_default_env_hint(env: str) -> None:
    """Print a hint about setting the default environment after login."""
    click.echo(f"  Set your default environment:  ramp env {env}")


@auth_group.command()
@click.option(
    "--token_stdin",
    is_flag=True,
    default=False,
    help="Read access token from stdin (skip OAuth). Usage: echo $TOKEN | ramp auth login --token_stdin",
)
@click.option(
    "--no_browser",
    is_flag=True,
    default=False,
    help="Print login URL instead of opening browser",
)
@click.pass_context
def login(ctx: click.Context, token_stdin: bool, no_browser: bool) -> None:
    """Authenticate with Ramp via browser."""
    from ramp_cli.auth.oauth import LoginOptions
    from ramp_cli.auth.oauth import login as do_login

    env = ctx.obj["env"]
    label = env_label(env)

    if token_stdin:
        token = sys.stdin.readline().strip()
        if not token:
            raise click.UsageError("No token provided on stdin.")
        store.save_tokens(env, token, "")
        fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
        if fmt == "json":
            print_agent_json(
                {"message": f"Token saved for {label}.", "environment": env},
                pagination=None,
            )
        else:
            click.echo(f"Token saved for {label}.")
            _show_default_env_hint(env)
        return

    click.echo(f"Logging into {label}...", err=True)

    opts = LoginOptions(no_browser=no_browser)
    token_resp = do_login(env, opts)

    store.save_tokens(
        env,
        token_resp.access_token,
        token_resp.refresh_token,
        access_token_expires_in=token_resp.expires_in,
        refresh_token_expires_in=token_resp.refresh_token_expires_in,
    )

    from ramp_cli.animations.nyc import show_nyc

    show_nyc(duration=5.0)

    envs = [
        (env_label(e), store.is_authenticated(e)) for e in (ENV_SANDBOX, ENV_PRODUCTION)
    ]
    show_status_box(envs)
    click.echo("  Run ramp --help to explore commands.")
    _show_default_env_hint(env)
    click.echo()


@auth_group.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show current authentication state."""

    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])

    if fmt == "json":
        data = {
            env: {
                "authenticated": store.is_authenticated(env),
                "base_url": base_url(env),
            }
            for env in (ENV_SANDBOX, ENV_PRODUCTION)
        }
        print_agent_json(data, pagination=None)
    else:
        envs = [
            (env_label(e), store.is_authenticated(e))
            for e in (ENV_SANDBOX, ENV_PRODUCTION)
        ]
        show_status_box(envs)


@auth_group.command()
@click.pass_context
def logout(ctx: click.Context) -> None:
    """Clear stored credentials."""

    env = ctx.obj["env"]
    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])

    if not store.has_tokens(env):
        if fmt == "json":
            print_agent_json(
                {
                    "message": f"No credentials stored for {env_label(env)}.",
                    "environment": env,
                },
                pagination=None,
            )
        else:
            click.echo(f"No credentials stored for {env_label(env)}.")
        return

    store.clear_tokens(env)
    if fmt == "json":
        print_agent_json(
            {"message": f"Logged out of {env_label(env)}.", "environment": env},
            pagination=None,
        )
    else:
        click.echo(f"Logged out of {env_label(env)}.")
