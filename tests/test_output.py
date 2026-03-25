"""Tests for output formatting."""

from __future__ import annotations

from ramp_cli.output.formatter import (
    extract_headers,
    format_value,
    resolve_format,
    truncate,
)


def test_resolve_format_flag_wins():
    assert resolve_format("json", "table") == "json"
    assert resolve_format("table", "json") == "table"


def test_resolve_format_config_default():
    assert resolve_format("", "json") == "json"
    assert resolve_format("", "table") == "table"


def test_truncate_short_string():
    assert truncate("hello", 10) == "hello"


def test_truncate_long_string():
    assert truncate("hello world", 8) == "hello..."


def test_truncate_boundary():
    assert truncate("abc", 3) == "abc"


def test_format_value_none():
    assert format_value(None) == ""


def test_format_value_string():
    assert format_value("hello") == "hello"


def test_format_value_int_float():
    assert format_value(42.0) == "42"
    assert format_value(3.14) == "3.14"


def test_format_value_bool():
    assert format_value(True) == "true"
    assert format_value(False) == "false"


def test_format_value_dict():
    assert format_value({"a": 1}) == "{...}"


def test_format_value_list():
    assert format_value([1, 2, 3]) == "1, 2, 3"


def test_extract_headers_priority():
    item = {"foo": 1, "id": "abc", "name": "test", "bar": 2}
    headers = extract_headers(item)
    assert headers[0] == "name"
    assert "id" not in headers[:2]  # id is not prioritized; available via --columns


def test_extract_headers_max_cols():
    item = {f"col{i}": i for i in range(20)}
    item["id"] = "x"
    headers = extract_headers(item, wide=False)
    assert len(headers) <= 8


def test_extract_headers_wide():
    item = {f"col{i}": i for i in range(20)}
    headers = extract_headers(item, wide=True)
    assert len(headers) == 20
