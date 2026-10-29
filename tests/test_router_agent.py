"""The Ramp Router agent: one service for background sync and repository tagging."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import plistlib
import stat
import subprocess
from pathlib import Path

import click
import httpx
import pytest
from click.testing import CliRunner

from ramp_cli.commands import router as router_module
from ramp_cli.commands import router_repo
from ramp_cli.commands import router_schedule as schedule
from ramp_cli.main import cli

# Captured before the conftest fixture turns these into no-ops.
_REAL_INSTALL = schedule.install
_REAL_UNINSTALL = schedule.uninstall
_REAL_UPDATE_AGENT = schedule.update_agent


def test_helper_url_round_trips_to_the_real_router_url():
    wrapped = router_repo.wrap_url("https://internal-api.router.com/v1", 47317)

    assert wrapped == "http://127.0.0.1:47317/u/internal-api.router.com/v1"
    assert router_repo.unwrap_url(wrapped) == "https://internal-api.router.com/v1"


def test_codex_provider_uses_helper_only_when_enabled(monkeypatch, tmp_path):
    key = tmp_path / "router-key"
    key.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(router_repo, "enabled", lambda: False)
    direct = router_module._codex_provider(
        key, "https://api.router.com/v1", repository_attribution=True
    )
    assert 'base_url = "https://api.router.com/v1"' in direct

    monkeypatch.setattr(router_repo, "enabled", lambda: True)
    monkeypatch.setattr(router_repo, "ensure_running", lambda: True)
    wrapped = router_module._codex_provider(
        key, "https://api.router.com/v1", repository_attribution=True
    )
    assert 'base_url = "http://127.0.0.1:47317/u/api.router.com/v1"' in wrapped


@pytest.fixture(autouse=True)
def background_opt_in(isolated_config):
    """The existing lifecycle tests describe explicitly opted-in users."""
    marker = schedule._enabled_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


def _executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


class _Launchd:
    """launchctl as seen by the module: which jobs are loaded, and every call."""

    def __init__(self):
        self.loaded: set[str] = set()
        self.calls: list[tuple[str, ...]] = []

    def run(self, *command: str) -> bool:
        self.calls.append(command)
        if command[0] != "launchctl":
            return True
        verb, target = command[1], command[-1]
        if verb == "print":
            return target.rsplit("/", 1)[-1] in self.loaded
        if verb == "bootstrap":
            self.loaded.add(plistlib.loads(open(target, "rb").read())["Label"])
        if verb == "bootout":
            self.loaded.discard(target.rsplit("/", 1)[-1])
        return True

    def verbs(self) -> list[tuple[str, str]]:
        return [(c[1], c[-1].rsplit("/", 1)[-1]) for c in self.calls]


@pytest.fixture
def launchd(monkeypatch, tmp_path) -> _Launchd:
    """macOS with the agent shipped beside a linked `ramp`."""
    monkeypatch.delenv(schedule.SCHEDULED_ENV, raising=False)
    for name in schedule.CA_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    lib = tmp_path / "lib" / "ramp-cli"
    real = _executable(lib / "ramp-darwin-arm64")
    _executable(lib / schedule.AGENT_BINARY)
    link = tmp_path / "bin" / "ramp"
    link.parent.mkdir()
    link.symlink_to(real)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(schedule.sys, "platform", "darwin")
    monkeypatch.setattr(schedule, "ramp_executable", lambda: link)
    fake = _Launchd()
    monkeypatch.setattr(schedule, "_run", fake.run)
    # Never read the developer's real launchd state.
    monkeypatch.setattr(schedule, "_launchd_disabled", lambda _label: False)
    monkeypatch.setattr(
        schedule, "_launchd_running", lambda t: t.rsplit("/", 1)[-1] in fake.loaded
    )
    return fake


def _plist(tmp_path) -> dict:
    path = tmp_path / "Library/LaunchAgents/com.ramp.router.plist"
    return plistlib.loads(path.read_bytes())


def _tagging(on: bool) -> None:
    path = router_repo.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"enabled": on}))


def test_agent_is_found_beside_the_real_ramp_file(monkeypatch, tmp_path, launchd):
    agent = schedule.agent_executable()
    assert agent == tmp_path / "lib" / "ramp-cli" / schedule.AGENT_BINARY

    # A development build overrides the bundled one; a missing one means none.
    dev = _executable(tmp_path / "dev" / "ramp-router-agent")
    monkeypatch.setenv(schedule.AGENT_PATH_ENV, str(dev))
    assert schedule.agent_executable() == dev
    monkeypatch.setenv(schedule.AGENT_PATH_ENV, str(tmp_path / "nowhere"))
    assert schedule.agent_executable() is None


def test_background_mode_is_low_priority_and_listens_on_nothing(tmp_path, launchd):
    _REAL_INSTALL()

    plist = _plist(tmp_path)
    arguments = plist["ProgramArguments"]
    assert arguments[:2] == [str(schedule.agent_executable()), "serve"]
    assert arguments[arguments.index("--sync-manifest") + 1] == str(
        schedule.manifest_path()
    )
    assert arguments[arguments.index("--sync-interval") + 1] == "300"
    assert arguments[arguments.index("--sync-ramp") + 1] == str(tmp_path / "bin/ramp")
    assert "--port" not in arguments
    assert plist["ProcessType"] == "Background"
    assert plist["LowPriorityIO"] is True
    assert "Sockets" not in plist
    assert plist["KeepAlive"] is True
    # A refresh the agent started survives an agent restart.
    assert plist["AbandonProcessGroup"] is True
    assert ("bootstrap", "com.ramp.router.plist") in launchd.verbs()
    # The plain 15-minute job is never installed beside the agent.
    assert not (tmp_path / "Library/LaunchAgents/com.ramp.router-sync.plist").exists()

    # Nothing changed: nothing reloads.
    launchd.calls.clear()
    _REAL_INSTALL()
    assert [verb for verb, _ in launchd.verbs()] == ["print"]


def test_background_is_off_until_explicitly_enabled(monkeypatch, tmp_path, launchd):
    schedule._enabled_marker().unlink()
    _REAL_INSTALL()  # configure/refresh/update share this path
    assert not schedule.background_enabled()
    assert launchd.calls == []
    assert not schedule.env_file_path().exists()

    monkeypatch.setattr(schedule, "install", _REAL_INSTALL)
    schedule.enable_background()
    assert schedule.background_enabled()
    assert launchd.loaded == {schedule.AGENT_LABEL}

    # A new invocation, even under another config root, retains the preference.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "another-config"))
    _REAL_INSTALL()
    assert schedule.background_enabled()
    assert launchd.loaded == {schedule.AGENT_LABEL}

    schedule.disable_background()
    assert not schedule.background_enabled()
    assert launchd.loaded == set()
    assert not schedule.manifest_path().exists()
    launchd.calls.clear()
    _REAL_INSTALL()
    monkeypatch.setenv(schedule.SCHEDULED_ENV, "1")
    _REAL_INSTALL()
    assert launchd.calls == []
    assert not schedule.background_enabled()

    monkeypatch.delenv(schedule.SCHEDULED_ENV)
    schedule.enable_background()
    assert schedule.background_enabled()
    assert launchd.loaded == {schedule.AGENT_LABEL}


def test_default_off_removes_a_service_from_a_preference_less_build(launchd):
    _REAL_INSTALL()
    assert launchd.loaded == {schedule.AGENT_LABEL}
    schedule._enabled_marker().unlink()
    _REAL_INSTALL()
    assert launchd.loaded == set()
    assert not schedule.background_enabled()


def test_stale_disabled_cleanup_cannot_undo_a_new_enable(monkeypatch, launchd):
    _REAL_INSTALL()
    schedule._remove(opt_out=True, only_if_disabled=True)
    assert schedule.background_enabled()
    assert launchd.loaded == {schedule.AGENT_LABEL}


@pytest.mark.parametrize(
    "override", ["dev/ramp-router-agent", "~/dev/ramp-router-agent"]
)
def test_agent_override_is_absolute_in_the_saved_job_environment(
    monkeypatch, tmp_path, launchd, override
):
    agent = _executable(tmp_path / "dev" / schedule.AGENT_BINARY)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(schedule.AGENT_PATH_ENV, override)
    _REAL_INSTALL()
    env_file = schedule.env_file_path()
    saved = json.loads(env_file.read_text())
    assert saved[schedule.AGENT_PATH_ENV] == str(agent)

    # The service runs outside the installing shell's working directory.
    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.delenv(schedule.AGENT_PATH_ENV)
    schedule.load_job_environment(str(env_file))
    monkeypatch.setenv(schedule.SCHEDULED_ENV, "1")
    _REAL_INSTALL()

    assert schedule.agent_executable() == agent
    assert launchd.loaded == {schedule.AGENT_LABEL}
    assert _plist(tmp_path)["ProgramArguments"][0] == str(agent)
    assert not schedule._job_installed()


def test_sync_opt_out_removes_the_agent_and_its_manifest(
    monkeypatch, tmp_path, launchd
):
    _REAL_INSTALL()
    schedule.write_manifest({"connections": []})

    monkeypatch.setenv("RAMP_NO_SESSION_SYNC", "1")
    _REAL_INSTALL()

    assert launchd.loaded == set()
    assert not (tmp_path / "Library/LaunchAgents/com.ramp.router.plist").exists()
    assert not schedule.manifest_path().exists()  # no copy of the keys left
    assert schedule.opted_out()


def test_tagging_switches_the_same_agent_to_a_listener(tmp_path, launchd):
    _REAL_INSTALL()
    _tagging(True)
    launchd.calls.clear()

    _REAL_UPDATE_AGENT()

    plist = _plist(tmp_path)
    arguments = plist["ProgramArguments"]
    assert arguments[arguments.index("--port") + 1] == "47317"
    assert "--sync-manifest" in arguments  # sync keeps running
    assert "ProcessType" not in plist  # serving requests: normal priority
    listener = plist["Sockets"]["Listeners"]
    assert (listener["SockNodeName"], listener["SockServiceName"]) == (
        "127.0.0.1",
        "47317",
    )
    assert [verb for verb, _ in launchd.verbs()] == [
        "print",
        "bootout",
        "bootstrap",
        "kickstart",
    ]

    _tagging(False)
    _REAL_UPDATE_AGENT()
    assert _plist(tmp_path)["ProcessType"] == "Background"


def test_sync_opt_out_keeps_a_listener_and_removes_a_background_agent(
    monkeypatch, tmp_path, launchd
):
    agent_plist = tmp_path / "Library/LaunchAgents/com.ramp.router.plist"
    _tagging(True)
    _REAL_INSTALL()
    schedule.write_manifest({"connections": []})

    # Opted out with tagging on: the agent stays to serve requests, and the
    # marker it checks stops its background sync.
    monkeypatch.setenv("RAMP_NO_SESSION_SYNC", "1")
    _REAL_INSTALL()
    assert agent_plist.exists()
    assert "--port" in _plist(tmp_path)["ProgramArguments"]
    assert schedule.opted_out()
    assert not schedule.manifest_path().exists()  # no copy of the keys left

    # Tagging off too: nothing is left for the agent to do.
    monkeypatch.delenv("RAMP_NO_SESSION_SYNC")
    _tagging(False)
    _REAL_UPDATE_AGENT()
    assert not agent_plist.exists()
    assert launchd.loaded == set()

    # Enabling tagging later never opts back into sync.
    _tagging(True)
    _REAL_UPDATE_AGENT()
    assert agent_plist.exists()
    assert schedule.opted_out()


def test_install_without_the_agent_falls_back_to_the_plain_job(
    monkeypatch, tmp_path, launchd
):
    _REAL_INSTALL()
    assert "com.ramp.router" in launchd.loaded

    # Another ramp without the agent (a dev checkout) keeps the installed agent
    # but points its sync at this ramp and this configuration root.
    agent = schedule.agent_executable()
    other = _executable(tmp_path / "dev/ramp")
    monkeypatch.setattr(schedule, "ramp_executable", lambda: other)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "other-config"))
    _REAL_INSTALL()
    arguments = _plist(tmp_path)["ProgramArguments"]
    assert launchd.loaded == {"com.ramp.router"}
    assert arguments[0] == str(agent)
    assert arguments[arguments.index("--sync-ramp") + 1] == str(other)
    assert arguments[arguments.index("--sync-manifest") + 1] == str(
        schedule.manifest_path()
    )
    assert schedule.agent_executable() == agent  # so syncs write its manifest

    # A stopped agent is started again rather than left as is.
    launchd.loaded.clear()
    _REAL_INSTALL()
    assert launchd.loaded == {"com.ramp.router"}
    monkeypatch.setattr(schedule, "ramp_executable", lambda: tmp_path / "bin/ramp")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    # An install that no longer ships the agent: the plain job replaces it.
    (tmp_path / "lib/ramp-cli" / schedule.AGENT_BINARY).unlink()
    _REAL_INSTALL()
    assert launchd.loaded == {"com.ramp.router-sync"}
    assert not (tmp_path / "Library/LaunchAgents/com.ramp.router.plist").exists()

    # And back: the agent replaces the plain job.
    _executable(tmp_path / "lib/ramp-cli" / schedule.AGENT_BINARY)
    _REAL_INSTALL()
    assert launchd.loaded == {"com.ramp.router"}
    assert not (tmp_path / "Library/LaunchAgents/com.ramp.router-sync.plist").exists()


def test_scheduled_fallback_defers_migration_until_it_is_safe(
    monkeypatch, tmp_path, launchd
):
    agent = tmp_path / "lib/ramp-cli" / schedule.AGENT_BINARY
    agent.unlink()
    _REAL_INSTALL()
    assert launchd.loaded == {schedule.LAUNCHD_LABEL}
    _executable(agent)
    launchd.calls.clear()

    monkeypatch.setenv(schedule.SCHEDULED_ENV, "1")
    _REAL_INSTALL()
    assert launchd.loaded == {schedule.LAUNCHD_LABEL}
    assert launchd.calls == []  # never boot out the refresh calling install

    monkeypatch.delenv(schedule.SCHEDULED_ENV)
    _REAL_INSTALL()
    assert launchd.loaded == {schedule.AGENT_LABEL}
    verbs = [verb for verb, _ in launchd.verbs()]
    assert verbs.index("bootstrap") < verbs.index("bootout")


def test_failed_agent_start_keeps_the_fallback_job(monkeypatch, tmp_path, launchd):
    agent = tmp_path / "lib/ramp-cli" / schedule.AGENT_BINARY
    agent.unlink()
    _REAL_INSTALL()
    _executable(agent)
    monkeypatch.setattr(schedule, "_launchd_disabled", lambda _label: True)

    _REAL_INSTALL()

    assert launchd.loaded == {schedule.LAUNCHD_LABEL}
    assert schedule._job_installed()


def test_uninstall_removes_the_agent_and_its_manifest(tmp_path, launchd):
    _tagging(True)
    _REAL_INSTALL()
    schedule.write_manifest({"connections": []})

    _REAL_UNINSTALL()

    assert launchd.loaded == set()
    assert not (tmp_path / "Library/LaunchAgents/com.ramp.router.plist").exists()
    assert not schedule.manifest_path().exists()


def test_it_ca_bundles_reach_the_agent_and_are_remembered(
    monkeypatch, tmp_path, launchd
):
    bundle = tmp_path / "corp-ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", str(bundle))
    _REAL_INSTALL()
    environment = _plist(tmp_path)["EnvironmentVariables"]
    assert environment[schedule.EXTRA_CA_ENV] == str(bundle)
    # Never SSL_CERT_FILE itself: on Linux that replaces the system store.
    assert "SSL_CERT_FILE" not in environment

    # A later run from a GUI app or hook without the variable keeps it.
    monkeypatch.delenv("NODE_EXTRA_CA_CERTS")
    assert schedule.ca_files() == [str(bundle)]
    bundle.unlink()
    assert schedule.ca_files() == []


def test_systemd_units_follow_the_same_two_modes(tmp_path):
    agent, ramp = tmp_path / "ramp-router-agent", tmp_path / 'my "ramp" 100%'

    service, socket = schedule.render_agent_systemd(agent, ramp, None)
    assert socket is None
    assert "Nice=10\nIOSchedulingClass=idle\n" in service
    assert "KillMode=process\n" in service
    assert '"--sync-ramp" "' + str(tmp_path) + '/my \\"ramp\\" 100%%"' in service

    service, socket = schedule.render_agent_systemd(agent, ramp, 47999)
    assert "Nice=" not in service
    assert '"--port" "47999"' in service
    assert "ListenStream=127.0.0.1:47999\n" in socket


@pytest.mark.parametrize("preference", [None, "1"])
def test_agent_sync_honors_the_users_update_preference(
    monkeypatch, tmp_path, launchd, preference
):
    monkeypatch.delenv("RAMP_NO_UPDATE_CHECK", raising=False)
    if preference:
        monkeypatch.setenv("RAMP_NO_UPDATE_CHECK", preference)
    _REAL_INSTALL()
    environment = _plist(tmp_path)["EnvironmentVariables"]
    assert "RAMP_NO_UPDATE_CHECK" not in environment
    schedule.load_job_environment(str(schedule.env_file_path()))
    assert os.environ.get("RAMP_NO_UPDATE_CHECK") == preference


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("listen", [None, 47999])
def test_relative_xdg_paths_are_absolute_in_the_service(
    monkeypatch, tmp_path, launchd, platform, listen
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-config")
    agent, ramp = schedule.agent_executable(), schedule.ramp_executable()
    arguments = schedule._agent_arguments(agent, ramp, listen)
    flags = ["--sync-env-file", "--sync-manifest", "--sync-log", "--sync-opt-out"]
    if listen is not None:
        flags += ["--settings", "--log"]
    for flag in flags:
        assert Path(arguments[arguments.index(flag) + 1]).is_absolute()
    assert arguments[arguments.index("--sync-env-file") + 1] == str(
        tmp_path / "relative-config/ramp/router-sync-env.json"
    )
    if platform == "darwin":
        plist = plistlib.loads(schedule.render_agent_plist(agent, ramp, listen))
        assert plist["ProgramArguments"] == arguments
        assert Path(plist["StandardOutPath"]).is_absolute()
        assert Path(plist["StandardErrorPath"]).is_absolute()
    else:
        service, _ = schedule.render_agent_systemd(agent, ramp, listen)
        assert f"StandardOutput=append:{tmp_path}/relative-config/ramp/" in service
        if listen is not None:
            for flag in ("--settings", "--log"):
                assert f'"{flag}" "{arguments[arguments.index(flag) + 1]}"' in service


def test_systemd_escapes_dollars_only_in_exec_arguments(monkeypatch, tmp_path):
    root = tmp_path / "${literal}"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    monkeypatch.setattr(schedule, "_agent_environment", lambda: {"EXAMPLE": "$literal"})

    service, _ = schedule.render_agent_systemd(root / "agent", root / "ramp", None)

    command = next(
        line for line in service.splitlines() if line.startswith("ExecStart=")
    )
    assert "${literal}" not in command.replace("$${literal}", "")
    assert f'"{tmp_path}/$${{literal}}/ramp"' in command
    assert f'"{tmp_path}/$${{literal}}/ramp/router-sync-env.json"' in command
    # Environment and output paths do not undergo ExecStart's dollar expansion.
    assert 'Environment="EXAMPLE=$literal"' in service
    assert f"StandardOutput=append:{root}/ramp/router-agent.log" in service


def test_manifest_lists_each_version_request_with_what_was_applied(
    monkeypatch, tmp_path, launchd
):
    with_defaults = router_module._SyncConnection(
        "https://router.test/v1", "key-a", frozenset({"https://ramp.test"})
    )
    revoked = router_module._SyncConnection("https://router.test/v1", "key-b")
    now = 1_000_000.0
    monkeypatch.setattr(router_module.time, "time", lambda: now)
    schedule.save_state(
        {
            "versions": {with_defaults.digest: "v7|d3"},
            "revoked": {revoked.digest: now - 60},
            "last_full_refresh": now - 3600,
        }
    )

    router_module._write_agent_manifest([with_defaults, revoked])

    path = schedule.manifest_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    manifest = json.loads(path.read_text())
    assert manifest["due_at"] == now - 3600 + schedule.FULL_REFRESH_SECONDS
    first, second = manifest["connections"]
    assert first["checks"] == [
        {"kind": "sync", "url": "https://router.test/v1/client/sync-version"},
        {
            "kind": "defaults",
            "url": "https://ramp.test/self-service/coding-agent-settings",
        },
    ]
    assert first["headers"]["Authorization"] == "Bearer key-a"
    assert first["expected"] == ["v7", "d3"]
    assert first["skip_until"] is None
    assert second["expected"] is None  # never applied: any answer is a change
    assert second["skip_until"] == now - 60 + schedule.FULL_REFRESH_SECONDS

    # A failed refresh backing off, or a Cowork pass still pending, sets when
    # the agent runs the sync regardless.
    schedule.save_state({"retry_after": now + 600})
    router_module._write_agent_manifest([with_defaults])
    assert json.loads(path.read_text())["due_at"] == now + 600
    # A pending Cowork pass waits for Claude Desktop to close: the agent
    # watches its processes. With Desktop closed the pass already ran this
    # sync, so a failing one waits for the next sync, not every tick.
    schedule.save_state({"cowork_pending": True, "retry_after": now + 600})
    monkeypatch.setattr(router_module.claude_cowork, "claude_pids", lambda: [4242])
    router_module._write_agent_manifest([with_defaults])
    manifest = json.loads(path.read_text())
    assert manifest["due_at"] == now + 600
    assert manifest["cowork_wait_pids"] == [4242]
    monkeypatch.setattr(router_module.claude_cowork, "claude_pids", lambda: [])
    router_module._write_agent_manifest([with_defaults])
    assert json.loads(path.read_text())["due_at"] == now + 600


@pytest.mark.parametrize("state", [{}, {"retry_after": 0, "cowork_pending": True}])
def test_no_refreshable_connections_stay_idle(monkeypatch, launchd, state):
    now = 1_000_000.0
    monkeypatch.setattr(router_module.time, "time", lambda: now)
    schedule.save_state(state)

    router_module._write_agent_manifest([])

    manifest = json.loads(schedule.manifest_path().read_text())
    assert manifest["connections"] == []
    assert manifest["due_at"] == now + schedule.FULL_REFRESH_SECONDS


def test_no_manifest_without_the_agent(monkeypatch, tmp_path, launchd):
    (tmp_path / "lib/ramp-cli" / schedule.AGENT_BINARY).unlink()
    router_module._write_agent_manifest([])
    assert not schedule.manifest_path().exists()


@pytest.mark.parametrize("cooldown_offset", [-60, 7200])
@pytest.mark.parametrize("retry_delay", [3600, 86400])
def test_manifest_legacy_cooldown_respects_retry_backoff(
    monkeypatch, launchd, cooldown_offset, retry_delay
):
    now = 1_000_000.0
    monkeypatch.setattr(router_module.time, "time", lambda: now)
    monkeypatch.setattr(
        router_module.router_sync_module,
        "cooldown_due_at",
        lambda: now + cooldown_offset,
    )
    schedule.save_state({"retry_after": now + retry_delay})
    router_module._write_agent_manifest(
        [router_module._SyncConnection("https://router.test/v1", "key")]
    )
    manifest = json.loads(schedule.manifest_path().read_text())
    assert manifest["due_at"] == now + retry_delay
    assert manifest["unsupported_due_at"] == now + max(cooldown_offset, retry_delay)


def test_an_updated_binary_reloads_the_agent(monkeypatch, tmp_path, launchd):
    monkeypatch.setattr(schedule, "_launchd_disabled", lambda _label: False)
    _REAL_INSTALL()
    launchd.calls.clear()

    # An update replaces the binary in place: same path, new file.
    agent = tmp_path / "lib/ramp-cli" / schedule.AGENT_BINARY
    agent.write_text("#!/bin/sh\n# a newer build\n")
    _REAL_INSTALL()

    assert [verb for verb, _ in launchd.verbs()] == [
        "print",
        "bootout",
        "bootstrap",
        "kickstart",
    ]
    arguments = _plist(tmp_path)["ProgramArguments"]
    assert arguments[arguments.index("--build") + 1] == schedule.agent_build(agent)


def test_an_agent_turned_off_in_login_items_is_reported_not_forced(
    monkeypatch, tmp_path, launchd
):
    monkeypatch.setattr(schedule, "_launchd_disabled", lambda _label: True)
    _REAL_INSTALL()

    # Written, but not loaded: only the user or an MDM profile turns it on.
    assert (tmp_path / "Library/LaunchAgents/com.ramp.router.plist").exists()
    assert "com.ramp.router" not in launchd.loaded
    assert schedule.agent_state() == schedule.AGENT_DISABLED
    log = (schedule.log_path()).read_text()
    assert "Login Items" in log

    monkeypatch.setattr(schedule, "_launchd_disabled", lambda _label: False)
    assert schedule.agent_state() == schedule.AGENT_NOT_LOADED
    _REAL_INSTALL()
    assert schedule.agent_state() == schedule.AGENT_RUNNING


@pytest.mark.parametrize(
    ("returncode", "output", "running"),
    [
        (0, "\tstate = running\n\tpid = 4242\n", True),
        (0, "\tstate = not running\n", False),
        (113, "", False),
    ],
)
def test_launchd_running_needs_a_pid(monkeypatch, returncode, output, running):
    result = subprocess.CompletedProcess([], returncode, stdout=output)
    monkeypatch.setattr(schedule.subprocess, "run", lambda *a, **k: result)
    assert schedule._launchd_running("gui/501/com.ramp.router") is running


def test_homebrew_agent_path_follows_upgrades(tmp_path):
    cellar = _executable(
        tmp_path / "Cellar/ramp-cli/1.0/libexec" / schedule.AGENT_BINARY
    )
    (tmp_path / "opt").mkdir()
    (tmp_path / "opt/ramp-cli").symlink_to(tmp_path / "Cellar/ramp-cli/1.0")
    stable = tmp_path / "opt/ramp-cli/libexec" / schedule.AGENT_BINARY
    assert schedule._homebrew_opt_path(cellar) == stable
    other = _executable(tmp_path / "lib" / schedule.AGENT_BINARY)
    assert schedule._homebrew_opt_path(other) == other


def test_launchd_disabled_list_is_parsed(monkeypatch):
    output = (
        "\tdisabled services = {\n"
        '\t\t"com.ramp.router" => disabled\n'
        '\t\t"com.example.on" => enabled\n'
        '\t\t"com.example.new" => true\n'
        "\t}\n"
    )

    class Result:
        stdout = output

    monkeypatch.setattr(schedule.subprocess, "run", lambda *a, **k: Result())
    assert schedule._launchd_disabled("com.ramp.router")
    assert not schedule._launchd_disabled("com.example.on")
    assert schedule._launchd_disabled("com.example.new")
    assert not schedule._launchd_disabled("com.ramp.router-sync")


@pytest.mark.parametrize("command", ["enable", "disable"])
def test_agent_commands_control_both_sync_and_tagging(
    monkeypatch, tmp_path, launchd, command
):
    monkeypatch.delenv(schedule.SCHEDULED_ENV, raising=False)
    monkeypatch.setenv("RAMP_NO_SESSION_SYNC", "1")
    _REAL_INSTALL()
    monkeypatch.delenv("RAMP_NO_SESSION_SYNC")
    assert schedule.opted_out()
    monkeypatch.setattr(schedule, "install", _REAL_INSTALL)
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [object()]
    )
    monkeypatch.setattr(router_repo, "ensure_running", lambda: True)
    monkeypatch.setattr(router_module, "configured_router_clients", lambda: [])
    monkeypatch.setattr(router_module, "clients_using_repo_helper", lambda: [])
    # A successful refresh reaches the scheduler-install path, not just a no-op.
    monkeypatch.setattr(
        router_module,
        "_refresh_cowork_model_selections",
        lambda **_kw: {"client": "cowork", "skipped_while_running": True},
    )
    if command == "disable":
        _tagging(True)
    result = CliRunner().invoke(cli, ["--agent", "router", "agent", command])
    assert result.exit_code == 0, result.output
    assert schedule.opted_out() == (command == "disable")
    assert schedule.background_enabled() == (command == "enable")
    assert router_repo.enabled() == (command == "enable")
    assert not schedule.manifest_path().exists()
    if command == "enable":
        assert "--port" in _plist(tmp_path)["ProgramArguments"]


def test_systemd_port_change_restarts_socket_before_service(monkeypatch, tmp_path):
    unit_dir = tmp_path / "systemd" / "user"
    monkeypatch.setattr(schedule, "_systemd_dir", lambda: unit_dir)
    monkeypatch.setattr(schedule, "_systemd_manager_paths", lambda: (unit_dir,))
    calls = []
    monkeypatch.setattr(schedule, "_run", lambda *args: calls.append(args) or True)
    name = f"{schedule.AGENT_SYSTEMD_NAME}.service"
    socket_name = f"{schedule.AGENT_SYSTEMD_NAME}.socket"
    agent, ramp = tmp_path / "agent", tmp_path / "ramp"
    service, socket = schedule.render_agent_systemd(agent, ramp, 47317)
    schedule._install_agent_systemd(service, socket)
    calls.clear()
    schedule._install_agent_systemd(service, socket)
    assert not any("restart" in call or "stop" in call for call in calls)

    calls.clear()
    # Changing the service alone must not interrupt the listening socket.
    schedule._install_agent_systemd(service + "# new build\n", socket)
    assert ("systemctl", "--user", "restart", name) in calls
    assert ("systemctl", "--user", "restart", socket_name) not in calls

    calls.clear()
    schedule._install_agent_systemd(*schedule.render_agent_systemd(agent, ramp, 47999))
    stop = calls.index(("systemctl", "--user", "stop", name))
    rebind = calls.index(("systemctl", "--user", "restart", socket_name))
    start = calls.index(("systemctl", "--user", "restart", name))
    assert stop < rebind < start
    assert "ListenStream=127.0.0.1:47999" in (unit_dir / socket_name).read_text()


def test_tagging_does_not_opt_default_off_users_into_sync(
    monkeypatch, tmp_path, launchd
):
    schedule._enabled_marker().unlink()
    _tagging(True)
    _REAL_UPDATE_AGENT()
    assert not schedule.background_enabled()
    assert schedule.opted_out()
    assert "--port" in _plist(tmp_path)["ProgramArguments"]
    _REAL_INSTALL()
    assert "--port" in _plist(tmp_path)["ProgramArguments"]
    assert not schedule.background_enabled()
    _tagging(False)
    _REAL_UPDATE_AGENT()
    assert not schedule.agent_installed()


def test_sync_enable_disable_preserves_the_tagging_listener(
    monkeypatch, tmp_path, launchd
):
    schedule._enabled_marker().unlink()
    _tagging(True)
    _REAL_UPDATE_AGENT()
    monkeypatch.setattr(schedule, "install", _REAL_INSTALL)
    schedule.enable_background()
    assert schedule.background_enabled()
    assert "--port" in _plist(tmp_path)["ProgramArguments"]
    schedule.disable_background()
    assert not schedule.background_enabled()
    assert schedule.opted_out()
    assert "--port" in _plist(tmp_path)["ProgramArguments"]


@pytest.mark.parametrize("failure", ["none", "partial", "before"])
def test_disable_keeps_listener_through_refresh(
    monkeypatch, tmp_path, launchd, failure
):
    schedule._enabled_marker().unlink()
    _tagging(True)
    _REAL_UPDATE_AGENT()
    remaining = ["claude-code", "codex"]

    def refresh(_ctx, _fmt, **_kwargs):
        assert not router_repo.enabled()
        assert router_repo.load_settings()["keep_listening"]
        _REAL_UPDATE_AGENT()
        assert "--port" in _plist(tmp_path)["ProgramArguments"]
        assert ("bootout", schedule.AGENT_LABEL) not in launchd.verbs()
        if failure == "before":
            raise click.ClickException("refresh failed")
        remaining.pop()
        if failure == "partial":
            raise click.ClickException("refresh failed")
        remaining.clear()
        return []

    monkeypatch.setattr(router_repo, "_refresh", refresh)
    monkeypatch.setattr(router_module, "clients_using_repo_helper", lambda: remaining)
    monkeypatch.setattr(router_repo, "uninstall_service", _REAL_UPDATE_AGENT)
    result = CliRunner().invoke(cli, ["--agent", "router", "agent", "disable"])
    assert result.exit_code == (0 if failure == "none" else 1), result.output
    assert not router_repo.enabled()
    if remaining:
        assert router_repo.load_settings()["keep_listening"]
        assert "--port" in _plist(tmp_path)["ProgramArguments"]
        assert ("bootout", schedule.AGENT_LABEL) not in launchd.verbs()
    else:
        assert "keep_listening" not in router_repo.load_settings()
        assert not schedule.agent_installed()


@pytest.mark.parametrize("tagging_on", [True, False])
@pytest.mark.parametrize(
    "failure",
    [
        "none",
        "detach-before",
        "detach-partial",
        "detach-complete",
        "stuck",
        "start",
        "attach-partial",
    ],
)
def test_port_change_never_strands_a_client(monkeypatch, tagging_on, failure):
    direct = "https://api.router.com/v1"
    old_port, new_port = 47317, 47999
    previous = {"enabled": tagging_on, "port": old_port, "marker": "preserved"}
    if not tagging_on:
        previous["keep_listening"] = True
    router_repo._save_settings(previous)
    endpoints = {
        client: router_repo.wrap_url(direct, old_port)
        for client in ("claude-code", "codex")
    }
    live_port = old_port
    refreshes = 0

    def assert_connected():
        for url in endpoints.values():
            assert url == direct or url == router_repo.wrap_url(direct, live_port)

    def ensure_running():
        nonlocal live_port
        # Every client must leave the old listener before it can be stopped.
        assert all(url == direct for url in endpoints.values())
        live_port = new_port
        return failure != "start"

    def restore_service():
        nonlocal live_port
        live_port = router_repo.listener_port()

    def refresh(_ctx, _fmt, **_kwargs):
        nonlocal refreshes
        refreshes += 1
        assert_connected()
        if refreshes == 1:
            assert not router_repo.enabled()
            assert router_repo.listener_port() == old_port
            if failure == "detach-before":
                raise click.ClickException("model discovery failed")
            target = direct
        else:
            assert router_repo.enabled()
            assert router_repo.listener_port() == new_port
            target = router_repo.wrap_url(direct, new_port)
        for i, client in enumerate(endpoints):
            if failure == "stuck" and i == 1:
                continue
            endpoints[client] = target
            assert_connected()
            if i == 0 and (
                (refreshes == 1 and failure == "detach-partial")
                or (refreshes == 2 and failure == "attach-partial")
            ):
                raise click.ClickException("model discovery failed")
        if refreshes == 1 and failure == "detach-complete":
            raise click.ClickException("model discovery failed")
        return [{"client": client} for client in endpoints]

    monkeypatch.setattr(router_repo, "service_supported", lambda: True)
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [object()]
    )
    monkeypatch.setattr(router_repo, "ensure_running", ensure_running)
    monkeypatch.setattr(router_repo, "uninstall_service", restore_service)
    monkeypatch.setattr(router_repo, "_refresh", refresh)
    monkeypatch.setattr(
        router_module,
        "clients_using_repo_helper",
        lambda: [
            client for client, url in endpoints.items() if router_repo.is_wrapped(url)
        ],
    )
    result = CliRunner().invoke(
        cli, ["--agent", "router", "agent", "enable", "--port", str(new_port)]
    )
    assert result.exit_code == (0 if failure == "none" else 1), result.output
    if failure != "none":
        expected = (
            "old helper"
            if failure == "stuck"
            else "did not start"
            if failure == "start"
            else "model discovery failed"
        )
        assert expected in result.output, repr(result.exception)
    if failure in {"none", "attach-partial"}:
        assert router_repo.port() == live_port == new_port
        assert router_repo.enabled()
        assert router_repo.load_settings()["marker"] == "preserved"
    else:
        assert router_repo.load_settings() == previous
        assert live_port == old_port
    assert_connected()


def test_health_authenticates_a_fresh_challenge_without_sending_the_key(monkeypatch):
    token = router_repo.health_token(create=True)
    assert token is not None
    path = router_repo.settings_path().with_name("router-agent-health-token")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert router_repo.health_token(create=True) == token
    challenges = []

    def get(url, *, headers, **kwargs):
        challenge = headers["x-ramp-health-challenge"]
        assert token not in str(headers)
        assert kwargs["trust_env"] is False
        challenges.append(challenge)
        proof = hmac.new(
            token.encode(),
            f"{router_repo.SERVICE_NAME}:47317:{challenge}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return httpx.Response(
            200,
            json={
                "service": router_repo.SERVICE_NAME,
                "health_proof": proof,
                "local_auth": True,
            },
        )

    monkeypatch.setattr(router_repo.httpx, "get", get)
    assert router_repo.health(47317) is not None
    assert router_repo.health(47317) is not None
    assert challenges[0] != challenges[1]
    assert router_repo.health(47999) is None  # proof is bound to the listening port


@pytest.mark.parametrize("proof", [None, "", "0" * 64, "é" * 64, 42])
def test_a_spoofed_health_endpoint_never_routes_clients_to_it(monkeypatch, proof):
    assert router_repo.health_token(create=True)
    router_repo._save_settings({"enabled": True})
    monkeypatch.setattr(
        router_repo.httpx,
        "get",
        lambda *a, **kw: httpx.Response(
            200, json={"service": router_repo.SERVICE_NAME, "health_proof": proof}
        ),
    )
    monkeypatch.setattr(router_repo, "service_supported", lambda: True)
    monkeypatch.setattr(router_repo, "_port_taken", lambda _: True)
    assert router_repo.health() is None
    assert not router_repo.ensure_running()
    direct = "https://api.router.com/v1"
    assert router_repo.attribution_url(direct) == direct


def test_health_key_rejects_unsafe_permissions_and_symlinks(tmp_path):
    assert router_repo.health() is None  # no preexisting private key
    assert router_repo.health_token(create=True)
    path = router_repo.settings_path().with_name("router-agent-health-token")
    path.chmod(0o644)
    assert router_repo.health_token() is None
    path.unlink()
    target = tmp_path / "other-key"
    target.write_text("0" * 64)
    target.chmod(0o600)
    path.symlink_to(target)
    assert router_repo.health_token(create=True) is None


def test_clear_manifest_reaches_the_installed_agents_root(
    monkeypatch, tmp_path, launchd
):
    _REAL_INSTALL()
    first = schedule.manifest_path()
    schedule.write_manifest({"connections": ["removed key"]})
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "other-config"))
    schedule.write_manifest({"connections": []})

    schedule.clear_manifest()

    assert not first.exists()
    assert not schedule.manifest_path().exists()


def test_disable_finishes_when_the_desktop_lock_is_unavailable(monkeypatch):
    @contextlib.contextmanager
    def unavailable():
        raise click.ClickException("Could not lock Claude Desktop configuration")
        yield

    monkeypatch.setattr(router_repo.sys, "platform", "darwin")
    monkeypatch.setattr(router_module.claude_cowork, "transaction_lock", unavailable)
    monkeypatch.setattr(router_repo, "_refresh", lambda *_a, **_k: [])
    monkeypatch.setattr(router_module, "clients_using_repo_helper", lambda: [])
    removed = []
    monkeypatch.setattr(router_repo, "uninstall_service", lambda: removed.append(1))
    _tagging(True)

    assert router_repo.disable(click.Context(cli)) == {
        "enabled": False,
        "still_using_helper": [],
        "refreshed": [],
    }
    assert "keep_listening" not in router_repo.load_settings()
    assert removed == [1]


def test_a_truncated_agent_plist_does_not_block_reconciliation(
    monkeypatch, tmp_path, launchd
):
    _REAL_INSTALL()
    plist = tmp_path / "Library/LaunchAgents/com.ramp.router.plist"
    plist.write_bytes(plist.read_bytes()[:80])
    assert schedule._installed_agent_arguments() == []
    schedule.clear_manifest()
    _REAL_INSTALL()
    assert "serve" in _plist(tmp_path)["ProgramArguments"]
