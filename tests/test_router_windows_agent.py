"""The Ramp Router agent on Windows: Task Scheduler, private files, and locks.

Most of these run everywhere with Windows calls faked; the ones marked
``windows_only`` exercise the real ACL and lock code on a Windows host.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

import ramp_cli.client.router_user as router_user
import ramp_cli.commands.router as router_module
import ramp_cli.commands.router_credentials as credentials
import ramp_cli.commands.router_local_auth as local_auth
import ramp_cli.commands.router_repo as router_repo
import ramp_cli.commands.router_schedule as schedule
import ramp_cli.commands.router_sync as router_sync
import ramp_cli.commands.router_winsec as winsec
from ramp_cli.main import cli

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="real Windows ACLs")

USER = "S-1-5-21-1-2-3-1001"
OTHER = "S-1-5-21-1-2-3-1002"
EVERYONE = "S-1-1-0"
FULL, READ, MODIFY = 0x1F01FF, 0x120089, 0x1301BF
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


# ---------------------------------------------------------------------------
# The private-file rule (the same one the native agent applies)
# ---------------------------------------------------------------------------


def test_profile_default_acl_is_private():
    aces = [
        (0, 0x10, FULL, winsec.SYSTEM_SID),
        (0, 0x10, FULL, winsec.ADMINISTRATORS_SID),
        (0, 0x10, FULL, USER),
    ]
    assert winsec.is_private(winsec.FILE, USER, aces, USER)
    assert winsec.is_private(winsec.DIRECTORY, USER, aces, USER)


@pytest.mark.parametrize("other", [OTHER, EVERYONE, "S-1-5-11", "S-1-5-32-545"])
def test_others_may_not_read_files_or_write_the_folder(other):
    read = [(0, 0, FULL, USER), (0, 0, READ, other)]
    assert not winsec.is_private(winsec.FILE, USER, read, USER)
    assert winsec.is_private(winsec.DIRECTORY, USER, read, USER)
    modify = [(0, 0, FULL, USER), (0, 0, MODIFY, other)]
    assert not winsec.is_private(winsec.DIRECTORY, USER, modify, USER)


def test_owner_null_dacl_and_unreadable_entries():
    mine = [(0, 0, FULL, USER)]
    assert not winsec.is_private(winsec.FILE, OTHER, mine, USER)
    # An elevated process of this user creates files owned by Administrators.
    assert winsec.is_private(winsec.FILE, winsec.ADMINISTRATORS_SID, mine, USER)
    assert not winsec.is_private(winsec.FILE, winsec.SYSTEM_SID, mine, USER)
    assert winsec.is_private(winsec.DIRECTORY, winsec.ADMINISTRATORS_SID, mine, USER)
    assert not winsec.is_private(winsec.FILE, USER, None, USER)
    deny = [(0, 0, FULL, USER), (1, 0, FULL, EVERYONE)]
    assert winsec.is_private(winsec.FILE, USER, deny, USER)
    inherit_only = [(0, 0, FULL, USER), (0, winsec.INHERIT_ONLY_ACE, FULL, EVERYONE)]
    assert winsec.is_private(winsec.FILE, USER, inherit_only, USER)
    for unknown in (4, 5, 9, 11):
        assert not winsec.is_private(
            winsec.FILE, USER, [(unknown, 0, FULL, USER)], USER
        )


# ---------------------------------------------------------------------------
# The Task Scheduler task
# ---------------------------------------------------------------------------


@pytest.fixture
def windows(monkeypatch, tmp_path):
    """Pretend to be Windows for the scheduling code, with schtasks faked."""
    monkeypatch.setattr(schedule.sys, "platform", "win32")
    monkeypatch.setenv("USERNAME", "kiran")
    monkeypatch.setattr(schedule.router_winsec, "current_user_sid", lambda: USER)
    calls: list[tuple[str, ...]] = []
    tasks: dict[str, str] = {}

    def run(*command, timeout=30):
        # Tools by absolute path (never the current directory's), recorded by name.
        program = command[0].replace("\\", "/")
        assert "/System32/" in program
        command = (Path(program).stem, *command[1:])
        calls.append(command)
        result = type("Result", (), {"returncode": 0, "stdout": b"", "stderr": b""})()
        if command[0] == "powershell":
            name = command[-1].split("TaskName -eq '")[1].split("'")[0]
            result.stdout = tasks.get(name, "Missing").encode()
        elif command[:2] == ("schtasks", "/Create"):
            xml_path = Path(command[command.index("/XML") + 1])
            assert xml_path.read_bytes().startswith(b"\xff\xfe")  # UTF-16 with a BOM
            tasks[command[3]] = "Ready"
        elif command[:2] == ("schtasks", "/Run"):
            if tasks.get(command[3]) == "Ready":
                tasks[command[3]] = "Running"
        elif command[:2] == ("schtasks", "/End"):
            if tasks.get(command[3]) == "Running":
                tasks[command[3]] = "Ready"
        elif command[:2] == ("schtasks", "/Delete"):
            tasks.pop(command[3], None)
        return result

    monkeypatch.setattr(schedule, "_windows_run", run)
    agent = tmp_path / "lib" / "ramp-router-agent.exe"
    ramp = tmp_path / "lib" / "ramp-windows-amd64.exe"
    agent.parent.mkdir()
    agent.write_bytes(b"agent")
    ramp.write_bytes(b"ramp")
    agent.chmod(0o755)
    return type(
        "Win", (), {"calls": calls, "tasks": tasks, "agent": agent, "ramp": ramp}
    )


def _task(xml: str) -> ET.Element:
    return ET.fromstring(xml.split("?>", 1)[1])


def test_task_starts_at_logon_as_the_user_without_a_password_or_limits(windows):
    root = _task(schedule.render_agent_task(windows.agent, windows.ramp, None, USER))
    triggers = root.find("t:Triggers", NS)
    assert triggers.find("t:LogonTrigger/t:UserId", NS).text == USER
    # Restarts a stopped agent within a minute while the user is logged on.
    assert triggers.find("t:TimeTrigger/t:Repetition/t:Interval", NS).text == "PT1M"
    principal = root.find("t:Principals/t:Principal", NS)
    assert principal.find("t:UserId", NS).text == USER
    assert principal.find("t:LogonType", NS).text == "InteractiveToken"
    assert principal.find("t:RunLevel", NS).text == "LeastPrivilege"
    settings = root.find("t:Settings", NS)
    assert settings.find("t:ExecutionTimeLimit", NS).text == "PT0S"
    assert settings.find("t:DisallowStartIfOnBatteries", NS).text == "false"
    assert settings.find("t:StopIfGoingOnBatteries", NS).text == "false"
    assert settings.find("t:MultipleInstancesPolicy", NS).text == "IgnoreNew"
    assert settings.find("t:RestartOnFailure/t:Interval", NS).text == "PT1M"
    assert settings.find("t:Priority", NS).text == "7"
    action = root.find("t:Actions/t:Exec", NS)
    assert action.find("t:Command", NS).text == str(windows.agent)
    arguments = action.find("t:Arguments", NS).text
    assert arguments.startswith("serve --build ")
    assert "--agent-log" in arguments and "--port" not in arguments


def test_serving_task_runs_at_normal_priority_and_quotes_paths(windows, monkeypatch):
    monkeypatch.setattr(router_repo, "health_token", lambda **_: "0" * 64)
    spaced = windows.agent.parent / "Program Files & more"
    spaced.mkdir()
    agent = spaced / "ramp-router-agent.exe"
    agent.write_bytes(b"agent")
    root = _task(schedule.render_agent_task(agent, windows.ramp, 47317, USER))
    assert root.find("t:Settings/t:Priority", NS).text == "5"
    action = root.find("t:Actions/t:Exec", NS)
    assert action.find("t:Command", NS).text == f'"{agent}"'
    assert "--port 47317" in action.find("t:Arguments", NS).text


def test_ca_bundles_reach_the_agent_as_an_argument(windows, monkeypatch, tmp_path):
    bundle = tmp_path / "corp-ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    for name in schedule.CA_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    arguments = schedule._agent_arguments(windows.agent, windows.ramp, None)
    assert arguments[arguments.index("--extra-ca-files") + 1] == str(bundle)
    root = _task(schedule.render_agent_task(windows.agent, windows.ramp, None, USER))
    assert "--extra-ca-files" in root.find("t:Actions/t:Exec/t:Arguments", NS).text


def test_install_registers_once_then_leaves_a_running_task_alone(windows):
    name = schedule.agent_task_name()
    assert name == "Ramp Router Agent (kiran)"
    schedule._install_agent_task(windows.agent, windows.ramp, None)
    assert windows.tasks[name] == "Running"
    created = [c for c in windows.calls if c[:2] == ("schtasks", "/Create")]
    assert len(created) == 1 and created[0][-1] == "/F"
    record = json.loads(schedule._task_record_path().read_text(encoding="utf-8"))
    assert record["name"] == name and record["arguments"][0] == str(windows.agent)
    assert schedule.agent_installed()
    assert schedule._installed_agent_arguments() == record["arguments"]
    assert schedule.agent_state() == schedule.AGENT_RUNNING
    # The temporary XML is gone.
    assert not list(router_sync.job_lock_dir().glob(".router-agent-task-*.xml"))

    windows.calls.clear()
    schedule._install_agent_task(windows.agent, windows.ramp, None)
    assert [c[0] for c in windows.calls] == ["powershell"]

    # Stopped (the task was ended): started again, not re-registered.
    windows.tasks[name] = "Ready"
    windows.calls.clear()
    schedule._install_agent_task(windows.agent, windows.ramp, None)
    assert [c[:2] for c in windows.calls[1:]] == [("schtasks", "/Run")]


def test_the_registered_name_wins_over_a_different_username(windows, monkeypatch):
    schedule._install_agent_task(windows.agent, windows.ramp, None)
    # The task's own environment (or a renamed account) has another USERNAME.
    monkeypatch.setenv("USERNAME", "someone-else")
    assert schedule.agent_task_name() == "Ramp Router Agent (kiran)"
    windows.agent.write_bytes(b"an updated agent")
    schedule._install_agent_task(windows.agent, windows.ramp, None)
    assert set(windows.tasks) == {"Ramp Router Agent (kiran)"}


def test_a_changed_definition_replaces_the_running_agent(windows):
    name = schedule.agent_task_name()
    schedule._install_agent_task(windows.agent, windows.ramp, None)
    windows.agent.write_bytes(b"an updated agent")  # --build changes
    windows.calls.clear()
    schedule._install_agent_task(windows.agent, windows.ramp, None)
    assert [c[:2] for c in windows.calls[1:]] == [
        ("schtasks", "/End"),
        ("schtasks", "/Create"),
        ("schtasks", "/Run"),
    ]
    assert windows.tasks[name] == "Running"


def test_a_task_the_user_disabled_stays_disabled(windows, monkeypatch):
    logged: list[str] = []
    monkeypatch.setattr(schedule, "log_failure", logged.append)
    windows.tasks[schedule.agent_task_name()] = "Disabled"
    schedule._install_agent_task(windows.agent, windows.ramp, None)
    assert not [c for c in windows.calls if c[0] == "schtasks"]
    assert "is off" in logged[0]


def test_an_unreadable_task_state_registers_nothing(windows, monkeypatch):
    # It may be a task the user disabled; re-registering would turn it on.
    logged: list[str] = []
    monkeypatch.setattr(schedule, "log_failure", logged.append)
    monkeypatch.setattr(schedule, "_task_state", lambda name=None: None)
    schedule._install_agent_task(windows.agent, windows.ramp, None)
    assert not [c for c in windows.calls if c[0] == "schtasks"]
    assert "could not read" in logged[0]


def test_a_background_run_never_ends_the_running_agent(windows, monkeypatch):
    # It may be the agent's own refresh, inside the task's job.
    name = schedule.agent_task_name()
    schedule._install_agent_task(windows.agent, windows.ramp, None)
    windows.agent.write_bytes(b"an updated agent")
    monkeypatch.setenv(schedule.SCHEDULED_ENV, "1")
    windows.calls.clear()
    schedule._install_agent_task(windows.agent, windows.ramp, None)
    assert [c[0] for c in windows.calls] == ["powershell"]
    assert windows.tasks[name] == "Running"
    # The next interactive install replaces it.
    monkeypatch.delenv(schedule.SCHEDULED_ENV)
    schedule._install_agent_task(windows.agent, windows.ramp, None)
    assert ("schtasks", "/End") in [c[:2] for c in windows.calls]


def test_the_agent_is_not_ended_while_a_refresh_writes(windows, monkeypatch):
    @contextlib.contextmanager
    def busy(wait=0):
        yield False

    schedule._install_agent_task(windows.agent, windows.ramp, None)
    windows.agent.write_bytes(b"an updated agent")
    monkeypatch.setattr(schedule, "sync_lock", busy)
    windows.calls.clear()
    schedule._install_agent_task(windows.agent, windows.ramp, None)
    assert [c[0] for c in windows.calls] == ["powershell"]


def test_uninstall_ends_and_deletes_the_task(windows):
    name = schedule.agent_task_name()
    schedule._install_agent_task(windows.agent, windows.ramp, None)
    windows.calls.clear()
    schedule._uninstall_agent_task()
    assert [c[:2] for c in windows.calls[1:]] == [
        ("schtasks", "/End"),
        ("schtasks", "/Delete"),
    ]
    assert name not in windows.tasks
    assert not schedule.agent_installed()
    assert schedule.agent_state() == schedule.AGENT_NOT_INSTALLED


def test_every_windows_command_runs_without_a_console_window(monkeypatch):
    seen: list[int] = []

    def fake_run(command, **kwargs):
        seen.append(kwargs.get("creationflags", 0))
        return type("Result", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

    monkeypatch.setattr(schedule.subprocess, "run", fake_run)
    schedule._windows_run(schedule._system32("schtasks.exe"), "/Query")
    assert seen == [0x0800_0000]  # CREATE_NO_WINDOW


def test_windows_never_falls_back_to_a_timer_job(windows, monkeypatch):
    monkeypatch.setattr(schedule, "ramp_executable", lambda: windows.ramp)
    monkeypatch.setattr(schedule, "agent_executable", lambda _ramp=None: None)
    monkeypatch.setattr(schedule, "background_enabled", lambda: True)
    for name in ("_install_launchd", "_install_systemd"):
        monkeypatch.setattr(schedule, name, lambda: pytest.fail("timer job on Windows"))
    schedule._install_service()
    assert not schedule._job_installed()


def test_agent_binary_is_found_beside_the_windows_executable(windows, monkeypatch):
    monkeypatch.setattr(schedule, "AGENT_BINARY", "ramp-router-agent.exe")
    monkeypatch.delenv(schedule.AGENT_PATH_ENV, raising=False)
    assert schedule.agent_executable(windows.ramp) == windows.agent


def test_ramp_executable_names_the_exe_not_the_cmd_shim(tmp_path):
    exe = tmp_path / "ramp-windows-amd64.exe"
    exe.write_bytes(b"")
    assert router_sync._windows_executable(exe) == exe
    # A pip/uv console script: argv[0] may lack the .exe.
    script = tmp_path / "Scripts" / "ramp.exe"
    script.parent.mkdir()
    script.write_bytes(b"")
    assert router_sync._windows_executable(script.with_suffix("")) == script
    # Never another program, and never the ramp.cmd shim.
    other = tmp_path / "python.exe"
    other.write_bytes(b"")
    assert router_sync._windows_executable(other) is None
    shim = tmp_path / "ramp.cmd"
    shim.write_bytes(b"")
    assert router_sync._windows_executable(shim) is None


# ---------------------------------------------------------------------------
# `ramp router agent enable` on Windows
# ---------------------------------------------------------------------------


def test_enable_is_allowed_on_windows_with_the_agent(monkeypatch):
    monkeypatch.setattr(router_module.sys, "platform", "win32")
    monkeypatch.setattr(
        router_module, "_scheduled_sync_connections", lambda: [object()]
    )
    monkeypatch.setattr(router_module.router_repo, "service_supported", lambda: True)
    enabled: list[str] = []
    monkeypatch.setattr(
        router_module.router_repo, "enable", lambda *_: enabled.append("tagging")
    )
    monkeypatch.setattr(
        router_module.router_schedule,
        "enable_background",
        lambda: enabled.append("sync"),
    )
    monkeypatch.setattr(router_module, "_print_background_status", lambda _ctx: None)
    result = CliRunner().invoke(cli, ["router", "agent", "enable"])
    assert result.exit_code == 0, result.output
    assert enabled == ["tagging", "sync"]


def test_enable_on_windows_without_the_agent_explains_why(monkeypatch):
    monkeypatch.setattr(router_module.sys, "platform", "win32")
    monkeypatch.setattr(router_module.router_repo, "service_supported", lambda: False)
    monkeypatch.setattr(
        router_module.router_schedule,
        "enable_background",
        lambda: pytest.fail("must not opt in without the agent"),
    )
    result = CliRunner().invoke(cli, ["router", "agent", "enable"])
    assert result.exit_code != 0
    assert "ramp-router-agent.exe" in result.output


# ---------------------------------------------------------------------------
# Locks without fcntl
# ---------------------------------------------------------------------------


class _FakeMsvcrt:
    LK_NBLCK, LK_UNLCK = 2, 0

    def __init__(self):
        self.held: set[str] = set()

    def locking(self, fd, mode, length):
        assert length == 1 and os.lseek(fd, 0, os.SEEK_CUR) == 0
        name = os.fstat(fd).st_ino
        if mode == self.LK_NBLCK:
            if name in self.held:
                raise OSError("locked")
            self.held.add(name)
        else:
            self.held.discard(name)


def test_sync_lock_without_fcntl_excludes_a_second_holder(monkeypatch):
    fake = _FakeMsvcrt()
    monkeypatch.setattr(router_sync, "fcntl", None)
    monkeypatch.setattr(router_sync, "msvcrt", fake)
    with router_sync.sync_lock() as first:
        assert first
        with router_sync.sync_lock() as second:
            assert not second
        assert not router_sync.claim_sync_slot()
    with router_sync.sync_lock() as again:
        assert again
    assert not fake.held


@windows_only
def test_real_windows_locks_exclude_a_second_holder():
    with router_sync.sync_lock() as first:
        assert first
        with router_sync.sync_lock() as second:
            assert not second
    with router_sync.sync_lock() as again:
        assert again


# ---------------------------------------------------------------------------
# Real ACLs (Windows hosts)
# ---------------------------------------------------------------------------


@windows_only
def test_private_files_round_trip_and_reject_shared_ones(tmp_path):
    directory = tmp_path / "ramp"
    local_auth.make_directory(directory)
    local_auth.check_directory(directory)
    path = directory / "router-agent-health-token"
    local_auth.write_private(path, "secret")
    assert local_auth.read_private(path) == "secret"
    with local_auth.private_lock(directory / "x.lock"):
        pass
    # Shared with Everyone: refused, like a 0644 file.
    subprocess.run(
        ["icacls", str(path), "/grant", "*S-1-1-0:R"], check=True, capture_output=True
    )
    with pytest.raises(OSError):
        local_auth.read_private(path)
    # Rewriting publishes a private file again.
    local_auth.write_private(path, "secret2")
    assert local_auth.read_private(path) == "secret2"
    # A folder others can add files to is refused.
    subprocess.run(
        ["icacls", str(directory), "/grant", "*S-1-1-0:(M)"],
        check=True,
        capture_output=True,
    )
    with pytest.raises(OSError):
        local_auth.check_directory(directory)


@windows_only
def test_tokens_register_and_resolve_on_windows(tmp_path):
    directory = tmp_path / "ramp"
    local_auth.make_directory(directory)
    credentials = directory / "router-agent-credentials"
    token = local_auth.register(credentials, "h" * 64, "api.router.com", "real-key")
    assert local_auth.resolve(credentials, token, "api.router.com") == "real-key"
    assert winsec.directory_is_private(credentials)


@windows_only
def test_an_inherited_shared_acl_is_replaced_on_folders_this_user_owns(tmp_path):
    # Codex's Windows sandbox gives its users Modify on folders it shares; a
    # config folder under one inherits that. Writing replaces it.
    directory = tmp_path / "ramp"
    directory.mkdir()
    # Owned by this user: an elevated runner's new folders belong to
    # Administrators instead.
    winsec.secure(directory)
    subprocess.run(
        ["icacls", str(directory), "/grant", "*S-1-1-0:(OI)(CI)(M)"],
        check=True,
        capture_output=True,
    )
    assert not winsec.directory_is_private(directory)
    local_auth.write_private(directory / "router-agent-cursor.json", "{}")
    assert winsec.directory_is_private(directory)
    assert local_auth.read_private(directory / "router-agent-cursor.json") == "{}"


# ---------------------------------------------------------------------------
# The Router setup key state (`ramp router configure --enable-agent` / enable)
# ---------------------------------------------------------------------------


@pytest.fixture
def windows_credentials(monkeypatch):
    """router_credentials on Windows: no os.getuid, the agent's private-file checks."""
    monkeypatch.setattr(credentials.sys, "platform", "win32")
    monkeypatch.delattr(credentials.os, "getuid", raising=False)
    return credentials


