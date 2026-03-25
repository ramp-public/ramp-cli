"""Tests for the unified receipt view renderer."""

from __future__ import annotations

import io
import json
import re

import ramp_cli.views.receipt as receipt_view_mod
from ramp_cli.views.receipt import render_receipt_view


class _FakeTTY(io.StringIO):
    def isatty(self):
        return True


def _strip_ansi(text: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", text)


def _render_to_string(data: dict, resource_type: str, use_color: bool = True) -> str:
    """Render a receipt view and capture the output string."""
    buf = _FakeTTY()
    orig_stdout = receipt_view_mod.sys.stdout
    receipt_view_mod.sys.stdout = buf
    try:
        orig_cs = receipt_view_mod._color_supported
        receipt_view_mod._color_supported = lambda f: use_color
        try:
            render_receipt_view(
                json.dumps(data).encode(), resource_type, use_color=use_color
            )
        finally:
            receipt_view_mod._color_supported = orig_cs
    finally:
        receipt_view_mod.sys.stdout = orig_stdout
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _transaction_with_receipt_items():
    return {
        "id": "fd14cd6a-846e-4321-abcd-000000000001",
        "merchant_name": "Vanta",
        "amount": 9000,  # cents
        "state": "CLEARED",
        "user_transaction_time": "2022-04-28T12:00:00Z",
        "settlement_date": "2022-05-03T00:00:00Z",
        "synced_at": "2022-05-04T10:30:00Z",
        "card_holder": {
            "first_name": "Patrick",
            "last_name": "Robinson",
            "department_name": "Engineering",
        },
        "merchant_data": {
            "receipt": {
                "items": [
                    {
                        "description": "Vanta Automated Compliance",
                        "unit_cost": 50.00,
                        "quantity": 1,
                        "total": 50.00,
                    },
                    {
                        "description": "Vanta Risk Mgmt",
                        "unit_cost": 40.00,
                        "quantity": 1,
                        "total": 40.00,
                    },
                ]
            }
        },
    }


def _transaction_with_line_items_only():
    return {
        "id": "abc123",
        "merchant_name": "Office Depot",
        "amount": 25000,
        "state": "CLEARED",
        "line_items": [
            {"category": "Office Supplies", "amount": 15000},
            {"category": "Furniture", "amount": 10000},
        ],
    }


def _receipt_data():
    return {
        "id": "rcpt-001",
        "merchant_name": "Uber Eats",
        "amount": "45.50",
        "date": "2022-06-15T00:00:00Z",
        "state": "ACTIVE",
        "card_holder": "Jane Doe",
        "transaction_id": "txn-xyz",
        "receipt_url": "https://example.com/receipt.pdf",
        "created_at": "2022-06-15T12:00:00Z",
    }


def _reimbursement_data():
    return {
        "id": "reimb-001",
        "merchant": "Delta Airlines",
        "amount": 342.50,
        "user_full_name": "Alice Chen",
        "transaction_date": "2022-07-01T00:00:00Z",
        "state": "APPROVED",
        "synced_at": "2022-07-05T00:00:00Z",
        "submitted_at": "2022-07-02T00:00:00Z",
        "user_email": "alice@example.com",
        "line_items": [
            {"category": "Travel", "amount": {"amount": 34250, "currency_code": "USD"}},
        ],
    }


def _purchase_order_data():
    return {
        "id": "po-001",
        "purchase_order_number": "PO-2022-0042",
        "name": "AWS Annual",
        "amount": {"amount": 120000, "currency_code": "USD"},
        "created_at": "2022-08-01T00:00:00Z",
        "billing_status": "OPEN",
        "line_items": [
            {
                "description": "EC2 Reserved Instances",
                "unit_price": 500.00,
                "quantity": 2,
                "amount": {"amount": 100000, "currency_code": "USD"},
            },
            {
                "description": "S3 Storage",
                "unit_price": 200.00,
                "quantity": 1,
                "amount": {"amount": 20000, "currency_code": "USD"},
            },
        ],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTransaction5ColLineItems:
    def test_renders_merchant_in_title(self):
        output = _strip_ansi(
            _render_to_string(_transaction_with_receipt_items(), "transaction")
        )
        assert "TRANSACTION" in output
        assert "Vanta" in output

    def test_renders_5col_header(self):
        output = _strip_ansi(
            _render_to_string(_transaction_with_receipt_items(), "transaction")
        )
        assert "Desc." in output
        assert "Unit Rate" in output
        assert "Count" in output

    def test_renders_line_item_descriptions(self):
        output = _strip_ansi(
            _render_to_string(_transaction_with_receipt_items(), "transaction")
        )
        assert "Vanta Automated Compliance" in output
        assert "Vanta Risk Mgmt" in output

    def test_renders_amount_from_cents(self):
        output = _strip_ansi(
            _render_to_string(_transaction_with_receipt_items(), "transaction")
        )
        assert "$90.00" in output


class TestTransaction3ColFallback:
    def test_renders_category_column(self):
        output = _strip_ansi(
            _render_to_string(_transaction_with_line_items_only(), "transaction")
        )
        assert "Category" in output
        assert "Office Supplies" in output
        assert "Furniture" in output

    def test_no_unit_rate_column(self):
        output = _strip_ansi(
            _render_to_string(_transaction_with_line_items_only(), "transaction")
        )
        assert "Unit Rate" not in output


class TestReceiptNoLineItems:
    def test_no_line_items_section(self):
        output = _strip_ansi(_render_to_string(_receipt_data(), "receipt"))
        assert "Line Items" not in output

    def test_shows_transaction_id_in_status(self):
        output = _strip_ansi(_render_to_string(_receipt_data(), "receipt"))
        assert "txn-xyz" in output

    def test_shows_receipt_url(self):
        output = _strip_ansi(_render_to_string(_receipt_data(), "receipt"))
        assert "receipt.pdf" in output


class TestReimbursementView:
    def test_renders_user_name(self):
        output = _strip_ansi(_render_to_string(_reimbursement_data(), "reimbursement"))
        assert "Alice Chen" in output

    def test_renders_amount_from_float(self):
        output = _strip_ansi(_render_to_string(_reimbursement_data(), "reimbursement"))
        assert "$342.50" in output

    def test_renders_3col_line_items(self):
        output = _strip_ansi(_render_to_string(_reimbursement_data(), "reimbursement"))
        assert "Travel" in output

    def test_renders_state(self):
        output = _strip_ansi(_render_to_string(_reimbursement_data(), "reimbursement"))
        assert "APPROVED" in output


class TestPurchaseOrder5Col:
    def test_renders_po_number_in_summary(self):
        output = _strip_ansi(
            _render_to_string(_purchase_order_data(), "purchase-order")
        )
        assert "PO-2022-0042" in output

    def test_renders_5col_items(self):
        output = _strip_ansi(
            _render_to_string(_purchase_order_data(), "purchase-order")
        )
        assert "EC2 Reserved Instances" in output
        assert "S3 Storage" in output


class TestMissingFieldsGraceful:
    def test_minimal_payload_renders(self):
        data = {"id": "minimal-001", "amount": 500}
        output = _strip_ansi(_render_to_string(data, "transaction"))
        assert "minimal-001" in output
        assert "$5.00" in output


class TestNonTTYReturnsFalse:
    def test_non_tty_returns_false(self):
        buf = io.StringIO()

        orig = receipt_view_mod.sys.stdout
        receipt_view_mod.sys.stdout = buf
        try:
            result = render_receipt_view(
                json.dumps({"id": "x"}).encode(), "transaction"
            )
        finally:
            receipt_view_mod.sys.stdout = orig
        assert result is False


class TestNoColorNoAnsi:
    def test_no_ansi_codes_in_content(self):
        output = _render_to_string(
            _transaction_with_receipt_items(), "transaction", use_color=False
        )
        # _window_wrap always adds shadow ANSI — check only content lines (between │ ... │)
        for line in output.splitlines():
            if line.strip().startswith("│") and line.strip().endswith("│"):
                assert "\033[" not in line, f"ANSI found in content line: {line}"


class TestNoWaveNoFooter:
    def test_no_wave_or_footer_text(self):
        output = _strip_ansi(
            _render_to_string(_transaction_with_receipt_items(), "transaction")
        )
        assert "shortcuts" not in output.lower()
        assert "commands" not in output.lower()
