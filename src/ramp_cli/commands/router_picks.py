"""Which saved model Router picked on its own, per harness.

When nobody chose a model, setup saves one for the user: the harness's own
default, or Router's fallback. That pick is Router's to move later, when the
harness's default or Router's fallback changes. A model the user or the app
chose, or a workspace default, is not. Claude Code keeps this record in its
own setup receipt; the other harnesses keep it here, one small file each, keyed by the setup's
config path so separate config homes (two ``CODEX_HOME``s, say) don't share
it. It is written only by the configure or refresh that holds that setup's
lock.

Setups made before this record existed have no entry. For those, a saved
model counts as Router's pick when it is a model older CLIs saved
automatically; someone who chose that same model themselves would be moved
along too and can pick it again.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

from ramp_cli.config.settings import config_dir

_DIRECTORY = "router-default-models"
# The fallback older CLIs saved when nobody chose a model.
LEGACY_PICKS: dict[str, frozenset[str]] = {
    "codex": frozenset({"gpt-6.1-sol"}),
}
_LEGACY_DEFAULT = frozenset({"gpt-5.6-sol"})


_MISSING = object()


def _path(client: str) -> Path:
    return config_dir() / _DIRECTORY / f"{client}.json"


def _setup_key(config_path: Path) -> str:
    try:
        return str(config_path.expanduser().resolve())
    except (OSError, RuntimeError):
        return str(config_path.expanduser().absolute())


def _read(client: str) -> dict[str, object] | None:
    """The recorded picks for ``client`` by setup, {} when none; None if unreadable."""
    try:
        data = json.loads(_path(client).read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    setups = data.get("setups") if isinstance(data, dict) else None
    return setups if isinstance(setups, dict) else {}


def router_pick(client: str, config_path: Path, current: object) -> str | None:
    """``current`` when it is the model Router picked for this setup, else None."""
    if not isinstance(current, str) or not current:
        return None
    setups = _read(client)
    if setups is None:
        return None
    recorded = setups.get(_setup_key(config_path), _MISSING)
    if recorded is _MISSING:
        legacy = LEGACY_PICKS.get(client, _LEGACY_DEFAULT)
        return current if current in legacy else None
    return current if recorded == current else None


def record(client: str, config_path: Path, model: str | None) -> None:
    """Remember ``model`` as Router's own pick for this setup (None: none)."""
    path = _path(client)
    with contextlib.suppress(OSError):
        setups = dict(_read(client) or {})
        setups[_setup_key(config_path)] = model
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps({"setups": setups}) + "\n")
        os.replace(temporary, path)
