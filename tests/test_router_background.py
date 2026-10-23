"""Explicit, persistent controls for the opt-in OS background service."""

from __future__ import annotations

import json
from unittest.mock import Mock

import click
import pytest
from click.testing import CliRunner

from ramp_cli.commands import router as router_module
from ramp_cli.commands import router_repo
from ramp_cli.commands import router_schedule as schedule
from ramp_cli.main import cli

_REAL_INSTALL = schedule.install


@pytest.fixture(autouse=True)
def isolated_service(monkeypatch, tmp_path, isolated_config):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("RAMP_NO_SESSION_SYNC", raising=False)
    monkeypatch.delenv(schedule.SCHEDULED_ENV, raising=False)
    monkeypatch.setattr(schedule, "_run", Mock(return_value=False))
    monkeypatch.setattr(schedule, "_launchd_disabled", lambda _label: False)


def invoke(*args):
    return CliRunner().invoke(cli, ["--human", "router", "agent", *args])


def test_status_defaults_to_off_without_writing_a_preference():
    result = invoke("status")
    assert result.exit_code == 0, result.output
    assert "Background sync: disabled" in result.output
    assert "none (not_installed)" in result.output
    assert not schedule._enabled_marker().exists()
    assert not schedule.opted_out()


def test_enable_requires_a_configured_connection(monkeypatch):
    monkeypatch.setattr(router_module, "_scheduled_sync_connections", lambda: [])
    result = invoke("enable")
    assert result.exit_code == 2
    assert "Configure Router first" in result.output
    assert not schedule.background_enabled()


def test_enable_rejects_environment_opt_out(monkeypatch):
    monkeypatch.setenv("RAMP_NO_SESSION_SYNC", "1")
    result = invoke("enable")
    assert result.exit_code == 2
    assert "Unset RAMP_NO_SESSION_SYNC" in result.output
    assert not schedule.background_enabled()


def test_enable_rejects_windows_without_saving_preference(monkeypatch):
    monkeypatch.setattr(router_module.sys, "platform", "win32")
    result = invoke("enable")
    assert result.exit_code == 2
    assert "requires macOS or Linux" in result.output
    assert not schedule.background_enabled()


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_choice_survives_refresh_and_fresh_shell(monkeypatch, tmp_path, platform):
    monkeypatch.setattr(schedule.sys, "platform", platform)
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [object()]
    )
    install_service = Mock()
    monkeypatch.setattr(schedule, "_install_service", install_service)
    monkeypatch.setattr(schedule, "install", _REAL_INSTALL)

    result = invoke("enable")
    assert result.exit_code == 0, result.output
    assert "preference is saved, but the service is not running" in result.output
    assert schedule.background_enabled()
    install_service.assert_called_once()

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "fresh-shell"))
    _REAL_INSTALL()  # the shared configure/refresh/update path
    assert schedule.background_enabled()
    assert install_service.call_count == 2

    result = invoke("disable")
    assert result.exit_code == 0, result.output
    assert "Background sync: disabled" in result.output
    install_service.reset_mock()
    _REAL_INSTALL()
    assert not schedule.background_enabled()
    install_service.assert_not_called()

    result = invoke("enable")
    assert result.exit_code == 0, result.output
    assert schedule.background_enabled()
    assert not schedule.opted_out()
    install_service.assert_called_once()


def test_agent_status_uses_standard_json_envelope():
    result = CliRunner().invoke(cli, ["router", "agent", "status", "--agent"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "1.0"
    assert payload["data"][0]["enabled"] is False
    assert payload["data"][0]["state"] == "not_installed"
    assert payload["data"][0]["repository_tagging"]["enabled"] is False


def test_repo_helper_commands_are_removed():
    result = CliRunner().invoke(cli, ["router", "repo-helper", "--help"])
    assert result.exit_code == 2
    assert "No such command" in result.output


def test_native_agent_enable_and_disable_share_one_json_status(monkeypatch):
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [object()]
    )
    monkeypatch.setattr(router_repo, "service_supported", lambda: True)
    monkeypatch.setattr(router_repo, "ensure_running", lambda: True)
    monkeypatch.setattr(
        router_repo, "health", lambda: {"service": router_repo.SERVICE_NAME}
    )
    monkeypatch.setattr(
        router_module,
        "clients_using_repo_helper",
        lambda: ["codex"] if router_repo.enabled() else [],
    )
    refresh = Mock(return_value=[{"client": "codex"}])
    monkeypatch.setattr(router_module, "_refresh_clients", refresh)

    for command, enabled in [("enable", True), ("disable", False)]:
        result = CliRunner().invoke(cli, ["--agent", "router", "agent", command])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"][0]
        assert data["enabled"] is enabled
        assert data["repository_tagging"]["enabled"] is enabled
        assert router_repo.enabled() is enabled
        assert schedule.background_enabled() is enabled
        assert refresh.call_args.kwargs == {
            "preserve_sync_opt_out": True,
            **({"failures_out": []} if enabled else {}),
        }
    assert refresh.call_count == 2
    assert router_repo.listener_port() is None


@pytest.mark.parametrize("failure", ["start", "refresh"])
def test_failed_native_enable_does_not_opt_into_background_sync(monkeypatch, failure):
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [object()]
    )
    monkeypatch.setattr(router_repo, "service_supported", lambda: True)
    monkeypatch.setattr(router_repo, "ensure_running", lambda: failure != "start")
    refresh = Mock(side_effect=click.ClickException("refresh failed"))
    monkeypatch.setattr(router_module, "_refresh_clients", refresh)
    result = invoke("enable")
    assert result.exit_code == 1, result.output
    assert not schedule.background_enabled()
    assert not schedule._enabled_marker().exists()
    # Partial migration must retain its listener; failed startup must restore
    # the previous preference before any client is rewritten.
    assert router_repo.enabled() is (failure == "refresh")
    assert refresh.call_count == (1 if failure == "refresh" else 0)


