"""ramp applications commands."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import click
import httpx
import jsonref

from ramp_cli.auth import store
from ramp_cli.auth.oauth import (
    OAuthTokenError,
    PkceCallback,
    TokenResponse,
    exchange_pkce_callback_code,
    start_pkce_callback,
)
from ramp_cli.client.api import RampClient
from ramp_cli.config.constants import api_url, application_signup_token
from ramp_cli.output.formatter import print_agent_json, print_json, resolve_format
from ramp_cli.tools.commands import GeneratedToolGroup
from ramp_cli.version_check import suppress_next_update_notice

_DEVELOPER_API_SPEC_URL = "https://docs.ramp.com/openapi/developer-api.json"

APPLICATION_CREATED_MESSAGE = (
    "An email has been sent to the applicant email to sign up for Ramp and continue the application. "
    "If the email already exists in Ramp, the email will contain instructions to "
    "sign in or continue the application, which will be unaffected by this request."
)

_APPLICATIONS_API_PATH = "/developer/v1/applications"
_APPLICATION_AUTH_FALLBACK_SCOPES = (
    "applications:read",
    "applications:write",
    "bank_accounts:read",
    "incorporation:read",
    "incorporation:write",
)
_APPLICATION_INVITE_LINK_KEYS = (
    "invite_link",
    "invite_url",
    "application_url",
    "signup_url",
)

APPLICATION_EXAMPLE: dict[str, Any] = {
    "applicant": {
        "email": "jane@acmeplumbing.com",
        "first_name": "Jane",
        "last_name": "Doe",
        "phone": "+14155550124",
    },
    "beneficial_owners": [
        {
            "address": {
                "city": "San Francisco",
                "country": "US",
                "postal_code": "94104",
                "state": "CA",
                "street_address": "200 Pine St",
            },
            "birth_date": "1982-11-04",
            "email": "john@acmeplumbing.com",
            "first_name": "John",
            "last_name": "Smith",
            "phone": "+14155550125",
            "ownership_percentage": 40,
            "ssn_last_4": None,
            "title": "Co-Founder",
        }
    ],
    "business": {
        "address": {
            "apt_suite": "Suite 500",
            "city": "San Francisco",
            "postal_code": "94105",
            "state": "CA",
            "street_address": "123 Market St",
        },
        "business_description": "Residential and commercial plumbing services",
        "business_name_dba": None,
        "business_name_legal": "Acme Plumbing LLC",
        "business_name_on_card": None,
        "business_website": "https://acmeplumbing.com",
        "incorporation": {
            "date_of_incorporation": "2018-06-15",
            "ein_number": "12-3456789",
            "entity_type": "LLC",
            "state_of_incorporation": "CA",
        },
        "phone": "+14155550123",
    },
    "controlling_officer": {
        "address": {
            "city": "San Francisco",
            "country": "US",
            "postal_code": "94105",
            "state": "CA",
            "street_address": "123 Market St",
        },
        "birth_date": "1985-03-12",
        "email": "jane@acmeplumbing.com",
        "first_name": "Jane",
        "is_beneficial_owner": True,
        "last_name": "Doe",
        "ownership_percentage": 60,
        "phone": "+14155550124",
        "ssn_last_4": None,
        "title": "Owner",
    },
    "financial_details": {
        "estimated_monthly_ap_spend_amount": 25000,
        "estimated_monthly_spend_amount": 50000,
    },
    "oauth_authorize_params": {
        "redirect_uri": "https://partner.example.com/oauth/callback",
        "state": "abc123",
        "code_challenge": "base64url-encoded-sha256-pkce-challenge",
    },
}


def _parse_json_body(raw_json: str) -> dict[str, Any]:
    try:
        body = json.loads(raw_json)
    except json.JSONDecodeError as e:
        raise click.BadParameter(f"invalid JSON: {e}", param_hint="'--json'") from e

    if not isinstance(body, dict):
        raise click.BadParameter(
            "application body must be a JSON object", param_hint="'--json'"
        )

    return body


def _render_success_message(format_flag: str | None, config_format: str) -> None:
    fmt = resolve_format(format_flag, config_format)
    if fmt == "json":
        print_agent_json(
            {"message": APPLICATION_CREATED_MESSAGE},
            pagination={"has_more": False, "next": None},
        )
        return

    # Creates do not expose a stable human-readable response body, so every
    # non-JSON format intentionally collapses to the same fixed success message.
    click.echo(APPLICATION_CREATED_MESSAGE)


def _manual_auth_fallback_command(env: str) -> str:
    scope_flags = " ".join(
        f"--scope {scope}" for scope in _APPLICATION_AUTH_FALLBACK_SCOPES
    )
    return f"ramp --env {env} auth login --auth-level business {scope_flags}"


def _agent_handoff_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "data": [payload],
        "pagination": {"has_more": False, "next": None},
    }


def _write_agent_handoff_result(payload: dict[str, Any], stream: Any) -> None:
    json.dump(_agent_handoff_envelope(payload), stream, indent=2, default=str)
    stream.write("\n")
    stream.flush()


def _redirect_stdout_to_devnull() -> None:
    try:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    except OSError:
        pass


def _render_handoff_result(
    env: str,
    authenticated: bool,
    auth_error: str | None,
    format_flag: str | None,
    config_format: str,
    *,
    invite_link: str | None = None,
    interrupted: bool = False,
) -> None:
    fmt = resolve_format(format_flag, config_format)
    fallback_command = None if authenticated else _manual_auth_fallback_command(env)
    message = APPLICATION_CREATED_MESSAGE
    if authenticated:
        message += " Ramp credentials were saved for this environment."
    elif interrupted:
        message += " CLI authentication was interrupted."
        if invite_link:
            message += " Use the invite link to continue the application."
        message += " Use the fallback command if credentials are still needed."
    else:
        message += " CLI authentication was not completed; use the fallback command."

    payload = {
        "message": message,
        "environment": env,
        "authenticated": authenticated,
        "fallback_command": fallback_command,
    }
    if invite_link:
        payload["invite_link"] = invite_link
    if interrupted:
        payload["interrupted"] = True
    if auth_error:
        payload["auth_error"] = auth_error

    if fmt == "json":
        try:
            print_agent_json(payload, pagination={"has_more": False, "next": None})
        except BrokenPipeError:
            if not interrupted:
                raise
            _write_agent_handoff_result(payload, sys.stderr)
            _redirect_stdout_to_devnull()
        return

    err = interrupted
    click.echo(APPLICATION_CREATED_MESSAGE, err=err)
    if invite_link:
        click.echo(f"Invite link: {invite_link}", err=err)
    if authenticated:
        click.echo("Ramp credentials were saved for this environment.", err=err)
        return

    if auth_error:
        click.echo(f"CLI authentication was not completed: {auth_error}", err=True)
    click.echo("Manual scoped auth fallback:", err=True)
    click.echo(f"  {fallback_command}", err=True)


def _application_create_response_payload(response: bytes) -> dict[str, Any] | None:
    if not response.strip():
        return None

    try:
        payload = json.loads(response)
    except json.JSONDecodeError:
        return None

    return payload if isinstance(payload, dict) else None


def _extract_application_invite_link(response: bytes) -> str | None:
    payload = _application_create_response_payload(response)
    if not payload:
        return None

    for key in _APPLICATION_INVITE_LINK_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value

    nested = payload.get("data")
    if isinstance(nested, dict):
        for key in _APPLICATION_INVITE_LINK_KEYS:
            value = nested.get(key)
            if isinstance(value, str) and value:
                return value

    return None


def _application_create_access_token(env: str) -> str:
    return os.environ.get("RAMP_ACCESS_TOKEN") or application_signup_token(env)


def _render_dry_run(
    env: str, body: dict[str, Any], format_flag: str | None, config_format: str
) -> None:
    fmt = resolve_format(format_flag, config_format)
    url = api_url(env, _APPLICATIONS_API_PATH)

    if fmt == "json":
        print_agent_json(
            {
                "dry_run": True,
                "method": "POST",
                "url": url,
                "body": body,
            },
            pagination={"has_more": False, "next": None},
        )
        return

    click.echo(f"DRY RUN: POST {url}", err=True)
    print_json(body)


def _inject_pkce_authorize_params(body: dict[str, Any], callback: PkceCallback) -> None:
    params = body.setdefault("oauth_authorize_params", {})
    if not isinstance(params, dict):
        raise click.BadParameter(
            "oauth_authorize_params must be a JSON object when --wait_for_auth is used",
            param_hint="'--json'",
        )

    params.update(
        {
            "redirect_uri": callback.redirect_uri,
            "state": callback.state,
            "code_challenge": callback.code_challenge,
        }
    )


def _save_token_response(env: str, token_resp: TokenResponse) -> None:
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


@click.group(
    "applications",
    cls=GeneratedToolGroup,
    tool_category="applications",
    help="Start a new application or continue the authenticated applicant's current one",
)
def applications_group() -> None:
    pass


@applications_group.command(
    "create",
    short_help="Start a new financing application (signup flow)",
)
@click.option(
    "--json",
    "json_body",
    required=False,
    default=None,
    help="Raw JSON body (see --example for the expected schema)",
)
@click.option(
    "--dry_run",
    "-n",
    is_flag=True,
    default=False,
    show_default=False,
    help="Print request without sending it",
)
@click.option(
    "--example",
    is_flag=True,
    default=False,
    help="Print a full example JSON payload and exit",
)
@click.option(
    "--wait_for_auth",
    "--wait-for-auth",
    is_flag=True,
    default=False,
    help=(
        "Inject localhost PKCE OAuth params, then wait for invite acceptance "
        "and save the resulting credentials."
    ),
)
@click.option(
    "--auth_timeout",
    "--auth-timeout",
    type=click.IntRange(1),
    default=900,
    show_default=True,
    help="Seconds to wait for the OAuth redirect when --wait_for_auth is used.",
)
@click.pass_context
def create_application(
    ctx: click.Context,
    json_body: str | None,
    dry_run: bool,
    example: bool,
    wait_for_auth: bool,
    auth_timeout: int,
) -> None:
    """Start a new financing application.

    This starts the signup flow. To continue an existing authenticated
    application, begin with `ramp applications progress`.
    Use --example to see the full JSON schema.
    """
    if example:
        fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
        if fmt == "json":
            print_agent_json(
                APPLICATION_EXAMPLE,
                pagination={"has_more": False, "next": None},
            )
        else:
            print_json(APPLICATION_EXAMPLE)
        return

    if not json_body:
        raise click.UsageError(
            "Missing option '--json'. Use --example to see the expected schema."
        )

    env = ctx.obj["env"]
    body = _parse_json_body(json_body)
    callback = None
    invite_link = None

    if dry_run and wait_for_auth:
        raise click.UsageError("--wait_for_auth cannot be used with --dry_run")

    try:
        if wait_for_auth:
            callback = start_pkce_callback()
            _inject_pkce_authorize_params(body, callback)

        if dry_run:
            _render_dry_run(env, body, ctx.obj["format"], ctx.obj["config_format"])
            return

        client = RampClient(env, access_token=_application_create_access_token(env))
        response = client.post(_APPLICATIONS_API_PATH, json.dumps(body).encode())
        invite_link = _extract_application_invite_link(response)
        if not callback:
            _render_success_message(ctx.obj["format"], ctx.obj["config_format"])
            return

        fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
        if fmt != "json":
            click.echo(
                "Waiting for the applicant to accept the invite in their browser...",
                err=True,
            )
        authenticated = False
        auth_error = None
        try:
            code = callback.wait_for_code(
                auth_timeout,
                timeout_message=f"OAuth redirect timed out after {auth_timeout} seconds",
            )
            token_resp = exchange_pkce_callback_code(env, callback, code)
            _save_token_response(env, token_resp)
            authenticated = True
        except KeyboardInterrupt:
            suppress_next_update_notice()
            _render_handoff_result(
                env,
                False,
                "Interrupted by user",
                ctx.obj["format"],
                ctx.obj["config_format"],
                invite_link=invite_link,
                interrupted=True,
            )
            if callback:
                try:
                    callback.shutdown()
                except KeyboardInterrupt:
                    pass
                callback = None
            sys.exit(130)
        except (click.ClickException, OAuthTokenError, httpx.HTTPError, OSError) as exc:
            auth_error = str(exc)

        _render_handoff_result(
            env,
            authenticated,
            auth_error,
            ctx.obj["format"],
            ctx.obj["config_format"],
            invite_link=invite_link,
        )
    finally:
        if callback:
            callback.shutdown()


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Merge *source* into *target* in-place, combining dicts and extending lists."""
    for key, value in source.items():
        if key in target:
            if isinstance(target[key], dict) and isinstance(value, dict):
                _deep_merge(target[key], value)
            elif isinstance(target[key], list) and isinstance(value, list):
                target[key] = target[key] + value
            else:
                target[key] = value
        else:
            target[key] = value


