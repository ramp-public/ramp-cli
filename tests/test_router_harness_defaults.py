"""Without a workspace default, setups follow the harness, then Router's fallback.

Codex starts on its own default for the installed version. Harnesses without
a default of their own (OpenCode, Pi, Hermes, ...) start on the fallback
Router serves, so a new model can become the default without a CLI release.
Only a model Router picked on its own moves later; a chosen one stays.
"""

import json
import tomllib

import pytest

from ramp_cli import admin_defaults
from ramp_cli.commands import codex_default, router, router_picks
from tests.test_router import (
    _admin_default,
    _choose,
    _configure_agent,
    _mock_models,
    _refresh,
    _unconfigure_agent,
)

_fallback_model = admin_defaults.fallback_model
MODELS = [{"id": "gpt-6.1-sol"}, {"id": "gpt-7-sol"}, {"id": "gpt-5.5"}]


@pytest.fixture
def world(monkeypatch):
    state = {"fallback": {}, "codex": None}
    monkeypatch.setattr(
        admin_defaults,
        "fallback_model",
        lambda client, *_a, **_k: (
            state["fallback"].get(client) or state["fallback"].get("default")
        ),
    )
    monkeypatch.setattr(
        codex_default, "builtin_default_model", lambda *_a, **_k: state["codex"]
    )
    return state


def _codex_model(tmp_path):
    return tomllib.loads((tmp_path / "codex" / "config.toml").read_text())["model"]


def _opencode_model(tmp_path):
    config = json.loads((tmp_path / "opencode" / "opencode.json").read_text())
    return config["model"].removeprefix("ramp-router/")


@pytest.fixture
def codex(tmp_path, monkeypatch, world):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    _mock_models(monkeypatch, MODELS)
    return world


@pytest.fixture
def opencode(tmp_path, monkeypatch, world):
    monkeypatch.setenv("OPENCODE_CONFIG", str(tmp_path / "opencode" / "opencode.json"))
    _mock_models(monkeypatch, MODELS)
    return world


def test_codex_starts_on_its_own_default_and_follows_codex_updates(tmp_path, codex):
    codex["codex"] = "gpt-6.1-sol"
    codex["fallback"] = {"default": "gpt-5.5"}
    _configure_agent("codex")
    assert _codex_model(tmp_path) == "gpt-6.1-sol"
    # A Codex update with a new default moves the setup along.
    codex["codex"] = "gpt-7-sol"
    _refresh()
    assert _codex_model(tmp_path) == "gpt-7-sol"


def test_codex_uses_routers_fallback_when_its_default_is_not_served(tmp_path, codex):
    codex["codex"] = "gpt-9-sol"
    codex["fallback"] = {"codex": "gpt-7-sol", "default": "gpt-5.5"}
    _configure_agent("codex")
    assert _codex_model(tmp_path) == "gpt-7-sol"


def test_a_chosen_codex_model_stays_through_codex_updates(tmp_path, codex):
    codex["codex"] = "gpt-6.1-sol"
    _configure_agent("codex")
    _choose("codex", "gpt-5.5")
    codex["codex"] = "gpt-7-sol"
    _refresh()
    _configure_agent("codex")
    assert _codex_model(tmp_path) == "gpt-5.5"


def test_codex_admin_default_wins_over_codex_default(tmp_path, monkeypatch, world):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    world["codex"] = "gpt-6.1-sol"
    _mock_models(monkeypatch, MODELS, coding_agent_settings=_admin_default("gpt-5.5"))
    _configure_agent("codex")
    assert _codex_model(tmp_path) == "gpt-5.5"


def test_opencode_starts_on_routers_fallback_and_follows_it(tmp_path, opencode):
    opencode["fallback"] = {"default": "gpt-6.1-sol"}
    _configure_agent("opencode")
    assert _opencode_model(tmp_path) == "gpt-6.1-sol"
    # A new model released as Router's fallback reaches the setup on refresh.
    opencode["fallback"] = {"default": "gpt-7-sol"}
    _refresh()
    assert _opencode_model(tmp_path) == "gpt-7-sol"
    # A harness's own entry wins over `default`.
    opencode["fallback"] = {"default": "gpt-7-sol", "opencode": "gpt-5.5"}
    _refresh()
    assert _opencode_model(tmp_path) == "gpt-5.5"