def test_setup_key_state_is_read_with_the_private_file_checks(
    windows_credentials, monkeypatch, tmp_path
):
    path = tmp_path / "router-setup-key.json"
    checked: list[Path] = []
    monkeypatch.setattr(local_auth, "check_directory", checked.append)
    monkeypatch.setattr(
        local_auth, "read_private", lambda p: json.dumps({"request_id": "r1"})
    )
    assert windows_credentials._read_state(path) == {"request_id": "r1"}
    assert checked == [tmp_path]

    # Missing folder or file: no state yet.
    assert windows_credentials._read_state(tmp_path / "missing" / "state.json") == {}

    def missing(_path):
        raise FileNotFoundError

    monkeypatch.setattr(local_auth, "read_private", missing)
    assert windows_credentials._read_state(path) == {}

    def shared(_path):
        raise OSError("Router agent file is not private to this user")

    monkeypatch.setattr(local_auth, "read_private", shared)
    with pytest.raises(click.ClickException, match="owner-private"):
        windows_credentials._read_state(path)

    def loose_folder(_path):
        raise OSError("Router agent directory is not owned and protected by this user")

    monkeypatch.setattr(local_auth, "check_directory", loose_folder)
    with pytest.raises(click.ClickException, match="not writable by other users"):
        windows_credentials._read_state(path)


