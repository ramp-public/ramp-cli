"""Codex uses GPT pricing bands and 80% context defaults for other families."""

from __future__ import annotations

import copy
import json
import subprocess
from dataclasses import replace

import pytest

from ramp_cli.commands import router


def _metadata(**pricing):
    return {
        "schema_version": 1,
        "request_name": "test-model",
        "provider_model": "gpt-future-model",
        "display_name": "Test Model",
        "limits": {"context_window": 1_000_000, "max_output_tokens": 128_000},
        "pricing": {
            "input": "2",
            "output": "10",
            "long_context_token_threshold": 272_000,
            "long_context": {"input": "4", "output": "15"},
            **pricing,
        },
    }


def _model(raw=None, **metadata):
    return router.RouterModel(
        id="test-model",
        metadata=replace(
            router._model_metadata(raw or _metadata(), "test-model"), **metadata
        ),
    )


def _row(slug="test-model", window=1_000_000, **extra):
    return {
        "slug": slug,
        "context_window": window,
        "max_context_window": window,
        "base_instructions": "Keep the prompt",
        **extra,
    }


@pytest.mark.parametrize(
    "pricing,expected",
    [
        ({}, 272_000),
        ({"long_context": None}, None),
        ({"long_context_token_threshold": None}, None),
        ({"long_context": {"input": "4"}}, None),
        ({"long_context": {"input": "bad", "output": "15"}}, None),
        ({"long_context": {"input": "9/2", "output": "31/2"}}, 272_000),
        ({"long_context": {"input": "NaN", "output": "15"}}, None),
        ({"long_context": {"input": "4", "output": "Infinity"}}, None),
        ({"long_context": {"input": "1/0", "output": "15"}}, None),
        ({"long_context": {"input": 4, "output": "15"}}, None),
        ({"long_context": {"input": "1e999", "output": "15"}}, None),
        *[
            ({"long_context_token_threshold": threshold}, None)
            for threshold in (
                0,
                -1,
                1.5,
                "272000",
                2**53,
                float("nan"),
                float("inf"),
                True,
                False,
            )
        ],
    ],
)
def test_threshold_matches_pi_and_opencode_contract(pricing, expected):
    raw = _metadata(**pricing)
    original = copy.deepcopy(raw)
    metadata = router._model_metadata(raw, "test-model")
    assert metadata.long_context_token_threshold == expected
    assert metadata.context_window == 1_000_000
    assert metadata.max_output_tokens == 128_000
    # NaN is not equal to itself; the same untouched object is retained.
    assert raw["limits"] == original["limits"]
    assert raw["pricing"] == original["pricing"]


@pytest.mark.parametrize("pricing", [None, {}, [], "invalid"])
def test_unpriced_models_keep_their_window(pricing):
    raw = {**_metadata(), "pricing": pricing}
    catalog = {"models": [_row()]}
    assert (
        json.loads(router._render_codex_catalog(catalog, "test-model", [_model(raw)]))
        == catalog
    )


@pytest.mark.parametrize("window", [128_000, 272_000, 272_001, 1_000_000])
def test_caps_default_window_at_the_inclusive_threshold(window):
    catalog = {"version": "preserve", "models": [_row(window=window)]}
    original = copy.deepcopy(catalog)
    rendered = json.loads(
        router._render_codex_catalog(catalog, "test-model", [_model()])
    )
    assert rendered == {
        **catalog,
        "models": [
            _row(
                window=min(window, 272_000),
                max_context_window=min(window, max(272_000, window - 128_000 - 50_000)),
            )
        ],
    }
    assert catalog == original


def test_caps_promoted_and_hidden_slugs_and_the_retained_selection():
    catalog = {
        "models": [_row("Test Model")]
        + [_row(f"other-{n}") for n in range(router.CODEX_CATALOG_MODEL_LIMIT)]
        + [_row(visibility="hide")]
    }
    original = copy.deepcopy(catalog)
    rendered = json.loads(
        router._render_codex_catalog(catalog, "test-model", [_model()])
    )
    assert len(rendered["models"]) == router.CODEX_CATALOG_MODEL_LIMIT
    assert rendered["models"][0] == _row(
        "Test Model", 272_000, max_context_window=822_000
    )
    assert rendered["models"][-1] == _row(
        window=272_000, max_context_window=822_000, visibility="hide"
    )
    assert rendered["models"][1] == original["models"][1]
    assert catalog == original


