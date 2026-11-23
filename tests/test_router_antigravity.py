"""Tests for Antigravity Ramp Router setup, model views, refresh, and teardown."""

from __future__ import annotations

import itertools
import json
import os
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from ramp_cli.commands import antigravity, router, router_picks
from ramp_cli.commands.antigravity import (
    restart_running as _real_restart_running,
)
from ramp_cli.commands.antigravity import (
    runs_inside_antigravity as _real_runs_inside,
)
from ramp_cli.main import cli


def _make_model(
    model_id: str,
    *,
    display_name: str | None = None,
    description: str = "",
    context_window: int = 200_000,
    max_output_tokens: int = 64_000,
    input_modalities: tuple[str, ...] = ("text", "image"),
    efforts: tuple[str, ...] = ("medium", "high"),
) -> router.RouterModel:
    return router.RouterModel(
        id=model_id,
        metadata=router.RouterModelMetadata(
            request_name=model_id,
            provider_model=model_id,
            display_name=display_name or model_id,
            description=description,
            listing_order=0,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            input_modalities=input_modalities,
            efforts=efforts,
            default_effort=efforts[0] if efforts else None,
            tool_calls=True,
        ),
    )


def _stub_models(
    monkeypatch: pytest.MonkeyPatch,
    all_models: list[router.RouterModel],
    compact_models: list[router.RouterModel] | None = None,
) -> None:
    compact = compact_models if compact_models is not None else all_models[:2]

    def fake_fetch(
        api_key: str,
        claude_code_view: bool = False,
        *,
        gateway_client: str | None = None,
        model_view: str | None = None,
        base_url: str | None = None,
        wait_for_key: bool = False,
    ) -> list[router.RouterModel]:
        if model_view == "compact":
            return list(compact)
        return list(all_models)

    monkeypatch.setattr(router, "_fetch_models", fake_fetch)


def test_configure_antigravity_compact_preserves_existing_keys(monkeypatch):
    all_models = [
        _make_model(
            "org/frontier", display_name="org/frontier", input_modalities=(), efforts=()
        ),
        _make_model("gpt-6.2-sol", display_name="GPT-6.2 Sol"),
        _make_model(
            "claude-sonnet-5-5",
            display_name="Claude Sonnet 5.5",
            input_modalities=("text", "image", "file"),
        ),
        _make_model(
            "gemini-4-hydrogen",
            display_name="Gemini 4 Hydrogen",
            input_modalities=("text", "image", "video"),
        ),
        _make_model("kimi-k3.1-pro", display_name="Kimi K3.1 Pro"),
    ]
    compact = all_models[:4]
    _stub_models(monkeypatch, all_models, compact)
    monkeypatch.setattr(antigravity, "is_running", lambda: False)

    cfg_path = antigravity.config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {
                "plugins": {"swarm": {"enabled": True}},
                "userSettings": {"enableTerminalSandbox": True},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Connected to: Antigravity" in result.output
    assert "Antigravity model picker: compact" in result.output

    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["plugins"] == {"swarm": {"enabled": True}}
    assert saved["userSettings"] == {"enableTerminalSandbox": True}
    assert saved["gateway"] == {
        "enabled": True,
        "url": router.router_base_url().rstrip("/"),
        "wireProtocol": "openai",
        "apiKey": "sk-router-secret-12345678",
        "customHeaders": {"X-Ramp-Router-Client": "antigravity"},
    }
    models_cfg = saved["models"]
    assert [m["modelId"] for m in models_cfg["list"]] == [m.id for m in compact]
    assert models_cfg["defaultModelId"] == "org/frontier"

    by_id = {m["modelId"]: m for m in models_cfg["list"]}
    assert by_id["gpt-6.2-sol"]["provider"] == "openai"
    assert by_id["gpt-6.2-sol"]["supportsThinking"] is True
    assert by_id["claude-sonnet-5-5"]["provider"] == "anthropic"
    assert by_id["claude-sonnet-5-5"]["supportsPdf"] is True
    assert by_id["gemini-4-hydrogen"]["provider"] == "google"
    assert by_id["gemini-4-hydrogen"]["supportsVideo"] is True
    assert "provider" not in by_id["org/frontier"]
    assert models_cfg["tiers"]["flashLite"]
    assert models_cfg["tiers"]["flash"]
    assert models_cfg["tiers"]["pro"][0] == "org/frontier"


def test_configure_antigravity_all_orders_recommended_first_and_notes_cap(monkeypatch):
    compact = [
        _make_model("org/frontier", display_name="org/frontier"),
        _make_model("claude-sonnet-5-5", display_name="Claude Sonnet 5.5"),
    ]
    extras = [
        _make_model(f"custom-model-{i}", display_name=f"Custom Model {i}")
        for i in range(40)
    ]
    # Duplicate display name across two distinct model IDs should disambiguate.
    extras.append(_make_model("dup-b", display_name="Custom Model 0"))
    all_models = [*extras[:5], *compact, *extras[5:]]
    _stub_models(monkeypatch, all_models, compact)
    monkeypatch.setattr(antigravity, "is_running", lambda: True)

    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--antigravity-models",
            "all",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Antigravity model picker: all" in result.output
    assert "up to 32 custom" in result.output
    assert "Restart Antigravity" in result.output

    saved = json.loads(antigravity.config_path().read_text(encoding="utf-8"))
    ids = [m["modelId"] for m in saved["models"]["list"]]
    labels = [m["displayName"] for m in saved["models"]["list"]]
    assert ids[:2] == ["org/frontier", "claude-sonnet-5-5"]
    assert len(ids) == len(all_models)
    assert len(labels) == len(set(labels))
    assert "Custom Model 0 (dup-b)" in labels


def test_set_model_pins_any_router_model_to_front(monkeypatch):
    compact = [
        _make_model("org/frontier", display_name="org/frontier"),
        _make_model("gpt-6.2-sol", display_name="GPT-6.2 Sol"),
    ]
    extras = [_make_model(f"extra-{i}", display_name=f"Extra {i}") for i in range(40)]
    all_models = [*compact, *extras]
    _stub_models(monkeypatch, all_models, compact)
    monkeypatch.setattr(antigravity, "is_running", lambda: False)

    runner = CliRunner()
    configured = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert configured.exit_code == 0, configured.output

    switched = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "model",
            "antigravity",
            "--model",
            "extra-39",
        ],
    )
    assert switched.exit_code == 0, switched.output

    saved = json.loads(antigravity.config_path().read_text(encoding="utf-8"))
    assert saved["models"]["defaultModelId"] == "extra-39"
    assert saved["models"]["list"][0]["modelId"] == "extra-39"
    assert "org/frontier" in [m["modelId"] for m in saved["models"]["list"]]


