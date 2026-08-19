"""Point Claude Code at Ramp Router, reversibly.

Claude Code needs no provider plugin. It supports an Anthropic-compatible
gateway directly through user settings, so configuring it is a matter of
writing the documented environment values and being able to put them back.
"""

import json
import os
import shlex
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

import click

from ramp_cli.commands.router_sync import (
    SYNC_HOOK_USER_MANAGED,
    command_runs_session_sync,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX fallback
    msvcrt = None

CLAUDE_SETTINGS_ENV = "CLAUDE_CONFIG_DIR"

# Claude Code dispatches sub-agents with a tier alias (sonnet, opus, haiku,
# fable) that it resolves to a canonical Anthropic model id unless one of
# these variables names a different model. They are how sub-agent tiers are
# pointed at any model Router serves, and how tier requests are kept from
# defaulting to ids the caller's access policy may not let Router serve.
# Claude Code reads them at each sub-agent spawn, not just at session start.
SUBAGENT_TIER_ENV_KEYS = {
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "fable": "ANTHROPIC_DEFAULT_FABLE_MODEL",
}
SUBAGENT_TIER_NAME_ENV_KEYS = {
    tier: f"{key}_NAME" for tier, key in SUBAGENT_TIER_ENV_KEYS.items()
}
SUBAGENT_TIER_DESCRIPTION_ENV_KEYS = {
    tier: f"{key}_DESCRIPTION" for tier, key in SUBAGENT_TIER_ENV_KEYS.items()
}
SUBAGENT_ENV_KEYS = (
    *SUBAGENT_TIER_ENV_KEYS.values(),
    *SUBAGENT_TIER_NAME_ENV_KEYS.values(),
    *SUBAGENT_TIER_DESCRIPTION_ENV_KEYS.values(),
)
SUBAGENT_DEFAULTS_STATE_KEY = "automatic_subagent_tiers"
CUSTOM_HEADERS_STATE_KEY = "managed_custom_headers"
_CLAUDE_AI_CONNECTORS_ENV = "ENABLE_CLAUDEAI_MCP_SERVERS"

# Router keys are bearer tokens, so ANTHROPIC_AUTH_TOKEN is correct and
# ANTHROPIC_API_KEY is deliberately not set: Claude Code treats them
# differently and setting both is ambiguous.
_OWNED_ENV_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    # Owned so it can be removed and put back. A profile that previously used
    # an API key would otherwise keep it alongside the Router bearer token, and
    # Claude Code can send the stale credential instead of ours.
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
    # Suppresses Claude's expected "connectors are disabled" warning in Router
    # sessions. Unlike the restrictive top-level setting, this normal env value
    # can be unset by the original settings overlay.
    _CLAUDE_AI_CONNECTORS_ENV,
    # Read by the status line script. ANTHROPIC_BASE_URL cannot serve as its
    # base because that points at the data plane, which does not answer the
    # session-usage endpoint the script calls.
    "ROUTER_BASE_URL",
    # Owned because 'ramp router subagents' writes them, and a value naming a
    # Router-served model is meaningless once Router is unconfigured: left
    # behind, every sub-agent spawn would fail against the restored gateway.
    *SUBAGENT_ENV_KEYS,
)
# Keys owned only since the feature that writes them shipped. A state file
# written by an older CLI has no snapshot for these, and that absence means
# "not captured", never "was absent": restoration leaves an uncaptured key
# alone, and merge_states adopts the fresh snapshot taken before this
# configure wrote it.
_ENV_KEYS_OWNED_LATER = (
    "ROUTER_BASE_URL",
    _CLAUDE_AI_CONNECTORS_ENV,
    *SUBAGENT_ENV_KEYS,
)
# Keys plan_configuration itself writes. Tier settings are applied separately
# through plan_subagent_update, so this first pass must not claim values it
# merely passed through: doing so would make a later unconfigure mistake the
# user's own tier edit for a Router write and replace it with an older snapshot.
_CONFIGURE_WRITTEN_ENV_KEYS = tuple(
    key for key in _OWNED_ENV_KEYS if key not in SUBAGENT_ENV_KEYS
)
_OWNED_TOP_LEVEL_KEYS = ("model", "disableClaudeAiConnectors")
_CONFIGURE_WRITTEN_TOP_LEVEL_KEYS = ("model",)
_LEGACY_TOP_LEVEL_KEYS = ("model",)
_ROUTER_MODEL_PREFIX = "claude-router-"

# Selects Router's Claude Code view of /v1/models, where models whose ids are
# not Claude-shaped appear under compatibility aliases. Presentation only; it
# grants no additional access.
GATEWAY_CLIENT_HEADER = "X-Gateway-Client"
GATEWAY_CLIENT_HEADER_VALUE = "claude-code"
MODEL_VIEW_HEADER = "X-Gateway-Model-View"