def test_a_chosen_opencode_model_stays_when_the_fallback_changes(tmp_path, opencode):
    opencode["fallback"] = {"default": "gpt-6.1-sol"}
    _configure_agent("opencode")
    _choose("opencode", "gpt-5.5")
    opencode["fallback"] = {"default": "gpt-7-sol"}
    _refresh()
    _configure_agent("opencode")
    assert _opencode_model(tmp_path) == "gpt-5.5"


def test_without_routers_fallback_the_built_in_one_applies(tmp_path, opencode):
    _configure_agent("opencode")
    assert _opencode_model(tmp_path) == "gpt-6.1-sol"
    # An unlisted fallback is ignored rather than written.
    opencode["fallback"] = {"default": "gpt-404"}
    _refresh()
    assert _opencode_model(tmp_path) == "gpt-6.1-sol"


def test_older_setups_on_the_old_built_in_fallback_move_once(
    tmp_path, monkeypatch, world
):
    monkeypatch.setenv("OPENCODE_CONFIG", str(tmp_path / "opencode" / "opencode.json"))
    _mock_models(monkeypatch, [*MODELS, {"id": "gpt-5.6-sol"}])
    world["fallback"] = {"default": "gpt-5.6-sol"}
    _configure_agent("opencode")
    # What an older CLI left: gpt-5.6-sol saved, and no record of the pick.
    router_picks._path("opencode").unlink()
    world["fallback"] = {"default": "gpt-7-sol"}
    _refresh()
    assert _opencode_model(tmp_path) == "gpt-7-sol"


def test_unconfigure_clears_routers_pick(tmp_path, opencode):
    opencode["fallback"] = {"default": "gpt-6.1-sol"}
    _configure_agent("opencode")
    config = tmp_path / "opencode" / "opencode.json"
    assert router_picks.router_pick("opencode", config, "gpt-6.1-sol") == "gpt-6.1-sol"
    _unconfigure_agent("opencode")
    assert router_picks.router_pick("opencode", config, "gpt-6.1-sol") is None


def test_explicitly_choosing_routers_pick_makes_it_the_users(tmp_path, opencode):
    opencode["fallback"] = {"default": "gpt-6.1-sol"}
    _configure_agent("opencode")
    # Confirming the model Router picked makes it a choice, so a later fallback
    # change leaves it alone.
    _choose("opencode", "gpt-6.1-sol")
    opencode["fallback"] = {"default": "gpt-7-sol"}
    _refresh()
    _configure_agent("opencode")
    assert _opencode_model(tmp_path) == "gpt-6.1-sol"


def test_explicit_choice_is_saved_even_after_the_fallback_moved(tmp_path, opencode):
    opencode["fallback"] = {"default": "gpt-6.1-sol"}
    _configure_agent("opencode")
    opencode["fallback"] = {"default": "gpt-7-sol"}
    _choose("opencode", "gpt-6.1-sol")
    assert _opencode_model(tmp_path) == "gpt-6.1-sol"


def test_routers_picks_are_kept_per_config_home(tmp_path, monkeypatch, codex):
    home_a, home_b = tmp_path / "codex-a", tmp_path / "codex-b"
    codex["codex"] = "gpt-6.1-sol"
    monkeypatch.setenv("CODEX_HOME", str(home_a))
    _configure_agent("codex")
    _choose("codex", "gpt-7-sol")
    # Another home where Router picks the same model must not claim A's choice.
    codex["codex"] = "gpt-7-sol"
    monkeypatch.setenv("CODEX_HOME", str(home_b))
    _configure_agent("codex")
    codex["codex"] = "gpt-6.1-sol"
    monkeypatch.setenv("CODEX_HOME", str(home_a))
    _refresh()
    assert tomllib.loads((home_a / "config.toml").read_text())["model"] == "gpt-7-sol"
    monkeypatch.setenv("CODEX_HOME", str(home_b))
    _refresh()
    assert tomllib.loads((home_b / "config.toml").read_text())["model"] == "gpt-6.1-sol"


