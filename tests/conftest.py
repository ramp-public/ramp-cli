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
    # Pin the resolved ramp entrypoint so hook commands in fixtures are
    # deterministic regardless of how the tests were launched.
    monkeypatch.setattr(
        "ramp_cli.commands.router_sync.ramp_executable",
        lambda: Path("/opt/ramp-cli/bin/ramp"),
    )
    # Hook-mode sync suppresses the shutdown update notice; only main() —
    # which CliRunner never reaches — resets the flag, so clear it here.
    monkeypatch.setattr("ramp_cli.version_check._SUPPRESS_NEXT_UPDATE_NOTICE", False)
    if "tests/private" not in str(request.node.path):
        monkeypatch.setattr("ramp_cli.config.settings.default_environment", lambda: "")
    yield tmp_path
