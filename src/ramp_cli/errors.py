"""Exit codes and error types."""

from __future__ import annotations

import json

EXIT_SUCCESS = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_AUTH_REQUIRED = 4


class RampCLIError(Exception):
    """Base error with an exit code."""

    def __init__(self, message: str, code: int = EXIT_RUNTIME) -> None:
        super().__init__(message)
        self.code = code


class ApiError(RampCLIError):
    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body.strip()

        # CB-3 / QW-6: Strip HTML from error bodies (e.g. Flask/Werkzeug 404 pages)
        if self.body and (
            self.body.lower().startswith("<!doctype")
            or self.body.lower().startswith("<html")
        ):
            self.body = "Resource not found"

        try:
            parsed = json.loads(self.body)
            detail = (
                parsed.get("message")
                or parsed.get("error_v2", {}).get("message")
                or parsed.get("error", {}).get("message")
                or self.body[:500]
            )
        except Exception:
            detail = self.body[:500]

        # UX-6: Append actionable hint for 403 errors
        if status_code == 403:
            detail += (
                "\n\n  This usually means your token doesn't have the required scope."
                "\n  To fix this, log in again:  ramp auth login"
            )

        super().__init__(f"API error {status_code}: {detail}")


class AuthRequiredError(RampCLIError):
    def __init__(self, env: str) -> None:
        super().__init__(
            f"Not authenticated for {env} — run: ramp --env {env} auth login",
            code=EXIT_AUTH_REQUIRED,
        )
        self.env = env


class RefreshFailedError(RampCLIError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=EXIT_RUNTIME)
