"""In-place JSON5 value edits keep everything but the replaced value."""

from __future__ import annotations

import json5
import pytest

from ramp_cli.json5_edit import replace_value

_DOCUMENT = """{
  // the provider: "model": "not this"
  $schema: 'https://opencode.ai/config.json',
  "model": "ramp-router/old", /* trailing } comment */
  plugins: [
    "other-plugin",
    {package: "file:///p", options: {'defaultModel': 'old', note: "a // b",},},
  ],
}
"""


def test_replaces_only_the_value_and_keeps_comments_and_layout():
    updated = replace_value(_DOCUMENT, ("model",), "ramp-router/new")
    assert updated == _DOCUMENT.replace('"ramp-router/old"', '"ramp-router/new"')


def test_reaches_nested_values_through_arrays_and_quoted_keys():
    updated = replace_value(_DOCUMENT, ("plugins", 1, "options", "defaultModel"), "new")
    assert updated == _DOCUMENT.replace("'old'", '"new"')
    assert json5.loads(updated)["plugins"][1]["options"] == {
        "defaultModel": "new",
        "note": "a // b",
    }


def test_a_repeated_key_resolves_to_its_last_occurrence():
    assert replace_value('{"a": 1, "a": 2}', ("a",), 3) == '{"a": 1, "a": 3}'
    assert replace_value("{a: {b: 1}, a: {b: 2}}", ("a", "b"), 3) == (
        "{a: {b: 1}, a: {b: 3}}"
    )
    # The parser keeps only the last "a", which has no "b".
    assert replace_value("{a: {b: 1}, a: {}}", ("a", "b"), 3) is None


@pytest.mark.parametrize(
    ("text", "path"),
    [
        ('{"a": 1}', ("b",)),
        ('{"a": [1]}', ("a", 1)),
        ('{"a": 1', ("a",)),
        ('{"a": 1} trailing', ("a",)),
        ('{"a": /* open', ("a",)),
    ],
)
def test_a_missing_value_or_broken_document_is_none(text, path):
    assert replace_value(text, path, 2) is None
