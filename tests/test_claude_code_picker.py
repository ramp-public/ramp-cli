"""The terminal picker is a receipt-owned lineup, not a model selection."""

import asyncio
import json
from unittest.mock import MagicMock

import click
import pytest
from click.testing import CliRunner

from ramp_cli.commands import claude_code, router
from ramp_cli.main import cli


def _model(identifier, label=None, order=0):
    return router.RouterModel(
        id=identifier,
        metadata=router.RouterModelMetadata(
            request_name=identifier,
            provider_model=identifier,
            display_name=label or identifier,
            description="",
            listing_order=order,
            efforts=(),
            default_effort="",
            input_modalities=("text",),
            context_window=128000,
            max_output_tokens=16384,
            tool_calls=True,
        ),
    )


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setattr(
        router, "_require_truthful_claude_code_model_names", lambda: None
    )
    monkeypatch.setattr(router, "_claude_code_supports_model_picker", lambda: True)
    monkeypatch.setattr(router, "_install_claude_code_statusline", lambda *a: None)
    monkeypatch.setattr(router.admin_defaults, "coding_agent_settings", lambda *a: None)
    monkeypatch.setattr(router, "_scrub_stale_fable_verdict", lambda: None)
    path = tmp_path / "settings.json"
    models = [_model("claude-router-first", "First")]

    def setup(settings=None):
        path.write_text(json.dumps(settings or {}))
        router._configure_claude_code(
            path,
            "synthetic-key",
            models,
            base_url="https://router.example/v1",
            model_view="all",
        )
        return path

    return setup, models


def _refresh(path, models):
    return router._refresh_claude_code_model(
        path,
        "synthetic-key",
        models,
        base_url="https://router.example/v1",
        selected_model=None,
    )


def test_setup_and_repeated_background_replacement_restore(configured):
    setup, _ = configured
    path = setup({"model": "sonnet", "permissions": {"allow": ["Read"]}})
    for identifier in ("claude-router-second", "claude-router-third"):
        _refresh(path, [_model(identifier, "Newest")])
        saved = claude_code.read_settings(path)
        assert saved["modelPicker"] == {
            "replaceBuiltInOptions": True,
            "options": [{"model": identifier, "label": "Newest"}],
        }
        assert saved["permissions"] == {"allow": ["Read"]}
        # Existing default semantics still migrate an unavailable Router id;
        # the picker planner itself never changes the session selection.
        assert saved["model"] == identifier
        assert claude_code._valid_state(claude_code.read_state(path))
    router._unconfigure_claude_code(path)
    assert claude_code.read_settings(path) == {
        "model": "sonnet",
        "permissions": {"allow": ["Read"]},
    }


@pytest.mark.parametrize("lineup", [{"options": [{"model": "mine"}]}, None, {}])
def test_preexisting_user_lineup_is_never_claimed(configured, lineup):
    setup, models = configured
    path = setup({"modelPicker": lineup})
    _refresh(path, models)
    router._configure_claude_code(path, "synthetic-key", models, model_view="all")
    assert claude_code.read_settings(path)["modelPicker"] == lineup
    assert "modelPicker" not in claude_code.read_state(path)
    router._unconfigure_claude_code(path)
    assert claude_code.read_settings(path) == {"modelPicker": lineup}


@pytest.mark.parametrize("delete", [False, True])
def test_user_edit_or_deletion_survives_refresh_reconfigure_and_unconfigure(
    configured, delete
):
    setup, models = configured
    path = setup()
    settings = claude_code.read_settings(path)
    if delete:
        settings.pop("modelPicker")
    else:
        settings["modelPicker"]["options"].append({"model": "mine", "label": "Mine"})
    path.write_text(json.dumps(settings))
    _refresh(path, [_model("claude-router-new")])
    router._configure_claude_code(path, "synthetic-key", models, model_view="all")
    assert claude_code.read_settings(path).get("modelPicker") == settings.get(
        "modelPicker"
    )
    router._unconfigure_claude_code(path)
    assert claude_code.read_settings(path).get("modelPicker") == settings.get(
        "modelPicker"
    )


