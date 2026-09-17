"""ramp auth login/logout/status commands."""

from __future__ import annotations

import os
import sys

import click
import httpx

from ramp_cli.animations.nyc import show_nyc
from ramp_cli.auth import store
from ramp_cli.auth.client_credentials import login as do_client_credentials_login
from ramp_cli.auth.environment import (
    environment_auth_required_message,
    missing_required_environment_auth,
)
from ramp_cli.auth.memberships import (
    fetch_business_memberships,
    fetch_token_info,
    summarize_memberships,
)
from ramp_cli.auth.oauth import LoginOptions, OAuthTokenError
from ramp_cli.auth.oauth import login as do_login
from ramp_cli.config import profiles, settings
from ramp_cli.config.constants import base_url, iter_environments
from ramp_cli.errors import EnvironmentAuthRequiredError
from ramp_cli.onboarding import is_first_login, record_first_login, show_welcome
from ramp_cli.output.formatter import print_agent_json, resolve_format
from ramp_cli.output.style import SUCCESS_GREEN, env_label, show_status_box
from ramp_cli.specs.sync import fetch_spec

CLIENT_SECRET_ENV_VAR = "RAMP_CLIENT_SECRET"


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


def _token_state(
    cfg: settings.Config, env: str, profile: str | None = None
) -> store.TokenState:
    if profile is not None:
        return store.get_token_state(env, profile=profile)
    env_config = settings.get_env_config(cfg, env)
    return store.TokenState(
        access_token=env_config.access_token,
        refresh_token=env_config.refresh_token,
        access_token_issued_at=env_config.access_token_issued_at,
        access_token_expires_in=env_config.access_token_expires_in,
        refresh_token_issued_at=env_config.refresh_token_issued_at,
        refresh_token_expires_in=env_config.refresh_token_expires_in,
    )


def _is_authenticated(
    cfg: settings.Config, env: str, profile: str | None = None
) -> bool:
    return _token_state(cfg, env, profile).is_authenticated()


def _granted_scopes(
    cfg: settings.Config, env: str, profile: str | None = None
) -> set[str]:
    if profile is not None:
        return store.get_granted_scopes(env, profile=profile)
    env_config = settings.get_env_config(cfg, env)
    if not env_config.granted_scopes:
        return set()
    return set(env_config.granted_scopes.split())


def _activate_profile(ctx: click.Context, profile: str) -> None:
    profiles.activate(profile)
    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
    if fmt != "json":
        click.echo(f"Active profile: {profile}")


def _with_profile(data: dict, profile: str | None) -> dict:
    if profile:
        return {**data, "profile": profile}
    return data


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
@click.option(
    "--client-id",
    help="OAuth client ID for standalone agent authentication.",
)
@click.option(
    "--client-secret",
    help=f"OAuth client secret. Prefer the {CLIENT_SECRET_ENV_VAR} environment variable.",
)
@click.pass_context
def login(
    ctx: click.Context,
    token_stdin: bool,
    no_browser: bool,
    scopes: tuple[str, ...],
    auth_level: str,
    client_id: str | None,
    client_secret: str | None,
) -> None:
    """Authenticate with Ramp."""
    env = ctx.obj["env"]
    profile = profiles.HUMAN_PROFILE
    label = env_label(env)

    if token_stdin:
        if client_id or client_secret is not None:
            raise click.UsageError(
                "--token_stdin cannot be used with --client-id or --client-secret."
            )
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
            profile=profile,
        )
        _activate_profile(ctx, profile)
        fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
        if fmt == "json":
            print_agent_json(
                _with_profile(
                    {
                        "message": f"Token saved for {label}.",
                        "environment": env,
                    },
                    profile,
                ),
                pagination=None,
            )
        else:
            click.echo(f"Token saved for {label}.")
            _show_default_env_hint(env)
        return

    if client_secret is not None and not client_id:
        raise click.UsageError("--client-secret requires --client-id.")

    if client_id:
        login_standalone_agent(
            ctx,
            env=env,
            label=label,
            client_id=client_id,
            client_secret=client_secret,
            no_browser=no_browser,
            scopes=scopes,
            auth_level=auth_level,
        )
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
        profile=profile,
    )
    _activate_profile(ctx, profile)

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
        (env_label(env.name), _is_authenticated(cfg, env.name, profile))
        for env in iter_environments()
    ]
    show_status_box(envs)

    if first_time:
        show_welcome(env)
    else:
        _show_post_login_hints(env)
        click.echo()


