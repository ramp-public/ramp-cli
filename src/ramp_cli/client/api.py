"""Core Developer API client."""

from __future__ import annotations

from urllib.parse import urlsplit

from ramp_cli.client.agent import infer_client_name
from ramp_cli.client.transport import AuthenticatedRampTransport, user_agent_string
from ramp_cli.config.constants import api_url, base_url
from ramp_cli.errors import UnsafeRequestUrlError


class RampClient:
    """Synchronous Core Developer API client with automatic token refresh."""

    def __init__(self, env: str, access_token: str | None = None) -> None:
        self.env = env
        self._transport = AuthenticatedRampTransport(env, access_token)

    def get(
        self,
        path: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        return self._transport.request(
            "GET",
            api_url(self.env, path, params),
            request_headers=headers,
        )

    def get_url(self, url: str) -> bytes:
        if not _same_origin(url, base_url(self.env)):
            raise UnsafeRequestUrlError(url)
        return self._transport.request("GET", url)

    def post(
        self, path: str, json_body: bytes, headers: dict[str, str] | None = None
    ) -> bytes:
        return self._transport.request(
            "POST",
            api_url(self.env, path),
            body=json_body,
            request_headers=headers,
        )

    def patch(
        self, path: str, json_body: bytes, headers: dict[str, str] | None = None
    ) -> bytes:
        return self._transport.request(
            "PATCH",
            api_url(self.env, path),
            body=json_body,
            request_headers=headers,
        )

    def put(
        self, path: str, json_body: bytes, headers: dict[str, str] | None = None
    ) -> bytes:
        return self._transport.request(
            "PUT",
            api_url(self.env, path),
            body=json_body,
            request_headers=headers,
        )

    def delete(
        self,
        path: str,
        json_body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        return self._transport.request(
            "DELETE",
            api_url(self.env, path),
            body=json_body,
            request_headers=headers,
        )

    def post_multipart(
        self,
        path: str,
        data: dict[str, str],
        files: dict[str, tuple],
        headers: dict[str, str] | None = None,
    ) -> bytes:
        """POST multipart/form-data (for file uploads)."""
        return self.request_multipart("POST", path, data, files, headers=headers)

    def request_multipart(
        self,
        method: str,
        path: str,
        data: dict[str, str],
        files: dict[str, tuple],
        headers: dict[str, str] | None = None,
    ) -> bytes:
        """Send multipart/form-data using the operation's declared method."""
        return self._transport.request_multipart(
            method.upper(),
            api_url(self.env, path),
            data=data,
            files=files,
            request_headers=headers,
        )


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


__all__ = ["RampClient", "infer_client_name", "user_agent_string"]
