"""Trust the Codex hooks Ramp Router writes, through Codex's own app-server.

Codex skips a user hook until it is reviewed, and records the review as
``hooks.state."<key>".trusted_hash``. Configuring Router for Codex is the
user's consent to Router's hooks, so configure records that trust the way
Codex's own "Trust" action does: ``hooks/list`` reports each hook's key and
current hash, and ``config/batchWrite`` saves that hash. Codex computes the
hash, so nothing here re-implements it.

Only handlers whose event and command are exactly the ones Router wrote are
trusted; every other hook keeps waiting for the user's review.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

# A server that has exited ends its output within this; one whose child keeps
# stdout open is left to the daemon reader rather than waited on.
_READER_EXIT_SECONDS = 2
_TIMEOUT_SECONDS = 10


class TrustUnavailable(Exception):
    """Codex could not be asked; the hooks stay as they were."""


def trust_router_hooks(
    executable: str | os.PathLike[str] | None,
    config_path: Path,
    commands: set[tuple[str, str]],
    retained: set[tuple[str, str]] = frozenset(),
    recorded: dict[str, str] | None = None,
) -> dict[str, str]:
    """Trust Router's hooks in ``config_path``; return ``{key: trusted_hash}``.

    ``commands`` holds ``(event, command)`` pairs, the event in Codex's
    camelCase (``sessionStart``, ``stop``). Hooks that are already trusted
    are returned too, so unconfigure knows every entry Router accounts for.
    A ``retained`` hook is never trusted here; it is returned only while it
    still carries the trust ``recorded`` for it, unchanged.
    Raises TrustUnavailable when Codex is missing, too old for these calls,
    or answers unexpectedly.
    """
    if executable is None or not (commands or retained):
        raise TrustUnavailable("no Codex executable")
    with _AppServer(executable, config_path.parent) as server:
        listed = server.request("hooks/list", {"cwds": [str(config_path.parent)]})
        try:
            hooks = [hook for entry in listed["data"] for hook in entry["hooks"]]
        except (KeyError, TypeError):
            raise TrustUnavailable("unexpected hooks/list response") from None
        ours = {
            hook["key"]: hook["currentHash"]
            for hook in hooks
            if _is_router_hook(hook, config_path, commands)
        }
        untrusted = {
            hook["key"]: {"trusted_hash": hook["currentHash"]}
            for hook in hooks
            if hook.get("key") in ours and hook.get("trustStatus") != "trusted"
        }
        kept = {
            hook["key"]: hook["currentHash"]
            for hook in hooks
            if _is_router_hook(hook, config_path, retained)
            and hook.get("trustStatus") == "trusted"
            and (recorded or {}).get(hook["key"]) == hook["currentHash"]
        }
        if untrusted:
            # Upsert merges into each entry, so a user's `enabled = false`
            # and every other hook's state are kept.
            server.request(
                "config/batchWrite",
                {
                    "filePath": str(config_path),
                    "edits": [
                        {
                            "keyPath": "hooks.state",
                            "mergeStrategy": "upsert",
                            "value": untrusted,
                        }
                    ],
                },
            )
    return {**kept, **ours}


def _is_router_hook(hook: object, config_path: Path, commands: set) -> bool:
    if not isinstance(hook, dict):
        return False
    key, current_hash = hook.get("key"), hook.get("currentHash")
    if not (isinstance(key, str) and isinstance(current_hash, str) and current_hash):
        return False
    source_path = hook.get("sourcePath")
    return (
        hook.get("source") == "user"
        and not hook.get("isManaged")
        and hook.get("handlerType") == "command"
        and isinstance(source_path, str)
        and Path(source_path).resolve() == config_path.resolve()
        and (hook.get("eventName"), hook.get("command")) in commands
    )


class _AppServer:
    """A short-lived ``codex app-server`` spoken to over stdio JSON-RPC."""

    def __init__(self, executable: str | os.PathLike[str], codex_home: Path):
        self._args = [os.fspath(executable), "app-server"]
        self._env = {**os.environ, "CODEX_HOME": str(codex_home)}
        self._next_id = 0
        self._deadline = 0.0

    def __enter__(self) -> _AppServer:
        self._deadline = time.monotonic() + _TIMEOUT_SECONDS
        try:
            self._process = subprocess.Popen(
                self._args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self._env,
            )
        except OSError as exc:
            raise TrustUnavailable(str(exc)) from None
        # A thread rather than a selector: Windows can't select on a pipe.
        self._lines: queue.Queue[bytes] = queue.Queue()
        self._reader = threading.Thread(target=self._read_lines, daemon=True)
        self._reader.start()
        try:
            self.request(
                "initialize", {"clientInfo": {"name": "ramp-cli", "version": "1"}}
            )
            self._send({"method": "initialized"})
        except BaseException:
            # __exit__ never runs when __enter__ raises, so reap it here.
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_exc: object) -> None:
        self._process.kill()
        self._process.wait()
        # Close stdin before waiting on the reader. npm's Codex launcher runs
        # the real server as a child sharing these pipes, so killing the
        # launcher leaves that child holding stdout open until stdin ends.
        self._close(self._process.stdin)
        # Ends at the end of output, which the kill or stdin's end brings;
        # joined before stdout closes under it.
        self._reader.join(_READER_EXIT_SECONDS)
        if not self._reader.is_alive():
            # A reader still blocked on a child that keeps stdout open holds
            # the pipe's lock, so closing would wait on it; the daemon thread
            # and the pipe go when the child does.
            self._close(self._process.stdout)

    @staticmethod
    def _close(pipe) -> None:
        # A request the server never read can leave stdin's buffer full;
        # closing retries that flush and would raise BrokenPipeError over
        # the TrustUnavailable already being handled. The pipe still closes.
        try:
            pipe.close()
        except OSError:
            pass

    def request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        request_id = self._next_id
        self._send({"id": request_id, "method": method, "params": params})
        while True:
            message = self._read_message()
            if message.get("id") != request_id:
                continue
            if "error" in message or not isinstance(message.get("result"), dict):
                # Older Codex builds answer -32601 for hooks/list.
                raise TrustUnavailable(f"{method} failed")
            return message["result"]

    def _send(self, message: dict) -> None:
        try:
            self._process.stdin.write(json.dumps(message).encode() + b"\n")
            self._process.stdin.flush()
        except OSError:
            raise TrustUnavailable("Codex app-server exited") from None

    def _read_lines(self) -> None:
        # b"" marks the end of output: the server exited or was killed.
        for line in iter(self._process.stdout.readline, b""):
            self._lines.put(line)
        self._lines.put(b"")

    def _read_message(self) -> dict:
        remaining = self._deadline - time.monotonic()
        try:
            line = self._lines.get(timeout=max(remaining, 0))
        except queue.Empty:
            raise TrustUnavailable("Codex app-server timed out") from None
        if not line:
            self._lines.put(b"")  # keep reporting the exit
            raise TrustUnavailable("Codex app-server exited")
        try:
            message = json.loads(line)
        except ValueError:
            raise TrustUnavailable("unreadable app-server output") from None
        return message if isinstance(message, dict) else {}
