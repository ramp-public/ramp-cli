"""The model the installed Codex starts on when no model is saved.

The Codex counterpart of ``claude_code_default``: when nobody chose a model
and no workspace default applies, Router saves Codex's own default for the
installed version, as long as Router's Codex catalog lists it, so the setup
follows Codex releases instead of a model the CLI hard-codes.

Codex has no command that prints its default. ``codex exec`` prints the
model in its startup header before it makes any request, so the run is
pointed at an unreachable local endpoint and stopped as soon as the header
names a model: nothing is sent anywhere and nothing is billed. It uses an
empty, temporary ``CODEX_HOME`` and drops inherited ``CODEX_*`` and
``OPENAI_*`` variables, so the user's own config doesn't masquerade as the
built-in default. Organization-managed Codex config still applies, as it
would to a real session.

The answer is cached per Codex version. Any failure reads as "unknown"
(None), and callers fall back to Router's choice.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from ramp_cli.config.settings import config_dir

_CACHE_FILENAME = "codex-default-model.json"
_PROBE_TIMEOUT_SECONDS = 15.0
_VERSION_TIMEOUT_SECONDS = 5.0
# Nothing listens here, so the probe can never reach a real endpoint.
_UNREACHABLE_BASE_URL = "http://127.0.0.1:9/v1"
_INHERITED_PREFIXES = ("CODEX_", "OPENAI_")
_PROBE_KEY_ENV = "RAMP_CODEX_DEFAULT_PROBE_KEY"
_MODEL_LINE = re.compile(r"^model:\s*(\S+)\s*$")
_PROBE_CONFIG = f"""model_provider = "ramp-default-probe"

[model_providers.ramp-default-probe]
name = "ramp-default-probe"
base_url = "{_UNREACHABLE_BASE_URL}"
wire_api = "responses"
env_key = "{_PROBE_KEY_ENV}"
"""


def builtin_default_model(executable: str | Path | None = None) -> str | None:
    """Codex's own default model slug, or None when it can't be read."""
    executable = str(executable) if executable else shutil.which("codex")
    if not executable:
        return None
    version = _version(executable)
    if version is None:
        return None
    cache = _read_cache()
    cached = cache.get(version)
    if isinstance(cached, str) and cached:
        return cached
    model = _probe(executable)
    if model is not None:
        _write_cache({**cache, version: model})
    return model


def _version(executable: str) -> str | None:
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"\b\d+\.\d+\.\d+\S*", result.stdout)
    if result.returncode != 0 or match is None:
        return None
    return match.group(0)


def _cache_path() -> Path:
    return config_dir() / _CACHE_FILENAME


def _read_cache() -> dict[str, object]:
    try:
        data = json.loads(_cache_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(cache: dict[str, object]) -> None:
    path = _cache_path()
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, path)


def _probe(executable: str) -> str | None:
    with tempfile.TemporaryDirectory(prefix="ramp-codex-default-") as home:
        Path(home, "config.toml").write_text(_PROBE_CONFIG)
        workdir = Path(home, "work")
        workdir.mkdir()
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(_INHERITED_PREFIXES)
        }
        environment.update({"CODEX_HOME": home, _PROBE_KEY_ENV: "ramp-cli-probe"})
        try:
            process = subprocess.Popen(
                [
                    executable,
                    "exec",
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "--color",
                    "never",
                    "--sandbox",
                    "read-only",
                    "ok",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=workdir,
                env=environment,
                text=True,
                start_new_session=os.name != "nt",
            )
        except OSError:
            return None
        try:
            return _read_header_model(process)
        finally:
            _stop(process)


def _read_header_model(process: subprocess.Popen[str]) -> str | None:
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.monotonic() + _PROBE_TIMEOUT_SECONDS
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty:
            return None
        if line is None:
            return None
        match = _MODEL_LINE.match(line.strip())
        if match is not None:
            return match.group(1)
    return None


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        with contextlib.suppress(OSError):
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover - exercised on Windows
                process.kill()
    with contextlib.suppress(subprocess.SubprocessError, OSError):
        process.wait(timeout=5)
