"""The scheduled sync refreshes only when Router's setup version moves."""

from __future__ import annotations

import contextlib
import json
import os
import plistlib
import sys
import threading
import time
from pathlib import Path

import click
import httpx
import pytest
from click.testing import CliRunner

import ramp_cli.commands.router as router_module
import ramp_cli.commands.router_schedule as schedule
import ramp_cli.commands.router_sync as router_sync
import ramp_cli.main as main_module
import ramp_cli.version_check as version_check
from ramp_cli.config.settings import config_dir
from ramp_cli.main import cli

# Captured before the conftest fixture turns install/uninstall into no-ops.
_REAL_INSTALL = schedule.install
_REAL_UNINSTALL = schedule.uninstall

_CONNECTION = router_module._SyncConnection("https://router.test/v1", "secret-key")


def test_fallback_jobs_absolutize_relative_paths(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    executable = Path("install/ramp")
    log = Path("config/ramp/sync.log")
    env_file = Path("config/ramp/env.json")
    plist = plistlib.loads(schedule.render_plist(executable, log, env_file))
    assert plist["ProgramArguments"][0] == str(tmp_path / executable)
    assert plist["ProgramArguments"][-1] == str(tmp_path / env_file)
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == str(tmp_path / log)
    service, _ = schedule.render_systemd(executable, log, env_file)
    assert f'ExecStart="{tmp_path / executable}"' in service
    assert f'--env-file" "{tmp_path / env_file}"' in service
    assert f"StandardOutput=append:{tmp_path / log}" in service


def test_fallback_systemd_escapes_specifiers_dollars_and_quotes(tmp_path):
    root = tmp_path / 'config %Q ${LITERAL} "quoted" \\ folder'
    executable, log, env_file = root / "ramp", root / "sync.log", root / "env.json"
    service, _ = schedule.render_systemd(executable, log, env_file)
    escaped = str(root).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    command = next(
        line for line in service.splitlines() if line.startswith("ExecStart=")
    )
    assert f'"{escaped.replace("$", "$$")}/ramp"' in command
    assert f'"{escaped.replace("$", "$$")}/env.json"' in command
    # Output paths expand specifiers, but not ExecStart's environment variables.
    assert f"StandardOutput=append:{escaped}/sync.log" in service
    assert f"StandardError=append:{escaped}/sync.log" in service


@pytest.fixture(autouse=True)
def background_opt_in(isolated_config):
    """These scheduler tests exercise users who explicitly enabled the service."""
    marker = schedule._enabled_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


class _Refreshes(list):
    """Each background refresh run, plus the connections each one skipped."""

    skips: list[frozenset]


@pytest.fixture
def refreshes(monkeypatch) -> _Refreshes:
    calls = _Refreshes()
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [_CONNECTION]
    )
    monkeypatch.setattr(router_module, "refresh_version_cache", lambda: None)

    def refresh(_ctx, *, models_only=False, skip=frozenset()):
        assert models_only, "a background refresh must write only models"
        calls.skips.append(skip)
        calls.append(True)
        return []

    calls.skips = []
    monkeypatch.setattr(router_module, "_refresh_clients", refresh)
    return calls


def _router(monkeypatch, status: int = 200, version: str = "v1") -> list[str]:
    requests: list[str] = []

    def get(url, **_kwargs):
        assert url == "https://router.test/v1/client/sync-version"
        requests.append(url)
        return httpx.Response(status, json={"version": version})

    monkeypatch.setattr(router_module.httpx, "get", get)
    return requests


def _sync():
    return CliRunner().invoke(cli, ["--human", "router", "sync", "--scheduled"])


def _state() -> dict:
    return json.loads((config_dir() / "router-sync.json").read_text())


def test_unchanged_version_skips_refresh_and_writes_nothing(monkeypatch, refreshes):
    _router(monkeypatch)
    assert _sync().exit_code == 0
    assert refreshes == [True]
    stamp = (config_dir() / "router-sync.json").stat().st_mtime_ns

    assert _sync().exit_code == 0

    assert refreshes == [True]
    assert (config_dir() / "router-sync.json").stat().st_mtime_ns == stamp


@pytest.mark.parametrize("opt_out", [False, True])
def test_scheduled_startup_checks_updates_only_after_loading_preferences(
    monkeypatch, refreshes, opt_out
):
    _router(monkeypatch)
    # A service does not inherit the user's opt-out; it loads it during dispatch.
    monkeypatch.setenv("RAMP_NO_UPDATE_CHECK", "")
    path = schedule.env_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"RAMP_NO_UPDATE_CHECK": "1" if opt_out else ""}))
    passive_checks, fetches = [], []
    monkeypatch.setattr(
        main_module, "check_for_update", lambda: passive_checks.append(True)
    )
    monkeypatch.setattr(
        version_check, "latest_version", lambda: fetches.append(True) or "99.0.0"
    )
    monkeypatch.setattr(
        router_module, "refresh_version_cache", version_check.refresh_version_cache
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["ramp", "router", "sync", "--quiet", "--scheduled", "--env-file", str(path)],
    )

    main_module.main()

    assert passive_checks == []
    assert fetches == ([] if opt_out else [True])
    assert refreshes == [True]


def test_changed_version_refreshes_and_stores_it_without_the_raw_key(
    monkeypatch, refreshes
):
    _router(monkeypatch, version="v1")
    _sync()
    _router(monkeypatch, version="v2")

    assert _sync().exit_code == 0

    assert refreshes == [True, True]
    state = _state()
    assert list(state["versions"].values()) == ["v2"]
    assert "secret-key" not in json.dumps(state)


