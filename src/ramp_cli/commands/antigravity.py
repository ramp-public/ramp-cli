"""Antigravity path, detection, and gateway-catalog helpers for Ramp Router.

Google Antigravity's local ``language_server`` reads user-level LLM gateway and
model-catalog settings from ``~/.gemini/config/config.json`` (top-level
``gateway`` and ``models`` blocks alongside Antigravity's own ``plugins``,
``sidecars``, and ``userSettings`` keys). Setting ``wireProtocol`` to
``"openai"`` routes Cascade chat, streaming, and tool-calling requests through
Ramp Router's ``/v1/chat/completions`` endpoint without modifying the signed
Antigravity app bundle or requiring a Google sign-in.

Antigravity's ``language_server`` maps models outside its built-in catalog into
a fixed 32-slot placeholder pool (``MODEL_PLACEHOLDER_M334..M365``). Ordering
the selected default model and Router's recommended models first guarantees
they never lose a slot, while ``ramp router configure model antigravity --model <id>``
pins any Router model to slot 1 so every Router model can be tested.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import click

GEMINI_DIR_ENV = "RAMP_ANTIGRAVITY_GEMINI_DIR"
APP_SUPPORT_ENV = "RAMP_ANTIGRAVITY_APP_SUPPORT"
_APP_SUPPORT_DIR = Path("Library/Application Support/Antigravity")
_APP_BUNDLES = (
    Path("/Applications/Antigravity.app"),
    Path.home() / "Applications" / "Antigravity.app",
)

APP_BUNDLE_ID = "com.google.antigravity"
_PROCESS_NAME = "Antigravity"
QUIT_WAIT_SECONDS = 15.0

LOCK_FILENAME = ".ramp-antigravity-config.lock"
STATE_FILENAME = "ramp-router-state.json"
MODEL_VIEW_KEY = "antigravity_model_view"
CUSTOM_PLACEHOLDER_LIMIT = 32
CLIENT_HEADER = "X-Ramp-Router-Client"
CLIENT_HEADER_VALUE = "antigravity"

_FLASH_LITE_PATTERNS = ("flash-lite", "haiku", "hydrogen", "terra", "nano", "mini")
_FLASH_PATTERNS = ("flash", "hydrogen", "haiku", "terra", "sol", "mini", "fast")


def gemini_dir() -> Path:
    """Locate the root ``.gemini`` directory used by Antigravity."""
    return Path(os.environ.get(GEMINI_DIR_ENV, "~/.gemini")).expanduser()


def config_dir() -> Path:
    """Locate Antigravity's user config directory (``~/.gemini/config``)."""
    return gemini_dir() / "config"


def config_path() -> Path:
    """Locate Antigravity's user ``config.json``."""
    return config_dir() / "config.json"


def mcp_config_path() -> Path:
    """Locate Antigravity's global MCP config (``~/.gemini/config/mcp_config.json``)."""
    return config_dir() / "mcp_config.json"


def state_path(path: Path | None = None) -> Path:
    """Locate the Ramp Router setup receipt beside Antigravity's config."""
    base = path.parent if path is not None else config_dir()
    return base / STATE_FILENAME


