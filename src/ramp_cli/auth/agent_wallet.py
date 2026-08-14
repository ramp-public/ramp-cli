"""Persistence for standalone Agent Wallet credentials."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ramp_cli.config import settings


def _api_key_path() -> Path:
    return settings.config_dir() / "agent-wallet.key"


def get_api_key() -> str:
    try:
        return _api_key_path().read_text().strip()
    except FileNotFoundError:
        return ""


def save_api_key(api_key: str) -> None:
    _atomic_write(_api_key_path(), api_key)


def _atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            file.write(value)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