def test_skipped_cowork_pass_is_retried_once_claude_desktop_closes(
    monkeypatch, refreshes
):
    skipped = {"client": "cowork", "skipped_while_running": True}
    monkeypatch.setattr(
        router_module, "_refresh_clients", lambda _ctx, **_kwargs: [skipped]
    )
    running = {"value": True}
    monkeypatch.setattr(
        router_module.claude_cowork, "claude_is_running", lambda: running["value"]
    )
    retries: list[bool] = []
    monkeypatch.setattr(
        router_module,
        "_refresh_cowork_model_selections",
        lambda **_kwargs: retries.append(True) or {"client": "cowork"},
    )
    _router(monkeypatch)
    _sync()
    assert _state()["cowork_pending"] is True

    _sync()  # same version, Desktop still open: nothing to do yet
    assert retries == [] and _state()["cowork_pending"] is True

    running["value"] = False
    _sync()
    assert retries == [True] and "cowork_pending" not in _state()


def test_missing_endpoint_falls_back_to_the_session_cooldown(monkeypatch, refreshes):
    _router(monkeypatch, status=404)
    _sync()
    assert refreshes == [True]  # cooldown never started: due

    _sync()
    assert refreshes == [True]  # cooldown now running: no refresh

    cooldown = config_dir() / "router-sync-last"
    old = time.time() - 13 * 3600
    os.utime(cooldown, (old, old))
    # The 24h safety net also stays quiet: the last full refresh was just now.
    _sync()
    assert refreshes == [True, True]


def test_job_definitions_render_and_unchanged_content_never_reloads(
    monkeypatch, tmp_path
):
    exe = tmp_path / "ramp"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    log = tmp_path / "router-sync.log"
    env_file = tmp_path / "router-sync-env.json"
    plist = schedule.render_plist(exe, log, env_file)
    assert b"<integer>900</integer>" in plist
    assert f"<string>{env_file}</string>".encode() in plist
    service, timer = schedule.render_systemd(exe, log, env_file)
    assert f'ExecStart="{exe}" "router" "sync" "--scheduled" "--env-file"' in service
    assert "Type=oneshot" in service
    assert "OnUnitActiveSec=15min" in timer
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert schedule._systemd_dir() == tmp_path / "xdg" / "systemd" / "user"

    commands: list[tuple[str, ...]] = []
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(schedule.sys, "platform", "darwin")
    monkeypatch.setattr(schedule, "ramp_executable", lambda: exe)
    monkeypatch.setattr(schedule, "log_path", lambda: log)
    loaded = {"value": False}

    def run(*command: str) -> bool:
        commands.append(command)
        if command[1] == "bootstrap":
            loaded["value"] = True
        return loaded["value"] if command[1] == "print" else True

    monkeypatch.setattr(schedule, "_run", run)

    # Custom config locations and preferences reach the job through its env
    # file.
    monkeypatch.setenv("CODEX_HOME", "/custom/codex")
    monkeypatch.setenv("RAMP_NO_UPDATE_CHECK", "1")
    _REAL_INSTALL()
    assert [c[1] for c in commands] == ["print", "bootstrap"]
    job_environment = json.loads(schedule.env_file_path().read_text())
    assert job_environment["CODEX_HOME"] == "/custom/codex"
    assert job_environment["RAMP_NO_UPDATE_CHECK"] == "1"
    commands.clear()

    # A changed environment needs no reload; inside Conductor's Codex the
    # user's own (here default) Codex home is kept, not Conductor's.
    settings = tmp_path / "conductor" / "settings.json"
    monkeypatch.setattr(router_module.conductor, "settings_path", lambda: settings)
    monkeypatch.setenv(
        "CODEX_HOME", str(settings.parent / "ramp-router" / "codex-home")
    )
    monkeypatch.setenv("RAMP_CONDUCTOR_ORIGINAL_CODEX_HOME", "")
    _REAL_INSTALL()
    assert [c[1] for c in commands] == ["print"]
    assert "CODEX_HOME" not in json.loads(schedule.env_file_path().read_text())
    # A custom home Conductor's sync hook already restored is the user's.
    monkeypatch.setenv("CODEX_HOME", "/restored/codex")
    _REAL_INSTALL()
    assert (
        json.loads(schedule.env_file_path().read_text())["CODEX_HOME"]
        == "/restored/codex"
    )
    # A background run never replaces the env file an interactive run wrote.
    monkeypatch.setenv(schedule.SCHEDULED_ENV, "1")
    monkeypatch.setenv("CODEX_HOME", "/stale/snapshot")
    _REAL_INSTALL()
    assert (
        json.loads(schedule.env_file_path().read_text())["CODEX_HOME"]
        == "/restored/codex"
    )
    monkeypatch.delenv(schedule.SCHEDULED_ENV)
    monkeypatch.delenv("RAMP_CONDUCTOR_ORIGINAL_CODEX_HOME")

    # A changed definition must not boot out a scheduled refresh in progress;
    # it is neither written nor reloaded until the sync lock is free.
    plist_path = tmp_path / "Library/LaunchAgents/com.ramp.router-sync.plist"
    before = plist_path.read_bytes()
    moved = tmp_path / "moved-ramp"
    monkeypatch.setattr(schedule, "ramp_executable", lambda: moved)

    @contextlib.contextmanager
    def busy(**_kwargs):
        yield False

    monkeypatch.setattr(schedule, "sync_lock", busy)
    commands.clear()
    _REAL_INSTALL()
    assert [c[1] for c in commands] == ["print"]
    assert plist_path.read_bytes() == before

    # An opt-out exported in the user's shell removes the job, and a background
    # run that started before it (no opt-out in its environment) can't reinstall.
    monkeypatch.setattr(schedule, "sync_lock", router_module.sync_lock)
    monkeypatch.setattr(schedule, "ramp_executable", lambda: exe)
    monkeypatch.setenv("RAMP_NO_SESSION_SYNC", "1")
    monkeypatch.setattr(schedule, "uninstall", _REAL_UNINSTALL)
    commands.clear()
    _REAL_INSTALL()
    assert [c[1] for c in commands] == ["bootout"]
    monkeypatch.delenv("RAMP_NO_SESSION_SYNC")
    monkeypatch.setenv(schedule.SCHEDULED_ENV, "1")
    commands.clear()
    _REAL_INSTALL()
    assert commands == []
    # Neither an interactive refresh nor an update silently opts back in.
    monkeypatch.delenv(schedule.SCHEDULED_ENV)
    loaded["value"] = False
    _REAL_INSTALL()
    assert commands == []
    monkeypatch.setattr(schedule, "install", _REAL_INSTALL)
    schedule.enable_background()
    assert [c[1] for c in commands] == ["print", "bootstrap"]


