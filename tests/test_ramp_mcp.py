from __future__ import annotations

import json
import tomllib

import pytest

from ramp_cli.router_connectors import ramp_mcp

PROD = "https://api.router.com/v1"


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        (PROD, True),
        ("https://internal-api.router.com/v1", True),
        ("https://router-api.ramp.com/v1", True),
        ("https://qa-router-api.ramp.com/v1", False),
        ("http://localhost:8000/v1", False),
    ],
)
def test_should_configure_only_for_prod_router(base_url: str, expected: bool) -> None:
    assert ramp_mcp.should_configure(base_url) is expected


@pytest.mark.parametrize(
    ("harness", "rel_path", "key", "expected_entry"),
    [
        (
            "claude-code",
            ".claude.json",
            "mcpServers",
            {"type": "http", "url": ramp_mcp.URL},
        ),
        ("codex", "config.toml", "mcp_servers", {"url": ramp_mcp.URL}),
        (
            "opencode",
            "opencode/opencode.json",
            "mcp",
            {"type": "remote", "url": ramp_mcp.URL, "enabled": True},
        ),
        ("cursor", ".cursor/mcp.json", "mcpServers", {"url": ramp_mcp.URL}),
        (
            "antigravity",
            "config/mcp_config.json",
            "mcpServers",
            {"url": ramp_mcp.URL},
        ),
    ],
)
def test_configure_writes_entry_once(
    monkeypatch, tmp_path, harness: str, rel_path: str, key: str, expected_entry: dict
) -> None:
    for var in (
        "HOME",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
        "RAMP_ANTIGRAVITY_GEMINI_DIR",
    ):
        monkeypatch.setenv(var, str(tmp_path))
    cfg = tmp_path / rel_path

    ramp_mcp.configure(harness, PROD)
    first = cfg.read_text()
    loads = tomllib.loads if harness == "codex" else json.loads
    assert loads(first)[key]["ramp"] == expected_entry

    ramp_mcp.configure(harness, PROD)
    assert cfg.read_text() == first


def test_configure_preserves_user_owned_ramp_entry(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    cfg = tmp_path / ".claude.json"
    custom = json.dumps(
        {
            "mcpServers": {
                "ramp": {"type": "http", "url": "https://demo-mcp.ramp.com/mcp"}
            }
        }
    )
    cfg.write_text(custom)

    ramp_mcp.configure("claude-code", PROD)
    assert cfg.read_text() == custom
