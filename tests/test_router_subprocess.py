"""Router children must not import Python packages from an untrusted cwd."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ramp_cli import __version__
from ramp_cli.commands import router_sync
from ramp_cli.router_ui import service as service_module
from ramp_cli.router_ui.service import RouterService
from ramp_cli.version_check import installed_version_path


@pytest.fixture
def untrusted_cwd(monkeypatch, tmp_path):
    project = tmp_path / "untrusted-project"
    package = project / "ramp_cli"
    package.mkdir(parents=True)
    marker = tmp_path / "cwd-package-imported"
    (package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    )
    (package / "main.py").write_text("def main():\n    print('cwd package executed')\n")
    monkeypatch.chdir(project)
    # Exercise the interpreter's defaults, not an inherited safety setting or
    # a test runner's source path. The installed package must be sufficient.
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    monkeypatch.setenv("RAMP_NO_UPDATE_CHECK", "1")
    monkeypatch.setenv("RAMP_INSTALLED_VERSION_FILE", str(tmp_path / "version"))
    return marker


def test_tui_command_ignores_cwd_package(untrusted_cwd):
    output = RouterService({}).command(
        ["configure", "connect", "--help"], secret="synthetic-router-key"
    )

    assert not untrusted_cwd.exists()
    assert "--setup-file" in output
    assert "--base-url" in output
    assert Path.cwd().name == "untrusted-project"


def test_detached_fallback_ignores_cwd_package(monkeypatch, untrusted_cwd):
    monkeypatch.setattr(router_sync, "ramp_executable", lambda: None)
    real_popen = subprocess.Popen
    children = []

    def popen(*args, **kwargs):
        # Keep the actual exec/import boundary and detached arguments intact;
        # retain the handle only to wait for completion and clean up on failure.
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(router_sync.subprocess, "Popen", popen)
    try:
        router_sync.spawn_detached_refresh()
        assert len(children) == 1
        assert children[0].wait(timeout=20) == 0, router_sync.log_path().read_text()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    assert not untrusted_cwd.exists()
    result = json.loads(router_sync.log_path().read_text())
    assert result["data"] == [{"clients": []}]
    assert installed_version_path().read_text() == __version__


@pytest.mark.parametrize("binary_marker", ["frozen", "__compiled__"])
def test_tui_native_binary_reexec_is_unchanged(monkeypatch, binary_marker):
    if binary_marker == "frozen":
        monkeypatch.setattr(sys, "frozen", True, raising=False)
    else:
        monkeypatch.setattr(service_module, "__compiled__", True, raising=False)
    calls = []
    monkeypatch.setattr(
        service_module.subprocess,
        "run",
        lambda args, **kwargs: (
            calls.append(args) or SimpleNamespace(returncode=0, stdout="Done")
        ),
    )

    assert RouterService({}).command(["refresh"]) == "Done"
    assert calls == [[sys.executable, "--human", "--no-input", "router", "refresh"]]