def test_connections_read_under_client_lock_and_skip_unenrolled(monkeypatch, tmp_path):
    """A concurrent unconfigure must never pair a restored key with Router."""
    held: list[str] = []

    @contextlib.contextmanager
    def lock(client: str):
        held.append(client)
        yield
        held.remove(client)

    def stored_key(client, _path):
        if client in ("claude-code", "codex"):
            assert held == [client], "the credential was read outside its lock"
            return f"{client}-key"
        assert client == "hermes", "an unenrolled setup's credential was read"
        assert held == ["hermes"], "the credential was read outside its lock"
        return "router-key"

    monkeypatch.setattr(
        router_module,
        "configured_router_clients",
        lambda: ("claude-code", "codex", "hermes", "conductor"),
    )
    monkeypatch.setattr(
        router_module, "_client_config_path", lambda client: tmp_path / client / "c"
    )
    (tmp_path / "hermes").mkdir()
    (tmp_path / "hermes" / "ramp-router-state.json").write_text("{}")
    (tmp_path / "claude-code").mkdir()
    (tmp_path / "claude-code" / "ramp-router-state.json").write_text("{}")
    monkeypatch.setattr(
        router_module.claude_code, "settings_lock", lambda _path: lock("claude-code")
    )
    monkeypatch.setattr(router_module, "codex_config_lock", lambda _path: lock("codex"))
    monkeypatch.setattr(router_module, "_hermes_config_lock", lambda: lock("hermes"))
    monkeypatch.setattr(
        router_module, "_conductor_settings_lock", lambda _path: lock("conductor")
    )
    monkeypatch.setattr(router_module, "_hermes_receipt_enrolls", lambda _p: True)
    monkeypatch.setattr(router_module, "_conductor_receipt_enrolls", lambda _p: False)
    monkeypatch.setattr(
        router_module, "_refresh_router_base_url", lambda *_a: "https://router.test/v1"
    )
    monkeypatch.setattr(router_module, "_stored_router_api_key", stored_key)
    monkeypatch.setattr(
        router_module, "_settings_origin", lambda *_a: "https://dashboard.test"
    )
    monkeypatch.setattr(
        router_module.claude_cowork, "state_path", lambda: tmp_path / "absent"
    )

    # Hermes reads no coding-agent defaults, so it has no settings origin.
    dashboard = frozenset({"https://dashboard.test"})
    assert router_module._scheduled_sync_connections() == [
        router_module._SyncConnection(
            "https://router.test/v1", "claude-code-key", dashboard
        ),
        router_module._SyncConnection("https://router.test/v1", "codex-key", dashboard),
        router_module._SyncConnection("https://router.test/v1", "router-key"),
    ]


