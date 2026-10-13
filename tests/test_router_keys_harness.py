"""`router keys strategies|set-strategy --harness` act on a harness's key."""

import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner

from ramp_cli.client.router_user import RouterUserClient
from ramp_cli.commands import router
from ramp_cli.main import cli
from ramp_cli.router_ui import harness

SECRET = "sk-harness-secret"
BASE_URL = "https://api.example/v1"


def profile(name, *, current=False, default=False, **flags):
    return {
        "id": f"id-{name.lower()}",
        "name": name,
        "is_default": default,
        "is_current": current,
        **flags,
    }


@pytest.fixture
def harness_env(monkeypatch):
    """A connected Claude Code harness and a key-authed Router, with no network."""
    state = {
        "profiles": [
            profile("Default", default=True),
            profile(
                "Cheap",
                current=True,
                cost_efficient_routing_enabled=True,
                switchyard_routing_enabled=True,
            ),
        ],
        "selected": [],
        "connections": [],
    }

    @contextmanager
    def connection(client):
        state["connections"].append(client)
        if client != "claude-code":
            raise router.click.UsageError("Connect this harness to Router first.")
        yield harness.Connection(client, Path("settings.json"), SECRET, BASE_URL)

    def key_profiles(api_key, *, base_url=None):
        assert (api_key, base_url) == (SECRET, BASE_URL)
        return state["profiles"]

    def select(api_key, name, *, base_url=None):
        assert (api_key, base_url) == (SECRET, BASE_URL)
        state["selected"].append(name)
        return {"routing_profile": {"name": name}}

    def no_login(*_args, **_kwargs):
        raise AssertionError("--harness must not use the Router web login")

    monkeypatch.setattr(harness, "connection", connection)
    monkeypatch.setattr(router, "_key_routing_profiles", key_profiles)
    monkeypatch.setattr(router, "_select_key_routing_profile", select)
    monkeypatch.setattr(RouterUserClient, "get", no_login)
    monkeypatch.setattr(RouterUserClient, "post", no_login)
    return state


def run(*args):
    return CliRunner().invoke(cli, ["--agent", "router", "keys", *args])


def run_human(*args):
    return CliRunner().invoke(cli, ["--human", "router", "keys", *args])


