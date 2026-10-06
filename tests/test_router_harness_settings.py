"""Installed-connection boundaries and scriptable harness model updates."""

import json
from contextlib import nullcontext
from types import SimpleNamespace

import click
import httpx
import pytest
from click.testing import CliRunner

from ramp_cli.commands import router
from ramp_cli.main import cli
from ramp_cli.router_ui import harness


@pytest.fixture
def installed(monkeypatch, tmp_path):
    writes = []
    monkeypatch.setattr(
        router, "_client_config_path", lambda client: tmp_path / client / "settings"
    )
    monkeypatch.setattr(
        router, "configured_router_clients", lambda: tuple(router.CLIENT_NAMES)
    )
    monkeypatch.setattr(
        router, "_stored_router_api_key", lambda client, path: "inference-secret"
    )
    monkeypatch.setattr(
        router,
        "_stored_router_base_url",
        lambda client, path: "https://qa.router.invalid/v1",
    )
    monkeypatch.setattr(router, "_configured_model", lambda client, path: "old")
    monkeypatch.setattr(router, "_configured_claude_model_view_all", lambda path: False)
    monkeypatch.setattr(router, "_hermes_config_lock", nullcontext)
    monkeypatch.setattr(router, "_conductor_settings_lock", lambda path: nullcontext())
    monkeypatch.setattr(
        router,
        "_fetch_models",
        lambda *args, **kwargs: [
            SimpleNamespace(id=name, metadata=SimpleNamespace(display_name=name))
            for name in ("old", "new")
        ],
    )
    monkeypatch.setattr(
        router,
        "_fetch_codex_catalog",
        lambda *args, **kwargs: {"models": [{"slug": "old"}, {"slug": "new"}]},
    )

    def configure(client, key, models, **options):
        writes.append((client, key, options))
        return tmp_path / client / "settings", options["selected_model"], False

    monkeypatch.setattr(router, "_configure_client", configure)
    return writes


@pytest.mark.parametrize("client", ["claude-code", "codex", "opencode", "pi", "hermes"])
def test_model_update_uses_existing_writer_and_saved_endpoint(installed, client):
    data = harness.editor(client)
    assert "inference-secret" not in json.dumps(data)
    result = harness.set_model(client, "new", expected=data["connection_token"])
    assert result["default_model"] == "new"
    name, key, options = installed[0]
    assert name == client and key == "inference-secret"
    assert options["base_url"] == "https://qa.router.invalid/v1"
    assert options["selected_model"] == "new"
    if client == "hermes":
        assert options["hermes_lock_held"] and options["hermes_require_receipt"]
    if client == "claude-code":
        assert options["claude_model_view"] == "compact"
        assert options["preserve_model_view_ownership"]


@pytest.mark.parametrize("flags", [["--agent"], ["--output", "json"], ["--human"]])
def test_model_command_dry_run_never_writes_and_agent_has_envelope(installed, flags):
    result = CliRunner().invoke(
        cli,
        ["router", "configure", "model", "pi", "--model", "new", "--dry-run", *flags],
    )
    assert result.exit_code == 0, result.output
    assert not installed
    assert "inference-secret" not in result.output
    if flags == ["--agent"]:
        data = json.loads(result.output)
        assert data["schema_version"] == "1.0"
        assert data["data"][0]["dry_run"]
    elif flags == ["--output", "json"]:
        assert json.loads(result.output)["dry_run"]


def test_invalid_model_changed_connection_and_missing_endpoint_fail_before_writes(
    installed, monkeypatch
):
    with pytest.raises(click.BadParameter):
        harness.set_model("pi", "unavailable")
    with pytest.raises(click.ClickException, match="connection changed"):
        harness.set_model("pi", "new", expected="stale")
    monkeypatch.setattr(router, "_stored_router_base_url", lambda *args: None)
    monkeypatch.setattr(
        router,
        "_fetch_models",
        lambda *args, **kwargs: pytest.fail("must not send the key to a fallback host"),
    )
    with pytest.raises(click.ClickException, match="endpoint is missing"):
        harness.editor("pi")
    assert not installed