def _merge_all_of(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge allOf arrays in a resolved JSON Schema."""
    if "allOf" in schema:
        merged: dict[str, Any] = {}
        for item in schema["allOf"]:
            resolved_item = _merge_all_of(item)
            _deep_merge(merged, resolved_item)
        for k, v in schema.items():
            if k != "allOf":
                merged[k] = v
        return _merge_all_of(merged)

    result = dict(schema)

    if "properties" in result:
        result["properties"] = {
            k: _merge_all_of(v) for k, v in result["properties"].items()
        }

    if "items" in result and isinstance(result["items"], dict):
        result["items"] = _merge_all_of(result["items"])

    return result


def _fetch_application_schema() -> dict[str, Any]:
    """Fetch the Developer API spec and extract the resolved applications create schema."""
    resp = httpx.get(_DEVELOPER_API_SPEC_URL, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    spec = resp.json()

    resolved_spec = jsonref.replace_refs(spec, proxies=False)
    schema = resolved_spec["paths"]["/developer/v1/applications"]["post"][
        "requestBody"
    ]["content"]["application/json"]["schema"]
    schema = _merge_all_of(schema)
    schema.pop("example", None)
    return schema


@applications_group.command("schema")
@click.pass_context
def schema_cmd(ctx: click.Context) -> None:
    """Print the JSON schema for the applications create request body."""
    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])

    try:
        schema = _fetch_application_schema()
    except Exception as exc:
        if fmt == "json":
            print_agent_json(
                {"error": "schema_fetch_failed", "url": _DEVELOPER_API_SPEC_URL},
                pagination={"has_more": False, "next": None},
            )
        else:
            click.echo(
                f"Failed to fetch schema: {exc}\nURL: {_DEVELOPER_API_SPEC_URL}",
                err=True,
            )
        sys.exit(1)

    if fmt == "json":
        print_agent_json(schema, pagination={"has_more": False, "next": None})
    else:
        print_json(schema)
