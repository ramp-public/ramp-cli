"""Tests for API error parsing and actionable hints."""

from __future__ import annotations

import json

from ramp_cli.errors import EXIT_RUNTIME, ApiError


def _error_body(error_code: str, message: str) -> str:
    return json.dumps({"error_v2": {"error_code": error_code, "message": message}})


def test_unauthenticated_error_includes_login_hint() -> None:
    error = ApiError(
        401,
        _error_body("DEVELOPER_7002", "Authentication token not found"),
    )

    message = str(error)
    assert "Authentication token not found" in message
    assert "No valid auth token was found" in message
    assert "ramp auth login" in message
    assert error.code == EXIT_RUNTIME


def test_expired_token_error_includes_reauthorization_hint() -> None:
    error = ApiError(
        401,
        _error_body("DEVELOPER_7028", "Access token expired"),
    )

    message = str(error)
    assert "Access token expired" in message
    assert "ramp auth login" in message


def test_vault_required_error_includes_proxy_hint() -> None:
    error = ApiError(
        400,
        _error_body("DEVELOPER_7098", "Vault service required"),
    )

    message = str(error)
    assert "Vault service required" in message
    assert "payment-vault proxy" in message
    assert "RAMP_VAULT_PROXY_ENABLED" in message
    assert "RAMP_VAULT_PROXY_URL" in message


def test_business_authorization_error_does_not_claim_scope_is_missing() -> None:
    error = ApiError(
        403,
        _error_body(
            "CUSTOMER_7004",
            "Business not authorized to use this application",
        ),
    )

    message = str(error)
    assert "Business not authorized to use this application" in message
    assert "missing the OAuth scope" not in message
    assert "log in again" not in message


def test_insufficient_scope_error_includes_scope_specific_hint() -> None:
    error = ApiError(
        403,
        _error_body("DEVELOPER_7100", "Insufficient scope"),
    )

    message = str(error)
    assert "Insufficient scope" in message
    assert "missing the OAuth scope" in message
    assert "ramp tools refresh" in message
    assert "ramp auth login" in message
    assert error.error_code == "DEVELOPER_7100"


def test_resource_not_found_does_not_claim_auth_is_missing() -> None:
    error = ApiError(
        404,
        _error_body("DEVELOPER_7002", "Session not found"),
    )

    message = str(error)
    assert "API error 404: Session not found" in message
    assert "No valid auth token was found" not in message
    assert "ramp auth login" not in message
    assert error.code == EXIT_RUNTIME


def test_invalid_schema_error_hints_at_missing_field() -> None:
    error = ApiError(
        422,
        _error_body("DEVELOPER_7001", "There was an error."),
    )

    message = str(error)
    assert "API error 422" in message
    assert "failed validation" in message
    assert "--help" in message
    assert "auth login" not in message
