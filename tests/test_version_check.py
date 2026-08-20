"""Tests for passive version-update detection."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import ramp_cli.version_check as vc
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
    suppress_next_update_notice,
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


class TestRecordInstalledVersion:
    @patch("ramp_cli.version_check.__version__", "0.5.0")
    def test_writes_the_running_version(self, isolated_config: Path):
        vc.record_installed_version()
        assert vc.installed_version_path().read_text() == "0.5.0"

    @patch("ramp_cli.version_check.__version__", "0.5.0")
    def test_skips_the_rewrite_when_unchanged(self, isolated_config: Path):
        vc.record_installed_version()
        path = vc.installed_version_path()
        stamp = time.time() - 1000
        os.utime(path, (stamp, stamp))
        before = path.stat().st_mtime_ns

        vc.record_installed_version()

        assert path.stat().st_mtime_ns == before

    def test_rewrites_when_the_version_changes(self, isolated_config: Path):
        with patch("ramp_cli.version_check.__version__", "0.5.0"):
            vc.record_installed_version()
        with patch("ramp_cli.version_check.__version__", "0.6.0"):
            vc.record_installed_version()
        assert vc.installed_version_path().read_text() == "0.6.0"

    @patch("ramp_cli.version_check.__version__", "0.5.0")
    def test_env_override_names_the_file(self, tmp_path: Path, monkeypatch):
        override = tmp_path / "elsewhere" / "installed"
        monkeypatch.setenv("RAMP_INSTALLED_VERSION_FILE", str(override))

        vc.record_installed_version()

        assert override.read_text() == "0.5.0"


class TestServerRecommendedVersion:
    """The Codex cost hook records the server's recommendation; reads are local."""

    def _write_server(self, version: str) -> None:
        path = vc.server_recommended_version_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(version)

    @patch("ramp_cli.version_check.__version__", "0.1.3")
    def test_a_newer_server_recommendation_wins(self, cache_file: Path):
        _write_cache("0.2.0")
        self._write_server("0.3.0\n")
        assert get_update_info() == {"current": "0.1.3", "latest": "0.3.0"}

    @patch("ramp_cli.version_check.__version__", "0.1.3")
    def test_an_older_server_recommendation_defers_to_the_cache(self, cache_file: Path):
        _write_cache("0.2.0")
        self._write_server("0.1.9")
        assert get_update_info() == {"current": "0.1.3", "latest": "0.2.0"}

    @patch("ramp_cli.version_check.__version__", "0.1.3")
    def test_the_server_recommendation_alone_is_enough(self, cache_file: Path):
        self._write_server("0.2.5")
        assert get_update_info() == {"current": "0.1.3", "latest": "0.2.5"}

    @pytest.mark.parametrize("garbage", ["not-a-version", ""])
    @patch("ramp_cli.version_check.__version__", "0.1.3")
    def test_garbage_server_content_is_ignored(self, cache_file: Path, garbage: str):
        _write_cache("0.2.0")
        self._write_server(garbage)
        assert get_update_info() == {"current": "0.1.3", "latest": "0.2.0"}

    @patch("ramp_cli.version_check.__version__", "0.1.3")
    def test_the_opt_out_silences_the_server_recommendation(
        self, cache_file: Path, monkeypatch
    ):
        monkeypatch.setenv("RAMP_NO_UPDATE_CHECK", "1")
        self._write_server("0.3.0\n")
        assert get_update_info() is None

    @patch("ramp_cli.version_check.__version__", "0.2.0")
    def test_a_stale_server_recommendation_stays_quiet(self, cache_file: Path):
        self._write_server("0.2.0")
        assert get_update_info() is None

    @patch("ramp_cli.version_check.__version__", "0.1.3")
    def test_env_override_names_the_file(self, tmp_path: Path, monkeypatch):
        override = tmp_path / "elsewhere" / "recommended"
        override.parent.mkdir(parents=True)
        override.write_text("9.9.9\n")
        monkeypatch.setenv("RAMP_SERVER_RECOMMENDED_VERSION_FILE", str(override))
        assert get_update_info() == {"current": "0.1.3", "latest": "9.9.9"}


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

    @patch("ramp_cli.version_check.__version__", "0.1.3")
    def test_suppress_next_notice_only_suppresses_once(self, cache_file: Path, capsys):
        _write_cache("0.2.0")

        suppress_next_update_notice()
        emit_update_notice(agent_mode=True)
        first = capsys.readouterr()
        assert first.err == ""

        emit_update_notice(agent_mode=True)
        second = capsys.readouterr()
        assert json.loads(second.err)["update_available"]["latest"] == "0.2.0"


class TestUpdateNoticeFile:
    @pytest.fixture()
    def notice_file(self, tmp_path, monkeypatch) -> Path:
        path = tmp_path / "notice" / "update-notice.json"
        monkeypatch.setattr(vc, "update_notice_path", lambda: path)
        return path

    @patch("ramp_cli.version_check.__version__", "0.1.0")
    def test_exists_exactly_while_pending(self, notice_file: Path, cache_file: Path):
        _write_cache("0.2.0")
        vc.sync_update_notice_file()
        payload = json.loads(notice_file.read_text())
        assert payload["latest_version"] == "0.2.0"
        assert payload["current_version"] == "0.1.0"
        assert payload["schema_version"] == 1

        # An upgrade makes the install current; the next reconcile clears it.
        with patch("ramp_cli.version_check.__version__", "0.2.0"):
            vc.sync_update_notice_file()
        assert not notice_file.exists()