def test_refresh_preserves_saved_view_and_model(monkeypatch):
    compact = [_make_model("gpt-6.2-sol", display_name="GPT-6.2 Sol")]
    all_models = [
        *compact,
        _make_model("claude-sonnet-5-5", display_name="Claude Sonnet 5.5"),
        _make_model("kimi-k3", display_name="Kimi K3"),
    ]
    _stub_models(monkeypatch, all_models, compact)
    monkeypatch.setattr(antigravity, "is_running", lambda: False)

    runner = CliRunner()
    configured = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--antigravity-models",
            "all",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert configured.exit_code == 0, configured.output

    # Add a new model on Router and verify refresh picks it up while keeping view=all.
    updated_all = [*all_models, _make_model("grok-4.7", display_name="Grok 4.7")]
    _stub_models(monkeypatch, updated_all, compact)

    refreshed = runner.invoke(cli, ["--human", "router", "refresh"])
    assert refreshed.exit_code == 0, refreshed.output

    saved = json.loads(antigravity.config_path().read_text(encoding="utf-8"))
    ids = [m["modelId"] for m in saved["models"]["list"]]
    assert "grok-4.7" in ids
    assert saved["models"]["defaultModelId"] == "gpt-6.2-sol"


def test_unconfigure_antigravity_restores_prior_config_and_removes_created_file(
    monkeypatch,
):
    all_models = [_make_model("gpt-6.2-sol", display_name="GPT-6.2 Sol")]
    _stub_models(monkeypatch, all_models, all_models)
    monkeypatch.setattr(antigravity, "is_running", lambda: False)
    runner = CliRunner()

    # Case 1: config.json did not exist beforehand -> unconfigure removes it.
    assert not antigravity.config_path().exists()
    r1 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert r1.exit_code == 0, r1.output
    u1 = runner.invoke(cli, ["--human", "router", "unconfigure", "antigravity"])
    assert u1.exit_code == 0, u1.output
    assert not antigravity.config_path().exists()
    assert not antigravity.state_path().exists()

    # Case 2: config.json had user keys + prior gateway -> restored verbatim.
    prior = {
        "plugins": {"swarm": {"enabled": True}},
        "gateway": {"enabled": False, "url": "https://old.example.com"},
    }
    antigravity.config_path().parent.mkdir(parents=True, exist_ok=True)
    antigravity.config_path().write_text(
        json.dumps(prior, indent=2) + "\n", encoding="utf-8"
    )

    r2 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert r2.exit_code == 0, r2.output
    u2 = runner.invoke(cli, ["--human", "router", "unconfigure", "antigravity"])
    assert u2.exit_code == 0, u2.output
    assert json.loads(antigravity.config_path().read_text(encoding="utf-8")) == prior