@pytest.mark.parametrize("agent_mode", [False, True])
def test_client_refresh_failure_saves_opt_in_and_reports_repair(
    monkeypatch, tmp_path, agent_mode
):
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [object()]
    )
    monkeypatch.setattr(router_repo, "service_supported", lambda: True)
    monkeypatch.setattr(router_repo, "ensure_running", lambda: True)
    monkeypatch.setattr(router_repo, "health", lambda: {"local_auth": True})
    monkeypatch.setattr(router_module, "clients_using_repo_helper", lambda: ["codex"])
    monkeypatch.setattr(
        router_module, "configured_router_clients", lambda: ("codex", "pi")
    )

    def stored_key(client, _path):
        if client == "pi":
            raise click.ClickException("expired credential")
        return "real-key"

    monkeypatch.setattr(router_module, "_stored_router_api_key", stored_key)
    monkeypatch.setattr(
        router_module,
        "_fetch_models",
        lambda *a, **kw: [router_module.RouterModel(id="model", metadata=None)],
    )
    monkeypatch.setattr(router_module, "_configured_model", lambda *a: None)
    monkeypatch.setattr(
        router_module,
        "_configure_client",
        lambda *a, **kw: (tmp_path / "config", "model", False),
    )
    args = ["--agent" if agent_mode else "--human", "router", "agent", "enable"]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert schedule.background_enabled()
    assert router_repo.enabled()
    failures = [{"client": "pi", "error": "expired credential"}]
    assert router_repo.status()["refresh_failures"] == failures
    if agent_mode:
        payload = json.loads(result.output)["data"][0]
        assert payload["enabled"] is True
        assert payload["repository_tagging"]["refresh_failures"] == failures
    else:
        assert "Client refresh failed (pi): expired credential" in result.output
    monkeypatch.setattr(router_module, "_stored_router_api_key", lambda *a: "real-key")
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert router_repo.status()["refresh_failures"] == []


def test_timer_fallback_reports_no_tagging_and_rejects_port(monkeypatch):
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [object()]
    )
    monkeypatch.setattr(router_repo, "service_supported", lambda: False)
    start_listener = Mock()
    monkeypatch.setattr(router_repo, "enable", start_listener)
    result = invoke("enable", "--port", "47999")
    assert result.exit_code == 2
    assert not schedule.background_enabled()
    result = invoke("enable")
    assert result.exit_code == 0, result.output
    assert schedule.background_enabled()
    assert "Repository tagging: unavailable" in result.output
    assert not router_repo.enabled()
    start_listener.assert_not_called()


@pytest.mark.parametrize("opted_out", [False, True])
def test_refresh_never_implicitly_enables_tagging_or_sync(monkeypatch, opted_out):
    if opted_out:
        schedule.disable_background()
    monkeypatch.setattr(router_repo, "service_supported", lambda: True)
    start_listener = Mock()
    monkeypatch.setattr(router_repo, "ensure_running", start_listener)
    monkeypatch.setattr(router_module, "configured_router_clients", lambda: [])
    monkeypatch.setattr(
        router_module, "_refresh_cowork_model_selections", lambda **_kw: None
    )
    result = CliRunner().invoke(cli, ["--agent", "router", "refresh"])
    assert result.exit_code == 0, result.output
    assert not router_repo.enabled()
    assert not schedule.background_enabled()
    assert schedule.opted_out() is opted_out
    start_listener.assert_not_called()


def test_default_off_scheduled_tick_does_not_refresh(monkeypatch):
    connections = Mock()
    monkeypatch.setattr(router_module, "_scheduled_sync_connections", connections)
    result = CliRunner().invoke(cli, ["router", "sync", "--scheduled"])
    assert result.exit_code == 0, result.output
    connections.assert_not_called()


def test_disable_leaves_session_hook_refresh_unchanged(monkeypatch):
    assert invoke("disable").exit_code == 0
    refresh = Mock()
    monkeypatch.setattr(router_module, "sync_is_due", lambda: True)
    monkeypatch.setattr(router_module, "claim_sync_slot", lambda: True)
    monkeypatch.setattr(router_module, "spawn_detached_refresh", refresh)
    monkeypatch.setattr(router_module, "_emit_hook_update_notice", lambda _client: None)
    result = CliRunner().invoke(cli, ["router", "sync", "--hook"])
    assert result.exit_code == 0, result.output
    refresh.assert_called_once()


def test_os_disabled_state_does_not_claim_running(monkeypatch):
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [object()]
    )
    monkeypatch.setattr(schedule, "agent_installed", lambda: True)
    monkeypatch.setattr(schedule, "agent_state", lambda: schedule.AGENT_DISABLED)
    result = invoke("enable")
    assert result.exit_code == 0, result.output
    assert "Background sync: enabled" in result.output
    assert "agent (disabled)" in result.output
    assert "System Settings" in result.output