def test_canonical_names_win_over_another_models_display_name():
    other = router.RouterModel(
        id="other",
        metadata=replace(
            _model().metadata, request_name="other", display_name="test-model"
        ),
    )
    unpriced = _model(long_context_token_threshold=None)
    catalog = {"models": [_row(), _row("other")]}
    rendered = json.loads(
        router._render_codex_catalog(catalog, "test-model", [unpriced, other])
    )
    assert rendered["models"] == [
        _row(),
        _row("other", 272_000, max_context_window=822_000),
    ]


def test_ambiguous_display_names_do_not_cap_unrelated_rows():
    other = router.RouterModel(
        id="other",
        metadata=replace(_model().metadata, request_name="other"),
    )
    catalog = {"models": [_row("Test Model")]}
    assert (
        json.loads(
            router._render_codex_catalog(catalog, "Test Model", [_model(), other])
        )
        == catalog
    )


def test_missing_ceiling_uses_metadata_and_smaller_ceilings_are_not_enlarged():
    catalog = {
        "models": [
            {"slug": "test-model"},
            _row("Test Model", max_context_window=128_000),
        ]
    }
    rendered = json.loads(
        router._render_codex_catalog(catalog, "test-model", [_model()])
    )
    assert rendered["models"] == [
        {"slug": "test-model", "max_context_window": 822_000},
        _row("Test Model", 128_000, max_context_window=128_000),
    ]


@pytest.mark.parametrize(
    "context,output,threshold,ceiling",
    [
        (1_050_000, 128_000, 272_000, 872_000),
        (1_050_000, 64_000, 272_000, 936_000),
        (400_000, 128_000, 272_000, 272_000),
        (1_000_000, 128_000, None, 1_000_000),
        (128_000, 64_000, 272_000, 128_000),
    ],
)
def test_gpt_fallback_ceiling_uses_router_limits_not_a_model_allowlist(
    context, output, threshold, ceiling
):
    model = _model(
        provider_model="gpt-future-provider-model",
        context_window=context,
        max_output_tokens=output,
        long_context_token_threshold=threshold,
    )
    catalog = {"models": [_row(window=context)]}
    rendered = json.loads(router._render_codex_catalog(catalog, "test-model", [model]))
    assert rendered["models"] == [
        _row(window=min(context, threshold or context), max_context_window=ceiling)
    ]


@pytest.mark.parametrize("lookup", ["provider_model", "request_name"])
def test_bundled_ceiling_wins_over_the_guess_for_a_new_model(lookup):
    model = _model(**{lookup: "gpt-brand-new-model"})
    catalog = {"models": [_row()]}
    rendered = json.loads(
        router._render_codex_catalog(
            catalog,
            "test-model",
            [model],
            native_windows={"gpt-brand-new-model": 765_432},
        )
    )
    assert rendered["models"] == [_row(window=272_000, max_context_window=765_432)]


def test_bundle_is_offline_validated_and_shared_with_prompt_setup(monkeypatch):
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        assert command == [
            "/installed/codex",
            "-c",
            'model_provider="openai"',
            "debug",
            "models",
            "--bundled",
        ]
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "models": [
                        {
                            "slug": "future",
                            "max_context_window": 765_432,
                            "base_instructions": "native prompt",
                        },
                        *[
                            {"slug": f"bad-{n}", "max_context_window": value}
                            for n, value in enumerate(
                                [None, 0, -1, 1.5, True, "872000"]
                            )
                        ],
                        None,
                    ]
                }
            ),
            "",
        )

    monkeypatch.setattr(router.subprocess, "run", run)
    assert router._codex_bundled_context_windows("/installed/codex") == {
        "future": 765_432
    }
    assert (
        router._codex_harness_prompt_from("/installed/codex", "future", bundled=True)
        == "native prompt"
    )
    monkeypatch.setattr(router.shutil, "which", lambda _name: "/installed/codex")
    assert (
        json.loads(router._codex_original_catalog({}))["models"][0][
            "max_context_window"
        ]
        == 765_432
    )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "body", ["invalid", "null", "[]", '{"models": null}', '{"models": {}}']
)
def test_malformed_bundled_catalog_leaves_fallback_available(monkeypatch, body):
    monkeypatch.setattr(
        router.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess([], 0, body, ""),
    )
    assert router._codex_bundled_context_windows("/installed/codex") == {}