def test_antigravity_models_flag_requires_antigravity_only():
    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "codex",
            "--antigravity-models",
            "compact",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert result.exit_code != 0
    assert "--antigravity-models requires exactly one agent" in result.output


def test_bare_configure_skips_antigravity_when_uninstalled_and_includes_when_installed(
    monkeypatch,
):
    all_models = [_make_model("gpt-6.2-sol", display_name="GPT-6.2 Sol")]
    _stub_models(monkeypatch, all_models, all_models)
    monkeypatch.setattr(antigravity, "is_running", lambda: False)
    monkeypatch.setattr(
        router, "_configure_claude_code", lambda *a, **kw: "gpt-6.2-sol"
    )
    monkeypatch.setattr(
        router, "_configure_codex", lambda *a, **kw: ("gpt-6.2-sol", False)
    )
    monkeypatch.setattr(router, "_configure_plugin_client", lambda *a, **kw: None)
    monkeypatch.setattr(router.claude_cowork, "is_available", lambda: False)
    monkeypatch.setattr(router.conductor, "is_installed", lambda: False)

    runner = CliRunner()
    assert not antigravity.is_installed()
    r1 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert r1.exit_code == 0, r1.output
    assert not antigravity.state_path().exists()

    # Now mark Antigravity installed via its data directory.
    (antigravity.gemini_dir() / "antigravity").mkdir(parents=True, exist_ok=True)
    assert antigravity.is_installed()
    r2 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert r2.exit_code == 0, r2.output
    assert antigravity.state_path().exists()


def test_configure_list_omits_antigravity_while_model_commands_work(monkeypatch):
    all_models = [
        _make_model("gpt-6.2-sol", display_name="GPT-6.2 Sol"),
        _make_model("claude-sonnet-5-5", display_name="Claude Sonnet 5.5"),
    ]
    _stub_models(monkeypatch, all_models, all_models)
    (antigravity.gemini_dir() / "antigravity").mkdir(parents=True, exist_ok=True)

    runner = CliRunner()
    configured = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert configured.exit_code == 0, configured.output

    listed = runner.invoke(cli, ["--agent", "router", "configure", "list"])
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)["data"]
    assert all(item["harness"] != "antigravity" for item in payload)

    models_cmd = runner.invoke(
        cli, ["--agent", "router", "configure", "models", "antigravity"]
    )
    assert models_cmd.exit_code == 0, models_cmd.output
    assert any(
        m["id"] == "claude-sonnet-5-5"
        for m in json.loads(models_cmd.output)["data"][0]["models"]
    )