def test_setup_key_state_size_and_shape_are_still_checked(
    windows_credentials, monkeypatch, tmp_path
):
    monkeypatch.setattr(local_auth, "check_directory", lambda _p: None)
    for text, message in [
        ("x" * 16385, "owner-private"),
        ("{not json", "Could not read"),
        (json.dumps({"no_request_id": True}), "Invalid Router setup key state"),
    ]:
        monkeypatch.setattr(local_auth, "read_private", lambda _p, text=text: text)
        with pytest.raises(click.ClickException, match=message):
            windows_credentials._read_state(tmp_path / "state.json")


def test_router_session_files_are_written_private_on_windows(monkeypatch, tmp_path):
    secured: list[Path] = []
    monkeypatch.setattr(router_user.sys, "platform", "win32")
    monkeypatch.setattr(router_user.router_winsec, "secure", secured.append)
    path = tmp_path / "session.json"
    router_user._atomic_write(path, {"secret": "s"})
    assert json.loads(path.read_text()) == {"secret": "s"}
    # The temporary file got its DACL before any content was written to it.
    assert len(secured) == 1 and secured[0].parent == tmp_path


@windows_only
def test_setup_key_state_round_trips_on_real_windows_acls(tmp_path):
    directory = tmp_path / "ramp"
    local_auth.make_directory(directory)
    path = directory / "router-setup-key.json"
    router_user._atomic_write(path, {"request_id": "r1"})
    assert credentials._read_state(path) == {"request_id": "r1"}
    subprocess.run(
        ["icacls", str(path), "/grant", "*S-1-1-0:R"], check=True, capture_output=True
    )
    with pytest.raises(click.ClickException, match="owner-private"):
        credentials._read_state(path)
