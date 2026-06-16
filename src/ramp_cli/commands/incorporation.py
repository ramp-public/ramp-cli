"""ramp incorporation commands — SSN env-var pattern (Phase 1.5).

This module wires the ``ramp incorporation submit`` subcommand for PR 1
(ADP-2789).  The full 8-verb ``ramp incorporation`` group (states, industries,
countries, applicant create/get, submit, status, documents) lives in PR 2
(ADP-2790) which stacks on top.

SSN handling
------------
The CLI process reads SSN values from environment variables when constructing
the request body.  The model never loads the SSN into its context; the tool's
argument schema does **not** declare any SSN field.  Any caller-supplied SSN
inside ``--json`` is rejected immediately with a structured error.

Env vars (set in the shell, never in the model's tool call):

    export RAMP_INCORPORATION_SSN_MEMBER_1=123-45-6789
    export RAMP_INCORPORATION_SSN_MEMBER_2=987-65-4321   # if 2 members
    export RAMP_INCORPORATION_SSN_RESPONSIBLE_PARTY=123-45-6789

The CLI fails fast (exit 1, structured error) if any required slot is missing.
No SSN value appears in stdout/stderr under any flag combination.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import click

from ramp_cli.client.api import RampClient
from ramp_cli.config.constants import api_url
from ramp_cli.errors import RampCLIError
from ramp_cli.output.formatter import print_agent_json, print_json, resolve_format

# TODO(workstream-1): reconcile with real path once ADP-2787 lands.
# The endpoint is registered by ToolApiAdapter for IncorporationSubmitFormation.
_INCORPORATION_SUBMIT_PATH = "/developer/v1/applications/incorporation/submit"

# Env-var name templates (must match DESIGN.md §7 exactly)
_SSN_MEMBER_ENV_TEMPLATE = "RAMP_INCORPORATION_SSN_MEMBER_{n}"
_SSN_RESPONSIBLE_PARTY_ENV = "RAMP_INCORPORATION_SSN_RESPONSIBLE_PARTY"

# Caller-supplied SSNs in --json are rejected two independent ways, so neither
# a clever key name nor an unexpected key can sneak an SSN past us:
#
#   1. Term detection (keys)  — any JSON key that references SSN is rejected.
#   2. Value-shape detection (values) — any JSON value that *looks* like an SSN
#      is rejected, regardless of the key it sits under.
#
# SSNs only ever arrive via env vars and are slotted in after the model's tool
# call, so a legitimate formation body never contains either signal.

# A key token marks the key as SSN-related when it *starts with* "ssn" (so
# "ssn", "ssn_number", and the concatenated "ssnvalue" all match) — but we do
# NOT substring-match, so benign keys like "className" ("cla[ssn]ame") or the
# abbreviation "assn" (association) are left alone.
_SSN_TOKEN_PREFIX = "ssn"

# Value-level SSN-shaped string: NNN-NN-NNNN or 9 consecutive digits.  Used to
# (a) reject SSN-shaped values supplied in --json and (b) as a defense-in-depth
# scan when rendering the request body to stdout/stderr (e.g. in --dry_run).
_SSN_VALUE_RE = re.compile(r"\b(?:\d{3}-\d{2}-\d{4}|\d{9})\b")


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


class MissingSSNEnvVarError(RampCLIError):
    """Raised when a required SSN env var is absent."""

    def __init__(self, var_name: str) -> None:
        self.var_name = var_name
        super().__init__(
            f"Required env var not set: {var_name}\n"
            f"  Set it in your shell before running this command:\n"
            f"    export {var_name}=<ssn>\n"
            f"  SSN is never passed as a CLI argument — it is always read from\n"
            f"  the environment so the model cannot observe it.",
            code=1,
        )


def _reject_ssn_in_json(body: dict[str, Any]) -> None:
    """Recursively scan *body* and raise if a caller tried to embed an SSN.

    Two independent checks, applied separately:

    * any *key* that references an SSN field (``_key_references_ssn``), and
    * any *value* shaped like an SSN (``_SSN_VALUE_RE``), regardless of its key.

    Callers must not pass SSN inside --json.  The env-var pattern is the only
    supported collection path.  Error messages name the offending key/path but
    never echo the SSN value itself.
    """

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if _key_references_ssn(k):
                    raise click.BadParameter(
                        f"SSN field '{k}' detected at JSON path '{path}.{k}'.\n"
                        f"  SSN must be set via environment variables, not --json:\n"
                        f"    export RAMP_INCORPORATION_SSN_MEMBER_N=<ssn>\n"
                        f"    export RAMP_INCORPORATION_SSN_RESPONSIBLE_PARTY=<ssn>\n"
                        f"  The model must never see SSN values.",
                        param_hint="'--json'",
                    )
                _walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                _walk(item, f"{path}[{i}]")
        elif isinstance(node, str) and _SSN_VALUE_RE.search(node):
            raise click.BadParameter(
                f"SSN-shaped value detected at JSON path '{path or '(root)'}'.\n"
                f"  SSN must be set via environment variables, not --json:\n"
                f"    export RAMP_INCORPORATION_SSN_MEMBER_N=<ssn>\n"
                f"    export RAMP_INCORPORATION_SSN_RESPONSIBLE_PARTY=<ssn>\n"
                f"  The model must never see SSN values.",
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


def _read_member_ssns(members: list[Any]) -> list[str]:
    """Read RAMP_INCORPORATION_SSN_MEMBER_N for each member (1-indexed).

    Fails fast if any required var is absent.  Never returns a partial list.
    """
    ssns: list[str] = []
    for i in range(1, len(members) + 1):
        var = _SSN_MEMBER_ENV_TEMPLATE.format(n=i)
        val = os.environ.get(var, "")
        if not val:
            raise MissingSSNEnvVarError(var)
        ssns.append(val)
    return ssns


def _read_responsible_party_ssn(body: dict[str, Any]) -> str | None:
    """Read RAMP_INCORPORATION_SSN_RESPONSIBLE_PARTY if the payload has a responsible_party."""
    if "responsible_party" not in body:
        return None
    val = os.environ.get(_SSN_RESPONSIBLE_PARTY_ENV, "")
    if not val:
        raise MissingSSNEnvVarError(_SSN_RESPONSIBLE_PARTY_ENV)
    return val


def _slot_ssns_into_body(
    body: dict[str, Any],
    member_ssns: list[str],
    rp_ssn: str | None,
) -> dict[str, Any]:
    """Return a copy of *body* with SSN fields inserted.

    SSN values are slotted in *after* the model emits the tool call.  They
    are never present in the model's context, in logs, or in stdout/stderr.
    """
    result = json.loads(json.dumps(body))  # deep copy via JSON round-trip

    members = result.get("members", [])
    for i, ssn in enumerate(member_ssns):
        if i < len(members):
            members[i]["ssn"] = ssn

    if rp_ssn is not None and "responsible_party" in result:
        result["responsible_party"]["ssn"] = rp_ssn

    return result


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

    # SECURITY: body already contains real SSNs at this point (slotted in from
    # env vars).  Strip them before printing — replace with a redaction sentinel.
    safe_body = _redact_ssns(body)

    if fmt == "json":
        print_agent_json(
            {
                "dry_run": True,
                "method": "POST",
                "url": url,
                "body": safe_body,
                "ssn_note": "SSN fields redacted; values read from env vars at send time",
            },
            pagination={"has_more": False, "next": None},
        )
        return

    click.echo(f"DRY RUN: POST {url}", err=True)
    click.echo(
        "  Note: SSN fields are redacted below — actual values come from env vars",
        err=True,
    )
    print_json(safe_body)


def _redact_ssns(body: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy with any SSN-shaped keys or values replaced by '<redacted>'.

    Defense in depth: redact both by key name (anything `_key_references_ssn`
    flags) and by value shape (any string containing an NNN-NN-NNNN or 9-digit
    run).  The env-var-slotted member/responsible-party SSNs live under "ssn"
    keys and are redacted here before --dry_run prints the request body.
    """
    raw = json.dumps(body)
    parsed = json.loads(raw)

    def _redact_string(s: str) -> str:
        return _SSN_VALUE_RE.sub("<redacted>", s)

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            for k in list(node.keys()):
                if _key_references_ssn(k):
                    node[k] = "<redacted>"
                else:
                    node[k] = _walk(node[k])
            return node
        if isinstance(node, list):
            return [_walk(item) for item in node]
        if isinstance(node, str):
            return _redact_string(node)
        return node

    _walk(parsed)
    return parsed