def test_repair_configure_over_lost_or_corrupt_receipt_does_not_snapshot_router_config(
    monkeypatch,
):
    all_models = [
        _make_model("gpt-6.2-sol", display_name="GPT-6.2 Sol"),
        _make_model("claude-sonnet-5-5", display_name="Claude Sonnet 5.5"),
    ]
    _stub_models(monkeypatch, all_models, all_models)
    runner = CliRunner()

    cfg_path = antigravity.config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    original = {
        "plugins": {"swarm": {"enabled": True}},
        "userSettings": {"enableTerminalSandbox": True},
    }
    cfg_path.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")

    # Initial configure.
    r1 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert r1.exit_code == 0, r1.output

    # Simulate lost receipt, then repair configure and unconfigure.
    antigravity.state_path().unlink()
    r2 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert r2.exit_code == 0, r2.output
    receipt = json.loads(antigravity.state_path().read_text(encoding="utf-8"))
    assert receipt["previous"] == {
        "gateway": {"present": False},
        "models": {"present": False},
    }

    u1 = runner.invoke(cli, ["--human", "router", "unconfigure", "antigravity"])
    assert u1.exit_code == 0, u1.output
    assert json.loads(cfg_path.read_text(encoding="utf-8")) == original

    # Simulate corrupt receipt after configure, then repair and unconfigure.
    r3 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert r3.exit_code == 0, r3.output
    antigravity.state_path().write_text("{corrupt json", encoding="utf-8")

    r4 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert r4.exit_code == 0, r4.output
    u2 = runner.invoke(cli, ["--human", "router", "unconfigure", "antigravity"])
    assert u2.exit_code == 0, u2.output
    assert json.loads(cfg_path.read_text(encoding="utf-8")) == original

    # Simulate invalid UTF-8 bytes in the receipt -> repair still succeeds.
    r5 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert r5.exit_code == 0, r5.output
    antigravity.state_path().write_bytes(b"\xff\xfe\x00invalid")

    r6 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert r6.exit_code == 0, r6.output
    u3 = runner.invoke(cli, ["--human", "router", "unconfigure", "antigravity"])
    assert u3.exit_code == 0, u3.output
    assert json.loads(cfg_path.read_text(encoding="utf-8")) == original


def test_configure_and_refresh_unlock_during_fetch_and_read_view_under_lock(
    monkeypatch,
):
    all_models = [
        _make_model("gpt-6.2-sol", display_name="GPT-6.2 Sol"),
        _make_model("claude-sonnet-5-5", display_name="Claude Sonnet 5.5"),
        _make_model("kimi-k3.1-pro", display_name="Kimi K3.1 Pro"),
    ]
    compact = all_models[:2]

    lock_depth = 0
    real_lock = router._antigravity_config_lock

    @contextmanager
    def tracking_lock(path):
        nonlocal lock_depth
        with real_lock(path):
            lock_depth += 1
            try:
                yield
            finally:
                lock_depth -= 1

    monkeypatch.setattr(router, "_antigravity_config_lock", tracking_lock)

    flip_view_on_compact_fetch = False

    def checked_fetch(
        api_key: str,
        claude_code_view: bool = False,
        *,
        gateway_client: str | None = None,
        model_view: str | None = None,
        base_url: str | None = None,
        wait_for_key: bool = False,
    ) -> list[router.RouterModel]:
        nonlocal flip_view_on_compact_fetch
        assert lock_depth == 0, (
            f"_fetch_models(model_view={model_view!r}) ran while holding config lock"
        )
        if model_view == "compact":
            if flip_view_on_compact_fetch:
                flip_view_on_compact_fetch = False
                state_file = antigravity.state_path()
                state = json.loads(state_file.read_text(encoding="utf-8"))
                state[antigravity.MODEL_VIEW_KEY] = "all"
                state_file.write_text(
                    json.dumps(state, indent=2) + "\n", encoding="utf-8"
                )
            return list(compact)
        return list(all_models)

    monkeypatch.setattr(router, "_fetch_models", checked_fetch)
    runner = CliRunner()

    # 1. Configure (compact) does not hold lock during _fetch_models.
    r1 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert r1.exit_code == 0, r1.output

    # 2. Concurrent view flip during _fetch_models is observed inside the lock
    # during repeat configure (no --antigravity-models passed).
    flip_view_on_compact_fetch = True
    r2 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert r2.exit_code == 0, r2.output
    saved = json.loads(antigravity.config_path().read_text(encoding="utf-8"))
    assert [m["modelId"] for m in saved["models"]["list"]] == [m.id for m in all_models]

    # 3. Full refresh, models-only refresh, and configure model all fetch unlocked.
    r3 = runner.invoke(cli, ["--human", "router", "refresh"])
    assert r3.exit_code == 0, r3.output

    effective = router._refresh_antigravity_models(
        antigravity.config_path(),
        "sk-router-secret-12345678",
        all_models,
        base_url=router.router_base_url(),
    )
    assert effective == "gpt-6.2-sol"

    r4 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "model",
            "antigravity",
            "--model",
            "kimi-k3.1-pro",
        ],
    )
    assert r4.exit_code == 0, r4.output


