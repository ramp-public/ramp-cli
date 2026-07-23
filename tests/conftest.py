"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
):
    """Redirect config to a temp directory so tests don't touch real config."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("RAMP_NO_TOOL_AVAILABILITY", "1")
    if "tests/private" not in str(request.node.path):
        monkeypatch.setattr("ramp_cli.config.settings.default_environment", lambda: "")
    yield tmp_path
