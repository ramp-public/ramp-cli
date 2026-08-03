"""Claude Code configuration must be reversible and must not disturb the user."""

import json
import stat

import click
import httpx
import pytest
from click.testing import CliRunner

import ramp_cli.commands.claude_code as claude_code
import ramp_cli.commands.router as router_module
from ramp_cli.main import cli


def _router_metadata(identifier):
    return {
        "schema_version": 1,
        "request_name": identifier,
        "display_name": identifier,
        "description": "",
        "listing": {"order": 0},
        "limits": {"context_window": 128000, "max_output_tokens": 16384},
        "capabilities": {
            "modalities": {"input": ["text", "image"]},
            "tools": {"supported": True},
            "reasoning": {"efforts": [], "default_effort": ""},
        },
    }


def _mock_models(monkeypatch, models=None):
    models = models or [{"id": "gpt-5.4", "owned_by": "openai"}]
    # Router describes every model it serves, and the CLI now requires it.
    models = [
        {**model, "router": model.get("router", _router_metadata(model["id"]))}
        for model in models
    ]
    seen = []

    def get(url, *, headers, timeout):
        seen.append(headers)
        return httpx.Response(
            200, json={"data": models}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(router_module.httpx, "get", get)
    return seen


def test_settings_live_in_the_user_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert claude_code.settings_path() == tmp_path / ".claude" / "settings.json"

    # An isolated config directory is how a test run or a second profile keeps
    # its settings separate, so it has to win.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert claude_code.settings_path() == tmp_path / "elsewhere" / "settings.json"


def test_base_url_is_the_host_root():
    # Claude Code appends /v1/messages itself, so a base URL ending in /v1
    # produces /v1/v1/messages and every request 404s.
    assert not router_module._router_host().endswith("/v1")
    assert router_module._router_host() == "https://router-api.ramp.com"


def test_setup_state_requires_the_complete_current_schema(tmp_path):
    settings = tmp_path / "settings.json"
    claude_code.state_path(settings).write_text(
        json.dumps({"env": {}, "top_level": {}}) + "\n"
    )

    with pytest.raises(click.ClickException, match="Could not read"):
        claude_code.read_state(settings)


def test_unrelated_settings_survive(tmp_path):
    path = tmp_path / "settings.json"
    original = {
        "theme": "dark",
        "env": {"MY_OWN": "keep", "ANTHROPIC_BASE_URL": "https://example.invalid"},
    }
    updated, _ = claude_code.plan_configuration(
        original, path, "https://router-api.ramp.com", "secret", "gpt-5.4"
    )

    assert updated["theme"] == "dark"
    assert updated["env"]["MY_OWN"] == "keep"
    assert updated["env"]["ANTHROPIC_BASE_URL"] == "https://router-api.ramp.com"
    # Router keys are bearer tokens, and setting both auth variables is
    # ambiguous to Claude Code.
    assert updated["env"]["ANTHROPIC_AUTH_TOKEN"] == "secret"
    assert "ANTHROPIC_API_KEY" not in updated["env"]


def test_unconfigure_restores_rather_than_deletes(tmp_path):
    path = tmp_path / "settings.json"
    original = {
        "model": "claude-opus-5",
        "env": {"ANTHROPIC_BASE_URL": "https://example.invalid", "MY_OWN": "keep"},
    }

    updated, state = claude_code.plan_configuration(
        original, path, "https://router-api.ramp.com", "secret", "gpt-5.4"
    )
    restored = claude_code.plan_restoration(updated, path, state)

    # Settings-file values override the shell environment, so deleting a key
    # the user set silently changes their setup rather than restoring it.
    assert restored == original


def test_unconfigure_removes_keys_the_user_never_had(tmp_path):
    path = tmp_path / "settings.json"
    updated, state = claude_code.plan_configuration(
        {}, path, "https://router-api.ramp.com", "secret", "gpt-5.4"
    )

    restored = claude_code.plan_restoration(updated, path, state)

    assert restored == {}


def test_malformed_settings_are_refused_not_replaced(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(click.ClickException):
        claude_code.read_settings(path)

    # The user's file must still be there to fix.
    assert path.read_text(encoding="utf-8") == "{not json"


def test_configure_is_idempotent_and_keeps_the_original_state(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _mock_models(monkeypatch)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://mine"}}))

    runner = CliRunner()
    for api_key in ("first-router-key", "replacement-router-key"):
        result = runner.invoke(
            cli,
            ["--human", "router", "configure", "claude-code", "--api-key", api_key],
        )
        assert result.exit_code == 0, result.output

    # A second configure keeps the original snapshot but records the
    # replacement key as Router-owned, so unconfigure removes it.
    state = json.loads((tmp_path / "ramp-router-state.json").read_text())
    assert state["env"]["ANTHROPIC_BASE_URL"]["value"] == "https://mine"
    assert state["written"]["env"]["ANTHROPIC_AUTH_TOKEN"] == "replacement-router-key"
    assert (
        json.loads(path.read_text())["env"]["ANTHROPIC_AUTH_TOKEN"]
        == "replacement-router-key"
    )

    result = runner.invoke(cli, ["--human", "router", "unconfigure", "claude-code"])
    assert result.exit_code == 0, result.output
    assert json.loads(path.read_text()) == {
        "env": {"ANTHROPIC_BASE_URL": "https://mine"}
    }


def test_failed_reconfigure_restores_the_previous_managed_values(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _mock_models(monkeypatch)
    runner = CliRunner()
    first = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "claude-code",
            "--api-key",
            "first-router-key",
        ],
    )
    assert first.exit_code == 0, first.output

    settings_path = tmp_path / "settings.json"
    state_path = tmp_path / "ramp-router-state.json"
    previous_settings = settings_path.read_text()
    previous_state = state_path.read_text()
    write_private_file = router_module._write_private_file

    def fail_settings_write(path, content):
        if path == settings_path:
            raise OSError("settings write failed")
        write_private_file(path, content)

    monkeypatch.setattr(router_module, "_write_private_file", fail_settings_write)
    second = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "claude-code",
            "--api-key",
            "replacement-router-key",
        ],
    )

    assert second.exit_code != 0
    assert "Could not update Claude Code settings" in second.output
    assert settings_path.read_text() == previous_settings
    assert state_path.read_text() == previous_state


