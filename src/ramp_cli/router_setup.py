"""Browser-assisted acquisition of a Ramp Router API key."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import click

from ramp_cli.auth.oauth import _callback_html, _open_browser

ROUTER_SETUP_TIMEOUT_SECONDS = 900
ROUTER_SETUP_PATH = "/cli/setup"
ROUTER_CALLBACK_PATH = "/callback"
MAX_CALLBACK_BODY_BYTES = 16 * 1024
CALLBACK_READ_TIMEOUT_SECONDS = 5
# The terminal card is 520px wide with 20px page margins. Keep enough height
# for browser chrome and the card without leaving a large empty popup.
CALLBACK_POPUP_WIDTH = 560
CALLBACK_POPUP_HEIGHT = 360
# Long enough for the confirmation to register and for the acceptance message
# to reach Router, short enough that the window does not feel abandoned.
CALLBACK_CLOSE_DELAY_MS = 1200


@dataclass
class RouterKeyCallback:
    """A short-lived loopback receiver for a one-time Router key."""

    redirect_uri: str
    state: str
    code_challenge: str
    _server: HTTPServer
    _thread: threading.Thread
    _event: threading.Event
    _result: dict[str, Any]

    def wait_for_key(self, timeout: int = ROUTER_SETUP_TIMEOUT_SECONDS) -> str:
        if not self._event.wait(timeout=timeout):
            raise click.ClickException("Router setup timed out after 15 minutes")
        if "error" in self._result:
            raise click.ClickException(str(self._result["error"]))
        return str(self._result["api_key"])

    def shutdown(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=1)
        self._server.server_close()


def start_router_key_callback(router_ui_url: str) -> RouterKeyCallback:
    """Listen on loopback for the authenticated Router page's form POST."""

    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(32)
    challenge_digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(challenge_digest).rstrip(b"=").decode()
    router_url = urlparse(router_ui_url)
    router_origin = f"{router_url.scheme}://{router_url.netloc}"
    result: dict[str, Any] = {}
    result_lock = threading.Lock()
    event = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if urlparse(self.path).path != ROUTER_CALLBACK_PATH:
                self.send_error(404)
                return

            content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
            if content_type.lower() != "application/x-www-form-urlencoded":
                self._finish_error(
                    415,
                    "Router setup returned an unsupported response",
                    "The callback must use a form POST. Please try again.",
                )
                return

            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                content_length = -1
            if content_length < 1 or content_length > MAX_CALLBACK_BODY_BYTES:
                self._finish_error(
                    413,
                    "Router setup returned an invalid response",
                    "The callback body was empty or too large. Please try again.",
                )
                return

            try:
                self.connection.settimeout(CALLBACK_READ_TIMEOUT_SECONDS)
                body = self.rfile.read(content_length).decode("utf-8")
                if len(body.encode("utf-8")) != content_length:
                    raise ValueError("Incomplete callback body")
                form = parse_qs(body, keep_blank_values=True, strict_parsing=True)
            except (TimeoutError, socket.timeout, UnicodeDecodeError, ValueError):
                self._finish_error(
                    400,
                    "Router setup returned an invalid response",
                    "The callback form could not be read. Please try again.",
                )
                return

            returned_state = _single_value(form, "state")
            if returned_state is None or not secrets.compare_digest(
                returned_state, state
            ):
                self._finish_error(
                    400,
                    "Router setup failed",
                    "State mismatch — the request may have been tampered with.",
                )
                return

            error = _single_value(form, "error")
            if error:
                description = _single_value(form, "error_description") or error
                self._finish_error(
                    200,
                    "Router setup was cancelled",
                    description,
                    cli_error=f"Router setup failed: {description}",
                )
                return

            api_key = _single_value(form, "api_key")
            if not api_key or not api_key.strip():
                self._finish_error(
                    400,
                    "Router setup returned no API key",
                    "No API key was received. Please try again.",
                )
                return

            with result_lock:
                if result.get("consumed"):
                    self._send_page(
                        409,
                        _callback_html(
                            success=False,
                            title="Router setup callback already used",
                            message="Return to the original Router setup window.",
                        ),
                    )
                    return
                result["consumed"] = True
                result["api_key"] = api_key.strip()
                # Wake the CLI before writing the response. A browser that closes
                # the popup during this write must not turn an accepted key into a
                # 15-minute timeout.
                event.set()
            self._send_page(
                200,
                _callback_result_html(
                    _callback_html(
                        success=True,
                        title="Ramp Router connected",
                        message="You can close this window and return to your terminal.",
                    ),
                    router_origin,
                    state,
                ),
            )

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/handshake":
                self.send_error(404)
                return
            returned_state = _single_value(parse_qs(parsed.query), "state")
            if returned_state is None or not secrets.compare_digest(
                returned_state, state
            ):
                self.send_error(400)
                return
            self._send_page(
                200,
                _handshake_html(router_origin, state, code_verifier),
            )

        def _finish_error(
            self,
            status: int,
            title: str,
            detail: str,
            *,
            cli_error: str | None = None,
        ) -> None:
            with result_lock:
                if result.get("consumed"):
                    self._send_page(409, "Callback already used")
                    return
                result["consumed"] = True
                result["error"] = cli_error or detail
                event.set()
            self._send_page(
                status,
                _callback_html(
                    success=False,
                    title=title,
                    message="You can close this window and return to your terminal.",
                    detail=detail,
                ),
            )

        def _send_page(self, status: int, page: str) -> None:
            encoded = page.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            try:
                self.end_headers()
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                pass

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return RouterKeyCallback(
        redirect_uri=f"http://127.0.0.1:{port}{ROUTER_CALLBACK_PATH}",
        state=state,
        code_challenge=code_challenge,
        _server=server,
        _thread=thread,
        _event=event,
        _result=result,
    )