def test_fallback_model_reads_the_harness_entry_then_default(monkeypatch):
    payloads = {
        "new": {
            "harnesses": {},
            "fallback_models": {"default": " gpt-7-sol ", "codex": "gpt-6.1-sol"},
        },
        "empty": {"harnesses": {}, "fallback_models": {}},
        "older": {"harnesses": {}},
        "down": None,
    }
    monkeypatch.setattr(
        admin_defaults, "_settings_payload", lambda key, *_a: payloads[key]
    )
    assert _fallback_model("codex", "new", "https://router.example") == "gpt-6.1-sol"
    assert _fallback_model("hermes", "new", "https://router.example") == "gpt-7-sol"
    for key in ("empty", "older", "down"):
        assert _fallback_model("codex", key, "https://router.example") is None


def test_cursor_suggests_routers_fallback(world):
    models = [
        router.RouterModel(
            id=name,
            metadata=router.RouterModelMetadata(
                request_name=name,
                provider_model=name,
                display_name=name,
                description="",
                listing_order=0,
                efforts=(),
                default_effort="",
                input_modalities=("text",),
                context_window=128000,
                max_output_tokens=16384,
                tool_calls=True,
            ),
        )
        for name in ("claude-opus-5-5", "gpt-6.1-sol", "gpt-7-sol")
    ]
    assert router._cursor_suggested_model(models, "gpt-7-sol") == "gpt-7-sol"
    assert router._cursor_suggested_model(models, None) == "gpt-6.1-sol"
    # Cursor's override never serves claude- ids.
    assert router._cursor_suggested_model(models, "claude-opus-5-5") == "gpt-6.1-sol"


def _pick_in_opencode(tmp_path, model, provider="ramp-router"):
    """What OpenCode's model dialog does: move the pick to the front of recent."""
    state = tmp_path / "home" / ".local" / "state" / "opencode" / "model.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    recent = []
    if state.exists():
        recent = json.loads(state.read_text())["recent"]
    entry = {"providerID": provider, "modelID": model}
    recent = [entry, *(item for item in recent if item != entry)]
    state.write_text(json.dumps({"recent": recent, "favorite": [], "variant": {}}))


def test_a_model_picked_in_opencode_becomes_the_users_default(tmp_path, opencode):
    opencode["fallback"] = {"default": "gpt-6.1-sol"}
    _configure_agent("opencode")
    _pick_in_opencode(tmp_path, "gpt-5.5")
    _refresh()
    assert _opencode_model(tmp_path) == "gpt-5.5"
    # It is the user's now: a new fallback leaves it alone.
    opencode["fallback"] = {"default": "gpt-7-sol"}
    _refresh()
    _configure_agent("opencode")
    assert _opencode_model(tmp_path) == "gpt-5.5"


def test_a_later_router_choice_wins_over_an_earlier_opencode_pick(tmp_path, opencode):
    opencode["fallback"] = {"default": "gpt-6.1-sol"}
    _configure_agent("opencode")
    _pick_in_opencode(tmp_path, "gpt-5.5")
    _choose("opencode", "gpt-7-sol")
    _refresh()
    assert _opencode_model(tmp_path) == "gpt-7-sol"


def test_opencode_picks_from_before_router_looked_are_not_replayed(tmp_path, opencode):
    opencode["fallback"] = {"default": "gpt-6.1-sol"}
    _pick_in_opencode(tmp_path, "gpt-5.5")
    _configure_agent("opencode")
    _refresh()
    assert _opencode_model(tmp_path) == "gpt-6.1-sol"


def test_a_pick_of_another_provider_in_opencode_is_ignored(tmp_path, opencode):
    opencode["fallback"] = {"default": "gpt-6.1-sol"}
    _configure_agent("opencode")
    _pick_in_opencode(tmp_path, "gpt-5.5", provider="openai")
    _refresh()
    assert _opencode_model(tmp_path) == "gpt-6.1-sol"


def test_an_opencode_pick_this_router_does_not_serve_keeps_the_saved_choice(
    tmp_path, opencode
):
    opencode["fallback"] = {"default": "gpt-6.1-sol"}
    _configure_agent("opencode")
    _choose("opencode", "gpt-5.5")
    # Picked against another Router setup: OpenCode keeps one recent list.
    _pick_in_opencode(tmp_path, "gpt-404")
    _refresh()
    _configure_agent("opencode")
    assert _opencode_model(tmp_path) == "gpt-5.5"
