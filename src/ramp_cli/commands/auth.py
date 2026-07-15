"""ramp auth login/logout/status commands."""

from __future__ import annotations

import sys

import click
import httpx

from ramp_cli.animations.nyc import show_nyc
from ramp_cli.auth import store
from ramp_cli.auth.environment import (
    environment_auth_required_message,
    missing_required_environment_auth,
)
from ramp_cli.auth.oauth import LoginOptions, OAuthTokenError
from ramp_cli.auth.oauth import login as do_login
from ramp_cli.config import settings
from ramp_cli.config.constants import base_url, iter_environments
from ramp_cli.errors import EnvironmentAuthRequiredError
from ramp_cli.onboarding import is_first_login, record_first_login, show_welcome
from ramp_cli.output.formatter import print_agent_json, resolve_format
from ramp_cli.output.style import env_label, show_status_box
from ramp_cli.specs.sync import fetch_spec


@click.group("auth", help="Manage authentication")
def auth_group() -> None:
    pass


def _show_default_env_hint(env: str) -> None:
    """Print a hint about setting the default environment after login."""
    click.echo(f"  Set your default environment:  ramp env {env}")


def _show_post_login_hints(env: str) -> None:
    click.echo("  Next step:  ramp funds enroll")
    click.echo("  Explore all commands:  ramp --help")
    _show_default_env_hint(env)


def _token_state(cfg: settings.Config, env: str) -> store.TokenState:
    env_config = settings.get_env_config(cfg, env)
    return store.TokenState(
        access_token=env_config.access_token,
        refresh_token=env_config.refresh_token,
        access_token_issued_at=env_config.access_token_issued_at,
        access_token_expires_in=env_config.access_token_expires_in,
        refresh_token_issued_at=env_config.refresh_token_issued_at,
        refresh_token_expires_in=env_config.refresh_token_expires_in,
    )


def _is_authenticated(cfg: settings.Config, env: str) -> bool:
    return _token_state(cfg, env).is_authenticated()


def _granted_scopes(cfg: settings.Config, env: str) -> set[str]:
    env_config = settings.get_env_config(cfg, env)
    if not env_config.granted_scopes:
        return set()
    return set(env_config.granted_scopes.split())


def _refresh_tool_spec_before_login(env: str) -> None:
    """Best-effort sync so newly deployed tool scopes are requested at login."""
    if settings.configured_scopes():
        return

    try:
        fetch_spec(env)
    except Exception:
        click.echo(
            "\n  ⚠  Could not refresh tool definitions before login."
            "\n     Using cached scopes. If newly deployed tools are missing,"
            "\n     run: ramp tools refresh && ramp auth login\n",
            err=True,
        )


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
@click.option(
    "--scope",
    "scopes",
    multiple=True,
    help="Request only this OAuth scope. Repeat for additional scopes.",
)
@click.option(
    "--auth-level",
    type=click.Choice(["auto", "business", "user"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="OAuth authorization level to request.",
)
@click.pass_context
def login(
    ctx: click.Context,
    token_stdin: bool,
    no_browser: bool,
    scopes: tuple[str, ...],
    auth_level: str,
) -> None:
    """Authenticate with Ramp via browser."""
    env = ctx.obj["env"]
    label = env_label(env)

    if token_stdin:
        if scopes or auth_level != "auto":
            raise click.UsageError(
                "--scope and --auth-level cannot be used with --token_stdin."
            )
        token = sys.stdin.readline().strip()
        if not token:
            raise click.UsageError("No token provided on stdin.")
        store.save_tokens(
            env,
            token,
            "",
            agent_key_uuid="",
            clear_granted_scopes=True,
        )
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

    if missing_required_environment_auth(env):
        raise EnvironmentAuthRequiredError(environment_auth_required_message(env))

    _refresh_tool_spec_before_login(env)

    opts = LoginOptions(
        no_browser=no_browser,
        scopes=scopes,
        auth_level=auth_level,
    )
    try:
        token_resp = do_login(env, opts)
    except OAuthTokenError as exc:
        raise click.ClickException(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise click.ClickException(f"Token request failed: {exc}") from exc

    store.save_tokens(
        env,
        token_resp.access_token,
        token_resp.refresh_token,
        access_token_expires_in=token_resp.expires_in,
        refresh_token_expires_in=token_resp.refresh_token_expires_in,
        granted_scopes=token_resp.scope,
        agent_key_uuid=token_resp.agent_key_uuid,
        clear_granted_scopes=not bool(token_resp.scope),
    )

    if not token_resp.scope:
        click.echo(
            "\n  ⚠  Your token was issued with no scopes.\n"
            "     All tool calls will fail. Try logging in again:\n\n"
            "       ramp auth login\n\n"
            "     If this keeps happening, run: ramp tools refresh\n",
            err=True,
        )

    first_time = is_first_login()
    record_first_login()

    show_nyc(duration=5.0)

    cfg = settings.load()
    envs = [
        (env_label(env.name), _is_authenticated(cfg, env.name))
        for env in iter_environments()
    ]
    show_status_box(envs)

    if first_time:
        show_welcome(env)
    else:
        _show_post_login_hints(env)
        click.echo()


@auth_group.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show current authentication state."""

    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
    cfg = settings.load()

    if fmt == "json":
        data = {
            env.name: {
                "authenticated": _is_authenticated(cfg, env.name),
                "base_url": base_url(env.name),
                "scopes": sorted(_granted_scopes(cfg, env.name)),
            }
            for env in iter_environments()
        }
        print_agent_json(data, pagination=None)
    else:
        envs = [
            (env_label(env.name), _is_authenticated(cfg, env.name))
            for env in iter_environments()
        ]
        show_status_box(envs)

        # Show scope warnings for authenticated environments
        current_env = ctx.obj["env"]
        if _is_authenticated(cfg, current_env):
            scopes = _granted_scopes(cfg, current_env)
            if not scopes:
                click.echo(
                    f"\n  ⚠  No scopes on your {env_label(current_env)} token."
                    "\n     Tools will fail. Log in again:  ramp auth login",
                    err=True,
                )
            else:
                click.echo(f"  Scopes: {len(scopes)} granted", err=True)


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
