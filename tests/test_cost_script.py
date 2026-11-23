"""Running Router's cost scripts through ramp, as Windows agents do."""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

from ramp_cli import cost_script

_ECHO_SCRIPT = """\
import os, sys
data = sys.stdin.read()
print(f"{sys.argv[1:]}|{data}|{os.environ.get('ROUTER_API_KEY')}|"
      f"{os.environ.get('ROUTER_BASE_URL')}|{'ramp_cli.commands.router' in sys.modules}")
sys.exit(3)
"""


def _script(tmp_path: Path) -> Path:
    script = tmp_path / "script"
    script.write_text(_ECHO_SCRIPT)
    return script


def test_the_fast_path_runs_the_script_before_the_cli_loads(tmp_path):
    script = _script(tmp_path)
    key = tmp_path / "key"
    key.write_text("router-secret\n")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ramp_cli.main",
            "router",
            "run-cost-script",
            "--key-file",
            str(key),
            "--base-url",
            "https://app.router.com",
            str(script),
            "extra",
        ],
        input='{"session_id": "s"}',
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    # Stdin, argv, the options and the exit code all pass through, and the
    # command modules were never imported.
    assert result.returncode == 3, result.stderr
    assert result.stdout == (
        '[\'extra\']|{"session_id": "s"}|router-secret|https://app.router.com|False\n'
    )