def test_refresh_follows_router_fallback_until_explicit_model_choice(monkeypatch):
    all_models = [
        _make_model("gpt-6.2-sol", display_name="GPT-6.2 Sol"),
        _make_model("claude-sonnet-5-5", display_name="Claude Sonnet 5.5"),
        _make_model("gemini-4-hydrogen", display_name="Gemini 4 Hydrogen"),
    ]
    _stub_models(monkeypatch, all_models, all_models)
    fallback = {"value": "gpt-6.2-sol"}
    monkeypatch.setattr(
        "ramp_cli.admin_defaults.fallback_model",
        lambda *_args, **_kwargs: fallback["value"],
    )
    runner = CliRunner()

    r1 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert r1.exit_code == 0, r1.output
    saved = json.loads(antigravity.config_path().read_text(encoding="utf-8"))
    assert saved["models"]["defaultModelId"] == "gpt-6.2-sol"

    # Server fallback changes -> refresh moves Router's own pick.
    fallback["value"] = "claude-sonnet-5-5"
    r2 = runner.invoke(cli, ["--human", "router", "refresh"])
    assert r2.exit_code == 0, r2.output
    saved = json.loads(antigravity.config_path().read_text(encoding="utf-8"))
    assert saved["models"]["defaultModelId"] == "claude-sonnet-5-5"

    # User explicitly picks a model -> later fallback change leaves it alone.
    r3 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "model",
            "antigravity",
            "--model",
            "gpt-6.2-sol",
        ],
    )
    assert r3.exit_code == 0, r3.output

    fallback["value"] = "gemini-4-hydrogen"
    r4 = runner.invoke(cli, ["--human", "router", "refresh"])
    assert r4.exit_code == 0, r4.output
    saved = json.loads(antigravity.config_path().read_text(encoding="utf-8"))
    assert saved["models"]["defaultModelId"] == "gpt-6.2-sol"


def test_refresh_and_configure_preserve_model_saved_during_compact_discovery(
    monkeypatch,
):
    all_models = [
        _make_model("gpt-6.2-sol", display_name="GPT-6.2 Sol"),
        _make_model("claude-sonnet-5-5", display_name="Claude Sonnet 5.5"),
        _make_model("kimi-k3.1-pro", display_name="Kimi K3.1 Pro"),
    ]
    compact = all_models[:2]
    monkeypatch.setattr(
        "ramp_cli.admin_defaults.fallback_model",
        lambda *_args, **_kwargs: "gpt-6.2-sol",
    )

    save_during_compact = {"model": None}

    def fetch_with_concurrent_save(
        api_key: str,
        claude_code_view: bool = False,
        *,
        gateway_client: str | None = None,
        model_view: str | None = None,
        base_url: str | None = None,
        wait_for_key: bool = False,
    ) -> list[router.RouterModel]:
        if model_view == "compact" and save_during_compact["model"]:
            chosen = save_during_compact["model"]
            save_during_compact["model"] = None
            cfg_path = antigravity.config_path()
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["models"]["defaultModelId"] = chosen
            cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
            router_picks.record("antigravity", cfg_path, None)
            return list(compact)
        return list(compact) if model_view == "compact" else list(all_models)

    monkeypatch.setattr(router, "_fetch_models", fetch_with_concurrent_save)
    runner = CliRunner()

    # Initial configure lands on Router's fallback.
    r1 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert r1.exit_code == 0, r1.output
    cfg_path = antigravity.config_path()
    assert (
        json.loads(cfg_path.read_text(encoding="utf-8"))["models"]["defaultModelId"]
        == "gpt-6.2-sol"
    )

    # 1. Full refresh: user saves kimi-k3.1-pro during compact discovery -> preserved.
    save_during_compact["model"] = "kimi-k3.1-pro"
    r2 = runner.invoke(cli, ["--human", "router", "refresh"])
    assert r2.exit_code == 0, r2.output
    assert (
        json.loads(cfg_path.read_text(encoding="utf-8"))["models"]["defaultModelId"]
        == "kimi-k3.1-pro"
    )
    assert router_picks.router_pick("antigravity", cfg_path, "kimi-k3.1-pro") is None

    # 2. Models-only refresh: user saves claude-sonnet-5-5 during compact discovery -> preserved.
    save_during_compact["model"] = "claude-sonnet-5-5"
    effective = router._refresh_antigravity_models(
        cfg_path,
        "sk-router-secret-12345678",
        all_models,
        base_url=router.router_base_url(),
        selected_model="kimi-k3.1-pro",
    )
    assert effective == "claude-sonnet-5-5"
    assert (
        json.loads(cfg_path.read_text(encoding="utf-8"))["models"]["defaultModelId"]
        == "claude-sonnet-5-5"
    )

    # 3. Repeat configure: user saves kimi-k3.1-pro during compact discovery -> preserved.
    save_during_compact["model"] = "kimi-k3.1-pro"
    r3 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert r3.exit_code == 0, r3.output
    assert (
        json.loads(cfg_path.read_text(encoding="utf-8"))["models"]["defaultModelId"]
        == "kimi-k3.1-pro"
    )


