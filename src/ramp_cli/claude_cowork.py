"""Host-side Claude Desktop configuration for Ramp Router.

Claude Cowork's shell runs in an isolated VM, so it cannot configure the
Desktop application that hosts it.  The Ramp CLI runs on the host and can use
the same local configuration library as Claude's own third-party inference UI.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import click

try:
    import fcntl
except ImportError:  # pragma: no cover - Cowork setup is macOS-only
    fcntl = None

CLAUDE_BUNDLE_ID = "com.anthropic.claudefordesktop"
CLAUDE_COWORK_URL = "claude://cowork/"
GATEWAY_CLIENT_HEADER = "X-Gateway-Client"
GATEWAY_CLIENT = "claude-cowork"
PROFILE_NAME = "Ramp Router"
STATE_VERSION = 1

_APP_SUPPORT_OVERRIDE = "RAMP_CLAUDE_DESKTOP_APP_SUPPORT"
_STATE_FILE = "ramp-router-cowork-state.json"
_DESKTOP_CONFIG_FILE = "claude_desktop_config.json"
_LOCAL_AGENT_SESSIONS_DIR = "local-agent-mode-sessions"
_ACCOUNT_SETTINGS_FILE = "cowork_account_settings.json"
_MODEL_SELECTOR_KEY = "__model_selector_state"
_ONE_MILLION_SUFFIX = "[1m]"
_MODEL_HASH_ALPHABET = frozenset("0123456789abcdef")
_MODEL_HASH_MIN_CHARS = 6
_ROUTER_MODEL_ID_MARKER = "-router-"


def _app_support_root() -> Path:
    override = os.environ.get(_APP_SUPPORT_OVERRIDE)
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support"


def state_path() -> Path:
    return _app_support_root() / "Claude" / _STATE_FILE


def _third_party_root() -> Path:
    return _app_support_root() / "Claude-3p"


def _desktop_config_path() -> Path:
    return _third_party_root() / _DESKTOP_CONFIG_FILE


def _config_library_path() -> Path:
    return _third_party_root() / "configLibrary"


def _config_meta_path() -> Path:
    return _config_library_path() / "_meta.json"


def _profile_path(profile_id: str) -> Path:
    return _config_library_path() / f"{profile_id}.json"


@contextmanager
def transaction_lock():
    """Serialize all Cowork configuration read-modify-write transactions."""
    if fcntl is None:  # pragma: no cover - rejected by the macOS preflight
        raise click.ClickException(
            "Automatic Claude Cowork setup requires macOS file locking."
        )
    lock_path = state_path().parent / ".ramp-router-cowork.lock"
    lock_file = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a+b")
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    except OSError as exc:
        if lock_file is not None:
            try:
                lock_file.close()
            except OSError:
                pass
        raise click.ClickException(
            f"Could not lock Claude Cowork configuration {lock_path}: {exc}"
        ) from None
    try:
        yield
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def _read_object(path: Path, label: str) -> tuple[dict, bool]:
    if not path.exists():
        return {}, False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise click.ClickException(f"Could not read {label} {path}: {exc}") from None
    if not isinstance(value, dict):
        raise click.ClickException(
            f"Could not read {label} {path}: expected an object."
        )
    return value, True


def _write_private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _snapshot_key(value: dict, key: str) -> dict:
    return {"present": key in value, "value": value.get(key)}


def _restore_key(value: dict, key: str, snapshot: object) -> None:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("present"), bool):
        raise click.ClickException("Could not read the Claude Cowork restore state.")
    if snapshot["present"]:
        value[key] = snapshot.get("value")
    else:
        value.pop(key, None)


def _restore_owned_key(
    value: dict,
    key: str,
    snapshot: object,
    *,
    written_present: bool,
    written_value: object = None,
) -> None:
    """Restore a key only while it still has the value written by this CLI."""
    if key in value:
        if not written_present or value[key] != written_value:
            return
    elif written_present:
        return
    _restore_key(value, key, snapshot)


def _credential_marker(api_key: str, *, salt: str | None = None) -> dict[str, str]:
    raw_salt = bytes.fromhex(salt) if salt is not None else secrets.token_bytes(16)
    return {
        "salt": raw_salt.hex(),
        "digest": hmac.new(
            raw_salt, api_key.encode("utf-8"), hashlib.sha256
        ).hexdigest(),
    }


def _credential_matches(api_key: object, marker: object) -> bool:
    if not isinstance(api_key, str) or not isinstance(marker, dict):
        return False
    salt = marker.get("salt")
    digest = marker.get("digest")
    if not isinstance(salt, str) or not isinstance(digest, str):
        return False
    try:
        actual = _credential_marker(api_key, salt=salt)["digest"]
    except ValueError:
        return False
    return hmac.compare_digest(actual, digest)


def gateway_base_url(router_base_url: str) -> str:
    """Return the base Claude expects before it appends its own ``/v1``."""
    value = router_base_url.strip().rstrip("/")
    if value.endswith("/v1"):
        value = value[:-3].rstrip("/")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        # Accessing port also validates malformed/non-numeric authorities.
        parsed.port
    except ValueError:
        raise click.ClickException(
            "Claude Cowork requires a valid HTTPS Router base URL."
        ) from None
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise click.ClickException("Claude Cowork requires an HTTPS Router base URL.")
    return value


def read_setup_file(path: Path) -> tuple[str, str]:
    """Read the browser handoff without ever rendering its credential."""
    try:
        setup, _ = _read_object(path, "Ramp Router setup file")
    except click.ClickException:
        raise click.BadParameter(
            f"{path} is not valid Ramp Router setup JSON. Download a new file "
            "from Router and try again.",
            param_hint="'--setup-file'",
        ) from None
    api_key = setup.get("api_key")
    base_url = setup.get("base_url")
    if (
        setup.get("version") != 1
        or setup.get("name") != PROFILE_NAME
        or not isinstance(api_key, str)
        or not api_key.strip()
        or not isinstance(base_url, str)
        or not base_url.strip()
    ):
        raise click.BadParameter(
            f"The Ramp Router setup file {path} is invalid. Download a new one "
            "from Router and try again.",
            param_hint="'--setup-file'",
        )
    # Validate before the caller changes app state. The app-facing value is
    # normalized later, but keeping /v1 here lets model discovery use the same
    # endpoint the setup file described.
    try:
        gateway_base_url(base_url)
    except click.ClickException as exc:
        raise click.BadParameter(exc.message, param_hint="'--setup-file'") from None
    return base_url.strip().rstrip("/"), api_key.strip()


def _profile(api_key: str, router_base_url: str) -> dict:
    return {
        "inferenceProvider": "gateway",
        "inferenceGatewayBaseUrl": gateway_base_url(router_base_url),
        "inferenceCredentialKind": "static",
        "inferenceGatewayApiKey": api_key,
        "inferenceGatewayAuthScheme": "bearer",
        "inferenceCustomHeaders": {GATEWAY_CLIENT_HEADER: GATEWAY_CLIENT},
        "modelDiscoveryEnabled": True,
    }


def _ensure_macos() -> None:
    if sys.platform != "darwin":
        raise click.ClickException(
            "Automatic Claude Cowork setup currently supports macOS only."
        )


def _run_quiet(command: list[str], *, timeout: float = 15) -> int:
    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        ).returncode
    except (OSError, subprocess.SubprocessError):
        return -1


def _claude_is_running() -> bool:
    return _run_quiet(["pgrep", "-x", "Claude"], timeout=2) == 0


def _ensure_claude_installed() -> None:
    if _run_quiet(["open", "-Ra", "Claude"], timeout=5) != 0:
        raise click.ClickException("Claude Desktop is not installed on this Mac.")


def preflight() -> None:
    """Validate host requirements before any authenticated Router request."""
    _ensure_macos()
    _ensure_claude_installed()


def is_available() -> bool:
    """Report whether this host can run automatic Cowork setup at all.

    The configure picker uses this to decide whether Cowork is worth
    offering. It answers quietly where preflight raises, because a missing
    Claude Desktop is a reason to leave Cowork off a menu, not an error.
    """
    if sys.platform != "darwin":
        return False
    return _run_quiet(["open", "-Ra", "Claude"], timeout=5) == 0


def _quit_claude() -> None:
    if not _claude_is_running():
        return
    script = f'tell application id "{CLAUDE_BUNDLE_ID}" to quit'
    if _run_quiet(["osascript", "-e", script]) != 0:
        raise click.ClickException(
            "Claude Desktop could not be closed. Quit it and run the command again."
        )
    deadline = time.monotonic() + 15
    while _claude_is_running() and time.monotonic() < deadline:
        time.sleep(0.25)
    if _claude_is_running():
        raise click.ClickException(
            "Claude Desktop did not finish closing. Quit it and run the command again."
        )


def _launch_claude(
    *, fresh_cowork: bool = False, failure_message: str | None = None
) -> None:
    command = (
        ["open", "-b", CLAUDE_BUNDLE_ID, CLAUDE_COWORK_URL]
        if fresh_cowork
        else ["open", "-b", CLAUDE_BUNDLE_ID]
    )
    if _run_quiet(command, timeout=10) != 0:
        raise click.ClickException(
            failure_message
            or "Ramp Router was configured, but Claude Desktop could not be reopened. "
            "Open Claude manually to finish setup."
        )


def _read_state() -> dict | None:
    path = state_path()
    if not path.exists():
        return None
    state, _ = _read_object(path, "Claude Cowork restore state")
    if state.get("version") != STATE_VERSION:
        raise click.ClickException(
            f"Could not read the Claude Cowork restore state {path}: "
            "unsupported version."
        )
    profile_id = state.get("profile_id")
    try:
        uuid.UUID(profile_id)
    except (AttributeError, TypeError, ValueError):
        raise click.ClickException(
            f"Could not read the Claude Cowork restore state {path}."
        ) from None
    return state


def _new_state(api_key: str, router_base_url: str) -> dict:
    desktop_config, desktop_existed = _read_object(
        _desktop_config_path(), "Claude Desktop configuration"
    )
    meta, meta_existed = _read_object(
        _config_meta_path(), "Claude Desktop configuration library"
    )
    entries = meta.get("entries", [])
    if not isinstance(entries, list):
        raise click.ClickException(
            f"Could not read Claude Desktop configuration library "
            f"{_config_meta_path()}: 'entries' must be an array."
        )
    return {
        "version": STATE_VERSION,
        "profile_id": str(uuid.uuid4()),
        "credential_marker": _credential_marker(api_key),
        "gateway_base_url": gateway_base_url(router_base_url),
        "desktop_config": {
            "existed": desktop_existed,
            "deploymentMode": _snapshot_key(desktop_config, "deploymentMode"),
            "awaitingSignIn": _snapshot_key(desktop_config, "awaitingSignIn"),
        },
        "config_library": {
            "existed": meta_existed,
            "appliedId": _snapshot_key(meta, "appliedId"),
        },
    }


def _restore_document(path: Path, value: dict, existed: bool) -> None:
    """Best-effort rollback for a document touched by one configure run."""
    try:
        if existed:
            _write_private_json(path, value)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _configure_stopped(api_key: str, router_base_url: str, profile: dict) -> Path:
    """Write the Cowork profile after Claude Desktop has stopped."""
    previous_state = _read_state()
    if previous_state is None:
        state = _new_state(api_key, router_base_url)
    else:
        # Keep the original restore snapshot across key rotations.
        state = json.loads(json.dumps(previous_state))
        state["credential_marker"] = _credential_marker(api_key)
        state["gateway_base_url"] = profile["inferenceGatewayBaseUrl"]

    profile_id = state["profile_id"]
    profile_path = _profile_path(profile_id)
    previous_profile, profile_existed = _read_object(
        profile_path, "Claude Cowork Router profile"
    )
    desktop_config, desktop_existed = _read_object(
        _desktop_config_path(), "Claude Desktop configuration"
    )
    previous_desktop = json.loads(json.dumps(desktop_config))
    meta, meta_existed = _read_object(
        _config_meta_path(), "Claude Desktop configuration library"
    )
    previous_meta = json.loads(json.dumps(meta))
    entries = meta.get("entries", [])
    if not isinstance(entries, list):
        raise click.ClickException(
            f"Could not read Claude Desktop configuration library "
            f"{_config_meta_path()}: 'entries' must be an array."
        )

    desktop_config["deploymentMode"] = "3p"
    desktop_config.pop("awaitingSignIn", None)
    meta["entries"] = [
        entry
        for entry in entries
        if not (isinstance(entry, dict) and entry.get("id") == profile_id)
    ]
    meta["entries"].append({"id": profile_id, "name": PROFILE_NAME})
    meta["appliedId"] = profile_id

    try:
        # The receipt is first so an interrupted run can still be undone. The
        # deployment mode is last so Claude never starts against a missing
        # profile or library entry.
        _write_private_json(state_path(), state)
        _write_private_json(profile_path, profile)
        _write_private_json(_config_meta_path(), meta)
        _write_private_json(_desktop_config_path(), desktop_config)
    except BaseException as exc:
        _restore_document(_desktop_config_path(), previous_desktop, desktop_existed)
        _restore_document(_config_meta_path(), previous_meta, meta_existed)
        _restore_document(profile_path, previous_profile, profile_existed)
        _restore_document(
            state_path(), previous_state or {}, previous_state is not None
        )
        if isinstance(exc, OSError):
            raise click.ClickException(
                f"Could not configure Claude Cowork: {exc}"
            ) from None
        raise

    return profile_path


def _account_settings_paths() -> list[Path]:
    """Every Cowork account-settings document Claude Desktop keeps locally."""
    sessions_root = _third_party_root() / _LOCAL_AGENT_SESSIONS_DIR
    if not sessions_root.is_dir():
        return []
    return sorted(sessions_root.glob(f"*/*/{_ACCOUNT_SETTINGS_FILE}"))


def _model_id_base(identifier: str) -> str:
    """The model id without its trailing 1M-context markers."""
    base = identifier
    while base.lower().endswith(_ONE_MILLION_SUFFIX):
        base = base[: -len(_ONE_MILLION_SUFFIX)]
    return base


def _model_hash_token(identifier: str) -> str | None:
    """The catalog hash Router embeds as a model id's final segment.

    Router has renamed its model ids more than once, but every generation of
    the scheme ends with a hex digest of the catalog entry — sometimes
    truncated to six characters, sometimes longer. That digest is the only
    part of the id that survives a rename, so it is what stale-selection
    migration joins on. Ids whose final segment is not hex (Anthropic's own
    model names) have no token and never join.
    """
    token = _model_id_base(identifier).rsplit("-", 1)[-1].lower()
    if len(token) < _MODEL_HASH_MIN_CHARS:
        return None
    if not set(token) <= _MODEL_HASH_ALPHABET:
        return None
    return token


def _is_router_model_id(identifier: str) -> bool:
    """Whether an id follows Router's generated naming scheme.

    Anthropic's own dated ids also end in an all-hex segment (for example
    ``claude-opus-4-20250514``), so the catalog-hash join must be reserved
    for ids Router generated — every generation of that scheme carries a
    ``-router-`` marker. Joining on a date segment could silently switch a
    selection to an unrelated model.
    """
    return _ROUTER_MODEL_ID_MARKER in _model_id_base(identifier)


def _resolve_current_model_id(
    identifier: str, current_ids: tuple[str, ...]
) -> str | None:
    """Map one persisted model id to the id Router serves for it today.

    Claude sizes a model's context window from the id string alone: an id it
    does not recognize falls into a 200k-token fallback unless the id carries
    the ``[1m]`` marker. A selection persisted before a Router rename keeps
    the old id forever, so a 1M model quietly compacts at a fifth of its
    window. Returns the current id when exactly one entry matches, and None
    when the id is already current or no single match exists — guessing
    between two candidates could silently switch the user's model.
    """
    if identifier in current_ids:
        return None
    base = _model_id_base(identifier)
    renamed = [
        candidate for candidate in current_ids if _model_id_base(candidate) == base
    ]
    if len(renamed) == 1:
        return renamed[0]
    if renamed:
        return None
    if not _is_router_model_id(identifier):
        return None
    token = _model_hash_token(identifier)
    if token is None:
        return None
    matches = []
    for candidate in current_ids:
        if not _is_router_model_id(candidate):
            continue
        candidate_token = _model_hash_token(candidate)
        if candidate_token is None:
            continue
        if candidate_token.startswith(token) or token.startswith(candidate_token):
            matches.append(candidate)
    if len(matches) == 1:
        return matches[0]
    return None


def _migrate_selector_state(selector: dict, current_ids: tuple[str, ...]) -> list[dict]:
    """Rewrite one ``__model_selector_state`` object in place."""
    changes = []
    for surface, preferences in selector.items():
        if not isinstance(preferences, dict):
            continue
        selected = preferences.get("model")
        if isinstance(selected, str):
            replacement = _resolve_current_model_id(selected, current_ids)
            if replacement is not None:
                preferences["model"] = replacement
                changes.append(
                    {
                        "surface": surface,
                        "field": "model",
                        "from": selected,
                        "to": replacement,
                    }
                )
        thinking = preferences.get("thinking_by_model")
        if isinstance(thinking, dict):
            for stale in list(thinking):
                if not isinstance(stale, str):
                    continue
                replacement = _resolve_current_model_id(stale, current_ids)
                if replacement is None or replacement in thinking:
                    continue
                thinking[replacement] = thinking.pop(stale)
                changes.append(
                    {
                        "surface": surface,
                        "field": "thinking_by_model",
                        "from": stale,
                        "to": replacement,
                    }
                )
    return changes


def _migrate_model_selections_stopped(
    model_ids: tuple[str, ...],
    still_safe: Callable[[], bool] | None = None,
) -> tuple[tuple[dict, ...], bool]:
    """Rewrite persisted model selections to ids Router serves today.

    Best-effort by design: these documents belong to Claude Desktop, so one
    that cannot be read or written is left for the app rather than failing
    the caller's configure or refresh. Runs only while Claude Desktop is
    stopped, because the app rewrites these documents from memory and would
    clobber a concurrent migration. ``still_safe`` is consulted immediately
    before each write so a launch that lands mid-pass aborts the remaining
    rewrites; the unavoidable residue — a launch inside the milliseconds
    between that check and the atomic replace — at worst restores the stale
    id when the app flushes, and the next configure or refresh converges it.
    Returns the changes written and whether the pass was aborted.
    """
    if not model_ids:
        return (), False
    migrated: list[dict] = []
    for path in _account_settings_paths():
        try:
            settings, existed = _read_object(path, "Claude Cowork account settings")
        except click.ClickException:
            continue
        if not existed:
            continue
        selector = settings.get(_MODEL_SELECTOR_KEY)
        if not isinstance(selector, dict):
            continue
        changes = _migrate_selector_state(selector, model_ids)
        if not changes:
            continue
        if still_safe is not None and not still_safe():
            return tuple(migrated), True
        try:
            _write_private_json(path, settings)
        except OSError:
            continue
        migrated.extend({"path": str(path), **change} for change in changes)
    return tuple(migrated), False


def claude_is_running() -> bool:
    """Whether Claude Desktop is currently running on this host."""
    return _claude_is_running()


@dataclass(frozen=True)
class ModelSelectionMigration:
    """What one opportunistic stale-selection migration pass did."""

    migrated: tuple[dict, ...]
    skipped_while_running: bool


def migrate_model_selections(model_ids: tuple[str, ...]) -> ModelSelectionMigration:
    """Migrate stale persisted Cowork model selections when it is safe.

    The refresh path calls this in the background, where quitting the user's
    Claude Desktop would be hostile, so a running app skips the pass instead;
    configure covers the deterministic case because it already stops the app.
    """
    if sys.platform != "darwin":
        return ModelSelectionMigration(migrated=(), skipped_while_running=False)
    with transaction_lock():
        if _claude_is_running():
            return ModelSelectionMigration(migrated=(), skipped_while_running=True)
        migrated, aborted = _migrate_model_selections_stopped(
            model_ids, still_safe=lambda: not _claude_is_running()
        )
        return ModelSelectionMigration(migrated=migrated, skipped_while_running=aborted)


def configure(
    api_key: str,
    router_base_url: str,
    model_ids: tuple[str, ...] = (),
) -> tuple[Path, tuple[dict, ...]]:
    """Configure and relaunch Claude Desktop with a Cowork-safe Router profile."""
    preflight()
    api_key = api_key.strip()
    if not api_key:
        raise click.ClickException("The Ramp Router API key cannot be empty.")
    profile = _profile(api_key, router_base_url)

    with transaction_lock():
        _quit_claude()
        try:
            profile_path = _configure_stopped(api_key, router_base_url, profile)
        except BaseException:
            try:
                _launch_claude()
            except click.ClickException:
                pass
            raise

        # While the app is stopped anyway, heal selections that still name
        # ids Router no longer serves: Claude sizes context windows from the
        # id string, and a stale id lands in its 200k unknown-model fallback.
        # Guarded like the refresh pass: a manual Desktop launch between the
        # quit above and this rewrite aborts the remaining writes.
        migrated, _aborted = _migrate_model_selections_stopped(
            model_ids, still_safe=lambda: not _claude_is_running()
        )
        _launch_claude(fresh_cowork=True)
        return profile_path, migrated


def configured_api_key() -> str:
    state = _read_state()
    if state is None:
        raise click.ClickException("Ramp Router is not configured in Claude Cowork.")
    profile, _ = _read_object(
        _profile_path(state["profile_id"]), "Claude Cowork Router profile"
    )
    api_key = profile.get("inferenceGatewayApiKey")
    if not _credential_matches(api_key, state.get("credential_marker")):
        raise click.ClickException(
            "The Claude Cowork Router credential changed after setup. "
            "Run the configure command again to adopt it."
        )
    return str(api_key)


def configured_gateway_base_url() -> str:
    """The Router origin the configured Cowork profile points at.

    Validated against the live profile the way ``configured_api_key``
    validates the credential: an endpoint edited after setup would let a
    refresh fetch one endpoint's catalog and rewrite selections Claude
    resolves against another.
    """
    state = _read_state()
    if state is None:
        raise click.ClickException("Ramp Router is not configured in Claude Cowork.")
    base_url = state.get("gateway_base_url")
    if not isinstance(base_url, str) or not base_url:
        raise click.ClickException(
            f"Could not read the Claude Cowork restore state {state_path()}."
        )
    profile, _ = _read_object(
        _profile_path(state["profile_id"]), "Claude Cowork Router profile"
    )
    if profile.get("inferenceGatewayBaseUrl") != base_url:
        raise click.ClickException(
            "The Claude Cowork Router endpoint changed after setup. "
            "Run the configure command again to adopt it."
        )
    return base_url


def _owned_profile(state: dict) -> dict | None:
    profile, profile_exists = _read_object(
        _profile_path(state["profile_id"]), "Claude Cowork Router profile"
    )
    if not profile_exists:
        return None
    expected_keys = set(_profile("placeholder", "https://router.invalid/v1"))
    if set(profile) != expected_keys or not _credential_matches(
        profile.get("inferenceGatewayApiKey"), state.get("credential_marker")
    ):
        raise click.ClickException(
            "The Claude Cowork Router profile changed after setup, so the CLI "
            "will not remove it automatically."
        )
    if (
        profile.get("inferenceProvider") != "gateway"
        or profile.get("inferenceCredentialKind") != "static"
        or profile.get("inferenceGatewayAuthScheme") != "bearer"
        or profile.get("inferenceCustomHeaders")
        != {GATEWAY_CLIENT_HEADER: GATEWAY_CLIENT}
        or profile.get("modelDiscoveryEnabled") is not True
        or profile.get("inferenceGatewayBaseUrl") != state.get("gateway_base_url")
    ):
        raise click.ClickException(
            "The Claude Cowork Router profile changed after setup, so the CLI "
            "will not remove it automatically."
        )
    return profile


def _unconfigure_stopped(state: dict) -> None:
    """Restore the previous profile after Claude Desktop has stopped."""
    previous_profile = _owned_profile(state)
    profile_existed = previous_profile is not None

    meta, meta_existed = _read_object(
        _config_meta_path(), "Claude Desktop configuration library"
    )
    previous_meta = json.loads(json.dumps(meta))
    entries = meta.get("entries", [])
    if not isinstance(entries, list):
        raise click.ClickException(
            f"Could not read Claude Desktop configuration library "
            f"{_config_meta_path()}: 'entries' must be an array."
        )
    profile_id = state["profile_id"]
    profile_is_active = meta.get("appliedId") == profile_id
    entries = [
        entry
        for entry in entries
        if not (isinstance(entry, dict) and entry.get("id") == profile_id)
    ]
    meta["entries"] = entries
    library_snapshot = state.get("config_library")
    if not isinstance(library_snapshot, dict):
        raise click.ClickException("Could not read the Claude Cowork restore state.")
    if profile_is_active:
        applied_snapshot = library_snapshot.get("appliedId")
        if not isinstance(applied_snapshot, dict) or not isinstance(
            applied_snapshot.get("present"), bool
        ):
            raise click.ClickException(
                "Could not read the Claude Cowork restore state."
            )
        prior = applied_snapshot.get("value")
        ids = [
            entry.get("id")
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        ]
        if not applied_snapshot["present"]:
            meta.pop("appliedId", None)
        elif prior in ids or prior is None or prior == "":
            meta["appliedId"] = prior
        else:
            meta["appliedId"] = ids[0] if ids else ""

    desktop_config, desktop_existed = _read_object(
        _desktop_config_path(), "Claude Desktop configuration"
    )
    previous_desktop = json.loads(json.dumps(desktop_config))
    desktop_snapshot = state.get("desktop_config")
    if not isinstance(desktop_snapshot, dict):
        raise click.ClickException("Could not read the Claude Cowork restore state.")
    if profile_is_active:
        _restore_owned_key(
            desktop_config,
            "deploymentMode",
            desktop_snapshot.get("deploymentMode"),
            written_present=True,
            written_value="3p",
        )
        _restore_owned_key(
            desktop_config,
            "awaitingSignIn",
            desktop_snapshot.get("awaitingSignIn"),
            written_present=False,
        )

    try:
        if desktop_snapshot.get("existed") is False and not desktop_config:
            _desktop_config_path().unlink(missing_ok=True)
        else:
            _write_private_json(_desktop_config_path(), desktop_config)

        meta_is_cli_created_and_empty = (
            library_snapshot.get("existed") is False
            and not entries
            and set(meta) <= {"entries", "appliedId"}
            and meta.get("appliedId", "") == ""
        )
        if meta_is_cli_created_and_empty:
            _config_meta_path().unlink(missing_ok=True)
        else:
            _write_private_json(_config_meta_path(), meta)
        _profile_path(profile_id).unlink(missing_ok=True)
        state_path().unlink()
    except BaseException as exc:
        _restore_document(_desktop_config_path(), previous_desktop, desktop_existed)
        _restore_document(_config_meta_path(), previous_meta, meta_existed)
        _restore_document(
            _profile_path(profile_id), previous_profile or {}, profile_existed
        )
        _restore_document(state_path(), state, True)
        if isinstance(exc, OSError):
            raise click.ClickException(
                f"Could not restore Claude Cowork: {exc}"
            ) from None
        raise


def unconfigure() -> None:
    """Remove the CLI-owned Router profile and restore the previous mode."""
    preflight()
    with transaction_lock():
        if not state_path().exists():
            raise click.ClickException(
                "Ramp Router is not configured in Claude Cowork."
            )

        _quit_claude()
        try:
            state = _read_state()
            if state is None:
                raise click.ClickException(
                    "Ramp Router is not configured in Claude Cowork."
                )
            _unconfigure_stopped(state)
        except BaseException:
            try:
                _launch_claude()
            except click.ClickException:
                pass
            raise

        _launch_claude(
            failure_message=(
                "Ramp Router was removed and the previous Cowork settings were "
                "restored, but Claude Desktop could not be reopened. Open Claude "
                "manually."
            )
        )