def login_standalone_agent(
    ctx: click.Context,
    *,
    env: str,
    label: str,
    client_id: str,
    client_secret: str | None,
    no_browser: bool,
    scopes: tuple[str, ...],
    auth_level: str,
) -> None:
    if no_browser:
        raise click.UsageError("--no_browser cannot be used with --client-id.")
    if auth_level != "auto":
        raise click.UsageError("--auth-level cannot be used with --client-id.")
    if missing_required_environment_auth(env):
        raise EnvironmentAuthRequiredError(environment_auth_required_message(env))

    env_secret = os.environ.get(CLIENT_SECRET_ENV_VAR)
    if client_secret is not None and env_secret is not None:
        click.echo(
            f"Warning: --client-secret overrides {CLIENT_SECRET_ENV_VAR}.",
            err=True,
        )
    resolved_secret = client_secret if client_secret is not None else env_secret
    if not resolved_secret:
        raise click.UsageError(
            f"--client-id requires --client-secret or {CLIENT_SECRET_ENV_VAR}."
        )

    try:
        token_resp = do_client_credentials_login(
            env,
            client_id=client_id,
            client_secret=resolved_secret,
            scopes=scopes,
        )
    except OAuthTokenError as exc:
        raise click.ClickException(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise click.ClickException(f"Token request failed: {exc}") from exc

    store.save_tokens(
        env,
        token_resp.access_token,
        "",
        access_token_expires_in=token_resp.expires_in,
        refresh_token_expires_in=0,
        granted_scopes=token_resp.scope,
        agent_key_uuid="",
        clear_granted_scopes=not bool(token_resp.scope),
        profile=profiles.AGENT_PROFILE,
    )
    _activate_profile(ctx, profiles.AGENT_PROFILE)

    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
    if fmt == "json":
        print_agent_json(
            _with_profile(
                {
                    "message": f"Authenticated standalone agent for {label}.",
                    "environment": env,
                    "scopes": sorted(set(token_resp.scope.split())),
                    "expires_in": token_resp.expires_in,
                },
                profiles.AGENT_PROFILE,
            ),
            pagination=None,
        )
        return

    click.secho("\u2713 Standalone agent authenticated", fg=SUCCESS_GREEN, bold=True)


@auth_group.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show current authentication state."""

    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
    cfg = settings.load()
    profile = ctx.obj["profile"]

    if fmt == "json":
        data = {
            env.name: _with_profile(
                {
                    "authenticated": _is_authenticated(cfg, env.name, profile),
                    "base_url": base_url(env.name),
                    "scopes": sorted(_granted_scopes(cfg, env.name, profile)),
                },
                profile,
            )
            for env in iter_environments()
        }
        print_agent_json(data, pagination=None)
    else:
        if profile:
            click.echo(f"  Profile: {profile}")
        envs = [
            (env_label(env.name), _is_authenticated(cfg, env.name, profile))
            for env in iter_environments()
        ]
        show_status_box(envs)

        # Show scope warnings for authenticated environments
        current_env = ctx.obj["env"]
        if _is_authenticated(cfg, current_env, profile):
            scopes = _granted_scopes(cfg, current_env, profile)
            if not scopes:
                click.echo(
                    f"\n  ⚠  No scopes on your {env_label(current_env)} token."
                    "\n     Tools will fail. Log in again:  ramp auth login",
                    err=True,
                )
            else:
                click.echo(f"  Scopes: {len(scopes)} granted", err=True)
                click.echo(
                    "  Tip: ramp auth businesses — list memberships when you "
                    "belong to multiple businesses",
                    err=True,
                )


@auth_group.command("businesses")
@click.pass_context
def businesses(ctx: click.Context) -> None:
    """List Ramp businesses your account can access in this environment."""

    env = ctx.obj["env"]
    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])

    if not _is_authenticated(settings.load(), env):
        raise click.ClickException(
            f"Not authenticated for {env_label(env)}. Run: ramp auth login"
        )

    token_info = fetch_token_info(env)
    active_business_id = str(token_info.get("business_id") or "")
    memberships = summarize_memberships(
        fetch_business_memberships(env), active_business_id
    )

    if not memberships:
        raise click.ClickException(
            "Could not load business memberships. Ensure your token includes "
            "users:read and try: ramp users me --agent"
        )

    if fmt == "json":
        print_agent_json(
            {
                "environment": env,
                "active_business_id": active_business_id or None,
                "membership_count": len(memberships),
                "memberships": memberships,
                "switch_hint": (
                    "Re-run ramp auth login and select the target business in "
                    "the OAuth UI to switch the active session."
                ),
            },
            pagination=None,
        )
        return

    click.echo(f"Business memberships for {env_label(env)}:")
    for row in memberships:
        marker = " (active session)" if row["is_active_session"] else ""
        click.echo(
            f"  • {row['name'] or row['email']} — {row['business_id']}{marker}"
        )
    if len(memberships) > 1:
        click.echo(
            "\n  To switch businesses, run ramp auth login and select the "
            "target business in the OAuth UI."
        )


def logout_profile(ctx: click.Context, profile: str | None) -> None:
    """Clear stored credentials for a profile."""
    env = ctx.obj["env"]
    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])

    if not store.has_tokens(env, profile=profile):
        if fmt == "json":
            print_agent_json(
                _with_profile(
                    {
                        "message": f"No credentials stored for {env_label(env)}.",
                        "environment": env,
                    },
                    profile,
                ),
                pagination=None,
            )
        else:
            click.echo(f"No credentials stored for {env_label(env)}.")
        return

    store.clear_tokens(env, profile=profile)
    if fmt == "json":
        print_agent_json(
            _with_profile(
                {
                    "message": f"Logged out of {env_label(env)}.",
                    "environment": env,
                },
                profile,
            ),
            pagination=None,
        )
    else:
        click.echo(f"Logged out of {env_label(env)}.")


# TODO: Remove `ramp auth logout` after callers migrate to `ramp agent logout`.
@auth_group.command()
@click.pass_context
def logout(ctx: click.Context) -> None:
    """Clear stored credentials."""
    logout_profile(ctx, ctx.obj["profile"])
