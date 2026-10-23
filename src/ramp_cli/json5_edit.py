"""Change one value in a JSON or JSON5 document and keep the rest as written.

Agent configs such as OpenCode's are JSON5: they can hold comments, trailing
commas, unquoted keys, and single-quoted strings. Parsing them and dumping the
result loses all of that, so a background model change edits the value's text
in place instead. :func:`replace_value` only locates and replaces; the caller
parses the result to confirm that nothing but that value changed.
"""

from __future__ import annotations

import json

import json5

_WHITESPACE = " \t\r\n\v\f\ufeff\u00a0\u2028\u2029"
# Characters that end an unquoted key or a literal (number, true, null, ...).
_LITERAL_END = _WHITESPACE + ",:[]{}/"

Key = str | int


class _Scanner:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def peek(self) -> str:
        if self.pos >= len(self.text):
            raise ValueError("unexpected end of document")
        return self.text[self.pos]

    def expect(self, char: str) -> None:
        if self.peek() != char:
            raise ValueError(f"expected {char!r} at offset {self.pos}")
        self.pos += 1

    def skip(self) -> None:
        text = self.text
        while self.pos < len(text):
            if text[self.pos] in _WHITESPACE:
                self.pos += 1
            elif text.startswith("//", self.pos):
                end = text.find("\n", self.pos)
                self.pos = len(text) if end < 0 else end + 1
            elif text.startswith("/*", self.pos):
                end = text.find("*/", self.pos + 2)
                if end < 0:
                    raise ValueError("unterminated comment")
                self.pos = end + 2
            else:
                return

    def string(self) -> str:
        """Consume a quoted string; return its raw text, quotes included."""
        start = self.pos
        quote = self.text[self.pos]
        self.pos += 1
        while True:
            char = self.peek()
            if char == "\\":
                self.pos += 2
            elif char == quote:
                self.pos += 1
                return self.text[start : self.pos]
            else:
                self.pos += 1

    def literal(self) -> str:
        start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] not in _LITERAL_END:
            self.pos += 1
        if self.pos == start:
            raise ValueError(f"unexpected character at offset {start}")
        return self.text[start : self.pos]

    def key(self) -> str:
        if self.peek() in "\"'":
            return json5.loads(self.string())
        return self.literal()

    def value(self, path: tuple[Key, ...] | None) -> tuple[int, int] | None:
        """Consume one value; return the span of the value at ``path`` in it.

        ``path`` is relative to this value; None means nothing below is wanted.
        A repeated key resolves to its last occurrence, as JSON5 parsers do.
        """
        self.skip()
        start = self.pos
        found = None
        char = self.peek()
        if char in "{[":
            closing = "}" if char == "{" else "]"
            self.pos += 1
            index = 0
            while True:
                self.skip()
                if self.peek() == closing:
                    self.pos += 1
                    break
                if char == "{":
                    member: Key = self.key()
                    self.skip()
                    self.expect(":")
                else:
                    member = index
                    index += 1
                below = path[1:] if path and path[0] == member else None
                span = self.value(below)
                if below is not None:
                    # Every later copy of the key replaces the earlier one,
                    # including a copy without the value: then there is none.
                    found = span
                self.skip()
                if self.peek() == ",":
                    self.pos += 1
                    continue
                self.expect(closing)
                break
        elif char in "\"'":
            self.string()
        else:
            self.literal()
        if path == ():
            return start, self.pos
        return found


def replace_value(text: str, path: tuple[Key, ...], value: object) -> str | None:
    """``text`` with the value at ``path`` replaced by ``value``, or None.

    None when the document cannot be scanned or has no value at ``path``.
    Everything outside the replaced value is kept byte for byte.
    """
    scanner = _Scanner(text)
    try:
        span = scanner.value(path)
        scanner.skip()
        if scanner.pos != len(text):
            return None
    except (ValueError, IndexError):
        return None
    if span is None:
        return None
    start, end = span
    return text[:start] + json.dumps(value, ensure_ascii=False) + text[end:]
