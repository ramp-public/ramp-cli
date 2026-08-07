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

    assert updated["disableClaudeAiConnectors"] is True
    restored = claude_code.plan_restoration(updated, path, state)

    assert restored == {}


def test_subagent_update_writes_tiers_and_unconfigure_restores_them(tmp_path):
    path = tmp_path / "settings.json"
    original = {"env": {"ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5"}}

    configured, state = claude_code.plan_configuration(
        original, path, "https://router-api.ramp.com", "secret", "gpt-5.4"
    )
    updated, state = claude_code.plan_subagent_update(
        configured,
        path,
        state,
        {"sonnet": "claude-router-a", "haiku": "claude-router-b"},
        display_names={"sonnet": "GPT-5.6 Terra", "haiku": "GPT-5 Mini"},
        descriptions={
            "sonnet": "Balanced Router model",
            "haiku": "Fast Router model",
        },
    )

    assert updated["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "claude-router-a"
    assert updated["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-router-b"
    assert updated["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL_NAME"] == "GPT-5.6 Terra"
    assert updated["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME"] == "GPT-5 Mini"
    assert (
        updated["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL_DESCRIPTION"]
        == "Balanced Router model"
    )
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in updated["env"]

    # A tier override naming a Router-served model is meaningless once Router
    # is unconfigured, so restoration puts back exactly what was there before.
    restored = claude_code.plan_restoration(updated, path, state)
    assert restored == original


def test_first_tier_write_snapshots_the_current_hand_set_value(tmp_path):
    path = tmp_path / "settings.json"
    # Configure runs on a profile with no tier value, so its snapshot marks
    # the key absent. The user then hand-sets the tier before the first
    # subagents write.
    configured, state = claude_code.plan_configuration(
        {}, path, "https://router-api.ramp.com", "secret", "gpt-5.4"
    )
    configured["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] = "user-value"

    updated, state = claude_code.plan_subagent_update(
        configured, path, state, {"sonnet": "claude-router-a"}
    )
    # The first write must snapshot the value that is there at write time,
    # not the stale absence captured at configure time; unconfigure then
    # puts the user's hand-set value back.
    restored = claude_code.plan_restoration(updated, path, state)
    assert restored["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "user-value"


def test_subagent_update_backfills_state_written_by_an_older_cli(tmp_path):
    path = tmp_path / "settings.json"
    original = {"env": {"ANTHROPIC_DEFAULT_SONNET_MODEL": "hand-set"}}
    configured, state = claude_code.plan_configuration(
        original, path, "https://router-api.ramp.com", "secret", "gpt-5.4"
    )
    # An older CLI captured no snapshot for the tier keys. That absence means
    # "not captured", so the update has to take its snapshot from the settings
    # as they are, or unconfigure would delete the user's hand-set value.
    for key in claude_code.SUBAGENT_ENV_KEYS:
        state["env"].pop(key, None)
        state["written"]["env"].pop(key, None)
    assert claude_code._valid_state(state)

    updated, state = claude_code.plan_subagent_update(
        configured, path, state, {"sonnet": "claude-router-a"}
    )
    assert claude_code._valid_state(state)
    restored = claude_code.plan_restoration(updated, path, state)

    assert restored == original


def test_refresh_does_not_claim_a_tier_the_user_edited_themselves(tmp_path):
    path = tmp_path / "settings.json"
    configured, state = claude_code.plan_configuration(
        {}, path, "https://router-api.ramp.com", "secret", "gpt-5.4"
    )
    # The user points a tier somewhere by hand, then a refresh reruns
    # configure. Configure never writes tier keys, so it must not record the
    # user's value as its own: unconfigure would otherwise replace their
    # newer choice with the pre-Router snapshot.
    configured["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] = "their-own-pick"
    refreshed, fresh = claude_code.plan_configuration(
        configured, path, "https://router-api.ramp.com", "secret", "gpt-5.4"
    )
    state = claude_code.merge_states(state, fresh)

    restored = claude_code.plan_restoration(refreshed, path, state)

    assert restored["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "their-own-pick"


def test_subagent_ownership_survives_a_refresh(tmp_path):
    path = tmp_path / "settings.json"
    configured, state = claude_code.plan_configuration(
        {}, path, "https://router-api.ramp.com", "secret", "gpt-5.4"
    )
    updated, state = claude_code.plan_subagent_update(
        configured, path, state, {"sonnet": "claude-router-a"}
    )
    # A refresh reruns configure over the updated settings. The tier write
    # must remain recorded as Router's, or unconfigure would leave the
    # Router-only model id behind for a gateway that cannot serve it.
    refreshed, fresh = claude_code.plan_configuration(
        updated, path, "https://router-api.ramp.com", "secret", "gpt-5.4"
    )
    state = claude_code.merge_states(state, fresh)

    restored = claude_code.plan_restoration(refreshed, path, state)

    assert "ANTHROPIC_DEFAULT_SONNET_MODEL" not in restored.get("env", {})


def test_deleting_a_pre_router_tier_edit_is_preserved(tmp_path):
    path = tmp_path / "settings.json"
    # A tier value was hand-set before Router. Configure captures it in the
    # state snapshot for restoration but never claims it as its own write.
    original = {"env": {"ANTHROPIC_DEFAULT_SONNET_MODEL": "their-own-pick"}}
    configured, state = claude_code.plan_configuration(
        original, path, "https://router-api.ramp.com", "secret", "gpt-5.4"
    )
    # The user then deletes the override themselves while Router stays
    # configured. That deletion must survive unconfigure: two None reads on
    # the same key must not read as "still ours".
    configured["env"].pop("ANTHROPIC_DEFAULT_SONNET_MODEL")
    restored = claude_code.plan_restoration(configured, path, state)

    assert "ANTHROPIC_DEFAULT_SONNET_MODEL" not in restored.get("env", {})


def test_settings_lock_excludes_a_second_holder(tmp_path):
    fcntl = pytest.importorskip("fcntl")
    path = tmp_path / "settings.json"

    with claude_code.settings_lock(path):
        lock_path = tmp_path / ".ramp-router-settings.lock"
        assert lock_path.exists()
        with lock_path.open("a+b") as second:
            with pytest.raises(OSError):
                fcntl.flock(second.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    # Released on exit: a fresh attempt succeeds.
    with claude_code.settings_lock(path):
        pass


def test_subagent_reset_removes_the_override(tmp_path):
    path = tmp_path / "settings.json"
    configured, state = claude_code.plan_configuration(
        {}, path, "https://router-api.ramp.com", "secret", "gpt-5.4"
    )
    updated, state = claude_code.plan_subagent_update(
        configured, path, state, {"opus": "claude-router-a"}
    )
    cleared, state = claude_code.plan_subagent_update(
        updated, path, state, dict.fromkeys(claude_code.SUBAGENT_TIER_ENV_KEYS)
    )

    assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in cleared["env"]
    assert claude_code.plan_restoration(cleared, path, state) == {}


def test_unconfigure_restores_existing_connector_preference(tmp_path):
    path = tmp_path / "settings.json"
    original = {"disableClaudeAiConnectors": False}
    updated, state = claude_code.plan_configuration(
        original, path, "https://router-api.ramp.com", "secret", "gpt-5.4"
    )

    assert updated["disableClaudeAiConnectors"] is True
    assert claude_code.plan_restoration(updated, path, state) == original


def test_connector_preference_changed_since_configure_is_left_alone(tmp_path):
    path = tmp_path / "settings.json"
    updated, state = claude_code.plan_configuration(
        {}, path, "https://router-api.ramp.com", "secret", "gpt-5.4"
    )
    changed = {**updated, "disableClaudeAiConnectors": False}

    restored = claude_code.plan_restoration(changed, path, state)

    assert restored["disableClaudeAiConnectors"] is False


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


def test_reconfigure_upgrades_legacy_state_for_connector_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _mock_models(monkeypatch)
    path = tmp_path / "settings.json"
    configured, state = claude_code.plan_configuration(
        {}, path, "https://router-api.ramp.com", "old-key", "gpt-5.4"
    )
    configured.pop("disableClaudeAiConnectors")
    state["top_level"].pop("disableClaudeAiConnectors")
    state["written"]["top_level"].pop("disableClaudeAiConnectors")
    path.write_text(json.dumps(configured))
    claude_code.state_path(path).write_text(json.dumps(state))

    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "claude-code",
            "--api-key",
            "replacement-key",
        ],
    )

    assert result.exit_code == 0, result.output
    upgraded = claude_code.read_state(path)
    assert upgraded["top_level"]["disableClaudeAiConnectors"] == {
        "present": False,
        "value": None,
    }
    assert json.loads(path.read_text())["disableClaudeAiConnectors"] is True

    result = CliRunner().invoke(
        cli, ["--human", "router", "unconfigure", "claude-code"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(path.read_text()) == {}


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


_STATUSLINE_SCRIPT = "#!/usr/bin/env python3\nprint('router statusline')\n"


def _mock_router_endpoints(monkeypatch, script=_STATUSLINE_SCRIPT):
    """Serve both the model list and the status line asset."""
    models = [
        {
            "id": "gpt-5.4",
            "owned_by": "openai",
            "router": _router_metadata("gpt-5.4"),
        }
    ]
    urls = []

    def get(url, *, headers, timeout):
        urls.append(url)
        request = httpx.Request("GET", url)
        if url.endswith("/claude-code-statusline"):
            if script is None:
                raise httpx.ConnectError("unreachable", request=request)
            return httpx.Response(200, text=script, request=request)
        return httpx.Response(200, json={"data": models}, request=request)

    monkeypatch.setattr(router_module.httpx, "get", get)
    return urls


def test_configure_installs_the_status_line(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    urls = _mock_router_endpoints(monkeypatch)

    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "claude-code", "--api-key", "secret"]
    )

    assert result.exit_code == 0, result.output
    # Served by the dashboard origin, not the data plane the gateway URL names.
    assert "https://router.ramp.com/claude-code-statusline" in urls
    script = tmp_path / "ramp-router-statusline"
    assert script.read_text() == _STATUSLINE_SCRIPT
    assert script.stat().st_mode & stat.S_IXUSR
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings["statusLine"] == {
        "type": "command",
        "command": claude_code.statusline_command(tmp_path / "settings.json"),
    }
    # The script's ANTHROPIC_BASE_URL fallback points at the data plane, which
    # does not serve the session-usage endpoint, so the control-plane origin
    # has to be written explicitly.
    assert settings["env"]["ROUTER_BASE_URL"] == "https://router.ramp.com"


def test_a_status_line_the_user_configured_is_left_alone(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _mock_router_endpoints(monkeypatch)
    theirs = {"type": "command", "command": "/home/me/my-statusline.sh"}
    settings_path = tmp_path / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"statusLine": theirs}))

    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "claude-code", "--api-key", "secret"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(settings_path.read_text())["statusLine"] == theirs


def test_a_repeat_configure_refreshes_the_status_line(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    runner = CliRunner()
    _mock_router_endpoints(monkeypatch)
    first = runner.invoke(
        cli, ["--human", "router", "configure", "claude-code", "--api-key", "secret"]
    )
    assert first.exit_code == 0, first.output

    fixed = "#!/usr/bin/env python3\nprint('fixed')\n"
    _mock_router_endpoints(monkeypatch, script=fixed)
    second = runner.invoke(
        cli, ["--human", "router", "configure", "claude-code", "--api-key", "secret"]
    )

    assert second.exit_code == 0, second.output
    assert (tmp_path / "ramp-router-statusline").read_text() == fixed
    # Still wired: our own entry is ours to rewrite.
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings["statusLine"]["command"] == claude_code.statusline_command(
        tmp_path / "settings.json"
    )


def test_a_failed_status_line_download_does_not_fail_configure(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _mock_router_endpoints(monkeypatch, script=None)

    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "claude-code", "--api-key", "secret"]
    )

    assert result.exit_code == 0, result.output
    assert "Skipping the Claude Code Router status line" in result.output
    assert not (tmp_path / "ramp-router-statusline").exists()
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert "statusLine" not in settings
    # The gateway configuration itself still went through.
    assert settings["env"]["ANTHROPIC_AUTH_TOKEN"] == "secret"


def test_a_block_page_is_not_installed_as_the_status_line(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _mock_router_endpoints(monkeypatch, script="<html>blocked</html>")

    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "claude-code", "--api-key", "secret"]
    )

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "ramp-router-statusline").exists()
    assert "statusLine" not in json.loads((tmp_path / "settings.json").read_text())


def test_a_missing_python3_skips_the_status_line(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _mock_router_endpoints(monkeypatch)
    monkeypatch.setattr(router_module.shutil, "which", lambda name: None)

    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "claude-code", "--api-key", "secret"]
    )

    assert result.exit_code == 0, result.output
    assert "python3 was not found" in result.output
    assert not (tmp_path / "ramp-router-statusline").exists()


def test_unconfigure_removes_the_status_line(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _mock_router_endpoints(monkeypatch)
    runner = CliRunner()
    configured = runner.invoke(
        cli, ["--human", "router", "configure", "claude-code", "--api-key", "secret"]
    )
    assert configured.exit_code == 0, configured.output

    result = runner.invoke(cli, ["--human", "router", "unconfigure", "claude-code"])

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "ramp-router-statusline").exists()
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert "statusLine" not in settings
    assert "ROUTER_BASE_URL" not in settings.get("env", {})


def test_a_status_line_changed_since_is_left_by_unconfigure(tmp_path):
    path = tmp_path / "settings.json"
    ours = claude_code.statusline_command(path)
    updated, state = claude_code.plan_configuration(
        {},
        path,
        "https://router-api.ramp.com",
        "secret",
        "gpt-5.4",
        statusline=ours,
    )
    assert updated["statusLine"] == {"type": "command", "command": ours}

    theirs = {"type": "command", "command": "/home/me/mine.sh"}
    changed_since = {**updated, "statusLine": theirs}
    restored = claude_code.plan_restoration(changed_since, path, state)

    assert restored["statusLine"] == theirs


def _legacy_state(path, settings):
    """Write the state a CLI predating the status line would have left."""
    updated, state = claude_code.plan_configuration(
        settings, path, "https://router-api.ramp.com", "secret", "gpt-5.4"
    )
    state["env"].pop("ROUTER_BASE_URL")
    state["written"]["env"].pop("ROUTER_BASE_URL")
    state.pop("statusLine", None)
    claude_code.state_path(path).parent.mkdir(parents=True, exist_ok=True)
    claude_code.state_path(path).write_text(json.dumps(state) + "\n")
    return updated


def test_state_written_before_the_status_line_still_reads(tmp_path):
    # A state file from a CLI predating the status line lacks its snapshots.
    # It must still read, and restoring from it must not delete values this
    # older configure never wrote.
    path = tmp_path / "settings.json"
    updated = _legacy_state(path, {})

    read = claude_code.read_state(path)
    later = {
        **updated,
        "statusLine": {"type": "command", "command": "/home/me/mine.sh"},
        "env": {**updated["env"], "ROUTER_BASE_URL": "https://mine.example"},
    }
    restored = claude_code.plan_restoration(later, path, read)

    assert restored["statusLine"]["command"] == "/home/me/mine.sh"
    assert restored["env"]["ROUTER_BASE_URL"] == "https://mine.example"


def test_a_reconfigure_over_legacy_state_keeps_values_set_since(monkeypatch, tmp_path):
    # A pre-status-line state file has no snapshot for the newly owned keys.
    # A repeat configure must adopt fresh snapshots for them, or unconfigure
    # restores their presumed absence and deletes what the user set since.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _mock_router_endpoints(monkeypatch)
    settings_path = tmp_path / "settings.json"
    theirs = {"type": "command", "command": "/home/me/my-statusline.sh"}
    settings = {
        "statusLine": theirs,
        "env": {"ROUTER_BASE_URL": "https://mine.example"},
    }
    updated = _legacy_state(settings_path, settings)
    settings_path.write_text(
        json.dumps(
            {
                **updated,
                "statusLine": theirs,
                "env": {**updated["env"], "ROUTER_BASE_URL": "https://mine.example"},
            }
        )
    )

    runner = CliRunner()
    reconfigured = runner.invoke(
        cli, ["--human", "router", "configure", "claude-code", "--api-key", "secret"]
    )
    assert reconfigured.exit_code == 0, reconfigured.output
    # The user's own status line survives the reconfigure untouched, while the
    # usage origin is Router's to overwrite.
    settings = json.loads(settings_path.read_text())
    assert settings["statusLine"] == theirs
    assert settings["env"]["ROUTER_BASE_URL"] == "https://router.ramp.com"

    unconfigured = runner.invoke(
        cli, ["--human", "router", "unconfigure", "claude-code"]
    )

    assert unconfigured.exit_code == 0, unconfigured.output
    settings = json.loads(settings_path.read_text())
    assert settings["statusLine"] == theirs
    assert settings["env"]["ROUTER_BASE_URL"] == "https://mine.example"


def test_a_user_status_line_never_becomes_router_state(monkeypatch, tmp_path):
    # A user with their own status line configures Router, then changes their
    # status line, then reconfigures. The slot was never Router's, so neither
    # configure may record it, and unconfigure must leave the newest choice.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _mock_router_endpoints(monkeypatch)
    settings_path = tmp_path / "settings.json"
    first = {"type": "command", "command": "/home/me/first.sh"}
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"statusLine": first}))

    runner = CliRunner()
    configured = runner.invoke(
        cli, ["--human", "router", "configure", "claude-code", "--api-key", "secret"]
    )
    assert configured.exit_code == 0, configured.output
    assert "statusLine" not in json.loads(
        (tmp_path / "ramp-router-state.json").read_text()
    )

    second = {"type": "command", "command": "/home/me/second.sh"}
    settings = json.loads(settings_path.read_text())
    settings_path.write_text(json.dumps({**settings, "statusLine": second}))
    reconfigured = runner.invoke(
        cli, ["--human", "router", "configure", "claude-code", "--api-key", "secret"]
    )
    assert reconfigured.exit_code == 0, reconfigured.output
    assert json.loads(settings_path.read_text())["statusLine"] == second

    unconfigured = runner.invoke(
        cli, ["--human", "router", "unconfigure", "claude-code"]
    )

    assert unconfigured.exit_code == 0, unconfigured.output
    assert json.loads(settings_path.read_text())["statusLine"] == second