@pytest.mark.parametrize(
    "error", [OSError("missing"), subprocess.TimeoutExpired("codex", 30)]
)
def test_unavailable_bundle_leaves_fallback_available(monkeypatch, error):
    def run(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(router.subprocess, "run", run)
    assert router._codex_bundled_context_windows("/installed/codex") == {}


def test_failed_bundled_command_leaves_fallback_available(monkeypatch):
    monkeypatch.setattr(
        router.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess([], 1, "", "unsupported"),
    )
    assert router._codex_bundled_context_windows("/installed/codex") == {}


def test_native_ceiling_never_enlarges_a_smaller_router_window():
    model = _model(provider_model="gpt-6.1-sol", context_window=128_000)
    catalog = {"models": [_row(window=128_000)]}
    assert (
        json.loads(router._render_codex_catalog(catalog, "test-model", [model]))
        == catalog
    )


def test_native_ceiling_never_enlarges_an_existing_ceiling():
    model = _model(provider_model="gpt-6.1-sol", context_window=1_050_000)
    catalog = {"models": [_row(window=1_050_000, max_context_window=128_000)]}
    rendered = json.loads(router._render_codex_catalog(catalog, "test-model", [model]))
    assert rendered["models"] == [_row(window=128_000)]


@pytest.mark.parametrize(
    "provider_model",
    ["gpt-next", "openai:gpt-next", "openai/gpt-next", "us.openai.gpt-next"],
)
def test_gpt_policy_recognizes_provider_qualified_identities(provider_model):
    model = _model(provider_model=provider_model, context_window=1_050_000)
    rendered = json.loads(
        router._render_codex_catalog(
            {"models": [_row(window=1_050_000)]}, "test-model", [model]
        )
    )
    assert rendered["models"] == [_row(window=272_000, max_context_window=872_000)]


@pytest.mark.parametrize(
    "provider_model,context,output,threshold,default",
    [
        ("grok-4.7", 500_000, 500_000, 200_000, 400_000),
        ("claude-haiku-5-5", 1_000_000, 128_000, 100_000, 800_000),
        ("other-model", 1_000_000, 128_000, None, 800_000),
        ("other-model", 1_000_000, 128_000, 900_000, 800_000),
        ("other-model", 1_000_003, 1_000_003, 100_000, 800_002),
        ("other-model", 1, 1, None, 1),
    ],
)
def test_non_gpt_defaults_use_eighty_percent_and_retain_the_full_ceiling(
    provider_model, context, output, threshold, default
):
    model = _model(
        provider_model=provider_model,
        context_window=context,
        max_output_tokens=output,
        long_context_token_threshold=threshold,
    )
    catalog = {"models": [_row(window=context)]}
    original = copy.deepcopy(catalog)
    rendered = json.loads(router._render_codex_catalog(catalog, "test-model", [model]))
    assert rendered["models"] == [_row(window=default, max_context_window=context)]
    assert catalog == original


def test_provider_identity_not_a_gpt_named_route_decides_the_policy():
    model = _model(provider_model="claude-haiku-5-5", request_name="gpt-route")
    model = replace(model, id="gpt-route")
    rendered = json.loads(
        router._render_codex_catalog(
            {"models": [_row("gpt-route")]}, "gpt-route", [model]
        )
    )
    assert rendered["models"] == [
        _row("gpt-route", 800_000, max_context_window=1_000_000)
    ]


def test_non_gpt_preserves_a_smaller_published_window_and_ignores_gpt_bundle_rules():
    model = _model(provider_model="other-model")
    catalog = {"models": [_row(window=100_000, max_context_window=500_000)]}
    rendered = json.loads(
        router._render_codex_catalog(
            catalog, "test-model", [model], native_windows={"other-model": 200_000}
        )
    )
    assert rendered == catalog
