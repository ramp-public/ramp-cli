"""Router picker names must retain the native model's harness instructions."""

import json
import subprocess
from dataclasses import replace

import pytest

from ramp_cli.commands import router


def _model(
    identifier="sol-route", *, request_name="sol-request", display_name="GPT-6.1 Sol"
):
    return router.RouterModel(
        identifier,
        router._model_metadata(
            {
                "schema_version": 1,
                "request_name": request_name,
                "provider_model": "gpt-6.1-sol",
                "display_name": display_name,
                "limits": {"context_window": 1_050_000, "max_output_tokens": 128_000},
            },
            identifier,
        ),
    )


@pytest.fixture
def native_catalog(monkeypatch):
    catalog = {
        "models": [
            {"slug": "gpt-6.1-sol", "base_instructions": "Sol 6.1 instructions"},
            {"slug": "gpt-5.6-sol", "base_instructions": "Shared 5.6 instructions"},
            {"slug": "gpt-5.6-luna", "base_instructions": "Shared 5.6 instructions"},
        ]
    }
    monkeypatch.setattr(
        router.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, json.dumps(catalog), ""
        ),
    )
    return catalog


@pytest.mark.parametrize(
    "name", ["gpt-6.1-sol", "GPT-6.1 Sol", "sol-route", "sol-request"]
)
def test_picker_identity_selects_the_models_own_instructions(native_catalog, name):
    assert (
        router._codex_harness_prompt_from(
            "/installed/codex", name, bundled=True, router_models=[_model()]
        )
        == "Sol 6.1 instructions"
    )


def test_native_slug_wins_over_router_identity(native_catalog):
    assert (
        router._codex_harness_prompt_from(
            "/installed/codex", "gpt-5.6-sol", router_models=[_model("gpt-5.6-sol")]
        )
        == "Shared 5.6 instructions"
    )


def test_request_identity_wins_over_another_models_display_name(native_catalog):
    models = [_model(), _model("other", display_name="sol-route")]
    assert (
        router._codex_harness_prompt_from(
            "/installed/codex", "sol-route", router_models=models
        )
        == "Sol 6.1 instructions"
    )


def test_ambiguous_display_names_do_not_borrow_one_models_prompt(native_catalog):
    models = [_model(), _model("other")]
    assert (
        router._codex_harness_prompt_from(
            "/installed/codex", "GPT-6.1 Sol", router_models=models
        )
        == "Shared 5.6 instructions"
    )


def test_unbundled_provider_identity_tries_the_request_name(native_catalog):
    model = _model(request_name="gpt-6.1-sol")
    model = replace(model, metadata=replace(model.metadata, provider_model="snapshot"))
    assert (
        router._codex_harness_prompt_from(
            "/installed/codex", "GPT-6.1 Sol", router_models=[model]
        )
        == "Sol 6.1 instructions"
    )


def test_unknown_model_retains_the_shared_prompt_fallback(native_catalog):
    assert (
        router._codex_harness_prompt_from(
            "/installed/codex", "unknown", router_models=[_model()]
        )
        == "Shared 5.6 instructions"
    )