def payload(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"][0]


@pytest.mark.parametrize("command", ["strategies", "set-strategy"])
def test_requires_exactly_one_of_key_id_or_harness(command, harness_env):
    extra = ["--routing-profile", "Cheap"] if command == "set-strategy" else []
    neither = run(command, *extra)
    both = run(command, "key-1", "--harness", "claude-code", *extra)
    for result in (neither, both):
        assert result.exit_code == 2
        assert "exactly one of KEY_ID or --harness" in result.output
    assert harness_env["connections"] == []


def test_harness_choices_match_known_agents(harness_env):
    result = run("strategies", "--harness", "not-a-harness")
    assert result.exit_code == 2
    assert "claude-code" in result.output and "desktop" in result.output


def test_strategies_harness_reads_the_harness_key(harness_env):
    data = payload(run("strategies", "--harness", "claude-code"))
    assert data["harness"] == "claude-code"
    assert data["source"] == "routing_profile"
    assert data["routing_profile"] == {
        "id": "id-cheap",
        "name": "Cheap",
        "is_default": False,
    }
    assert data["allow_flex_tier_default"] is True
    assert data["switchyard_routing_enabled"] is True
    assert [(p["name"], p["is_current"]) for p in data["routing_profiles"]] == [
        ("Default", False),
        ("Cheap", True),
    ]
    assert SECRET not in json.dumps(data)


def test_strategies_harness_falls_back_to_account_defaults(harness_env, monkeypatch):
    harness_env["profiles"] = [profile("Default", default=True)]
    monkeypatch.setattr(
        router,
        "_strategy_settings_request",
        lambda key, *, base_url=None: {
            "allow_flex_tier_default": True,
            "switchyard_routing_enabled": False,
        },
    )
    data = payload(run("strategies", "--harness", "claude-code"))
    assert data["source"] == "account_defaults"
    assert data["routing_profile"] is None
    assert data["allow_flex_tier_default"] is True


def test_unconnected_harness_is_a_clear_error(harness_env):
    result = run("strategies", "--harness", "codex")
    assert result.exit_code == 2
    assert "Codex isn't connected to Router" in result.output
    assert "ramp router configure" in result.output


def test_unconnected_harness_with_real_connection_lookup(monkeypatch):
    monkeypatch.setattr(router, "configured_router_clients", lambda: ())
    result = run("strategies", "--harness", "claude-code")
    assert result.exit_code == 2
    assert "Claude Code isn't connected to Router" in result.output


def test_set_strategy_harness_selects_profile_by_name_or_id(harness_env):
    payload(
        run(
            "set-strategy",
            "--harness",
            "claude-code",
            "--from",
            "Cheap",
            "--to",
            "Default",
        )
    )
    harness_env["profiles"] = [
        profile("Default", default=True),
        profile("Cheap", current=True),
    ]
    payload(
        run(
            "set-strategy",
            "--harness",
            "claude-code",
            "--from",
            "Cheap",
            "--to",
            "id-default",
        )
    )
    assert harness_env["selected"] == ["Default", "Default"]


def test_harness_requires_from(harness_env):
    for extra in ((), ("--dry-run",)):
        result = run(
            "set-strategy", "--harness", "claude-code", "--to", "Default", *extra
        )
        assert result.exit_code == 2
        assert "With --harness, --from is required" in result.output
        assert "stale view" in result.output
    assert harness_env["selected"] == []
    assert harness_env["connections"] == []


def test_set_strategy_harness_dry_run_sends_nothing(harness_env):
    # test_dry_run_shows_the_transition covers the transition itself.
    payload(
        run(
            "set-strategy",
            "--harness",
            "claude-code",
            "--from",
            "Cheap",
            "--to",
            "Default",
            "--dry-run",
        )
    )
    assert harness_env["selected"] == []


def test_dry_run_shows_the_transition(harness_env):
    data = payload(
        run(
            "set-strategy",
            "--harness",
            "claude-code",
            "--from",
            "id-cheap",
            "--to",
            "Default",
            "--dry-run",
        )
    )
    assert data == {
        "method": "PUT",
        "path": "/session-usage/routing-profile",
        "body": {"name": "Default"},
        "transition": {"from": "Cheap", "to": "Default"},
    }


def test_set_strategy_harness_unknown_profile(harness_env):
    result = run(
        "set-strategy", "--harness", "claude-code", "--from", "Cheap", "--to", "Missing"
    )
    assert result.exit_code == 2
    assert "No routing profile named 'Missing'" in result.output
    assert harness_env["selected"] == []


def test_from_mismatch_changes_nothing_and_exits_1(harness_env):
    result = run(
        "set-strategy", "--harness", "claude-code", "--from", "Default", "--to", "Cheap"
    )
    assert result.exit_code == 1
    assert "This key now uses 'Cheap', not 'Default'. Nothing was changed." in (
        result.output
    )
    assert harness_env["selected"] == []


def test_from_matches_by_name_or_id(harness_env):
    for current in ("cheap", "id-cheap"):
        payload(
            run(
                "set-strategy",
                "--harness",
                "claude-code",
                "--from",
                current,
                "--to",
                "Default",
            )
        )
    assert harness_env["selected"] == ["Default", "Default"]


def test_from_none_means_account_defaults(harness_env):
    args = (
        "set-strategy",
        "--harness",
        "claude-code",
        "--from",
        "none",
        "--to",
        "Default",
    )
    result = run(*args)
    assert result.exit_code == 1
    assert "not 'none'" in result.output
    harness_env["profiles"] = [profile("Default", default=True)]
    assert run(*args).exit_code == 0
    assert harness_env["selected"] == ["Default"]


def test_harness_human_success_names_both_strategies(harness_env):
    result = run_human(
        "set-strategy",
        "--harness",
        "claude-code",
        "--from",
        "Cheap",
        "--to",
        "Default",
    )
    assert result.exit_code == 0, result.output
    assert "Routing strategy changed from 'Cheap' to 'Default'." in result.output


def test_harness_choices_cover_every_client_that_holds_a_key():
    option = next(
        param
        for param in cli.commands["router"]
        .commands["keys"]
        .commands["strategies"]
        .params
        if param.name == "harness_client"
    )
    assert set(option.type.choices) == {
        router._command_name(name) for name in router.AGENT_NAMES
    }


# The KEY_ID path keeps its requests, output, and validation.


@pytest.fixture
def key_id_env(monkeypatch):
    key = {"id": "one", "name": "Coding", "enabled": True}
    profiles = [
        {
            "id": "default",
            "name": "Account default",
            "is_default": True,
            "assigned_api_key_ids": ["one"],
        },
        {"id": "cheap", "name": "Cheap", "assigned_api_key_ids": []},
    ]
    writes = []

    def get(self, path, **kwargs):
        if path == "/client/api-keys":
            return {"data": [key], "has_more": False}
        if path == "/client/routing-profiles":
            return {"data": profiles}
        if path == "/admin/me/experiment-settings":
            return {}
        raise AssertionError(path)

    def post(self, path, *, json, **kwargs):
        writes.append((path, json))
        return {"ok": True}

    def no_harness(*_args, **_kwargs):
        raise AssertionError("KEY_ID must not read harness connections")

    monkeypatch.setattr(RouterUserClient, "get", get)
    monkeypatch.setattr(RouterUserClient, "post", post)
    monkeypatch.setattr(harness, "connection", no_harness)
    return writes


def test_key_id_strategies_output_is_unchanged(key_id_env):
    data = payload(run("strategies", "one"))
    assert data == {
        "source": "routing_profile",
        "routing_profile": {
            "id": "default",
            "name": "Account default",
            "is_default": True,
        },
        "allow_flex_tier_default": False,
        "switchyard_routing_enabled": False,
        "auto_routing_enabled": False,
        "shadow_models_enabled": None,
    }


def test_key_id_set_strategy_dry_run_is_unchanged(key_id_env):
    data = payload(
        run("set-strategy", "one", "--routing-profile", "Cheap", "--dry-run")
    )
    assert data == {
        "method": "POST",
        "path": "/client/routing-profiles/cheap/assignments",
        "body": {"api_key_ids": ["one"]},
    }
    assert key_id_env == []


def test_from_is_optional_with_key_id(key_id_env):
    payload(run("set-strategy", "one", "--to", "Cheap"))
    assert key_id_env == [
        ("/client/routing-profiles/cheap/assignments", {"api_key_ids": ["one"]})
    ]


def test_routing_profile_alias_still_works(key_id_env):
    data = payload(run("set-strategy", "one", "--routing-profile", "Cheap"))
    assert data == {"ok": True}
    assert key_id_env == [
        ("/client/routing-profiles/cheap/assignments", {"api_key_ids": ["one"]})
    ]


def test_to_and_routing_profile_are_the_same_option():
    command = cli.commands["router"].commands["keys"].commands["set-strategy"]
    option = next(p for p in command.params if p.name == "routing_profile")
    assert set(option.opts) == {"--to", "--routing-profile"}
    assert option.required
    assert "expect_routing_profile" not in {p.name for p in command.params}


def test_key_id_from_mismatch_changes_nothing(key_id_env):
    result = run("set-strategy", "one", "--from", "Cheap", "--to", "Cheap")
    assert result.exit_code == 1
    assert (
        "This key now uses 'Account default', not 'Cheap'. Nothing was changed."
        in result.output
    )
    assert key_id_env == []


def test_key_id_from_transition(key_id_env):
    for current in ("default", "Account default"):
        result = run_human("set-strategy", "one", "--from", current, "--to", "Cheap")
        assert result.exit_code == 0, result.output
        assert (
            "Routing strategy changed from 'Account default' to 'Cheap'."
            in result.output
        )
    dry = payload(
        run("set-strategy", "one", "--from", "default", "--to", "cheap", "--dry-run")
    )
    assert dry["transition"] == {"from": "Account default", "to": "Cheap"}
    assert len(key_id_env) == 2


def test_key_id_from_none_means_account_defaults(key_id_env):
    result = run("set-strategy", "one", "--from", "none", "--to", "Cheap")
    assert result.exit_code == 1
    assert key_id_env == []


def test_key_id_show_still_reports_key_and_strategies(key_id_env, monkeypatch):
    original = RouterUserClient.get

    def get(self, path, **kwargs):
        if path == "/client/usage/dashboard":
            return {"summary": {}}
        return original(self, path, **kwargs)

    monkeypatch.setattr(RouterUserClient, "get", get)
    data = payload(run("show", "one"))
    assert data["key"]["id"] == "one"
    assert data["strategies"]["routing_profile"]["id"] == "default"
    assert "harness" not in data
