"""Point Claude Code at Ramp Router, reversibly.

Claude Code needs no provider plugin. It supports an Anthropic-compatible
gateway directly through user settings, so configuring it is a matter of
writing the documented environment values and being able to put them back.
"""

import json
import os
import shlex
from pathlib import Path

import click

CLAUDE_SETTINGS_ENV = "CLAUDE_CONFIG_DIR"

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
    # Read by the status line script. ANTHROPIC_BASE_URL cannot serve as its
    # base because that points at the data plane, which does not answer the
    # session-usage endpoint the script calls.
    "ROUTER_BASE_URL",
)
# Keys owned only since the status line shipped. A state file written by an
# older CLI has no snapshot for these, and that absence means "not captured",
# never "was absent": restoration leaves an uncaptured key alone, and
# merge_states adopts the fresh snapshot taken before this configure wrote it.
_ENV_KEYS_OWNED_LATER = ("ROUTER_BASE_URL",)
_OWNED_TOP_LEVEL_KEYS = ("model",)

# Selects Router's Claude Code view of /v1/models, where models whose ids are
# not Claude-shaped appear under compatibility aliases. Presentation only; it
# grants no additional access.
_GATEWAY_CLIENT_HEADER = "X-Gateway-Client: claude-code"

_STATE_FILENAME = "ramp-router-state.json"


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
    state = {
        "env": {
            key: {"present": key in environment, "value": environment.get(key)}
            for key in _OWNED_ENV_KEYS
        },
        "top_level": {
            key: {"present": key in settings, "value": settings.get(key)}
            for key in _OWNED_TOP_LEVEL_KEYS
        },
    }
    updated = dict(settings)
    updated["env"] = {
        **environment,
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": api_key,
        "ANTHROPIC_CUSTOM_HEADERS": _GATEWAY_CLIENT_HEADER,
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
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
        "env": {key: updated["env"].get(key) for key in _OWNED_ENV_KEYS},
        "top_level": {key: updated.get(key) for key in _OWNED_TOP_LEVEL_KEYS},
    }
    return updated, state


def plan_restoration(settings: dict, path: Path, state: dict) -> dict:
    """Return the settings with every owned key put back as it was.

    A key the user has changed since is left alone. Claude Code writes these
    itself when someone picks a different model or points at another gateway,
    and replacing that with a snapshot from before Router was configured would
    silently undo it.
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
        if not _still_ours(settings, key, written["top_level"]):
            continue
        if previous.get("present"):
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
        "written": fresh["written"],
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
    """Report whether a key still holds the value this command wrote."""
    return current.get(key) == written.get(key)


def state_path(path: Path) -> Path:
    return path.parent / _STATE_FILENAME


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
    if (
        not isinstance(environment, dict)
        or not _valid_env_keys(set(environment))
        or not isinstance(top_level, dict)
        or set(top_level) != set(_OWNED_TOP_LEVEL_KEYS)
        or not isinstance(written, dict)
        or not isinstance(written.get("env"), dict)
        or not _valid_env_keys(set(written["env"]))
        or not isinstance(written.get("top_level"), dict)
        or set(written["top_level"]) != set(_OWNED_TOP_LEVEL_KEYS)
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
