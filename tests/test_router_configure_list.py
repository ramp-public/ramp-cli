"""`router configure list|models` report harness state without side effects."""

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from ramp_cli.commands import router
from ramp_cli.main import cli
from ramp_cli.router_ui import harness

SECRET = "sk-router-0123456789-abcdef"
MASKED = "sk-r…cdef"
BASE_URL = "https://api.example/v1"


@pytest.fixture
def machine(monkeypatch):
    """Claude Code and Codex connected, OpenCode installed only, no network."""
    state = {
        "installed": {"claude-code", "opencode"},
        "codex_app": True,
        "connected": ("claude-code", "codex"),
        "models": {"claude-code": "claude-opus", "codex": "gpt-5"},
        "provider": router.ROUTER_PROVIDER,
        "fetched": [],
    }

    monkeypatch.setattr(router, "_installed_clients", lambda: tuple(state["installed"]))
    monkeypatch.setattr(router, "_codex_app_installed", lambda: state["codex_app"])
    monkeypatch.setattr(
        router, "conductor", SimpleNamespace(is_installed=lambda: False)
    )
    monkeypatch.setattr(router, "configured_router_clients", lambda: state["connected"])
    monkeypatch.setattr(router, "_client_config_path", lambda c: Path(f"/x/{c}.json"))
    monkeypatch.setattr(router, "_stored_router_base_url", lambda c, p: BASE_URL)
    monkeypatch.setattr(router, "_stored_router_api_key", lambda c, p: SECRET)
    monkeypatch.setattr(
        router, "_configured_model", lambda c, p: state["models"].get(c)
    )
    monkeypatch.setattr(
        router,
        "_read_codex_config",
        lambda p: ({}, {"model_provider": state["provider"]}),
    )

    @contextmanager
    def connection(client):
        if client not in state["connected"]:
            raise router.click.UsageError("Connect this harness to Router first.")
        yield harness.Connection(client, Path(f"/x/{client}.json"), SECRET, BASE_URL)

    def fetch_models(key, *, claude_code_view=False, model_view=None, base_url):
        assert (key, base_url) == (SECRET, BASE_URL)
        state["fetched"].append("models")
        return [
            SimpleNamespace(
                id="claude-sonnet",
                created=1,
                metadata=SimpleNamespace(display_name="Claude Sonnet"),
            ),
            SimpleNamespace(
                id="claude-opus",
                created=2,
                metadata=SimpleNamespace(display_name="Claude Opus"),
            ),
        ]

    def fetch_codex(key, *, base_url):
        assert (key, base_url) == (SECRET, BASE_URL)
        state["fetched"].append("codex")
        return {
            "models": [
                {"slug": "gpt-5", "display_name": "GPT-5"},
                {"slug": "gpt-5-mini"},
            ]
        }

    monkeypatch.setattr(harness, "connection", connection)
    monkeypatch.setattr(router, "_fetch_models", fetch_models)
    monkeypatch.setattr(router, "_fetch_codex_catalog", fetch_codex)
    return state


def agent(*args):
    return CliRunner().invoke(cli, ["--agent", "router", "configure", *args])


def human(*args):
    return CliRunner().invoke(cli, ["--human", "router", "configure", *args])


def rows(result):
    assert result.exit_code == 0, result.output
    return {r["harness"]: r for r in json.loads(result.output)["data"]}


def test_list_reports_every_supported_harness(machine):
    result = rows(agent("list"))
    assert list(result) == list(router.CLIENT_NAMES)
    assert result["claude-code"]["installed"] is True
    assert result["claude-code"]["connected"] is True
    assert result["claude-code"]["model"] == "claude-opus"
    assert result["claude-code"]["endpoint"] == BASE_URL
    assert result["opencode"]["installed"] is True
    assert result["opencode"]["connected"] is False
    assert result["opencode"]["model"] is None
    assert result["pi"]["installed"] is False
    assert result["pi"]["endpoint"] is None and result["pi"]["api_key"] is None


def test_list_detects_the_codex_app_as_installed(machine):
    assert rows(agent("list"))["codex"]["installed"] is True
    machine["codex_app"] = False
    assert rows(agent("list"))["codex"]["installed"] is False


def test_list_never_prints_the_full_key(machine):
    for result in (
        agent("list"),
        human("list"),
        CliRunner().invoke(cli, ["--output", "json", "router", "configure", "list"]),
    ):
        assert result.exit_code == 0, result.output
        assert SECRET not in result.output
        # JSON output escapes the ellipsis, so accept either spelling.
        assert MASKED in result.output or json.dumps(MASKED)[1:-1] in result.output