def test_unconfigured_harness_and_app_managed_default_are_not_overwritten(
    installed, monkeypatch
):
    assert harness.editor("conductor")["model_note"]
    with pytest.raises(click.UsageError, match="inside its app"):
        harness.set_model("conductor", "new")
    monkeypatch.setattr(router, "configured_router_clients", lambda: ())
    with pytest.raises(click.UsageError, match="Connect this harness"):
        harness.set_model("pi", "new")
    assert not installed


def test_codex_model_removed_between_catalog_reads_does_not_write_hooks_or_config(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        router,
        "_fetch_codex_catalog",
        lambda *args, **kwargs: {"models": [{"slug": "new"}]},
    )
    monkeypatch.setattr(
        router,
        "_install_codex_cost_hook",
        lambda *args, **kwargs: pytest.fail("must validate before changing hooks"),
    )
    with pytest.raises(click.BadParameter, match="no longer available"):
        router._configure_codex(
            tmp_path / "config.toml",
            "fake-secret",
            [],
            selected_model="removed slug",
            strict_model=True,
        )
    assert not (tmp_path / "config.toml").exists()


def test_routing_follows_saved_connection_instead_of_ambient_ui_override(
    installed, monkeypatch
):
    monkeypatch.setenv(router.ROUTER_UI_URL_ENV, "https://unrelated.invalid")
    requests = []
    payload = {"allow_flex_tier_default": False, "switchyard_routing_enabled": False}

    def get(url, **kwargs):
        requests.append((url, kwargs))
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(router.httpx, "get", get)
    data = harness.routing_settings("pi")
    assert requests[0][0] == "https://qa.router.invalid/session-usage/strategies"
    assert requests[0][1]["headers"]["Authorization"] == "Bearer inference-secret"
    assert "inference-secret" not in json.dumps(data)
    monkeypatch.setattr(
        router.httpx,
        "patch",
        lambda *args, **kwargs: pytest.fail("stale connection must not write"),
    )
    with pytest.raises(click.ClickException, match="connection changed"):
        harness.save_routing("pi", {"allow_flex_tier_default": True}, "stale")


def test_withheld_routing_change_is_not_reported_as_success(installed, monkeypatch):
    monkeypatch.setattr(
        router,
        "_strategy_settings_request",
        lambda *args, **kwargs: {
            "allow_flex_tier_default": False,
            "switchyard_routing_enabled": False,
        },
    )
    token = harness.editor("pi")["connection_token"]
    with pytest.raises(click.ClickException):
        harness.save_routing("pi", {"allow_flex_tier_default": True}, token)


def test_connection_changed_during_discovery_is_not_restored_by_model_edit(
    installed, monkeypatch
):
    state = {"key": "original"}
    monkeypatch.setattr(router, "_stored_router_api_key", lambda *args: state["key"])

    def discover(*args, **kwargs):
        state["key"] = "replacement"
        return [SimpleNamespace(id="new", metadata=SimpleNamespace(display_name="New"))]

    monkeypatch.setattr(router, "_fetch_models", discover)
    with pytest.raises(click.ClickException, match="connection changed"):
        harness.set_model("pi", "new")
    assert not installed


@pytest.mark.parametrize("accepted", [False, True])
def test_switchyard_custom_configuration_must_be_confirmed(
    installed, monkeypatch, accepted
):
    desired = {"efficient_model": "new", "effort": "high"}
    monkeypatch.setattr(
        router,
        "_strategy_settings_request",
        lambda *args, **kwargs: {
            "allow_flex_tier_default": False,
            "switchyard_routing_enabled": True,
            "switchyard": {"config": desired if accepted else {}},
        },
    )
    token = harness.editor("pi")["connection_token"]
    changes = {"switchyard_routing_enabled": True, "switchyard_config": desired}
    if accepted:
        assert (
            harness.save_routing("pi", changes, token)["switchyard"]["config"]
            == desired
        )
    else:
        with pytest.raises(click.ClickException, match="did not confirm"):
            harness.save_routing("pi", changes, token)
