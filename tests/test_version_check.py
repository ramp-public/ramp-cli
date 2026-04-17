"""Tests for passive version-update detection."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ramp_cli.version_check import (
    _COOLDOWN_SECONDS,
    _cache_path,
    _cooldown_expired,
    _read_cache,
    _write_cache,
    emit_update_notice,
    get_update_info,
    get_update_warning,
    parse_version,
)


@pytest.fixture()
def cache_file(isolated_config: Path) -> Path:
    """Return the cache file path inside the isolated config dir."""
    return _cache_path()


class TestParseVersion:
    @pytest.mark.parametrize(
        "version,expected",
        [
            ("0.1.3", (0, 1, 3)),
            ("1.0.0", (1, 0, 0)),
            ("10.20.30", (10, 20, 30)),
        ],
        ids=["patch", "major", "multi-digit"],
    )
    def test_parses(self, version: str, expected: tuple[int, ...]):
        assert parse_version(version) == expected

    def test_comparison(self):
        assert parse_version("0.2.0") > parse_version("0.1.3")
        assert parse_version("0.1.3") == parse_version("0.1.3")
        assert parse_version("0.1.2") < parse_version("0.1.3")


class TestCache:
    def test_read_empty(self, cache_file: Path):
        assert _read_cache() is None

    def test_write_and_read(self, cache_file: Path):
        _write_cache("0.2.0")
        assert _read_cache() == "0.2.0"

    def test_overwrite(self, cache_file: Path):
        _write_cache("0.1.0")
        _write_cache("0.2.0")
        assert _read_cache() == "0.2.0"


class TestCooldown:
    def test_expired_when_no_cache(self, cache_file: Path):
        assert _cooldown_expired()

    def test_not_expired_after_write(self, cache_file: Path):
        _write_cache("0.2.0")
        assert not _cooldown_expired()

    def test_expired_after_cooldown(self, cache_file: Path):
        _write_cache("0.2.0")
        # Backdate the file mtime
        old_time = time.time() - _COOLDOWN_SECONDS - 1
        os.utime(cache_file, (old_time, old_time))
        assert _cooldown_expired()


class TestGetUpdateInfo:
    @patch("ramp_cli.version_check.__version__", "0.1.3")
    def test_update_available(self, cache_file: Path):
        _write_cache("0.2.0")
        info = get_update_info()
        assert info == {"current": "0.1.3", "latest": "0.2.0"}

    @patch("ramp_cli.version_check.__version__", "0.2.0")
    def test_up_to_date(self, cache_file: Path):
        _write_cache("0.2.0")
        assert get_update_info() is None

    @patch("ramp_cli.version_check.__version__", "0.2.0")
    def test_ahead_of_latest(self, cache_file: Path):
        _write_cache("0.1.0")
        assert get_update_info() is None

    def test_no_cache(self, cache_file: Path):
        assert get_update_info() is None


class TestGetUpdateWarning:
    @patch("ramp_cli.version_check.__version__", "0.1.3")
    def test_warning_text(self, cache_file: Path):
        _write_cache("0.2.0")
        warning = get_update_warning()
        assert warning is not None
        assert "v0.1.3" in warning
        assert "v0.2.0" in warning
        assert "ramp update" in warning

    @patch("ramp_cli.version_check.__version__", "0.2.0")
    def test_no_warning_when_current(self, cache_file: Path):
        _write_cache("0.2.0")
        assert get_update_warning() is None


class TestEmitUpdateNotice:
    @patch("ramp_cli.version_check.__version__", "0.1.3")
    def test_human_mode(self, cache_file: Path, capsys):
        _write_cache("0.2.0")
        emit_update_notice(agent_mode=False)
        captured = capsys.readouterr()
        assert "v0.1.3" in captured.err
        assert "v0.2.0" in captured.err
        assert "ramp update" in captured.err

    @patch("ramp_cli.version_check.__version__", "0.1.3")
    def test_agent_mode(self, cache_file: Path, capsys):
        _write_cache("0.2.0")
        emit_update_notice(agent_mode=True)
        captured = capsys.readouterr()
        notice = json.loads(captured.err)
        assert notice["update_available"]["current"] == "0.1.3"
        assert notice["update_available"]["latest"] == "0.2.0"
        assert notice["update_available"]["command"] == "ramp update"

    @patch("ramp_cli.version_check.__version__", "0.2.0")
    def test_no_notice_when_current(self, cache_file: Path, capsys):
        _write_cache("0.2.0")
        emit_update_notice(agent_mode=False)
        captured = capsys.readouterr()
        assert captured.err == ""