def test_unconfigure_preserves_subsequent_provider_configuration_and_keeps_file(
    monkeypatch,
):
    all_models = [
        _make_model("gpt-6.2-sol", display_name="GPT-6.2 Sol"),
        _make_model("claude-sonnet-5-5", display_name="Claude Sonnet 5.5"),
    ]
    _stub_models(monkeypatch, all_models, all_models)
    runner = CliRunner()
    cfg_path = antigravity.config_path()

    # Case 1: Router created config.json, then user replaced gateway and models
    # with another provider -> unconfigure preserves both and does not unlink.
    r1 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert r1.exit_code == 0, r1.output

    other_provider = {
        "gateway": {
            "enabled": True,
            "url": "https://other-provider.example.com/v1",
            "wireProtocol": "openai",
            "apiKey": "other-secret",
        },
        "models": {
            "list": [{"modelId": "custom-model", "displayName": "Custom"}],
            "defaultModelId": "custom-model",
        },
    }
    cfg_path.write_text(json.dumps(other_provider, indent=2) + "\n", encoding="utf-8")

    u1 = runner.invoke(cli, ["--human", "router", "unconfigure", "antigravity"])
    assert u1.exit_code == 0, u1.output
    assert cfg_path.exists()
    assert json.loads(cfg_path.read_text(encoding="utf-8")) == other_provider
    assert not antigravity.state_path().exists()

    # Case 2: User customized only models while keeping Router's gateway ->
    # unconfigure removes Router's gateway and preserves the custom models block.
    cfg_path.unlink()
    r2 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert r2.exit_code == 0, r2.output

    edited = json.loads(cfg_path.read_text(encoding="utf-8"))
    custom_models = {
        "list": [{"modelId": "my-local-model", "displayName": "My Local"}],
        "defaultModelId": "my-local-model",
    }
    edited["models"] = custom_models
    cfg_path.write_text(json.dumps(edited, indent=2) + "\n", encoding="utf-8")

    u2 = runner.invoke(cli, ["--human", "router", "unconfigure", "antigravity"])
    assert u2.exit_code == 0, u2.output
    assert cfg_path.exists()
    assert json.loads(cfg_path.read_text(encoding="utf-8")) == {"models": custom_models}


def test_refresh_rolls_back_config_on_receipt_write_failure_and_repairs_stale_receipt(
    monkeypatch,
):
    all_models = [
        _make_model("gpt-6.2-sol", display_name="GPT-6.2 Sol"),
        _make_model("claude-sonnet-5-5", display_name="Claude Sonnet 5.5"),
    ]
    _stub_models(monkeypatch, all_models, all_models)
    runner = CliRunner()
    cfg_path = antigravity.config_path()
    state_path = antigravity.state_path()

    r1 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert r1.exit_code == 0, r1.output
    before_cfg = cfg_path.read_text(encoding="utf-8")

    # 1. Fail when writing state_file during refresh -> config.json is rolled back.
    expanded = [
        *all_models,
        _make_model("kimi-k3.1-pro", display_name="Kimi K3.1 Pro"),
    ]
    _stub_models(monkeypatch, expanded, expanded)
    real_write = router._write_private_file

    def fail_state_write(target, content):
        if target == state_path:
            raise OSError("disk full")
        real_write(target, content)

    monkeypatch.setattr(router, "_write_private_file", fail_state_write)
    with pytest.raises(router.click.ClickException, match="disk full"):
        router._refresh_antigravity_models(
            cfg_path,
            "sk-router-secret-12345678",
            expanded,
            base_url=router.router_base_url(),
        )
    assert cfg_path.read_text(encoding="utf-8") == before_cfg

    # 2. If receipt is stale while config.json already has the latest models,
    #    a retry still repairs the receipt so unconfigure restores cleanly.
    monkeypatch.setattr(router, "_write_private_file", real_write)
    router._refresh_antigravity_models(
        cfg_path,
        "sk-router-secret-12345678",
        expanded,
        base_url=router.router_base_url(),
    )
    stale_state = json.loads(state_path.read_text(encoding="utf-8"))
    stale_state["written"]["models"] = {"list": [], "defaultModelId": "stale"}
    state_path.write_text(json.dumps(stale_state, indent=2) + "\n", encoding="utf-8")

    router._refresh_antigravity_models(
        cfg_path,
        "sk-router-secret-12345678",
        expanded,
        base_url=router.router_base_url(),
    )
    repaired_state = json.loads(state_path.read_text(encoding="utf-8"))
    current_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert repaired_state["written"]["models"] == current_cfg["models"]


