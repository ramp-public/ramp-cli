"""A machine where Router is set up only through Cursor.

Cursor writes no receipt a refresh could read, so nothing here may assume at
least one CLIENT_NAMES setup exists: the agent still has to install, run its
listener (which uploads Cursor's repository tags), survive refreshes and
updates, and go away only with the last setup.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, Mock, patch

import pytest
from click.testing import CliRunner

from ramp_cli.commands import cursor, router_repo
from ramp_cli.commands import router as router_module
from ramp_cli.commands import router_schedule as schedule
from ramp_cli.main import cli
from tests.test_router_agent import _executable, _plist, launchd  # noqa: F401
from tests.test_router_cursor import _mock_cursor_setup
from tests.test_router_systemd import systemd  # noqa: F401

# Captured before the conftest fixture turns these into no-ops.
_REAL_INSTALL = schedule.install
_REAL_UNINSTALL = schedule.uninstall
_REAL_UPDATE_AGENT = schedule.update_agent
_REAL_INSTALL_SERVICE = router_repo.install_service
_REAL_UNINSTALL_SERVICE = router_repo.uninstall_service

CURSOR_KEY = "sk-cursor-only"


def _bind(base_url: str = "https://api.router.com/v1") -> None:
    router_repo.bind_cursor(base_url, CURSOR_KEY, cursor.user_dir())
    assert router_repo.cursor_bound()


def _listening() -> bool:
    return schedule.agent_installed() and "--port" in (
        schedule._installed_agent_arguments()
    )


def _real_service(monkeypatch) -> None:
    """The real service lifecycle; only the OS service manager is faked."""
    monkeypatch.setattr(schedule, "install", _REAL_INSTALL)
    monkeypatch.setattr(schedule, "uninstall", _REAL_UNINSTALL)
    monkeypatch.setattr(schedule, "update_agent", _REAL_UPDATE_AGENT)
    monkeypatch.setattr(router_repo, "install_service", _REAL_INSTALL_SERVICE)
    monkeypatch.setattr(router_repo, "uninstall_service", _REAL_UNINSTALL_SERVICE)
    monkeypatch.setattr(router_repo, "_port_taken", lambda _port: False)
    monkeypatch.setattr(router_repo, "START_WAIT_SECONDS", 0.5)
    # The listener answers exactly while the installed agent runs it.
    monkeypatch.setattr(
        router_repo,
        "health",
        lambda *_a: (
            {"service": router_repo.SERVICE_NAME, "local_auth": True}
            if _listening()
            else None
        ),
    )


@pytest.fixture(params=["darwin", "linux"])
def service(request, monkeypatch, tmp_path):
    """macOS launchd or Linux systemd, each with the agent beside `ramp`."""
    if request.param == "darwin":
        request.getfixturevalue("launchd")
    else:
        request.getfixturevalue("systemd")
        _executable(tmp_path / schedule.AGENT_BINARY)
    _real_service(monkeypatch)
    return request.param


def _status() -> dict:
    result = CliRunner().invoke(cli, ["--agent", "router", "agent", "status"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"][0]


def test_cursor_only_lifecycle_enable_status_disable_unconfigure(service):
    _bind()

    result = CliRunner().invoke(cli, ["--human", "router", "agent", "enable"])
    assert result.exit_code == 0, result.output
    assert "Configure Router first" not in result.output
    # Cursor's calls never pass through a local endpoint.
    assert "Restart open coding-agent sessions" not in result.output
    assert schedule.background_enabled()
    assert router_repo.enabled()
    status = _status()
    assert status["service"] == "agent"
    assert status["state"] == "running"
    assert status["repository_tagging"]["enabled"] is True
    assert status["repository_tagging"]["healthy"] is True
    arguments = schedule._installed_agent_arguments()
    # --settings is what starts the agent's Cursor uploads.
    assert "--port" in arguments and "--settings" in arguments

    result = CliRunner().invoke(cli, ["--human", "router", "agent", "disable"])
    assert result.exit_code == 0, result.output
    assert "Restart open coding-agent sessions" not in result.output
    assert not schedule.agent_installed()
    assert not router_repo.enabled()
    assert _status()["state"] == "not_installed"

    # Enabling again works the same way, and removing Cursor, the last setup,
    # removes the agent with it.
    result = CliRunner().invoke(cli, ["--agent", "router", "agent", "enable"])
    assert result.exit_code == 0, result.output
    assert _status()["state"] == "running"
    result = CliRunner().invoke(cli, ["--human", "router", "unconfigure", "cursor"])
    assert result.exit_code == 0, result.output
    assert not router_repo.cursor_bound()
    assert not schedule.agent_installed()
    # Nothing is stranded, so status asks for no repair: following either
    # "enable again" line would fail without a setup.
    result = CliRunner().invoke(cli, ["--human", "router", "agent", "status"])
    assert result.exit_code == 0, result.output
    assert result.output == (
        "Background sync: enabled.\n"
        "Service: none (not_installed).\n"
        "Repository tagging: enabled; the agent starts again with the next "
        "Router setup.\n"
    )


def test_status_still_flags_a_stopped_service_while_a_setup_remains(monkeypatch):
    """The "not running" line stays wherever enabling again can work."""
    marker = schedule._enabled_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    router_repo._save_settings({"enabled": True})
    monkeypatch.setattr(router_repo, "health", lambda *_a: None)
    _bind()
    result = CliRunner().invoke(cli, ["--human", "router", "agent", "status"])
    assert result.exit_code == 0, result.output
    assert "Service: none (not_installed)." in result.output
    assert "nothing answers on port" in result.output
    saved = "The preference is saved, but the service is not running."
    assert saved in result.output
    assert "starts again with the next Router setup" not in result.output


def test_rerunning_cursor_setup_brings_back_a_removed_agent(service, monkeypatch):
    _mock_cursor_setup(monkeypatch)
    _bind()
    assert CliRunner().invoke(cli, ["router", "agent", "enable"]).exit_code == 0
    assert CliRunner().invoke(cli, ["router", "unconfigure", "cursor"]).exit_code == 0
    assert not schedule.agent_installed()
    # Both preferences outlive the removed setup, so setting Cursor up again
    # restarts what serves it, as configure does for every other client.
    result = CliRunner().invoke(cli, ["router", "cursor", "--api-key", CURSOR_KEY])
    assert result.exit_code == 0, result.output
    assert _listening()


def test_configure_cursor_enable_agent_works_without_another_client(
    service, monkeypatch
):
    _mock_cursor_setup(monkeypatch)
    monkeypatch.setattr(router_module, "_can_prompt", lambda *_a: True)
    monkeypatch.setattr(router_module, "_write_cursor_settings", lambda *a, **k: None)
    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "cursor",
            "--enable-agent",
            "--api-key",
            CURSOR_KEY,
        ],
    )
    assert result.exit_code == 0, result.output
    assert router_repo.cursor_bound()
    assert router_repo.enabled()
    assert schedule.background_enabled()
    assert _status()["state"] == "running"
    # Enabling is part of this command, so no hint to run it.
    assert "ramp router agent enable" not in result.output


def test_enable_records_the_custom_deployment_cursor_was_set_up_with(
    launchd,  # noqa: F811
    monkeypatch,
):
    _real_service(monkeypatch)
    _bind("https://router.example.com:8443/v1")
    # A later shell no longer carries RAMP_ROUTER_BASE_URL.
    result = CliRunner().invoke(cli, ["--agent", "router", "agent", "enable"])
    assert result.exit_code == 0, result.output
    assert "router.example.com" in router_repo.load_settings()["hosts"]
    info = router_repo.cursor_tagging("https://router.example.com:8443/v1", False)
    assert info["state"] == router_repo.CURSOR_BOUND
    assert info["uploading"] is True


@pytest.mark.parametrize("tagging", [False, True])
def test_switching_cursor_to_a_custom_deployment_records_it_only_while_on(tagging):
    router_repo._save_settings({"enabled": tagging})
    _bind("https://router.example.com/v1")
    hosts = router_repo.load_settings().get("hosts")
    assert hosts == (["router.example.com"] if tagging else None)
    # Router's own hosts are never written down.
    _bind("https://qa-api.router.com/v1")
    assert router_repo.load_settings().get("hosts") == hosts


def test_enable_without_any_setup_still_asks_for_one(monkeypatch):
    monkeypatch.setattr(router_module, "_scheduled_sync_connections", lambda: [])
    result = CliRunner().invoke(cli, ["--human", "router", "agent", "enable"])
    assert result.exit_code == 2
    assert "Configure Router first" in result.output


def test_a_binding_whose_key_is_gone_is_not_a_setup(monkeypatch):
    monkeypatch.setattr(router_module, "_scheduled_sync_connections", lambda: [])
    _bind()
    for stored in (router_repo.config_dir() / "router-agent-credentials").iterdir():
        stored.unlink()
    assert not router_repo.cursor_bound()
    result = CliRunner().invoke(cli, ["--human", "router", "agent", "enable"])
    assert result.exit_code == 2
    assert "Configure Router first" in result.output
    assert not schedule.background_enabled()


def test_cursor_only_enable_needs_the_native_agent(monkeypatch):
    monkeypatch.setattr(router_module, "_scheduled_sync_connections", lambda: [])
    monkeypatch.setattr(router_repo, "service_supported", lambda: False)
    _bind()
    result = CliRunner().invoke(cli, ["--human", "router", "agent", "enable"])
    assert result.exit_code == 1
    assert "needs the Ramp Router agent" in result.output
    # A plain timer would sync nothing, so no preference is saved.
    assert not schedule.background_enabled()


def test_scheduled_sync_rerenders_the_agent_for_an_updated_binary(
    launchd,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    _real_service(monkeypatch)
    _bind()
    assert CliRunner().invoke(cli, ["router", "agent", "enable"]).exit_code == 0
    agent = tmp_path / "lib/ramp-cli" / schedule.AGENT_BINARY
    agent.write_text("#!/bin/sh\n# a newer build\n")
    launchd.calls.clear()

    result = CliRunner().invoke(cli, ["router", "sync", "--scheduled"])
    assert result.exit_code == 0, result.output
    arguments = _plist(tmp_path)["ProgramArguments"]
    assert arguments[arguments.index("--build") + 1] == schedule.agent_build(agent)
    assert ("bootout", schedule.AGENT_LABEL) in launchd.verbs()
    assert ("kickstart", schedule.AGENT_LABEL) in launchd.verbs()
    assert "--port" in arguments
    # Nothing to check: the agent stays idle until its daily recheck.
    manifest = json.loads(schedule.manifest_path().read_text())
    assert manifest["connections"] == []


@pytest.mark.parametrize("bound", [False, True])
def test_scheduled_sync_reconciles_only_for_a_cursor_binding(monkeypatch, bound):
    marker = schedule._enabled_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    install = Mock()
    monkeypatch.setattr(schedule, "install", install)
    if bound:
        _bind()
    result = CliRunner().invoke(cli, ["router", "sync", "--scheduled"])
    assert result.exit_code == 0, result.output
    assert install.call_count == (1 if bound else 0)
    # The agent and its opt-in are never removed for having nothing to sync.
    assert schedule.background_enabled()


def test_refresh_keeps_the_agent_for_cursor_only(monkeypatch):
    install = Mock()
    monkeypatch.setattr(schedule, "install", install)
    result = CliRunner().invoke(cli, ["--human", "router", "refresh"])
    assert "not configured in any coding agent" in result.output
    install.assert_not_called()

    _bind()
    result = CliRunner().invoke(cli, ["--human", "router", "refresh"])
    assert result.exit_code == 0, result.output
    assert "Cursor keeps its Router settings in Cursor" in result.output
    install.assert_called_once_with()
    result = CliRunner().invoke(cli, ["--agent", "router", "refresh"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"][0] == {"clients": []}


@pytest.mark.parametrize("homebrew", [False, True])
def test_update_refreshes_a_cursor_only_agent(monkeypatch, homebrew):
    _bind()
    monkeypatch.setattr(schedule, "agent_installed", lambda: True)
    with (
        patch("ramp_cli.commands.update.__version__", "0.1.3"),
        patch("ramp_cli.commands.update.latest_version", return_value="0.2.0"),
        patch("ramp_cli.commands.update.shutil.which") as which,
        patch("ramp_cli.commands.update.subprocess.run") as run,
        patch("ramp_cli.commands.update.suppress_next_update_notice"),
        patch("ramp_cli.commands.update._is_homebrew_install", return_value=homebrew),
        patch("ramp_cli.commands.update._offer_optional_skills_update") as skills,
    ):
        which.side_effect = lambda command: {
            "curl": "/usr/bin/curl",
            "ramp": "/usr/local/bin/ramp",
        }[command]
        run.side_effect = [MagicMock(returncode=0), MagicMock(returncode=0)]
        result = CliRunner().invoke(
            cli, ["update"], env={"RAMP_INSTALL_DIR": "/usr/local/bin"}
        )
    assert result.exit_code == 0, result.output
    # No CLIENT_NAMES agent to name.
    assert "configured agents only" not in result.output
    if homebrew:
        assert "brew upgrade ramp-cli && ramp router refresh" in result.output
        run.assert_not_called()
    else:
        assert run.call_args_list[1].args[0] == [
            "/usr/local/bin/ramp",
            "router",
            "refresh",
        ]
        skills.assert_not_called()


def test_update_without_a_running_cursor_agent_is_unchanged(monkeypatch):
    _bind()
    monkeypatch.setattr(schedule, "agent_installed", lambda: False)
    with (
        patch("ramp_cli.commands.update.__version__", "0.1.3"),
        patch("ramp_cli.commands.update.latest_version", return_value="0.2.0"),
        patch("ramp_cli.commands.update._is_homebrew_install", return_value=True),
    ):
        result = CliRunner().invoke(cli, ["update"])
    assert result.exit_code == 0, result.output
    assert "&& ramp router refresh" not in result.output


def _receipt(client: str) -> None:
    path = router_module._client_config_path(client).parent / "ramp-router-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")


@pytest.mark.parametrize("other", [None, "codex"])
def test_unconfigure_cursor_removes_the_agent_only_with_the_last_setup(
    monkeypatch, other
):
    removed = Mock()
    monkeypatch.setattr(schedule, "uninstall", removed)
    _bind()
    if other:
        _receipt(other)
    result = CliRunner().invoke(cli, ["--human", "router", "unconfigure", "cursor"])
    assert result.exit_code == 0, result.output
    assert not router_repo.cursor_bound()
    assert removed.call_count == (0 if other else 1)


def test_removing_the_last_other_setup_drops_its_key_from_the_agent(monkeypatch):
    monkeypatch.setattr(router_module.claude_cowork, "unconfigure", lambda: None)
    removed = Mock()
    monkeypatch.setattr(schedule, "uninstall", removed)
    _bind()
    schedule.write_manifest({"connections": [{"headers": {"Authorization": "x"}}]})
    result = CliRunner().invoke(cli, ["--human", "router", "unconfigure", "cowork"])
    assert result.exit_code == 0, result.output
    # The agent stays for Cursor, but stops checking the removed key.
    removed.assert_not_called()
    assert not schedule.manifest_path().exists()


def test_removing_one_of_several_setups_leaves_the_manifest(monkeypatch):
    monkeypatch.setattr(router_module.claude_cowork, "unconfigure", lambda: None)
    _bind()
    _receipt("codex")
    schedule.write_manifest({"connections": []})
    result = CliRunner().invoke(cli, ["--human", "router", "unconfigure", "cowork"])
    assert result.exit_code == 0, result.output
    assert schedule.manifest_path().exists()


@pytest.mark.parametrize(
    "command", [["router", "cursor"], ["router", "configure", "cursor"]]
)
def test_cursor_setup_points_at_tagging_while_it_is_off(monkeypatch, command):
    _mock_cursor_setup(monkeypatch)
    monkeypatch.setattr(router_repo, "service_supported", lambda: True)
    human = CliRunner().invoke(cli, ["--human", *command, "--api-key", CURSOR_KEY])
    assert human.exit_code == 0, human.output
    hint = [line for line in human.output.splitlines() if "agent enable" in line]
    assert hint == [
        "Repository tagging is off; run 'ramp router agent enable' to tag Cursor "
        "chats by repository ('ramp router configure cursor --enable-agent' does both)."
    ]
    # Machine output stays one parseable envelope.
    agent = CliRunner().invoke(cli, ["--agent", *command, "--api-key", CURSOR_KEY])
    assert agent.exit_code == 0, agent.output
    assert json.loads(agent.output)["data"][0]["client"] == "cursor"


@pytest.mark.parametrize("reason", ["enabled", "unsupported", "opted_out"])
def test_cursor_setup_hint_only_when_enabling_would_help(monkeypatch, reason):
    _mock_cursor_setup(monkeypatch)
    monkeypatch.setattr(
        router_repo, "service_supported", lambda: reason != "unsupported"
    )
    if reason == "enabled":
        router_repo._save_settings({"enabled": True})
    if reason == "opted_out":
        monkeypatch.setenv("RAMP_NO_SESSION_SYNC", "1")
    result = CliRunner().invoke(
        cli, ["--human", "router", "cursor", "--api-key", CURSOR_KEY]
    )
    assert result.exit_code == 0, result.output
    assert "agent enable" not in result.output


def test_cursor_alongside_other_setups_keeps_the_existing_enable(monkeypatch):
    """With a refreshable setup too, enable behaves exactly as before."""
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [object()]
    )
    monkeypatch.setattr(router_repo, "service_supported", lambda: True)
    enabled = Mock()
    monkeypatch.setattr(router_repo, "enable", enabled)
    _bind()
    result = CliRunner().invoke(cli, ["--human", "router", "agent", "enable"])
    assert result.exit_code == 0, result.output
    enabled.assert_called_once()
    assert "Restart open coding-agent sessions" in result.output


def test_enable_agent_still_rejects_harnesses_it_does_not_cover(monkeypatch):
    monkeypatch.setattr(router_module, "_can_prompt", lambda *_a: True)
    monkeypatch.setattr(router_repo, "service_supported", lambda: True)
    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "hermes", "--enable-agent"]
    )
    assert result.exit_code == 2
    assert "--enable-agent supports only" in result.output
    assert "cursor" in result.output