@click.group("incorporation", help="Manage US LLC incorporation via Doola")
def incorporation_group() -> None:
    pass


@incorporation_group.command("submit")
@click.option(
    "--json",
    "json_body",
    required=True,
    metavar="JSON",
    help=(
        "Formation request body (without SSN — those come from env vars).\n\n"
        "Required env vars before running:\n\n"
        "  export RAMP_INCORPORATION_SSN_MEMBER_1=<ssn>   # first LLC member\n"
        "  export RAMP_INCORPORATION_SSN_MEMBER_N=<ssn>   # for each additional member\n"
        "  export RAMP_INCORPORATION_SSN_RESPONSIBLE_PARTY=<ssn>   # if responsible_party present\n\n"
        "Example (2-member LLC):\n\n"
        "  export RAMP_INCORPORATION_SSN_MEMBER_1=123-45-6789\n"
        "  export RAMP_INCORPORATION_SSN_MEMBER_2=987-65-4321\n"
        "  export RAMP_INCORPORATION_SSN_RESPONSIBLE_PARTY=123-45-6789\n"
        '  ramp incorporation submit --json \'{"state":"DE",...}\'\n\n'
        "SSN values are read from env vars at send time — the model never sees them.\n"
        "Passing an 'ssn' key inside --json is rejected as an error."
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
    """Submit a Doola LLC formation request.

    SSN values are read from environment variables — never from CLI arguments.
    Set the required env vars before calling this command:

    \b
        export RAMP_INCORPORATION_SSN_MEMBER_1=123-45-6789
        export RAMP_INCORPORATION_SSN_RESPONSIBLE_PARTY=123-45-6789
        ramp incorporation submit --json '{...}'

    The endpoint path is stubbed as:

    \b
        POST /developer/v1/applications/incorporation/submit

    TODO(workstream-1): reconcile with real path once ADP-2787 / ADP-2786 land.
    """
    env = ctx.obj["env"]
    body = _parse_json_body(json_body)

    # Step 1: Reject any SSN that the caller tried to embed in --json.
    _reject_ssn_in_json(body)

    # Step 2: Read SSN values from env vars.
    members = body.get("members", [])
    member_ssns = _read_member_ssns(members)
    rp_ssn = _read_responsible_party_ssn(body)

    # Step 3: Slot SSNs into request body (now the body is ready to POST).
    full_body = _slot_ssns_into_body(body, member_ssns, rp_ssn)

    if dry_run:
        _render_dry_run(env, full_body, ctx.obj["format"], ctx.obj["config_format"])
        return

    # Step 4: POST.  SSN travels in the request body to Ramp; Ramp forwards to
    # Doola and drops the full value (stores only ssn_last_4).
    client = RampClient(env)
    response = client.post(_INCORPORATION_SUBMIT_PATH, json.dumps(full_body).encode())

    # Step 5: Render response.  Never echo full_body or SSN values here.
    _render_submit_success(response, ctx.obj["format"], ctx.obj["config_format"])
