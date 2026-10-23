"""Linux service lifecycle must not follow the shell's changing config root."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

from ramp_cli.commands import router_schedule as schedule

_REAL_RUN = schedule._run


class _Units(set):
    def __init__(self):
        super().__init__()
        self.enabled = set()


@pytest.fixture
def systemd(monkeypatch, tmp_path, isolated_config):
    root = tmp_path / "original-config"
    units = root / "systemd" / "user"
    manager_paths = [units]
    calls = []
    active = _Units()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    monkeypatch.setattr(schedule.sys, "platform", "linux")
    monkeypatch.setattr(schedule.shutil, "which", lambda _name: "/usr/bin/systemctl")
    monkeypatch.setattr(schedule, "ramp_executable", lambda: tmp_path / "ramp")

    def run(*command):
        calls.append(command)
        action, name = command[2], command[-1]
        if action == "is-active":
            return name in active
        if action == "is-enabled":
            return name in active.enabled
        if action == "link":
            for source in command[3:]:
                link = manager_paths[0] / Path(source).name
                link.parent.mkdir(parents=True, exist_ok=True)
                if link.exists() or link.is_symlink():
                    if link.resolve() != Path(source).resolve():
                        return False
                else:
                    link.symlink_to(source)
        if action in ("enable", "restart"):
            if not any((path / name).exists() for path in manager_paths):
                return False  # changing the client's env cannot change UnitPath
            if action == "enable":
                active.enabled.add(name)
            if action == "restart" or "--now" in command:
                active.add(name)
        if action == "disable":
            active.enabled.discard(name)
            if "--now" in command:
                active.discard(name)
            for directory in manager_paths:
                link = directory / name
                if link.is_symlink():
                    link.unlink()
        return True

    def query(command, **kwargs):
        assert command[:3] == ["systemctl", "--user", "show"]
        if "--property=UnitPath" in command:
            return subprocess.CompletedProcess(
                command, 0, shlex.join(map(str, manager_paths)), ""
            )
        paths = [str(units / name) for name in command if (units / name).is_file()]
        return subprocess.CompletedProcess(command, 0, "\n".join(paths), "")

    monkeypatch.setattr(schedule, "_run", run)
    monkeypatch.setattr(schedule.subprocess, "run", query)
    return units, calls, active, manager_paths


def test_active_agent_is_reenabled_after_external_disable(systemd):
    _, calls, active, _ = systemd
    schedule._install_agent_systemd("unchanged service")
    active.enabled.remove("ramp-router.service")
    calls.clear()
    schedule._install_agent_systemd("unchanged service")
    assert "ramp-router.service" in active.enabled
    assert ("systemctl", "--user", "enable", "ramp-router.service") in calls


def test_enabled_timer_is_started_after_external_stop(systemd):
    _, calls, active, _ = systemd
    schedule._install_systemd()
    active.remove("ramp-router-sync.timer")
    calls.clear()
    schedule._install_systemd()
    assert "ramp-router-sync.timer" in active
    assert ("systemctl", "--user", "enable", "--now", "ramp-router-sync.timer") in calls


@pytest.mark.parametrize("kind", ["agent", "timer"])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("new_config", [None, "other-config"])
def test_status_and_disable_find_units_from_another_config_root(
    monkeypatch, tmp_path, systemd, kind, legacy, new_config
):
    units, calls, active, _ = systemd
    schedule.enable_background()  # install is isolated by the shared fixture
    if kind == "agent":
        schedule._install_agent_systemd("[Service]\nExecStart=/test/agent\n")
        names = ["ramp-router.service"]
    else:
        schedule._install_systemd()
        names = ["ramp-router-sync.timer", "ramp-router-sync.service"]
    assert all((units / name).exists() for name in names)
    if legacy:
        # A build before location receipts: recover the path from systemd.
        (schedule.router_sync.job_lock_dir() / "router-systemd-dir").unlink(
            missing_ok=True
        )
    else:
        # Recorded installs remain discoverable even without a user bus.
        monkeypatch.setattr(
            schedule.subprocess,
            "run",
            lambda *_a, **_kw: (_ for _ in ()).throw(OSError("no user bus")),
        )
    if new_config is None:
        monkeypatch.delenv("XDG_CONFIG_HOME")
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / new_config))
    state = schedule.background_status()
    assert state["service"] == kind
    assert state["state"] == ("running" if kind == "agent" else "scheduled")

    schedule.disable_background()
    assert ("systemctl", "--user", "disable", "--now", names[0]) in calls
    assert names[0] not in active
    assert all(not (units / name).exists() for name in names)
    assert schedule.background_status()["state"] == "not_installed"
    assert not schedule.background_enabled()


def test_repair_keeps_the_original_systemd_directory(monkeypatch, tmp_path, systemd):
    units, _, _, _ = systemd
    schedule._install_agent_systemd("first")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "other-config"))
    schedule._install_agent_systemd("updated")
    assert (units / "ramp-router.service").read_text() == "updated"
    assert not (tmp_path / "other-config/systemd/user/ramp-router.service").exists()


def test_systemctl_uses_the_installed_config_root(monkeypatch, tmp_path, systemd):
    units, _, _, manager_paths = systemd
    schedule._install_agent_systemd("first")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "other-config"))
    observed = []

    def run(command, **kwargs):
        if "--property=UnitPath" in command:
            return subprocess.CompletedProcess(
                command, 0, shlex.join(map(str, manager_paths)), ""
            )
        observed.append(kwargs["env"]["XDG_CONFIG_HOME"])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(schedule.subprocess, "run", run)
    assert _REAL_RUN("systemctl", "--user", "disable", "--now", "ramp-router.service")
    assert observed == [str(units.parent.parent)]


@pytest.mark.parametrize("kind", ["agent", "timer"])
@pytest.mark.parametrize("disable_first", [False, True])
def test_new_login_links_recorded_units_into_the_current_manager_path(
    monkeypatch, tmp_path, systemd, kind, disable_first
):
    units, calls, active, manager_paths = systemd
    schedule.enable_background()

    def install():
        if kind == "agent":
            schedule._install_agent_systemd("[Service]\nExecStart=/test/agent\n")
        else:
            schedule._install_systemd()

    names = (
        ["ramp-router.service"]
        if kind == "agent"
        else ["ramp-router-sync.service", "ramp-router-sync.timer"]
    )
    install()
    if disable_first:
        schedule.disable_background()
    active.clear()  # the original user manager stopped at logout
    current = tmp_path / "new login config" / "systemd" / "user"
    manager_paths[:] = [current]
    # The invoking shell is different from both the old and current manager.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "third-config"))
    schedule.enable_background()
    install()

    assert all((current / name).is_symlink() for name in names)
    assert all((current / name).resolve() == units / name for name in names)
    assert names[-1] in active
    assert schedule._systemd_dir() == units  # recorded storage remains stable
    schedule.disable_background()
    assert names[-1] not in active
    assert all(not (units / name).exists() for name in names)
    assert all(not (current / name).is_symlink() for name in names)


def test_systemctl_uses_current_manager_not_recorded_config_root(
    monkeypatch, tmp_path, systemd
):
    _, _, _, manager_paths = systemd
    schedule._install_agent_systemd("first")
    current = tmp_path / "new-manager" / "systemd" / "user"
    manager_paths[:] = [current]
    observed = []

    def run(command, **kwargs):
        if "--property=UnitPath" in command:
            return subprocess.CompletedProcess(
                command, 0, shlex.quote(str(current)), ""
            )
        observed.append(kwargs["env"]["XDG_CONFIG_HOME"])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(schedule.subprocess, "run", run)
    assert _REAL_RUN("systemctl", "--user", "enable", "ramp-router.service")
    assert observed == [str(current.parent.parent)]


@pytest.mark.parametrize("kind", ["agent", "timer"])
def test_unknown_manager_path_does_not_enable_units(monkeypatch, systemd, kind):
    _, calls, _, manager_paths = systemd
    manager_paths.clear()
    if kind == "agent":
        schedule._install_agent_systemd("service")
    else:
        schedule._install_systemd()
    assert not any(command[2] in ("link", "enable", "restart") for command in calls)


def test_link_conflict_preserves_foreign_unit_and_does_not_enable(tmp_path, systemd):
    units, calls, _, manager_paths = systemd
    schedule._install_agent_systemd("original")
    current = tmp_path / "new-manager" / "systemd" / "user"
    current.mkdir(parents=True)
    foreign = current / "ramp-router.service"
    foreign.write_text("foreign service")
    manager_paths[:] = [current]
    calls.clear()
    schedule._install_agent_systemd("updated")
    assert foreign.read_text() == "foreign service"
    assert not any(command[2] in ("enable", "restart") for command in calls)


def test_system_wide_paths_are_not_link_destinations(systemd):
    _, calls, _, manager_paths = systemd
    manager_paths[:] = [
        Path("/etc/systemd/user"),
        Path("/usr/lib/systemd/user"),
        Path("/run/systemd/user"),
    ]
    schedule._install_agent_systemd("service")
    assert not any(command[2] in ("link", "enable", "restart") for command in calls)


def test_listener_socket_is_linked_into_a_new_manager_search_path(tmp_path, systemd):
    units, calls, active, manager_paths = systemd
    schedule._install_agent_systemd("service", "socket")
    current = tmp_path / "new-manager" / "systemd" / "user"
    manager_paths[:] = [current]
    active.clear()
    schedule._install_agent_systemd("service", "socket")
    for name in ("ramp-router.service", "ramp-router.socket"):
        assert (current / name).resolve() == units / name
        assert name in active
        assert name in active.enabled
    schedule._uninstall_agent()
    for name in ("ramp-router.service", "ramp-router.socket"):
        assert not (current / name).is_symlink()
        assert not (units / name).exists()
