"""Tests for pagination extraction in agent mode."""

from __future__ import annotations

import pytest

from ramp_cli.tools.commands import _extract_pagination


class TestExtractPagination:
    @pytest.mark.parametrize(
        "data,expected",
        [
            pytest.param(
                {"transactions": [], "next_page_cursor": "tok_abc"},
                {"next_cursor": "tok_abc"},
                id="next_page_cursor",
            ),
            pytest.param(
                {"bills": [], "page_cursor": "cur_123"},
                {"next_cursor": "cur_123"},
                id="page_cursor",
            ),
            pytest.param(
                {"items": [], "cursor": "c_999"},
                {"next_cursor": "c_999"},
                id="cursor",
            ),
            pytest.param(
                {"items": [], "next": "n_456"},
                {"next_cursor": "n_456"},
                id="next",
            ),
            pytest.param(
                {"transactions": [], "total_count": 5},
                None,
                id="no_cursor_field",
            ),
            pytest.param(
                {"transactions": [], "next_page_cursor": None},
                None,
                id="cursor_is_none_means_last_page",
            ),
            pytest.param(
                "not a dict",
                None,
                id="non_dict_response",
            ),
            pytest.param(
                None,
                None,
                id="none_response",
            ),
        ],
    )
    def test_extract_pagination(self, data, expected):
        assert _extract_pagination(data) == expected