def test_the_key_is_stored_privately_and_never_printed(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _mock_models(monkeypatch)

    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "claude-code", "--api-key", "router-secret"],
    )

    assert result.exit_code == 0, result.output
    assert "router-secret" not in result.output
    path = tmp_path / "settings.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (
        json.loads(path.read_text())["env"]["ANTHROPIC_AUTH_TOKEN"] == "router-secret"
    )


def test_discovery_is_enabled_with_the_projection_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    seen = _mock_models(monkeypatch)

    CliRunner().invoke(
        cli, ["--human", "router", "configure", "claude-code", "--api-key", "secret"]
    )

    env = json.loads((tmp_path / "settings.json").read_text())["env"]
    assert env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "X-Gateway-Client: claude-code"
    # The default model has to come from the list Claude Code itself will see,
    # or the id written here is one its picker does not recognize.
    assert any(headers.get("X-Gateway-Client") == "claude-code" for headers in seen)


def test_capability_overrides_are_not_written(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _mock_models(monkeypatch)

    CliRunner().invoke(
        cli, ["--human", "router", "configure", "claude-code", "--api-key", "secret"]
    )

    # These have no effect behind ANTHROPIC_BASE_URL, so writing them would
    # only imply a per-model effort UI that Claude Code cannot render.
    env = json.loads((tmp_path / "settings.json").read_text())["env"]
    assert not [key for key in env if key.endswith("_SUPPORTED_CAPABILITIES")]


def test_an_existing_api_key_is_cleared_and_restored(tmp_path):
    # Two auth variables at once is ambiguous to Claude Code, so a profile that
    # previously used an API key could send the stale credential instead of the
    # Router token.
    path = tmp_path / "settings.json"
    original = {"env": {"ANTHROPIC_API_KEY": "sk-old", "MY_OWN": "keep"}}

    updated, state = claude_code.plan_configuration(
        original, path, "https://router-api.ramp.com", "router-secret", "gpt-5.4"
    )

    assert "ANTHROPIC_API_KEY" not in updated["env"]
    assert updated["env"]["ANTHROPIC_AUTH_TOKEN"] == "router-secret"
    assert updated["env"]["MY_OWN"] == "keep"

    # It was the user's, so unconfigure has to give it back.
    assert claude_code.plan_restoration(updated, path, state) == original


def test_settings_are_read_after_the_model_request(monkeypatch, tmp_path):
    # The settings are rewritten whole, so a snapshot taken before the network
    # call would discard anything written while the request was in flight.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"env": {}}))

    def get(url, *, headers, timeout):
        path.write_text(json.dumps({"theme": "written-mid-flight", "env": {}}))
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "gpt-5.4",
                        "owned_by": "openai",
                        "router": _router_metadata("gpt-5.4"),
                    }
                ]
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(router_module.httpx, "get", get)

    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "claude-code", "--api-key", "secret"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(path.read_text())["theme"] == "written-mid-flight"


def test_a_model_chosen_after_configuring_survives_unconfigure(tmp_path):
    # Claude Code writes this itself when someone picks a different model.
    # Replacing it with the snapshot from before Router was configured would
    # silently undo their choice.
    path = tmp_path / "settings.json"
    updated, state = claude_code.plan_configuration(
        {"model": "claude-opus-4-8"}, path, "https://router.example", "k", "gpt-5.6-sol"
    )
    assert updated["model"] == "gpt-5.6-sol"

    chosen_since = {**updated, "model": "claude-sonnet-4-6"}
    restored = claude_code.plan_restoration(chosen_since, path, state)

    assert restored["model"] == "claude-sonnet-4-6"


def test_an_untouched_setting_is_still_put_back(tmp_path):
    path = tmp_path / "settings.json"
    updated, state = claude_code.plan_configuration(
        {"model": "claude-opus-4-8"}, path, "https://router.example", "k", "gpt-5.6-sol"
    )

    restored = claude_code.plan_restoration(updated, path, state)

    assert restored["model"] == "claude-opus-4-8"
    assert "ANTHROPIC_BASE_URL" not in restored.get("env", {})


def test_a_gateway_pointed_elsewhere_since_is_left_alone(tmp_path):
    path = tmp_path / "settings.json"
    updated, state = claude_code.plan_configuration(
        {}, path, "https://router.example", "k", "gpt-5.6-sol"
    )

    moved = {
        **updated,
        "env": {**updated["env"], "ANTHROPIC_BASE_URL": "https://elsewhere"},
    }
    restored = claude_code.plan_restoration(moved, path, state)

    assert restored["env"]["ANTHROPIC_BASE_URL"] == "https://elsewhere"