def test_opt_out_during_a_background_install_still_wins(monkeypatch, tmp_path):
    """A run that passed the opt-out check must not re-enable the job after it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(schedule.sys, "platform", "darwin")
    events: list[str] = []
    inside = threading.Event()

    def slow_install():
        inside.set()
        time.sleep(0.3)  # systemctl/launchctl calls take this long or longer
        events.append("installed")

    monkeypatch.setattr(schedule, "_install_launchd", slow_install)
    monkeypatch.setattr(
        schedule, "_run", lambda *command: events.append(command[1]) or True
    )
    background = threading.Thread(target=_REAL_INSTALL)
    background.start()
    inside.wait(5)
    monkeypatch.setenv("RAMP_NO_SESSION_SYNC", "1")
    _REAL_INSTALL()
    background.join(5)

    assert events == ["installed", "bootout"]
    assert schedule.opted_out()


def _save_state(state: dict) -> None:
    (config_dir() / "router-sync.json").write_text(json.dumps(state))


def test_a_revoked_key_is_left_alone_and_rechecked_daily(monkeypatch, refreshes):
    requests = _router(monkeypatch, status=401)
    assert _sync().exit_code == 0
    assert refreshes == [] and len(requests) == 1
    assert list(_state()["revoked"]) == [_CONNECTION.digest]
    log = (config_dir() / "router-sync.log").read_text()
    assert log.count("revoked or inactive") == 1

    _sync()  # within a day the key is not even checked again
    assert refreshes == [] and len(requests) == 1

    state = _state()
    state["revoked"][_CONNECTION.digest] = time.time() - 25 * 3600
    _save_state(state)
    _router(monkeypatch, version="v1")  # the key works again
    _sync()
    assert refreshes == [True]
    assert "revoked" not in _state()


def test_a_revoked_key_is_skipped_while_healthy_ones_refresh(monkeypatch, refreshes):
    healthy = router_module._SyncConnection("https://router.test/v1", "good-key")
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [_CONNECTION, healthy]
    )

    def get(url, *, headers, **_kwargs):
        if headers["Authorization"] == "Bearer secret-key":
            return httpx.Response(403, json={})
        return httpx.Response(200, json={"version": "v1"})

    monkeypatch.setattr(router_module.httpx, "get", get)
    _sync()

    assert refreshes == [True]
    assert refreshes.skips == [frozenset({("https://router.test/v1", "secret-key")})]
    # The failure a dead key would cause never schedules a retry.
    assert "retry_after" not in _state()


@pytest.mark.parametrize("legacy_retry", [False, True])
@pytest.mark.parametrize(
    "last_refresh", [None, -60, -schedule.FULL_REFRESH_SECONDS - 1]
)
def test_revocation_clears_pending_retries_and_keeps_the_agent_idle(
    monkeypatch, tmp_path, refreshes, legacy_retry, last_refresh
):
    now = {"value": 1_000_000.0}
    monkeypatch.setattr(router_module.time, "time", lambda: now["value"])
    monkeypatch.setattr(schedule, "agent_executable", lambda: tmp_path / "agent")
    state = {
        "versions": {_CONNECTION.digest: "v1"},
        "retry_after": now["value"] - 1,
        "retry_delay": schedule.RETRY_SECONDS,
    }
    if not legacy_retry:
        state["retry_connections"] = [_CONNECTION.digest]
    if last_refresh is not None:
        state["last_full_refresh"] = now["value"] + last_refresh
    schedule.save_state(state)
    requests = _router(monkeypatch, status=401)

    assert _sync().exit_code == 0

    state = _state()
    assert refreshes == []
    assert state["revoked"] == {_CONNECTION.digest: now["value"]}
    assert not {"retry_connections", "retry_after", "retry_delay"} & state.keys()
    manifest = json.loads(schedule.manifest_path().read_text())
    until = now["value"] + schedule.FULL_REFRESH_SECONDS
    assert manifest["due_at"] == until
    assert manifest["connections"][0]["skip_until"] == until
    assert _sync().exit_code == 0
    assert _state() == state
    assert len(requests) == 1  # the revocation cooldown still suppresses probes

    now["value"] = until + 1
    _router(monkeypatch, version="v1")
    assert _sync().exit_code == 0
    assert refreshes == [True]  # rechecked after cooldown even with the same version
    assert "revoked" not in _state()


def test_revocation_keeps_other_pending_retries(monkeypatch, refreshes):
    other = router_module._SyncConnection("https://router.test/v1", "other-key")
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [_CONNECTION, other]
    )
    now = 1_000_000.0
    monkeypatch.setattr(router_module.time, "time", lambda: now)
    schedule.save_state(
        {
            "versions": {_CONNECTION.digest: "v1", other.digest: "v1"},
            "last_full_refresh": now,
            "retry_connections": [_CONNECTION.digest, other.digest],
            "retry_after": now - 1,
            "retry_delay": schedule.RETRY_SECONDS,
        }
    )

    def get(_url, *, headers, **_kwargs):
        status = 401 if headers["Authorization"] == "Bearer secret-key" else 503
        return httpx.Response(status, json={})

    monkeypatch.setattr(router_module.httpx, "get", get)
    assert _sync().exit_code == 0

    state = _state()
    assert refreshes == []
    assert state["retry_connections"] == [other.digest]
    assert state["retry_after"] == now + 2 * schedule.RETRY_SECONDS
    assert state["revoked"] == {_CONNECTION.digest: now}


def test_revocation_does_not_leave_a_healthy_connection_backing_off(
    monkeypatch, refreshes
):
    other = router_module._SyncConnection("https://router.test/v1", "other-key")
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [_CONNECTION, other]
    )
    now = 1_000_000.0
    monkeypatch.setattr(router_module.time, "time", lambda: now)
    schedule.save_state(
        {
            "versions": {_CONNECTION.digest: "v1", other.digest: "v1"},
            "last_full_refresh": now - schedule.FULL_REFRESH_SECONDS - 1,
            "retry_connections": [_CONNECTION.digest],
            "retry_after": now + schedule.RETRY_SECONDS,
            "retry_delay": schedule.RETRY_SECONDS,
        }
    )

    def get(_url, *, headers, **_kwargs):
        status = 401 if headers["Authorization"] == "Bearer secret-key" else 200
        return httpx.Response(status, json={"version": "v1"})

    monkeypatch.setattr(router_module.httpx, "get", get)
    assert _sync().exit_code == 0

    assert refreshes == [True]  # the revoked key cannot delay the daily safety pass
    assert not {"retry_connections", "retry_after", "retry_delay"} & _state().keys()
    assert refreshes.skips == [frozenset({(_CONNECTION.base_url, _CONNECTION.api_key)})]


def test_an_outage_is_retried_next_tick_without_the_fallback(monkeypatch, refreshes):
    _router(monkeypatch, status=503)
    _sync()
    assert refreshes == []
    assert not (config_dir() / "router-sync-last").exists()  # no cooldown

    def unreachable(url, **_kwargs):
        raise httpx.ConnectError("offline", request=httpx.Request("GET", url))

    monkeypatch.setattr(router_module.httpx, "get", unreachable)
    _sync()
    assert refreshes == []

    _router(monkeypatch, version="v1")  # Router is back
    _sync()
    assert refreshes == [True]


def test_a_failed_refresh_is_retried_hourly(monkeypatch, refreshes):
    _router(monkeypatch, status=404)  # an older Router: the cooldown fallback

    def failing(_ctx, **_kwargs):
        refreshes.append(True)
        raise click.ClickException("Codex: broken config")

    monkeypatch.setattr(router_module, "_refresh_clients", failing)
    _sync()
    assert refreshes == [True]
    # Only a successful refresh starts the 12-hour cooldown.
    assert not (config_dir() / "router-sync-last").exists()

    _sync()  # still backing off
    assert refreshes == [True]

    state = _state()
    state["retry_after"] = time.time() - 1
    _save_state(state)
    _sync()  # the hour passed: retried, not left for the cooldown
    assert refreshes == [True, True]


def test_a_changed_workspace_default_refreshes(monkeypatch, refreshes):
    connection = router_module._SyncConnection(
        "https://router.test/v1",
        "secret-key",
        frozenset({"https://dashboard.test"}),
    )
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [connection]
    )
    harnesses = {"codex": {"effective_model": "gpt-5.4"}}

    def get(url, **_kwargs):
        if url == "https://router.test/v1/client/sync-version":
            return httpx.Response(200, json={"version": "v1"})
        assert url == "https://dashboard.test/self-service/coding-agent-settings"
        return httpx.Response(
            200,
            json={"version": 3, "harnesses": harnesses},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(router_module.httpx, "get", get)
    _sync()
    _sync()
    assert refreshes == [True]

    # Router's model list is unchanged; only the workspace default moved.
    harnesses["codex"] = {"effective_model": "claude-fable-5-1"}
    _sync()
    assert refreshes == [True, True]


def test_a_control_plane_without_defaults_still_syncs(monkeypatch, refreshes):
    connection = router_module._SyncConnection(
        "https://router.test/v1", "secret-key", frozenset({"https://dashboard.test"})
    )
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [connection]
    )

    def get(url, **_kwargs):
        if url.endswith("/client/sync-version"):
            return httpx.Response(200, json={"version": "v1"})
        return httpx.Response(404, json={})

    monkeypatch.setattr(router_module.httpx, "get", get)
    _sync()
    _sync()
    assert refreshes == [True]


@pytest.mark.parametrize("settings_status", [503, 302, 403])
def test_a_failing_defaults_check_never_blocks_model_list_sync(
    monkeypatch, refreshes, settings_status
):
    connection = router_module._SyncConnection(
        "https://router.test/v1", "secret-key", frozenset({"https://dashboard.test"})
    )
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [connection]
    )
    answer = {"version": "v1", "settings": 200, "default": "gpt-5.4"}

    def get(url, **_kwargs):
        if url.endswith("/client/sync-version"):
            return httpx.Response(200, json={"version": answer["version"]})
        harnesses = {"codex": {"effective_model": answer["default"]}}
        return httpx.Response(
            answer["settings"],
            json={"harnesses": harnesses},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(router_module.httpx, "get", get)
    _sync()
    assert refreshes == [True]

    # The control plane fails (an outage, an Access redirect, a 403): Router's
    # version alone still decides, and an unchanged one refreshes nothing.
    answer["settings"] = settings_status
    _sync()
    assert refreshes == [True]
    answer["version"] = "v2"
    _sync()
    assert refreshes == [True, True]
    assert refreshes.skips[-1] == frozenset()

    # The defaults digest last seen was kept, so a default that moved while
    # the control plane was failing still refreshes once it answers again.
    answer["default"] = "claude-fable-5-1"
    _sync()
    assert refreshes == [True, True]
    answer["settings"] = 200
    _sync()
    assert refreshes == [True, True, True]
    _sync()
    assert refreshes == [True, True, True]


@pytest.mark.parametrize("coverage", [True, False, None, "true"])
def test_settings_failure_remains_visible_to_agent(monkeypatch, refreshes, coverage):
    connection = router_module._SyncConnection(
        "https://router.test/v1", "secret-key", frozenset({"https://dashboard.test"})
    )
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [connection]
    )
    answer = {"version": "v1", "status": 200}

    def get(url, **_kwargs):
        if url.endswith("/client/sync-version"):
            return httpx.Response(
                200,
                json={
                    "version": answer["version"],
                    "includes_coding_agent_settings": coverage,
                },
            )
        return httpx.Response(
            answer["status"], json={"harnesses": {"codex": {"model": "new"}}}
        )

    monkeypatch.setattr(router_module.httpx, "get", get)
    assert _sync().exit_code == 0
    original_defaults = _state()["versions"][connection.digest].split("|")[1]
    answer.update(version="v2", status=503)
    assert _sync().exit_code == 0
    defaults = "?" if coverage is True else original_defaults
    assert _state()["versions"][connection.digest] == f"v2|{defaults}"
    assert refreshes == [True, True]  # model refresh still runs
    answer["status"] = 200
    assert _sync().exit_code == 0
    assert _state()["versions"][connection.digest] == f"v2|{original_defaults}"
    assert len(refreshes) == (3 if coverage is True else 2)


def test_a_broken_setup_backs_off_without_holding_back_new_versions(
    monkeypatch, refreshes
):
    version = {"value": "v1"}

    def get(url, **_kwargs):
        return httpx.Response(200, json={"version": version["value"]})

    def failing(_ctx, **_kwargs):
        refreshes.append(True)
        raise click.ClickException("Codex: broken config")

    monkeypatch.setattr(router_module.httpx, "get", get)
    monkeypatch.setattr(router_module, "_refresh_clients", failing)
    _sync()
    assert refreshes == [True]
    state = _state()
    # The version is recorded anyway, so the failure does not look like a
    # change on every tick; the retry schedule owns it.
    assert list(state["versions"].values()) == ["v1"]
    assert state["retry_delay"] == schedule.RETRY_SECONDS
    _sync()
    assert refreshes == [True]

    state["retry_after"] = time.time() - 1
    _save_state(state)
    _sync()  # retried, still failing: the wait doubles
    assert refreshes == [True, True]
    state = _state()
    assert state["retry_delay"] == 2 * schedule.RETRY_SECONDS

    state["retry_delay"] = schedule.FULL_REFRESH_SECONDS
    state["retry_after"] = time.time() - 1
    _save_state(state)
    _sync()  # capped at a day
    assert _state()["retry_delay"] == schedule.FULL_REFRESH_SECONDS

    version["value"] = "v2"  # a new version refreshes despite the backoff
    calls = len(refreshes)
    _sync()
    assert len(refreshes) == calls + 1

    def healthy(_ctx, **_kwargs):
        refreshes.append(True)
        return []

    monkeypatch.setattr(router_module, "_refresh_clients", healthy)
    version["value"] = "v3"
    _sync()
    state = _state()
    assert list(state["versions"].values()) == ["v3"]
    assert "retry_after" not in state and "retry_delay" not in state
    assert (config_dir() / "router-sync-last").exists()


def test_opt_out_never_removes_the_job_under_a_running_refresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(schedule.sys, "platform", "darwin")
    events: list[str] = []
    monkeypatch.setattr(
        schedule, "_run", lambda *command: events.append(command[1]) or True
    )
    monkeypatch.setattr(schedule, "REMOVE_WAIT_SECONDS", 0.3)
    monkeypatch.setenv("RAMP_NO_SESSION_SYNC", "1")

    with router_sync.sync_lock() as held:  # a scheduled refresh is writing
        assert held
        _REAL_INSTALL()

    assert events == []  # never booted out mid-write
    assert schedule.opted_out()

    # The job's next tick removes the job before it writes anything.
    monkeypatch.delenv("RAMP_NO_SESSION_SYNC")
    assert _sync().exit_code == 0
    assert events == ["bootout"]
    assert not schedule.env_file_path().exists()


def test_opt_out_removes_the_job_once_a_running_refresh_finishes(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(schedule.sys, "platform", "darwin")
    events: list[str] = []
    monkeypatch.setattr(
        schedule, "_run", lambda *command: events.append(command[1]) or True
    )
    started = threading.Event()

    def refresh():
        with router_sync.sync_lock() as held:
            assert held
            started.set()
            time.sleep(0.3)
            events.append("refresh finished")

    running = threading.Thread(target=refresh)
    running.start()
    started.wait(5)
    monkeypatch.setenv("RAMP_NO_SESSION_SYNC", "1")
    _REAL_INSTALL()
    running.join(5)

    assert events == ["refresh finished", "bootout"]


def test_a_deferred_opt_out_reaches_a_job_under_another_config_root(
    monkeypatch, tmp_path
):
    """The marker must be where the job looks, whatever XDG_CONFIG_HOME it has."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(schedule.sys, "platform", "darwin")
    events: list[str] = []
    monkeypatch.setattr(
        schedule, "_run", lambda *command: events.append(command[1]) or True
    )
    monkeypatch.setattr(schedule, "REMOVE_WAIT_SECONDS", 0.3)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "shell-config"))
    monkeypatch.setenv("RAMP_NO_SESSION_SYNC", "1")
    with router_sync.sync_lock() as held:  # a scheduled refresh is writing
        assert held
        _REAL_INSTALL()
    assert events == []

    # The job's own environment names the config root it was installed with.
    monkeypatch.delenv("RAMP_NO_SESSION_SYNC")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "job-config"))
    assert schedule.opted_out()
    assert _sync().exit_code == 0
    assert events == ["bootout"]


