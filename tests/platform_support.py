"""Helpers for tests that run on both POSIX and Windows."""

from __future__ import annotations

import os
import stat
import sys

import pytest

WINDOWS = sys.platform == "win32"


def assert_mode(path: str | os.PathLike[str], expected: int) -> None:
    """Assert a file's permission bits, except on Windows.

    Windows has no POSIX permission bits: os.chmod only toggles the read-only
    flag, so st_mode always reports 0o666 or 0o444 there.
    """
    if WINDOWS:
        return
    actual = stat.S_IMODE(os.stat(path).st_mode)
    assert actual == expected, f"{path}: mode {oct(actual)} != {oct(expected)}"


def skip_on_windows(reason: str) -> None:
    """Skip from inside a fixture; collection-time code uses ``posix_only``."""
    if WINDOWS:
        pytest.skip(f"POSIX only: {reason}")
