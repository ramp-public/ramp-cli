"""Tests for the ramp update command."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from ramp_cli.commands.update import _is_homebrew_install
from ramp_cli.main import cli
from ramp_cli.version_check import latest_version, parse_version


def _invoke(args, **kwargs):
    runner = CliRunner()
    return runner.invoke(cli, args, catch_exceptions=False, **kwargs)


_DEFAULT_MANAGED_TARGET = Path("/managed/skills")


@contextmanager
def _interactive_update(targets=(_DEFAULT_MANAGED_TARGET,)):
    with (
        patch("ramp_cli.commands.update.__version__", "0.1.3"),
        patch("ramp_cli.commands.update.latest_version", return_value="0.2.0"),
        patch("ramp_cli.commands.update.shutil.which") as mock_which,
        patch("ramp_cli.commands.update.subprocess.run") as mock_run,
        patch("ramp_cli.commands.update.suppress_next_update_notice"),
        patch("ramp_cli.commands.update._is_homebrew_install", return_value=False),
        patch("ramp_cli.commands.update._is_interactive", return_value=True),
        patch(
            "ramp_cli.commands.update.managed_skill_targets",
            return_value=list(targets),
        ),
    ):
        mock_which.side_effect = lambda command: {
            "curl": "/usr/bin/curl",
            "ramp": "/usr/local/bin/ramp",
        }[command]
        yield mock_run


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
    @patch("ramp_cli.commands.update.suppress_next_update_notice")
    @patch("ramp_cli.commands.update.subprocess.run")
    @patch("ramp_cli.commands.update.shutil.which", return_value="/usr/bin/curl")
    @patch("ramp_cli.commands.update.latest_version", return_value="0.2.0")
    @patch("ramp_cli.commands.update.__version__", "0.1.3")
    def test_runs_install_script(
        self,
        mock_latest,
        mock_which,
        mock_run,
        mock_notice,
        mock_brew,
        isolated_config,
    ):
        mock_which.side_effect = lambda command: {
            "curl": "/usr/bin/curl",
            "ramp": "/usr/local/bin/ramp",
        }[command]
        mock_run.return_value = MagicMock(returncode=0)
        result = _invoke(["update"], env={"RAMP_INSTALL_DIR": "/usr/local/bin"})
        assert result.exit_code == 0
        assert "v0.1.3" in result.output
        assert "v0.2.0" in result.output
        assert "installed binary passed checksum verification" in result.output
        mock_run.assert_called_once()
        cmd = mock_run.call_args_list[0].args[0]
        assert "agents.ramp.com/install.sh" in cmd[-1]
        assert 'sh "$tmp" --no-skills' in cmd[-1]
        assert "agent skills" not in result.output
        mock_notice.assert_called_once_with()

    def test_declining_skills_update_does_not_install_skills(self, isolated_config):
        with _interactive_update() as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = _invoke(
                ["--human", "update"],
                input="n\n",
                env={"RAMP_INSTALL_DIR": "/usr/local/bin"},
            )

        assert result.exit_code == 0
        assert str(_DEFAULT_MANAGED_TARGET) in result.output
        assert "latest version? [y/N]: n" in result.output
        assert "Ramp skills were not updated." in result.output
        mock_run.assert_called_once()

    def test_no_managed_targets_skips_skills_update(self, isolated_config):
        with _interactive_update(targets=()) as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = _invoke(
                ["--human", "update"], env={"RAMP_INSTALL_DIR": "/usr/local/bin"}
            )

        assert result.exit_code == 0
        assert "Update these skills" not in result.output
        mock_run.assert_called_once()

    def test_no_input_skips_skills_update(self, isolated_config):
        with _interactive_update() as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = _invoke(
                ["--human", "--no-input", "update"],
                env={"RAMP_INSTALL_DIR": "/usr/local/bin"},
            )

        assert result.exit_code == 0
        assert "Ramp-managed skills are installed in" not in result.output
        mock_run.assert_called_once()

    def test_json_output_skips_skills_update(self, isolated_config):
        with _interactive_update() as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = _invoke(
                ["--output", "json", "update"],
                env={"RAMP_INSTALL_DIR": "/usr/local/bin"},
            )

        assert result.exit_code == 0
        assert "Ramp-managed skills are installed in" not in result.output
        mock_run.assert_called_once()

    def test_accepting_skills_update_installs_latest_catalog(self, isolated_config):
        targets = (Path("/managed/claude-skills"), Path("/custom/cursor-skills"))
        with _interactive_update(targets) as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = _invoke(
                ["--human", "update"],
                input="y\n",
                env={"RAMP_INSTALL_DIR": "/usr/local/bin"},
            )

        assert result.exit_code == 0
        assert all(str(target) in result.output for target in targets)
        assert "latest version? [y/N]: y" in result.output
        assert [call.args[0] for call in mock_run.call_args_list[1:]] == [
            ["/usr/local/bin/ramp", "skills", "update"],
            [
                "/usr/local/bin/ramp",
                "skills",
                "install",
                "--all",
                "--target",
                "/managed/claude-skills",
            ],
            [
                "/usr/local/bin/ramp",
                "skills",
                "install",
                "--all",
                "--target",
                "/custom/cursor-skills",
            ],
        ]

    def test_skills_catalog_failure_does_not_touch_skill_directories(
        self, isolated_config
    ):
        with _interactive_update() as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=1),
            ]
            result = _invoke(
                ["--human", "update"],
                input="y\n",
                env={"RAMP_INSTALL_DIR": "/usr/local/bin"},
            )

        assert result.exit_code == 0
        assert "optional skills catalog could not be updated" in result.output
        assert [call.args[0] for call in mock_run.call_args_list[1:]] == [
            ["/usr/local/bin/ramp", "skills", "update"]
        ]

    @patch("ramp_cli.commands.update._is_homebrew_install", return_value=False)
    @patch("ramp_cli.commands.update.suppress_next_update_notice")
    @patch("ramp_cli.commands.update.subprocess.run")
    @patch("ramp_cli.commands.update.shutil.which")
    @patch("ramp_cli.commands.update.latest_version", return_value="0.2.0")
    @patch("ramp_cli.commands.update.__version__", "0.1.8")
    @patch("ramp_cli.commands.update.sys.argv", ["/repo/.venv/bin/ramp"])
    def test_warns_when_updated_binary_is_shadowed_on_path(
        self,
        mock_latest,
        mock_which,
        mock_run,
        mock_notice,
        mock_brew,
        isolated_config,
    ):
        mock_which.side_effect = lambda command: {
            "curl": "/usr/bin/curl",
            "ramp": "/repo/.venv/bin/ramp",
        }[command]
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout="ramp-cli, version 0.2.0\n"),
        ]

        result = _invoke(["update"], env={"RAMP_INSTALL_DIR": "/home/user/.local/bin"})

        assert result.exit_code == 0
        assert "WARNING: The updated ramp CLI is shadowed on PATH." in result.output
        assert "Resolved ramp:  /repo/.venv/bin/ramp (v0.1.8)" in result.output
        assert "Installed ramp: /home/user/.local/bin/ramp (v0.2.0)" in result.output
        assert "/home/user/.local/bin/ramp --version" in result.output
        assert "export PATH=/home/user/.local/bin:$PATH && hash -r" in result.output
        assert ["/repo/.venv/bin/ramp", "--version"] not in [
            call.args[0] for call in mock_run.call_args_list
        ]

    @patch(
        "ramp_cli.commands.update.configured_router_clients",
        return_value=("opencode",),
    )
    @patch("ramp_cli.commands.update._is_homebrew_install", return_value=False)
    @patch("ramp_cli.commands.update.suppress_next_update_notice")
    @patch("ramp_cli.commands.update.subprocess.run")
    @patch("ramp_cli.commands.update.shutil.which", return_value="/usr/bin/curl")
    @patch("ramp_cli.commands.update.latest_version", return_value="0.2.0")
    @patch("ramp_cli.commands.update.__version__", "0.1.3")
    def test_router_update_refreshes_only_configured_agents(
        self,
        mock_latest,
        mock_which,
        mock_run,
        mock_notice,
        mock_brew,
        mock_configured,
        isolated_config,
    ):
        mock_which.side_effect = lambda command: {
            "curl": "/usr/bin/curl",
            "ramp": "/usr/local/bin/ramp",
        }[command]
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
        ]

        result = _invoke(["update"], env={"RAMP_INSTALL_DIR": "/usr/local/bin"})

        assert result.exit_code == 0
        assert "configured agents only: OpenCode" in result.output
        assert mock_run.call_count == 2
        install_command = mock_run.call_args_list[0].args[0][-1]
        assert 'sh "$tmp" --no-skills' in install_command
        assert "--router" not in install_command
        assert mock_run.call_args_list[1].args[0] == [
            "/usr/local/bin/ramp",
            "router",
            "refresh",
        ]
        mock_notice.assert_called_once_with()

    @patch(
        "ramp_cli.commands.update.configured_router_clients",
        return_value=("opencode",),
    )
    @patch("ramp_cli.commands.update._is_homebrew_install", return_value=False)
    @patch("ramp_cli.commands.update.suppress_next_update_notice")
    @patch("ramp_cli.commands.update.subprocess.run")
    @patch("ramp_cli.commands.update.shutil.which")
    @patch("ramp_cli.commands.update.latest_version", return_value="0.2.0")
    @patch("ramp_cli.commands.update.__version__", "0.1.3")
    @patch("ramp_cli.commands.update.sys.platform", "win32")
    def test_router_refresh_uses_sh_for_windows_wrapper(
        self,
        mock_latest,
        mock_which,
        mock_run,
        mock_notice,
        mock_brew,
        mock_configured,
        isolated_config,
    ):
        mock_which.side_effect = lambda command: {
            "curl": "/usr/bin/curl",
            "ramp": "C:/Users/user/.local/bin/ramp",
        }[command]
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
        ]

        result = _invoke(
            ["update"], env={"RAMP_INSTALL_DIR": "C:/Users/user/.local/bin"}
        )

        assert result.exit_code == 0
        assert mock_run.call_args_list[1].args[0] == [
            "sh",
            "C:/Users/user/.local/bin/ramp",
            "router",
            "refresh",
        ]

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

    @patch(
        "ramp_cli.commands.update.configured_router_clients",
        return_value=("pi",),
    )
    @patch("ramp_cli.commands.update._is_homebrew_install", return_value=True)
    @patch("ramp_cli.commands.update.latest_version", return_value="0.2.0")
    @patch("ramp_cli.commands.update.__version__", "0.1.3")
    def test_homebrew_update_refreshes_only_existing_router_configurations(
        self, mock_latest, mock_brew, mock_configured, isolated_config
    ):
        result = _invoke(["update"])

        assert result.exit_code == 0
        assert "brew upgrade ramp-cli && ramp router refresh" in result.output

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