def test_relative_config_locations_are_saved_as_absolute_paths(monkeypatch, tmp_path):
    # The job runs from another working directory, so a relative location
    # must point at the same place it did in the installing shell.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_HOME", "./codex-home")
    monkeypatch.setenv("OPENCODE_CONFIG", "~/opencode.json")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/abs/link/../claude")
    monkeypatch.setenv("HERMES_HOME", " /abs/hermes ")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "/abs/pi ")
    monkeypatch.setenv("RAMP_OPENCODE_MAJOR", "1")

    environment = schedule._job_environment()

    assert environment["CODEX_HOME"] == os.path.join(os.getcwd(), "./codex-home")
    # ~ is left for the readers to expand; absolute paths are never
    # normalized, since ".." past a symlink resolves on disk, not in text.
    assert environment["OPENCODE_CONFIG"] == "~/opencode.json"
    assert environment["CLAUDE_CONFIG_DIR"] == "/abs/link/../claude"
    # Only the Hermes reader trims; every other value is kept as typed.
    assert environment["HERMES_HOME"] == "/abs/hermes"
    assert environment["PI_CODING_AGENT_DIR"] == "/abs/pi "
    assert environment["RAMP_OPENCODE_MAJOR"] == "1"


@pytest.mark.parametrize("name", schedule.CA_ENV_VARS)
def test_ca_environment_reaches_the_spawned_sync(monkeypatch, tmp_path, name):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(name, "relative-ca.pem")
    environment = schedule._job_environment()
    assert environment[name] == str(tmp_path / "relative-ca.pem")
    schedule.env_file_path().parent.mkdir(parents=True, exist_ok=True)
    schedule.env_file_path().write_text(json.dumps(environment))
    monkeypatch.delenv(name)

    schedule.load_job_environment(str(schedule.env_file_path()))

    assert os.environ[name] == str(tmp_path / "relative-ca.pem")


