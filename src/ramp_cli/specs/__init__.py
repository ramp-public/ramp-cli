"""Bundled agent-tool spec and spec path constants.

All spec paths are defined here — import from this module instead of
constructing paths with Path(__file__) elsewhere.
"""

from pathlib import Path

from ramp_cli.config.settings import config_dir

_PKG_DIR = Path(__file__).parent

AGENT_TOOL_SPEC = _PKG_DIR / "agent-tool.json"


def local_agent_tool_spec(env: str) -> Path:
    return config_dir() / f"agent-tool-{env}.json"


def local_agent_tool_hash(env: str) -> Path:
    return config_dir() / f"agent-tool-{env}-hash.txt"