def test_picker_refresh_preserves_a_user_saved_model(configured):
    setup, _ = configured
    path = setup()
    settings = claude_code.read_settings(path)
    settings["model"] = "user-session-choice"
    path.write_text(json.dumps(settings))
    assert _refresh(path, [_model("claude-router-new")]) == "user-session-choice"
    saved = claude_code.read_settings(path)
    assert saved["model"] == "user-session-choice"
    assert saved["modelPicker"]["options"][0]["model"] == "claude-router-new"


def test_compact_discovery_uses_claude_projection_and_preserves_default(
    configured, monkeypatch
):
    setup, models = configured
    path = setup()
    calls = []

    def fetch(key, **kwargs):
        calls.append((key, kwargs))
        return [_model("claude-router-compact", "Compact")]

    monkeypatch.setattr(router, "_fetch_models", fetch)
    router._write_claude_model_view(path, "compact")
    _refresh(path, models)
    saved = claude_code.read_settings(path)
    assert saved["modelPicker"]["options"] == [
        {"model": "claude-router-compact", "label": "Compact"}
    ]
    assert saved["model"] == models[0].id
    assert all(kwargs["claude_code_view"] for _, kwargs in calls)
    assert calls[-1][1]["model_view"] == "compact"
    router._write_claude_model_view(path, "all")
    assert (
        claude_code.read_settings(path)["modelPicker"]["options"][0]["model"]
        == "claude-router-compact"
    )
    assert calls[-1][1]["model_view"] == "all"


@pytest.mark.parametrize("override", router.BASE_URL_ENVS)
def test_reconfigure_picker_pairs_saved_key_with_saved_endpoint(
    configured, monkeypatch, override
):
    setup, _ = configured
    path = setup()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(path.parent))
    monkeypatch.setenv(override, "https://other-deployment.example/v1")
    original = claude_code.read_settings(path)
    calls = []

    def fetch(key, **kwargs):
        calls.append((key, kwargs))
        assert key == "synthetic-key"
        assert kwargs["base_url"] == "https://router.example/v1"
        return [_model("claude-router-updated")]

    monkeypatch.setattr(router, "_fetch_models", fetch)
    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "--no-input",
            "router",
            "configure",
            "claude-code",
            "--no-browser",
            "--claude-models",
            "all",
        ],
    )
    if override == router.ROUTER_BASE_URL_ENV:
        # The primary override already requires an explicit reconnect. The
        # legacy override can take the preference-only shortcut instead.
        assert result.exit_code == 2, result.output
        assert not calls
        assert claude_code.read_settings(path) == original
        return
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][1]["claude_code_view"] is True
    assert calls[0][1]["model_view"] == "all"
    saved = claude_code.read_settings(path)
    assert {
        k: v for k, v in saved["env"].items() if k != "ANTHROPIC_CUSTOM_HEADERS"
    } == {k: v for k, v in original["env"].items() if k != "ANTHROPIC_CUSTOM_HEADERS"}
    assert set(saved["env"]["ANTHROPIC_CUSTOM_HEADERS"].splitlines()) == set(
        original["env"]["ANTHROPIC_CUSTOM_HEADERS"].splitlines()
    )
    assert saved["model"] == original["model"]
    assert saved["modelPicker"]["options"] == [
        {"model": "claude-router-updated", "label": "claude-router-updated"}
    ]


@pytest.mark.parametrize("override", router.BASE_URL_ENVS)
def test_picker_writer_ignores_endpoint_overrides(configured, monkeypatch, override):
    setup, models = configured
    path = setup()
    monkeypatch.setenv(override, "https://other-deployment.example/v1")
    calls = []

    def fetch(key, **kwargs):
        calls.append(kwargs)
        assert key == "synthetic-key"
        assert kwargs["base_url"] == "https://router.example/v1"
        return models

    monkeypatch.setattr(router, "_fetch_models", fetch)
    router._write_claude_model_view(path, "all")
    assert len(calls) == 1