def app_support_dir() -> Path:
    """Locate Antigravity's macOS Application Support directory."""
    configured = os.environ.get(APP_SUPPORT_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / _APP_SUPPORT_DIR


def is_installed() -> bool:
    """Report whether Google Antigravity appears to be present on this machine."""
    if app_support_dir().exists():
        return True
    if (gemini_dir() / "antigravity").exists():
        return True
    if config_path().is_file():
        return True
    if APP_SUPPORT_ENV not in os.environ and GEMINI_DIR_ENV not in os.environ:
        return any(bundle.exists() for bundle in _APP_BUNDLES)
    return False


def _run_quiet(command: list[str], *, timeout: float = 15.0) -> int:
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


def running_pids() -> list[int] | None:
    """PIDs of this user's Antigravity main process.

    Returns ``[]`` when Antigravity is not running, and ``None`` when process
    status cannot be determined (which callers treat conservatively as running).
    Uses ``pgrep -a -u <uid> -x Antigravity`` on macOS so helper processes are
    ignored and ancestor processes are still visible when invoked from inside
    Antigravity's terminal.
    """
    if sys.platform != "darwin":
        return []
    try:
        completed = subprocess.run(
            ["pgrep", "-a", "-u", str(os.getuid()), "-x", _PROCESS_NAME],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode == 1:
        return []
    if completed.returncode != 0:
        return None
    pids: list[int] = []
    for token in (completed.stdout or "").split():
        try:
            pids.append(int(token))
        except ValueError:
            continue
    return pids


def is_running() -> bool:
    """Report whether Antigravity is (or may be) running for the current user."""
    return running_pids() != []


def runs_inside_antigravity(pids: list[int] | None = None) -> bool | None:
    """Report whether this CLI process is running inside Antigravity's terminal.

    Returns ``True`` if an ancestor PID is Antigravity, ``False`` if the
    process tree reaches PID 1 without encountering Antigravity, and ``None``
    when ancestry cannot be determined (callers treat ``None`` conservatively
    and skip auto-quit).
    """
    if sys.platform != "darwin":
        return False
    resolved = running_pids() if pids is None else pids
    if resolved is None:
        return None
    if not resolved:
        return False
    target = set(resolved)
    pid = os.getpid()
    seen: set[int] = set()
    while pid > 1:
        if pid in seen:
            return None
        seen.add(pid)
        if pid in target:
            return True
        try:
            out = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0:
            return None
        try:
            pid = int((out.stdout or "").strip())
        except ValueError:
            return None
    return False


def restart_running(*, action: str = "configured") -> bool:
    """Gracefully quit and reopen Antigravity if it is currently running.

    Never force-kills Antigravity and skips auto-quit when invoked from inside
    Antigravity's own terminal (or when ancestry cannot be determined). Returns
    ``True`` when Antigravity was restarted, ``False`` when it was not running
    or auto-quit was skipped.
    """
    if sys.platform != "darwin":
        return False
    pids = running_pids()
    if pids == []:
        return False
    if pids is not None and runs_inside_antigravity(pids) is not False:
        return False
    failure = (
        f"Ramp Router was {action}, but Antigravity could not be restarted. "
        "Quit and reopen Antigravity manually to apply the configuration."
    )
    if (
        pids is None
        or _run_quiet(
            ["osascript", "-e", f'tell application id "{APP_BUNDLE_ID}" to quit'],
            timeout=15.0,
        )
        != 0
    ):
        raise click.ClickException(failure)
    deadline = time.monotonic() + QUIT_WAIT_SECONDS
    while True:
        current = running_pids()
        if current == []:
            break
        # A quit request can wait for unsaved work; never force-quit.
        if current is None or time.monotonic() >= deadline:
            raise click.ClickException(failure)
        time.sleep(0.25)
    if _run_quiet(["open", "-b", APP_BUNDLE_ID], timeout=10.0) != 0:
        raise click.ClickException(
            f"Ramp Router was {action}, but Antigravity could not be reopened. "
            "Open Antigravity manually to finish setup."
        )
    return True


def read_config(path: Path) -> dict[str, Any]:
    """Read Antigravity's ``config.json`` as a dict, or ``{}`` when absent."""
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise click.ClickException(
            f"Could not read Antigravity config at {path}: {exc}"
        ) from None
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise click.ClickException(
            f"Antigravity config at {path} is not valid JSON: {exc}"
        ) from None
    if not isinstance(parsed, dict):
        raise click.ClickException(
            f"Antigravity config at {path} must be a JSON object."
        )
    return parsed


def receipt_enrolls(path: Path) -> bool:
    """Report whether an Antigravity receipt represents an active setup."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    if not isinstance(state, dict):
        return False
    if state.get("pending") or state.get("unconfigured"):
        return False
    base_url = state.get("base_url")
    return isinstance(base_url, str) and bool(base_url.strip())


def provider_for_model(model_id: str) -> str | None:
    """Map a Router model ID to Antigravity's recognized provider tags."""
    low = model_id.strip().lower()
    if (
        "claude" in low
        or low.startswith("anthropic.")
        or low.startswith(("ramp-opus", "ramp-sonnet", "ramp-haiku", "ramp-fable"))
    ):
        return "anthropic"
    if low.startswith(("gpt-", "o1", "o3", "o4", "openai/")):
        return "openai"
    if "gemini" in low:
        return "google"
    return None


def select_models(
    all_models: Sequence[Any],
    *,
    compact_models: Sequence[Any] | None,
    model_view: str,
    default_model: str,
) -> list[Any]:
    """Order Router models for Antigravity's ``models.list``.

    ``compact`` keeps Router's recommended shortlist. ``all`` leads with
    Router's recommended models followed by the rest of the catalog so
    recommended models are never evicted by Antigravity's 32-slot custom
    placeholder pool. When ``default_model`` falls outside the first 32
    entries (or outside ``compact``), it is promoted to index 0 so it is
    always selectable and active.
    """
    by_id: dict[str, Any] = {}
    for model in all_models:
        if model.id not in by_id:
            by_id[model.id] = model

    recommended: list[Any] = []
    seen: set[str] = set()
    for model in compact_models or ():
        if model.id in by_id and model.id not in seen:
            recommended.append(by_id[model.id])
            seen.add(model.id)

    if model_view == "compact" and recommended:
        ordered = list(recommended)
    else:
        ordered = list(recommended)
        for model in all_models:
            if model.id not in seen:
                ordered.append(model)
                seen.add(model.id)

    if default_model in by_id:
        ids = [model.id for model in ordered]
        if default_model not in ids:
            ordered.insert(0, by_id[default_model])
        elif ids.index(default_model) >= CUSTOM_PLACEHOLDER_LIMIT:
            target = ordered.pop(ids.index(default_model))
            ordered.insert(0, target)
    return ordered


def build_model_entries(selected_models: Sequence[Any]) -> list[dict[str, Any]]:
    """Convert Router models into Antigravity ``models.list`` entries.

    Antigravity's web UI indexes picker entries by ``label`` (``displayName``),
    so duplicate display names across aliases/variants are disambiguated with
    the model ID.
    """
    entries: list[dict[str, Any]] = []
    used_labels: set[str] = set()
    for model in selected_models:
        meta = model.metadata
        base_label = (meta.display_name or "").strip() or model.id
        label = base_label
        if label in used_labels:
            label = f"{base_label} ({model.id})"
        used_labels.add(label)

        entry: dict[str, Any] = {
            "modelId": model.id,
            "displayName": label,
        }
        description = (meta.description or "").strip()
        if description:
            entry["description"] = description
        provider = provider_for_model(model.id)
        if provider is not None:
            entry["provider"] = provider
        if meta.context_window and meta.context_window > 0:
            entry["maxTokens"] = meta.context_window
        if meta.max_output_tokens and meta.max_output_tokens > 0:
            entry["maxOutputTokens"] = meta.max_output_tokens

        modalities = set(meta.input_modalities or ())
        if not modalities or "image" in modalities:
            entry["supportsImages"] = True
        if "file" in modalities or "pdf" in modalities:
            entry["supportsPdf"] = True
        if "video" in modalities:
            entry["supportsVideo"] = True
        if meta.efforts:
            entry["supportsThinking"] = True

        entries.append(entry)
    return entries


def build_tiers(
    selected_models: Sequence[Any], default_model: str
) -> dict[str, list[str]]:
    """Build ``models.tiers`` (``flashLite``, ``flash``, ``pro``) for Antigravity.

    Picks only from the leading catalog window so internal Antigravity utility
    calls (title generation, checkpoints, subagents) resolve to active models.
    """
    active = list(selected_models[:CUSTOM_PLACEHOLDER_LIMIT]) or list(selected_models)
    active_ids = [model.id for model in active]

    def pick(patterns: tuple[str, ...]) -> list[str]:
        found: list[str] = []
        for pattern in patterns:
            for mid in active_ids:
                if pattern in mid.lower() and mid not in found:
                    found.append(mid)
                    if len(found) >= 3:
                        return found
        if not found:
            fallback = (
                default_model
                if default_model in active_ids
                else (active_ids[0] if active_ids else default_model)
            )
            if fallback:
                found.append(fallback)
        return found

    pro: list[str] = []
    if default_model:
        pro.append(default_model)
    for mid in active_ids:
        if mid not in pro:
            pro.append(mid)
        if len(pro) >= 3:
            break

    return {
        "flashLite": pick(_FLASH_LITE_PATTERNS),
        "flash": pick(_FLASH_PATTERNS),
        "pro": pro,
    }


def is_router_gateway(gateway: Any, *, base_url: str | None = None) -> bool:
    """Report whether a ``gateway`` block belongs to Ramp Router."""
    if not isinstance(gateway, dict):
        return False
    custom_headers = gateway.get("customHeaders")
    if (
        isinstance(custom_headers, dict)
        and custom_headers.get(CLIENT_HEADER) == CLIENT_HEADER_VALUE
    ):
        return True
    if base_url:
        url = gateway.get("url")
        if isinstance(url, str) and url.strip().rstrip("/") == base_url.strip().rstrip(
            "/"
        ):
            return True
    return False


def gateway_config(base_url: str, api_key: str) -> dict[str, Any]:
    """Build Antigravity's ``gateway`` block pointing at Ramp Router."""
    return {
        "enabled": True,
        "url": base_url.rstrip("/"),
        "wireProtocol": "openai",
        "apiKey": api_key,
        "customHeaders": {
            CLIENT_HEADER: CLIENT_HEADER_VALUE,
        },
    }
