"""httpx-based API client with automatic token refresh on 401."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from ramp_cli import __version__ as VERSION
from ramp_cli.auth import store
from ramp_cli.auth.environment import (
    environment_auth_required_message,
    extra_auth_headers,
    missing_required_environment_auth,
)
from ramp_cli.auth.refresh import try_refresh
from ramp_cli.client.headers import agent_headers
from ramp_cli.config.constants import api_url, base_url
from ramp_cli.errors import (
    ApiError,
    AuthRequiredError,
    EnvironmentAuthRequiredError,
    RefreshFailedError,
    UnsafeRequestUrlError,
)

# Client timeout should exceed the server-side timeout (60s) so we always
# receive the server's response rather than giving up prematurely.
_REQUEST_TIMEOUT = 75.0

# Explicit override that any harness or wrapper script can set.
_CLIENT_OVERRIDE_ENV = "RAMP_CLIENT_NAME"

# Exact env-var sentinels for harnesses whose vendors commit to setting them.
_HARNESS_SENTINELS: tuple[tuple[str, str], ...] = (
    ("CLAUDECODE", "claude-code"),
    ("OPENCODE", "opencode"),
    ("CODEX_SANDBOX", "codex"),
)

_SAFE_COMMENT_RE = re.compile(r"[^A-Za-z0-9._/+:-]+")
_MAX_COMMENT_LEN = 64


class RampClient:
    """Synchronous Ramp API client with auto-refresh."""

    def __init__(self, env: str, access_token: str | None = None) -> None:
        self.env = env
        self._static_access_token = access_token or os.environ.get("RAMP_ACCESS_TOKEN")

    def get(self, path: str, params: dict[str, str] | None = None) -> bytes:
        return self._do_request("GET", api_url(self.env, path, params))

    def get_url(self, url: str) -> bytes:
        if not _same_origin(url, base_url(self.env)):
            raise UnsafeRequestUrlError(url)
        return self._do_request("GET", url)

    def post(self, path: str, json_body: bytes) -> bytes:
        return self._do_request("POST", api_url(self.env, path), body=json_body)

    def patch(self, path: str, json_body: bytes) -> bytes:
        return self._do_request("PATCH", api_url(self.env, path), body=json_body)

    def put(self, path: str, json_body: bytes) -> bytes:
        return self._do_request("PUT", api_url(self.env, path), body=json_body)

    def delete(self, path: str, json_body: bytes | None = None) -> bytes:
        return self._do_request("DELETE", api_url(self.env, path), body=json_body)

    def post_multipart(
        self, path: str, data: dict[str, str], files: dict[str, tuple]
    ) -> bytes:
        """POST multipart/form-data (for file uploads)."""
        return self.request_multipart("POST", path, data, files)

    def request_multipart(
        self,
        method: str,
        path: str,
        data: dict[str, str],
        files: dict[str, tuple],
    ) -> bytes:
        """Send multipart/form-data using the operation's declared method."""
        return self._do_request_multipart(
            method.upper(), api_url(self.env, path), data=data, files=files
        )

    def _do_request_multipart(
        self, method: str, url: str, data: dict[str, str], files: dict[str, tuple]
    ) -> bytes:
        extra_headers = self._extra_auth_headers_or_raise()
        access_token = self._get_request_access_token()

        with httpx.Client(timeout=_REQUEST_TIMEOUT) as http:
            resp = self._request_multipart(
                http,
                method,
                url,
                access_token,
                data=data,
                files=files,
                extra_headers=extra_headers,
            )

            if resp.status_code == 401 and not self._static_access_token:
                new_token = try_refresh(self.env)
                if new_token:
                    resp = self._request_multipart(
                        http,
                        method,
                        url,
                        new_token,
                        data=data,
                        files=files,
                        extra_headers=extra_headers,
                    )
                else:
                    raise AuthRequiredError(self.env)

            if resp.status_code == 401:
                raise AuthRequiredError(self.env)
            if resp.is_error:
                raise ApiError(resp.status_code, resp.text)
            return resp.content

    def _do_request(self, method: str, url: str, body: bytes | None = None) -> bytes:
        extra_headers = self._extra_auth_headers_or_raise()
        access_token = self._get_request_access_token()

        with httpx.Client(timeout=_REQUEST_TIMEOUT) as http:
            resp = self._request(
                http, method, url, access_token, body=body, extra_headers=extra_headers
            )

            if resp.status_code == 401 and not self._static_access_token:
                new_token = try_refresh(self.env)
                if new_token:
                    resp = self._request(
                        http,
                        method,
                        url,
                        new_token,
                        body=body,
                        extra_headers=extra_headers,
                    )
                else:
                    raise AuthRequiredError(self.env)

            if resp.status_code == 401:
                raise AuthRequiredError(self.env)
            if resp.is_error:
                raise ApiError(resp.status_code, resp.text)
            return resp.content

    def _get_request_access_token(self) -> str:
        if self._static_access_token:
            return self._static_access_token
        return self._get_access_token()

    def _extra_auth_headers_or_raise(self) -> dict[str, str]:
        if missing_required_environment_auth(self.env):
            raise EnvironmentAuthRequiredError(
                environment_auth_required_message(self.env)
            )
        return extra_auth_headers(self.env)

    def _get_access_token(self) -> str:
        state = store.get_token_state(self.env)
        now = int(time.time())

        if state.access_token and not state.access_token_is_expired(now):
            if state.refresh_token and state.access_token_is_expiring_soon(now):
                try:
                    new_token = try_refresh(self.env)
                except RefreshFailedError:
                    return state.access_token
                if new_token:
                    return new_token
            return state.access_token

        if state.refresh_token:
            new_token = try_refresh(self.env)
            if new_token:
                return new_token
        raise AuthRequiredError(self.env)

    def _request(
        self,
        http: Any,
        method: str,
        url: str,
        token: str,
        body: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        headers = self._base_headers(token, extra_headers)
        if body is not None:
            headers["Content-Type"] = "application/json"
        return http.request(method, url, headers=headers, content=body)

    def _request_multipart(
        self,
        http: Any,
        method: str,
        url: str,
        token: str,
        data: dict[str, str],
        files: dict[str, tuple],
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        headers = self._base_headers(token, extra_headers)
        # Do NOT set Content-Type — httpx sets the multipart boundary automatically
        return http.request(method, url, headers=headers, data=data, files=files)

    def _base_headers(
        self,
        token: str,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": user_agent_string(),
            "Accept": "application/json",
            **agent_headers(infer_harness_name()),
        }
        headers.update(
            extra_headers if extra_headers is not None else extra_auth_headers(self.env)
        )
        return headers


def _same_origin(url: str, expected_base_url: str) -> bool:
    actual = _origin(url)
    expected = _origin(expected_base_url)
    return actual is not None and actual == expected


def _origin(url: str) -> tuple[str, str, int | None] | None:
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname or parts.username or parts.password:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = {"http": 80, "https": 443}.get(parts.scheme.lower())
    return parts.scheme.lower(), parts.hostname.lower(), port


def user_agent_string(environ: Mapping[str, str] | None = None) -> str:
    """Build the User-Agent header value, including a parenthesized
    product-comment naming the host harness when one can be identified."""
    base = f"ramp-cli/{VERSION}"
    client = infer_client_name(environ)
    if client is None:
        return base
    return f"{base} ({client})"


def infer_client_name(environ: Mapping[str, str] | None = None) -> str | None:
    """Return a sanitized client-name string when we can identify the host
    harness, otherwise None.

    Detection is intentionally narrow: an explicit RAMP_CLIENT_NAME override or
    one of a small list of exact env-var sentinels that the harness vendor
    commits to setting. TERM_PROGRAM, SHELL, and env-var prefix matching are
    deliberately excluded — they produced false positives for plain human CLI
    use and for unrelated tools that share a vendor namespace.
    """
    env = environ if environ is not None else os.environ

    override = _sanitize_comment(env.get(_CLIENT_OVERRIDE_ENV))
    if override:
        return override

    return infer_harness_name(env)


def infer_harness_name(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the host harness name when an exact sentinel is present."""
    env = environ if environ is not None else os.environ
    return next((name for key, name in _HARNESS_SENTINELS if env.get(key)), None)


def _sanitize_comment(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = _SAFE_COMMENT_RE.sub("-", value.strip())[:_MAX_COMMENT_LEN].strip(" -")
    return cleaned or None
