"""ramp incorporation commands.

The ``ramp incorporation`` group provides the full 8-verb surface for Ramp
US LLC formation: states, industries, countries, applicant create/get, submit,
status, and documents.

Pre-EIN early access
--------------------
By default a financing application waits for a fully formed entity (an EIN)
before it can proceed. When pre-EIN early access is enabled, a business can
onboard once the formation has been filed with the state (submission status
``SUBMITTED``) but before the IRS has issued the EIN: the account is granted
limited access, and it auto-promotes to full access when Ramp receives the EIN
(submission status ``APPROVED``). ``ramp incorporation status`` annotates the
Ramp-facing ``formation_submission_status`` with this lifecycle so an agent
knows what access it has and what unblocks full access. See
``_pre_ein_status_note`` for the mapping.

SSN handling
------------
The CLI never collects SSN values for incorporation. Any caller-supplied SSN
field inside ``--json`` is rejected immediately with a structured error. SSN
entry belongs in the Ramp-hosted application form, not in CLI arguments,
environment variables, prompts, stdout, or stderr.
"""

from __future__ import annotations

import json
import re
from typing import Any

import click

from ramp_cli.client.api import RampClient
from ramp_cli.config.constants import api_url
from ramp_cli.output.formatter import print_agent_json, print_json, resolve_format

_INCORPORATION_BASE = "/developer/v1/incorporation"
_INCORPORATION_SUBMIT_PATH = f"{_INCORPORATION_BASE}/formation"
_INCORPORATION_STATUS_PATH = f"{_INCORPORATION_BASE}/company-status"

# Caller-supplied SSNs in --json are rejected two independent ways, so neither
# a clever key name nor an unexpected key can sneak an SSN past us:
#
#   1. Term detection (keys)  — any JSON key that references SSN is rejected.
#   2. Value-shape detection (values) — any JSON value that *looks* like an SSN
#      is rejected, regardless of the key it sits under.
#
# The CLI does not collect SSNs, so a legitimate formation body never contains
# either signal.

# A key token marks the key as SSN-related when it *starts with* "ssn" (so
# "ssn", "ssn_number", and the concatenated "ssnvalue" all match) — but we do
# NOT substring-match, so benign keys like "className" ("cla[ssn]ame") or the
# abbreviation "assn" (association) are left alone.
_SSN_TOKEN_PREFIX = "ssn"

# Value-level full SSN-shaped string: NNN-NN-NNNN or 9 consecutive digits.
# This is rejected globally. Exact 4-digit strings are only SSN-like when the
# surrounding JSON path references SSN, because normal fields such as
# non-US postal codes can be exactly four digits.
_FULL_SSN_VALUE_RE = re.compile(r"\b(?:\d{3}-\d{2}-\d{4}|\d{9})\b")
_SSN_LAST_4_RE = re.compile(r"^\d{4}$")


def _key_references_ssn(key: str) -> bool:
    """Return True if *key* names an SSN field in any common casing convention.

    The key is split into word tokens at non-alphanumeric separators *and* at
    camelCase / PascalCase / acronym boundaries, so ``memberSSN``,
    ``applicant_ssn_value``, ``socialSecurityNumber``, ``SOCIAL_SECURITY``, and
    ``member-ssn`` all tokenize cleanly.  Benign keys such as ``className``
    (-> ``["class", "name"]``) do not match because we compare whole tokens
    rather than substrings.
    """
    # Insert a separator at lowercase/digit -> uppercase transitions
    # (e.g. "memberSSN" -> "member SSN", "applicantSsn" -> "applicant Ssn")
    # and at acronym -> TitleCase transitions (e.g. "SSNValue" -> "SSN Value").
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", key)
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
    tokens = [t for t in re.split(r"[^a-zA-Z0-9]+", spaced.lower()) if t]

    if any(t.startswith(_SSN_TOKEN_PREFIX) for t in tokens):
        return True
    # "social security" as adjacent tokens, or collapsed into one token
    # ("socialsecurity"), covers social_security_number / socialSecurityNumber.
    return "socialsecurity" in "".join(tokens)


