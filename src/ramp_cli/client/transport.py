"""Authenticated HTTP transport shared by Ramp service clients."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from typing import Any

import httpx

from ramp_cli import __version__ as VERSION
from ramp_cli.auth import store
from ramp_cli.auth.environment import (
    environment_auth_required_message,
    extra_auth_headers,
    missing_required_environment_auth,
)
from ramp_cli.auth.refresh import try_refresh
from ramp_cli.client.agent import infer_client_name, infer_harness_name
from ramp_cli.client.headers import agent_headers
from ramp_cli.errors import (
    ApiError,
    AuthRequiredError,
    EnvironmentAuthRequiredError,
    RefreshFailedError,
)

# Client timeout should exceed the server-side timeout (60s) so we always
# receive the server's response rather than giving up prematurely.
_REQUEST_TIMEOUT = 75.0


class AuthenticatedRampTransport:
    """Send authenticated requests without owning service URLs or operations."""

    def __init__(
        self,
        env: str,
        access_token: str | None = None,
        profile: str | None = None,
    ) -> None:
        self.env = env
        self.profile = profile
        self._static_access_token = access_token or os.environ.get("RAMP_ACCESS_TOKEN")

    def request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        request_headers: dict[str, str] | None = None,
        proxied: bool = False,
    ) -> bytes:
        extra_headers = self._extra_auth_headers_or_raise()
        access_token = self._get_request_access_token()

        with httpx.Client(timeout=_REQUEST_TIMEOUT) as http:
            resp = self._request(
                http,
                method,
                url,
                access_token,
                body=body,
                extra_headers=extra_headers,
                request_headers=request_headers,
            )

            # A proxied request (payment-vault routing) forwards both proxy-origin
            # and Core-origin responses. A genuine Core bearer expiry still gets
            # the normal refresh-and-retry below, but a residual 401 (e.g. a bad
            # proxy-auth key, or a Core auth failure that refresh could not fix)
            # is surfaced as the actual response via ApiError instead of being
            # reported as AuthRequiredError, which would wrongly send the user to
            # `ramp auth login` for a proxy-configuration problem.
            if resp.status_code == 401 and not self._static_access_token:
                new_token = self._try_refresh()
                if new_token:
                    resp = self._request(
                        http,
                        method,
                        url,
                        new_token,
                        body=body,
                        extra_headers=extra_headers,
                        request_headers=request_headers,
                    )
                elif proxied:
                    raise ApiError(resp.status_code, resp.text)
                else:
                    raise AuthRequiredError(self.env, self.profile)

            if resp.status_code == 401:
                if proxied:
                    raise ApiError(resp.status_code, resp.text)
                raise AuthRequiredError(self.env, self.profile)
            if resp.is_error:
                raise ApiError(resp.status_code, resp.text)
            return resp.content

    def request_multipart(
        self,
        method: str,
        url: str,
        data: dict[str, str],
        files: dict[str, tuple],
        request_headers: dict[str, str] | None = None,
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
                request_headers=request_headers,
            )

            if resp.status_code == 401 and not self._static_access_token:
                new_token = self._try_refresh()
                if new_token:
                    resp = self._request_multipart(
                        http,
                        method,
                        url,
                        new_token,
                        data=data,
                        files=files,
                        extra_headers=extra_headers,
                        request_headers=request_headers,
                    )
                else:
                    raise AuthRequiredError(self.env, self.profile)

            if resp.status_code == 401:
                raise AuthRequiredError(self.env, self.profile)
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
        state = (
            store.get_token_state(self.env, profile=self.profile)
            if self.profile
            else store.get_token_state(self.env)
        )
        now = int(time.time())

        if state.access_token and not state.access_token_is_expired(now):
            if state.refresh_token and state.access_token_is_expiring_soon(now):
                try:
                    new_token = self._try_refresh()
                except RefreshFailedError:
                    return state.access_token
                if new_token:
                    return new_token
            return state.access_token

        if state.refresh_token:
            new_token = self._try_refresh()
            if new_token:
                return new_token
        raise AuthRequiredError(self.env, self.profile)

    def _try_refresh(self) -> str | None:
        if self.profile:
            return try_refresh(self.env, profile=self.profile)
        return try_refresh(self.env)

    def _request(
        self,
        http: Any,
        method: str,
        url: str,
        token: str,
        body: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
        request_headers: dict[str, str] | None = None,
    ) -> Any:
        headers = self._base_headers(token, extra_headers)
        if body is not None:
            headers["Content-Type"] = "application/json"
        if request_headers:
            headers.update(request_headers)
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
        request_headers: dict[str, str] | None = None,
    ) -> Any:
        headers = self._base_headers(token, extra_headers)
        if request_headers:
            headers.update(request_headers)
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


def user_agent_string(environ: Mapping[str, str] | None = None) -> str:
    """Build the User-Agent header, including a host-harness comment when known."""
    base = f"ramp-cli/{VERSION}"
    client = infer_client_name(environ)
    if client is None:
        return base
    return f"{base} ({client})"