@pytest.mark.parametrize("key", ["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"])
def test_picker_reconfigure_detects_saved_connection_edit_despite_override(
    configured, monkeypatch, key
):
    setup, _ = configured
    path = setup()
    monkeypatch.setenv("LLM_GATEWAY_BASE_URL", "https://override.example/v1")
    state = claude_code.state_path(path).read_bytes()

    def fetch(api_key, **kwargs):
        assert api_key == "synthetic-key"
        assert kwargs["base_url"] == "https://router.example/v1"
        settings = claude_code.read_settings(path)
        settings["env"][key] = (
            "https://edited.example" if key == "ANTHROPIC_BASE_URL" else "edited-key"
        )
        path.write_text(json.dumps(settings))
        return [_model("claude-router-updated")]

    monkeypatch.setattr(router, "_fetch_models", fetch)
    picker = claude_code.read_settings(path)["modelPicker"]
    with pytest.raises(click.ClickException, match="connection changed"):
        router._write_claude_model_view(path, "all")
    assert claude_code.read_settings(path)["modelPicker"] == picker
    assert claude_code.state_path(path).read_bytes() == state


def test_picker_reconfigure_missing_saved_endpoint_fails_before_discovery(
    configured, monkeypatch
):
    setup, _ = configured
    path = setup()
    settings = claude_code.read_settings(path)
    settings["env"].pop("ANTHROPIC_BASE_URL")
    path.write_text(json.dumps(settings))
    before = path.read_bytes(), claude_code.state_path(path).read_bytes()
    monkeypatch.setenv("LLM_GATEWAY_BASE_URL", "https://override.example/v1")
    monkeypatch.setattr(
        router,
        "_fetch_models",
        lambda *a, **kw: pytest.fail("must not discover without a saved endpoint"),
    )
    with pytest.raises(click.ClickException, match="saved Claude Code Router endpoint"):
        router._write_claude_model_view(path, "all")
    assert (path.read_bytes(), claude_code.state_path(path).read_bytes()) == before


def test_failed_compact_discovery_preserves_good_settings_and_receipt(
    configured, monkeypatch
):
    setup, models = configured
    path = setup()
    settings = claude_code.read_settings(path)
    settings["env"]["ANTHROPIC_CUSTOM_HEADERS"] = settings["env"][
        "ANTHROPIC_CUSTOM_HEADERS"
    ].replace("X-Gateway-Model-View: all", "X-Gateway-Model-View: compact")
    path.write_text(json.dumps(settings))
    before = path.read_bytes(), claude_code.state_path(path).read_bytes()

    def fail(*a, **kw):
        raise click.ClickException("Discovery failed")

    monkeypatch.setattr(router, "_fetch_models", fail)
    with pytest.raises(click.ClickException, match="Discovery failed"):
        _refresh(path, models)
    assert (path.read_bytes(), claude_code.state_path(path).read_bytes()) == before


def test_older_receipt_adoption_updates_escape_overlay(configured):
    setup, models = configured
    path = setup()
    settings, state = claude_code.read_settings(path), claude_code.read_state(path)
    settings.pop("modelPicker")
    state.pop("modelPicker")
    path.write_text(json.dumps(settings))
    claude_code.state_path(path).write_text(json.dumps(state))
    overlay, body, claim = router._prepare_claude_original_settings(path, state)
    router._save_claude_original_settings(overlay, body, claim)
    claude_code.state_path(path).write_text(json.dumps(state))
    _refresh(path, models)
    assert claude_code.read_settings(claude_code.original_settings_path(path))[
        "modelPicker"
    ] == {"replaceBuiltInOptions": False, "options": []}
    router._unconfigure_claude_code(path)
    assert "modelPicker" not in claude_code.read_settings(path)


@pytest.mark.parametrize(
    "version, supported",
    [
        (None, False),
        ((2, 1, 118), False),
        ((2, 1, 241), False),
        ((2, 1, 242), True),
        ((2, 1, 319), True),
    ],
)
def test_documented_syntax_version_gate(monkeypatch, version, supported):
    monkeypatch.setattr(router.shutil, "which", lambda _: "/synthetic/claude")
    monkeypatch.setattr(router, "_claude_code_version", lambda _: version)
    assert router._claude_code_supports_model_picker() is supported


def test_missing_binary_does_not_assume_support(monkeypatch):
    monkeypatch.setattr(router.shutil, "which", lambda _: None)
    assert not router._claude_code_supports_model_picker()


def test_unsupported_version_retains_a_previously_good_lineup(configured, monkeypatch):
    setup, _ = configured
    path = setup()
    picker = claude_code.read_settings(path)["modelPicker"]
    monkeypatch.setattr(router, "_claude_code_supports_model_picker", lambda: False)
    _refresh(path, [_model("claude-router-new")])
    assert claude_code.read_settings(path)["modelPicker"] == picker
    assert "2.1.242" in " ".join(router._claude_code_picker_note_lines(path))