def _path_references_ssn(path: str) -> bool:
    """Return True if any segment in a JSON path names an SSN field."""
    path_segments = [
        segment
        for segment in re.split(r"[.\[\]]+", path)
        if segment and not segment.isdigit()
    ]
    return any(_key_references_ssn(segment) for segment in path_segments)


def _reject_ssn_in_json(body: dict[str, Any]) -> None:
    """Recursively scan *body* and raise if a caller tried to embed an SSN.

    These checks are applied separately:

    * any *key* that references an SSN field (``_key_references_ssn``),
    * any full-SSN-shaped *value* (``_FULL_SSN_VALUE_RE``), regardless of key,
      and
    * any exact-4-digit *value* when its JSON path references SSN.

    Callers must not pass SSN inside --json. Error messages name the offending
    key/path but never echo the SSN value itself.
    """

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if _key_references_ssn(k):
                    raise click.BadParameter(
                        f"SSN field '{k}' detected at JSON path '{path}.{k}'.\n"
                        f"  SSN must be entered in the Ramp application form,\n"
                        f"  not collected by the CLI or passed in --json.",
                        param_hint="'--json'",
                    )
                _walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                _walk(item, f"{path}[{i}]")
        elif isinstance(node, str):
            is_full_ssn = _FULL_SSN_VALUE_RE.search(node) is not None
            is_ssn_last_4 = _SSN_LAST_4_RE.fullmatch(
                node
            ) is not None and _path_references_ssn(path)
            if is_full_ssn or is_ssn_last_4:
                raise click.BadParameter(
                    f"SSN-shaped value detected at JSON path '{path or '(root)'}'.\n"
                    f"  SSN must be entered in the Ramp application form,\n"
                    f"  not collected by the CLI or passed in --json.",
                    param_hint="'--json'",
                )

    _walk(body, "")


def _parse_json_body(raw_json: str) -> dict[str, Any]:
    try:
        body = json.loads(raw_json)
    except json.JSONDecodeError as e:
        raise click.BadParameter(f"invalid JSON: {e}", param_hint="'--json'") from e

    if not isinstance(body, dict):
        raise click.BadParameter(
            "incorporation body must be a JSON object", param_hint="'--json'"
        )

    return body


def _render_submit_success(
    response_body: bytes,
    format_flag: str | None,
    config_format: str,
) -> None:
    fmt = resolve_format(format_flag, config_format)
    try:
        data = json.loads(response_body)
    except (json.JSONDecodeError, ValueError):
        data = {"raw": response_body.decode("utf-8", errors="replace")}

    if fmt == "json":
        print_agent_json(data, pagination={"has_more": False, "next": None})
    else:
        print_json(data)


def _render_dry_run(
    env: str,
    body: dict[str, Any],
    format_flag: str | None,
    config_format: str,
) -> None:
    fmt = resolve_format(format_flag, config_format)
    url = api_url(env, _INCORPORATION_SUBMIT_PATH)

    # SECURITY: SSN fields are rejected before this point. Redact defensively in
    # case this helper is ever called from another path.
    safe_body = _redact_ssns(body)

    if fmt == "json":
        print_agent_json(
            {
                "dry_run": True,
                "method": "POST",
                "url": url,
                "body": safe_body,
                "ssn_note": "SSN fields are rejected; use the Ramp application form for SSN entry",
            },
            pagination={"has_more": False, "next": None},
        )
        return

    click.echo(f"DRY RUN: POST {url}", err=True)
    click.echo(
        "  Note: SSN fields are rejected; use the Ramp application form for SSN entry",
        err=True,
    )
    print_json(safe_body)