_STATE_FILENAME = "ramp-router-state.json"
_ORIGINAL_SETTINGS_FILENAME = "original.settings.json"
# Records that this CLI created the overlay, so unconfigure removes only a
# file it wrote and never one the user happened to name the same.
ORIGINAL_SETTINGS_STATE_KEY = "original_settings"
# Records what the overlay held when it was written, so one the user has since
# edited is left alone instead of deleted.
ORIGINAL_SETTINGS_DIGEST_KEY = "original_settings_digest"


def settings_path() -> Path:
    """Locate Claude Code's user settings.

    This is a user-level integration, so the project-level .claude/settings.json
    files are deliberately not touched.
    """
    configured = os.environ.get(CLAUDE_SETTINGS_ENV)
    home = Path(configured).expanduser() if configured else Path.home() / ".claude"
    return home / "settings.json"


def read_settings(path: Path) -> dict:
    """Read existing settings, refusing to overwrite a file we cannot parse."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise click.ClickException(
            f"Could not read Claude Code settings {path}: {exc}. "
            "Fix or move the file, then try again."
        ) from None
    if not isinstance(data, dict):
        raise click.ClickException(
            f"Could not read Claude Code settings {path}: "
            "the top-level value must be an object."
        )
    return data


# Claude Code's user config (distinct from settings.json). It carries caches
# Claude Code maintains for itself, including the Anthropic account's
# per-model entitlements under this key.
USER_CONFIG_FILENAME = ".claude.json"
_MODEL_ACCESS_CACHE_KEY = "modelAccessCache"
# Matches every Fable-family model id ("claude-fable-5", dated variants, and
# future Fable releases) without touching any other family.
_FABLE_MODEL_ID_MARKER = "claude-fable"
# One re-read after a lost race with a concurrent Claude Code write. More
# attempts would only matter under sustained rewriting, where next configure
# or refresh gets another chance anyway.
_SCRUB_ATTEMPTS = 2


def user_config_path() -> Path:
    """Locate Claude Code's user config (~/.claude.json).

    Claude Code keeps this file at the root of CLAUDE_CONFIG_DIR when that is
    set, and directly in the home directory otherwise — unlike settings.json,
    which lives under ~/.claude by default.
    """
    configured = os.environ.get(CLAUDE_SETTINGS_ENV)
    if configured:
        return Path(configured).expanduser() / USER_CONFIG_FILENAME
    return Path.home() / USER_CONFIG_FILENAME


def scrub_stale_fable_access(path: Path) -> bool:
    """Drop cached not-entitled Fable verdicts that hide Router's Fable row.

    When Claude Code talks to Anthropic directly it caches the account's
    per-model entitlements in its user config, and its /model picker keeps
    trusting a cached ``claude-fable-5: entitled=false`` verdict after the
    session is pointed at Router: the Fable row this setup provides through
    ANTHROPIC_DEFAULT_FABLE_MODEL disappears from the picker while the model
    stays perfectly callable. Router sessions never refresh the entitlement
    cache, so without this the stale verdict persists indefinitely.

    Only Fable-family records claiming no entitlement are removed. The rest
    of the cache is Anthropic-owned state that direct Anthropic use rebuilds
    on its own, and nothing here is snapshotted for unconfigure: restoring a
    stale verdict would hide the row again. A missing, unreadable, or
    unexpectedly shaped file is left alone — the file belongs to Claude Code,
    and a best-effort cleanup must not break it.

    Returns whether the file changed.
    """
    # Follow a dotfiles-style symlink so the replacement lands in the link's
    # target and the link itself survives.
    try:
        target = path.resolve(strict=True)
    except OSError:
        return False
    for _ in range(_SCRUB_ATTEMPTS):
        try:
            raw = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return False
        try:
            config = json.loads(raw)
        except json.JSONDecodeError:
            return False
        if not isinstance(config, dict):
            return False
        records = config.get(_MODEL_ACCESS_CACHE_KEY)
        if not isinstance(records, list):
            return False

        def stale_fable_verdict(record: object) -> bool:
            return (
                isinstance(record, dict)
                and isinstance(record.get("apiName"), str)
                and _FABLE_MODEL_ID_MARKER in record["apiName"]
                and record.get("entitled") is False
            )

        retained = [record for record in records if not stale_fable_verdict(record)]
        if len(retained) == len(records):
            return False
        config[_MODEL_ACCESS_CACHE_KEY] = retained
        try:
            replaced = _replace_user_config_if_unchanged(
                target, raw, json.dumps(config, indent=2) + "\n"
            )
        except OSError:
            # The row stays hidden until a later run gets to write, which
            # beats failing a configure whose settings writes already landed.
            return False
        if replaced:
            return True
        # Claude Code persisted a newer document while this one was being
        # prepared; its writes must win, so retry against what it wrote.
    return False


def _replace_user_config_if_unchanged(path: Path, expected: str, content: str) -> bool:
    """Atomically replace a file Claude Code also rewrites, keeping its mode.

    The file is re-read just before the rename and left alone when it no
    longer matches ``expected``: a concurrent Claude Code session persisted a
    newer document, and discarding its writes to remove a cache record would
    be a bad trade. The remaining check-to-rename window is the same
    last-writer-wins race Claude Code's own concurrent sessions already have,
    with a whole document winning either way — never a torn file.

    Returns whether the replacement happened.
    """
    mode = stat.S_IMODE(os.stat(path).st_mode)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(tmp_name, mode)
        if path.read_text(encoding="utf-8") != expected:
            os.unlink(tmp_name)
            return False
        os.replace(tmp_name, path)
        return True
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _is_router_model(value: object) -> bool:
    """Return whether Claude Code's selected model is one of Router's aliases."""
    return isinstance(value, str) and value.startswith(_ROUTER_MODEL_PREFIX)


def _environment(settings: dict, path: Path) -> dict:
    environment = settings.get("env", {})
    if not isinstance(environment, dict):
        raise click.ClickException(
            f"Could not update Claude Code settings {path}: 'env' must be an object."
        )
    return environment


def _custom_header_value(headers: object, name: str) -> str | None:
    if not isinstance(headers, str):
        return None
    expected = name.casefold()
    for line in headers.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().casefold() == expected:
            return value.strip()
    return None


def _set_custom_header(headers: object, name: str, value: str | None) -> str | None:
    """Set one Claude custom header without disturbing unrelated lines."""
    lines = headers.splitlines() if isinstance(headers, str) else []
    expected = name.casefold()
    retained = []
    for line in lines:
        key, separator, _ = line.partition(":")
        if separator and key.strip().casefold() == expected:
            continue
        retained.append(line)
    if value is not None:
        retained.append(f"{name}: {value}")
    rendered = "\n".join(retained)
    return rendered or None


def model_view(settings: dict, path: Path) -> str:
    environment = _environment(settings, path)
    return (
        "all"
        if _custom_header_value(
            environment.get("ANTHROPIC_CUSTOM_HEADERS"), MODEL_VIEW_HEADER
        )
        == "all"
        else "compact"
    )


def statusline_path(path: Path) -> Path:
    """Where the managed status line script lives, beside the settings file."""
    return path.parent / "ramp-router-statusline"


def statusline_command(path: Path) -> str:
    """The command Claude Code runs for the managed status line."""
    # Quoted because Claude Code hands the command to a shell, and the settings
    # directory is ordinarily under a home directory that can contain spaces.
    return shlex.quote(str(statusline_path(path)))


def _statusline_slot_is_ours(settings: dict, path: Path) -> bool:
    """Report whether the statusLine slot is empty or holds our own command.

    A status line the user configured themselves is never replaced; theirs is
    a choice this command has no standing to override, so it is left alone
    rather than recorded and restored.
    """
    current = settings.get("statusLine")
    if current is None:
        return True
    if not isinstance(current, dict) or current.get("type") != "command":
        return False
    command = current.get("command")
    return isinstance(command, str) and command in {
        statusline_command(path),
        str(statusline_path(path)),
    }


# The exact SessionStart entry this CLI registered — or the user-managed
# sentinel when a user hook already invokes the sync — so only that entry is
# ever replaced or removed.
SESSION_START_HOOK_STATE_KEY = "session_start_hook"
SESSION_START_HOOKS_PRESENT_STATE_KEY = "session_start_hooks_present"
SESSION_START_PRESENT_STATE_KEY = "session_start_present"
# Headroom for interpreter startup; the hook itself only stats a file or
# spawns a detached refresh. Set explicitly because Claude's default hook
# timeout (60s) could stall session start that long; Codex's group sets no
# timeout since its default is already acceptable there.
SESSION_START_HOOK_TIMEOUT_SECONDS = 20
# Fresh and resumed sessions both read the installed artifacts.
_SESSION_START_MATCHER = "startup|resume"


def session_start_hook_entry(command: str) -> dict:
    """The SessionStart entry this CLI registers for the given sync command."""
    return {
        "matcher": _SESSION_START_MATCHER,
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": SESSION_START_HOOK_TIMEOUT_SECONDS,
            }
        ],
    }


def _entry_invokes_sync(entry: object) -> bool:
    """Report whether one SessionStart entry runs the managed sync at all.

    Any-command on purpose: a user hook wrapping the sync already keeps the
    artifacts fresh, so registration stands down. This never establishes
    ownership — only the receipt-recorded entry is ours to touch.
    """
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(
        isinstance(hook, dict)
        and isinstance(hook.get("command"), str)
        and command_runs_session_sync(hook["command"])
        for hook in hooks
    )


def plan_session_start_hook(
    settings: dict, state: dict, command: str
) -> tuple[dict, dict]:
    """Merge the receipt-owned SessionStart sync hook into the settings.

    Replaces only the exact entry the receipt records, which still repairs a
    stale ramp path. A user-authored entry that already invokes the sync is
    left untouched, no managed entry is added, and the skip is recorded as
    user-managed. An unreadable hooks layout is the user's; nothing is
    registered.
    """
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return settings, state
    session_start = hooks.get("SessionStart", [])
    if not isinstance(session_start, list):
        return settings, state
    state = dict(state)
    state.setdefault(SESSION_START_HOOKS_PRESENT_STATE_KEY, "hooks" in settings)
    state.setdefault(SESSION_START_PRESENT_STATE_KEY, "SessionStart" in hooks)
    recorded = state.get(SESSION_START_HOOK_STATE_KEY)
    owned_index = next(
        (index for index, item in enumerate(session_start) if item == recorded), None
    )
    if any(
        _entry_invokes_sync(item)
        for index, item in enumerate(session_start)
        if index != owned_index
    ):
        # Two registrations would run the sync twice; the user's supersedes.
        retained = [
            item for index, item in enumerate(session_start) if index != owned_index
        ]
        updated_state = {**state, SESSION_START_HOOK_STATE_KEY: SYNC_HOOK_USER_MANAGED}
        if retained == session_start:
            return settings, updated_state
        return {**settings, "hooks": {**hooks, "SessionStart": retained}}, updated_state
    entry = session_start_hook_entry(command)
    if owned_index is not None:
        updated_list = list(session_start)
        updated_list[owned_index] = entry
    else:
        updated_list = [*session_start, entry]
    updated = {**settings, "hooks": {**hooks, "SessionStart": updated_list}}
    return updated, {**state, SESSION_START_HOOK_STATE_KEY: entry}


def plan_session_start_hook_removal(settings: dict, state: dict) -> dict:
    """Remove exactly the SessionStart entry the receipt records.

    User hooks that also invoke the sync — including ones added after Router
    was configured — are never touched; the user-managed sentinel removes
    nothing.
    """
    recorded = state.get(SESSION_START_HOOK_STATE_KEY)
    if not isinstance(recorded, dict):
        return settings
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    session_start = hooks.get("SessionStart")
    if not isinstance(session_start, list):
        return settings
    try:
        owned_index = session_start.index(recorded)
    except ValueError:
        return settings
    retained = session_start[:owned_index] + session_start[owned_index + 1 :]
    updated_hooks = dict(hooks)
    if retained:
        updated_hooks["SessionStart"] = retained
    elif state.get(SESSION_START_PRESENT_STATE_KEY) is True:
        updated_hooks["SessionStart"] = []
    else:
        updated_hooks.pop("SessionStart")
    restored = dict(settings)
    if updated_hooks or state.get(SESSION_START_HOOKS_PRESENT_STATE_KEY) is True:
        restored["hooks"] = updated_hooks
    else:
        restored.pop("hooks", None)
    return restored


def plan_configuration(
    settings: dict,
    path: Path,
    base_url: str,
    api_key: str,
    model: str,
    *,
    usage_base_url: str | None = None,
    statusline: str | None = None,
    model_view_all: bool = False,
) -> tuple[dict, dict]:
    """Return the settings to write and the state needed to undo them.

    The state records whether each owned key existed and what it held, so
    unconfiguring restores the user's values instead of deleting keys they set
    themselves. Settings-file values override the inherited shell environment,
    so a key we remove without restoring silently changes their setup.

    It also records what this command wrote, so unconfiguring can tell a value
    it still owns from one the user has since changed. Restoring a key whose
    value is no longer ours would undo their newer choice.
    """
    environment = _environment(settings, path)
    top_level = {}
    for key in _OWNED_TOP_LEVEL_KEYS:
        value = settings.get(key)
        # A Router model selected during an earlier Router session is not part
        # of the native setup. Treat it as absent so a fresh configure cannot
        # preserve it in the escape overlay or restore it on unconfigure.
        present = key in settings and not (key == "model" and _is_router_model(value))
        top_level[key] = {"present": present, "value": value if present else None}
    state = {
        "env": {
            key: {"present": key in environment, "value": environment.get(key)}
            for key in _OWNED_ENV_KEYS
        },
        "top_level": top_level,
        CUSTOM_HEADERS_STATE_KEY: {
            GATEWAY_CLIENT_HEADER: GATEWAY_CLIENT_HEADER_VALUE,
            MODEL_VIEW_HEADER: "all" if model_view_all else None,
        },
    }
    custom_headers = _set_custom_header(
        environment.get("ANTHROPIC_CUSTOM_HEADERS"),
        GATEWAY_CLIENT_HEADER,
        GATEWAY_CLIENT_HEADER_VALUE,
    )
    custom_headers = _set_custom_header(
        custom_headers, MODEL_VIEW_HEADER, "all" if model_view_all else None
    )
    updated = dict(settings)
    updated["env"] = {
        **environment,
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": api_key,
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
        _CLAUDE_AI_CONNECTORS_ENV: "false",
    }
    if custom_headers is None:
        updated["env"].pop("ANTHROPIC_CUSTOM_HEADERS", None)
    else:
        updated["env"]["ANTHROPIC_CUSTOM_HEADERS"] = custom_headers
    # Two auth variables at once is ambiguous to Claude Code, so the one we do
    # not own is cleared rather than left to compete. It is restored on
    # unconfigure like every other owned key.
    updated["env"].pop("ANTHROPIC_API_KEY", None)
    if usage_base_url is not None:
        updated["env"]["ROUTER_BASE_URL"] = usage_base_url
    updated["model"] = model
    if statusline is not None and _statusline_slot_is_ours(settings, path):
        updated["statusLine"] = {"type": "command", "command": statusline}
        # Recorded only when this command actually takes the slot. A status
        # line the user configured is not Router state: capturing it would make
        # a later refresh treat their setting as ours to restore or remove.
        state["statusLine"] = {
            "present": "statusLine" in settings,
            "value": settings.get("statusLine"),
            "written": updated["statusLine"],
        }
    state["written"] = {
        "env": {key: updated["env"].get(key) for key in _CONFIGURE_WRITTEN_ENV_KEYS},
        "top_level": {
            key: updated.get(key) for key in _CONFIGURE_WRITTEN_TOP_LEVEL_KEYS
        },
    }
    return updated, state


def original_settings_path(path: Path) -> Path:
    """Where the settings Router displaced live as a runnable overlay."""
    return path.parent / _ORIGINAL_SETTINGS_FILENAME


def original_settings_command(path: Path, *, use_default_model: bool = False) -> str:
    """The command that runs Claude Code on the setup Router replaced.

    Written against ``~`` when the file sits under the home directory, since
    this is meant to be typed. Only the part after the tilde is quoted: a
    quoted tilde is a literal one, and the shell would look for a directory
    actually named "~".
    """
    settings = original_settings_path(path)
    model_option = " --model default" if use_default_model else ""
    try:
        relative = settings.relative_to(Path.home())
    except (ValueError, RuntimeError):
        return f"claude --settings {shlex.quote(str(settings))}{model_option}"
    return f"claude --settings ~/{shlex.quote(str(relative))}{model_option}"


def plan_original_settings(state: dict) -> dict:
    """Build an overlay that puts one session back on the previous provider.

    Claude Code has no profiles, but ``--settings`` overrides the keys it names
    for a single session and leaves the rest of the file alone. A key Router
    introduced is written as an empty string rather than omitted: omitting it
    would inherit the Router value from user settings, and Claude Code
    documents an empty value as unset for provider selection.

    ``model`` is the exception. It is a top-level key, where the empty-value
    rule does not apply. When the previous settings did not pin one, leave it
    out and let Claude Code resolve its default for the restored provider.
    """
    environment = {}
    for key in _OWNED_ENV_KEYS:
        captured = state.get("env", {}).get(key)
        if not isinstance(captured, dict):
            continue
        environment[key] = captured["value"] if captured.get("present") else ""
    overlay: dict = {"env": environment}
    for key in _OWNED_TOP_LEVEL_KEYS:
        captured = state.get("top_level", {}).get(key)
        if (
            isinstance(captured, dict)
            and captured.get("present")
            and not (key == "model" and _is_router_model(captured.get("value")))
        ):
            overlay[key] = captured["value"]
    return overlay


def plan_subagent_update(
    settings: dict,
    path: Path,
    state: dict,
    tiers: dict[str, str | None],
    *,
    display_names: dict[str, str] | None = None,
    automatic_tiers: set[str] | None = None,
) -> tuple[dict, dict]:
    """Return the settings and state after pointing sub-agent tiers at models.

    A string points the tier's variable at that model and writes its real
    display name. Claude Code otherwise labels the tier as Sonnet, Opus, Haiku,
    or Fable even when a gateway runs a different model. None removes the model
    and Router-owned name override so the tier falls back to Claude Code's own
    default.

    ``automatic_tiers`` records which writes came from configure defaults.
    Passing an empty set marks every tier in this update as a manual choice.
    Omitting it preserves provenance for callers that only update the receipt.

    The state advances the same way plan_configuration records its writes: the
    written values move to what Router owns now, and a setting this state never
    captured takes its snapshot from the settings as they are before this
    write — the old file cannot say what the key held earlier, and unconfigure
    has to restore the user's value, not delete it.
    """
    environment = _environment(settings, path)
    updated_state = {
        **state,
        "env": dict(state["env"]),
        "written": {**state["written"], "env": dict(state["written"]["env"])},
    }
    updated_environment = dict(environment)
    display_names = display_names or {}
    for tier, model in tiers.items():
        values = {
            SUBAGENT_TIER_ENV_KEYS[tier]: model,
            SUBAGENT_TIER_NAME_ENV_KEYS[tier]: (
                display_names.get(tier) if model is not None else None
            ),
        }
        for key, value in values.items():
            is_name = key == SUBAGENT_TIER_NAME_ENV_KEYS[tier]
            if is_name and (
                (
                    key in updated_state["written"]["env"]
                    and environment.get(key) != updated_state["written"]["env"][key]
                )
                or (key not in updated_state["written"]["env"] and key in environment)
            ):
                # A claimed name changed since Router wrote it, or an older
                # receipt never claimed the existing name. Either is the
                # user's label and must survive configure, refresh, and reset.
                continue
            # Snapshot on the first write of this key, not the first sighting.
            # A snapshot taken at configure time cannot speak for a value the
            # user hand-set between configure and the first subagents write:
            # restoring that stale snapshot would revert the user's edit.
            if key not in updated_state["written"]["env"]:
                updated_state["env"][key] = {
                    "present": key in environment,
                    "value": environment.get(key),
                }
            if value is None:
                updated_environment.pop(key, None)
            else:
                updated_environment[key] = value
            updated_state["written"]["env"][key] = value
    if automatic_tiers is not None:
        automatic_defaults = dict(state.get(SUBAGENT_DEFAULTS_STATE_KEY, {}))
        for tier, model in tiers.items():
            if tier in automatic_tiers and model is not None:
                automatic_defaults[tier] = model
            else:
                automatic_defaults.pop(tier, None)
        updated_state[SUBAGENT_DEFAULTS_STATE_KEY] = automatic_defaults
    updated = {**settings, "env": updated_environment}
    return updated, updated_state


def plan_automatic_subagent_cleanup(
    settings: dict, path: Path, state: dict
) -> tuple[dict, dict]:
    """Remove only unchanged tier overrides recorded as configure defaults."""
    automatic = state.get(SUBAGENT_DEFAULTS_STATE_KEY, {})
    if not automatic:
        return settings, state
    environment = dict(_environment(settings, path))
    updated_state = {
        **state,
        "written": {**state["written"], "env": dict(state["written"]["env"])},
        SUBAGENT_DEFAULTS_STATE_KEY: {},
    }
    written = updated_state["written"]["env"]
    for tier, automatic_model in automatic.items():
        keys = (
            SUBAGENT_TIER_ENV_KEYS[tier],
            SUBAGENT_TIER_NAME_ENV_KEYS[tier],
            SUBAGENT_TIER_DESCRIPTION_ENV_KEYS[tier],
        )
        for key in keys:
            current = environment.get(key)
            if key not in written or current != written[key]:
                continue
            if key == SUBAGENT_TIER_ENV_KEYS[tier] and current != automatic_model:
                continue
            environment.pop(key, None)
            # Preserve the original snapshot and record the absence we now own,
            # so unconfigure can still restore the pre-Router value.
            written[key] = None
    return {**settings, "env": environment}, updated_state


def plan_model_view_update(
    settings: dict, path: Path, state: dict, view: str
) -> tuple[dict, dict]:
    environment = dict(_environment(settings, path))
    current_headers = environment.get("ANTHROPIC_CUSTOM_HEADERS")
    managed = dict(state.get(CUSTOM_HEADERS_STATE_KEY, {}))
    if GATEWAY_CLIENT_HEADER not in managed:
        old_written = state["written"]["env"].get("ANTHROPIC_CUSTOM_HEADERS")
        old_gateway = _custom_header_value(old_written, GATEWAY_CLIENT_HEADER)
        if (
            old_gateway == GATEWAY_CLIENT_HEADER_VALUE
            and _custom_header_value(current_headers, GATEWAY_CLIENT_HEADER)
            == old_gateway
        ):
            managed[GATEWAY_CLIENT_HEADER] = old_gateway
    desired = "all" if view == "all" else None
    rendered = _set_custom_header(current_headers, MODEL_VIEW_HEADER, desired)
    if rendered is None:
        environment.pop("ANTHROPIC_CUSTOM_HEADERS", None)
    else:
        environment["ANTHROPIC_CUSTOM_HEADERS"] = rendered
    managed[MODEL_VIEW_HEADER] = desired
    updated_state = {
        **state,
        CUSTOM_HEADERS_STATE_KEY: managed,
        "written": {
            **state["written"],
            "env": {
                **state["written"]["env"],
                "ANTHROPIC_CUSTOM_HEADERS": rendered,
            },
        },
    }
    return {**settings, "env": environment}, updated_state


def plan_restoration(settings: dict, path: Path, state: dict) -> dict:
    """Return the settings with every owned key put back as it was.

    A key the user has changed since is left alone. Claude Code writes these
    itself when someone picks a different model or points at another gateway,
    and replacing that with a snapshot from before Router was configured would
    silently undo it. A model in Router's alias namespace is the exception:
    switching between Router models must not make one survive Router itself.
    """
    written = state["written"]
    environment = dict(_environment(settings, path))
    for key, previous in state["env"].items():
        if key == "ANTHROPIC_CUSTOM_HEADERS" and isinstance(
            state.get(CUSTOM_HEADERS_STATE_KEY), dict
        ):
            continue
        if not _still_ours(environment, key, written["env"]):
            continue
        if previous.get("present"):
            environment[key] = previous.get("value")
        else:
            environment.pop(key, None)
    managed_headers = state.get(CUSTOM_HEADERS_STATE_KEY)
    if isinstance(managed_headers, dict):
        current_headers = environment.get("ANTHROPIC_CUSTOM_HEADERS")
        previous = state["env"]["ANTHROPIC_CUSTOM_HEADERS"]
        original_headers = previous.get("value") if previous.get("present") else None
        for name, managed_value in managed_headers.items():
            if _custom_header_value(current_headers, name) != managed_value:
                continue
            current_headers = _set_custom_header(
                current_headers, name, _custom_header_value(original_headers, name)
            )
        if current_headers is None:
            environment.pop("ANTHROPIC_CUSTOM_HEADERS", None)
        else:
            environment["ANTHROPIC_CUSTOM_HEADERS"] = current_headers
    restored = dict(settings)
    if environment:
        restored["env"] = environment
    else:
        # An env object we created and then emptied should not be left behind.
        restored.pop("env", None)
    for key, previous in state["top_level"].items():
        current_is_router_model = key == "model" and _is_router_model(settings.get(key))
        if not current_is_router_model and not _still_ours(
            settings, key, written["top_level"]
        ):
            continue
        previous_is_router_model = key == "model" and _is_router_model(
            previous.get("value")
        )
        if previous.get("present") and not previous_is_router_model:
            restored[key] = previous.get("value")
        else:
            restored.pop(key, None)
    record = state.get("statusLine")
    # No record means Router never managed the slot, so whatever is there is
    # the user's and stays. With one, the same changed-since rule applies as
    # for every other owned key.
    if isinstance(record, dict) and settings.get("statusLine") == record.get("written"):
        if record.get("present"):
            restored["statusLine"] = record.get("value")
        else:
            restored.pop("statusLine", None)
    return restored


def restore_legacy_connector_preference(settings: dict, state: dict) -> dict:
    """Restore the connector preference replaced by older Router setups.

    Claude Code treats ``true`` in any settings source as authoritative, so an
    additional settings file cannot re-enable connectors while this value
    remains in user settings. Change it only when the old receipt proves that
    Router wrote the current value, then put back the captured user preference.
    """
    written = state.get("written", {}).get("top_level", {})
    key = "disableClaudeAiConnectors"
    if settings.get(key) is not True or written.get(key) is not True:
        return settings
    updated = dict(settings)
    previous = state.get("top_level", {}).get(key)
    if isinstance(previous, dict) and previous.get("present"):
        updated[key] = previous.get("value")
    else:
        updated.pop(key)
    return updated


def merge_states(
    previous: dict, fresh: dict, *, preserve_model_view_ownership: bool = False
) -> dict:
    """Carry an existing snapshot forward through a repeat configure.

    The original snapshot is what unconfigure restores, so it is kept, while
    the written values advance to what Router owns now; otherwise configure A,
    configure B, unconfigure would mistake B for a user edit and leave it
    behind. A key the previous state never captured — owned only since it was
    written, or a status line slot Router was not managing then — takes the
    fresh snapshot instead: the old file cannot say what that key held, and
    presuming absence would delete a value the user set in the meantime.
    """
    merged = {
        **previous,
        "env": {**fresh["env"], **previous["env"]},
        "top_level": {**fresh["top_level"], **previous["top_level"]},
        # Written values advance to this configure's writes, except keys it
        # does not write: a tier the subagents command owns keeps its record,
        # or a refresh would orphan that write and unconfigure would leave the
        # Router-only model id behind.
        "written": {
            **fresh["written"],
            "env": {**previous["written"]["env"], **fresh["written"]["env"]},
        },
    }
    fresh_managed_headers = dict(fresh.get(CUSTOM_HEADERS_STATE_KEY, {}))
    if preserve_model_view_ownership:
        # Refresh mirrors the user's current view into the rewritten settings;
        # it does not make a direct edit their own value. Keep the prior claim
        # (or lack of one) so unconfigure removes only a view Router selected.
        fresh_managed_headers.pop(MODEL_VIEW_HEADER, None)
    managed_headers = {
        **previous.get(CUSTOM_HEADERS_STATE_KEY, {}),
        **fresh_managed_headers,
    }
    if managed_headers:
        merged[CUSTOM_HEADERS_STATE_KEY] = managed_headers
    if "statusLine" in fresh:
        record = previous.get("statusLine")
        merged["statusLine"] = (
            {**record, "written": fresh["statusLine"]["written"]}
            if isinstance(record, dict)
            else fresh["statusLine"]
        )
    return merged


def _still_ours(current: dict, key: str, written: dict) -> bool:
    """Report whether a key still holds the value this CLI wrote.

    Membership in ``written`` is required. Otherwise a key the CLI captured
    without ever writing (a tier value the user hand-set before Router, which
    configure records but never claims) would appear equal to a matching
    absence — both sides ``None`` — and get restored to its old value after
    the user deletes it themselves.
    """
    return key in written and current.get(key) == written[key]


def state_path(path: Path) -> Path:
    return path.parent / _STATE_FILENAME


@contextmanager
def settings_lock(path: Path):
    """Serialize a read-modify-write of the settings and state documents.

    Both files are rewritten whole from an in-memory snapshot, so two
    concurrent updates would each keep their own view and the last writer
    would silently drop the first one's change — and could pair a settings
    document with a state receipt describing the other update.
    """
    with advisory_lock(path.parent / ".ramp-router-settings.lock"):
        yield


@contextmanager
def advisory_lock(lock_path: Path):
    """Hold an exclusive advisory lock on a dedicated lock file.

    Same lock shape as the auth token refresh: fcntl on POSIX, msvcrt on
    Windows, and a lock file that is never replaced so the lock stays valid
    while the guarded files are atomically swapped.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:
            _lock_with_msvcrt(lock_file)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                _unlock_with_msvcrt(lock_file)


