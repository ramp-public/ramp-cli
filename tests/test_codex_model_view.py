"""Compact Codex picker projection and persisted Router preferences."""

from __future__ import annotations

import copy
import json
import tomllib

import click
import httpx
import pytest
from click.testing import CliRunner

from ramp_cli.commands import router
from ramp_cli.main import cli


def _recommended(request_name="recommended", display_name="Recommended"):
    return router.RouterModel(
        id="claude-router-opaque-compatibility-alias",
        metadata=router.RouterModelMetadata(
            request_name=request_name,
            provider_model="provider-model",
            display_name=display_name,
            description="Recommended model",
            listing_order=1,
            efforts=(("medium", "Balanced"),),
            default_effort="medium",
            input_modalities=("text", "image"),
            context_window=200000,
            max_output_tokens=32000,
            tool_calls=True,
        ),
    )


def _row(slug, visibility="list", **extra):
    return {
        "slug": slug,
        "display_name": slug,
        "visibility": visibility,
        "base_instructions": "Keep these instructions",
        "context_window": 200000,
        "supported_reasoning_levels": [{"effort": "medium", "description": "Balanced"}],
        **extra,
    }


def _visible(catalog):
    return [
        row["slug"]
        for row in catalog["models"]
        if row.get("visibility", "list") == "list"
    ]


def test_compact_preserves_metadata_and_does_not_mutate_input():
    catalog = {
        "version": "server-value",
        "models": [_row("other"), _row("recommended"), _row("legacy", "hide")],
    }
    original = copy.deepcopy(catalog)
    result = router._compact_codex_catalog(catalog, [_recommended()], "recommended")
    assert catalog == original
    assert result["version"] == "server-value"
    assert _visible(result) == ["recommended"]
    assert result["models"][0] == original["models"][1]
    by_slug = {row["slug"]: row for row in result["models"]}
    assert by_slug["other"] == {**original["models"][0], "visibility": "hide"}
    assert by_slug["legacy"] == original["models"][2]


@pytest.mark.parametrize("slug", ["recommended", "Recommended"])
def test_compact_accepts_canonical_or_promoted_slug(slug):
    result = router._compact_codex_catalog(
        {"models": [_row(slug), _row("other")]}, [_recommended()], slug
    )
    assert _visible(result) == [slug]


def test_shared_display_label_does_not_recommend_an_unrelated_route():
    result = router._compact_codex_catalog(
        {
            "models": [
                _row("recommended", display_name="Recommended"),
                _row("unrelated-route", display_name="Recommended"),
            ]
        },
        [_recommended()],
        "recommended",
    )
    assert _visible(result) == ["recommended"]


def test_compact_keeps_visible_current_selection_outside_shortlist():
    result = router._compact_codex_catalog(
        {"models": [_row("current"), _row("recommended"), _row("other")]},
        [_recommended()],
        "current",
    )
    assert _visible(result) == ["recommended", "current"]


def test_compact_does_not_unhide_selected_canonical_alias():
    result = router._compact_codex_catalog(
        {"models": [_row("Recommended"), _row("recommended", "hide"), _row("other")]},
        [_recommended()],
        "recommended",
    )
    assert _visible(result) == ["Recommended"]
    selected = next(row for row in result["models"] if row["slug"] == "recommended")
    assert selected["visibility"] == "hide"


def test_compact_retains_selected_hidden_metadata_after_desktop_cap():
    catalog = {
        "models": [_row(f"other-{n}") for n in range(110)]
        + [_row("recommended"), _row("saved-hidden", "hide", context_window=999999)]
    }
    compact = router._compact_codex_catalog(catalog, [_recommended()], "saved-hidden")
    bounded = json.loads(router._render_codex_catalog(compact, "saved-hidden", []))
    assert len(bounded["models"]) == router.CODEX_CATALOG_MODEL_LIMIT
    assert _visible(bounded) == ["recommended"]
    assert bounded["models"][-1]["slug"] == "saved-hidden"
    assert bounded["models"][-1]["context_window"] == 999999
    assert bounded["models"][-1]["visibility"] == "hide"


