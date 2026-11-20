import shutil
import tomllib
from pathlib import Path
from urllib.parse import urlparse

from ramp_cli.errors import RampCLIError
from ramp_cli.router_connectors import harnesses

SERVER = "ramp"
URL = "https://mcp.ramp.com/mcp"
_ENTRIES = {
    "claude-code": {"type": "http", "url": URL},
    "codex": {"url": URL},
    "opencode": {"type": "remote", "url": URL, "enabled": True},
    "cursor": {"url": URL},
}
_PROD_ROUTER_HOSTS = frozenset({"api.router.com", "internal-api.router.com"})


def should_configure(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host in _PROD_ROUTER_HOSTS


def desktop_entry() -> dict | None:
    if shutil.which("npx") is None:
        return None
    return {"command": "npx", "args": ["-y", "mcp-remote", URL]}


def configure(harness: str, base_url: str) -> None:
    """Add the Ramp MCP server if absent. Failures never block Router setup."""
    entry = _ENTRIES.get(harness)
    if entry is None or not should_configure(base_url):
        return
    try:
        path = (
            Path.home() / ".cursor" / "mcp.json"
            if harness == "cursor"
            else harnesses.config_path(harness)
        )
        with harnesses._lock(harness, path):
            text, data, current = harnesses._read(path, harness, server=SERVER)
            if current is not None:
                return
            if harness == "codex":
                updated = _render_codex(text, data, entry)
            else:
                key = "mcp" if harness == "opencode" else "mcpServers"
                updated = harnesses._render_json(text, key, entry, server=SERVER)
            harnesses._write(path, updated, text)
    except (RampCLIError, OSError):
        pass


def _render_codex(text: str, data: dict, entry: dict) -> str:
    # Router setup moves its provider table to the end of config.toml, so
    # place this table before it and repeat runs leave the file unchanged.
    rendered = harnesses._render_codex(text, data, entry, server=SERVER)
    head, marker, tail = text.partition("\n[model_providers.ramp-router]")
    if not marker:
        return rendered
    section = rendered[len(text.rstrip()) :].strip()
    candidate = f"{head.rstrip()}\n\n{section}\n{marker}{tail}"
    return (
        candidate if tomllib.loads(candidate) == tomllib.loads(rendered) else rendered
    )