def _redact_ssns(body: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy with any SSN-shaped keys or values replaced by '<redacted>'.

    Defense in depth: redact both by key name (anything `_key_references_ssn`
    flags) and by value shape (any string containing an NNN-NN-NNNN or 9-digit
    run, plus exact 4-digit values only under SSN-like paths). This guarantees
    that --dry_run output never echoes SSN-like values even if a caller
    smuggles one under an unexpected key.
    """
    raw = json.dumps(body)
    parsed = json.loads(raw)

    def _redact_string(s: str, path: str) -> str:
        if _SSN_LAST_4_RE.fullmatch(s) is not None and _path_references_ssn(path):
            return "<redacted>"
        return _FULL_SSN_VALUE_RE.sub("<redacted>", s)

    def _walk(node: Any, path: str) -> Any:
        if isinstance(node, dict):
            for k in list(node.keys()):
                if _key_references_ssn(k):
                    node[k] = "<redacted>"
                else:
                    node[k] = _walk(node[k], f"{path}.{k}")
            return node
        if isinstance(node, list):
            return [_walk(item, f"{path}[{i}]") for i, item in enumerate(node)]
        if isinstance(node, str):
            return _redact_string(node, path)
        return node

    _walk(parsed, "")
    return parsed


@click.group("incorporation", help="Manage Ramp US LLC incorporation")
def incorporation_group() -> None:
    pass


@incorporation_group.command("submit")
@click.option(
    "--json",
    "json_body",
    required=True,
    metavar="JSON",
    help=(
        "Formation request body (without SSN fields).\n\n"
        "The CLI never collects SSN values for incorporation. Use the Ramp\n"
        "application form for SSN entry; passing an 'ssn' key inside --json is\n"
        "rejected as an error.\n\n"
        'Example: ramp incorporation submit --json \'{"state":"DE",...}\'\n\n'
        "SSN values must not appear in CLI arguments, prompts, env vars, stdout, or stderr."
    ),
)
@click.option(
    "--dry_run",
    "--dry-run",
    "-n",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print request (SSN redacted) without sending",
)
@click.pass_context
def submit_formation(ctx: click.Context, json_body: str, dry_run: bool) -> None:
    """Submit a Ramp LLC formation request.

    SSN values are never collected by this command. Enter SSN in the Ramp
    application form; never pass SSN in CLI arguments or --json.

    \b
        ramp incorporation submit --json '{...}'

    Endpoint:

    \b
        POST /developer/v1/incorporation/formation
    """
    env = ctx.obj["env"]
    body = _parse_json_body(json_body)

    # Step 1: Reject any SSN that the caller tried to embed in --json.
    _reject_ssn_in_json(body)

    if dry_run:
        _render_dry_run(env, body, ctx.obj["format"], ctx.obj["config_format"])
        return

    # Step 2: POST the non-sensitive formation request body.
    client = RampClient(env, profile=ctx.obj["profile"])
    response = client.post(_INCORPORATION_SUBMIT_PATH, json.dumps(body).encode())

    # Step 3: Render response. Never echo request_body or SSN values here.
    _render_submit_success(response, ctx.obj["format"], ctx.obj["config_format"])


# ── Read-only convenience commands ────────────────────────────────────────────
#
# These commands wire: IncorporationGetStateOptions, IncorporationSearchIndustries,
# IncorporationListCountries, IncorporationCreateApplicant, IncorporationGetApplicant,
# IncorporationGetCompanyStatus, and IncorporationGetDocuments.
#
def _render_get(
    response: bytes,
    format_flag: str | None,
    config_format: str,
) -> None:
    fmt = resolve_format(format_flag, config_format)
    try:
        data = json.loads(response)
    except (json.JSONDecodeError, ValueError):
        data = {"raw": response.decode("utf-8", errors="replace")}

    if fmt == "json":
        print_agent_json(data, pagination={"has_more": False, "next": None})
    else:
        print_json(data)


@incorporation_group.command("states")
@click.pass_context
def get_state_options(ctx: click.Context) -> None:
    """List US states available for Ramp LLC formation.

    Maps to: IncorporationGetStateOptions

    \b
    Endpoint: GET /developer/v1/incorporation/states
    """
    client = RampClient(ctx.obj["env"], profile=ctx.obj["profile"])
    response = client.get(f"{_INCORPORATION_BASE}/states")
    _render_get(response, ctx.obj["format"], ctx.obj["config_format"])


@incorporation_group.group(
    "industries", help="Search incorporation industry classifications"
)
def industries_group() -> None:
    pass


@industries_group.command("search")
@click.option(
    "--q",
    "query",
    required=True,
    help="Free-text industry search query (e.g. 'saas restaurant')",
)
@click.pass_context
def search_industries(ctx: click.Context, query: str) -> None:
    """Search NAICS industries for Ramp formation.

    Maps to: IncorporationSearchIndustries

    \b
    Example:
        ramp incorporation industries search --q "saas restaurant"

    Endpoint: GET /developer/v1/incorporation/industries
    """
    client = RampClient(ctx.obj["env"], profile=ctx.obj["profile"])
    response = client.get(f"{_INCORPORATION_BASE}/industries", params={"search": query})
    _render_get(response, ctx.obj["format"], ctx.obj["config_format"])


@incorporation_group.command("countries")
@click.pass_context
def list_countries(ctx: click.Context) -> None:
    """List countries of residence accepted for Ramp LLC formation.

    Maps to: IncorporationListCountries

    \b
    Endpoint: GET /developer/v1/incorporation/countries
    """
    client = RampClient(ctx.obj["env"], profile=ctx.obj["profile"])
    response = client.get(f"{_INCORPORATION_BASE}/countries")
    _render_get(response, ctx.obj["format"], ctx.obj["config_format"])


@incorporation_group.group("applicant", help="Manage incorporation applicant records")
def applicant_group() -> None:
    pass


@applicant_group.command("create")
@click.option(
    "--country-of-residence",
    "country_of_residence",
    required=False,
    default=None,
    help="ISO 3166-1 alpha-2 country code returned by `ramp incorporation countries` (e.g. 'US')",
)
@click.option(
    "--json",
    "json_body",
    required=False,
    default=None,
    help="Full applicant body as raw JSON (overrides individual flags)",
)
@click.pass_context
def create_applicant(
    ctx: click.Context,
    country_of_residence: str | None,
    json_body: str | None,
) -> None:
    """Create an incorporation applicant record for the authenticated business.

    Maps to: IncorporationCreateApplicant

    \b
    Example:
        ramp incorporation applicant create --country-of-residence US

    Endpoint: POST /developer/v1/incorporation/applicant
    """
    if json_body:
        body: dict[str, Any] = _parse_json_body(json_body)
    else:
        body = {}
        if country_of_residence:
            body["country_of_residence"] = country_of_residence

    # Same SSN-rejection posture as `submit`: even though applicant-create
    # doesn't request SSN fields, defense-in-depth rejects any caller JSON
    # that smuggles SSN-like keys or values (P1 Codex thread on #247).
    _reject_ssn_in_json(body)

    client = RampClient(ctx.obj["env"], profile=ctx.obj["profile"])
    response = client.post(
        f"{_INCORPORATION_BASE}/applicant", json.dumps(body).encode()
    )
    _render_get(response, ctx.obj["format"], ctx.obj["config_format"])


@applicant_group.command("get")
@click.pass_context
def get_applicant(ctx: click.Context) -> None:
    """Retrieve the incorporation applicant record for the authenticated business.

    Maps to: IncorporationGetApplicant

    \b
    Endpoint: GET /developer/v1/incorporation/applicant
    """
    client = RampClient(ctx.obj["env"], profile=ctx.obj["profile"])
    response = client.get(f"{_INCORPORATION_BASE}/applicant")
    _render_get(response, ctx.obj["format"], ctx.obj["config_format"])


# Ramp-facing formation_submission_status values that carry pre-EIN early-access
# meaning. Core maps provider statuses before returning this field.
# Statuses with no pre-EIN meaning (NOT_SUBMITTED, PENDING_REVIEW) are absent
# on purpose.
_PRE_EIN_STATUS_NOTES: dict[str, dict[str, Any]] = {
    "SUBMITTED": {
        "access": "LIMITED",
        "ein": "pending",
        "summary": (
            "Formation filed with the state; EIN not yet issued. The business "
            "has limited access now and auto-promotes to full access when Ramp "
            "receives the EIN."
        ),
        "unblocks_full_access": "EIN issued (formation_submission_status APPROVED)",
    },
    "APPROVED": {
        "access": "FULL_PENDING_KYB",
        "ein": "issued",
        "summary": (
            "EIN issued. Business-entity verification runs now; access promotes "
            "to full once it passes."
        ),
        "unblocks_full_access": "business-entity KYB approved",
    },
    "REJECTED": {
        "access": "AT_RISK",
        "ein": "not issued",
        "summary": (
            "Formation was rejected. Review the rejection reason and re-file; "
            "limited access may be revoked if no EIN is obtained."
        ),
        "unblocks_full_access": "resolve rejection, re-file formation, and obtain an EIN",
    },
}


def _pre_ein_status_note(status: str | None) -> dict[str, Any] | None:
    """Return the pre-EIN annotation for a formation_submission_status value.

    Pure lookup; returns None for a missing status or one with no pre-EIN
    meaning, so callers can attach the note conditionally.
    """
    if not status:
        return None
    return _PRE_EIN_STATUS_NOTES.get(status.upper())


def _render_status(
    response: bytes,
    format_flag: str | None,
    config_format: str,
) -> None:
    """Render company status, annotating the pre-EIN lifecycle when present."""
    try:
        data: Any = json.loads(response)
    except (json.JSONDecodeError, ValueError):
        data = {"raw": response.decode("utf-8", errors="replace")}

    if isinstance(data, dict):
        status = data.get("formation_submission_status")
        note = _pre_ein_status_note(status if isinstance(status, str) else None)
        if note is not None:
            data = {**data, "pre_ein": note}

    if resolve_format(format_flag, config_format) == "json":
        print_agent_json(data, pagination={"has_more": False, "next": None})
    else:
        print_json(data)


@incorporation_group.command("status")
@click.pass_context
def get_company_status(ctx: click.Context) -> None:
    """Get the current Ramp formation status for this business.

    Maps to: IncorporationGetCompanyStatus

    Checks status live from Ramp. If the formation is APPROVED but Ramp's
    local state hasn't been backfilled yet, the server triggers the
    IncorporationBackfillWorkflow inline so the FA blockers stay consistent.

    For pre-EIN early access, the output is annotated with a ``pre_ein`` block
    explaining the access the business has at the reported submission status and
    what unblocks full access (e.g. SUBMITTED → limited access, EIN pending).

    \b
    Endpoint: GET /developer/v1/incorporation/company-status
    """
    client = RampClient(ctx.obj["env"], profile=ctx.obj["profile"])
    response = client.get(_INCORPORATION_STATUS_PATH)
    _render_status(response, ctx.obj["format"], ctx.obj["config_format"])


@incorporation_group.command("documents")
@click.pass_context
def get_documents(ctx: click.Context) -> None:
    """List formation documents (articles of incorporation, EIN letter, etc.).

    Maps to: IncorporationGetDocuments

    \b
    Endpoint: GET /developer/v1/incorporation/documents
    """
    client = RampClient(ctx.obj["env"], profile=ctx.obj["profile"])
    response = client.get(f"{_INCORPORATION_BASE}/documents")
    _render_get(response, ctx.obj["format"], ctx.obj["config_format"])