@pytest.mark.parametrize("legacy_retry", [False, True])
def test_partial_refresh_retains_unavailable_connections_for_retry(
    monkeypatch, tmp_path, refreshes, legacy_retry
):
    other = router_module._SyncConnection("https://router.test/v1", "other-key")
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [_CONNECTION, other]
    )
    now = {"value": 1_000_000.0}
    monkeypatch.setattr(router_module.time, "time", lambda: now["value"])
    answer = {"unavailable": False, "version": "v1"}

    def get(_url, *, headers, **_kwargs):
        if headers["Authorization"] == "Bearer secret-key":
            return httpx.Response(200, json={"version": answer["version"]})
        return httpx.Response(
            503 if answer["unavailable"] else 200, json={"version": "v1"}
        )

    monkeypatch.setattr(router_module.httpx, "get", get)
    assert _sync().exit_code == 0
    assert refreshes == [True]
    if legacy_retry:
        state = _state()
        state.update(schedule.retry_after_failure({}, now["value"]))
        _save_state(state)
    # One connection changes while the other cannot be checked/refreshed.
    answer.update(unavailable=True, version="v2")
    assert _sync().exit_code == 0
    assert refreshes == [True, True]
    state = _state()
    assert state["retry_connections"] == [other.digest]
    assert state["versions"][other.digest] == "v1"

    now["value"] = state["retry_after"] + 1
    assert _sync().exit_code == 0
    assert refreshes == [True, True]  # reachable healthy key has no pending retry
    assert _state()["retry_connections"] == [other.digest]
    delayed = _state()
    assert delayed["retry_after"] == now["value"] + 2 * schedule.RETRY_SECONDS
    assert delayed["retry_delay"] == 2 * schedule.RETRY_SECONDS
    monkeypatch.setattr(schedule, "agent_executable", lambda: tmp_path / "agent")
    router_module._write_agent_manifest([_CONNECTION, other])
    manifest = json.loads(schedule.manifest_path().read_text())
    assert manifest["due_at"] == delayed["retry_after"]
    assert manifest["due_at"] > now["value"]
    assert _sync().exit_code == 0
    assert _state() == delayed  # an early tick neither retries nor extends the backoff

    answer["unavailable"] = False  # recovers without a version change
    now["value"] = delayed["retry_after"] + 1
    assert _sync().exit_code == 0
    assert refreshes == [True, True, True]
    assert "retry_connections" not in _state()
    assert "retry_after" not in _state()
    assert _sync().exit_code == 0
    assert refreshes == [True, True, True]