@pytest.mark.parametrize("models", [[_row("unrelated")], [_row("recommended", "hide")]])
def test_no_visible_recommendation_fails_without_mutating_catalog(models):
    catalog = {"models": models}
    original = copy.deepcopy(catalog)
    with pytest.raises(click.ClickException, match="did not match"):
        router._compact_codex_catalog(catalog, [_recommended()], models[0]["slug"])
    assert catalog == original


def test_absent_preference_defaults_to_compact(tmp_path):
    assert router._configured_codex_model_view(tmp_path / "config.toml") == "compact"


@pytest.mark.parametrize(
    "value,expected",
    [
        ({}, "compact"),
        ({"codex_model_view": "compact"}, "compact"),
        ({"codex_model_view": "all"}, "all"),
        ({"codex_model_view": "unknown"}, "compact"),
    ],
)
def test_saved_preference_is_read(tmp_path, value, expected):
    (tmp_path / "ramp-router-state.json").write_text(json.dumps(value))
    assert router._configured_codex_model_view(tmp_path / "config.toml") == expected


@pytest.mark.parametrize("state", ["not json", "[]", "null", '"compact"'])
def test_malformed_receipt_is_not_silently_replaced(tmp_path, state):
    (tmp_path / "ramp-router-state.json").write_text(state)
    with pytest.raises(
        click.ClickException, match="Could not read Ramp Router setup state"
    ):
        router._configured_codex_model_view(tmp_path / "config.toml")


@pytest.fixture
def model_server(monkeypatch):
    """Serve distinct Codex, compact, and full model views without network access."""
    requests = []
    catalog = {"models": [_row("recommended"), _row("other"), _row("legacy", "hide")]}
    state = {"fail_compact": False}

    def get(url, *, headers, timeout):
        request = httpx.Request("GET", url)
        if not url.endswith("/models"):
            return httpx.Response(404, request=request)
        assert headers["Authorization"] == "Bearer test-key"
        client = headers.get("X-Gateway-Client")
        view = headers.get("X-Gateway-Model-View")
        requests.append(client or view)
        if client == "codex":
            return httpx.Response(200, json=copy.deepcopy(catalog), request=request)
        if view == "compact" or (client == "claude-code" and view != "all"):
            if state["fail_compact"]:
                return httpx.Response(503, request=request)
            names = ["recommended"]
        else:
            names = ["recommended", "other"]
        return httpx.Response(
            200,
            request=request,
            json={
                "data": [
                    {
                        "id": f"claude-router-{name}"
                        if client == "claude-code"
                        else name,
                        "router": {
                            "schema_version": 1,
                            "request_name": name,
                            "display_name": name,
                            "provider_model": state.get(
                                "provider_model", f"gpt-{name}"
                            ),
                            "pricing": state.get("pricing"),
                            "listing": {"order": 0},
                            "limits": {
                                "context_window": state.get("context_window", 200000),
                                "max_output_tokens": 32000,
                            },
                            "capabilities": {
                                "modalities": {"input": ["text"]},
                                "reasoning": {"efforts": [], "default_effort": ""},
                            },
                        },
                    }
                    for name in names
                ]
            },
        )

    monkeypatch.setattr(router.httpx, "get", get)
    monkeypatch.setattr(router.shutil, "which", lambda _name: None)
    monkeypatch.setattr(router, "_codex_harness_prompt", lambda _model, **_kwargs: None)
    monkeypatch.setattr(router, "_install_codex_cost_hook", lambda *_a, **_k: None)
    monkeypatch.setattr(router, "session_sync_hook_command", lambda *_a, **_k: None)
    monkeypatch.setattr(router.claude_cowork, "is_available", lambda: False)
    return requests, catalog, state