def test_run_if_invoked_passes_stdin_through(tmp_path, monkeypatch, capsys):
    script = _script(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
    monkeypatch.delenv("ROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ROUTER_BASE_URL", raising=False)

    with pytest.raises(SystemExit) as exited:
        cost_script.run_if_invoked(["ramp", "router", "run-cost-script", str(script)])

    assert exited.value.code == 3
    assert capsys.readouterr().out.startswith("[]|{}|None|None|")


def test_other_invocations_are_left_to_the_cli():
    cost_script.run_if_invoked(["ramp", "router", "configure"])
    cost_script.run_if_invoked(["ramp", "--no-input", "router", "run-cost-script"])


def test_an_unreadable_key_file_leaves_the_script_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("ROUTER_API_KEY", "stale")
    script = tmp_path / "script"
    script.write_text("import os, sys\nsys.exit(len(os.environ['ROUTER_API_KEY']))\n")

    code = cost_script.run(["--key-file", str(tmp_path / "missing"), str(script)])

    assert code == 0


def test_the_shell_command_is_bare_for_git_bash_and_powershell():
    command = cost_script.shell_command(
        r"C:\Users\me\AppData\Local\Ramp\ramp.exe",
        Path(r"C:\Users\me\.claude\ramp-router-statusline"),
    )

    assert command == (
        "C:/Users/me/AppData/Local/Ramp/ramp.exe router run-cost-script "
        "C:/Users/me/.claude/ramp-router-statusline"
    )
    assert cost_script.invocation(command) == (
        {},
        "C:/Users/me/.claude/ramp-router-statusline",
    )


def test_a_path_with_a_space_uses_its_short_form(monkeypatch):
    short = {
        r"C:\Users\Me Too\ramp.exe": r"C:\Users\METOO~1\ramp.exe",
        r"C:\Users\Me Too\.claude\ramp-router-statusline": (
            r"C:\Users\METOO~1\.claude\ramp-router-statusline"
        ),
    }
    monkeypatch.setattr(cost_script, "_short_path", short.get)

    command = cost_script.shell_command(
        r"C:\Users\Me Too\ramp.exe",
        Path(r"C:\Users\Me Too\.claude\ramp-router-statusline"),
    )

    assert command == (
        "C:/Users/METOO~1/ramp.exe router run-cost-script "
        "C:/Users/METOO~1/.claude/ramp-router-statusline"
    )


def test_the_hook_command_is_bare_for_cmd_and_powershell(monkeypatch):
    short = {
        r"C:\Users\Me Too\ramp.exe": r"C:\Users\METOO~1\ramp.exe",
        r"C:\Users\Me Too\.codex\ramp-router-key": (
            r"C:\Users\METOO~1\.codex\ramp-router-key"
        ),
        r"C:\Users\Me Too\.codex\codex-cost-hook": (
            r"C:\Users\METOO~1\.codex\codex-cost-hook"
        ),
    }
    monkeypatch.setattr(cost_script, "_short_path", short.get)

    command = cost_script.hook_command(
        r"C:\Users\Me Too\ramp.exe",
        Path(r"C:\Users\Me Too\.codex\codex-cost-hook"),
        key_file=Path(r"C:\Users\Me Too\.codex\ramp-router-key"),
        base_url="https://app.router.com",
    )

    # No quotes: PowerShell runs a quoted program only through `&`.
    assert command == (
        r"C:\Users\METOO~1\ramp.exe router run-cost-script "
        r"--key-file C:\Users\METOO~1\.codex\ramp-router-key "
        r"--base-url https://app.router.com "
        r"C:\Users\METOO~1\.codex\codex-cost-hook"
    )
    assert cost_script.invocation(command) == (
        {
            "--key-file": r"C:\Users\METOO~1\.codex\ramp-router-key",
            "--base-url": "https://app.router.com",
        },
        r"C:\Users\METOO~1\.codex\codex-cost-hook",
    )


def test_a_path_with_no_bare_form_writes_no_command(monkeypatch):
    monkeypatch.setattr(cost_script, "_short_path", lambda _path: None)
    ramp = r"C:\Users\Me Too\ramp.exe"

    assert cost_script.shell_command(ramp, Path(r"C:\x")) is None
    assert (
        cost_script.hook_command(
            ramp, Path(r"C:\x"), key_file=Path(r"C:\k"), base_url="https://r"
        )
        is None
    )


def _fake_path_calls(monkeypatch, table):
    monkeypatch.setattr(
        cost_script,
        "_windows_path_call",
        lambda function, path: table.get((function, path)),
    )


def test_the_short_form_is_the_same_before_and_after_the_download(monkeypatch):
    folder = r"C:\Users\Me Too\.claude"
    script = folder + r"\ramp-router-statusline"
    table = {("GetShortPathNameW", folder): r"C:\Users\METOO~1\CLAUDE~1"}
    _fake_path_calls(monkeypatch, table)
    before = cost_script.bare_path(script)

    # Once downloaded, the file has a short name of its own.
    table[("GetShortPathNameW", script)] = r"C:\Users\METOO~1\CLAUDE~1\RAMP-R~1"

    assert before == "C:/Users/METOO~1/CLAUDE~1/ramp-router-statusline"
    assert cost_script.bare_path(script) == before


def test_a_short_form_matches_its_long_path(monkeypatch):
    _fake_path_calls(
        monkeypatch,
        {
            ("GetLongPathNameW", r"C:/Users/METOO~1/CLAUDE~1"): (
                r"C:\Users\Me Too\.claude"
            ),
        },
    )

    assert cost_script.same_windows_path(
        "C:/Users/METOO~1/CLAUDE~1/ramp-router-statusline",
        r"C:\Users\Me Too\.claude\ramp-router-statusline",
    )
    assert not cost_script.same_windows_path(
        "C:/Users/METOO~1/CLAUDE~1/ramp-router-statusline",
        r"C:\Users\Someone Else\.claude\ramp-router-statusline",
    )


def test_the_running_ramp_is_found_through_a_launcher(tmp_path, monkeypatch):
    # pip and uv's ramp.exe launchers drop ".exe" from argv[0] before main(),
    # and another ramp first on PATH must not stand in for the one running.
    launcher = tmp_path / "Scripts" / "ramp.exe"
    launcher.parent.mkdir()
    launcher.write_bytes(b"")
    monkeypatch.setattr("shutil.which", lambda name: "C:/elsewhere/ramp.CMD")
    for argv0 in (str(launcher), str(launcher.with_suffix(""))):
        monkeypatch.setattr(sys, "argv", [argv0, "router", "sync"])
        assert cost_script.windows_executable() == str(launcher)

    monkeypatch.setattr(sys, "argv", ["-c"])
    assert cost_script.windows_executable() == "C:/elsewhere/ramp.CMD"
