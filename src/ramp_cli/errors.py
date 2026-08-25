"""Exit codes and error types."""

from __future__ import annotations

import json
from http import HTTPStatus

EXIT_SUCCESS = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_AUTH_REQUIRED = 4


# Maps ramp_error_code values from API responses to actionable CLI hints.
# Only include codes where the user can self-service; skip transient/server errors.
# Source: Datadog 30-day analysis of 4xx errors on /developer/v1/agent-tools/*.
_ERROR_CODE_HINTS: dict[str, tuple[frozenset[int], str]] = {
    # Request validation failure — bad or missing parameters.
    "2001": (
        frozenset({400, 422}),
        (
            "The request body failed validation.  Run `ramp <resource> <command> --help`\n"
            "  to check required parameters and accepted values."
        ),
    ),
    # Request-body validation failure on an agent-tool (DEVELOPER_INVALID_SCHEMA).
    "DEVELOPER_7001": (
        frozenset({400, 422}),
        (
            "The request body failed validation — a required field is missing or invalid.\n"
            "  Run `ramp <resource> <command> --help` to check required parameters and\n"
            "  accepted values.\n"
        ),
    ),
    # Auth token not found.
    "DEVELOPER_7002": (
        frozenset({401}),
        (
            "No valid auth token was found for this request.\n"
            "  Run `ramp auth login` to authenticate, then retry."
        ),
    ),
    # Expired access token.
    "DEVELOPER_7028": (
        frozenset({401}),
        ("Your access token has expired.\n  Run `ramp auth login` to re-authenticate."),
    ),
    # Agent Card credentials request did not traverse the required vault proxy.
    "DEVELOPER_7098": (
        frozenset({400}),
        (
            "This Agent Card credential request must go through the payment-vault "
            "proxy.\n  Set RAMP_VAULT_PROXY_ENABLED=1 and RAMP_VAULT_PROXY_URL to "
            "the proxy host provisioned for your integration, or contact your Ramp "
            "representative."
        ),
    ),
    # Insufficient OAuth scope.
    "DEVELOPER_7100": (
        frozenset({403}),
        (
            "Your token is missing the OAuth scope required by this endpoint.\n"
            "  Refresh the tool definitions with `ramp tools refresh`, then run\n"
            "  `ramp auth login` to re-authorize with the necessary permissions."
        ),
    ),
    # Tool not found on the server — spec may be outdated.
    "DEVELOPER_7127": (
        frozenset({404}),
        (
            "This tool does not exist on the server.  Your CLI spec may be outdated.\n"
            "  Run `ramp tools list` to see available tools, or update the CLI."
        ),
    ),
}


class RampCLIError(Exception):
    """Base error with an exit code."""

    def __init__(self, message: str, code: int = EXIT_RUNTIME) -> None:
        super().__init__(message)
        self.code = code


class ApiError(RampCLIError):
    def __init__(
        self,
        status_code: int,
        body: str,
        *,
        contextual_hint: str | None = None,
    ) -> None:
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
                or parsed.get("detail")
                or self.body[:500]
            )
            if not isinstance(detail, str):
                detail = self.body[:500]
            validation_details = (
                parsed.get("errors")
                or parsed.get("details")
                or error_obj.get("details")
            )
            if validation_details:
                rendered = json.dumps(
                    validation_details,
                    ensure_ascii=True,
                    indent=2,
                    sort_keys=True,
                )
                detail += f"\n\nValidation details:\n{rendered}"
            error_code = (
                parsed.get("ramp_error_code")
                or error_v2.get("error_code")
                or error_v2.get("code")
            )
        except Exception:
            detail = self.body[:500]
            error_code = None

        if not detail:
            try:
                detail = HTTPStatus(status_code).phrase
            except ValueError:
                detail = "Request failed."

        self.error_code = error_code

        # Error codes can be reused in responses with different HTTP semantics.
        # Only offer recovery guidance when both classifications agree.
        hint = contextual_hint
        error_hint = _ERROR_CODE_HINTS.get(error_code or "")
        if hint is None and error_hint is not None:
            expected_statuses, candidate_hint = error_hint
            if status_code in expected_statuses:
                hint = candidate_hint
        if hint:
            detail += f"\n\n  {hint}"

        self.detail = detail
        super().__init__(f"API error {status_code}: {detail}")


class AuthRequiredError(RampCLIError):
    def __init__(self, env: str, profile: str | None = None) -> None:
        profile_label = f" profile {profile!r}" if profile else ""
        login_command = "ramp auth login"
        if profile == "agent":
            login_command += " --client-id <agent-client-id>"
        super().__init__(
            f"Not authenticated for {env}{profile_label} — run: "
            f"ramp --env {env} {login_command.removeprefix('ramp ')}",
            code=EXIT_AUTH_REQUIRED,
        )
        self.env = env
        self.profile = profile


class EnvironmentAuthRequiredError(RampCLIError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=EXIT_AUTH_REQUIRED)


class RefreshFailedError(RampCLIError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=EXIT_RUNTIME)


class UnsafeRequestUrlError(RampCLIError):
    def __init__(self, url: str) -> None:
        super().__init__(
            f"Refusing to send authenticated request to an untrusted URL: {url}",
            code=EXIT_RUNTIME,
        )