def test_list_masks_short_keys_completely(machine, monkeypatch):
    monkeypatch.setattr(router, "_stored_router_api_key", lambda c, p: "short")
    assert rows(agent("list"))["claude-code"]["api_key"] == "…"


def test_list_agent_json_shape(machine):
    envelope = json.loads(agent("list").output)
    assert envelope["schema_version"] == "1.0"
    assert envelope["pagination"] is None
    assert set(envelope["data"][0]) == {
        "harness",
        "name",
        "installed",
        "connected",
        "model",
        "endpoint",
        "api_key",
        "model_managed_by_app",
        "error",
    }


def test_list_human_table(machine):
    result = human("list")
    assert result.exit_code == 0, result.output
    header, *body = result.output.splitlines()
    assert header.split() == [
        "HARNESS",
        "INSTALLED",
        "CONNECTED",
        "MODEL",
        "ENDPOINT",
        "KEY",
    ]
    claude = next(line for line in body if line.startswith("claude-code"))
    assert claude.split() == [
        "claude-code",
        "yes",
        "yes",
        "claude-opus",
        BASE_URL,
        MASKED,
    ]
    pi = next(line for line in body if line.startswith("pi "))
    assert pi.split() == ["pi", "no", "no", "-", "-", "-"]


def test_list_codex_on_another_provider_is_not_connected(machine):
    machine["provider"] = "openai"
    codex = rows(agent("list"))["codex"]
    assert codex["connected"] is False
    assert codex["model"] is None and codex["api_key"] is None


def test_list_reports_unreadable_configuration_without_failing(machine, monkeypatch):
    def broken(client, path):
        raise router.click.ClickException("unreadable")

    monkeypatch.setattr(router, "_configured_model", broken)
    result = rows(agent("list"))
    assert result["claude-code"]["connected"] is True
    assert result["claude-code"]["error"]
    assert result["claude-code"]["api_key"] is None
    assert result["opencode"]["error"] is None

    table = human("list").output.splitlines()
    claude = next(line for line in table if line.startswith("claude-code"))
    assert claude.split() == ["claude-code", "yes", "yes"] + ["(unreadable)"] * 3


def test_list_makes_no_network_calls(machine):
    rows(agent("list"))
    assert machine["fetched"] == []


def test_models_for_a_non_codex_harness(machine):
    result = agent("models", "claude-code")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"][0]
    assert data["harness"] == "claude-code"
    assert data["default_model"] == "claude-opus"
    # Newest release first, the same order the model picker uses.
    assert data["models"] == [
        {"id": "claude-opus", "name": "Claude Opus", "is_default": True},
        {"id": "claude-sonnet", "name": "Claude Sonnet", "is_default": False},
    ]
    assert SECRET not in result.output


def test_models_for_codex_use_catalog_slugs(machine):
    data = json.loads(agent("models", "codex").output)["data"][0]
    assert data["default_model"] == "gpt-5"
    assert [m["id"] for m in data["models"]] == ["gpt-5", "gpt-5-mini"]
    assert data["models"][1]["name"] == "gpt-5-mini"
    assert [m["is_default"] for m in data["models"]] == [True, False]
    assert machine["fetched"] == ["models", "codex"]


def test_models_ids_are_accepted_by_configure_model(machine, monkeypatch):
    saved = []

    def configure_client(client, key, models, **options):
        saved.append(options["selected_model"])
        return None, options["selected_model"], None

    monkeypatch.setattr(router, "_configure_client", configure_client)
    monkeypatch.setattr(router, "_stored_usage_origin", lambda c, p: None)
    for client in ("claude-code", "codex"):
        listed = json.loads(agent("models", client).output)["data"][0]["models"]
        target = listed[-1]["id"]
        result = agent("model", client, "--model", target)
        assert result.exit_code == 0, result.output
        assert saved[-1] == target


def test_models_human_table(machine):
    result = human("models", "claude-code")
    assert result.exit_code == 0, result.output
    header, *body = result.output.splitlines()
    assert header.split() == ["MODEL", "NAME", "DEFAULT"]
    assert body[0].split() == ["claude-opus", "Claude", "Opus", "yes"]


def test_models_for_an_unconnected_harness_is_a_clear_error(machine):
    result = agent("models", "opencode")
    assert result.exit_code == 2
    assert "OpenCode isn't connected to Router" in result.output
    assert "ramp router configure" in result.output
    assert machine["fetched"] == []


def test_models_for_an_app_managed_harness_explains_itself(machine):
    machine["connected"] = ("conductor",)
    result = agent("models", "conductor")
    assert result.exit_code == 2
    assert "inside its app" in result.output


def test_models_rejects_unknown_harnesses(machine):
    result = agent("models", "not-a-harness")
    assert result.exit_code == 2
    assert "claude-code" in result.output