def _configure(runner, view=None):
    arguments = [
        "--agent",
        "router",
        "configure",
        "codex",
        "--no-browser",
        "--api-key",
        "test-key",
    ]
    if view is not None:
        arguments.extend(["--codex-models", view])
    result = runner.invoke(cli, arguments)
    assert result.exit_code == 0, result.output
    return result


def test_configure_compact_refresh_then_all_restores_server_visibility(
    tmp_path, model_server
):
    requests, server_catalog, _ = model_server
    runner = CliRunner()
    home = tmp_path / "codex"
    catalog_path = home / router.CODEX_ROUTER_CATALOG
    _configure(runner, "all")
    assert json.loads(catalog_path.read_text()) == server_catalog
    assert "compact" not in requests

    configured = _configure(runner, "compact")
    data = json.loads(configured.output)["data"][0]
    assert data["model_view"] == "compact"
    assert data["models_visible"] == 1
    compact = json.loads(catalog_path.read_text())
    assert _visible(compact) == ["recommended"]
    assert router._configured_codex_model_view(home / "config.toml") == "compact"
    assert requests.count("compact") == 1
    config = tomllib.loads((home / "config.toml").read_text())
    assert config["model_catalog_json"] == str(catalog_path)

    refreshed = runner.invoke(cli, ["--agent", "router", "refresh"])
    assert refreshed.exit_code == 0, refreshed.output
    assert json.loads(catalog_path.read_text()) == compact
    assert requests.count("compact") == 2

    _configure(runner, "all")
    assert json.loads(catalog_path.read_text()) == server_catalog
    assert router._configured_codex_model_view(home / "config.toml") == "all"
    assert requests.count("compact") == 2
    refreshed = runner.invoke(cli, ["--agent", "router", "refresh"])
    assert refreshed.exit_code == 0, refreshed.output
    assert json.loads(catalog_path.read_text()) == server_catalog
    assert requests.count("compact") == 2


@pytest.mark.parametrize("view", ["compact", "all"])
@pytest.mark.parametrize(
    "provider_model,window,default,ceiling,refreshed_default",
    [
        ("gpt-6.1-sol", 1_050_000, 160_000, 968_000, 120_000),
        ("grok-4.7", 500_000, 400_000, 500_000, 400_000),
        ("claude-haiku-5-5", 1_000_000, 800_000, 1_000_000, 800_000),
    ],
)
def test_configure_and_refresh_cap_context_without_extra_discovery(
    tmp_path,
    model_server,
    view,
    provider_model,
    window,
    default,
    ceiling,
    refreshed_default,
):
    requests, catalog, state = model_server
    state.update(provider_model=provider_model, context_window=window)
    state["pricing"] = {
        "long_context_token_threshold": 160_000,
        "long_context": {"input": "4", "output": "15"},
    }
    for row in catalog["models"]:
        row.update(context_window=window, max_context_window=window)
    original = copy.deepcopy(catalog)
    runner = CliRunner()
    _configure(runner, view)
    path = tmp_path / "codex" / router.CODEX_ROUTER_CATALOG
    configured = json.loads(path.read_text())
    assert {row["context_window"] for row in configured["models"]} == {default, window}
    for row in configured["models"]:
        # The generic list does not describe legacy; do not guess its policy.
        expected = window if row["slug"] == "legacy" else default
        assert row["context_window"] == expected
        assert row["max_context_window"] == (
            window if row["slug"] == "legacy" else ceiling
        )
    assert requests.count(None) == requests.count("codex") == 1
    assert requests.count("compact") == (1 if view == "compact" else 0)

    state["pricing"]["long_context_token_threshold"] = 120_000
    result = runner.invoke(cli, ["--agent", "router", "refresh"])
    assert result.exit_code == 0, result.output
    refreshed = json.loads(path.read_text())
    for row in refreshed["models"]:
        expected = window if row["slug"] == "legacy" else refreshed_default
        assert row["context_window"] == expected
        assert row["max_context_window"] == (
            window if row["slug"] == "legacy" else ceiling
        )
    assert requests.count(None) == requests.count("codex") == 2
    assert requests.count("compact") == (2 if view == "compact" else 0)
    assert catalog == original