def test_unconfigure_preserves_edited_gateway_even_with_custom_header(monkeypatch):
    all_models = [_make_model("gpt-6.2-sol", display_name="GPT-6.2 Sol")]
    _stub_models(monkeypatch, all_models, all_models)
    runner = CliRunner()
    cfg_path = antigravity.config_path()

    r1 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert r1.exit_code == 0, r1.output

    # User edits gateway URL to another provider without removing customHeaders.
    edited = json.loads(cfg_path.read_text(encoding="utf-8"))
    edited["gateway"]["url"] = "https://other-gateway.example.com/v1"
    cfg_path.write_text(json.dumps(edited, indent=2) + "\n", encoding="utf-8")

    u1 = runner.invoke(cli, ["--human", "router", "unconfigure", "antigravity"])
    assert u1.exit_code == 0, u1.output
    assert cfg_path.exists()
    after = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert after["gateway"] == edited["gateway"]
    assert "models" not in after


_RESTART_IS_MACOS_ONLY = pytest.mark.posix_only(
    reason="restarting Antigravity uses macOS pgrep -u <uid>, osascript and open"
)


@_RESTART_IS_MACOS_ONLY
@pytest.mark.parametrize(
    ("platform", "state", "expect_restarted", "expect_error"),
    [
        ("darwin", "running", True, None),
        ("darwin", "stopped", False, None),
        ("darwin", "inside-app", False, None),
        ("darwin", "probe-failed", False, "could not be restarted"),
        ("darwin", "quit-failed", False, "could not be restarted"),
        ("darwin", "quit-pending", False, "could not be restarted"),
        ("darwin", "open-failed", False, "could not be reopened"),
        ("linux", "running", False, None),
    ],
)
def test_antigravity_restart_running_behavior(
    monkeypatch, platform, state, expect_restarted, expect_error
):
    monkeypatch.setattr(antigravity, "sys", SimpleNamespace(platform=platform))
    clock = itertools.count(step=5)
    monkeypatch.setattr(
        antigravity,
        "time",
        SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _d: None),
    )
    monkeypatch.setattr(
        antigravity,
        "runs_inside_antigravity",
        lambda _pids=None: state == "inside-app",
    )
    calls: list[list[str]] = []
    probes_after_quit = 0
    quit_cmd = [
        "osascript",
        "-e",
        f'tell application id "{antigravity.APP_BUNDLE_ID}" to quit',
    ]
    open_cmd = ["open", "-b", antigravity.APP_BUNDLE_ID]

    def fake_run(command, **kwargs):
        nonlocal probes_after_quit
        calls.append(command)
        if command[0] == "pgrep":
            assert command == [
                "pgrep",
                "-a",
                "-u",
                str(os.getuid()),
                "-x",
                "Antigravity",
            ]
            if state == "stopped":
                return subprocess.CompletedProcess(command, 1, stdout="")
            if state == "probe-failed":
                return subprocess.CompletedProcess(command, 2, stdout="")
            if quit_cmd in calls:
                probes_after_quit += 1
                if state == "quit-pending" or probes_after_quit == 1:
                    return subprocess.CompletedProcess(command, 0, stdout="4242\n")
                return subprocess.CompletedProcess(command, 1, stdout="")
            return subprocess.CompletedProcess(command, 0, stdout="4242\n")
        if command == quit_cmd:
            return subprocess.CompletedProcess(
                command, 1 if state == "quit-failed" else 0
            )
        assert command == open_cmd
        return subprocess.CompletedProcess(command, 1 if state == "open-failed" else 0)

    monkeypatch.setattr(
        antigravity,
        "subprocess",
        SimpleNamespace(**{**vars(subprocess), "run": fake_run}),
    )

    if expect_error is not None:
        with pytest.raises(router.click.ClickException, match=expect_error):
            _real_restart_running(action="configured")
    else:
        assert _real_restart_running(action="configured") is expect_restarted
        if expect_restarted:
            assert quit_cmd in calls
            assert open_cmd in calls
        elif platform != "darwin" or state in ("stopped", "inside-app"):
            assert quit_cmd not in calls
            assert open_cmd not in calls


