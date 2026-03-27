"""ramp applications commands."""

from __future__ import annotations

import json
import sys
from typing import Any

import click
import httpx
import jsonref

from ramp_cli.client.api import RampClient
from ramp_cli.config.constants import application_signup_token, base_url
from ramp_cli.output.formatter import print_agent_json, print_json, resolve_format

_DEVELOPER_API_SPEC_URL = "https://docs.ramp.com/openapi/developer-api.json"

APPLICATION_CREATED_MESSAGE = (
    "An email has been sent to the applicant email to sign up for Ramp and continue the application. "
    "If the email already exists in Ramp, the email will contain instructions to "
    "sign in or continue the application, which will be unaffected by this request."
)

_APPLICATIONS_API_PATH = "/developer/v1/applications"

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
            "ssn_last_4": "5678",
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
        "phone": "+14155550124",
        "ssn_last_4": "1234",
        "title": "Owner",
    },
    "financial_details": {
        "estimated_monthly_ap_spend_amount": 25000,
        "estimated_monthly_spend_amount": 50000,
    },
    "oauth_authorize_params": {
        "redirect_uri": "https://partner.example.com/oauth/callback",
        "state": "abc123",
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


def _render_dry_run(
    env: str, body: dict[str, Any], format_flag: str | None, config_format: str
) -> None:
    fmt = resolve_format(format_flag, config_format)
    url = f"{base_url(env)}{_APPLICATIONS_API_PATH}"

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


@click.group("applications", help="Apply for a Ramp account")
def applications_group() -> None:
    pass


@applications_group.command("create")
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
@click.pass_context
def create_application(
    ctx: click.Context, json_body: str | None, dry_run: bool, example: bool
) -> None:
    """Create a financing application.

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

    if dry_run:
        _render_dry_run(env, body, ctx.obj["format"], ctx.obj["config_format"])
        return

    client = RampClient(env, access_token=application_signup_token(env))
    client.post(_APPLICATIONS_API_PATH, json.dumps(body).encode())
    _render_success_message(ctx.obj["format"], ctx.obj["config_format"])


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
