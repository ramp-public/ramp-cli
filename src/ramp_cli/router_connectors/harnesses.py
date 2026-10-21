from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

import json5

from ramp_cli.auth.refresh import _refresh_lock
from ramp_cli.errors import RampCLIError

HARNESSES = ("claude-code", "codex", "opencode")
SERVER = "ramp-connectors"
EXECUTABLE_NAMES = (
    "ramp",
    "ramp.exe",
    "ramp-darwin-amd64",
    "ramp-darwin-arm64",
    "ramp-linux-amd64",
    "ramp-linux-arm64",
    "ramp-windows-amd64.exe",
)


def config_path(harness: str) -> Path:
    if harness == "claude-code":
        configured = os.environ.get("CLAUDE_CONFIG_DIR")
        return (
            Path(configured) / ".claude.json"
            if configured
            else Path.home() / ".claude.json"
        )
    if harness == "codex":
        return (
            Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
            / "config.toml"
        )
    configured = os.environ.get("OPENCODE_CONFIG")
    if configured:
        return Path(configured).expanduser()
    root = (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "opencode"
    )
    paths = [root / "opencode.json", root / "opencode.jsonc"]
    existing = [p for p in paths if p.exists()]
    if len(existing) > 1:
        raise RampCLIError(
            "Multiple OpenCode configurations found; resolve them before connector setup."
        )
    return existing[0] if existing else paths[0]


def _executable() -> str:
    executable = shutil.which("ramp")
    if executable:
        return str(Path(executable).absolute())
    path = Path(sys.argv[0]).absolute()
    if path.name in EXECUTABLE_NAMES and path.is_file() and os.access(path, os.X_OK):
        return str(path)
    raise RampCLIError("Install the ramp executable before registering connectors.")


def _entry(harness: str, binding: str) -> dict:
    command = _executable()
    args = ["router", "connectors", "bridge", "--binding", binding]
    if harness == "opencode":
        return {"type": "local", "command": [command, *args], "enabled": True}
    result = {"command": command, "args": args}
    if harness == "claude-code":
        result["type"] = "stdio"
    return result


def _owned(harness: str, entry: object) -> str | None:
    if not isinstance(entry, dict):
        return None
    if harness == "opencode" and (
        entry.get("type") != "local" or type(entry.get("enabled")) is not bool
    ):
        return None
    if harness == "claude-code" and entry.get("type") != "stdio":
        return None
    command, args = entry.get("command"), entry.get("args")
    if harness == "opencode":
        if not isinstance(command, list) or not command:
            return None
        command, args = command[0], command[1:]
    if (
        not isinstance(command, str)
        or not Path(command).is_absolute()
        or Path(command).name not in EXECUTABLE_NAMES
        or not isinstance(args, list)
        or len(args) != 5
        or args[:4] != ["router", "connectors", "bridge", "--binding"]
        or not isinstance(args[4], str)
        or not re.fullmatch(r"[a-f0-9]{32}", args[4])
    ):
        return None
    # Extra environment/headers could inject credentials or change the destination.
    allowed = (
        {"type", "command", "enabled"}
        if harness == "opencode"
        else {"command", "args", "type"}
        if harness == "claude-code"
        else {"command", "args"}
    )
    if set(entry) - allowed:
        return None
    return args[4]


def _read(path: Path, harness: str) -> tuple[str, dict, object]:
    try:
        text = path.read_text() if path.exists() else ""
        data = (
            tomllib.loads(text)
            if harness == "codex"
            else json5.loads(text or "{}", allow_duplicate_keys=False)
        )
        if not isinstance(data, dict):
            raise ValueError
        key = (
            "mcp"
            if harness == "opencode"
            else "mcp_servers"
            if harness == "codex"
            else "mcpServers"
        )
        servers = data.get(key, {})
        if not isinstance(servers, dict):
            raise ValueError
        return text, data, servers.get(SERVER)
    except (ValueError, OSError) as exc:
        raise RampCLIError(
            "Could not safely read harness configuration; nothing was changed."
        ) from exc


