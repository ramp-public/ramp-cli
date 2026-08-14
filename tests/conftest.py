"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
):
    """Redirect config to a temp directory so tests don't touch real config.

    Every coding agent this CLI configures keeps its files somewhere under the
    user's home and reads an environment variable to find them. Isolating only
    XDG_CONFIG_HOME covered OpenCode and left the rest pointing at the real
    home, so a test that configured every agent wrote its fixture credential
    into the developer's own Codex and Pi setup.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    # The CLI reads these, so a developer pointing their own shell at a local
    # stack would otherwise change what the tests assert.
    for leaked in (
        "RAMP_ROUTER_BASE_URL",
        "LLM_GATEWAY_BASE_URL",
        "RAMP_ROUTER_CONFIGURE_API_KEY",
        "RAMP_AGENT_WALLET_API_KEY",
        "RAMP_AGENT_WALLET_CONFIGURE_API_KEY",
        "RAMP_AGENT_WALLET_API_URL",
    ):
        monkeypatch.delenv(leaked, raising=False)
    monkeypatch.setenv("RAMP_NO_TOOL_AVAILABILITY", "1")
    if "tests/private" not in str(request.node.path):
        monkeypatch.setattr("ramp_cli.config.settings.default_environment", lambda: "")
    yield tmp_path
