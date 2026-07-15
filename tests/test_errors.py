"""Tests for API error parsing and actionable hints."""

from __future__ import annotations

import json

from ramp_cli.errors import ApiError


def test_business_authorization_error_does_not_claim_scope_is_missing() -> None:
    error = ApiError(
        403,
        json.dumps(
            {
                "error_v2": {
                    "error_code": "CUSTOMER_7004",
                    "message": "Business not authorized to use this application",
                }
            }
        ),
    )

    message = str(error)
    assert "Business not authorized to use this application" in message
    assert "missing the OAuth scope" not in message
    assert "log in again" not in message


def test_insufficient_scope_error_includes_scope_specific_hint() -> None:
    error = ApiError(
        403,
        json.dumps(
            {
                "error_v2": {
                    "error_code": "DEVELOPER_7100",
                    "message": "Insufficient scope",
                }
            }
        ),
    )

    message = str(error)
    assert "Insufficient scope" in message
    assert "missing the OAuth scope" in message
    assert "ramp tools refresh" in message
    assert "ramp auth login" in message
    assert error.error_code == "DEVELOPER_7100"


def test_invalid_schema_error_hints_at_missing_field() -> None:
    error = ApiError(
        422,
        json.dumps(
            {
                "error_v2": {
                    "error_code": "DEVELOPER_7001",
                    "message": "There was an error.",
                }
            }
        ),
    )

    message = str(error)
    assert "API error 422" in message
    assert "failed validation" in message
    assert "--help" in message
