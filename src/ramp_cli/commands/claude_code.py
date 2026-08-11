"""Point Claude Code at Ramp Router, reversibly.

Claude Code needs no provider plugin. It supports an Anthropic-compatible
gateway directly through user settings, so configuring it is a matter of
writing the documented environment values and being able to put them back.
"""

import json
import os
import shlex
from contextlib import contextmanager
from pathlib import Path

import click

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
# fable)
# that it resolves to a canonical Anthropic model id unless one of these
# variables names a different model. Router serves no Anthropic models, so
# these are how sub-agent tiers are pointed at models Router can serve.
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
_GATEWAY_CLIENT_HEADER = "X-Gateway-Client: claude-code"

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


def plan_configuration(
    settings: dict,
    path: Path,
    base_url: str,
    api_key: str,
    model: str,
    *,
    usage_base_url: str | None = None,
    statusline: str | None = None,
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
    }
    updated = dict(settings)
    updated["env"] = {
        **environment,
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": api_key,
        "ANTHROPIC_CUSTOM_HEADERS": _GATEWAY_CLIENT_HEADER,
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
        _CLAUDE_AI_CONNECTORS_ENV: "false",
    }
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
    descriptions: dict[str, str] | None = None,
    automatic_tiers: set[str] | None = None,
) -> tuple[dict, dict]:
    """Return the settings and state after pointing sub-agent tiers at models.

    A string points the tier's variable at that model and writes its real
    display name and description. Claude Code otherwise labels the tier as
    Sonnet, Opus, Haiku, or Fable even when a gateway runs a different model.
    None removes the model and presentation overrides so the tier falls back
    to Claude Code's own default.

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
    descriptions = descriptions or {}
    for tier, model in tiers.items():
        values = {
            SUBAGENT_TIER_ENV_KEYS[tier]: model,
            SUBAGENT_TIER_NAME_ENV_KEYS[tier]: (
                display_names.get(tier) if model is not None else None
            ),
            SUBAGENT_TIER_DESCRIPTION_ENV_KEYS[tier]: (
                descriptions.get(tier) if model is not None else None
            ),
        }
        for key, value in values.items():
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
        if not _still_ours(environment, key, written["env"]):
            continue
        if previous.get("present"):
            environment[key] = previous.get("value")
        else:
            environment.pop(key, None)
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


def merge_states(previous: dict, fresh: dict) -> dict:
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
    document with a state receipt describing the other update. Same advisory
    lock shape as the auth token refresh.
    """
    lock_path = path.parent / ".ramp-router-settings.lock"
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