@pytest.mark.parametrize("view", ["compact", "all"])
def test_configure_and_refresh_use_the_installed_bundled_ceiling(
    tmp_path, model_server, monkeypatch, view
):
    _, catalog, state = model_server
    state.update(provider_model="gpt-future-model", context_window=1_050_000)
    state["pricing"] = {
        "long_context_token_threshold": 272_000,
        "long_context": {"input": "4", "output": "15"},
    }
    for row in catalog["models"]:
        row.update(context_window=1_050_000, max_context_window=1_050_000)
    monkeypatch.setattr(
        router.shutil,
        "which",
        lambda name: "/installed/codex" if name == "codex" else None,
    )
    calls = []

    def catalog_from(executable, *, bundled=False):
        calls.append((executable, bundled))
        return {
            "version": "native",
            "models": [{"slug": "gpt-future-model", "max_context_window": 765_432}],
        }

    monkeypatch.setattr(router, "_codex_catalog_from", catalog_from)
    runner = CliRunner()
    _configure(runner, view)
    path = tmp_path / "codex" / router.CODEX_ROUTER_CATALOG
    for row in json.loads(path.read_text())["models"]:
        if row["slug"] != "legacy":
            assert row["context_window"] == 272_000
            assert row["max_context_window"] == 765_432
    result = runner.invoke(cli, ["--agent", "router", "refresh"])
    assert result.exit_code == 0, result.output
    assert calls == [("/installed/codex", True)]
    assert json.loads(path.read_text())["models"][0]["max_context_window"] == 765_432


def test_failed_compact_discovery_preserves_existing_configuration(
    tmp_path, model_server
):
    _, _, state = model_server
    runner = CliRunner()
    _configure(runner)
    home = tmp_path / "codex"
    paths = [
        home / "config.toml",
        home / router.CODEX_ROUTER_CATALOG,
        home / "ramp-router-state.json",
    ]
    before = {path: path.read_bytes() for path in paths}
    state["fail_compact"] = True
    result = runner.invoke(
        cli,
        [
            "--agent",
            "router",
            "configure",
            "codex",
            "--no-browser",
            "--api-key",
            "test-key",
            "--codex-models",
            "compact",
        ],
    )
    assert result.exit_code != 0
    assert {path: path.read_bytes() for path in paths} == before


@pytest.mark.parametrize("view", [None, "compact", "all"])
def test_background_refresh_preserves_the_saved_codex_view(
    tmp_path, model_server, view
):
    _, server_catalog, _ = model_server
    _configure(CliRunner(), view)
    home = tmp_path / "codex"
    catalog_path = home / router.CODEX_ROUTER_CATALOG
    state_path = home / "ramp-router-state.json"
    # Change catalog metadata so both the catalog and its receipt digest move.
    server_catalog["models"][0]["description"] = "Updated recommendation"
    models = router._fetch_models("test-key")

    router._refresh_codex_models(
        home / "config.toml", "test-key", models, base_url=None, selected_model=None
    )

    catalog = json.loads(catalog_path.read_text())
    assert _visible(catalog) == (
        ["recommended", "other"] if view == "all" else ["recommended"]
    )
    assert catalog["models"][0]["description"] == "Updated recommendation"
    receipt = json.loads(state_path.read_text())
    assert receipt[router.CODEX_MODEL_VIEW_KEY] == (view or "compact")
    assert receipt[router.CODEX_ROUTER_CATALOG_DIGEST_KEY] == router._content_digest(
        catalog_path.read_text()
    )


