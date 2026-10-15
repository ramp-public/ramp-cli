"""Tests for npm install-record detection."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from ramp_cli import install_record


def _write(directory: Path, data: object) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "ramp-test"
    binary.write_text("")
    (directory / "install.json").write_text(json.dumps(data))
    return binary


def test_detects_npm_record(tmp_path: Path):
    binary = _write(tmp_path, {"owner": "@ramp/cli", "package": "@ramp/cli"})
    with patch.object(install_record, "_candidates", return_value=[str(binary)]):
        record = install_record.npm_install_record()
    assert record is not None
    assert install_record.record_package(record) == "@ramp/cli"


def test_entrypoint_is_the_stable_link_beside_the_recorded_binary(tmp_path: Path):
    binary = _write(tmp_path, {"owner": "@ramp/cli"})
    binary.chmod(0o755)
    with patch.object(install_record, "_candidates", return_value=[str(binary)]):
        assert install_record.npm_entrypoint() is None
        (tmp_path / "ramp").symlink_to(binary.name)
        assert install_record.npm_entrypoint() == tmp_path / "ramp"
    with patch.object(install_record, "_candidates", return_value=[""]):
        assert install_record.npm_entrypoint() is None


def test_ignores_other_owner(tmp_path: Path):
    binary = _write(tmp_path, {"owner": "someone-else"})
    with patch.object(install_record, "_candidates", return_value=[str(binary)]):
        assert install_record.npm_install_record() is None


def test_ignores_missing_or_invalid_record(tmp_path: Path):
    (tmp_path / "install.json").write_text("not json")
    binary = tmp_path / "ramp-test"
    with patch.object(install_record, "_candidates", return_value=[str(binary), ""]):
        assert install_record.npm_install_record() is None


def test_channel_defaults_to_public():
    assert install_record.record_channel({"owner": "@ramp/cli"}) == "public"
    assert install_record.record_channel({"channel": "internal"}) == "internal"


def test_update_lines_default_to_router_package():
    lines = install_record.npm_update_lines({"owner": "@ramp/cli"})
    assert "npm update -g @ramp/router" in lines[1]
    assert "npx @ramp/router@latest" in lines[2]


def test_update_lines_use_recorded_package():
    lines = install_record.npm_update_lines({"package": "@ramp/cli"})
    assert "(@ramp/cli)" in lines[0]
    assert "npm update -g @ramp/cli" in lines[1]


def test_update_command_uses_the_persistent_helper(tmp_path: Path):
    binary = _write(tmp_path, {"owner": "@ramp/cli"})
    helper = tmp_path / "npm-update.js"
    with (
        patch.object(install_record, "_candidates", return_value=[str(binary)]),
        patch.object(install_record.sys, "platform", "linux"),
        patch.object(install_record.shutil, "which", return_value="/usr/bin/node"),
    ):
        assert install_record.npm_update_command() is None
        helper.write_text("// updater")
        assert install_record.npm_update_command() == ["/usr/bin/node", str(helper)]
        with patch.object(install_record.shutil, "which", return_value=None):
            assert install_record.npm_update_command() is None
        with patch.object(install_record.sys, "platform", "win32"):
            assert install_record.npm_update_command() is None