def acquire_router_api_key(router_ui_url: str, *, no_browser: bool = False) -> str:
    """Open Router setup and wait for its one-time key handoff."""

    callback = start_router_key_callback(router_ui_url)
    query = urlencode(
        {
            "redirect_uri": callback.redirect_uri,
            "state": callback.state,
            "code_challenge": callback.code_challenge,
            "code_challenge_method": "S256",
            "client": "ramp-cli",
        }
    )
    setup_url = f"{router_ui_url.rstrip('/')}{ROUTER_SETUP_PATH}?{query}"
    try:
        if no_browser:
            click.echo(
                f"Open this URL in your browser to connect Ramp Router:\n\n  {setup_url}\n",
                err=True,
            )
        elif not _open_browser(setup_url):
            click.echo(
                f"Could not open browser. Open this URL manually:\n\n  {setup_url}\n",
                err=True,
            )
        click.echo("Waiting for Ramp Router setup in browser...", err=True)
        return callback.wait_for_key()
    finally:
        callback.shutdown()


def _single_value(form: dict[str, list[str]], field: str) -> str | None:
    values = form.get(field)
    if not values or len(values) != 1:
        return None
    return values[0]


def _handshake_html(router_origin: str, state: str, code_verifier: str) -> str:
    payload = json.dumps(
        {
            "type": "ramp-cli-ready",
            "state": state,
            "code_verifier": code_verifier,
        }
    )
    page = _callback_html(
        success=True,
        title="Connecting to Ramp CLI\u2026",
        message="Confirming this connection with your terminal\u2026",
    )
    script = (
        "<script>if (window.opener) {"
        f"window.resizeTo({CALLBACK_POPUP_WIDTH}, {CALLBACK_POPUP_HEIGHT});"
        f"window.opener.postMessage({payload}, {json.dumps(router_origin)});"
        "}</script>"
    )
    return page.replace("</body>", f"{script}</body>")


def _callback_result_html(page: str, router_origin: str, state: str) -> str:
    """Tell Router the key arrived, then close the window Router opened.

    Setup is finished by the time this renders, so leaving the window up only
    asks the user to clean up after us. A window opened with ``window.open``
    may be closed from script, and the guard keeps that to exactly the popup
    Router opened. Closing is deferred so the acceptance message is delivered
    first, and a browser that refuses to close simply leaves the page saying
    the window can be closed by hand.
    """
    accepted = json.dumps({"type": "ramp-cli-accepted", "state": state})
    origin = json.dumps(router_origin)
    script = (
        "<script>if (window.opener) {"
        f"window.opener.postMessage({accepted}, {origin});"
        f"setTimeout(function(){{window.close();}}, {CALLBACK_CLOSE_DELAY_MS});"
        "}</script>"
    )
    return page.replace("</body>", f"{script}</body>")