def test_background_compact_discovery_failure_preserves_all_files(
    tmp_path, model_server
):
    _, _, server_state = model_server
    _configure(CliRunner(), "compact")
    home = tmp_path / "codex"
    paths = [
        home / name
        for name in (
            "config.toml",
            router.CODEX_ROUTER_CATALOG,
            "ramp-router-state.json",
        )
    ]
    before = {path: path.read_bytes() for path in paths}
    models = router._fetch_models("test-key")
    server_state["fail_compact"] = True

    with pytest.raises(click.ClickException):
        router._refresh_codex_models(
            home / "config.toml", "test-key", models, base_url=None, selected_model=None
        )

    assert {path: path.read_bytes() for path in paths} == before


@pytest.mark.parametrize("clients", [[], ["claude-code"], ["codex", "claude-code"]])
def test_codex_models_flag_requires_codex_only(clients):
    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "router",
            "configure",
            *clients,
            "--codex-models",
            "compact",
            "--no-browser",
        ],
    )
    assert result.exit_code == 2
    assert "requires exactly one agent" in result.output


def test_changing_only_view_preserves_current_model(tmp_path, model_server):
    runner = CliRunner()
    _configure(runner)
    home = tmp_path / "codex"
    config_path = home / "config.toml"
    config_path.write_text(
        config_path.read_text().replace('model = "recommended"', 'model = "other"')
    )
    for view in ("compact", "all", "compact"):
        _configure(runner, view)
        config = tomllib.loads(config_path.read_text())
        assert config["model"] == "other"
        catalog = json.loads((home / router.CODEX_ROUTER_CATALOG).read_text())
        assert "other" in _visible(catalog)


def test_changing_selected_model_preserves_compact_preference(tmp_path, model_server):
    runner = CliRunner()
    _configure(runner, "compact")
    result = runner.invoke(
        cli, ["--agent", "router", "configure", "model", "codex", "--model", "other"]
    )
    assert result.exit_code == 0, result.output
    home = tmp_path / "codex"
    assert router._configured_codex_model_view(home / "config.toml") == "compact"
    assert tomllib.loads((home / "config.toml").read_text())["model"] == "other"
    assert _visible(json.loads((home / router.CODEX_ROUTER_CATALOG).read_text())) == [
        "recommended",
        "other",
    ]


def test_refresh_honors_view_changed_during_discovery(
    tmp_path, model_server, monkeypatch
):
    runner = CliRunner()
    _configure(runner, "compact")
    home = tmp_path / "codex"
    state_path = home / "ramp-router-state.json"
    fetch = router._fetch_models

    def change_view(*args, **kwargs):
        result = fetch(*args, **kwargs)
        if kwargs.get("model_view") == "compact":
            state = json.loads(state_path.read_text())
            state[router.CODEX_MODEL_VIEW_KEY] = "all"
            state_path.write_text(json.dumps(state))
        return result

    monkeypatch.setattr(router, "_fetch_models", change_view)
    result = runner.invoke(cli, ["--agent", "router", "refresh"])
    assert result.exit_code == 0, result.output
    assert router._configured_codex_model_view(home / "config.toml") == "all"
    assert _visible(json.loads((home / router.CODEX_ROUTER_CATALOG).read_text())) == [
        "recommended",
        "other",
    ]


def test_display_name_colliding_with_another_canonical_slug_does_not_include_it():
    result = router._compact_codex_catalog(
        {"models": [_row("recommended", display_name="Other"), _row("Other")]},
        [_recommended(display_name="Other")],
        "recommended",
    )
    assert _visible(result) == ["recommended"]


def test_new_connection_uses_compact_without_view_flag(tmp_path, model_server):
    requests, _, _ = model_server
    runner = CliRunner()
    result = _configure(runner)
    data = json.loads(result.output)["data"][0]
    assert data["model_view"] == "compact"
    assert data["models_visible"] == 1
    home = tmp_path / "codex"
    assert router._configured_codex_model_view(home / "config.toml") == "compact"
    assert _visible(json.loads((home / router.CODEX_ROUTER_CATALOG).read_text())) == [
        "recommended"
    ]
    assert requests.count("compact") == 1


