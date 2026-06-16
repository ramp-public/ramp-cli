"""Bundled generated-tool specs and cache path definitions.

All spec paths are defined here — import from this module instead of
constructing paths with Path(__file__) elsewhere.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ramp_cli.config.constants import agent_tool_spec_hash_url, agent_tool_spec_url
from ramp_cli.config.settings import config_dir

_PKG_DIR = Path(__file__).parent
_PKG_ROOT = _PKG_DIR.parent  # ramp_cli/

AGENT_TOOL_SPEC = _PKG_DIR / "agent-tool.json"
SKILLS_DIR = _PKG_ROOT / "skills"


@dataclass(frozen=True, slots=True)
class GeneratedToolSpec:
    """Configuration for a remotely refreshable OpenAPI tool document."""

    source: str
    bundled_path: Path
    cache_stem: str
    spec_url: Callable[[str], str]
    hash_url: Callable[[str], str]
    path_prefix: str | None = None
    synthesize_cli_tools: bool = False

    def local_spec(self, cache_key: str) -> Path:
        return config_dir() / f"{self.cache_stem}-{cache_key}.json"

    def local_hash(self, cache_key: str) -> Path:
        return config_dir() / f"{self.cache_stem}-{cache_key}-hash.txt"

    def resolve(self, cache_key: str) -> Path:
        cached = self.local_spec(cache_key)
        return cached if cached.exists() else self.bundled_path


AGENT_TOOL_DEFINITION = GeneratedToolSpec(
    source="agent-tools",
    bundled_path=AGENT_TOOL_SPEC,
    cache_stem="agent-tool",
    spec_url=agent_tool_spec_url,
    hash_url=agent_tool_spec_hash_url,
    path_prefix=None,
    synthesize_cli_tools=True,
)
GENERATED_TOOL_SPECS = (AGENT_TOOL_DEFINITION,)


def local_agent_tool_spec(env: str) -> Path:
    return AGENT_TOOL_DEFINITION.local_spec(env)


def local_agent_tool_hash(env: str) -> Path:
    return AGENT_TOOL_DEFINITION.local_hash(env)