@pytest.mark.parametrize(
    "delay", [schedule.RETRY_SECONDS, schedule.FULL_REFRESH_SECONDS]
)
def test_due_retry_backs_off_when_no_connection_is_reachable(
    monkeypatch, refreshes, delay
):
    now = 1_000_000.0
    monkeypatch.setattr(router_module.time, "time", lambda: now)
    _router(monkeypatch, status=503)
    schedule.save_state(
        {
            "versions": {_CONNECTION.digest: "v1"},
            "last_full_refresh": now - 60,
            "retry_connections": [_CONNECTION.digest],
            "retry_after": now - 1,
            "retry_delay": delay,
        }
    )

    assert _sync().exit_code == 0

    state = _state()
    assert refreshes == []
    assert state["retry_after"] == now + min(delay * 2, schedule.FULL_REFRESH_SECONDS)
    assert state["retry_connections"] == [_CONNECTION.digest]
    assert state["versions"] == {_CONNECTION.digest: "v1"}
    assert state["last_full_refresh"] == now - 60  # no refresh was attempted


@pytest.mark.parametrize("last_refresh", [None, -schedule.FULL_REFRESH_SECONDS - 1])
@pytest.mark.parametrize("recovered_version", ["v1", "v2"])
def test_overdue_daily_refresh_backs_off_through_an_outage(
    monkeypatch, tmp_path, refreshes, last_refresh, recovered_version
):
    now = {"value": 1_000_000.0}
    monkeypatch.setattr(router_module.time, "time", lambda: now["value"])
    monkeypatch.setattr(schedule, "agent_executable", lambda: tmp_path / "agent")
    original = {"versions": {_CONNECTION.digest: "v1"}}
    if last_refresh is not None:
        original["last_full_refresh"] = now["value"] + last_refresh
    schedule.save_state(original)
    _router(monkeypatch, status=503)

    assert _sync().exit_code == 0

    delayed = _state()
    assert refreshes == []
    assert delayed["retry_connections"] == [_CONNECTION.digest]
    assert delayed["retry_after"] == now["value"] + schedule.RETRY_SECONDS
    assert delayed.get("last_full_refresh") == original.get("last_full_refresh")
    manifest = json.loads(schedule.manifest_path().read_text())
    assert manifest["due_at"] == delayed["retry_after"] > now["value"]
    now["value"] += schedule.AGENT_INTERVAL_SECONDS
    assert _sync().exit_code == 0
    assert _state() == delayed

    now["value"] = delayed["retry_after"]
    assert _sync().exit_code == 0
    delayed = _state()
    assert delayed["retry_after"] == now["value"] + 2 * schedule.RETRY_SECONDS
    assert delayed["retry_delay"] == 2 * schedule.RETRY_SECONDS
    assert refreshes == []

    _router(monkeypatch, version=recovered_version)
    if recovered_version == "v1":
        assert _sync().exit_code == 0
        assert refreshes == []  # unchanged versions honor the current backoff
        now["value"] = delayed["retry_after"]
    assert _sync().exit_code == 0
    assert refreshes == [True]  # new versions still refresh without waiting
    assert not {"retry_connections", "retry_after", "retry_delay"} & _state().keys()
    assert _state()["versions"] == {_CONNECTION.digest: recovered_version}