def test_repeat_configure_without_view_flag_preserves_explicit_all(
    tmp_path, model_server
):
    requests, catalog, _ = model_server
    runner = CliRunner()
    _configure(runner, "all")
    result = _configure(runner)
    assert json.loads(result.output)["data"][0]["model_view"] == "all"
    assert "compact" not in requests
    home = tmp_path / "codex"
    assert json.loads((home / router.CODEX_ROUTER_CATALOG).read_text()) == catalog


@pytest.mark.parametrize("command", ["configure", "refresh"])
def test_legacy_connection_adopts_compact_without_view_flag(
    tmp_path, model_server, command
):
    runner = CliRunner()
    _configure(runner, "all")
    home = tmp_path / "codex"
    state_path = home / "ramp-router-state.json"
    state = json.loads(state_path.read_text())
    del state[router.CODEX_MODEL_VIEW_KEY]
    state_path.write_text(json.dumps(state))
    if command == "configure":
        _configure(runner)
    else:
        result = runner.invoke(cli, ["--agent", "router", "refresh"])
        assert result.exit_code == 0, result.output
    assert router._configured_codex_model_view(home / "config.toml") == "compact"
    assert _visible(json.loads((home / router.CODEX_ROUTER_CATALOG).read_text())) == [
        "recommended"
    ]


@pytest.mark.parametrize(
    "view,expected", [("all", ["recommended", "other"]), ("compact", ["recommended"])]
)
def test_interactive_codex_picker_applies_the_answer(
    tmp_path, monkeypatch, model_server, view, expected
):
    monkeypatch.setattr(router, "_can_prompt", lambda *_a: True)
    monkeypatch.setattr(router, "_pick_installed_clients", lambda: ("codex",))
    seen = []

    def pick(current, clients):
        seen.append((current, clients))
        return view

    monkeypatch.setattr(router, "_pick_claude_models", pick)
    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "--no-browser", "--api-key", "test-key"]
    )
    assert result.exit_code == 0, result.output
    assert seen == [("compact", ("codex",))]
    home = tmp_path / "codex"
    assert router._configured_codex_model_view(home / "config.toml") == view
    assert (
        _visible(json.loads((home / router.CODEX_ROUTER_CATALOG).read_text()))
        == expected
    )


def test_interactive_codex_picker_starts_from_its_saved_view(
    tmp_path, monkeypatch, model_server
):
    runner = CliRunner()
    _configure(runner, "all")
    monkeypatch.setattr(router, "_can_prompt", lambda *_a: True)
    monkeypatch.setattr(router, "_pick_installed_clients", lambda: ("codex",))
    seen = []

    def pick(current, clients):
        seen.append(current)
        return current

    monkeypatch.setattr(router, "_pick_claude_models", pick)
    result = runner.invoke(
        cli, ["--human", "router", "configure", "--no-browser", "--api-key", "test-key"]
    )
    assert result.exit_code == 0, result.output
    assert seen == ["all"]
    assert router._configured_codex_model_view(tmp_path / "codex/config.toml") == "all"


@pytest.mark.parametrize("clients", [("codex", "opencode"), ("opencode", "codex")])
def test_multi_client_summary_reports_each_clients_count(
    monkeypatch, model_server, clients
):
    monkeypatch.setattr(router, "_configure_plugin_client", lambda *_a, **_k: None)
    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "--no-input",
            "router",
            "configure",
            *clients,
            "--no-browser",
            "--api-key",
            "test-key",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Codex: 1 model added." in result.output
    assert "OpenCode: 2 models added." in result.output
    assert result.output.count("Restart any running agents, then pick a model.") == 1


