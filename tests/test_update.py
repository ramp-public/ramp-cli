"""Tests for the ramp update command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from ramp_cli.commands.update import _is_homebrew_install
from ramp_cli.main import cli
from ramp_cli.version_check import latest_version, parse_version


def _invoke(args, **kwargs):
    runner = CliRunner()
    return runner.invoke(cli, args, catch_exceptions=False, **kwargs)


class TestUpdateAlreadyCurrent:
    @patch("ramp_cli.commands.update.latest_version", return_value="0.1.3")
    @patch("ramp_cli.commands.update.__version__", "0.1.3")
    def test_up_to_date(self, mock_latest, isolated_config):
        result = _invoke(["update"])
        assert result.exit_code == 0
        assert "Already up to date" in result.output

    @patch("ramp_cli.commands.update.latest_version", return_value="0.1.2")
    @patch("ramp_cli.commands.update.__version__", "0.1.3")
    def test_ahead_of_latest(self, mock_latest, isolated_config):
        result = _invoke(["update"])
        assert result.exit_code == 0
        assert "Already up to date" in result.output


class TestUpdateAvailable:
    @patch("ramp_cli.commands.update._is_homebrew_install", return_value=False)
    @patch("ramp_cli.commands.update.subprocess.run")
    @patch("ramp_cli.commands.update.shutil.which", return_value="/usr/bin/curl")
    @patch("ramp_cli.commands.update.latest_version", return_value="0.2.0")
    @patch("ramp_cli.commands.update.__version__", "0.1.3")
    def test_runs_install_script(
        self, mock_latest, mock_which, mock_run, mock_brew, isolated_config
    ):
        mock_run.return_value = MagicMock(returncode=0)
        result = _invoke(["update"])
        assert result.exit_code == 0
        assert "v0.1.3" in result.output
        assert "v0.2.0" in result.output
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "agents.ramp.com/install.sh" in cmd[-1]

    @patch("ramp_cli.commands.update._is_homebrew_install", return_value=False)
    @patch("ramp_cli.commands.update.subprocess.run")
    @patch("ramp_cli.commands.update.shutil.which", return_value="/usr/bin/curl")
    @patch("ramp_cli.commands.update.latest_version", return_value="0.2.0")
    @patch("ramp_cli.commands.update.__version__", "0.1.3")
    def test_install_failure(
        self, mock_latest, mock_which, mock_run, mock_brew, isolated_config
    ):
        mock_run.return_value = MagicMock(returncode=1)
        result = _invoke(["update"])
        assert result.exit_code != 0
        assert "Update failed" in result.output


class TestUpdateHomebrew:
    @patch("ramp_cli.commands.update.subprocess.run")
    @patch("ramp_cli.commands.update._is_homebrew_install", return_value=True)
    @patch("ramp_cli.commands.update.latest_version", return_value="0.2.0")
    @patch("ramp_cli.commands.update.__version__", "0.1.3")
    def test_homebrew_install_routes_to_brew_upgrade(
        self, mock_latest, mock_brew, mock_run, isolated_config
    ):
        result = _invoke(["update"])
        assert result.exit_code == 0
        assert "Homebrew" in result.output
        assert "brew upgrade ramp-cli" in result.output
        mock_run.assert_not_called()

    @patch("ramp_cli.commands.update.sys")
    def test_detects_apple_silicon_homebrew_path(self, mock_sys):
        mock_sys.executable = (
            "/opt/homebrew/Cellar/ramp-cli/0.1.7/libexec/ramp-darwin-arm64"
        )
        assert _is_homebrew_install()

    @patch("ramp_cli.commands.update.sys")
    def test_detects_intel_homebrew_path(self, mock_sys):
        mock_sys.executable = (
            "/usr/local/Cellar/ramp-cli/0.1.7/libexec/ramp-darwin-amd64"
        )
        assert _is_homebrew_install()

    @patch("ramp_cli.commands.update.sys")
    def test_ignores_install_sh_path(self, mock_sys):
        mock_sys.executable = "/Users/zfield/.local/lib/ramp-cli/ramp-darwin-arm64"
        assert not _is_homebrew_install()

    @patch("ramp_cli.commands.update.sys")
    def test_ignores_brew_python_for_source_install(self, mock_sys):
        # uv / pip / source install on a Homebrew-managed Python: sys.executable
        # is the interpreter, not the ramp binary, so it lives under
        # Cellar/python@X.Y/... — we must NOT route to `brew upgrade ramp-cli`.
        mock_sys.executable = (
            "/opt/homebrew/Cellar/python@3.12/3.12.7/Frameworks/"
            "Python.framework/Versions/3.12/bin/python3.12"
        )
        assert not _is_homebrew_install()

    @patch("ramp_cli.commands.update.sys")
    def test_ignores_venv_python(self, mock_sys):
        mock_sys.executable = "/Users/zfield/code/ramp-cli/.venv/bin/python3"
        assert not _is_homebrew_install()


class TestUpdateNetworkError:
    @patch("ramp_cli.commands.update.latest_version", return_value=None)
    def test_version_check_fails(self, mock_latest, isolated_config):
        result = _invoke(["update"])
        assert result.exit_code != 0
        assert "Could not check for updates" in result.output


class TestUpdateNoCurl:
    @patch("ramp_cli.commands.update.shutil.which", return_value=None)
    @patch("ramp_cli.commands.update.latest_version", return_value="0.2.0")
    @patch("ramp_cli.commands.update.__version__", "0.1.3")
    def test_no_curl_available(self, mock_latest, mock_which, isolated_config):
        result = _invoke(["update"])
        assert result.exit_code != 0
        assert "curl is required" in result.output


class TestLatestVersionLookup:
    @patch("ramp_cli.version_check.httpx.Client")
    def test_github_api_success(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"tag_name": "v0.3.0"}
        mock_http = MagicMock()
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_http.get.return_value = mock_resp
        mock_client_cls.return_value = mock_http

        assert latest_version() == "0.3.0"

    @patch("ramp_cli.version_check.httpx.Client")
    def test_network_failure(self, mock_client_cls):
        mock_http = MagicMock()
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_http.get.side_effect = Exception("network error")
        mock_client_cls.return_value = mock_http

        assert latest_version() is None


class TestParseVersion:
    def test_simple(self):
        assert parse_version("0.1.3") == (0, 1, 3)
        assert parse_version("1.0.0") == (1, 0, 0)
        assert parse_version("0.2.0") > parse_version("0.1.3")
        assert parse_version("0.1.3") == parse_version("0.1.3")
        assert parse_version("0.1.2") < parse_version("0.1.3")
