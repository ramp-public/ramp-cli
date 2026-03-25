"""Tests for bundled specs — ensures specs are packaged correctly."""

import json

from ramp_cli.specs import AGENT_TOOL_SPEC, local_agent_tool_spec


class TestBundledSpecs:
    def test_agent_tool_spec_exists(self):
        assert AGENT_TOOL_SPEC.exists(), (
            f"Agent tool spec not found at {AGENT_TOOL_SPEC}"
        )

    def test_agent_tool_spec_is_valid_json(self):
        spec = json.loads(AGENT_TOOL_SPEC.read_text())
        assert "paths" in spec
        tools = [p for p in spec["paths"] if "agent-tools" in p]
        assert len(tools) >= 40

    def test_specs_inside_package(self):
        """Specs must live inside ramp_cli/, not at the repo root."""
        assert "ramp_cli" in str(AGENT_TOOL_SPEC)

    def test_local_spec_path_is_env_specific(self):
        assert str(local_agent_tool_spec("production")).endswith(
            "ramp/agent-tool-production.json"
        )
        assert str(local_agent_tool_spec("sandbox")).endswith(
            "ramp/agent-tool-sandbox.json"
        )
