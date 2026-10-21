from __future__ import annotations

import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore, Lock

import click
import httpx

from ramp_cli.client.router_user import RouterUserClient
from ramp_cli.errors import RampCLIError
from ramp_cli.router_connectors.auth import issue, load_binding

MAX_MESSAGE_BYTES = 8 * 1024 * 1024


def _nonfinite(_value: str):
    raise ValueError("JSON numbers must be finite")


def _decode(value):
    return json.loads(value, parse_constant=_nonfinite)


def _message(value: object) -> bool:
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        return False
    if "id" in value and (
        isinstance(value["id"], bool)
        or not isinstance(value["id"], (str, int, float, type(None)))
        or (isinstance(value["id"], float) and not math.isfinite(value["id"]))
    ):
        return False
    if "method" in value:
        return (
            "result" not in value
            and "error" not in value
            and isinstance(value["method"], str)
            and ("params" not in value or isinstance(value["params"], (dict, list)))
        )
    return "id" in value and (("result" in value) != ("error" in value))


def _events(response: httpx.Response):
    data: list[str] = []
    size = 0
    for line in response.iter_lines():
        if not line:
            if data:
                yield _decode("\n".join(data))
            data, size = [], 0
        elif line.startswith("data:"):
            part = line[5:].removeprefix(" ")
            size += len(part.encode())
            if size > MAX_MESSAGE_BYTES:
                raise RampCLIError("Router MCP message exceeds the supported size.")
            data.append(part)
    # SSE dispatch requires a terminating blank line. An interrupted final event
    # must not become a complete protocol message.


class Bridge:
    def __init__(self, binding: str):
        self.binding = binding
        self.target = load_binding(binding)
        self.account = RouterUserClient(
            self.target["origin"],
            profile=self.target.get("profile"),
            no_input=True,
            passive_edge=True,
        )
        self.http = httpx.Client(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(300, connect=30),
        )
        self.output_lock = Lock()
        self.session_lock = Lock()
        self.session: str | None = None
        self.version: str | None = None

    def close(self):
        self.http.close()
        self.account.close()

    def emit(self, message: dict):
        with self.output_lock:
            sys.stdout.write(
                json.dumps(
                    message, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                )
                + "\n"
            )
            sys.stdout.flush()

    def send(self, message: dict):
        responded = [False]
        try:
            self._send(message, responded)
        except Exception:
            # Worker futures are not awaited; unexpected failures must also
            # complete the request instead of leaving the harness waiting.
            # Neither transport exceptions nor auth responses are safe to print:
            # httpx may include URLs, headers or upstream payloads in exceptions.
            if "id" in message and "method" in message and not responded[0]:
                self.emit(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {
                            "code": -32000,
                            "message": "Router connectors unavailable. Check 'ramp router connectors status'; sign in with 'ramp router login --connectors' and explicitly re-enable if authorization changed.",
                        },
                    }
                )
            click.echo(
                "Router connectors request failed; check connector status or explicit authorization.",
                err=True,
            )

    def _send(self, message: dict, responded: list[bool]):
        expected = self.target["authorization"]
        token = issue(self.account, expected, self.binding)
        for attempt in range(2):
            with self.session_lock:
                headers = {
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                }
                if self.session:
                    headers["Mcp-Session-Id"] = self.session
                if self.version:
                    headers["MCP-Protocol-Version"] = self.version
            with self.http.stream(
                "POST", expected["resource"], json=message, headers=headers
            ) as response:
                if response.status_code == 401 and attempt == 0:
                    token = issue(self.account, expected, self.binding, rejected=token)
                    continue
                if response.status_code == 202:
                    if "method" in message and "id" in message:
                        raise RampCLIError("MCP request returned no response.")
                    return
                if response.status_code != 200:
                    raise RampCLIError("Router MCP transport failed.")
                session = response.headers.get("mcp-session-id")
                if session:
                    if not session.isascii() or any(
                        ord(c) < 0x21 or ord(c) > 0x7E for c in session
                    ):
                        raise RampCLIError("Invalid MCP session.")
                    with self.session_lock:
                        if self.session and self.session != session:
                            raise RampCLIError("MCP session changed unexpectedly.")
                        if message.get("method") == "initialize":
                            self.session = session
                content_type = (
                    response.headers.get("content-type", "").split(";", 1)[0].strip()
                )
                if content_type == "text/event-stream":
                    for event in _events(response):
                        self.receive(event, message, responded)
                elif content_type == "application/json":
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_MESSAGE_BYTES:
                            raise RampCLIError(
                                "Router MCP response exceeds the supported size."
                            )
                    self.receive(_decode(body), message, responded)
                else:
                    raise RampCLIError("Unsupported MCP transport response.")
                if "id" in message and "method" in message and not responded[0]:
                    raise RampCLIError("MCP response stream ended before its result.")
                return

    def receive(self, message: object, request: dict, responded: list[bool]):
        if not _message(message):
            raise RampCLIError("Invalid upstream MCP message.")
        if (
            request.get("method") == "initialize"
            and message.get("id") == request.get("id")
            and "result" in message
        ):
            version = message["result"].get("protocolVersion")
            if (
                not isinstance(version, str)
                or not version.isascii()
                or any(c.isspace() for c in version)
            ):
                raise RampCLIError("Invalid MCP protocol version.")
            with self.session_lock:
                self.version = version
        self.emit(message)
        if "method" not in message and message.get("id") == request.get("id"):
            responded[0] = True


def run(binding: str) -> None:
    bridge = Bridge(binding)
    slots = BoundedSemaphore(16)

    def send(message):
        try:
            bridge.send(message)
        finally:
            slots.release()

    try:
        # Initialize is synchronous so the session/protocol version is pinned
        # before the peer can send initialized or other messages. Later requests
        # run concurrently, allowing cancellation and server-request responses.
        with ThreadPoolExecutor(max_workers=16) as executor:
            while True:
                line = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
                    raise RampCLIError("Invalid or oversized MCP stdio message.")
                try:
                    message = _decode(line)
                except (ValueError, UnicodeError):
                    bridge.emit(
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32700, "message": "Parse error"},
                        }
                    )
                    continue
                if not _message(message):
                    bridge.emit(
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32600, "message": "Invalid Request"},
                        }
                    )
                    continue
                if (
                    "id" not in message
                    or "method" not in message
                    or message.get("method") == "initialize"
                ):
                    # Control messages must pass even when all tool workers are
                    # occupied, or cancellation can deadlock behind their work.
                    bridge.send(message)
                else:
                    if slots.acquire(blocking=False):
                        executor.submit(send, message)
                    else:
                        bridge.emit(
                            {
                                "jsonrpc": "2.0",
                                "id": message["id"],
                                "error": {
                                    "code": -32000,
                                    "message": "Too many concurrent Router connector requests; retry after a request completes.",
                                },
                            }
                        )
    finally:
        bridge.close()