def test_a_slot_taken_since_the_first_configure_is_freed_again(monkeypatch, tmp_path):
    # First configure could not install the status line; the user still has
    # none when a repeat configure succeeds and takes the empty slot. The
    # merged state must adopt that fresh snapshot so unconfigure frees it.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    runner = CliRunner()
    _mock_router_endpoints(monkeypatch, script=None)
    first = runner.invoke(
        cli, ["--human", "router", "configure", "claude-code", "--api-key", "secret"]
    )
    assert first.exit_code == 0, first.output
    settings_path = tmp_path / "settings.json"
    assert "statusLine" not in json.loads(settings_path.read_text())

    _mock_router_endpoints(monkeypatch)
    second = runner.invoke(
        cli, ["--human", "router", "configure", "claude-code", "--api-key", "secret"]
    )
    assert second.exit_code == 0, second.output
    assert json.loads(settings_path.read_text())["statusLine"] == {
        "type": "command",
        "command": claude_code.statusline_command(settings_path),
    }

    unconfigured = runner.invoke(
        cli, ["--human", "router", "unconfigure", "claude-code"]
    )

    assert unconfigured.exit_code == 0, unconfigured.output
    assert "statusLine" not in json.loads(settings_path.read_text())


def test_the_status_line_origin_follows_a_base_url_override(monkeypatch):
    monkeypatch.delenv("RAMP_ROUTER_BASE_URL", raising=False)
    assert router_module._statusline_origin() == "https://router.ramp.com"

    # A base-URL override names a single-origin deployment, so the same host
    # serves the script and the usage endpoint.
    monkeypatch.setenv("RAMP_ROUTER_BASE_URL", "https://qa-router.ramp.dev/v1")
    assert router_module._statusline_origin() == "https://qa-router.ramp.dev"
