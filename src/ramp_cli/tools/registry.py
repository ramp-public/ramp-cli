"""Tool registry — loads and caches ToolDefs from the agent-tool spec.

Spec resolution order:
  1. Local cache at ~/.config/ramp/agent-tool-{env}.json (written by `ramp tools refresh`)
  2. Bundled spec inside the package at ramp_cli/specs/agent-tool.json
"""

from ramp_cli.auth.store import get_granted_scopes
from ramp_cli.config.settings import resolve_environment
from ramp_cli.specs import AGENT_TOOL_SPEC, local_agent_tool_spec
from ramp_cli.tools.parser import ToolDef, parse_spec


def _resolve_spec_path(env: str):
    """Return the most up-to-date spec path available for *env*."""
    local = local_agent_tool_spec(env)
    if local.exists():
        return local
    return AGENT_TOOL_SPEC


def _default_env() -> str:
    return resolve_environment()


class _Registry:
    """Lazy-loading, env-aware cache for parsed tool definitions.

    Tracks which env's spec is loaded and automatically reloads when a
    different env is requested.
    """

    def __init__(self) -> None:
        self._tools: list[ToolDef] | None = None
        self._index: dict[str, ToolDef] | None = None
        self._loaded_env: str | None = None

    def _ensure_loaded(self, env: str | None = None) -> None:
        if env is None:
            env = _default_env()
        if self._tools is not None and self._loaded_env == env:
            return
        self._tools = parse_spec(_resolve_spec_path(env))
        self._index = {t.name: t for t in self._tools}
        self._loaded_env = env

    def reload(self, env: str | None = None) -> None:
        """Force reload from disk (after refresh)."""
        if env is None:
            env = _default_env()
        self._tools = parse_spec(_resolve_spec_path(env))
        self._index = {t.name: t for t in self._tools}
        self._loaded_env = env

    def list_names(self, env: str | None = None) -> list[str]:
        self._ensure_loaded(env)
        assert self._tools is not None
        return [t.name for t in self._tools]

    def list_defs(self, env: str | None = None) -> list[ToolDef]:
        self._ensure_loaded(env)
        assert self._tools is not None
        return self._tools

    def get(self, name: str, env: str | None = None) -> ToolDef | None:
        self._ensure_loaded(env)
        assert self._index is not None
        return self._index.get(name)


_registry = _Registry()


def _filter_by_scopes(tools: list[ToolDef], env: str) -> list[ToolDef]:
    """Filter tools to only those the current token has scopes for."""
    granted = get_granted_scopes(env)
    if not granted:
        # No scope info stored — show all tools (backwards compatible with
        # tokens saved before scope persistence was added).
        return tools
    return [
        t for t in tools if not t.required_scopes or set(t.required_scopes) <= granted
    ]


def list_tools(env: str | None = None) -> list[str]:
    """Return sorted tool names."""
    return _registry.list_names(env)


def list_tool_defs(env: str | None = None) -> list[ToolDef]:
    """Return all parsed ToolDefs."""
    return _registry.list_defs(env)


def get_tool(name: str, env: str | None = None) -> ToolDef | None:
    """Look up a tool by endpoint name (e.g. 'get-funds')."""
    return _registry.get(name, env)


def list_categories(env: str | None = None) -> dict[str, list[ToolDef]]:
    """Return tools grouped by category, filtered to accessible tools."""
    if env is None:
        env = _default_env()
    tools = _filter_by_scopes(list_tool_defs(env), env)
    categories: dict[str, list[ToolDef]] = {}
    for t in tools:
        cat = t.category or "general"
        categories.setdefault(cat, []).append(t)
    return dict(sorted(categories.items()))


def reload(env: str | None = None) -> None:
    """Force reload the registry from disk."""
    _registry.reload(env)
