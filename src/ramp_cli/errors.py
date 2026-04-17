"""Exit codes and error types."""

from __future__ import annotations

import json

EXIT_SUCCESS = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_AUTH_REQUIRED = 4


# Maps ramp_error_code values from API responses to actionable CLI hints.
# Only include codes where the user can self-service; skip transient/server errors.
# Source: Datadog 30-day analysis of 4xx errors on /developer/v1/agent-tools/*.
_ERROR_CODE_HINTS: dict[str, str] = {
    # Request validation failure — bad or missing parameters.
    "2001": (
        "The request body failed validation.  Run `ramp <resource> <command> --help`\n"
        "  to check required parameters and accepted values."
    ),
    # Auth token not found.
    "DEVELOPER_7002": (
        "No valid auth token was found for this request.\n"
        "  Run `ramp auth login` to authenticate, then retry."
    ),
    # Expired access token.
    "DEVELOPER_7028": (
        "Your access token has expired.\n  Run `ramp auth login` to re-authenticate."
    ),
    # Fund not eligible for agent card payments.
    "DEVELOPER_7078": (
        "Run `ramp funds list` to see which funds support agent card payments.\n"
        "  If the list is empty, virtual cards may not be enabled on your funds."
    ),
    # Insufficient OAuth scope.
    "DEVELOPER_7100": (
        "Your token is missing the OAuth scope required by this endpoint.\n"
        "  Run `ramp auth login` to re-authorize with the necessary permissions."
    ),
    # Tool not found on the server — spec may be outdated.
    "DEVELOPER_7127": (
        "This tool does not exist on the server.  Your CLI spec may be outdated.\n"
        "  Run `ramp tools list` to see available tools, or update the CLI."
    ),
}


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
            error_v2 = parsed.get("error_v2")
            if not isinstance(error_v2, dict):
                error_v2 = {}
            error_obj = parsed.get("error")
            if not isinstance(error_obj, dict):
                error_obj = {}
            detail = (
                parsed.get("message")
                or error_v2.get("message")
                or error_obj.get("message")
                or self.body[:500]
            )
            error_code = (
                parsed.get("ramp_error_code")
                or error_v2.get("error_code")
                or error_v2.get("code")
            )
        except Exception:
            detail = self.body[:500]
            error_code = None

        # UX-6: Append actionable hint for 403 errors
        if status_code == 403:
            detail += (
                "\n\n  This usually means your token doesn't have the required scope."
                "\n  To fix this, log in again:  ramp auth login"
            )

        # Append actionable hints for known error codes
        hint = _ERROR_CODE_HINTS.get(error_code or "")
        if hint:
            detail += f"\n\n  {hint}"

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
