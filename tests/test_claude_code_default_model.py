"""Claude Code keeps its own default model unless someone chose another.

Picking Default in Claude Code's ``/model`` removes the saved ``model``.
Router used to treat that as an empty slot and save the first model it
listed (Fable), so the next session started on Fable again.
"""

import json
import stat
import sys

import pytest

from ramp_cli.commands import claude_code, claude_code_default, router

_probe_builtin_default = claude_code_default.builtin_default_model
KEY = claude_code.ROUTER_DEFAULT_MODEL_STATE_KEY


def _model(request_name, *, one_million=True):
    identifier = f"{request_name}[1m]" if one_million else request_name
    return router.RouterModel(
        id=identifier,
        metadata=router.RouterModelMetadata(
            request_name=request_name,
            provider_model=request_name,
            display_name=request_name,
            description="",
            listing_order=0,
            efforts=(),
            default_effort="",
            input_modalities=("text",),
            context_window=1000000,
            max_output_tokens=16384,
            tool_calls=True,
        ),
    )


# Router's Claude Code view lists models alphabetically, so Fable comes first.
MODELS = [
    _model("claude-fable-latest"),
    _model("claude-fable-5-2"),
    _model("claude-haiku-5-5"),
    _model("claude-opus-latest"),
    _model("claude-opus-5-5"),
    _model("claude-sonnet-latest"),
    _model("claude-sonnet-5-5"),
]


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(
        router, "_require_truthful_claude_code_model_names", lambda: None
    )
    monkeypatch.setattr(router, "_claude_code_supports_model_picker", lambda: False)
    monkeypatch.setattr(router, "_install_claude_code_statusline", lambda *a: None)
    monkeypatch.setattr(router, "_scrub_stale_fable_verdict", lambda: None)
    path = tmp_path / "settings.json"
    world = {"builtin": "claude-opus-5-5", "admin": None}
    monkeypatch.setattr(
        claude_code_default, "builtin_default_model", lambda *a: world["builtin"]
    )
    monkeypatch.setattr(
        router.admin_defaults,
        "coding_agent_settings",
        lambda *a, **k: (
            {"claude-code": {"effective_model": world["admin"]}}
            if world["admin"]
            else {}
        ),
    )

    def configure(settings=None, **options):
        if settings is not None or not path.exists():
            path.write_text(json.dumps(settings or {}))
        return router._configure_claude_code(
            path,
            "synthetic-key",
            MODELS,
            base_url="https://router.example/v1",
            **options,
        )

    def refresh():
        return router._refresh_claude_code_model(
            path,
            "synthetic-key",
            MODELS,
            base_url="https://router.example/v1",
            selected_model=claude_code.read_settings(path).get("model"),
        )

    def choose_default_in_claude_code():
        settings = claude_code.read_settings(path)
        settings.pop("model", None)
        path.write_text(json.dumps(settings))

    return path, world, configure, refresh, choose_default_in_claude_code


def _saved(path):
    return claude_code.read_settings(path).get("model")


def test_setup_leaves_claude_code_on_its_own_default(setup):
    path, _, configure, _, _ = setup
    assert configure() is None
    assert "model" not in claude_code.read_settings(path)
    state = claude_code.read_state(path)
    assert state[KEY] is None
    assert claude_code._valid_state(state)


def test_choosing_default_in_claude_code_survives_refresh(setup):
    path, _, configure, refresh, choose_default = setup
    configure(explicit_model=True, selected_model="claude-sonnet-5-5[1m]")
    assert _saved(path) == "claude-sonnet-5-5[1m]"
    choose_default()
    for _ in range(3):
        refresh()
        assert "model" not in claude_code.read_settings(path)
    configure()
    assert "model" not in claude_code.read_settings(path)


def test_a_chosen_model_survives_refresh_and_reconfigure(setup):
    path, _, configure, refresh, _ = setup
    configure()
    settings = claude_code.read_settings(path)
    settings["model"] = "claude-sonnet-5-5[1m]"
    path.write_text(json.dumps(settings))
    refresh()
    configure()
    assert _saved(path) == "claude-sonnet-5-5[1m]"


@pytest.mark.parametrize("builtin", [None, "claude-opus-9-9"])
def test_unusable_claude_code_default_pins_opus_not_the_first_listed(setup, builtin):
    path, world, configure, refresh, choose_default = setup
    world["builtin"] = builtin
    assert configure() == "claude-opus-latest[1m]"
    assert claude_code.read_state(path)[KEY] == "claude-opus-latest[1m]"
    # Claude Code's default would fail, so Router keeps a working model saved.
    choose_default()
    refresh()
    assert _saved(path) == "claude-opus-latest[1m]"


def test_router_pin_gives_way_once_claude_code_default_is_served(setup):
    path, world, configure, refresh, _ = setup
    world["builtin"] = None
    configure()
    assert _saved(path) == "claude-opus-latest[1m]"
    world["builtin"] = "claude-opus-5-5"
    refresh()
    assert "model" not in claude_code.read_settings(path)
    assert claude_code.read_state(path)[KEY] is None