def test_partial_success_advances_an_expired_retry_for_the_skipped_connection(
    monkeypatch, refreshes
):
    other = router_module._SyncConnection("https://router.test/v1", "other-key")
    now = 1_000_000.0
    monkeypatch.setattr(router_module.time, "time", lambda: now)
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [_CONNECTION, other]
    )
    schedule.save_state(
        {
            "versions": {_CONNECTION.digest: "v1", other.digest: "v1"},
            "last_full_refresh": now,
            "retry_connections": [other.digest],
            "retry_after": now - 1,
            "retry_delay": schedule.RETRY_SECONDS,
        }
    )

    def get(_url, *, headers, **_kwargs):
        status = 200 if headers["Authorization"] == "Bearer secret-key" else 503
        return httpx.Response(status, json={"version": "v2"})

    monkeypatch.setattr(router_module.httpx, "get", get)
    assert _sync().exit_code == 0

    state = _state()
    assert refreshes == [True]
    assert state["retry_connections"] == [other.digest]
    assert state["retry_after"] == now + 2 * schedule.RETRY_SECONDS
    assert state["versions"] == {_CONNECTION.digest: "v2", other.digest: "v1"}


def test_failed_refresh_tracks_retries_for_reachable_and_skipped_connections(
    monkeypatch, refreshes
):
    other = router_module._SyncConnection("https://router.test/v1", "other-key")
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [_CONNECTION, other]
    )

    def get(_url, *, headers, **_kwargs):
        status = 200 if headers["Authorization"] == "Bearer secret-key" else 503
        return httpx.Response(status, json={"version": "v1"})

    def fail(_ctx, **_kwargs):
        raise click.ClickException("broken config")

    monkeypatch.setattr(router_module.httpx, "get", get)
    monkeypatch.setattr(router_module, "_refresh_clients", fail)
    assert _sync().exit_code == 0
    assert _state()["retry_connections"] == sorted([_CONNECTION.digest, other.digest])


def test_the_refresh_applies_the_defaults_the_check_read(monkeypatch, refreshes):
    connection = router_module._SyncConnection(
        "https://router.test/v1", "secret-key", frozenset({"https://dashboard.test"})
    )
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [connection]
    )
    answer = {"default": "gpt-5.4"}
    during_refresh = {"value": False}
    applied: list[str | None] = []

    def get(url, **_kwargs):
        if url.endswith("/client/sync-version"):
            return httpx.Response(200, json={"version": "v1"})
        # A slow or failing control plane during the refresh itself.
        status = 503 if during_refresh["value"] else 200
        harnesses = {"codex": {"effective_model": answer["default"]}}
        return httpx.Response(
            status, json={"harnesses": harnesses}, request=httpx.Request("GET", url)
        )

    def refresh(_ctx, **_kwargs):
        refreshes.append(True)
        router_module.admin_defaults.clear_cache()
        during_refresh["value"] = True
        harnesses = router_module.admin_defaults.coding_agent_settings(
            "secret-key", "https://dashboard.test"
        )
        during_refresh["value"] = False
        applied.append(router_module.admin_defaults.effective_model(harnesses, "codex"))
        return []

    monkeypatch.setattr(router_module.httpx, "get", get)
    monkeypatch.setattr(router_module, "_refresh_clients", refresh)
    _sync()
    answer["default"] = "claude-fable-5-1"
    _sync()
    _sync()

    # One refresh per change, each applying the body the check read.
    assert refreshes == [True, True]
    assert applied == ["gpt-5.4", "claude-fable-5-1"]


def test_switchyard_being_offered_refreshes(monkeypatch, refreshes):
    connection = router_module._SyncConnection(
        "https://router.test/v1", "secret-key", frozenset({"https://dashboard.test"})
    )
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [connection]
    )
    body = {"harnesses": {"codex": {"effective_model": None}}}

    def get(url, **_kwargs):
        if url.endswith("/client/sync-version"):
            return httpx.Response(200, json={"version": "v1"})
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(router_module.httpx, "get", get)
    _sync()
    _sync()
    assert refreshes == [True]

    # Only the one-time push offer changed: refresh so it is pushed.
    body["switchyard_offered"] = True
    _sync()
    assert refreshes == [True, True]