@pytest.mark.parametrize("view", ["all", "compact"])
def test_interactive_shared_model_view_applies_to_both_clients(
    tmp_path, monkeypatch, model_server, view
):
    monkeypatch.setattr(router, "_can_prompt", lambda *_a: True)
    monkeypatch.setattr(
        router, "_pick_installed_clients", lambda: ("claude-code", "codex")
    )
    monkeypatch.setattr(router, "_pick_claude_models", lambda *_a: view)
    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "--no-browser", "--api-key", "test-key"]
    )
    assert result.exit_code == 0, result.output
    assert router._configured_codex_model_view(tmp_path / "codex/config.toml") == view
    assert router._configured_claude_model_view_all(
        tmp_path / "claude/settings.json"
    ) == (view == "all")


def test_unattended_mixed_configure_preserves_independent_views(
    tmp_path, monkeypatch, model_server
):
    runner = CliRunner()
    _configure(runner, "all")
    monkeypatch.setattr(
        router,
        "_pick_claude_models",
        lambda *_a: pytest.fail("unattended setup must not prompt"),
    )
    result = runner.invoke(
        cli,
        [
            "--agent",
            "--no-input",
            "router",
            "configure",
            "claude-code",
            "codex",
            "--no-browser",
            "--api-key",
            "test-key",
        ],
    )
    assert result.exit_code == 0, result.output
    assert router._configured_codex_model_view(tmp_path / "codex/config.toml") == "all"
    assert not router._configured_claude_model_view_all(
        tmp_path / "claude/settings.json"
    )


@pytest.mark.parametrize("output_flag", ["--agent", "--human"])
def test_relative_codex_home_reports_visible_count(
    tmp_path, monkeypatch, model_server, output_flag
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_HOME", "relative-codex")
    result = CliRunner().invoke(
        cli,
        [
            output_flag,
            "--no-input",
            "router",
            "configure",
            "codex",
            "--no-browser",
            "--api-key",
            "test-key",
        ],
    )
    assert result.exit_code == 0, result.output
    if output_flag == "--agent":
        assert json.loads(result.output)["data"][0]["models_visible"] == 1
    else:
        assert "1 model added." in result.output
        assert "2 models added." not in result.output
    config = tomllib.loads((tmp_path / "relative-codex/config.toml").read_text())
    assert config["model_catalog_json"] == str(
        tmp_path / "relative-codex" / router.CODEX_ROUTER_CATALOG
    )


@pytest.mark.parametrize("client", ["opencode", "pi"])
def test_plugin_model_view_defaults_to_all_and_persists(model_server, client):
    runner = CliRunner()
    path = router._json_config_path(client)
    written = path.parent / (
        "opencode.json" if client == "opencode" else router.PI_PLUGIN_CONFIG_FILE
    )
    configure = [
        "--agent",
        "router",
        "configure",
        client,
        "--no-browser",
        "--api-key",
        "test-key",
    ]
    refresh = ["--agent", "router", "refresh"]
    for arguments, view in (
        (configure, "all"),
        (refresh, "all"),
        ([*configure, f"--{client}-models", "compact"], "compact"),
        (configure, "compact"),
    ):
        result = runner.invoke(cli, arguments)
        assert result.exit_code == 0, result.output
        assert router._configured_plugin_model_view(path) == view
        assert f'"modelView": "{view}"' in written.read_text()


def test_interactive_picker_applies_the_answer_to_plugins(monkeypatch, model_server):
    monkeypatch.setattr(router, "_can_prompt", lambda *_a: True)
    monkeypatch.setattr(router, "_pick_installed_clients", lambda: ("opencode", "pi"))
    seen = []

    def pick(current, clients):
        seen.append((current, clients))
        return "compact"

    monkeypatch.setattr(router, "_pick_claude_models", pick)
    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "--no-browser", "--api-key", "test-key"]
    )
    assert result.exit_code == 0, result.output
    assert seen == [("all", ("opencode", "pi"))]
    for client in ("opencode", "pi"):
        path = router._json_config_path(client)
        assert router._configured_plugin_model_view(path) == "compact"