def test_picker_guidance_does_not_claim_every_session_updated(configured):
    setup, _ = configured
    path = setup()
    notes = " ".join(router._claude_code_picker_note_lines(path))
    assert "Reopen /model" in notes
    assert "--settings" in notes
    assert "not every running session is confirmed" in notes
    assert "does not switch the current session model" in notes
    assert "outer Claude Desktop" in notes


@pytest.mark.parametrize("reader", ["read_settings", "read_state"])
@pytest.mark.parametrize("error", [RuntimeError, OSError, click.ClickException])
def test_picker_status_read_errors_have_fixed_safe_fallback(
    configured, monkeypatch, reader, error
):
    setup, _ = configured
    path = setup()

    def fail(*_args):
        raise error("private-setting-value")

    monkeypatch.setattr(claude_code, reader, fail)
    assert router._claude_code_picker_note_lines(path) == [
        "Claude Code picker status could not be checked. "
        "Reopen /model in the terminal to check the lineup."
    ]


@pytest.mark.parametrize(
    "error", [KeyboardInterrupt, SystemExit, asyncio.CancelledError]
)
def test_picker_status_does_not_suppress_interruption(configured, monkeypatch, error):
    setup, _ = configured
    path = setup()

    def fail(*_args):
        raise error()

    monkeypatch.setattr(claude_code, "read_settings", fail)
    with pytest.raises(error):
        router._claude_code_picker_note_lines(path)


@pytest.mark.parametrize("error", [RuntimeError, click.ClickException])
@pytest.mark.parametrize("output", ["--human", "--agent"])
def test_post_save_picker_status_failure_keeps_success_and_refreshes_next_client(
    configured, monkeypatch, tmp_path, error, output
):
    setup, models = configured
    claude_path = setup()
    paths = {"claude-code": claude_path, "opencode": tmp_path / "opencode.json"}
    updated = []
    push = MagicMock()
    push.plan.return_value = None

    def configure(client, *_args, **_kwargs):
        paths[client].write_text(json.dumps({"saved": True}))
        updated.append(client)
        return paths[client], models[0].id, False

    def read_settings(_path):
        assert updated == ["claude-code"]
        raise error("private-setting-value")

    monkeypatch.setattr(router, "configured_router_clients", lambda: tuple(paths))
    monkeypatch.setattr(router, "_client_config_path", lambda client: paths[client])
    monkeypatch.setattr(router, "_stored_router_api_key", lambda *_a: "synthetic-key")
    monkeypatch.setattr(
        router, "_refresh_router_base_url", lambda *_a: "https://router.example/v1"
    )
    monkeypatch.setattr(router, "_stored_usage_origin", lambda *_a: None)
    monkeypatch.setattr(router, "_fetch_models", lambda *_a, **_kw: models)
    monkeypatch.setattr(router, "_configured_model", lambda *_a: None)
    monkeypatch.setattr(router, "_configured_claude_model_view_all", lambda *_a: False)
    monkeypatch.setattr(router, "_SwitchyardPush", lambda *_a: push)
    monkeypatch.setattr(router, "_configure_client", configure)
    monkeypatch.setattr(router, "_refresh_cowork_model_selections", lambda **_kw: None)
    monkeypatch.setattr(router, "_opencode_user_model_note", lambda *_a: None)
    monkeypatch.setattr(claude_code, "read_settings", read_settings)

    result = CliRunner().invoke(cli, [output, "router", "refresh"])
    assert result.exit_code == 0, result.output
    assert updated == ["claude-code", "opencode"]
    assert all(
        json.loads(path.read_text()) == {"saved": True} for path in paths.values()
    )
    assert "private-setting-value" not in result.output
    assert "picker status could not be checked" in result.output
    if output == "--agent":
        clients = json.loads(result.output)["data"][0]["clients"]
        assert [client["client"] for client in clients] == list(paths)
    else:
        assert (
            "Refreshed the Ramp Router configuration for Claude Code." in result.output
        )
        assert "Refreshed the Ramp Router configuration for OpenCode." in result.output


