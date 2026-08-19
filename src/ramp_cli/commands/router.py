"""Configure coding agents to use Ramp Router."""

import hashlib
import hmac
import io
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path, PurePath
from typing import Literal

import click
import httpx
import json5
import questionary
import zstandard
from click.core import ParameterSource

from ramp_cli import claude_cowork
from ramp_cli.commands import claude_code
from ramp_cli.commands.router_sync import (
    SYNC_HOOK_USER_MANAGED,
    SYNC_OPT_OUT_ENV,
    claim_sync_slot,
    command_runs_session_sync,
    log_failure,
    session_sync_hook_command,
    spawn_detached_refresh,
    sync_is_due,
    update_notice,
)
from ramp_cli.output.formatter import print_agent_json, resolve_format
from ramp_cli.output.style import show_notice, start_spinner
from ramp_cli.router_integrations import integration_package_path
from ramp_cli.router_integrations.codex_cost_hook import CODEX_COST_HOOK_SCRIPT
from ramp_cli.router_setup import acquire_router_api_key
from ramp_cli.version_check import (
    refresh_version_cache,
    suppress_next_update_notice,
    sync_update_notice_file,
)

DEFAULT_ROUTER_BASE_URL = "https://router-api.ramp.com/v1"

# The bundled plugins already take these, and reading them here too means a
# stack other than production can be reached without hand-editing the files
# this command writes, which a later configure would overwrite anyway.
BASE_URL_ENVS = ("RAMP_ROUTER_BASE_URL", "LLM_GATEWAY_BASE_URL")

# Read by the bundled Pi plugin, which Pi gives no other way to be told which
# Router to call.
PI_PLUGIN_CONFIG_FILE = "ramp-router-config.json"
PI_PLUGIN_MODEL_CACHE_FILE = "ramp-router-model-cache.json"
PI_PLUGIN_MODEL_CACHE_KEY_FILE = "ramp-router-model-cache-key"
PI_PLUGIN_RUNTIME_MODELS_FILE = "ramp-router-runtime-models.json"
ROUTER_UI_URL = "https://router.ramp.com"
ROUTER_UI_URL_ENV = "RAMP_ROUTER_UI_URL"
ROUTER_PROVIDER = "ramp-router"
# Read instead of --api-key so the secret stays out of shell history and out
# of the process list.
CONFIGURE_KEY_ENV = "RAMP_ROUTER_CONFIGURE_API_KEY"
# Browser-created keys reach the control-plane database before the data plane's
# request-time projection. Retry only that known-new key long enough for its
# materialized record to arrive.
NEW_KEY_RETRY_DELAYS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)

# Router answers a client in that client's own shape when it says which client
# it is. This selects a response format and nothing else.
GATEWAY_CLIENT_HEADER = "X-Gateway-Client"
CODEX_CLIENT_NAME = "codex"
CODEX_ROUTER_CATALOG = "ramp-router-models.json"
CODEX_ROUTER_CATALOG_DIGEST_KEY = "router_catalog_digest"
CODEX_ROUTER_CATALOG_MANAGED_KEY = "router_catalog_managed"
# The name the Router dashboard serves; manual installs land on the same file.
CODEX_COST_HOOK = "codex-cost-hook"
CODEX_COST_HOOK_MANAGED_KEY = "cost_hook_managed"
# The digest of the exact SessionStart group this CLI rendered — or the
# user-managed sentinel when a user group already invokes the sync — so only
# that group is ever replaced or removed.
CODEX_SYNC_HOOK_STATE_KEY = "session_sync_hook"
CODEX_CATALOG_MODEL_LIMIT = 100
CLIENT_NAMES = {
    "claude-code": "Claude Code",
    "codex": "Codex",
    "opencode": "OpenCode",
    "pi": "Pi",
}
# The command each agent puts on PATH. Used to tell which agents this machine
# actually has, so the picker does not offer to write configuration into
# directories for agents that were never installed.
CLIENT_EXECUTABLES = {
    "claude-code": "claude",
    "codex": "codex",
    "opencode": "opencode",
    "pi": "pi",
}
# Claude Cowork joins the configure and unconfigure pickers but stays out of
# CLIENT_NAMES: it has no config file of its own, exists only on macOS, and is
# set up through Claude Desktop's profile library rather than through the
# config-path machinery the coding agents share.
COWORK_CLIENT = "cowork"
AGENT_NAMES = {**CLIENT_NAMES, COWORK_CLIENT: "Claude Cowork"}
ArtifactClaim = Literal["create", "replace"]


@dataclass(frozen=True)
class RouterModel:
    """One model as Router describes it on GET /v1/models.

    Router states its metadata for every model it serves, so this is required
    rather than optional. Guessing a model's efforts or limits from the shape
    of its name produced answers that were confidently wrong, and refusing to
    guess is the point of the extension.
    """

    id: str
    metadata: "RouterModelMetadata"


@dataclass(frozen=True)
class RouterModelMetadata:
    request_name: str
    display_name: str
    description: str
    listing_order: int
    efforts: tuple[tuple[str, str], ...]
    default_effort: str
    input_modalities: tuple[str, ...]
    # Router states a window for every model it serves. The plugins refuse a
    # model without one, so accepting it here would let configure claim
    # success for a list the agent then rejects.
    context_window: int
    max_output_tokens: int
    tool_calls: bool | None


def _model_metadata(raw: object, identifier: str) -> RouterModelMetadata:
    """Read Router's description of one model.

    A missing or unrecognized schema is an error rather than a cue to fall
    back. Router publishes this for everything it serves, so its absence means
    the two are out of step, and configuring an agent from guesses would hide
    that until a request failed.
    """
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise click.ClickException(
            f"Ramp Router described {identifier!r} in a format this version of "
            "the CLI does not understand. Update the Ramp CLI and try again."
        )
    capabilities = raw.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    reasoning = capabilities.get("reasoning")
    reasoning = reasoning if isinstance(reasoning, dict) else {}
    modalities = capabilities.get("modalities")
    modalities = modalities if isinstance(modalities, dict) else {}
    listing = raw.get("listing")
    listing = listing if isinstance(listing, dict) else {}

    efforts = tuple(
        (option["value"], option.get("description") or "")
        for option in reasoning.get("efforts") or []
        if isinstance(option, dict) and isinstance(option.get("value"), str)
    )
    inputs = tuple(
        value for value in modalities.get("input") or [] if isinstance(value, str)
    )
    limits = raw.get("limits")
    limits = limits if isinstance(limits, dict) else {}
    tools = capabilities.get("tools")
    tools = tools if isinstance(tools, dict) else {}
    order = listing.get("order")
    return RouterModelMetadata(
        request_name=raw.get("request_name") or "",
        display_name=raw.get("display_name") or "",
        description=raw.get("description") or "",
        listing_order=order if isinstance(order, int) else sys.maxsize,
        efforts=efforts,
        default_effort=reasoning.get("default_effort") or "",
        input_modalities=inputs,
        context_window=_token_limit(
            limits.get("context_window"), "context_window", identifier
        ),
        max_output_tokens=_token_limit(
            limits.get("max_output_tokens"), "max_output_tokens", identifier
        ),
        tool_calls=tools.get("supported")
        if isinstance(tools.get("supported"), bool)
        else None,
    )


def _token_limit(value: object, field: str, identifier: str) -> int:
    """Read a token limit, refusing one Router did not state.

    A guessed limit makes an agent truncate or over-promise against a boundary
    the model does not have, which surfaces much later as a confusing failure.
    """
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    raise click.ClickException(
        f"Ramp Router did not state {field} for {identifier!r}. "
        "Update the Ramp CLI and try again."
    )


# TODO(router-plugin-npm): Reconsider publishing these plugins if they need a
# lifecycle independent of ramp-cli. Bundling gives us an atomic, offline-tested
# CLI/plugin release, but gives up several npm-distribution benefits:
# - users and third-party installers could install a plugin without ramp-cli;
# - plugin fixes could ship under their own semver without a new Python release;
# - OpenCode/Pi could participate in native package install/update and pinning;
# - npm would provide registry discovery/download telemetry plus standard
#   dependency resolution, caching, lockfile integrity, and provenance tooling.
_OPENCODE_PLUGIN_PACKAGE = "@ramp/router-opencode-provider"
_PI_PLUGIN_PACKAGE = "@ramp/router-pi-provider"


def _human_join(values: list[str]) -> str:
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return " and ".join(values)
    return ", ".join(values[:-1]) + f", and {values[-1]}"