def status(harness: str) -> dict:
    _, _, entry = _read(config_path(harness), harness)
    binding = _owned(harness, entry)
    return {
        "harness": harness,
        "state": "disabled"
        if entry is None or (binding and entry.get("enabled") is False)
        else "enabled"
        if binding
        else "conflict",
        "binding_id": binding,
    }


def _skip_json_trivia(text: str, position: int) -> int:
    while position < len(text):
        if text[position].isspace():
            position += 1
        elif text.startswith("//", position):
            end = text.find("\n", position)
            position = len(text) if end < 0 else end + 1
        elif text.startswith("/*", position):
            end = text.find("*/", position + 2)
            if end < 0:
                raise ValueError("Unclosed comment")
            position = end + 2
        else:
            break
    return position


def _json_members(text: str) -> tuple[list[tuple[str, int, int, int]], int]:
    position = _skip_json_trivia(text, 0)
    if text[position : position + 1] != "{":
        raise ValueError("Expected object")
    position += 1
    members = []
    while True:
        position = _skip_json_trivia(text, position)
        if text[position : position + 1] == "}":
            return members, position
        start = position
        if text[position : position + 1] in ("'", '"'):
            key, error, position = json5.parse(
                text, start=position, consume_trailing=False
            )
            if error or not isinstance(key, str):
                raise ValueError("Invalid key")
        else:
            match = re.match(r"[A-Za-z_$][A-Za-z0-9_$-]*", text[position:])
            if match is None:
                raise ValueError("Unsupported JSON5 key syntax")
            key = match.group()
            position += len(key)
        position = _skip_json_trivia(text, position)
        if text[position : position + 1] != ":":
            raise ValueError("Invalid member")
        value_start = _skip_json_trivia(text, position + 1)
        _, error, value_end = json5.parse(
            text, start=value_start, consume_trailing=False
        )
        if error:
            raise ValueError("Invalid value")
        members.append((key, start, value_start, value_end))
        position = _skip_json_trivia(text, value_end)
        if text[position : position + 1] == ",":
            position += 1
        elif text[position : position + 1] != "}":
            raise ValueError("Invalid separator")


def _json_field(text: str, key: str, replacement: str | None) -> str:
    members, end = _json_members(text)
    for index, (name, start, value_start, value_end) in enumerate(members):
        if name != key:
            continue
        if replacement is not None:
            return text[:value_start] + replacement + text[value_end:]
        following = _skip_json_trivia(text, value_end)
        if text[following : following + 1] == ",":
            return text[:start] + text[following + 1 :]
        if index:
            comma = _skip_json_trivia(text, members[index - 1][3])
            if text[comma : comma + 1] != ",":
                raise ValueError("Missing separator")
            return text[:comma] + text[comma + 1 : start] + text[value_end:]
        return text[:start] + text[value_end:]
    if replacement is None:
        return text
    comma = ""
    last_end = _skip_json_trivia(text, members[-1][3]) if members else end
    trailing_comma = bool(members) and text[last_end : last_end + 1] == ","
    if members and not trailing_comma:
        comma = ","
    return (
        text[:end]
        + comma
        + "\n"
        + json.dumps(key)
        + ": "
        + replacement
        + ("," if trailing_comma else "")
        + "\n"
        + text[end:]
    )


def _render_json(text: str, key: str, entry: dict | None) -> str:
    original = text or "{}\n"
    try:
        members, _ = _json_members(original)
        server_map = next(
            (original[start:end] for name, _, start, end in members if name == key),
            "{}",
        )
        updated_map = _json_field(
            server_map, SERVER, json.dumps(entry) if entry is not None else None
        )
        candidate = _json_field(original, key, updated_map)
        expected = json5.loads(original, allow_duplicate_keys=False)
        if entry is None:
            expected.setdefault(key, {}).pop(SERVER, None)
        else:
            expected.setdefault(key, {})[SERVER] = entry
        if json5.loads(candidate, allow_duplicate_keys=False) != expected:
            raise ValueError("Configuration changed unexpectedly")
        return candidate
    except (ValueError, IndexError) as exc:
        raise RampCLIError(
            "Cannot safely edit this JSON configuration; nothing was changed."
        ) from exc