def test_restart_called_on_configure_model_and_unconfigure_never_on_refresh(
    monkeypatch,
):
    all_models = [
        _make_model("gpt-6.2-sol", display_name="GPT-6.2 Sol"),
        _make_model("claude-sonnet-5-5", display_name="Claude Sonnet 5.5"),
    ]
    _stub_models(monkeypatch, all_models, all_models)
    restarts: list[str] = []

    def fake_restart(*, action: str = "configured") -> bool:
        restarts.append(action)
        return True

    monkeypatch.setattr(antigravity, "restart_running", fake_restart)
    runner = CliRunner()

    r1 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "antigravity",
            "--api-key",
            "sk-router-secret-12345678",
            "--no-input",
        ],
    )
    assert r1.exit_code == 0, r1.output
    assert "Reopened: Antigravity." in r1.output
    assert "Restart Antigravity so it reloads" not in r1.output
    assert restarts == ["configured"]

    # Refresh must never restart Antigravity.
    r2 = runner.invoke(cli, ["--human", "router", "refresh"])
    assert r2.exit_code == 0, r2.output
    assert restarts == ["configured"]

    # configure model restarts Antigravity.
    r3 = runner.invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "model",
            "antigravity",
            "--model",
            "claude-sonnet-5-5",
        ],
    )
    assert r3.exit_code == 0, r3.output
    assert "Antigravity was restarted." in r3.output
    assert restarts == ["configured", "updated"]

    # unconfigure restarts Antigravity.
    r4 = runner.invoke(cli, ["--human", "router", "unconfigure", "antigravity"])
    assert r4.exit_code == 0, r4.output
    assert "Antigravity was restarted." in r4.output
    assert restarts == ["configured", "updated", "removed"]


@_RESTART_IS_MACOS_ONLY
def test_restart_skips_quit_when_ancestry_probe_fails(monkeypatch):
    monkeypatch.setattr(antigravity, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(antigravity, "runs_inside_antigravity", _real_runs_inside)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == "pgrep":
            return subprocess.CompletedProcess(command, 0, stdout="4242\n")
        if command[0] == "ps":
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 2))
        raise AssertionError(
            f"Unexpected command after failed ancestry probe: {command}"
        )

    monkeypatch.setattr(
        antigravity,
        "subprocess",
        SimpleNamespace(**{**vars(subprocess), "run": fake_run}),
    )

    assert _real_runs_inside([4242]) is None
    assert _real_restart_running(action="configured") is False
    assert [cmd[0] for cmd in calls] == ["ps", "pgrep", "ps"]


def test_build_model_entries_clamps_shared_window_max_output_tokens():
    entries = {
        e["modelId"]: e
        for e in antigravity.build_model_entries(
            [
                _make_model(
                    "grok-4.7", context_window=500_000, max_output_tokens=500_000
                ),
                _make_model(
                    "muse-wonder-1.0",
                    context_window=1_048_576,
                    max_output_tokens=1_048_576,
                ),
                _make_model(
                    "custom/small", context_window=16_384, max_output_tokens=16_384
                ),
                _make_model(
                    "deepseek-v4.1-flash",
                    context_window=1_048_576,
                    max_output_tokens=262_144,
                ),
            ]
        )
    }
    assert entries["grok-4.7"]["maxOutputTokens"] == 125_000
    assert entries["muse-wonder-1.0"]["maxOutputTokens"] == 128_000
    assert entries["custom/small"]["maxOutputTokens"] == 4_096
    assert entries["deepseek-v4.1-flash"]["maxOutputTokens"] == 262_144