def test_older_receipt_moves_off_the_first_listed_model_once(setup):
    path, _, configure, refresh, _ = setup
    configure()
    # What an older CLI left behind: Fable saved as Router's own choice.
    settings = claude_code.read_settings(path)
    settings["model"] = "claude-fable-latest[1m]"
    path.write_text(json.dumps(settings))
    state = claude_code.read_state(path)
    state.pop(KEY)
    state["written"]["top_level"]["model"] = "claude-fable-latest[1m]"
    claude_code.state_path(path).write_text(json.dumps(state))
    refresh()
    assert "model" not in claude_code.read_settings(path)
    assert claude_code.read_state(path)[KEY] is None


def test_older_receipt_keeps_a_model_chosen_through_router(setup):
    path, _, configure, refresh, _ = setup
    configure(explicit_model=True, selected_model="claude-sonnet-5-5[1m]")
    state = claude_code.read_state(path)
    state.pop(KEY)
    claude_code.state_path(path).write_text(json.dumps(state))
    refresh()
    assert _saved(path) == "claude-sonnet-5-5[1m]"


def test_admin_default_applies_and_clearing_it_returns_to_claude_code(setup):
    path, world, configure, refresh, choose_default = setup
    configure()
    world["admin"] = "claude-fable-5-2"
    refresh()
    assert _saved(path) == "claude-fable-5-2[1m]"
    # The hooks put the workspace default back after a session's own pick.
    choose_default()
    assert router._apply_claude_code_admin_default(path)
    assert _saved(path) == "claude-fable-5-2[1m]"
    world["admin"] = None
    refresh()
    assert "model" not in claude_code.read_settings(path)


def test_clearing_admin_default_restores_the_users_model(setup):
    path, world, configure, refresh, _ = setup
    configure(explicit_model=True, selected_model="claude-sonnet-5-5[1m]")
    world["admin"] = "claude-fable-5-2"
    refresh()
    assert _saved(path) == "claude-fable-5-2[1m]"
    world["admin"] = None
    refresh()
    assert _saved(path) == "claude-sonnet-5-5[1m]"


def test_unconfigure_restores_the_pre_router_model(setup):
    path, _, configure, _, _ = setup
    configure({"model": "sonnet", "theme": "dark"})
    assert "model" not in claude_code.read_settings(path)
    router._unconfigure_claude_code(path)
    assert claude_code.read_settings(path) == {"model": "sonnet", "theme": "dark"}


def test_fallback_order():
    def names(*request_names):
        return [_model(name) for name in request_names]

    fallback = router._claude_code_fallback_model
    assert fallback(MODELS) == "claude-opus-latest[1m]"
    assert (
        fallback(names("claude-fable-5-2", "claude-opus-4-6", "claude-opus-5-5"))
        == "claude-opus-5-5[1m]"
    )
    assert (
        fallback(names("claude-fable-5-2", "claude-sonnet-5-5"))
        == "claude-sonnet-5-5[1m]"
    )
    assert fallback(names("claude-fable-5-2")) == "claude-fable-5-2[1m]"


def test_legacy_pick_uses_the_old_preferred_default_not_todays():
    # Older CLIs preferred gpt-5.6-sol; a listed gpt-6.1-sol was a user's choice.
    listed = [_model("gpt-6.1-sol"), _model("gpt-5.6-sol"), _model("claude-fable")]
    assert router._legacy_claude_code_default(listed) == "gpt-5.6-sol[1m]"
    assert router._legacy_claude_code_default(listed[:1]) == "gpt-6.1-sol[1m]"


def test_served_matches_canonical_ids_and_1m_suffixes():
    assert router._claude_code_serves("claude-opus-5-5", MODELS)
    assert router._claude_code_serves("claude-opus-5-5[1m]", MODELS)
    assert router._claude_code_serves("CLAUDE-OPUS-5-5", MODELS)
    assert not router._claude_code_serves("claude-opus-9-9", MODELS)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake executable")
def test_probe_reads_init_model_once_per_version(tmp_path, monkeypatch):
    calls = tmp_path / "calls"
    fake = tmp_path / "claude"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "2.1.999 (Claude Code)"; exit 0; fi\n'
        f'echo run >> "{calls}"\n'
        # The probe must hide the caller's own overrides and point nowhere.
        '[ -n "$ANTHROPIC_MODEL" ] && exit 1\n'
        '[ "$ANTHROPIC_BASE_URL" = "http://127.0.0.1:9" ] || exit 1\n'
        'echo \'{"type":"system","subtype":"init","model":"claude-opus-5-5"}\'\n'
        "sleep 30\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("ANTHROPIC_MODEL", "user-override")
    assert _probe_builtin_default(str(fake)) == "claude-opus-5-5"
    assert _probe_builtin_default(str(fake)) == "claude-opus-5-5"
    assert calls.read_text().count("run") == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake executable")
def test_probe_failure_reads_as_unknown_and_is_not_cached(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "2.1.998"; exit 0; fi\n'
        "echo not-json\n"
        "exit 1\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    assert _probe_builtin_default(str(fake)) is None
    assert "2.1.998" not in claude_code_default._read_cache()
