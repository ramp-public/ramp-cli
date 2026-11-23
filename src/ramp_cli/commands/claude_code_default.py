"""The model the installed Claude Code starts on when no model is saved.

When nobody has chosen a model (no saved ``model`` and no workspace default),
Router leaves the choice to Claude Code, whose built-in default follows its
own releases. That only works if Router serves that model, so setup asks the
installed Claude Code which model it would start on.

Claude Code has no command that prints its default. A print-mode run reports
the model in its first stream-json event (``system``/``init``) before any
request is made, so the run is pointed at an unreachable local endpoint and
stopped as soon as that event arrives: nothing is sent anywhere and nothing is
billed. The run uses an empty, temporary config directory and drops inherited
``ANTHROPIC_*`` and ``CLAUDE_*`` variables, so the user's own settings and
shell overrides don't masquerade as the built-in default. Organization-managed
settings still apply, as they would to a real session.

The answer is cached per Claude Code version, so the run happens once after
each Claude Code update. Any failure reads as "unknown" (None), and callers
then pin a Router model instead of relying on Claude Code's default.
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
from collections.abc import Iterator
from pathlib import Path

from ramp_cli.config.settings import config_dir

_CACHE_FILENAME = "claude-code-default-model.json"
_PROBE_TIMEOUT_SECONDS = 15.0
# Up to two seconds for Windows to release a stopped process's files.
_CLEANUP_ATTEMPTS = 20
_VERSION_TIMEOUT_SECONDS = 5.0
# Nothing listens here, so the probe can never reach a real endpoint.
_UNREACHABLE_BASE_URL = "http://127.0.0.1:9"
_INHERITED_PREFIXES = ("ANTHROPIC_", "CLAUDE_")


def builtin_default_model(executable: str | None = None) -> str | None:
    """Claude Code's own default model id, or None when it can't be read."""
    executable = executable or shutil.which("claude")
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
    match = re.search(r"\b\d+\.\d+\.\d+\b", result.stdout)
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


@contextlib.contextmanager
def _temporary_home(prefix: str) -> Iterator[str]:
    """A scratch home that is removed even when Windows releases it late.

    Windows keeps a just-stopped process's files open for a moment, so a
    single removal attempt would leave the directory behind.
    """
    home = tempfile.mkdtemp(prefix=prefix)
    try:
        yield home
    finally:
        for _ in range(_CLEANUP_ATTEMPTS):
            shutil.rmtree(home, ignore_errors=True)
            if not os.path.exists(home):
                break
            time.sleep(0.1)


def _probe(executable: str) -> str | None:
    with _temporary_home("ramp-claude-default-") as home:
        Path(home, "settings.json").write_text("{}\n")
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(_INHERITED_PREFIXES)
        }
        environment.update(
            {
                "CLAUDE_CONFIG_DIR": home,
                "ANTHROPIC_BASE_URL": _UNREACHABLE_BASE_URL,
                "ANTHROPIC_AUTH_TOKEN": "ramp-cli-default-model-probe",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_AUTOUPDATER": "1",
            }
        )
        try:
            process = subprocess.Popen(
                [
                    executable,
                    "-p",
                    "ok",
                    "--output-format",
                    "stream-json",
                    "--verbose",
                    "--max-turns",
                    "1",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=home,
                env=environment,
                text=True,
                # Its own process group, so stopping it also stops anything
                # it started.
                start_new_session=os.name != "nt",
            )
        except OSError:
            return None
        try:
            return _read_init_model(process)
        finally:
            _stop(process)


def _read_init_model(process: subprocess.Popen[str]) -> str | None:
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
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(event, dict)
            and event.get("type") == "system"
            and event.get("subtype") == "init"
        ):
            model = event.get("model")
            return model.strip() or None if isinstance(model, str) else None
    return None


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover - exercised on Windows
                # An npm-installed harness is a .cmd shim, so kill() would
                # stop only cmd.exe and leave the real CLI holding files in
                # the temporary home. Stop the whole process tree instead.
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
    with contextlib.suppress(subprocess.SubprocessError, OSError):
        process.wait(timeout=5)