def _lock_with_msvcrt(lock_file):  # pragma: no cover - exercised on Windows
    lock_file.seek(0, 2)
    if lock_file.tell() == 0:
        lock_file.write(b"\0")
        lock_file.flush()
    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)


def _unlock_with_msvcrt(lock_file):  # pragma: no cover - exercised on Windows
    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def read_state(path: Path) -> dict:
    location = state_path(path)
    if not location.exists():
        raise click.ClickException("Ramp Router is not configured in Claude Code.")
    try:
        state = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise click.ClickException(
            f"Could not read Ramp Router setup state from {location}."
        ) from None
    if not _valid_state(state):
        raise click.ClickException(
            f"Could not read Ramp Router setup state from {location}."
        )
    return state


def _valid_env_keys(keys: set) -> bool:
    # Later-owned keys may be absent from an older state file, which is still
    # honored; anything else missing or unrecognized is a broken state.
    return (
        set(_OWNED_ENV_KEYS) - set(_ENV_KEYS_OWNED_LATER)
        <= keys
        <= set(_OWNED_ENV_KEYS)
    )


def _valid_state(state: object) -> bool:
    if not isinstance(state, dict):
        return False
    environment = state.get("env")
    top_level = state.get("top_level")
    written = state.get("written")
    record = state.get("statusLine")
    automatic_defaults = state.get(SUBAGENT_DEFAULTS_STATE_KEY, {})
    managed_headers = state.get(CUSTOM_HEADERS_STATE_KEY, {})
    top_level_keys = set(top_level) if isinstance(top_level, dict) else set()
    written_top_level = written.get("top_level") if isinstance(written, dict) else None
    written_top_level_keys = (
        set(written_top_level) if isinstance(written_top_level, dict) else set()
    )
    if (
        not isinstance(environment, dict)
        or not _valid_env_keys(set(environment))
        or not isinstance(top_level, dict)
        or top_level_keys
        not in (set(_LEGACY_TOP_LEVEL_KEYS), set(_OWNED_TOP_LEVEL_KEYS))
        or not isinstance(written, dict)
        or not isinstance(written.get("env"), dict)
        or not _valid_env_keys(set(written["env"]))
        or not isinstance(written_top_level, dict)
        or not (set(_LEGACY_TOP_LEVEL_KEYS) <= written_top_level_keys <= top_level_keys)
        or not isinstance(automatic_defaults, dict)
        or not set(automatic_defaults) <= set(SUBAGENT_TIER_ENV_KEYS)
        or not all(isinstance(model, str) for model in automatic_defaults.values())
        or not isinstance(managed_headers, dict)
        or not set(managed_headers) <= {GATEWAY_CLIENT_HEADER, MODEL_VIEW_HEADER}
        or not all(
            value is None or isinstance(value, str)
            for value in managed_headers.values()
        )
    ):
        return False
    # The status line record exists only for a slot Router actually took.
    if record is not None and not (
        isinstance(record, dict)
        and isinstance(record.get("present"), bool)
        and "value" in record
        and "written" in record
    ):
        return False
    return all(
        isinstance(previous, dict)
        and isinstance(previous.get("present"), bool)
        and "value" in previous
        for previous in (*environment.values(), *top_level.values())
    )