def _codex_harness_prompt(default_model: str) -> str | None:
    """Read Codex's own harness prompt out of the installed binary.

    Codex replaces its prompt with whatever a catalog entry's base_instructions
    says, and that prompt is some 18,000 characters describing its editing
    constraints, tool protocol and output rules. Router cannot supply it and a
    stand-in makes the agent worse, so it comes from the user's own install and
    matches their version. Returns None when Codex is absent or says something
    unexpected, since a missing prompt is better than a wrong one.
    """
    if not shutil.which("codex"):
        return None
    try:
        result = subprocess.run(
            ["codex", "debug", "models"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        models = json.loads(result.stdout).get("models")
    except (json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(models, list):
        return None

    prompts = {
        model["slug"]: model["base_instructions"]
        for model in models
        if isinstance(model, dict)
        and isinstance(model.get("slug"), str)
        and isinstance(model.get("base_instructions"), str)
        and model["base_instructions"]
    }
    if not prompts:
        return None
    if default_model in prompts:
        return prompts[default_model]
    # Codex ships a different prompt per model family, but instructions are a
    # single global setting. With no prompt for the model being configured,
    # take the one the most families share: that is Codex's general harness
    # text rather than a specialization. Ties break on the slug so two runs
    # against the same install agree.
    prompt_stats: dict[str, tuple[int, str]] = {}
    for slug, prompt in prompts.items():
        count, minimum_slug = prompt_stats.get(prompt, (0, slug))
        prompt_stats[prompt] = (count + 1, min(minimum_slug, slug))
    return max(prompt_stats, key=prompt_stats.__getitem__)


def _codex_original_catalog(root_values: dict) -> str | None:
    """Snapshot the restored provider's catalog for the original profile.

    Codex 0.147.0 caches every provider in the same models_cache.json and keys
    that cache only by Codex version. A profile can therefore select OpenAI
    correctly and still draw Router's cached models. model_catalog_json is
    authoritative, so a profile-local snapshot keeps its picker isolated until
    Codex starts partitioning the shared cache by provider.
    """
    if root_values.get("model_catalog_json") or not shutil.which("codex"):
        return None

    provider = _codex_restored_provider(root_values)
    if provider == "openai":
        # The ordinary debug command still consults models_cache.json. That is
        # the provider-agnostic cache we are escaping, so a fresh Router entry
        # there would make us snapshot the exact contamination this file is
        # meant to prevent. The bundled catalog cannot read that cache.
        commands = [
            [
                "codex",
                "-c",
                f"model_provider={json.dumps(provider)}",
                "debug",
                "models",
                "--bundled",
            ]
        ]
    else:
        commands = [
            [
                "codex",
                "-c",
                f"model_provider={json.dumps(provider)}",
                "debug",
                "models",
            ]
        ]

    for command in commands:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0:
            continue
        try:
            catalog = json.loads(result.stdout)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(catalog, dict)
            and isinstance(catalog.get("models"), list)
            and catalog["models"]
        ):
            return json.dumps(catalog, indent=2) + "\n"
    return None


def _codex_key_command(key_path: Path) -> tuple[str, list[str]]:
    """Return a command that prints the key file, for this platform.

    Codex needs a command rather than an inline token, and the obvious POSIX
    choice does not exist on Windows.
    """
    if os.name == "nt":
        return "cmd", ["/c", "type", str(key_path)]
    return "/bin/cat", [str(key_path)]


def _codex_provider(key_path: Path, base_url: str | None = None) -> str:
    """Describe the Router provider to Codex.

    The credential is read by a command rather than written inline, because
    Codex only refreshes its model list for providers using command auth or a
    ChatGPT account. With experimental_bearer_token it never asks the provider
    what models exist, which is what forced a generated catalog file. Printing
    the key from a private file is the smallest command that satisfies it.

    The client marker is what makes Router answer with Codex's own catalog
    shape. It is declared here rather than left to the version string Codex
    already sends, because that shape carries no harness prompt and is only
    safe alongside the model_instructions_file this same setup writes.
    """
    command, args = _codex_key_command(key_path)
    return f'''[model_providers.ramp-router]
name = "Ramp Router"
base_url = "{(base_url or router_base_url()).rstrip("/")}"
wire_api = "responses"
supports_websockets = false
http_headers = {{ "{GATEWAY_CLIENT_HEADER}" = "{CODEX_CLIENT_NAME}" }}

[model_providers.ramp-router.auth]
command = {json.dumps(command)}
args = {json.dumps(args)}
timeout_ms = 5000
'''


_TABLE_HEADER = re.compile(r"^\s*\[.*]\s*(?:#.*)?$")
_OWNED_CODEX_TABLE = re.compile(
    r"^\s*\[\s*(?:model_providers|\"model_providers\"|'model_providers')\s*\.\s*"
    r"(?:ramp-router|\"ramp-router\"|'ramp-router')\s*(?:\.|])"
)
_OWNED_CODEX_ROOT_KEY = re.compile(
    r"^\s*(?:model|model_provider|model_catalog_json|model_instructions_file)\s*="
)
# A Stop group is not ours by header alone; see _is_owned_cost_hook_chunk.
_CODEX_STOP_HOOK_TABLE = re.compile(
    r"^\s*\[\[\s*(?:hooks|\"hooks\"|'hooks')\s*\.\s*"
    r"(?:Stop|\"Stop\"|'Stop')\s*]]\s*(?:#.*)?$"
)
# A SessionStart group is not ours by header alone; see
# _is_recorded_sync_hook_chunk.
_CODEX_SESSION_START_HOOK_TABLE = re.compile(
    r"^\s*\[\[\s*(?:hooks|\"hooks\"|'hooks')\s*\.\s*"
    r"(?:SessionStart|\"SessionStart\"|'SessionStart')\s*]]\s*(?:#.*)?$"
)
_OWNED_ROOT_KEYS = (
    "model",
    "model_provider",
    "model_catalog_json",
    # Written by this command, so its previous value has to be captured to be
    # given back.
    "model_instructions_file",
)

# Codex has no way to make a profile the default, so Router has to own the root
# keys to be the default. The selection it displaces is written here instead of
# only into the undo receipt, which keeps the previous setup one flag away
# rather than reachable only by unconfiguring.
CODEX_ORIGINAL_PROFILE = "original"
# Codex resolves an empty model_instructions_file to CODEX_HOME and refuses to
# start, so a profile cannot blank the key Router writes at the root. It gets
# its own copy of Codex's prompt instead. Named like the rest of what this
# command writes, since the profile filename is the only one Codex dictates.
CODEX_ORIGINAL_INSTRUCTIONS = "ramp-router-original-instructions.md"
# Codex currently shares models_cache.json across providers. The original
# profile points at this provider-specific snapshot instead.
CODEX_ORIGINAL_CATALOG = "ramp-router-original-models.json"
# Records what the escape profile held when it was written, so a profile the
# user has since edited is left alone instead of deleted.
CODEX_ORIGINAL_PROFILE_DIGEST_KEY = "original_profile_digest"
# Names the file this CLI created, so unconfigure removes only its own and
# never a profile the user happened to name the same.
CODEX_ORIGINAL_PROFILE_STATE_KEY = "original_profile"
CODEX_ORIGINAL_CATALOG_DIGEST_KEY = "original_catalog_digest"
CODEX_ORIGINAL_CATALOG_STATE_KEY = "original_catalog"
CODEX_ORIGINAL_INSTRUCTIONS_DIGEST_KEY = "original_instructions_digest"
CODEX_ORIGINAL_INSTRUCTIONS_STATE_KEY = "original_instructions"
CODEX_PREEXISTING_ROUTER_SESSIONS_STATE_KEY = "preexisting_router_sessions"


# prompt_toolkit ships ("selected", "reverse"), which paints every checked row
# in inverse video. Every agent starts checked here, so the default turns the
# whole list into one indistinguishable block. Each class is therefore stated
# outright, and the checked rows carry the brand color the rest of the CLI uses
# for a current selection.
_RAMP_YELLOW = (228, 242, 33)
_PICKER_YELLOW = "#e4f221"
_PICKER_POINTER = "\u25b6"
_PICKER_STYLE = questionary.Style(
    [
        ("qmark", f"fg:{_PICKER_YELLOW} bold"),
        ("question", "bold"),
        ("instruction", "fg:#8a8a8a"),
        ("pointer", f"fg:{_PICKER_YELLOW} bold"),
        ("highlighted", "fg:#ffffff bold noreverse"),
        ("selected", f"fg:{_PICKER_YELLOW} noreverse"),
        ("answer", f"fg:{_PICKER_YELLOW} bold"),
        ("text", "fg:#a8a8a8"),
        ("disabled", "fg:#585858 italic"),
    ]
)


def _codex_original_profile_path(home: Path) -> Path:
    return home / f"{CODEX_ORIGINAL_PROFILE}.config.toml"


def _claim_escape_artifact(
    path: Path,
    state: dict,
    state_key: str,
    digest_key: str,
) -> ArtifactClaim | None:
    """Decide what to do with an escape artifact this run wants to own.

    A missing path can be created exclusively. An existing path is replaceable
    only when the receipt names it and its recorded digest still matches.
    Matching bytes alone establish no provenance: a user can independently
    create exactly the same default profile or overlay.
    """
    if not path.exists():
        return "create"
    if (
        state.get(state_key) == path.name
        and isinstance(state.get(digest_key), str)
        and _is_unmodified(path, state.get(digest_key))
    ):
        return "replace"
    return None


def _record_escape_artifact(
    state: dict,
    path: Path,
    content: str,
    state_key: str,
    digest_key: str,
) -> bool:
    """Record one owned artifact; return whether the receipt changed."""
    recorded = (path.name, _content_digest(content))
    if (state.get(state_key), state.get(digest_key)) == recorded:
        return False
    state[state_key], state[digest_key] = recorded
    return True


def _render_codex_original_profile(
    root_values: dict,
    instructions: Path | None = None,
    catalog: Path | None = None,
) -> str:
    """Render the profile that puts a session back on the previous provider.

    A profile overrides only the keys it states, and the base config now holds
    Router's values, so every key Router writes has to be answered here. A
    config that never named a provider still names the built-in one, and one
    that never named an instructions file is pointed at its own copy of
    Codex's prompt rather than being left to inherit Router's.
    """
    values = {key: root_values.get(key) for key in _OWNED_ROOT_KEYS}
    # Do not synthesize a model when the previous setup had none. Once this
    # profile selects its provider and isolated catalog, Codex resolves that
    # catalog's own default without inheriting Router's root model.
    values["model_provider"] = values.get("model_provider") or "openai"
    if instructions is not None and not values.get("model_instructions_file"):
        values["model_instructions_file"] = str(instructions)
    if catalog is not None and not values.get("model_catalog_json"):
        values["model_catalog_json"] = str(catalog)
    return "".join(
        f"{key} = {json.dumps(value)}\n"
        for key, value in values.items()
        if value is not None
    )


_PROFILE_CLIENTS = ("claude-code", "codex")


def _profile_notice(results: list[dict], restore_clients: str) -> list[str]:
    """Spell out what taking the default costs, for the agents it costs it in.

    Neither Claude Code nor Codex can select a profile by default, so Router
    has to own the default outright, and that is not something a user should
    discover from a support thread. Returns no lines when neither was
    configured, since nothing was displaced.
    """
    configured = [
        result["client"] for result in results if result["client"] in _PROFILE_CLIENTS
    ]
    if not configured:
        return []
    names = " & ".join(CLIENT_NAMES[client] for client in configured)
    verb = "supports" if len(configured) == 1 else "support"
    lines = [
        f"{names} only {verb} one model provider at a time, so we've replaced "
        "the default with Ramp Router.",
    ]
    escapes = [
        result.get("original_setup_command")
        for result in results
        if result.get("original_setup_command")
    ]
    if escapes:
        lines += ["", "Run your previous setup at any time in parallel:"]
        lines += [f"  {escape}" for escape in escapes]
    lines += [
        "",
        "To remove Router and make your previous setup the default again:",
        f"  ramp router unconfigure{restore_clients}",
    ]
    if "claude-code" in configured:
        # Last, because it is the consequence a reader should leave with rather
        # than something to scroll past on the way to the commands.
        lines += [
            "",
            "claude.ai connectors and other subscription-only features are "
            "unavailable in Claude Code while Router is the default.",
        ]
    return lines


def _read_state_file(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _read_codex_catalog(path_value: object) -> dict | None:
    if not isinstance(path_value, str) or not path_value:
        return None
    catalog_path = Path(path_value).expanduser()
    if not catalog_path.is_absolute():
        return None
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    models = catalog.get("models") if isinstance(catalog, dict) else None
    return catalog if isinstance(models, list) and bool(models) else None


def _codex_catalog_is_usable(path_value: object) -> bool:
    return _read_codex_catalog(path_value) is not None


def _codex_original_profile_is_usable(home: Path, state: dict) -> bool:
    """Check that an unowned profile still escapes Router safely.

    Codex writes selections such as model_reasoning_effort back into the active
    profile. That intentionally breaks our deletion digest, but it does not
    make the profile unusable. Provider and catalog are the fields that decide
    whether the command really leaves Router, so validate those directly.
    """
    root_values = state.get("root")
    if not isinstance(root_values, dict):
        return False
    try:
        profile = tomllib.loads(
            _codex_original_profile_path(home).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return False
    if profile.get("model_provider") != _codex_restored_provider(root_values):
        return False

    expected_catalog = root_values.get("model_catalog_json")
    if not isinstance(expected_catalog, str) or not expected_catalog:
        expected_catalog = str(home / CODEX_ORIGINAL_CATALOG)
    if profile.get("model_catalog_json") != expected_catalog:
        return False
    catalog = _read_codex_catalog(expected_catalog)
    if catalog is None:
        return False
    profile_model = profile.get("model")
    catalog_models = {
        model.get("slug")
        for model in catalog["models"]
        if isinstance(model, dict) and isinstance(model.get("slug"), str)
    }
    if profile_model is not None and profile_model not in catalog_models:
        return False
    if root_values.get("model_catalog_json"):
        # A catalog from the pre-Router config is user-selected provenance.
        return True

    catalog_path = Path(expected_catalog)
    if state.get(CODEX_ORIGINAL_CATALOG_STATE_KEY) == catalog_path.name and isinstance(
        state.get(CODEX_ORIGINAL_CATALOG_DIGEST_KEY), str
    ):
        return _is_unmodified(
            catalog_path, state.get(CODEX_ORIGINAL_CATALOG_DIGEST_KEY)
        )

    # A profile may survive because Codex added harmless settings and broke
    # its ownership digest. If its catalog lacks receipt provenance too,
    # compare it with a fresh provider-specific snapshot before advertising.
    expected_body = _codex_original_catalog(root_values)
    if expected_body is None:
        return False
    try:
        return catalog_path.read_text(encoding="utf-8") == expected_body
    except (OSError, UnicodeError):
        return False


def _claude_original_settings_are_usable(path: Path, state: dict) -> bool:
    """Validate an escape overlay independently of who owns its file."""
    try:
        current = claude_code.read_settings(claude_code.original_settings_path(path))
    except click.ClickException:
        return False
    expected = claude_code.plan_original_settings(state)
    current_environment = current.get("env")
    expected_environment = expected.get("env")
    if not isinstance(current_environment, dict) or not isinstance(
        expected_environment, dict
    ):
        return False
    if any(
        current_environment.get(key) != value
        for key, value in expected_environment.items()
    ):
        return False
    return all(
        current.get(key) == value for key, value in expected.items() if key != "env"
    )


def _original_setup_command(client: str, path: Path) -> str | None:
    """How to run one agent on the setup Router replaced, if we kept it.

    A receipt proves ownership. A profile Codex or the user has since edited
    remains theirs, but can still be offered when its provider and catalog
    prove that it safely runs the setup Router replaced.
    """
    if client == "codex":
        state = _read_state_file(path.parent / "ramp-router-state.json")
        if _codex_original_profile_is_usable(path.parent, state):
            return f"codex --profile {CODEX_ORIGINAL_PROFILE}"
        return None
    if client == "claude-code":
        try:
            state = claude_code.read_state(path)
        except click.ClickException:
            return None
        if _claude_original_settings_are_usable(path, state):
            original = claude_code.plan_original_settings(state)
            return claude_code.original_settings_command(
                path, use_default_model="model" not in original
            )
    return None


def _clients_with_a_receipt() -> tuple[str, ...]:
    """Return the agents this CLI has a Router setup recorded for.

    Deliberately weaker than configured_router_clients, which also requires a
    readable credential: an unusable key is a reason to unconfigure, not a
    reason to hide the agent from the list of what can be undone.
    """
    return tuple(
        client
        for client in CLIENT_NAMES
        if (_client_config_path(client).parent / "ramp-router-state.json").is_file()
    )


def _installed_clients() -> tuple[str, ...]:
    """Return the coding agents that appear to be present on this machine.

    An agent counts when its command is on PATH or when it already keeps a
    configuration directory, because an agent installed somewhere PATH does not
    reach still has a setup worth pointing at Router. Detection only narrows
    what the picker offers; naming an agent explicitly always configures it, so
    a missed install costs an argument rather than the whole command.
    """
    return tuple(
        client
        for client, executable in CLIENT_EXECUTABLES.items()
        if shutil.which(executable) or _client_config_path(client).parent.is_dir()
    )


def _can_draw_picker(ctx: click.Context) -> bool:
    """Report whether there is a terminal to draw the picker on.

    An installer or CI job supplies the key through the environment without
    passing --no-input, and a full-screen prompt in that shape aborts the
    command instead of configuring anything. Omitting agent names there means
    what it always meant: every agent.
    """
    if ctx.obj["no_input"]:
        return False
    return all(
        hasattr(stream, "isatty") and stream.isatty()
        for stream in (sys.stdin, sys.stdout)
    )


def _can_prompt(ctx: click.Context, fmt: str) -> bool:
    """Keep machine output non-interactive even when it is attached to a TTY."""
    return fmt != "json" and _can_draw_picker(ctx)


def _pick_installed_clients() -> tuple[str, ...]:
    """Ask which coding agents to configure, with every agent preselected.

    Cowork is deliberately not a line here and asks no question of its own:
    it is Claude Desktop's side of the same Claude setup, so the one Claude
    entry covers both Claude apps, the way the one Codex entry covers the
    Codex CLI and the Codex app.
    """
    candidates = _installed_clients()
    cowork_available = claude_cowork.is_available()
    if cowork_available and "claude-code" not in candidates:
        # The Claude entry is the only interactive road to Cowork, so an
        # available Cowork earns it a place even where Claude Code itself
        # was not detected.
        candidates = (*candidates, "claude-code")
    if not candidates:
        # Offering nothing would end the command on a machine where detection
        # simply missed, and configuring an agent before installing it is a
        # supported thing to do.
        click.echo("No coding agents found. Showing every agent Router supports.")
        candidates = tuple(CLIENT_NAMES)
    # The list says which apps an entry covers, so nobody has to think of
    # Claude Code and Claude Desktop as two separate things to configure.
    titles = dict(AGENT_NAMES)
    titles["codex"] = "Codex (CLI + desktop app)"
    if cowork_available:
        titles["claude-code"] = "Claude (CLI + desktop app)"
    selected = _pick_clients(
        "Choose the agents you want to configure with Ramp Router",
        candidates,
        titles=titles,
    )
    if cowork_available and "claude-code" in selected:
        # The Claude entry covers both Claude apps without another question,
        # the way the Codex entry covers the Codex CLI and app. The same
        # availability answer that rendered the label expands the selection,
        # so what was promised on screen is exactly what happens.
        selected = tuple(
            dict.fromkeys(
                expanded
                for client in selected
                for expanded in (
                    ("claude-code", COWORK_CLIENT)
                    if client == "claude-code"
                    else (client,)
                )
            )
        )
    return selected


def _pick_clients(
    message: str,
    candidates: tuple[str, ...],
    titles: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Ask which of ``candidates`` to act on, with every one preselected."""
    labels = titles or AGENT_NAMES
    selected = questionary.checkbox(
        message,
        choices=[
            questionary.Choice(title=labels[client], value=client, checked=True)
            for client in candidates
        ],
        validate=lambda answer: bool(answer) or "Select at least one agent.",
        style=_PICKER_STYLE,
        pointer=_PICKER_POINTER,
        instruction="(enter to confirm, space to toggle, a to toggle all)",
    ).ask()
    if selected is None:
        raise click.Abort()
    return tuple(selected)


def _pick_claude_models(
    current: str = "compact", clients: tuple[str, ...] = ("claude-code",)
) -> str:
    model_clients = tuple(client for client in clients if client in _PROFILE_CLIENTS)
    subject = _human_join([CLIENT_NAMES[client] for client in model_clients])
    selected = questionary.select(
        f"Which models should {subject} show?",
        choices=[
            questionary.Choice(title="Recommended models (default)", value="compact"),
            questionary.Choice(title="All available models", value="all"),
        ],
        default=current,
        style=_PICKER_STYLE,
        pointer=_PICKER_POINTER,
        instruction="(enter to confirm)",
    ).ask()
    if selected is None:
        raise click.Abort()
    return selected


@click.group("router", help="Configure coding agents to use Ramp Router")
def router_group() -> None:
    pass


@router_group.command(
    "configure-cowork",
    hidden=True,
    help="Deprecated alias for 'ramp router configure cowork'.",
)
@click.option(
    "--setup-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Router setup JSON downloaded by the Get started button.",
)
@click.option(
    "--api-key",
    metavar="KEY",
    envvar=CONFIGURE_KEY_ENV,
    help=(
        "Ramp Router API key. Opens browser setup when omitted. "
        f"Set {CONFIGURE_KEY_ENV} instead to keep it out of shell history."
    ),
)
@click.option(
    "--no-browser",
    is_flag=True,
    help="Print the Router setup URL instead of opening a browser.",
)
@click.pass_context
def router_configure_cowork(
    ctx: click.Context,
    setup_file: Path | None,
    api_key: str | None,
    no_browser: bool,
) -> None:
    """Kept for scripts and muscle memory; Cowork now lives in configure."""
    _run_configure(
        ctx,
        clients=(COWORK_CLIENT,),
        setup_file=setup_file,
        api_key=api_key,
        no_browser=no_browser,
        claude_models=None,
        api_key_source=ctx.get_parameter_source("api_key"),
        legacy_cowork_alias=True,
    )


@router_group.command(
    "unconfigure-cowork",
    hidden=True,
    help="Deprecated alias for 'ramp router unconfigure cowork'.",
)
@click.pass_context
def router_unconfigure_cowork(ctx: click.Context) -> None:
    """Kept for scripts and muscle memory; Cowork now lives in unconfigure."""
    _run_unconfigure(ctx, clients=(COWORK_CLIENT,), legacy_cowork_alias=True)


@router_group.command(
    "configure",
    help="Configure coding agents and Claude Cowork; omit names to choose interactively",
)
@click.argument(
    "clients",
    type=click.Choice(tuple(AGENT_NAMES), case_sensitive=False),
    nargs=-1,
)
@click.option(
    "--setup-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Router setup JSON downloaded by the Get started button.",
)
@click.option(
    "--api-key",
    metavar="KEY",
    envvar=CONFIGURE_KEY_ENV,
    help=(
        "Ramp Router API key. Opens browser setup when omitted. "
        f"Set {CONFIGURE_KEY_ENV} instead to keep it out of shell history."
    ),
)
@click.option(
    "--no-browser",
    is_flag=True,
    help="Print the Router setup URL instead of opening a browser.",
)
@click.option(
    "--claude-models",
    type=click.Choice(("compact", "all"), case_sensitive=False),
    help="Models shown by Claude Code: recommended compact list or all models.",
)
@click.pass_context
def router_configure(
    ctx: click.Context,
    clients: tuple[str, ...],
    setup_file: Path | None,
    api_key: str | None,
    no_browser: bool,
    claude_models: str | None,
) -> None:
    _run_configure(
        ctx,
        clients=clients,
        setup_file=setup_file,
        api_key=api_key,
        no_browser=no_browser,
        claude_models=claude_models,
        api_key_source=ctx.get_parameter_source("api_key"),
    )


def _run_configure(
    ctx: click.Context,
    *,
    clients: tuple[str, ...],
    setup_file: Path | None,
    api_key: str | None,
    no_browser: bool,
    claude_models: str | None,
    api_key_source: ParameterSource | None,
    legacy_cowork_alias: bool = False,
) -> None:
    if (
        setup_file is not None
        and api_key is not None
        and api_key_source is ParameterSource.COMMANDLINE
    ):
        raise click.UsageError("--setup-file cannot be combined with --api-key.")
    # Click has already copied any configured value into api_key. Drop the
    # secret from the environment before anything here spawns a child: the
    # Cowork availability probe behind the picker, the host preflight, and
    # Codex itself all inherit what this process still carries.
    os.environ.pop(CONFIGURE_KEY_ENV, None)
    if setup_file is None and api_key is not None:
        # A flag- or environment-supplied key is known now, so an empty one
        # is named as the invalid input before the picker draws, any host
        # check runs, or any network work happens.
        api_key = api_key.strip()
        if not api_key:
            raise click.BadParameter(
                "cannot be empty", param_hint="'--api-key'"
            ) from None
    all_clients = tuple(CLIENT_NAMES)
    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
    requested_clients = tuple(dict.fromkeys(clients))
    picked_from_menu = False
    if not requested_clients and _can_prompt(ctx, fmt):
        requested_clients = _pick_installed_clients()
        picked_from_menu = True
    clients = requested_clients or all_clients
    if (
        claude_models is not None
        and clients != ("claude-code",)
        and not (picked_from_menu and "claude-code" in clients)
    ):
        # A picker selection that grew to cover Cowork still has exactly one
        # Claude Code for the option to describe.
        raise click.UsageError(
            "--claude-models requires exactly one agent: "
            "'ramp router configure claude-code --claude-models compact|all'."
        )

    claude_path = _client_config_path("claude-code")
    existing_claude = claude_code.state_path(claude_path).is_file()
    if (
        clients == ("claude-code",)
        and existing_claude
        and setup_file is None
        and api_key is None
    ):
        # A repeat configure changes only the presentation preference. It does
        # not need a new key, a model request, or another pass over auth and
        # discovery settings that are already configured.
        _stored_router_api_key("claude-code", claude_path)
        current_view = claude_code.model_view(
            claude_code.read_settings(claude_path), claude_path
        )
        if claude_models is None and _can_prompt(ctx, fmt):
            claude_models = _pick_claude_models(current_view, clients)
        view = claude_models or current_view
        _write_claude_model_view(claude_path, view)
        if fmt == "json":
            settings = claude_code.read_settings(claude_path)
            print_agent_json(
                {
                    "client": "claude-code",
                    "config_path": str(claude_path),
                    "provider": ROUTER_PROVIDER,
                    "default_model": settings.get("model"),
                    # A local-only preference update deliberately performs no
                    # discovery, so the current projection count is unknown.
                    "models_available": None,
                    "setup_file_deleted": False,
                    "replaced_outdated_setup": False,
                    "original_setup_command": _original_setup_command(
                        "claude-code", claude_path
                    ),
                    "model_view": view,
                },
                pagination=None,
            )
        else:
            click.echo(f"Claude Code model list: {view}")
        return

    if ctx.obj["no_input"] and setup_file is None and api_key is None:
        raise click.UsageError(
            "Pass --setup-file or --api-key when using non-interactive mode, or set "
            f"{CONFIGURE_KEY_ENV} to keep the key out of shell history "
            f"and process listings. Create a key at {router_ui_url()}."
        )
    asks_model_view = "claude-code" in clients or (
        picked_from_menu and "codex" in clients
    )
    if asks_model_view and claude_models is None:
        current_view = (
            claude_code.model_view(claude_code.read_settings(claude_path), claude_path)
            if existing_claude
            else "compact"
        )
        claude_models = (
            _pick_claude_models(current_view, clients)
            if _can_prompt(ctx, fmt)
            else current_view
        )
    restore_clients = (
        "" if clients == all_clients else "".join(f" {client}" for client in clients)
    )
    target_phrase = (
        "Claude Cowork"
        if clients == (COWORK_CLIENT,)
        else "your coding agent"
        if len(clients) == 1
        else "your coding agents"
    )
    if fmt != "json":
        click.echo(f"Connecting Ramp Router to {target_phrase}")
    base_url = router_base_url()
    browser_acquired_key = False
    if setup_file is not None:
        # Parsed before the host check so a malformed file is classified as
        # the invalid input even where Cowork setup cannot run at all.
        base_url, api_key = claude_cowork.read_setup_file(setup_file)
    if COWORK_CLIENT in clients:
        # Reject a Router endpoint Cowork cannot safely call before the host
        # check runs: what the user got wrong outranks what the host lacks,
        # and both come before discovery puts the credential on the wire.
        claude_cowork.gateway_base_url(base_url)
        # Fail before a browser opens, a key is created, or discovery puts the
        # credential on the wire for a host where Cowork setup cannot succeed.
        claude_cowork.preflight()
    if setup_file is None and api_key is None and _can_prompt(ctx, fmt):
        api_key = _pick_stored_router_api_key()
    if setup_file is None and api_key is None:
        api_key = acquire_router_api_key(router_ui_url(), no_browser=no_browser)
        browser_acquired_key = True
    api_key = api_key.strip()
    if not api_key:
        # A user-supplied key was validated above, so what is empty here came
        # back from browser setup, a stored credential, or a setup file.
        raise click.ClickException("Ramp Router did not return an API key.")
    results = []
    failures = []
    cowork_note = None
    # Everything from here to the summary is silent work against the network
    # and each agent's files, so it is the stretch that needs to look alive.
    stop_spinner = (
        start_spinner(f"Setting up {target_phrase}")
        if fmt != "json" and not ctx.obj["quiet"]
        else (lambda: None)
    )
    try:
        models_by_client: dict[str, list[RouterModel]] = {}
        standard_clients = tuple(
            item for item in clients if item not in ("claude-code", COWORK_CLIENT)
        )
        if standard_clients:
            models = _fetch_models(
                api_key,
                base_url=base_url,
                wait_for_key=setup_file is not None or browser_acquired_key,
            )
            models_by_client.update(dict.fromkeys(standard_clients, models))
        if COWORK_CLIENT in clients:
            # Cowork accepts the ids from its own projection, same as every
            # other client, so discovery asks for that projection by name.
            models_by_client[COWORK_CLIENT] = _fetch_models(
                api_key,
                gateway_client=claude_cowork.GATEWAY_CLIENT,
                base_url=base_url,
                wait_for_key=setup_file is not None or browser_acquired_key,
            )
        if "claude-code" in clients:
            # The --claude-models choice is a presentation preference for
            # Claude Code's own picker; it is honored below by what gets
            # written into the settings, not by this fetch. This list exists
            # to choose and validate a default model id, so it must see every
            # callable model: ids are identical across views, but the compact
            # view can omit callable models (the GPT tiers, which duplicate
            # Claude Code's own tier rows), and a fetch that missed
            # DEFAULT_MODEL would silently hand fresh setups whatever model
            # happens to be listed first.
            models_by_client["claude-code"] = _fetch_models(
                api_key,
                claude_code_view=True,
                model_view_all=True,
                base_url=base_url,
                wait_for_key=setup_file is not None or browser_acquired_key,
            )
        for item in clients:
            models = models_by_client[item]
            if item == COWORK_CLIENT:
                try:
                    profile_path = claude_cowork.configure(api_key, base_url)
                except click.ClickException as exc:
                    failures.append(f"{AGENT_NAMES[item]}: {exc.message}")
                    continue
                preferred = next(
                    (
                        model
                        for model in models
                        if model.metadata.request_name == DEFAULT_MODEL
                    ),
                    models[0],
                )
                preferred_label = (
                    preferred.metadata.display_name
                    or preferred.metadata.request_name
                    or preferred.id
                )
                cowork_note = (
                    "Claude Desktop was restarted. "
                    f"Pick {preferred_label} in Cowork and send a message to "
                    "verify the connection."
                )
                results.append(
                    {
                        "client": item,
                        "config_path": str(profile_path),
                        "provider": ROUTER_PROVIDER,
                        # Cowork keeps its own model selection, so configure
                        # recommends a starting model rather than claiming to
                        # have set a default.
                        "recommended_model": preferred.id,
                        "models_available": len(models),
                        "setup_file_deleted": setup_file is not None,
                    }
                )
                continue
            try:
                configure_options = {"base_url": base_url}
                if item == "claude-code":
                    configure_options["claude_model_view"] = claude_models
                path, default_model, repaired = _configure_client(
                    item,
                    api_key,
                    models,
                    **configure_options,
                )
            except click.ClickException as exc:
                failures.append(f"{AGENT_NAMES[item]}: {exc.message}")
                continue
            result = {
                "client": item,
                "config_path": str(path),
                "provider": ROUTER_PROVIDER,
                "default_model": default_model,
                "models_available": len(models),
                "setup_file_deleted": setup_file is not None,
                "replaced_outdated_setup": repaired,
                "original_setup_command": _original_setup_command(item, path),
            }
            results.append(result)
        if setup_file is not None and not failures:
            try:
                setup_file.unlink()
            except OSError as exc:
                configured_names = _human_join(
                    [AGENT_NAMES[result["client"]] for result in results]
                )
                raise click.ClickException(
                    f"Ramp Router was configured for {configured_names}, but "
                    f"the setup file {setup_file} could not be deleted: {exc}"
                ) from None
    finally:
        # Stopped on the way out too, so a failed key does not leave the
        # terminal spinning with the cursor hidden.
        stop_spinner()

    notice = _profile_notice(results, restore_clients) if fmt != "json" else []
    if failures and fmt == "json":
        raise click.ClickException("Could not configure " + "; ".join(failures))
    if fmt == "json":
        payload = (
            results[0]
            if len(requested_clients) == 1 and results
            else {"clients": results}
        )
        if legacy_cowork_alias and results:
            # The deprecated alias is kept precisely for existing scripts, so
            # it keeps answering with the client name those scripts parse.
            payload = {**payload, "client": claude_cowork.GATEWAY_CLIENT}
        print_agent_json(payload, pagination=None)
    elif not failures:
        connected_names = _human_join(
            [AGENT_NAMES[result["client"]] for result in results]
        )
        model_count = results[0]["models_available"]
        model_label = "model" if model_count == 1 else "models"
        click.echo(f"Connected to: {connected_names}")
        if all(result["client"] == COWORK_CLIENT for result in results):
            # Cowork keeps its own model selection, so nothing was "added".
            click.echo(f"{model_count} {model_label} discovered.")
        else:
            click.echo(
                f"{model_count} {model_label} added. Start an agent and pick a model."
            )
        if cowork_note:
            click.echo(cowork_note)
        if notice:
            show_notice("NOTICE", notice)

    if failures:
        # Configuration is written before the agent-specific steps, so a
        # partial failure is recoverable; the user has no reason to guess that.
        raise click.ClickException(
            "Could not configure "
            + "; ".join(failures)
            + f". Run 'ramp router unconfigure{restore_clients}' to restore your "
            "previous settings."
        )
    if fmt != "json" and not notice:
        # The notice carries this line itself, alongside the profiles it is
        # restoring, so repeating it here would only be noise.
        click.echo(
            f"Run 'ramp router unconfigure{restore_clients}' to restore the previous "
            "settings."
        )
    if fmt != "json":
        remaining_credits = _fetch_remaining_credits(api_key, base_url=base_url)
        if remaining_credits is not None:
            click.secho(
                f"Router credits remaining: {_format_credits_usd(remaining_credits)}",
                fg=_RAMP_YELLOW,
            )


@router_group.command(
    "refresh",
    help="Reapply current Router setup to agents already configured with it",
)
@click.pass_context
def router_refresh(ctx: click.Context) -> None:
    clients = configured_router_clients()
    results = []
    failures = []
    models_by_request: dict[tuple[str, bool, str, bool], list[RouterModel]] = {}
    for client in clients:
        path = _client_config_path(client)
        try:
            api_key = _stored_router_api_key(client, path)
            # The endpoint paired with the stored credential wins for every
            # client, so a refresh run under a different environment — such
            # as one spawned by session sync — cannot rewrite a
            # non-production setup to the default Router.
            base_url = _stored_router_base_url(client, path) or router_base_url()
            claude_code_view = client == "claude-code"
            request_key = (api_key, claude_code_view, base_url)
            models = models_by_request.get(request_key)
            if models is None:
                # Always the exhaustive view, whatever picker view the user
                # chose: this list validates the configured default model id,
                # ids are identical across views, and the compact view can
                # omit callable models. The configured preference still flows
                # into the settings via claude_model_view below.
                models = (
                    _fetch_models(
                        api_key,
                        claude_code_view=True,
                        model_view_all=True,
                        base_url=base_url,
                    )
                    if claude_code_view
                    else _fetch_models(api_key, base_url=base_url)
                )
                models_by_request[request_key] = models
            selected_model = _configured_model(client, path)
            configure_options = {
                "base_url": base_url,
                "selected_model": selected_model,
            }
            if client == "claude-code":
                configure_options["claude_model_view"] = (
                    "all" if _configured_claude_model_view_all(path) else "compact"
                )
                configure_options["preserve_model_view_ownership"] = True
            _, default_model, _ = _configure_client(
                client, api_key, models, **configure_options
            )
        except click.ClickException as exc:
            failures.append(f"{CLIENT_NAMES[client]}: {exc.message}")
            continue
        results.append(
            {
                "client": client,
                "config_path": str(path),
                "default_model": default_model,
                "models_available": len(models),
            }
        )

    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
    if failures and fmt == "json":
        raise click.ClickException("Could not refresh " + "; ".join(failures))
    if fmt == "json":
        print_agent_json({"clients": results}, pagination=None)
    elif not clients:
        click.echo("Ramp Router is not configured in any coding agent.")
    else:
        for result in results:
            name = CLIENT_NAMES[result["client"]]
            click.echo(f"Refreshed the Ramp Router configuration for {name}.")

    if failures:
        raise click.ClickException("Could not refresh " + "; ".join(failures))


@router_group.command(
    "sync",
    hidden=True,
)
@click.option(
    "--hook",
    "hook_mode",
    is_flag=True,
    help="Session-start hook mode: quiet, cooldown-gated, and fail-open.",
)
@click.option(
    "--client",
    type=click.Choice(("claude-code", "codex")),
    default=None,
    help="Hosting agent; selects the structured hook output in --hook mode.",
)
@click.option(
    "--detached",
    "detached_mode",
    is_flag=True,
    hidden=True,
    help="Internal: the spawned background half of --hook.",
)
@click.pass_context
def router_sync(
    ctx: click.Context, hook_mode: bool, client: str | None, detached_mode: bool
) -> None:
    """Keep locally installed Router artifacts fresh.

    --hook is the session-start mode: cooldown-gated, detached, and fail-open.
    """
    if detached_mode:
        # The background half of --hook, off the session-start path, so
        # blocking on the network is fine here. The synchronous cache fetch
        # is what keeps "latest" fresh — and the update notice able to fire —
        # on machines where ramp only ever runs through hooks; the passive
        # daemon-thread check dies with short-lived processes.
        refresh_version_cache()
        ctx.invoke(router_refresh)
        return
    if hook_mode:
        _run_hook_sync(client)
        return
    raise click.UsageError("Use --hook for session-start sync or 'router refresh'.")


def _run_hook_sync(client: str | None) -> None:
    """The session-start path: never block, never fail, no model-visible output.

    Plain SessionStart stdout is injected into the model's context by both
    agents — which must never see the update notice, lest it run `ramp update`
    itself — so the only output ever emitted is Codex's notice as a
    structured systemMessage. The shutdown update notice is suppressed for
    the same reason, and failures go to the state-dir log.
    """
    suppress_next_update_notice()
    try:
        if os.environ.get(SYNC_OPT_OUT_ENV):
            return
        # claim_sync_slot re-checks dueness and advances the cooldown under
        # an exclusive lock, so concurrent session starts cannot stampede;
        # losers fall through silently. Spawned before the notice because the
        # spawn truncates the log to this run.
        if sync_is_due() and claim_sync_slot():
            spawn_detached_refresh()
        # Every session start, fast path included: these are user-only
        # channels reading cached state, so recurring per session is nudging,
        # not context spam, and it stops the moment the install is current.
        _emit_hook_update_notice(client)
    except Exception as exc:  # fail-open is this mode's contract
        log_failure(f"session sync failed: {exc!r}")


def _emit_hook_update_notice(client: str | None) -> None:
    """Surface a pending CLI update to the user, never the model."""
    # Keeps the statusline's pending-update file in step on every session
    # start, so an out-of-band upgrade clears the nudge immediately.
    sync_update_notice_file()
    notice = update_notice()
    if not notice:
        return
    if client != "codex":
        # claude-code emits nothing on any channel: its statusline renders a
        # persistent nudge from the pending-update file instead. An absent
        # --client also withholds — the registered commands always pass it,
        # and guessing a host's output contract risks plain text reaching
        # the model.
        if client is None:
            log_failure("hook invoked without --client; update notice withheld")
        return
    # systemMessage is a SessionStart output field Codex shows to the user —
    # with a "warning:" prefix, so the text must read naturally as one — and
    # never adds to model context. Never additionalContext or plain stdout,
    # which the model would see.
    click.echo(json.dumps({"systemMessage": notice}))


SUBAGENT_TIERS = ("sonnet", "opus", "haiku", "fable")
MINIMUM_TRUTHFUL_CLAUDE_CODE_VERSION = (2, 1, 118)


@router_group.command(
    "subagents", help="Show or set the models Claude Code sub-agents use"
)
@click.option(
    "--sonnet",
    metavar="MODEL",
    help="Model for sonnet-tier sub-agents, the tier Claude Code spawns by default.",
)
@click.option("--opus", metavar="MODEL", help="Model for opus-tier sub-agents.")
@click.option(
    "--haiku",
    metavar="MODEL",
    help="Model for haiku-tier sub-agents and Claude Code's background helpers.",
)
@click.option("--fable", metavar="MODEL", help="Model for fable-tier sub-agents.")
@click.option("--reset", is_flag=True, help="Remove all four tier overrides.")
@click.pass_context
def router_subagents(
    ctx: click.Context,
    sonnet: str | None,
    opus: str | None,
    haiku: str | None,
    fable: str | None,
    reset: bool,
) -> None:
    """Point Claude Code's sub-agent tiers at models Router serves.

    Claude Code dispatches some sub-agents with a tier alias (sonnet, opus,
    haiku, fable) that it resolves to a canonical Anthropic model id unless an
    ANTHROPIC_DEFAULT_<TIER>_MODEL setting names a different model. Router
    writes the matching _MODEL_NAME setting too, so Claude Code reports the
    real model instead of the Anthropic tier whose behavior it is using.
    Changes apply to running sessions.

    MODEL may be an exact id from Claude Code's model list, or any unique
    fragment of an id, request name, or display name.
    """
    picks = {
        tier: value
        for tier, value in (
            ("sonnet", sonnet),
            ("opus", opus),
            ("haiku", haiku),
            ("fable", fable),
        )
        if value is not None
    }
    if reset and picks:
        raise click.UsageError("--reset cannot be combined with a tier option.")
    path = _client_config_path("claude-code")
    # Read before anything else so an unconfigured setup fails with the state
    # message rather than a credential or network error.
    claude_code.read_state(path)
    if picks:
        _require_truthful_claude_code_model_names()
    # Reset only removes local keys, so it must work exactly when the remote
    # setup is unusable — an expired key or an unreachable Router must not
    # stop someone from clearing overrides that are failing every spawn.
    models: list[RouterModel] = []
    if picks or not reset:
        api_key = _stored_router_api_key("claude-code", path)
        # Tier overrides may point at any callable model, and the labels
        # rendered below must recognize every id a tier could hold, so this
        # fetch ignores the configured picker view and asks for the exhaustive
        # one. The compact view can omit callable models (the GPT tiers, which
        # duplicate Claude Code's own tier rows); resolving against it would
        # reject those ids as not found and mislabel existing overrides as no
        # longer served.
        models = _fetch_models(
            api_key,
            claude_code_view=True,
            model_view_all=True,
            base_url=_configured_claude_code_router_url(path),
        )
    models_by_id = {model.id: model for model in models}

    if picks or reset:
        tiers: dict[str, str | None] = (
            dict.fromkeys(SUBAGENT_TIERS)
            if reset
            else {
                tier: _resolve_subagent_model(value, models, f"--{tier}")
                for tier, value in picks.items()
            }
        )
        settings = _write_subagent_tiers(path, tiers, models)
    else:
        settings = claude_code.read_settings(path)

    environment = settings.get("env")
    environment = environment if isinstance(environment, dict) else {}
    tiers_payload: dict[str, dict] = {}
    for tier in SUBAGENT_TIERS:
        value = environment.get(claude_code.SUBAGENT_TIER_ENV_KEYS[tier])
        entry: dict = {"model": value}
        if value in models_by_id:
            entry["display_name"] = models_by_id[value].metadata.display_name
        elif value is not None:
            # A model this Router no longer lists still dispatches nothing, so
            # saying so beats letting sub-agents fail with model_not_found.
            entry["served"] = False
        tiers_payload[tier] = entry

    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
    if fmt == "json":
        print_agent_json(
            {"config_path": str(path), "tiers": tiers_payload}, pagination=None
        )
        return
    for tier in SUBAGENT_TIERS:
        entry = tiers_payload[tier]
        if entry["model"] is None:
            rendered = (
                "not set — Claude Code uses its Anthropic default, "
                "which this Router does not serve"
            )
        elif "display_name" in entry:
            rendered = f"{entry['display_name']} ({entry['model']})"
        else:
            rendered = f"{entry['model']} — no longer served by this Router"
        click.echo(f"{tier:<7} {rendered}")
    if not picks and not reset:
        click.echo(
            "Set with: ramp router subagents --sonnet MODEL "
            "[--opus MODEL] [--haiku MODEL] [--fable MODEL]"
        )


def _write_subagent_tiers(
    path: Path,
    tiers: dict[str, str | None],
    models: list[RouterModel],
) -> dict:
    """Apply tier overrides to the settings and state documents, atomically.

    Everything from reading the documents to writing them happens under the
    settings lock: both files are rewritten whole, so a concurrent update
    working from its own snapshot would otherwise be silently dropped by
    whichever write lands last, and the state receipt could end up describing
    the other update.
    """
    with claude_code.settings_lock(path):
        state = claude_code.read_state(path)
        settings = claude_code.read_settings(path)
        display_names = _subagent_tier_metadata(tiers, models)
        updated, new_state = claude_code.plan_subagent_update(
            settings,
            path,
            state,
            tiers,
            display_names=display_names,
            automatic_tiers=set(),
        )
        state_path = claude_code.state_path(path)
        # State first, so a successful settings write is always paired with
        # the managed values needed to undo it.
        try:
            _write_private_file(state_path, json.dumps(new_state, indent=2) + "\n")
        except OSError as exc:
            raise click.ClickException(
                f"Could not save Claude Code setup state to {state_path}: {exc}"
            ) from None
        try:
            _write_private_file(path, json.dumps(updated, indent=2) + "\n")
        except OSError as exc:
            _restore_private_file(state_path, json.dumps(state, indent=2) + "\n")
            raise click.ClickException(
                f"Could not update Claude Code settings {path}: {exc}"
            ) from None
    return updated


def _write_claude_model_view(path: Path, view: str) -> None:
    """Update only Claude's model projection preference and its receipt."""
    with claude_code.settings_lock(path):
        state = claude_code.read_state(path)
        original_state = state
        settings = claude_code.read_settings(path)
        settings, state = claude_code.plan_automatic_subagent_cleanup(
            settings, path, state
        )
        updated, new_state = claude_code.plan_model_view_update(
            settings, path, state, view
        )
        state_path = claude_code.state_path(path)
        try:
            _write_private_file(state_path, json.dumps(new_state, indent=2) + "\n")
        except OSError as exc:
            raise click.ClickException(
                f"Could not save Claude Code setup state to {state_path}: {exc}"
            ) from None
        try:
            _write_private_file(path, json.dumps(updated, indent=2) + "\n")
        except OSError as exc:
            _restore_private_file(
                state_path, json.dumps(original_state, indent=2) + "\n"
            )
            raise click.ClickException(
                f"Could not update Claude Code settings {path}: {exc}"
            ) from None


def _subagent_tier_metadata(
    tiers: dict[str, str | None],
    models: list[RouterModel],
) -> dict[str, str]:
    """Return truthful display names for every selected Router model."""
    models_by_id = {model.id: model for model in models}
    return {
        tier: models_by_id[model].metadata.display_name
        for tier, model in tiers.items()
        if model in models_by_id
    }


def _resolve_subagent_model(value: str, models: list[RouterModel], flag: str) -> str:
    """Resolve what the user typed to one id from Claude Code's model view.

    An exact id wins, then an exact request or display name, then a unique
    case-insensitive fragment of any of the three. Ambiguity is an error
    rather than a guess: sub-agents will quietly run whatever this resolves
    to, so a wrong pick costs real money without being seen.
    """
    if value in {model.id for model in models}:
        return value
    named = sorted(
        {
            model.id
            for model in models
            if value in (model.metadata.request_name, model.metadata.display_name)
        }
    )
    if len(named) == 1:
        return named[0]
    needle = value.lower()
    matches = named or sorted(
        {
            model.id
            for model in models
            if needle in model.id.lower()
            or needle in model.metadata.request_name.lower()
            or needle in model.metadata.display_name.lower()
        }
    )
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise click.BadParameter(
            f"{value!r} matches several models: {', '.join(matches)}. "
            "Use the exact id.",
            param_hint=flag,
        )
    raise click.BadParameter(
        f"{value!r} does not match a model this Router serves.", param_hint=flag
    )


@router_group.command(
    "unconfigure",
    help="Restore coding agents and Claude Cowork; omit names to choose interactively",
)
@click.argument(
    "clients",
    type=click.Choice(tuple(AGENT_NAMES), case_sensitive=False),
    nargs=-1,
)
@click.pass_context
def router_unconfigure(ctx: click.Context, clients: tuple[str, ...]) -> None:
    _run_unconfigure(ctx, clients=clients)


def _run_unconfigure(
    ctx: click.Context,
    *,
    clients: tuple[str, ...],
    legacy_cowork_alias: bool = False,
) -> None:
    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
    requested_clients = tuple(dict.fromkeys(clients))
    if not requested_clients and _can_prompt(ctx, fmt):
        # Offered from the receipts rather than the installed agents: this
        # removes a setup, so the only meaningful choices are the setups that
        # exist. With none, the list would be empty and the command falls
        # through to saying Router is not configured anywhere.
        receipted = _clients_with_a_receipt()
        cowork_configured = claude_cowork.state_path().exists()
        candidates = receipted
        if cowork_configured and "claude-code" not in candidates:
            # A Cowork receipt is the same kind of evidence: this CLI set
            # Cowork up, so removing that setup is a meaningful choice. The
            # Claude entry is the only interactive road to it, so the entry
            # is offered even where Claude Code itself holds no receipt.
            candidates = ("claude-code", *candidates)
        if candidates:
            # One Claude entry covers both Claude apps and the Codex entry
            # covers the Codex CLI and app, exactly as the configure picker
            # promises: the two menus show the same lines with the same
            # titles, so nobody meets Cowork as a separate thing to remove.
            titles = dict(AGENT_NAMES)
            titles["codex"] = "Codex (CLI + desktop app)"
            if cowork_configured:
                titles["claude-code"] = "Claude (CLI + desktop app)"
            selected = _pick_clients(
                "Which coding agents do you want to remove Ramp Router from?",
                candidates,
                titles=titles,
            )
            if cowork_configured and "claude-code" in selected:
                # The Claude entry removes both Claude setups without another
                # question, but only the setups that exist: a Claude Code
                # without a receipt has nothing to restore, and expanding to
                # it would end the run with a failure it did not earn.
                claude_targets = (
                    ("claude-code", COWORK_CLIENT)
                    if "claude-code" in receipted
                    else (COWORK_CLIENT,)
                )
                selected = tuple(
                    dict.fromkeys(
                        expanded
                        for client in selected
                        for expanded in (
                            claude_targets if client == "claude-code" else (client,)
                        )
                    )
                )
            requested_clients = selected
    # A run that names no agents keeps meaning the coding agents, exactly as
    # it does for configure: Cowork restarts Claude Desktop, so it is removed
    # only when picked or named. This also keeps every previously generated
    # "run 'ramp router unconfigure'" recovery hint meaning what it meant
    # when it was printed.
    clients = requested_clients or tuple(CLIENT_NAMES)
    target_phrase = (
        "Claude Cowork"
        if clients == (COWORK_CLIENT,)
        else "your coding agent"
        if len(clients) == 1
        else "your coding agents"
    )
    results = []
    failures = []
    stop_spinner = (
        start_spinner(f"Restoring {target_phrase}")
        if fmt != "json" and not ctx.obj["quiet"]
        else (lambda: None)
    )
    try:
        for item in clients:
            if item == COWORK_CLIENT:
                try:
                    claude_cowork.unconfigure()
                except click.ClickException as exc:
                    failures.append(f"{AGENT_NAMES[item]}: {exc.message}")
                    continue
                results.append(
                    {
                        "client": item,
                        "config_path": str(claude_cowork.state_path()),
                        "removed": True,
                    }
                )
                continue
            path = _client_config_path(item)
            if (
                not requested_clients
                and not (path.parent / "ramp-router-state.json").exists()
            ):
                continue
            try:
                _unconfigure_client(item, path)
            except click.ClickException as exc:
                failures.append(f"{AGENT_NAMES[item]}: {exc.message}")
                continue
            results.append({"client": item, "config_path": str(path), "removed": True})
    finally:
        stop_spinner()

    if not results and not failures:
        raise click.ClickException("Ramp Router is not configured in any coding agent.")
    if failures and fmt == "json":
        raise click.ClickException("Could not unconfigure " + "; ".join(failures))
    if fmt == "json":
        if legacy_cowork_alias and results:
            # The deprecated alias is kept precisely for existing scripts, so
            # it keeps answering with the payload those scripts parse.
            payload = {"client": claude_cowork.GATEWAY_CLIENT, "restored": True}
        else:
            payload = (
                results[0]
                if len(requested_clients) == 1 and results
                else {"clients": results}
            )
        print_agent_json(payload, pagination=None)
    elif results:
        restored_names = _human_join(
            [AGENT_NAMES[result["client"]] for result in results]
        )
        click.echo(
            "Removed Ramp Router and restored your previous settings for: "
            f"{restored_names}."
        )
        if any(result["client"] == COWORK_CLIENT for result in results):
            click.echo("Claude Desktop was restarted.")
    if failures:
        raise click.ClickException("Could not unconfigure " + "; ".join(failures))


def _unconfigure_client(client: str, path: Path) -> None:
    if client == "claude-code":
        _unconfigure_claude_code(path)
        return
    if client != "codex":
        _unconfigure_json_client(client, path)
        return
    with codex_config_lock(path):
        _unconfigure_codex(path)


def _unconfigure_codex(path: Path) -> None:
    state_path = path.parent / "ramp-router-state.json"
    if not state_path.exists():
        raise click.ClickException("Ramp Router is not configured in Codex.")

    existing, _ = _read_codex_config(path)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        root_values = state["root"]
        previous_provider = state["provider"]
        sessions = state.get("sessions", [])
        original_profile = state.get(CODEX_ORIGINAL_PROFILE_STATE_KEY)
        original_digest = state.get(CODEX_ORIGINAL_PROFILE_DIGEST_KEY)
        original_catalog = state.get(CODEX_ORIGINAL_CATALOG_STATE_KEY)
        original_catalog_digest = state.get(CODEX_ORIGINAL_CATALOG_DIGEST_KEY)
        router_catalog_managed = state.get(CODEX_ROUTER_CATALOG_MANAGED_KEY)
        router_catalog_digest = state.get(CODEX_ROUTER_CATALOG_DIGEST_KEY)
        original_instructions = state.get(CODEX_ORIGINAL_INSTRUCTIONS_STATE_KEY)
        original_instructions_digest = state.get(CODEX_ORIGINAL_INSTRUCTIONS_DIGEST_KEY)
        cost_hook_managed = state.get(CODEX_COST_HOOK_MANAGED_KEY)
        recorded_sync_hook = state.get(CODEX_SYNC_HOOK_STATE_KEY)
        preexisting_router_sessions = state.get(
            CODEX_PREEXISTING_ROUTER_SESSIONS_STATE_KEY
        )
        if not isinstance(root_values, dict) or not isinstance(previous_provider, list):
            raise ValueError
        if not isinstance(sessions, list) or (
            preexisting_router_sessions is not None
            and (
                not isinstance(preexisting_router_sessions, list)
                or not all(
                    isinstance(item, str) for item in preexisting_router_sessions
                )
            )
        ):
            raise ValueError
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        raise click.ClickException(
            f"Could not read Ramp Router setup state from {state_path}."
        ) from None

    chunks = _drop_recorded_sync_hook_chunk(
        _config_chunks(existing), recorded_sync_hook
    )
    chunks[0] = [_render_root_config(chunks[0], root_values)]
    preserved = "".join(
        "".join(chunk)
        for chunk in chunks
        if not chunk
        or not (
            _OWNED_CODEX_TABLE.match(chunk[0])
            # Only the receipt makes a matching group ours to remove; the
            # sync group must match its recorded rendering exactly.
            or (cost_hook_managed is True and _is_owned_cost_hook_chunk(chunk))
        )
    ).rstrip()
    restored_provider = "".join(previous_provider).strip()
    updated = (
        f"{preserved}\n\n{restored_provider}\n"
        if restored_provider
        else f"{preserved}\n"
        if preserved
        else ""
    )

    if updated:
        try:
            tomllib.loads(updated)
        except tomllib.TOMLDecodeError as exc:
            raise click.ClickException(
                f"Could not restore Codex config {path}: {exc}"
            ) from None
    try:
        _restore_codex_sessions(path.parent, sessions)
    except (OSError, click.ClickException) as exc:
        _rollback_codex_sessions(path.parent, sessions)
        raise click.ClickException(
            f"Could not restore Codex config {path}: {exc}"
        ) from None
    try:
        _write_private_file(path, updated)
    except OSError as exc:
        _rollback_codex_sessions(path.parent, sessions)
        raise click.ClickException(
            f"Could not restore Codex config {path}: {exc}"
        ) from None
    try:
        if preexisting_router_sessions is None:
            # Receipts from before this snapshot existed cannot distinguish a
            # Router thread that predated configure from one created during it.
            # Preserve every unknown Router thread rather than risk silently
            # reassigning user-owned history.
            preexisting_router_sessions = _preexisting_codex_router_session_ids(
                path.parent
            )
        _reclaim_codex_router_sessions(
            path.parent,
            sessions,
            _codex_restored_provider(root_values),
            preexisting_router_sessions=preexisting_router_sessions,
        )
    except (OSError, sqlite3.Error, click.ClickException) as exc:
        # Raised rather than warned so the receipt below survives. A locked
        # session index is the ordinary cause and it clears on its own, but
        # without the receipt there is nothing left to retry from and those
        # conversations stay hidden for good.
        message = exc.message if isinstance(exc, click.ClickException) else exc
        raise click.ClickException(
            f"Restored the Codex configuration, but could not move the "
            f"conversations created while Ramp Router was configured: {message}. "
            "Close Codex and run this command again to finish."
        ) from None
    try:
        router_catalog = path.parent / CODEX_ROUTER_CATALOG
        if router_catalog_managed is not False and _is_unmodified(
            router_catalog, router_catalog_digest
        ):
            router_catalog.unlink(missing_ok=True)
        # The key outlives the config unless it is removed here, leaving a
        # live credential on disk after the user asked for it to be undone.
        (path.parent / "ramp-router-key").unlink(missing_ok=True)
        (path.parent / "ramp-router-instructions.md").unlink(missing_ok=True)
        # Keep the script while anything still references it. Substring on
        # purpose: false positives just keep a small file on disk.
        if cost_hook_managed is True and CODEX_COST_HOOK not in updated:
            (path.parent / CODEX_COST_HOOK).unlink(missing_ok=True)
        # The values it holds are back at the root, so the profile would only
        # be a stale duplicate of the default. Removed by the name recorded at
        # configure time, and only while it still holds what was written then:
        # a profile the user has since edited is theirs to keep, and the
        # instructions file it points at has to stay with it.
        if isinstance(original_profile, str) and original_profile:
            profile_path = path.parent / Path(original_profile).name
            if _is_unmodified(profile_path, original_digest):
                profile_path.unlink(missing_ok=True)
                if isinstance(original_instructions, str) and original_instructions:
                    original_instructions_path = (
                        path.parent / Path(original_instructions).name
                    )
                    if _is_unmodified(
                        original_instructions_path, original_instructions_digest
                    ):
                        original_instructions_path.unlink(missing_ok=True)
                if isinstance(original_catalog, str) and original_catalog:
                    catalog_path = path.parent / Path(original_catalog).name
                    if _is_unmodified(catalog_path, original_catalog_digest):
                        catalog_path.unlink(missing_ok=True)
        state_path.unlink()
    except OSError as exc:
        raise click.ClickException(
            f"Restored Codex config but could not remove Ramp Router state: {exc}"
        ) from None


def _codex_config_path() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return codex_home.expanduser() / "config.toml"


@contextmanager
def codex_config_lock(path: Path):
    """Serialize every read-modify-write of the Codex config.toml.

    The file is rewritten whole from an in-memory snapshot, so every writer —
    `ramp router configure codex`/`refresh`/`unconfigure` and
    `ramp codex compaction` — must share this one lock or the last replacement
    silently drops the other writer's change.
    """
    with claude_code.advisory_lock(path.parent / ".ramp-codex-config.lock"):
        yield


def _statusline_origin(base_url: str | None = None) -> str:
    """The origin serving the cost scripts and their session-usage endpoint.

    The data plane behind the agents' base URLs serves neither, which is why
    ROUTER_BASE_URL is written explicitly. A base-URL override names a
    single-origin deployment, so the same host serves everything there.
    """
    base = (base_url or router_base_url()).rstrip("/")
    if base == DEFAULT_ROUTER_BASE_URL:
        return router_ui_url()
    return base.removesuffix("/v1").rstrip("/")


def router_ui_url() -> str:
    """Return the Router browser origin, allowing local end-to-end testing."""
    return os.environ.get(ROUTER_UI_URL_ENV, ROUTER_UI_URL).rstrip("/")


def _fetch_remaining_credits(
    api_key: str, *, base_url: str | None = None
) -> Decimal | None:
    """Fetch the key owner's remaining Router credits, or None.

    The credits line is an extra, so every failure — a Router without the
    balance endpoint yet, a network problem, an owner without a billing
    account — reads as "no line" rather than an error at the end of an
    otherwise successful configure.
    """
    url = f"{_statusline_origin(base_url)}/session-usage/usage/balance"
    try:
        response = httpx.get(
            url, headers={"Authorization": f"Bearer {api_key}"}, timeout=5
        )
        if response.status_code != 200:
            return None
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    balance = payload.get("balance") if isinstance(payload, dict) else None
    if not isinstance(balance, dict):
        return None
    remaining = balance.get("remaining_credit_usd")
    if not isinstance(remaining, str):
        return None
    try:
        parsed = Decimal(remaining)
    except InvalidOperation:
        return None
    # Decimal accepts "NaN" and "Infinity", which the formatter cannot
    # compare or render; a balance that is not a finite number is no balance.
    return parsed if parsed.is_finite() else None


def _format_credits_usd(value: Decimal) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _install_claude_code_statusline(
    settings_file: Path, base_url: str | None = None
) -> str | None:
    """Install Router's cost status line for Claude Code, when possible.

    Returns the command to configure, or None when the status line cannot be
    installed here. A failure is a notice rather than an error: the status
    line is an extra, and it must never fail an otherwise working configure.
    """
    url = f"{_statusline_origin(base_url)}/claude-code-statusline"
    script_path = claude_code.statusline_path(settings_file)
    if not _install_cost_script(url, script_path, _skip_statusline):
        return None
    return claude_code.statusline_command(settings_file)


def _skip_statusline(reason: str) -> None:
    # Written to stderr so agent-mode JSON on stdout stays parseable.
    click.echo(f"Skipping the Claude Code Router status line: {reason}.", err=True)


def _install_cost_script(
    url: str, script_path: Path, skip: Callable[[str], None]
) -> bool:
    """Download one Router cost script, refreshing it on every run.

    A failure warns through the caller's skip notice and keeps the old copy.
    """
    if not _cost_script_runtime_available(skip):
        return False
    try:
        response = httpx.get(url, headers={"Accept": "text/plain"}, timeout=10)
        response.raise_for_status()
        script = response.text
    except (httpx.HTTPError, ValueError):
        script = ""
    # The shebang check rejects WAF block pages and misrouted responses.
    if not script.startswith("#!"):
        skip(f"it could not be downloaded from {url}")
        return False
    return _write_cost_script(script, script_path, skip)


def _cost_script_runtime_available(skip: Callable[[str], None]) -> bool:
    """Report whether this platform can execute the bundled Python scripts."""
    if os.name == "nt":
        # The python3 shebang means nothing to Windows' command interpreter.
        skip("it is not supported on Windows")
        return False
    if not shutil.which("python3"):
        skip("python3 was not found on PATH")
        return False
    return True


def _write_cost_script(
    script: str, script_path: Path, skip: Callable[[str], None]
) -> bool:
    """Atomically install a trusted cost script and make it executable."""
    try:
        script_path.parent.mkdir(parents=True, exist_ok=True)
        # Temp file + rename, so a partial download can't clobber a working script.
        _write_private_file(script_path, script)
        script_path.chmod(0o755)
    except OSError as exc:
        skip(str(exc))
        return False
    return True


def _install_codex_cost_hook(path: Path, base_url: str | None = None) -> str | None:
    """Install the release-pinned Codex hook and return its Stop command."""
    hook_path = path.parent / CODEX_COST_HOOK
    if not _cost_script_runtime_available(_skip_codex_cost_hook):
        return None
    if not _write_cost_script(CODEX_COST_HOOK_SCRIPT, hook_path, _skip_codex_cost_hook):
        return None
    return _codex_cost_hook_command(path.parent, base_url)


def _skip_codex_cost_hook(reason: str) -> None:
    # Written to stderr so agent-mode JSON on stdout stays parseable.
    click.echo(f"Skipping the Codex Router cost hook: {reason}.", err=True)


def _codex_cost_hook_command(home: Path, base_url: str | None = None) -> str:
    """The shell command Codex runs on Stop, with Router credentials wired in."""
    command, args = _codex_key_command(home / "ramp-router-key")
    read_key = " ".join(shlex.quote(part) for part in (command, *args))
    # The key is read at run time from the private key file, never written
    # into config.toml; a missing file degrades to the hook's silent path.
    return (
        f'ROUTER_API_KEY="$({read_key} 2>/dev/null)" '
        f"ROUTER_BASE_URL={shlex.quote(_statusline_origin(base_url))} "
        f"{shlex.quote(str(home / CODEX_COST_HOOK))}"
    )


def _render_codex_cost_hook_group(command: str) -> str:
    """Render the [[hooks.Stop]] matcher group that registers the cost hook."""
    return f"""[[hooks.Stop]]
hooks = [{{ type = "command", command = {json.dumps(command)} }}]
"""


# A leading NAME= word is an environment assignment, not the executable.
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _command_runs_cost_hook(command: str) -> bool:
    """Report whether a hook command's executable is the managed script."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    for token in tokens:
        # Skip env wrappers and their assignments; an unparsed env option
        # bails out as not-ours rather than risk claiming a user hook.
        if _ENV_ASSIGNMENT.match(token) or os.path.basename(token) == "env":
            continue
        if token.startswith("-"):
            return False
        # Only the executable counts: a path passed as an argument, or a
        # wrapper that merely resembles the name, is a reference.
        return os.path.basename(token) == CODEX_COST_HOOK
    return False


def _hook_group_commands(group: object) -> Iterator[str | None]:
    """Yield each hook entry's command in one parsed hook group, None if unreadable."""
    entries = group.get("hooks") if isinstance(group, dict) else None
    if not isinstance(entries, list):
        return
    for entry in entries:
        command = entry.get("command") if isinstance(entry, dict) else None
        yield command if isinstance(command, str) else None


def _is_owned_cost_hook_chunk(chunk: list[str]) -> bool:
    """Report whether one config chunk is a Stop group holding only the hook.

    TODO(ramp-cli#519 review): receipt-record the rendering like the sync group.
    """
    if not chunk or not _CODEX_STOP_HOOK_TABLE.match(chunk[0]):
        return False
    try:
        # The header line makes the chunk standalone-valid TOML.
        parsed = tomllib.loads("".join(chunk))
    except tomllib.TOMLDecodeError:
        return False
    hooks = parsed.get("hooks")
    groups = hooks.get("Stop") if isinstance(hooks, dict) else None
    if not isinstance(groups, list) or len(groups) != 1:
        return False
    commands = list(_hook_group_commands(groups[0]))
    # Shared groups are the user's; solo ones are replaceable, ours or
    # hand-written, because a duplicate would answer every Stop event twice.
    return bool(commands) and all(
        command is not None and _command_runs_cost_hook(command) for command in commands
    )


def _codex_cost_hook_registered(config: dict) -> bool:
    """Report whether any Stop hook in the parsed config runs the script."""
    hooks = config.get("hooks")
    groups = hooks.get("Stop") if isinstance(hooks, dict) else None
    if not isinstance(groups, list):
        return False
    return any(
        command is not None and _command_runs_cost_hook(command)
        for group in groups
        for command in _hook_group_commands(group)
    )


def _register_codex_cost_hook(path: Path, config: str, command: str) -> str:
    """Append the hook's Stop group unless something already registers it."""
    try:
        if _codex_cost_hook_registered(tomllib.loads(config)):
            return config
    except tomllib.TOMLDecodeError:
        # The caller validates and reports against the document it writes.
        return config
    registered = f"{config}\n{_render_codex_cost_hook_group(command)}"
    try:
        tomllib.loads(registered)
    except tomllib.TOMLDecodeError:
        # E.g. an inline hooks table cannot take another [[hooks.Stop]] group;
        # the hook is an extra, so the user's layout wins.
        _skip_codex_cost_hook(
            f"the existing hooks in {path} could not take another entry"
        )
        return config
    return registered


def _render_codex_sync_hook_group(command: str) -> str:
    """Render the [[hooks.SessionStart]] group that keeps artifacts fresh.

    Deliberately no feature flag: hooks are stable and enabled by default
    since Codex 0.124.0, and older builds ignore an unflagged [[hooks.*]]
    group — the same exposure as the [[hooks.Stop]] cost hook. Writing
    `features.codex_hooks` ourselves would opt pre-0.124 users into an engine
    Codex marked under development, so a too-old Codex simply ignores this.
    """
    return f"""[[hooks.SessionStart]]
matcher = "startup|resume"
hooks = [{{ type = "command", command = {json.dumps(command)} }}]
"""


def _command_runs_session_sync(command: str) -> bool:
    return command_runs_session_sync(command)


def _sync_hook_chunk_digest(chunk_text: str) -> str:
    """Digest one hook group, ignoring the blank lines chunking absorbs."""
    return _content_digest(chunk_text.strip() + "\n")


def _is_recorded_sync_hook_chunk(chunk: list[str], recorded: object) -> bool:
    """Report whether one chunk is exactly the sync group the receipt records.

    Only that rendering is ours; user-authored or user-edited groups never
    match, and the user-managed sentinel matches nothing.
    """
    if not chunk or not _CODEX_SESSION_START_HOOK_TABLE.match(chunk[0]):
        return False
    if not isinstance(recorded, str) or not recorded:
        return False
    return _sync_hook_chunk_digest("".join(chunk)) == recorded


def _drop_recorded_sync_hook_chunk(
    chunks: list[list[str]], recorded: object
) -> list[list[str]]:
    """Drop one receipt-owned group, preserving byte-identical user copies."""
    preserved = []
    consumed = False
    for chunk in chunks:
        if not consumed and _is_recorded_sync_hook_chunk(chunk, recorded):
            consumed = True
            continue
        preserved.append(chunk)
    return preserved


def _codex_sync_hook_registered(config: dict) -> bool:
    """Report whether any SessionStart hook in the parsed config runs the sync."""
    hooks = config.get("hooks")
    groups = hooks.get("SessionStart") if isinstance(hooks, dict) else None
    if not isinstance(groups, list):
        return False
    # Any command actually invoking the sync makes registration stand down.
    # Ownership is only the receipt-recorded rendering; see
    # _is_recorded_sync_hook_chunk.
    return any(
        command is not None and _command_runs_session_sync(command)
        for group in groups
        for command in _hook_group_commands(group)
    )


def _register_codex_sync_hook(
    path: Path, config: str, command: str, receipt: dict
) -> tuple[str, bool]:
    """Register the managed SessionStart group; return (config, receipt changed).

    The recorded group was already dropped, so an ordinary run appends a fresh
    rendering and records its digest — which is what repairs a stale ramp
    path. A surviving group that already invokes the sync is user-authored (or
    user-edited): it is left untouched, nothing is added, and the skip is
    recorded as user-managed.
    """
    try:
        already_registered = _codex_sync_hook_registered(tomllib.loads(config))
    except tomllib.TOMLDecodeError:
        # The caller validates and reports against the document it writes.
        return config, False
    if already_registered:
        if receipt.get(CODEX_SYNC_HOOK_STATE_KEY) == SYNC_HOOK_USER_MANAGED:
            return config, False
        receipt[CODEX_SYNC_HOOK_STATE_KEY] = SYNC_HOOK_USER_MANAGED
        return config, True
    rendered = _render_codex_sync_hook_group(command)
    registered = f"{config}\n{rendered}"
    try:
        tomllib.loads(registered)
    except tomllib.TOMLDecodeError:
        # E.g. an inline hooks table cannot take another group; the hook is
        # an extra, so the user's layout wins. Stderr keeps agent-mode JSON
        # on stdout parseable.
        click.echo(
            f"Skipping the Codex session sync hook: the existing hooks in "
            f"{path} could not take another entry.",
            err=True,
        )
        return config, False
    digest = _sync_hook_chunk_digest(rendered)
    if receipt.get(CODEX_SYNC_HOOK_STATE_KEY) == digest:
        return registered, False
    receipt[CODEX_SYNC_HOOK_STATE_KEY] = digest
    return registered, True


def _configure_claude_code(
    path: Path,
    api_key: str,
    models: list[RouterModel],
    *,
    base_url: str | None = None,
    selected_model: str | None = None,
    model_view: str = "compact",
    preserve_model_view_ownership: bool = False,
) -> str:
    """Write Claude Code's gateway settings and record how to undo them.

    The default model is read from Router's Claude Code view of /v1/models
    rather than derived locally, because that view is what Claude Code's picker
    shows and a model written here must be an id it will recognize.
    """
    _require_truthful_claude_code_model_names()
    model = _claude_code_default_model(models, selected_model=selected_model)
    statusline = _install_claude_code_statusline(path, base_url)
    # Read after the network calls, not before. The settings are rewritten as a
    # whole document, so anything Claude Code or the user changed while the
    # requests were in flight would be silently discarded by a stale snapshot.
    # The lock covers the whole read-modify-write for the same reason: another
    # writer landing between this read and the writes below would be discarded,
    # or left paired with a state receipt describing a different setup.
    with claude_code.settings_lock(path):
        settings = claude_code.read_settings(path)
        state_path = claude_code.state_path(path)
        previous_state = claude_code.read_state(path) if state_path.exists() else None
        updated, state = claude_code.plan_configuration(
            settings,
            path,
            _router_host(base_url),
            api_key,
            model,
            usage_base_url=_statusline_origin(base_url),
            statusline=statusline,
            model_view_all=model_view == "all",
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        if previous_state is not None:
            # Keep the original snapshot, but advance the values that identify
            # what Router currently owns; keys that snapshot never captured
            # take the fresh one taken before this write.
            updated = claude_code.restore_legacy_connector_preference(
                updated, previous_state
            )
            state = claude_code.merge_states(
                previous_state,
                state,
                preserve_model_view_ownership=preserve_model_view_ownership,
            )
        updated, state = claude_code.plan_automatic_subagent_cleanup(
            updated, path, state
        )
        # In the shared configure/refresh path so existing setups gain the
        # hook and refresh repairs a stale ramp path. None (Windows or no
        # resolvable entrypoint) registers nothing.
        sync_hook_command = session_sync_hook_command("claude-code")
        if sync_hook_command is not None:
            updated, state = claude_code.plan_session_start_hook(
                updated, state, sync_hook_command
            )
        # Rendered before the receipt is written so the receipt can record
        # what the overlay holds, which is what lets unconfigure tell its own
        # file from one the user has edited. Planned from the merged state, so
        # it describes what the user had before Router rather than what a
        # previous configure left behind.
        original_settings = claude_code.original_settings_path(path)
        overlay_body = (
            json.dumps(claude_code.plan_original_settings(state), indent=2) + "\n"
        )
        overlay_claim = _claim_escape_artifact(
            original_settings,
            state,
            claude_code.ORIGINAL_SETTINGS_STATE_KEY,
            claude_code.ORIGINAL_SETTINGS_DIGEST_KEY,
        )
        if overlay_claim is None:
            original_settings = None
        else:
            state[claude_code.ORIGINAL_SETTINGS_STATE_KEY] = original_settings.name
            state[claude_code.ORIGINAL_SETTINGS_DIGEST_KEY] = _content_digest(
                overlay_body
            )
        # Written first so a successful settings write is always paired with
        # the managed values needed to undo it. A failed settings write puts
        # the prior state back below.
        try:
            _write_private_file(state_path, json.dumps(state, indent=2) + "\n")
        except OSError as exc:
            raise click.ClickException(
                f"Could not save Claude Code setup state to {state_path}: {exc}"
            ) from None
        try:
            _write_private_file(path, json.dumps(updated, indent=2) + "\n")
        except OSError as exc:
            previous_state_contents = (
                None
                if previous_state is None
                else json.dumps(previous_state, indent=2) + "\n"
            )
            _restore_private_file(state_path, previous_state_contents)
            raise click.ClickException(
                f"Could not update Claude Code settings {path}: {exc}"
            ) from None
        if overlay_claim in ("create", "replace"):
            # Written last, and only after the settings landed, because it is
            # an extra: failing to write it must not fail a setup that is
            # otherwise complete. New files are created exclusively; existing
            # files are rewritten only with receipt provenance above.
            try:
                if overlay_claim == "create":
                    _write_new_private_file(original_settings, overlay_body)
                else:
                    _write_private_file(original_settings, overlay_body)
            except OSError as exc:
                click.echo(
                    "Could not save your previous Claude Code settings to "
                    f"{original_settings}: {exc}.",
                    err=True,
                )
    return model


def _require_truthful_claude_code_model_names() -> None:
    """Refuse a client that would label Router models as Anthropic models."""
    executable = shutil.which("claude")
    if executable is None:
        # Configuration may be prepared before Claude Code is installed. A
        # future install will get the current release, while there is no
        # executable here that can misreport a model now.
        return
    version = _claude_code_version(executable)
    minimum = MINIMUM_TRUTHFUL_CLAUDE_CODE_VERSION
    if version is None:
        raise click.ClickException(
            "Could not determine the installed Claude Code version. "
            f"Claude Code {'.'.join(map(str, minimum))} or newer is required "
            "to show Router models by their real names."
        )
    if version < minimum:
        raise click.ClickException(
            f"Claude Code {'.'.join(map(str, minimum))} or newer is required "
            "to show Router models by their real names. Update Claude Code, "
            "then run this command again."
        )


@lru_cache(maxsize=None)
def _claude_code_version(executable: str) -> tuple[int, int, int] | None:
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", result.stdout)
    if result.returncode != 0 or match is None:
        return None
    return tuple(int(part) for part in match.groups())


def _unconfigure_claude_code(path: Path) -> None:
    # Read once before taking the lock: acquiring it creates the settings
    # directory and lock file, which a machine with no Claude Code setup
    # should not gain from a command that then refuses to run.
    claude_code.read_state(path)
    # Locked so a concurrent configure or tier update cannot land between the
    # restoration read and the receipt removal: it would either be discarded
    # by the restored document or survive with its receipt deleted, leaving
    # Router configured and impossible to restore. The receipt is re-read
    # under the lock; the preflight above cannot speak for this moment.
    with claude_code.settings_lock(path):
        state = claude_code.read_state(path)
        settings = claude_code.read_settings(path)
        restored = claude_code.plan_restoration(settings, path, state)
        restored = claude_code.plan_session_start_hook_removal(restored, state)
        _write_private_file(path, json.dumps(restored, indent=2) + "\n")
        # The script lives under a name this command manages, so it goes even
        # when the statusLine setting itself was the user's and stays.
        claude_code.statusline_path(path).unlink(missing_ok=True)
        # These values are back in the settings file, so the overlay would only
        # be a stale duplicate. Removed by the name recorded at configure time,
        # and only while it still holds what was written then: an overlay the
        # user has since edited is theirs to keep.
        overlay = claude_code.original_settings_path(path)
        if state.get(claude_code.ORIGINAL_SETTINGS_STATE_KEY) and _is_unmodified(
            overlay, state.get(claude_code.ORIGINAL_SETTINGS_DIGEST_KEY)
        ):
            overlay.unlink(missing_ok=True)
        claude_code.state_path(path).unlink(missing_ok=True)


def _claude_code_default_model(
    models: list[RouterModel], *, selected_model: str | None = None
) -> str:
    """Choose a default from the ids Claude Code itself will see.

    Claude Code sends back the exact id from its picker, so the value written
    here must come from its own view of the list. That view publishes
    compatibility aliases, and each entry names the canonical model behind it,
    which is how the ordinary choice of default is matched to an alias.
    """
    if selected_model and selected_model in {model.id for model in models}:
        return selected_model
    canonical = [model.metadata.request_name for model in models]
    preferred = DEFAULT_MODEL if DEFAULT_MODEL in canonical else canonical[0]
    for model in models:
        if model.metadata.request_name == preferred:
            return model.id
    return preferred


def _router_host(base_url: str | None = None) -> str:
    """Return the host root, since Claude Code appends /v1 itself."""
    return (base_url or router_base_url()).removesuffix("/v1").rstrip("/")


def _configured_claude_code_router_url(path: Path) -> str:
    """Return the Router URL Claude Code was configured to use.

    An explicit environment override still wins. Otherwise follow the saved
    Claude Code gateway so commands after a non-production configure do not
    send that gateway's key to the production Router.
    """
    if any(os.environ.get(name, "").strip() for name in BASE_URL_ENVS):
        return router_base_url()
    settings = claude_code.read_settings(path)
    environment = settings.get("env")
    base_url = (
        environment.get("ANTHROPIC_BASE_URL") if isinstance(environment, dict) else None
    )
    if isinstance(base_url, str) and base_url.strip():
        host = base_url.strip().rstrip("/").removesuffix("/v1")
        return f"{host}/v1"
    return router_base_url()


def _configured_claude_model_view_all(path: Path) -> bool:
    """Use the expanded projection only for an existing explicit Claude setup."""
    if not claude_code.state_path(path).is_file():
        return False
    return claude_code.model_view(claude_code.read_settings(path), path) == "all"


def _json_config_path(client: str) -> Path:
    if client == "opencode":
        configured = os.environ.get("OPENCODE_CONFIG")
        if configured:
            return Path(configured).expanduser()
        config_home = Path(
            os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
        ).expanduser()
        return config_home / "opencode" / "opencode.json"
    pi_home = Path(os.environ.get("PI_CODING_AGENT_DIR", Path.home() / ".pi" / "agent"))
    return pi_home.expanduser() / "settings.json"


def _opencode_tui_config_path() -> Path:
    """Locate the TUI plugin config, exactly as OpenCode's TUI resolves it.

    Deliberately not derived from OPENCODE_CONFIG: the TUI does not consult
    that variable either, so following it would write a file the TUI never
    reads.
    """
    configured = os.environ.get("OPENCODE_TUI_CONFIG")
    if configured:
        return Path(configured).expanduser()
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    ).expanduser()
    return config_home / "opencode" / "tui.json"


def _client_config_path(client: str) -> Path:
    """Locate one client's configuration file."""
    if client == "claude-code":
        return claude_code.settings_path()
    return _codex_config_path() if client == "codex" else _json_config_path(client)


def configured_router_clients() -> tuple[str, ...]:
    """Return only clients this CLI previously configured for Router.

    The receipt is stronger evidence than the presence of an agent directory,
    but a first-time configure can be interrupted after creating it and before
    writing the credential. Requiring both keeps updates from enrolling that
    partial setup and failing while trying to refresh it.
    """
    configured = []
    for client in CLIENT_NAMES:
        path = _client_config_path(client)
        if not (path.parent / "ramp-router-state.json").is_file():
            continue
        try:
            _stored_router_api_key(client, path)
        except click.ClickException:
            continue
        configured.append(client)
    return tuple(configured)


def _stored_router_api_key_choices() -> list[tuple[str, tuple[str, ...]]]:
    """Group compatible receipt-backed credentials by the clients using them."""
    active_url = router_base_url().rstrip("/")
    api_keys: dict[str, list[str]] = {}
    for client in configured_router_clients():
        path = _client_config_path(client)
        try:
            if _stored_router_base_url(client, path) != active_url:
                continue
            api_key = _stored_router_api_key(client, path)
        except click.ClickException:
            continue
        api_keys.setdefault(api_key, []).append(client)
    return [(api_key, tuple(clients)) for api_key, clients in api_keys.items()]


def _pick_stored_router_api_key() -> str | None:
    """Choose a compatible stored credential or request browser key creation."""
    credentials = _stored_router_api_key_choices()
    if not credentials:
        return None
    choices = [
        questionary.Choice(
            title=f"Reuse existing key used by {_human_join([CLIENT_NAMES[item] for item in clients])}",
            value=index,
        )
        for index, (_, clients) in enumerate(credentials)
    ]
    choices.append(questionary.Choice(title="Create a new key", value="new"))
    selected = questionary.select(
        "Choose a Ramp Router API key",
        choices=choices,
        style=_PICKER_STYLE,
        pointer=_PICKER_POINTER,
        instruction="(enter to confirm)",
    ).ask()
    if selected is None:
        raise click.Abort()
    return None if selected == "new" else credentials[selected][0]


def _stored_router_base_url(client: str, path: Path) -> str | None:
    """Read the Router endpoint paired with a stored client credential."""
    base_url = None
    if client == "claude-code":
        settings = claude_code.read_settings(path)
        environment = settings.get("env")
        if isinstance(environment, dict):
            base_url = environment.get("ANTHROPIC_BASE_URL")
        if isinstance(base_url, str) and base_url.strip():
            base_url = f"{base_url.strip().rstrip('/').removesuffix('/v1')}/v1"
    elif client == "codex":
        _, config = _read_codex_config(path)
        providers = config.get("model_providers")
        provider = (
            providers.get(ROUTER_PROVIDER) if isinstance(providers, dict) else None
        )
        if isinstance(provider, dict):
            base_url = provider.get("base_url")
    elif client == "opencode":
        config = _read_json_config(client, path)
        package_path = _bundled_plugin_path(client)
        plugins = config.get("plugin", [])
        if isinstance(plugins, list):
            for entry in plugins:
                if _is_router_plugin_entry(client, entry, package_path):
                    if isinstance(entry, list) and len(entry) > 1:
                        options = entry[1]
                        if isinstance(options, dict):
                            base_url = options.get("baseURL")
                    break
    elif client == "pi":
        config = _read_json_config(client, path.parent / PI_PLUGIN_CONFIG_FILE)
        base_url = config.get("baseUrl")

    if isinstance(base_url, str) and base_url.strip():
        return base_url.strip().rstrip("/")
    return None


def _stored_router_api_key(client: str, path: Path) -> str:
    """Read the credential this CLI previously wrote without exposing it."""
    api_key = None
    try:
        if client == "claude-code":
            settings = claude_code.read_settings(path)
            environment = settings.get("env")
            if isinstance(environment, dict):
                api_key = environment.get("ANTHROPIC_AUTH_TOKEN")
        elif client == "codex":
            api_key = (path.parent / "ramp-router-key").read_text(encoding="utf-8")
        elif client == "opencode":
            config = _read_json_config(client, path)
            package_path = _bundled_plugin_path(client)
            plugins = config.get("plugin", [])
            if isinstance(plugins, list):
                for entry in plugins:
                    if not _is_router_plugin_entry(client, entry, package_path):
                        continue
                    if isinstance(entry, list) and len(entry) > 1:
                        options = entry[1]
                        if isinstance(options, dict):
                            api_key = options.get("apiKey")
                            break
        elif client == "pi":
            auth = _read_json_config(client, path.parent / "auth.json")
            credential = auth.get(ROUTER_PROVIDER)
            if isinstance(credential, dict):
                api_key = credential.get("key")
        else:
            raise click.ClickException(
                f"Refreshing {CLIENT_NAMES[client]} is not supported by this CLI."
            )
    except (OSError, UnicodeError):
        api_key = None

    if isinstance(api_key, str) and api_key.strip():
        return api_key.strip()
    raise click.ClickException(
        f"Could not read the existing Ramp Router API key for {CLIENT_NAMES[client]}. "
        f"Run 'ramp router configure {client}' to repair it."
    )


def _configured_model(client: str, path: Path) -> str | None:
    """Return the client's selected Router model when one is still selected."""
    if client == "claude-code":
        config = claude_code.read_settings(path)
        model = config.get("model")
    elif client == "codex":
        _, config = _read_codex_config(path)
        model = config.get("model")
    else:
        config = _read_json_config(client, path)
        if client == "opencode":
            model = config.get("model")
            prefix = f"{ROUTER_PROVIDER}/"
            if isinstance(model, str) and model.startswith(prefix):
                model = model[len(prefix) :]
            else:
                model = None
        elif client == "pi" and config.get("defaultProvider") == ROUTER_PROVIDER:
            model = config.get("defaultModel")
        else:
            model = None
    return model if isinstance(model, str) and model else None


def router_base_url() -> str:
    """Return the Router this command should point clients at."""
    for name in BASE_URL_ENVS:
        configured = os.environ.get(name, "").strip()
        if configured:
            return configured.rstrip("/")
    return DEFAULT_ROUTER_BASE_URL


# The model a fresh setup starts on. Router publishes no recommendation, and
# its listing order is presentation rather than a ranking, so naming one here
# is the only way to avoid defaulting everybody to whichever model happens to
# sort first. That means this goes stale on its own and has to be moved when a
# better default ships.
DEFAULT_MODEL = "gpt-5.6-sol"


def _preferred_model(
    models: list[RouterModel], selected_model: str | None = None
) -> str:
    """Pick the model to configure as the default."""
    identifiers = [model.id for model in models]
    if selected_model in identifiers:
        return selected_model
    return DEFAULT_MODEL if DEFAULT_MODEL in identifiers else identifiers[0]


def _configure_client(
    client: str,
    api_key: str,
    models: list[RouterModel],
    *,
    base_url: str | None = None,
    selected_model: str | None = None,
    claude_model_view: str | None = None,
    preserve_model_view_ownership: bool = False,
) -> tuple[Path, str, bool]:
    """Configure one client, reporting whether it replaced an outdated setup."""
    if client == "claude-code":
        path = _client_config_path(client)
        return (
            path,
            _configure_claude_code(
                path,
                api_key,
                models,
                base_url=base_url,
                selected_model=selected_model,
                model_view=claude_model_view or "compact",
                preserve_model_view_ownership=preserve_model_view_ownership,
            ),
            False,
        )

    if client == "codex":
        path = _codex_config_path()
        default_model, repaired = _configure_codex(
            path,
            api_key,
            models,
            base_url=base_url,
            selected_model=selected_model,
        )
        return path, default_model, repaired

    path = _json_config_path(client)
    default_model = _preferred_model(models, selected_model)
    _configure_plugin_client(client, path, api_key, default_model, base_url=base_url)
    return path, default_model, False


def _plugin_source(entry: object) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, list) and entry and isinstance(entry[0], str):
        return entry[0]
    if isinstance(entry, dict) and isinstance(entry.get("source"), str):
        return entry["source"]
    return None


def _is_router_plugin_entry(client: str, entry: object, package_path: Path) -> bool:
    source = _plugin_source(entry)
    if source is None:
        return False
    package_name = (
        _OPENCODE_PLUGIN_PACKAGE if client == "opencode" else _PI_PLUGIN_PACKAGE
    )
    legacy_name = (
        "@llm-router/opencode-provider"
        if client == "opencode"
        else "@llm-router/pi-provider"
    )
    expected_sources = {str(package_path), package_path.as_uri()}
    if source in expected_sources or source in {package_name, f"npm:{package_name}"}:
        return True
    if source in {legacy_name, f"npm:{legacy_name}"}:
        return True
    package_dir = package_path.name
    # The bundled path moves on every CLI upgrade, so a stale entry is matched
    # by its directory name. Compared as a path rather than a string suffix,
    # because Pi records a native path and Windows separates with backslashes.
    if PurePath(source.rstrip("/\\")).name == package_dir:
        return True
    if client == "opencode" and isinstance(entry, list) and len(entry) > 1:
        options = entry[1]
        if isinstance(options, dict) and options.get("providerID") in {
            ROUTER_PROVIDER,
            "router",
        }:
            return True
    return False


def _bundled_plugin_path(client: str) -> Path:
    path = integration_package_path(client).resolve()
    if (
        not (path / "package.json").is_file()
        or not (path / "src" / "index.ts").is_file()
    ):
        raise click.ClickException(
            f"The bundled {CLIENT_NAMES[client]} Ramp Router plugin is missing. "
            "Reinstall ramp-cli and try again."
        )
    return path


def _configure_plugin_client(
    client: str,
    path: Path,
    api_key: str,
    default_model: str,
    base_url: str | None = None,
) -> None:
    package_path = _bundled_plugin_path(client)
    state_path = path.parent / "ramp-router-state.json"
    state = None

    if client == "opencode":
        existing = _read_json_config(client, path)
        plugins = existing.get("plugin", [])
        if not isinstance(plugins, list):
            raise click.ClickException(
                f"Could not update OpenCode config {path}: 'plugin' must be an array."
            )
        providers = existing.get("provider", {})
        if not isinstance(providers, dict):
            raise click.ClickException(
                f"Could not update OpenCode config {path}: 'provider' must be an object."
            )
        # OpenCode's TUI loads plugins only from tui.json; a package listed in
        # opencode.json alone never gets its sidebar entrypoint loaded. The
        # same tuple therefore goes into both files: opencode.json for the
        # server-side provider, tui.json for the usage sidebar.
        tui_path = _opencode_tui_config_path()
        tui_existed = tui_path.exists()
        tui_config = _read_json_config(client, tui_path)
        tui_plugins = tui_config.get("plugin", [])
        if not isinstance(tui_plugins, list):
            raise click.ClickException(
                f"Could not update OpenCode config {tui_path}: "
                "'plugin' must be an array."
            )
        previous_plugins = [
            entry
            for entry in plugins
            if _is_router_plugin_entry(client, entry, package_path)
        ]
        previous_tui_plugins = [
            entry
            for entry in tui_plugins
            if _is_router_plugin_entry(client, entry, package_path)
        ]
        if not state_path.exists():
            state = (
                json.dumps(
                    {
                        "provider_present": ROUTER_PROVIDER in providers,
                        "provider": providers.get(ROUTER_PROVIDER),
                        "model_present": "model" in existing,
                        "model": existing.get("model"),
                        "plugin_present": "plugin" in existing,
                        "plugin_entries": previous_plugins,
                        "tui_file_present": tui_existed,
                        "tui_plugin_present": "plugin" in tui_config,
                        "tui_plugin_entries": previous_tui_plugins,
                    },
                    indent=2,
                )
                + "\n"
            )

        plugin_entry = [
            package_path.as_uri(),
            {
                "providerID": ROUTER_PROVIDER,
                "name": "Ramp Router",
                "baseURL": base_url or router_base_url(),
                # The dashboard origin serving /session-usage, which the
                # data plane behind baseURL does not. Written explicitly
                # for the same reason Claude Code gets ROUTER_BASE_URL:
                # the plugin cannot know a split-origin deployment's
                # dashboard host from the data-plane URL alone.
                "usageBaseURL": _statusline_origin(base_url),
                "apiKey": api_key,
            },
        ]
        existing["plugin"] = [
            entry
            for entry in plugins
            if not _is_router_plugin_entry(client, entry, package_path)
        ]
        existing["plugin"].append(plugin_entry)
        providers.pop(ROUTER_PROVIDER, None)
        if providers:
            existing["provider"] = providers
        else:
            existing.pop("provider", None)
        existing["model"] = f"{ROUTER_PROVIDER}/{default_model}"
        if not tui_existed:
            tui_config["$schema"] = "https://opencode.ai/tui.json"
        tui_config["plugin"] = [
            entry
            for entry in tui_plugins
            if not _is_router_plugin_entry(client, entry, package_path)
        ]
        tui_config["plugin"].append(plugin_entry)
        writes = [(path, existing), (tui_path, tui_config)]
    else:
        settings = _read_json_config(client, path)
        packages = settings.get("packages", [])
        if not isinstance(packages, list):
            raise click.ClickException(
                f"Could not update Pi config {path}: 'packages' must be an array."
            )
        models_path = path.parent / "models.json"
        models_config = _read_json_config(client, models_path)
        providers = models_config.get("providers", {})
        if not isinstance(providers, dict):
            raise click.ClickException(
                f"Could not update Pi config {models_path}: "
                "'providers' must be an object."
            )
        auth_path = path.parent / "auth.json"
        auth = _read_json_config(client, auth_path)
        previous_packages = [
            entry
            for entry in packages
            if _is_router_plugin_entry(client, entry, package_path)
        ]
        configured_auth = {"type": "api_key", "key": api_key}
        if state_path.exists():
            try:
                state_payload = json.loads(state_path.read_text(encoding="utf-8"))
                if not isinstance(state_payload, dict):
                    raise ValueError
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                raise click.ClickException(
                    f"Could not read Ramp Router setup state from {state_path}."
                ) from None
        else:
            state_payload = {
                "provider_present": ROUTER_PROVIDER in providers,
                "provider": providers.get(ROUTER_PROVIDER),
                "default_provider_present": "defaultProvider" in settings,
                "default_provider": settings.get("defaultProvider"),
                "default_model_present": "defaultModel" in settings,
                "default_model": settings.get("defaultModel"),
                "packages_present": "packages" in settings,
                "package_entries": previous_packages,
                "auth_present": ROUTER_PROVIDER in auth,
                "auth": auth.get(ROUTER_PROVIDER),
            }
        # Preserve the original values across repeated configure calls, but
        # update the salted marker for the credential this invocation
        # installed. Unconfigure restores the original only while this marker
        # still matches, so a later Pi /login or manual rotation is never
        # overwritten. The installed secret itself is not duplicated into the
        # longer-lived setup-state file.
        state_payload["configured_auth_marker"] = _auth_marker(api_key)
        state = json.dumps(state_payload, indent=2) + "\n"

        settings["packages"] = [
            entry
            for entry in packages
            if not _is_router_plugin_entry(client, entry, package_path)
        ]
        settings["packages"].append(str(package_path))
        settings["defaultProvider"] = ROUTER_PROVIDER
        settings["defaultModel"] = default_model
        auth[ROUTER_PROVIDER] = configured_auth
        providers.pop(ROUTER_PROVIDER, None)
        # Keep the key even when it empties out. Pi's schema requires
        # "providers", so dropping it leaves {} on disk, which Pi rejects
        # wholesale: it discards the file and falls back to cached models
        # rather than reporting a missing provider.
        models_config["providers"] = providers
        # Pi hands an extension no configuration of its own, so the Router to
        # call is written beside the credential rather than left to the
        # environment, where it would have to be set again in every shell.
        writes = [
            (path, settings),
            (auth_path, auth),
            (
                path.parent / PI_PLUGIN_CONFIG_FILE,
                {"baseUrl": base_url or router_base_url()},
            ),
        ]
        if models_path.exists():
            writes.append((models_path, models_config))

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if state is not None:
            _write_private_file(state_path, state)
        for output_path, payload in writes:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            _write_private_file(output_path, json.dumps(payload, indent=2) + "\n")
    except OSError as exc:
        raise click.ClickException(
            f"Could not write {CLIENT_NAMES[client]} config {path}: {exc}"
        ) from None


def _unconfigure_json_client(client: str, path: Path) -> None:
    state_path = path.parent / "ramp-router-state.json"
    if not state_path.exists():
        raise click.ClickException(
            f"Ramp Router is not configured in {CLIENT_NAMES[client]}."
        )
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        provider_present = state["provider_present"]
        if not isinstance(state, dict) or not isinstance(provider_present, bool):
            raise ValueError
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ):
        raise click.ClickException(
            f"Could not read Ramp Router setup state from {state_path}."
        ) from None

    package_path = integration_package_path(client).resolve()
    if client == "opencode":
        existing = _read_json_config(client, path)
        plugins = existing.get("plugin", [])
        if not isinstance(plugins, list):
            raise click.ClickException(
                f"Could not restore OpenCode config {path}: 'plugin' must be an array."
            )
        previous_plugins = state.get("plugin_entries", [])
        if not isinstance(previous_plugins, list):
            raise click.ClickException(
                f"Could not read Ramp Router setup state from {state_path}."
            )
        plugins = [
            entry
            for entry in plugins
            if not _is_router_plugin_entry(client, entry, package_path)
        ]
        plugins.extend(previous_plugins)
        if plugins or state.get("plugin_present") is True:
            existing["plugin"] = plugins
        else:
            existing.pop("plugin", None)

        providers = existing.get("provider", {})
        if not isinstance(providers, dict):
            raise click.ClickException(
                f"Could not restore OpenCode config {path}: "
                "'provider' must be an object."
            )
        _restore_json_value(
            providers,
            ROUTER_PROVIDER,
            provider_present,
            state.get("provider"),
        )
        if providers:
            existing["provider"] = providers
        else:
            existing.pop("provider", None)
        if str(existing.get("model", "")).startswith(f"{ROUTER_PROVIDER}/"):
            _restore_json_value(
                existing,
                "model",
                state.get("model_present"),
                state.get("model"),
            )
        writes = [(path, existing)]

        # Undo the TUI registration too. A receipt from before configure
        # wrote tui.json has no record of it, so those setups only strip the
        # Router entries this CLI recognizes and leave the rest untouched.
        tui_path = _opencode_tui_config_path()
        if tui_path.exists():
            tui_config = _read_json_config(client, tui_path)
            tui_plugins = tui_config.get("plugin", [])
            if not isinstance(tui_plugins, list):
                raise click.ClickException(
                    f"Could not restore OpenCode config {tui_path}: "
                    "'plugin' must be an array."
                )
            previous_tui_plugins = state.get("tui_plugin_entries", [])
            if not isinstance(previous_tui_plugins, list):
                raise click.ClickException(
                    f"Could not read Ramp Router setup state from {state_path}."
                )
            tui_plugins = [
                entry
                for entry in tui_plugins
                if not _is_router_plugin_entry(client, entry, package_path)
            ]
            tui_plugins.extend(previous_tui_plugins)
            if tui_plugins or state.get("tui_plugin_present") is True:
                tui_config["plugin"] = tui_plugins
            else:
                tui_config.pop("plugin", None)
            if state.get("tui_file_present") is False and set(tui_config) <= {
                "$schema"
            }:
                # Configure created this file and nothing else moved in, so
                # removing it restores exactly what the user had: no file.
                try:
                    tui_path.unlink()
                except OSError as exc:
                    raise click.ClickException(
                        f"Could not restore OpenCode config {tui_path}: {exc}"
                    ) from None
            else:
                writes.append((tui_path, tui_config))
    else:
        settings = _read_json_config(client, path)
        packages = settings.get("packages", [])
        if not isinstance(packages, list):
            raise click.ClickException(
                f"Could not restore Pi config {path}: 'packages' must be an array."
            )
        previous_packages = state.get("package_entries", [])
        if not isinstance(previous_packages, list):
            raise click.ClickException(
                f"Could not read Ramp Router setup state from {state_path}."
            )
        packages = [
            entry
            for entry in packages
            if not _is_router_plugin_entry(client, entry, package_path)
        ]
        packages.extend(previous_packages)
        if packages or state.get("packages_present") is True:
            settings["packages"] = packages
        else:
            settings.pop("packages", None)
        if settings.get("defaultProvider") == ROUTER_PROVIDER:
            _restore_json_value(
                settings,
                "defaultProvider",
                state.get("default_provider_present"),
                state.get("default_provider"),
            )
            _restore_json_value(
                settings,
                "defaultModel",
                state.get("default_model_present"),
                state.get("default_model"),
            )

        auth_path = path.parent / "auth.json"
        auth = _read_json_config(client, auth_path)
        if "configured_auth_marker" not in state:
            # Older setup state does not identify the credential installed by
            # Ramp. Restoring unconditionally could overwrite a later Pi
            # /login; leaving it silently would lose the only copy of the
            # original credential. Fail before writing anything so a repeated
            # configure can add a marker while preserving that original state.
            raise click.ClickException(
                "Could not safely restore legacy Pi credentials. Run "
                "'ramp router configure pi' once, then unconfigure again."
            )
        configured_auth_marker = state.get("configured_auth_marker")
        if not _valid_auth_marker(configured_auth_marker):
            raise click.ClickException(
                f"Could not read Ramp Router setup state from {state_path}."
            )
        if _auth_matches_marker(auth.get(ROUTER_PROVIDER), configured_auth_marker):
            _restore_json_value(
                auth,
                ROUTER_PROVIDER,
                state.get("auth_present"),
                state.get("auth"),
            )
        models_path = path.parent / "models.json"
        models_config = _read_json_config(client, models_path)
        providers = models_config.get("providers", {})
        if not isinstance(providers, dict):
            raise click.ClickException(
                f"Could not restore Pi config {models_path}: "
                "'providers' must be an object."
            )
        # Configure removes the legacy Router provider entry. Restore it only
        # if that absence is still intact; a provider added afterward belongs
        # to the user or a newer Pi flow and must survive unconfigure.
        if ROUTER_PROVIDER not in providers:
            _restore_json_value(
                providers,
                ROUTER_PROVIDER,
                provider_present,
                state.get("provider"),
            )
        # Keep the key even when it empties out. Pi's schema requires
        # "providers", so dropping it leaves {} on disk, which Pi rejects
        # wholesale: it discards the file and falls back to cached models
        # rather than reporting a missing provider.
        models_config["providers"] = providers
        writes = [(path, settings), (auth_path, auth)]
        (path.parent / PI_PLUGIN_CONFIG_FILE).unlink(missing_ok=True)
        (path.parent / PI_PLUGIN_MODEL_CACHE_FILE).unlink(missing_ok=True)
        (path.parent / PI_PLUGIN_MODEL_CACHE_KEY_FILE).unlink(missing_ok=True)
        (path.parent / PI_PLUGIN_RUNTIME_MODELS_FILE).unlink(missing_ok=True)
        if models_path.exists() or provider_present:
            writes.append((models_path, models_config))

    try:
        for output_path, payload in writes:
            _write_private_file(output_path, json.dumps(payload, indent=2) + "\n")
        state_path.unlink()
    except OSError as exc:
        raise click.ClickException(
            f"Could not restore {CLIENT_NAMES[client]} config {path}: {exc}"
        ) from None


def _restore_json_value(data: dict, key: str, present: object, value: object) -> None:
    if present is True:
        data[key] = value
    else:
        data.pop(key, None)


def _auth_marker(api_key: str) -> dict[str, str]:
    salt = os.urandom(32)
    return {
        "salt": salt.hex(),
        "digest": hmac.new(salt, api_key.encode("utf-8"), hashlib.sha256).hexdigest(),
    }


def _valid_auth_marker(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"salt", "digest"}
        and isinstance(value.get("salt"), str)
        and isinstance(value.get("digest"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["salt"]) is not None
        and re.fullmatch(r"[0-9a-f]{64}", value["digest"]) is not None
    )


def _auth_matches_marker(value: object, marker: object) -> bool:
    if (
        not _valid_auth_marker(marker)
        or not isinstance(value, dict)
        or set(value) != {"type", "key"}
        or value.get("type") != "api_key"
        or not isinstance(value.get("key"), str)
    ):
        return False
    salt = bytes.fromhex(marker["salt"])
    actual = hmac.new(
        salt,
        value["key"].encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(actual, marker["digest"])


def _read_json_config(client: str, path: Path) -> dict:
    try:
        if not path.exists():
            return {}
        data = json5.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("the top-level value must be an object")
        return data
    except (OSError, UnicodeError, ValueError) as exc:
        raise click.ClickException(
            f"Could not read {CLIENT_NAMES[client]} config {path}: {exc}"
        ) from None


def _fetch_models(
    api_key: str,
    claude_code_view: bool = False,
    *,
    gateway_client: str | None = None,
    model_view_all: bool = False,
    base_url: str | None = None,
    wait_for_key: bool = False,
) -> list[RouterModel]:
    if claude_code_view and gateway_client is not None:
        raise ValueError("Choose one Router client projection.")
    requested_client = "claude-code" if claude_code_view else gateway_client
    try:
        for retry in range(len(NEW_KEY_RETRY_DELAYS) + 1):
            response = httpx.get(
                f"{(base_url or router_base_url()).rstrip('/')}/models",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    # Ask for the projection whose ids the named client accepts.
                    **(
                        {GATEWAY_CLIENT_HEADER: requested_client}
                        if requested_client is not None
                        else {}
                    ),
                    **(
                        {claude_code.MODEL_VIEW_HEADER: "all"} if model_view_all else {}
                    ),
                },
                timeout=10,
            )
            if (
                wait_for_key
                and response.status_code == 401
                and retry < len(NEW_KEY_RETRY_DELAYS)
            ):
                time.sleep(NEW_KEY_RETRY_DELAYS[retry])
                continue
            break
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            raise click.ClickException(
                "That API key wasn't accepted by Ramp Router. "
                f"Create or copy a key at {ROUTER_UI_URL} and try again."
            ) from None
        raise click.ClickException(
            "Ramp Router couldn't validate the key "
            f"(HTTP {exc.response.status_code}). Please try again."
        ) from None
    except (httpx.HTTPError, ValueError):
        raise click.ClickException(
            "We couldn't reach Ramp Router. Check your connection and try again."
        ) from None

    models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise click.ClickException(
            "Ramp Router returned an unexpected response. Please try again."
        )
    discovered: dict[str, RouterModel] = {}
    for model in models:
        identifier = model.get("id", "").strip() if isinstance(model, dict) else ""
        if not identifier:
            # The plugins abort on a nameless entry rather than skipping it, so
            # skipping here would let configure claim success for a list they
            # will refuse.
            raise click.ClickException(
                "Ramp Router returned a model without an id. Please try again."
            )
        if identifier in discovered:
            continue
        discovered[identifier] = RouterModel(
            id=identifier, metadata=_model_metadata(model.get("router"), identifier)
        )
    if not discovered:
        raise click.ClickException("No models are available for this Ramp Router key.")
    return list(discovered.values())


def _fetch_codex_catalog(api_key: str, *, base_url: str | None = None) -> dict:
    """Fetch Router's exact provider-specific catalog for Codex Desktop.

    Codex stores every provider in one shared cache. Saving the same catalog
    Router serves dynamically makes the active provider authoritative, so a
    first-party refresh cannot replace Router's model picker with OpenAI's.
    ``ramp router refresh`` rewrites this snapshot whenever Router changes.
    """
    url = f"{(base_url or router_base_url()).rstrip('/')}/models"
    try:
        response = httpx.get(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                GATEWAY_CLIENT_HEADER: CODEX_CLIENT_NAME,
            },
            timeout=10,
        )
        response.raise_for_status()
        catalog = response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            raise click.ClickException(
                "That API key wasn't accepted by Ramp Router. "
                f"Create or copy a key at {ROUTER_UI_URL} and try again."
            ) from None
        raise click.ClickException(
            "Ramp Router couldn't load the Codex model catalog "
            f"(HTTP {exc.response.status_code}). Please try again."
        ) from None
    except (httpx.HTTPError, ValueError):
        raise click.ClickException(
            "We couldn't load the Codex model catalog from Ramp Router. "
            "Check your connection and try again."
        ) from None

    models = catalog.get("models") if isinstance(catalog, dict) else None
    if not isinstance(models, list) or not models:
        raise click.ClickException(
            "Ramp Router returned an unexpected Codex model catalog. Please try again."
        )
    if any(
        not isinstance(model, dict)
        or not isinstance(model.get("slug"), str)
        or not model["slug"].strip()
        for model in models
    ):
        raise click.ClickException(
            "Ramp Router returned an invalid Codex model catalog. Please try again."
        )
    return catalog


def _render_codex_catalog(catalog: dict, selected_model: str) -> str:
    """Bound a validated catalog while retaining Codex's current selection."""
    models = catalog["models"]
    limited = list(models[:CODEX_CATALOG_MODEL_LIMIT])
    selected = next(
        (model for model in models if model["slug"] == selected_model), None
    )
    if selected is None:
        raise click.ClickException(
            f"Ramp Router's Codex catalog does not include the selected model "
            f"{selected_model!r}. Update the Ramp CLI and try again."
        )
    if selected is not None and selected not in limited:
        limited[-1] = selected
    return json.dumps({**catalog, "models": limited}, indent=2) + "\n"


def _codex_provider_is_out_of_date(existing: dict) -> bool:
    """Report an existing Router block written before the client marker.

    Router answers with Codex's own catalog shape only for a client that asks
    by name. A block written before that will keep receiving the OpenAI model
    list, which Codex ignores entirely, so its picker simply comes up empty
    with nothing said. Rewriting the block fixes it, and saying so is what
    tells someone why they had to.
    """
    providers = existing.get("model_providers")
    if not isinstance(providers, dict):
        return False
    router = providers.get(ROUTER_PROVIDER)
    if not isinstance(router, dict):
        return False
    headers = router.get("http_headers")
    stated = headers.get(GATEWAY_CLIENT_HEADER) if isinstance(headers, dict) else None
    return stated != CODEX_CLIENT_NAME


def _configure_codex(
    path: Path,
    api_key: str,
    models: list[RouterModel],
    *,
    base_url: str | None = None,
    selected_model: str | None = None,
) -> tuple[str, bool]:
    """Point the Codex CLI and Desktop app at Ramp Router."""
    # Finish remote discovery before taking the lock and the local config
    # snapshot used by the read-modify-write below. Otherwise a slow request
    # would stall every other config.toml writer, and a user or Codex edit
    # made while this request is in flight can be replaced by the stale
    # pre-request copy.
    router_catalog = _fetch_codex_catalog(api_key, base_url=base_url)
    # None means nothing to (re)register; existing registrations stay as-is.
    cost_hook_command = _install_codex_cost_hook(path, base_url)
    # None (Windows or no resolvable ramp entrypoint) registers nothing.
    sync_hook_command = session_sync_hook_command("codex")
    with codex_config_lock(path):
        return _configure_codex_in_lock(
            path,
            api_key,
            models,
            router_catalog,
            base_url=base_url,
            selected_model=selected_model,
            cost_hook_command=cost_hook_command,
            sync_hook_command=sync_hook_command,
        )


def _configure_codex_in_lock(
    path: Path,
    api_key: str,
    models: list[RouterModel],
    router_catalog: dict,
    *,
    base_url: str | None,
    selected_model: str | None,
    cost_hook_command: str | None,
    sync_hook_command: str | None,
) -> tuple[str, bool]:
    default_model = _preferred_model(models, selected_model)
    existing, existing_data = _read_codex_config(path)
    if selected_model is not None:
        # Refresh captures a selection before discovery starts. Read it again
        # from the post-discovery snapshot so a model change made while the
        # request was in flight is preserved.
        default_model = _preferred_model(models, existing_data.get("model"))
    router_catalog_body = _render_codex_catalog(router_catalog, default_model)
    chunks = _config_chunks(existing)
    repaired = _codex_provider_is_out_of_date(existing_data)

    # Read before the receipt is assembled, because the escape profile is
    # recorded there along with what it was written to hold, and both depend
    # on the model this run settles on.
    instructions_path = path.parent / "ramp-router-instructions.md"
    harness_prompt = _codex_harness_prompt(default_model)
    if (
        harness_prompt is None
        and existing_data.get("model_provider") == ROUTER_PROVIDER
        and instructions_path.exists()
    ):
        try:
            harness_prompt = instructions_path.read_text(encoding="utf-8") or None
        except (OSError, UnicodeError):
            harness_prompt = None
    state_path = path.parent / "ramp-router-state.json"
    previous_state = None
    # The receipt as it will be written, or None when this run changes nothing
    # in it.
    state_document: dict | None = None
    if not state_path.exists():
        previous_provider = [
            "".join(chunk)
            for chunk in chunks
            if chunk and _OWNED_CODEX_TABLE.match(chunk[0])
        ]
        sessions = _prepare_codex_sessions(path.parent)
        saved_sessions = sessions
        root_values = {
            key: existing_data[key] for key in _OWNED_ROOT_KEYS if key in existing_data
        }
        receipt = {
            "root": root_values,
            "provider": previous_provider,
            "sessions": sessions,
            CODEX_PREEXISTING_ROUTER_SESSIONS_STATE_KEY: (
                _preexisting_codex_router_session_ids(path.parent)
            ),
        }
        state_document = receipt
    else:
        try:
            previous_state = state_path.read_text(encoding="utf-8")
            existing_state = json.loads(previous_state)
        except (OSError, UnicodeError, ValueError) as exc:
            raise click.ClickException(
                f"Could not read Ramp Router setup state from {state_path}: {exc}"
            ) from None
        if not isinstance(existing_state, dict):
            raise click.ClickException(
                f"Could not read Ramp Router setup state from {state_path}."
            )
        existing_sessions = existing_state.get("sessions", [])
        if not isinstance(existing_sessions, list):
            raise click.ClickException(
                f"Could not read Ramp Router setup state from {state_path}."
            )
        sessions = _prepare_codex_sessions(path.parent)
        saved_sessions = existing_sessions
        receipt = existing_state
        recorded_root = existing_state.get("root")
        root_values = recorded_root if isinstance(recorded_root, dict) else {}
        if sessions or "sessions" not in existing_state:
            existing_ids = {
                session.get("id")
                for session in existing_sessions
                if isinstance(session, dict)
            }
            existing_state["sessions"] = [
                *existing_sessions,
                *(
                    session
                    for session in sessions
                    if session.get("id") not in existing_ids
                ),
            ]
            saved_sessions = existing_state["sessions"]
            state_document = existing_state

    # Codex 0.147+ still shares one cache across every provider. This snapshot
    # prevents OpenAI's catalog refresh from replacing Router's Desktop picker;
    # configure and refresh both rewrite it from Router's live response.
    router_catalog_path = path.parent / CODEX_ROUTER_CATALOG
    catalog_digest = _content_digest(router_catalog_body)
    if receipt.get(CODEX_ROUTER_CATALOG_MANAGED_KEY) is not True:
        receipt[CODEX_ROUTER_CATALOG_MANAGED_KEY] = True
        state_document = receipt
    if receipt.get(CODEX_ROUTER_CATALOG_DIGEST_KEY) != catalog_digest:
        receipt[CODEX_ROUTER_CATALOG_DIGEST_KEY] = catalog_digest
        state_document = receipt
    # Lets unconfigure know the script and its Stop group are ours to remove.
    if (
        cost_hook_command is not None
        and receipt.get(CODEX_COST_HOOK_MANAGED_KEY) is not True
    ):
        receipt[CODEX_COST_HOOK_MANAGED_KEY] = True
        state_document = receipt
    # Read before registration advances it; identifies the one group a
    # previous run wrote and may now replace.
    recorded_sync_hook = receipt.get(CODEX_SYNC_HOOK_STATE_KEY)
    key_path = path.parent / "ramp-router-key"

    # Router sets model_instructions_file only when it could read Codex's own
    # prompt, so exactly when the key needs answering in the profile, the
    # prompt for the profile's own model can be read the same way.
    original_instructions = None
    original_prompt = None
    instructions_claim = None
    if harness_prompt and not root_values.get("model_instructions_file"):
        original_prompt = _codex_harness_prompt(
            root_values.get("model") or default_model
        )
        if original_prompt:
            instructions_path_candidate = path.parent / CODEX_ORIGINAL_INSTRUCTIONS
            instructions_claim = _claim_escape_artifact(
                instructions_path_candidate,
                receipt,
                CODEX_ORIGINAL_INSTRUCTIONS_STATE_KEY,
                CODEX_ORIGINAL_INSTRUCTIONS_DIGEST_KEY,
            )
            if instructions_claim is not None:
                original_instructions = instructions_path_candidate

    original_catalog = None
    catalog_body = _codex_original_catalog(root_values)
    catalog_claim = None
    if catalog_body is not None:
        catalog_path = path.parent / CODEX_ORIGINAL_CATALOG
        catalog_claim = _claim_escape_artifact(
            catalog_path,
            receipt,
            CODEX_ORIGINAL_CATALOG_STATE_KEY,
            CODEX_ORIGINAL_CATALOG_DIGEST_KEY,
        )
        if catalog_claim is not None:
            original_catalog = catalog_path

    root_catalog = root_values.get("model_catalog_json")
    profile_path = _codex_original_profile_path(path.parent)
    profile_body = _render_codex_original_profile(
        root_values,
        original_instructions,
        original_catalog,
    )
    needs_generated_catalog = not _codex_catalog_is_usable(root_catalog)
    needs_generated_instructions = bool(
        harness_prompt and not root_values.get("model_instructions_file")
    )
    if (needs_generated_catalog and original_catalog is None) or (
        needs_generated_instructions and original_instructions is None
    ):
        # Without both provider-specific artifacts the profile would inherit
        # Router's shared model cache or harness prompt. Do not create or
        # advertise an escape command that does not actually escape Router.
        profile_claim = None
    else:
        profile_claim = _claim_escape_artifact(
            profile_path,
            receipt,
            CODEX_ORIGINAL_PROFILE_STATE_KEY,
            CODEX_ORIGINAL_PROFILE_DIGEST_KEY,
        )
    original_profile = profile_path if profile_claim is not None else None
    if original_profile is not None:
        if _record_escape_artifact(
            receipt,
            original_profile,
            profile_body,
            CODEX_ORIGINAL_PROFILE_STATE_KEY,
            CODEX_ORIGINAL_PROFILE_DIGEST_KEY,
        ):
            state_document = receipt
        if original_catalog is not None and _record_escape_artifact(
            receipt,
            original_catalog,
            catalog_body,
            CODEX_ORIGINAL_CATALOG_STATE_KEY,
            CODEX_ORIGINAL_CATALOG_DIGEST_KEY,
        ):
            state_document = receipt
        if original_instructions is not None and _record_escape_artifact(
            receipt,
            original_instructions,
            original_prompt,
            CODEX_ORIGINAL_INSTRUCTIONS_STATE_KEY,
            CODEX_ORIGINAL_INSTRUCTIONS_DIGEST_KEY,
        ):
            state_document = receipt
    chunks[0] = [
        _render_root_config(
            chunks[0],
            # A None is not written, and every owned root key is stripped
            # before these go back, so None is also how an existing one is
            # dropped.
            {
                "model": default_model,
                "model_provider": "ramp-router",
                "model_catalog_json": str(router_catalog_path.resolve()),
                # Restores the harness prompt Router cannot supply. Absent when
                # Codex is not installed to read it from.
                "model_instructions_file": str(instructions_path.resolve())
                if harness_prompt
                else None,
            },
        )
    ]

    if sync_hook_command is not None:
        chunks = _drop_recorded_sync_hook_chunk(chunks, recorded_sync_hook)
    preserved = "".join(
        "".join(chunk)
        for chunk in chunks
        if not chunk
        or not (
            _OWNED_CODEX_TABLE.match(chunk[0])
            # Dropped only when a fresh registration is about to replace it,
            # which is also what repairs a stale ramp path. For the sync group
            # only the receipt-recorded rendering is dropped.
            or (cost_hook_command is not None and _is_owned_cost_hook_chunk(chunk))
        )
    ).rstrip()
    provider = _codex_provider(key_path, base_url)
    updated = f"{preserved}\n\n{provider}" if preserved else provider
    if cost_hook_command is not None:
        updated = _register_codex_cost_hook(path, updated, cost_hook_command)
    if sync_hook_command is not None:
        updated, receipt_changed = _register_codex_sync_hook(
            path, updated, sync_hook_command, receipt
        )
        if receipt_changed:
            state_document = receipt
    # Serialized after the sync registration, the receipt's last write.
    state = (
        json.dumps(state_document, indent=2) + "\n"
        if state_document is not None
        else None
    )

    # Kept so a failure can put the file back. Without it the undo below
    # deletes the key this config points at and leaves Codex configured for a
    # provider it can no longer authenticate to.
    previous_config = path.read_text() if path.exists() else None
    # Read alongside the config so a failure can put back exactly what was
    # here. On a repeat configure these already exist and the restored config
    # still points at them, so deleting them would leave a setup that worked a
    # moment ago unable to authenticate.
    previous_key = key_path.read_text() if key_path.exists() else None
    previous_instructions = (
        instructions_path.read_text() if instructions_path.exists() else None
    )
    previous_router_catalog = (
        router_catalog_path.read_text() if router_catalog_path.exists() else None
    )

    previous_profile_body = None
    created_profile = False
    previous_catalog_body = None
    created_catalog = False
    previous_original_instructions_body = None
    created_instructions = False
    try:
        tomllib.loads(updated)
        path.parent.mkdir(parents=True, exist_ok=True)
        if state is not None:
            _write_private_file(state_path, state)
        _write_private_file(router_catalog_path, router_catalog_body)
        # Written before the session rewrite because this is the write that
        # fails on a full or read-only disk. Succeeding here means a partial
        # failure afterwards still leaves a usable configuration.
        _write_private_file(path, updated)
        if original_profile is not None:
            if original_catalog is not None:
                if catalog_claim == "create":
                    _write_new_private_file(original_catalog, catalog_body)
                    created_catalog = True
                elif catalog_claim == "replace":
                    previous_catalog_body = original_catalog.read_text(encoding="utf-8")
                    _write_private_file(original_catalog, catalog_body)
            # Keep each receipt-provenanced dependency current before writing
            # the profile that points at it.
            if original_instructions is not None:
                if instructions_claim == "create":
                    _write_new_private_file(original_instructions, original_prompt)
                    created_instructions = True
                elif instructions_claim == "replace":
                    previous_original_instructions_body = (
                        original_instructions.read_text(encoding="utf-8")
                    )
                    _write_private_file(original_instructions, original_prompt)
            if profile_claim == "create":
                # Exclusive: a file that appeared under this name since the
                # check above is the user's, and failing here leaves it alone
                # rather than replacing it and deleting it at unconfigure.
                _write_new_private_file(original_profile, profile_body)
                created_profile = True
            elif profile_claim == "replace":
                previous_profile_body = original_profile.read_text(encoding="utf-8")
                _write_private_file(original_profile, profile_body)
        _write_private_file(key_path, api_key)
        if harness_prompt:
            _write_private_file(instructions_path, harness_prompt)
        else:
            instructions_path.unlink(missing_ok=True)
        _sync_codex_sessions(path.parent, saved_sessions)
        _update_codex_session_index(path.parent, saved_sessions, restore=False)
    except (OSError, tomllib.TOMLDecodeError, click.ClickException) as exc:
        # Only sessions discovered by this invocation can have changed here.
        # Restoring the cumulative receipt would retag sessions that belonged
        # to the previously working Router setup.
        try:
            _restore_codex_sessions(path.parent, sessions)
        except (OSError, click.ClickException):
            # Already failing; a failed undo must not replace the real cause.
            pass
        _restore_private_file(path, previous_config)
        _restore_private_file(router_catalog_path, previous_router_catalog)
        # Removed only when this run is what created them, so a file that
        # turned out to be the user's survives the failure it caused.
        if created_profile:
            _restore_private_file(original_profile, None)
        elif previous_profile_body is not None:
            _restore_private_file(original_profile, previous_profile_body)
        if created_catalog:
            _restore_private_file(original_catalog, None)
        elif previous_catalog_body is not None:
            _restore_private_file(original_catalog, previous_catalog_body)
        if created_instructions:
            _restore_private_file(original_instructions, None)
        elif previous_original_instructions_body is not None:
            _restore_private_file(
                original_instructions, previous_original_instructions_body
            )
        _restore_private_file(key_path, previous_key)
        _restore_private_file(instructions_path, previous_instructions)
        _restore_private_file(state_path, previous_state)
        message = exc.message if isinstance(exc, click.ClickException) else str(exc)
        raise click.ClickException(
            f"Could not write Codex config {path}: {message}"
        ) from None
    return default_model, repaired


def _write_new_private_file(path: Path, content: str) -> None:
    """Create a file this command owns, refusing to replace an existing one.

    The escape artifacts are the only files here written under names a user
    could plausibly have chosen, and unconfigure deletes what it wrote, so
    creation has to fail rather than clobber.
    """
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def _content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _is_unmodified(path: Path, digest: object) -> bool:
    """Report whether a file still holds exactly what this command wrote.

    A receipt without a digest predates them being recorded, and its file is
    treated as ours: that was the behavior when it was written.
    """
    if not isinstance(digest, str) or not digest:
        return True
    try:
        return _content_digest(path.read_text(encoding="utf-8")) == digest
    except (OSError, UnicodeError):
        return False


def _restore_private_file(path: Path, previous: str | None) -> None:
    """Put a file back as it was, or remove one this run created."""
    try:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            _write_private_file(path, previous)
    except OSError:
        # Already failing; a failed undo must not replace the real cause.
        pass


def _preexisting_codex_router_session_ids(home: Path) -> list[str]:
    """Snapshot Router threads that predate this CLI-managed configuration."""
    identifiers: set[str] = set()
    for directory in ("sessions", "archived_sessions"):
        root = home / directory
        if not root.is_dir():
            continue
        for transcript in sorted(root.rglob("*")):
            if (
                transcript.is_symlink()
                or not transcript.is_file()
                or not transcript.name.endswith((".jsonl", ".jsonl.zst"))
            ):
                continue
            try:
                item = _read_codex_session_meta(transcript)
            except (OSError, UnicodeError, zstandard.ZstdError):
                continue
            if item is None or item["payload"].get("model_provider") != ROUTER_PROVIDER:
                continue
            identifier = item["payload"].get("id")
            if isinstance(identifier, str):
                identifiers.add(identifier)

    database_path = home / "state_5.sqlite"
    if database_path.is_file():
        try:
            with sqlite3.connect(database_path, timeout=1) as database:
                columns = {
                    row[1]
                    for row in database.execute("PRAGMA table_info(threads)").fetchall()
                }
                if {"id", "model_provider"}.issubset(columns):
                    identifiers.update(
                        identifier
                        for (identifier,) in database.execute(
                            "SELECT id FROM threads WHERE model_provider = ?",
                            (ROUTER_PROVIDER,),
                        )
                        if isinstance(identifier, str)
                    )
        except sqlite3.Error as exc:
            raise click.ClickException(
                f"Could not read Codex session index {database_path}: {exc}"
            ) from None
    return sorted(identifiers)


def _prepare_codex_sessions(home: Path) -> list[dict]:
    # Codex hides sessions whose provider differs from the active provider.
    # See https://github.com/openai/codex/issues/15494.
    sessions = []
    sessions_dir = home / "sessions"
    session_paths = []
    if sessions_dir.is_dir():
        session_paths = sorted(
            path
            for path in sessions_dir.rglob("*")
            if path.name.endswith((".jsonl", ".jsonl.zst"))
        )
    for session_path in session_paths:
        if session_path.is_symlink() or not session_path.is_file():
            continue
        try:
            item = _read_codex_session_meta(session_path)
        except (OSError, UnicodeError, zstandard.ZstdError):
            continue
        if item is None:
            continue
        payload = item["payload"]
        item_session_id = payload.get("id")
        provider = payload.get("model_provider")
        if (
            not isinstance(item_session_id, str)
            or (provider is not None and not isinstance(provider, str))
            or provider == ROUTER_PROVIDER
        ):
            continue
        sessions.append(
            {
                "path": str(session_path.relative_to(home)),
                "id": item_session_id,
                "transcript_updated": True,
                "had_model_provider": "model_provider" in payload,
                "model_provider": provider,
            }
        )

    _record_codex_index_providers(home, sessions)
    return sessions


def _record_codex_index_providers(home: Path, sessions: list[dict]) -> None:
    database_path = home / "state_5.sqlite"
    if not database_path.is_file():
        return
    try:
        with sqlite3.connect(database_path, timeout=1) as database:
            columns = {
                row[1]
                for row in database.execute("PRAGMA table_info(threads)").fetchall()
            }
            if not {"id", "model_provider"}.issubset(columns):
                return
            sessions_by_id: dict[str, list[dict]] = {}
            for session in sessions:
                sessions_by_id.setdefault(session["id"], []).append(session)
            has_rollout_path = "rollout_path" in columns
            selected_columns = (
                "id, model_provider, rollout_path"
                if has_rollout_path
                else "id, model_provider"
            )
            for row in database.execute(f"SELECT {selected_columns} FROM threads"):
                session_id, provider, *rollout_path = row
                transcript_sessions = sessions_by_id.get(session_id)
                if transcript_sessions:
                    for session in transcript_sessions:
                        session["index_model_provider"] = provider
                    continue
                if (
                    not has_rollout_path
                    or provider is None
                    or provider == ROUTER_PROVIDER
                ):
                    continue
                try:
                    relative_path = (
                        Path(rollout_path[0]).resolve().relative_to(home.resolve())
                    )
                except (TypeError, ValueError):
                    continue
                if not relative_path.parts or relative_path.parts[0] != "sessions":
                    continue
                session = {
                    "path": str(relative_path),
                    "id": session_id,
                    "transcript_updated": False,
                    "index_model_provider": provider,
                }
                sessions.append(session)
                sessions_by_id[session_id] = [session]
    except sqlite3.Error as exc:
        raise click.ClickException(
            f"Could not read Codex session index {database_path}: {exc}"
        ) from None


def _update_codex_session_index(
    home: Path, sessions: list[dict], *, restore: bool
) -> None:
    database_path = home / "state_5.sqlite"
    if not database_path.is_file() or not sessions:
        return
    try:
        with sqlite3.connect(database_path, timeout=1) as database:
            columns = {
                row[1]
                for row in database.execute("PRAGMA table_info(threads)").fetchall()
            }
            if not {"id", "model_provider"}.issubset(columns):
                return
            for session in sessions:
                if restore:
                    if "index_model_provider" not in session:
                        continue
                    provider = session["index_model_provider"]
                    expected_provider = ROUTER_PROVIDER
                else:
                    provider = ROUTER_PROVIDER
                    expected_provider = session.get(
                        "index_model_provider", session.get("model_provider")
                    )
                    if expected_provider is None:
                        continue
                database.execute(
                    "UPDATE threads SET model_provider = ? "
                    "WHERE id = ? AND model_provider = ?",
                    (provider, session["id"], expected_provider),
                )
    except (KeyError, TypeError, sqlite3.Error) as exc:
        raise click.ClickException(
            f"Could not update Codex session index {database_path}: {exc}"
        ) from None


def _codex_restored_provider(root_values: dict) -> str:
    """The provider Codex answers to once Router's root keys are gone.

    An absent model_provider means Codex falls back to its built-in one, which
    is also what the threads predating Router are tagged with.
    """
    configured = root_values.get("model_provider")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return "openai"


def _reclaim_codex_router_sessions(
    home: Path,
    sessions: list[dict],
    provider: str,
    *,
    preexisting_router_sessions: list[str] | tuple[str, ...] = (),
) -> None:
    """Move conversations Router itself created onto the restored provider.

    Codex hides a thread whose provider is not the active one, so a session
    started while Router was configured disappears the moment Router is
    removed. The receipt cannot account for these: it lists what existed
    before configure, and these were born after it.

    A session the receipt does know about is skipped. One still on Router here
    is one whose restoration was declined because the user had changed it, and
    reclaiming it would override exactly the choice that declining protected.
    """
    recorded = {
        session.get("id") for session in sessions if isinstance(session, dict)
    } | set(preexisting_router_sessions)
    for directory in ("sessions", "archived_sessions"):
        root = home / directory
        if not root.is_dir():
            continue
        for transcript in sorted(root.rglob("*")):
            if transcript.is_symlink() or not transcript.is_file():
                continue
            if not transcript.name.endswith((".jsonl", ".jsonl.zst")):
                continue
            try:
                item = _read_codex_session_meta(transcript)
            except (OSError, UnicodeError, zstandard.ZstdError):
                continue
            if item is None:
                continue
            payload = item["payload"]
            identifier = payload.get("id")
            if (
                not isinstance(identifier, str)
                or identifier in recorded
                or payload.get("model_provider") != ROUTER_PROVIDER
            ):
                continue
            _rewrite_codex_session_provider(
                home,
                {"path": str(transcript.relative_to(home)), "id": identifier},
                expected_provider=ROUTER_PROVIDER,
                provider_present=True,
                provider=provider,
            )
    _reclaim_codex_index_rows(home, recorded, provider)


def _reclaim_codex_index_rows(home: Path, recorded: set, provider: str) -> None:
    """Point index rows Router still owns at the restored provider.

    The index is what the pickers read, and it holds rows for threads whose
    transcript is gone or unreadable, so it is swept in its own right rather
    than only alongside a rewritten file.
    """
    database_path = home / "state_5.sqlite"
    if not database_path.is_file():
        return
    with sqlite3.connect(database_path, timeout=1) as database:
        columns = {
            row[1] for row in database.execute("PRAGMA table_info(threads)").fetchall()
        }
        if not {"id", "model_provider"}.issubset(columns):
            return
        stranded = database.execute(
            "SELECT id FROM threads WHERE model_provider = ?", (ROUTER_PROVIDER,)
        ).fetchall()
        for (identifier,) in stranded:
            if identifier in recorded:
                continue
            database.execute(
                "UPDATE threads SET model_provider = ? "
                "WHERE id = ? AND model_provider = ?",
                (provider, identifier, ROUTER_PROVIDER),
            )


def _restore_codex_sessions(home: Path, sessions: list[dict]) -> None:
    for session in sessions:
        if not session.get("transcript_updated", True):
            continue
        try:
            had_provider = session["had_model_provider"]
            provider = session.get("model_provider")
        except (KeyError, TypeError):
            raise click.ClickException(
                "Codex session setup state is invalid."
            ) from None
        _rewrite_codex_session_provider(
            home,
            session,
            expected_provider=ROUTER_PROVIDER,
            provider_present=had_provider,
            provider=provider,
        )
    _update_codex_session_index(home, sessions, restore=True)


def _sync_codex_sessions(home: Path, sessions: list[dict]) -> None:
    for session in sessions:
        if not session.get("transcript_updated", True):
            continue
        try:
            expected_provider = session.get("model_provider")
            provider_present = session["had_model_provider"]
        except (KeyError, TypeError):
            raise click.ClickException(
                "Codex session setup state is invalid."
            ) from None
        _rewrite_codex_session_provider(
            home,
            session,
            expected_provider=expected_provider,
            expected_present=provider_present,
            provider_present=True,
            provider=ROUTER_PROVIDER,
        )


def _rollback_codex_sessions(home: Path, sessions: list[dict]) -> None:
    try:
        _sync_codex_sessions(home, sessions)
        _update_codex_session_index(home, sessions, restore=False)
    except (OSError, click.ClickException):
        pass


def _read_codex_config(path: Path) -> tuple[str, dict]:
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        data = tomllib.loads(existing) if existing else {}
        return existing, data
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise click.ClickException(
            f"Could not read Codex config {path}: {exc}"
        ) from None


def _config_chunks(config: str) -> list[list[str]]:
    chunks: list[list[str]] = [[]]
    for line in config.splitlines(keepends=True):
        if _TABLE_HEADER.match(line):
            chunks.append([])
        chunks[-1].append(line)
    return chunks


def _render_root_config(lines: list[str], values: dict) -> str:
    root = "".join(line for line in lines if not _OWNED_CODEX_ROOT_KEY.match(line))
    root = root.rstrip()
    settings = "\n".join(
        # A None would render as the JSON null, which is not valid TOML and
        # would leave Codex unable to read its own config at all.
        f"{key} = {json.dumps(value)}"
        for key, value in values.items()
        if value is not None
    )
    if root and settings:
        return f"{root}\n{settings}\n"
    if settings:
        return f"{settings}\n"
    return f"{root}\n" if root else ""


def _write_private_file(path: Path, content: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@contextmanager
def _open_codex_session(path: Path):
    if path.suffix != ".zst":
        with path.open(encoding="utf-8") as session:
            yield session
        return
    with path.open("rb") as compressed:
        with zstandard.ZstdDecompressor().stream_reader(compressed) as reader:
            with io.TextIOWrapper(reader, encoding="utf-8") as text:
                yield text


def _read_codex_session_meta(path: Path) -> dict | None:
    with _open_codex_session(path) as session:
        for line in session:
            try:
                item = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(item, dict):
                continue
            payload = item.get("payload")
            if item.get("type") == "session_meta" and isinstance(payload, dict):
                return item
    return None


def _session_path(home: Path, session: dict) -> Path:
    try:
        path = home / Path(session["path"])
        path.resolve().relative_to(home.resolve())
    except (KeyError, TypeError, ValueError):
        raise click.ClickException("Codex session setup state is invalid.") from None
    return path


def _rewrite_codex_session_provider(
    home: Path,
    session: dict,
    *,
    expected_provider: str | None,
    provider_present: bool,
    provider: str | None,
    expected_present: bool = True,
) -> None:
    path = _session_path(home, session)
    if not path.is_file() or path.is_symlink():
        return
    try:
        meta = _read_codex_session_meta(path)
    except (OSError, UnicodeError, zstandard.ZstdError) as exc:
        raise click.ClickException(
            f"Could not read Codex session {path}: {exc}"
        ) from None
    if meta is None or meta["payload"].get("id") != session.get("id"):
        return
    payload = meta["payload"]
    current_present = "model_provider" in payload
    current_provider = payload.get("model_provider")
    if current_present == provider_present and current_provider == provider:
        return
    if current_present != expected_present or current_provider != expected_provider:
        return

    original = path.stat()
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with _open_codex_session(path) as source:
            if path.suffix == ".zst":
                with os.fdopen(fd, "wb") as raw_output:
                    with zstandard.ZstdCompressor(level=3).stream_writer(
                        raw_output, closefd=False
                    ) as output:
                        _copy_codex_session(
                            source,
                            output.write,
                            session["id"],
                            provider_present,
                            provider,
                            encode=True,
                        )
                    raw_output.flush()
                    os.fsync(raw_output.fileno())
            else:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    _copy_codex_session(
                        source,
                        output.write,
                        session["id"],
                        provider_present,
                        provider,
                    )
                    output.flush()
                    os.fsync(output.fileno())
        os.chmod(tmp_name, 0o600)
        current = path.stat()
        if (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
        ) != (
            original.st_dev,
            original.st_ino,
            original.st_size,
            original.st_mtime_ns,
        ):
            raise click.ClickException(
                f"Codex session changed while updating {path}. Close Codex and try again."
            )
        os.replace(tmp_name, path)
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _copy_codex_session(
    source,
    write,
    session_id: str,
    provider_present: bool,
    provider: str | None,
    *,
    encode: bool = False,
) -> None:
    replaced = False
    for line in source:
        rendered = line
        if not replaced:
            try:
                item = json.loads(line)
            except (ValueError, TypeError):
                item = None
            payload = item.get("payload") if isinstance(item, dict) else None
            if (
                isinstance(payload, dict)
                and item.get("type") == "session_meta"
                and payload.get("id") == session_id
            ):
                if provider_present:
                    payload["model_provider"] = provider
                else:
                    payload.pop("model_provider", None)
                newline = "\n" if line.endswith("\n") else ""
                rendered = json.dumps(item, separators=(",", ":")) + newline
                replaced = True
        write(rendered.encode("utf-8") if encode else rendered)
