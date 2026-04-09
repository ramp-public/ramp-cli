"""OAuth 2.0 authorization code flow with PKCE."""

from __future__ import annotations

import base64
import hashlib
import html as html_mod
import secrets
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import click
import httpx

from ramp_cli.auth.constants import INVALID_GRANT
from ramp_cli.config.constants import (
    DEVAPI_SCOPES,
    PREFERRED_CALLBACK_PORT,
    auth_url,
    client_id,
    token_url,
)
from ramp_cli.config.settings import configured_scopes
from ramp_cli.tools.parser import extract_all_scopes
from ramp_cli.tools.registry import _resolve_spec_path


@dataclass
class TokenResponse:
    access_token: str
    refresh_token: str = ""
    token_type: str = ""
    expires_in: int = 0
    refresh_token_expires_in: int = 0
    scope: str = ""


@dataclass
class LoginOptions:
    no_browser: bool = False


class OAuthTokenError(Exception):
    def __init__(self, error: str, description: str = "") -> None:
        self.error = error
        self.description = description
        message = error
        if description:
            message += f" — {description}"
        super().__init__(message)


def _callback_html(*, success: bool, title: str, message: str, detail: str = "") -> str:
    """Build a styled HTML page for the OAuth callback screen.

    The design mirrors the terminal-window aesthetic of agents.ramp.com/cards:
    neutral background, white card, macOS-style title bar with traffic-light
    dots, monospace content area, and the CLI's signature filled-diamond
    status indicator.
    """
    accent = "#10b981" if success else "#ef4444"
    symbol = "\u25c6" if success else "\u2715"  # ◆ or ✕
    safe_title = html_mod.escape(title)
    safe_message = html_mod.escape(message)

    detail_block = ""
    if detail:
        safe_detail = html_mod.escape(detail)
        detail_block = f'<div class="d"><span>{safe_detail}</span></div>'

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ramp-cli</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
    Helvetica,Arial,sans-serif;
  background:#fafafa;min-height:100vh;
  display:flex;align-items:center;justify-content:center;padding:20px
}}
.w{{
  width:520px;max-width:calc(100vw - 40px);background:#fff;
  border-radius:12px;border:1px solid #e5e5e5;overflow:hidden;
  box-shadow:0 1px 3px rgba(0,0,0,.04),0 6px 16px rgba(0,0,0,.04)
}}
.tb{{
  height:32px;background:#fafafa;border-bottom:1px solid #e5e5e5;
  display:flex;align-items:center;padding:0 12px;position:relative
}}
.dots{{display:flex;gap:6px}}
.dot{{
  width:12px;height:12px;border-radius:50%;
  border:1.5px solid #d4d4d4
}}
.tt{{
  position:absolute;left:0;right:0;text-align:center;
  font-size:12px;color:#262626;pointer-events:none;
  font-weight:500
}}
.c{{
  padding:40px 32px 32px;
  font-family:"SF Mono","Fira Code","Cascadia Code",
    "JetBrains Mono",Menlo,Monaco,"Courier New",monospace;
  font-size:14px;line-height:1.6;color:#262626
}}
.si{{font-size:22px;color:{accent};margin-bottom:16px}}
.h{{font-size:15px;font-weight:600;margin-bottom:8px;color:#262626}}
.m{{color:#737373;font-size:13px}}
.d{{
  margin-top:16px;padding:12px 16px;background:#fafafa;
  border:1px solid #e5e5e5;border-radius:8px;
  font-size:12px;color:#525252;word-break:break-word
}}
.p{{margin-top:28px;color:#a3a3a3;font-size:13px}}
</style>
</head>
<body>
<div class="w">
  <div class="tb">
    <div class="dots">
      <div class="dot"></div><div class="dot"></div><div class="dot"></div>
    </div>
    <span class="tt">ramp-cli</span>
  </div>
  <div class="c">
    <div class="si">{symbol}</div>
    <div class="h">{safe_title}</div>
    <div class="m">{safe_message}</div>
    {detail_block}
    <div class="p">$ \u2588</div>
  </div>
</div>
</body>
</html>"""


def login(env: str, opts: LoginOptions | None = None) -> TokenResponse:
    """Perform the full OAuth authorization code + PKCE flow."""

    opts = opts or LoginOptions()

    verifier = _generate_verifier()
    challenge = _generate_challenge(verifier)
    state = _generate_state()

    listener = _listen_for_callback()
    port = listener.getsockname()[1]
    redirect_uri = f"http://localhost:{port}/callback"

    scopes = _resolve_scopes(env)
    auth = _build_auth_url(env, redirect_uri, state, challenge, scopes)

    # Channel for the authorization code
    result: dict[str, Any] = {}
    event = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self.send_error(404)
                return

            qs = parse_qs(parsed.query)

            if qs.get("state", [None])[0] != state:
                result["error"] = "State mismatch"
                page = _callback_html(
                    success=False,
                    title="Authentication failed",
                    message="You can close this window.",
                    detail="State parameter mismatch — the request may have"
                    " been tampered with. Please try again.",
                )
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(page.encode())
                event.set()
                return

            if "error" in qs:
                desc = qs.get("error_description", [""])[0]
                result["error"] = f"OAuth error: {qs['error'][0]} — {desc}"
                page = _callback_html(
                    success=False,
                    title="Authentication failed",
                    message="You can close this window.",
                    detail=desc,
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(page.encode())
                event.set()
                return

            code = qs.get("code", [None])[0]
            if not code:
                result["error"] = "No authorization code in callback"
                page = _callback_html(
                    success=False,
                    title="Authentication failed",
                    message="You can close this window.",
                    detail="No authorization code was received.",
                )
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(page.encode())
                event.set()
                return

            result["code"] = code
            page = _callback_html(
                success=True,
                title="Authenticated",
                message="You can close this window and return to your terminal.",
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page.encode())
            event.set()

        def log_message(self, format: str, *args: Any) -> None:
            pass  # Suppress HTTP server logs

    server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
    # Rebind to our pre-opened socket
    server.socket.close()
    server.socket = listener
    server.server_address = listener.getsockname()

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        if opts.no_browser:
            click.echo(
                f"Open this URL in your browser to authenticate:\n\n  {auth}\n",
                err=True,
            )
        else:
            if not _open_browser(auth):
                click.echo(
                    f"Could not open browser. Open this URL manually:\n\n  {auth}\n",
                    err=True,
                )

        click.echo("Waiting for authentication in browser...", err=True)
        if not event.wait(timeout=300):
            raise RuntimeError("Authentication timed out after 5 minutes")
    finally:
        server.shutdown()

    if "error" in result:
        raise RuntimeError(result["error"])

    return _exchange_code(env, result["code"], redirect_uri, verifier)


def refresh_tokens(env: str, refresh_token: str) -> TokenResponse:
    """Exchange a refresh token for a new access token."""

    url = token_url(env)
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    resp = _do_token_request(env, url, data)
    body = _parse_token_response(resp)
    _raise_for_token_error(resp, body, grant_type="refresh_token")
    return TokenResponse(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token", ""),
        token_type=body.get("token_type", ""),
        expires_in=body.get("expires_in", 0),
        refresh_token_expires_in=body.get("refresh_token_expires_in", 0),
        scope=body.get("scope", ""),
    )


# --- Internal helpers ---


def _listen_for_callback() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", PREFERRED_CALLBACK_PORT))
    except OSError:
        sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock


def _build_auth_url(
    env: str, redirect_uri: str, state: str, challenge: str, scopes: str
) -> str:
    params = {
        "auth_level": "auto",
        "response_type": "code",
        "client_id": client_id(env),
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": scopes,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return auth_url(env) + "?" + urlencode(params)


def _exchange_code(
    env: str, code: str, redirect_uri: str, verifier: str
) -> TokenResponse:
    url = token_url(env)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    resp = _do_token_request(env, url, data)
    body = _parse_token_response(resp)
    _raise_for_token_error(resp, body, grant_type="authorization_code")
    return TokenResponse(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token", ""),
        token_type=body.get("token_type", ""),
        expires_in=body.get("expires_in", 0),
        refresh_token_expires_in=body.get("refresh_token_expires_in", 0),
        scope=body.get("scope", ""),
    )


def _do_token_request(env: str, url: str, data: dict[str, str]) -> Any:
    # Public client: client_id in form body, PKCE for security
    data["client_id"] = client_id(env)
    return httpx.post(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


def _parse_token_response(resp: Any) -> dict[str, Any]:
    try:
        body = resp.json()
    except Exception:
        raise OAuthTokenError(
            "token_request_failed",
            getattr(resp, "text", "").strip() or f"HTTP {resp.status_code}",
        )
    if not isinstance(body, dict):
        raise OAuthTokenError("token_request_failed", str(body))
    return body


def _raise_for_token_error(resp: Any, body: dict[str, Any], grant_type: str) -> None:
    if not resp.is_error and "error" not in body and "access_token" in body:
        return

    description = _token_error_description(body, resp)
    error = body.get("error")
    if not isinstance(error, str) or not error:
        error = _classify_token_error(resp.status_code, grant_type, description)
    raise OAuthTokenError(str(error), description)


def _token_error_description(body: dict[str, Any], resp: Any) -> str:
    description = body.get("error_description") or body.get("message")
    nested_error = body.get("error")
    if not description and isinstance(nested_error, dict):
        description = nested_error.get("message")
    nested_error_v2 = body.get("error_v2")
    if not description and isinstance(nested_error_v2, dict):
        description = nested_error_v2.get("message")
    if description:
        return str(description)
    text = getattr(resp, "text", "").strip()
    if text:
        return text
    return str(body)


def _classify_token_error(status_code: int, grant_type: str, description: str) -> str:
    lower = description.lower()
    if grant_type == "refresh_token":
        refresh_invalid_markers = (
            "refresh token with given refresh_token not found",
            "invalid refresh token",
            "expired refresh token",
            "refresh token expired",
            "refresh token revoked",
            "invalid_grant",
        )
        if any(marker in lower for marker in refresh_invalid_markers):
            return INVALID_GRANT
        if status_code in (400, 401) and "refresh token" in lower:
            return INVALID_GRANT
    return "token_request_failed"


def _resolve_scopes(env: str) -> str:
    """Build the OAuth scope string for login.

    Uses custom scopes if configured, otherwise merges DevAPI scopes
    with scopes extracted from the agent-tool OpenAPI spec. Prefers
    the env-specific cached spec (written by ``tools refresh``) so
    that newly introduced scopes are requested after a sync.
    """
    custom = configured_scopes()
    if custom:
        return custom

    all_scopes = set(DEVAPI_SCOPES)
    try:
        all_scopes.update(extract_all_scopes(_resolve_spec_path(env)))
    except Exception:
        click.echo(
            "\n  ⚠  Could not read tool definitions — some scopes may be missing."
            "\n     Run 'ramp tools refresh' then 'ramp auth login' to fix.\n",
            err=True,
        )
    return " ".join(sorted(all_scopes))


def _generate_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def _generate_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _generate_state() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode()


def _open_browser(url: str) -> bool:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(
                ["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        elif sys.platform.startswith("linux"):
            subprocess.Popen(
                ["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        elif sys.platform == "win32":
            subprocess.Popen(
                ["rundll32", "url.dll,FileProtocolHandler", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            return False
        return True
    except Exception:
        return False