def _render_codex(text: str, data: dict, entry: dict | None) -> str:
    # Append/remove one standalone table, retaining all unrelated text/comments.
    # Reject unusual nested/inline representations rather than risk a lossy edit.
    header = re.compile(
        r'^\s*\[mcp_servers\.(?:ramp-connectors|"ramp-connectors"|\'ramp-connectors\')\]\s*(?:#.*)?$'
    )
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if header.match(line.rstrip("\r\n"))]
    if len(starts) > 1:
        raise RampCLIError("Ambiguous Codex connector table; nothing was changed.")
    if starts:
        start = starts[0]
        end = next(
            (
                i
                for i in range(start + 1, len(lines))
                if lines[i].lstrip().startswith("[")
            ),
            len(lines),
        )
        candidate = "".join(lines[:start] + lines[end:])
    else:
        candidate = text
    if entry is not None:
        candidate = (
            candidate.rstrip()
            + "\n\n[mcp_servers.ramp-connectors]\ncommand = "
            + json.dumps(entry["command"])
            + "\nargs = "
            + json.dumps(entry["args"])
            + "\n"
        )
    expected = json.loads(json.dumps(data))
    servers = expected.setdefault("mcp_servers", {})
    if entry is None:
        servers.pop(SERVER, None)
    else:
        servers[SERVER] = entry
    try:
        parsed = tomllib.loads(candidate)
    except tomllib.TOMLDecodeError as exc:
        raise RampCLIError(
            "Cannot safely edit this Codex configuration; nothing was changed."
        ) from exc
    if not parsed.get("mcp_servers") and not expected.get("mcp_servers"):
        parsed.pop("mcp_servers", None)
        expected.pop("mcp_servers", None)
    if parsed != expected:
        raise RampCLIError(
            "Cannot safely edit this Codex configuration; nothing was changed."
        )
    return candidate


def _write(path: Path, text: str, original: str) -> None:
    if path.is_symlink():
        path = path.resolve(strict=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        if path.exists():
            os.fchmod(fd, path.stat().st_mode & 0o777)
        with os.fdopen(fd, "w") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        if (path.read_text() if path.exists() else "") != original:
            raise RampCLIError("Harness configuration changed during setup; retry.")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _change(harness: str, binding: str | None) -> dict:
    path = config_path(harness)
    if harness == "codex":
        # Router imports these commands before defining the shared config lock.
        from ramp_cli.commands.router import codex_config_lock  # noqa: PLC0415

        lock = codex_config_lock(path)
    else:
        lock = _refresh_lock(
            "connectors-config-"
            + hashlib.sha256(str(path.resolve()).encode()).hexdigest()
        )
    with lock:
        text, data, current = _read(path, harness)
        if current is not None and not _owned(harness, current):
            raise RampCLIError(
                "Existing ramp-connectors registration is not CLI-owned; nothing was changed."
            )
        entry = _entry(harness, binding) if binding else None
        if harness == "codex":
            updated = _render_codex(text, data, entry)
        else:
            key = "mcp" if harness == "opencode" else "mcpServers"
            updated = _render_json(text, key, entry)
        if current is not None or binding:
            _write(path, updated, text)
    return {
        "contract_version": 1,
        "harness": harness,
        "state": "enabled" if binding else "disabled",
        "binding_id": binding,
    }


def enable(harness: str, binding: str) -> dict:
    return _change(harness, binding)


def disable(harness: str) -> dict:
    return _change(harness, None)
