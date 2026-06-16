"""Ramp agent metadata headers for API requests."""

from __future__ import annotations

import platform

from ramp_cli import __version__ as VERSION
from ramp_cli.client.session import get_session_id

_OS_NAMES = {"darwin": "macos", "linux": "linux", "windows": "windows"}
_DESKTOP_SYSTEMS = {"darwin", "windows"}
_ARCHITECTURES = {
    "aarch64": "arm64",
    "arm64": "arm64",
    "amd64": "amd64",
    "x86_64": "amd64",
}


def agent_headers(harness_name: str | None) -> dict[str, str]:
    """Build the shared runtime/session headers and optional CLI device context."""
    headers = {
        "X-Ramp-Agent-Runtime-Name": "ramp-cli",
        "X-Ramp-Agent-Runtime-Version": VERSION,
        "X-External-Session-Id": get_session_id(),
    }
    if harness_name:
        headers["X-Ramp-Agent-Harness-Name"] = harness_name

    system = platform.system().lower()
    headers["X-Ramp-Agent-Device-Type"] = (
        "desktop" if system in _DESKTOP_SYSTEMS else "unknown"
    )
    os_name = _OS_NAMES.get(system)
    if os_name:
        headers["X-Ramp-Agent-Device-OS"] = os_name
        os_version = platform.mac_ver()[0] if system == "darwin" else platform.release()
        if os_version:
            headers["X-Ramp-Agent-Device-OS-Version"] = os_version

    architecture = _ARCHITECTURES.get(platform.machine().lower())
    if architecture:
        headers["X-Ramp-Agent-Device-Architecture"] = architecture

    return headers
