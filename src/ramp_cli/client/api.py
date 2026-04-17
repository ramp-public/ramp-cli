"""httpx-based API client with automatic token refresh on 401."""

from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from ramp_cli import __version__ as VERSION
from ramp_cli.auth import store
from ramp_cli.auth.refresh import try_refresh
from ramp_cli.client.session import get_session_id
from ramp_cli.config.constants import base_url
from ramp_cli.errors import ApiError, AuthRequiredError, RefreshFailedError

# Client timeout should exceed the server-side timeout (60s) so we always
# receive the server's response rather than giving up prematurely.
_REQUEST_TIMEOUT = 75.0


class RampClient:
    """Synchronous Ramp API client with auto-refresh."""

    def __init__(self, env: str, access_token: str | None = None) -> None:
        self.env = env
        self.base_url = base_url(env)
        self._static_access_token = access_token or os.environ.get("RAMP_ACCESS_TOKEN")

    def get(self, path: str, params: dict[str, str] | None = None) -> bytes:
        url = self.base_url + path
        if params:
            filtered = {k: v for k, v in params.items() if v}
            if filtered:
                url += "?" + urlencode(filtered)
        return self._do_request("GET", url)

    def get_url(self, url: str) -> bytes:
        return self._do_request("GET", url)

    def post(self, path: str, json_body: bytes) -> bytes:
        return self._do_request("POST", self.base_url + path, body=json_body)

    def patch(self, path: str, json_body: bytes) -> bytes:
        return self._do_request("PATCH", self.base_url + path, body=json_body)

    def put(self, path: str, json_body: bytes) -> bytes:
        return self._do_request("PUT", self.base_url + path, body=json_body)

    def delete(self, path: str, json_body: bytes | None = None) -> bytes:
        return self._do_request("DELETE", self.base_url + path, body=json_body)

    def post_multipart(
        self, path: str, data: dict[str, str], files: dict[str, tuple]
    ) -> bytes:
        """POST multipart/form-data (for file uploads)."""
        return self._do_request_multipart(
            "POST", self.base_url + path, data=data, files=files
        )

    def _do_request_multipart(
        self, method: str, url: str, data: dict[str, str], files: dict[str, tuple]
    ) -> bytes:
        access_token = self._get_request_access_token()

        with httpx.Client(timeout=_REQUEST_TIMEOUT) as http:
            resp = self._request_multipart(
                http, method, url, access_token, data=data, files=files
            )

            if resp.status_code == 401 and not self._static_access_token:
                new_token = try_refresh(self.env)
                if new_token:
                    resp = self._request_multipart(
                        http, method, url, new_token, data=data, files=files
                    )
                else:
                    raise AuthRequiredError(self.env)

            if resp.status_code == 401:
                raise AuthRequiredError(self.env)
            if resp.is_error:
                raise ApiError(resp.status_code, resp.text)
            return resp.content

    def _do_request(self, method: str, url: str, body: bytes | None = None) -> bytes:
        access_token = self._get_request_access_token()

        with httpx.Client(timeout=_REQUEST_TIMEOUT) as http:
            resp = self._request(http, method, url, access_token, body=body)

            if resp.status_code == 401 and not self._static_access_token:
                new_token = try_refresh(self.env)
                if new_token:
                    resp = self._request(http, method, url, new_token, body=body)
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
    ) -> Any:
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": f"ramp-cli/{VERSION}",
            "Accept": "application/json",
            "X-External-Session-Id": get_session_id(),
        }
        devtool_token = os.environ.get("RAMP_DEVTOOL_TOKEN")
        if devtool_token:
            headers["X-Rampy-Auth"] = devtool_token
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
    ) -> Any:
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": f"ramp-cli/{VERSION}",
            "Accept": "application/json",
            "X-External-Session-Id": get_session_id(),
        }
        # Do NOT set Content-Type — httpx sets the multipart boundary automatically
        return http.request(method, url, headers=headers, data=data, files=files)
