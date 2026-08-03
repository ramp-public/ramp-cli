"""Point Claude Code at Ramp Router, reversibly.

Claude Code needs no provider plugin. It supports an Anthropic-compatible
gateway directly through user settings, so configuring it is a matter of
writing the documented environment values and being able to put them back.
"""

import json
import os
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
)
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


def plan_configuration(
    settings: dict, path: Path, base_url: str, api_key: str, model: str
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
    updated["model"] = model
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
    return restored


def _still_ours(current: dict, key: str, written: dict) -> bool:
    """Report whether a key still holds the value this command wrote."""
    return current.get(key) == written[key]


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


def _valid_state(state: object) -> bool:
    if not isinstance(state, dict):
        return False
    environment = state.get("env")
    top_level = state.get("top_level")
    written = state.get("written")
    if (
        not isinstance(environment, dict)
        or set(environment) != set(_OWNED_ENV_KEYS)
        or not isinstance(top_level, dict)
        or set(top_level) != set(_OWNED_TOP_LEVEL_KEYS)
        or not isinstance(written, dict)
        or not isinstance(written.get("env"), dict)
        or set(written["env"]) != set(_OWNED_ENV_KEYS)
        or not isinstance(written.get("top_level"), dict)
        or set(written["top_level"]) != set(_OWNED_TOP_LEVEL_KEYS)
    ):
        return False
    return all(
        isinstance(previous, dict)
        and isinstance(previous.get("present"), bool)
        and "value" in previous
        for previous in (*environment.values(), *top_level.values())
    )