def test_picker_planning_errors_are_not_best_effort(configured, monkeypatch):
    setup, models = configured
    path = setup()
    before = path.read_bytes(), claude_code.state_path(path).read_bytes()

    def fail(*_args):
        raise RuntimeError("picker planning failed")

    monkeypatch.setattr(claude_code, "plan_model_picker_update", fail)
    with pytest.raises(RuntimeError, match="picker planning failed"):
        _refresh(path, models)
    assert (path.read_bytes(), claude_code.state_path(path).read_bytes()) == before


def test_empty_lineup_does_not_clear_a_good_picker(configured):
    setup, _ = configured
    path = setup()
    settings, state = claude_code.read_settings(path), claude_code.read_state(path)
    assert claude_code.plan_model_picker_update(settings, state, []) == (
        settings,
        state,
    )


def test_picker_planning_never_writes_the_model_selection(configured):
    setup, _ = configured
    path = setup()
    settings, state = claude_code.read_settings(path), claude_code.read_state(path)
    updated, new_state = claude_code.plan_model_picker_update(
        settings, state, [{"model": "new", "label": "New"}]
    )
    assert {k: v for k, v in updated.items() if k != "modelPicker"} == {
        k: v for k, v in settings.items() if k != "modelPicker"
    }
    assert new_state["written"] == state["written"]


def test_picker_user_edit_during_discovery_wins(configured, monkeypatch):
    setup, models = configured
    path = setup()
    mine = {"options": [{"model": "my-new-choice"}]}

    def discover(*a, **kw):
        settings = claude_code.read_settings(path)
        settings["modelPicker"] = mine
        settings["unrelated"] = "changed during discovery"
        path.write_text(json.dumps(settings))
        return None

    monkeypatch.setattr(router.admin_defaults, "coding_agent_settings", discover)
    _refresh(path, models)
    assert claude_code.read_settings(path)["modelPicker"] == mine
    assert claude_code.read_settings(path)["unrelated"] == "changed during discovery"


def test_model_view_changed_during_discovery_does_not_get_stale_lineup(
    configured, monkeypatch
):
    setup, models = configured
    path = setup()
    before = claude_code.read_settings(path)["modelPicker"]

    def discover(*a, **kw):
        settings = claude_code.read_settings(path)
        settings["env"]["ANTHROPIC_CUSTOM_HEADERS"] = settings["env"][
            "ANTHROPIC_CUSTOM_HEADERS"
        ].replace("X-Gateway-Model-View: all", "X-Gateway-Model-View: compact")
        path.write_text(json.dumps(settings))
        return None

    monkeypatch.setattr(router.admin_defaults, "coding_agent_settings", discover)
    _refresh(path, models + [_model("claude-router-more")])
    assert claude_code.read_settings(path)["modelPicker"] == before
    assert claude_code.model_view(claude_code.read_settings(path), path) == "compact"


def test_settings_write_failure_restores_picker_receipt(configured, monkeypatch):
    setup, _ = configured
    path = setup()
    before = path.read_bytes(), claude_code.state_path(path).read_bytes()
    write = router._write_private_file

    def fail(target, content):
        if target == path:
            raise OSError("synthetic write failure")
        write(target, content)

    monkeypatch.setattr(router, "_write_private_file", fail)
    with pytest.raises(click.ClickException, match="synthetic write failure"):
        _refresh(path, [_model("claude-router-new")])
    assert (path.read_bytes(), claude_code.state_path(path).read_bytes()) == before


def test_foreground_refresh_preserves_view_edited_during_discovery(
    configured, monkeypatch
):
    setup, models = configured
    path = setup()
    before = claude_code.read_settings(path)["modelPicker"]

    def discover(*a, **kw):
        settings = claude_code.read_settings(path)
        settings["env"]["ANTHROPIC_CUSTOM_HEADERS"] = settings["env"][
            "ANTHROPIC_CUSTOM_HEADERS"
        ].replace("X-Gateway-Model-View: all", "X-Gateway-Model-View: compact")
        path.write_text(json.dumps(settings))
        return None

    monkeypatch.setattr(router.admin_defaults, "coding_agent_settings", discover)
    router._configure_claude_code(
        path,
        "synthetic-key",
        models + [_model("claude-router-more")],
        model_view="all",
        preserve_model_view_ownership=True,
    )
    assert claude_code.model_view(claude_code.read_settings(path), path) == "compact"
    assert claude_code.read_settings(path)["modelPicker"] == before
