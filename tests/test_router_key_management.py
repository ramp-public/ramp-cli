"""Selected-key management uses owner endpoints, never shared strategy edits."""

import json

import pytest
from click.testing import CliRunner

from ramp_cli.client.router_user import RouterUserClient
from ramp_cli.commands import router_user
from ramp_cli.errors import ApiError
from ramp_cli.main import cli


@pytest.fixture
def management(monkeypatch):
    key = {"id": "one", "name": "Coding", "enabled": True}
    profiles = [
        {
            "id": "default",
            "name": "Account default",
            "is_default": True,
            "assigned_api_key_ids": ["one", "two"],
        },
        {"id": "cheap", "name": "Cheap", "assigned_api_key_ids": ["three"]},
    ]
    writes = []

    def get(self, path, **kwargs):
        if path == "/client/api-keys":
            return {"data": [dict(key)], "has_more": False}
        if path == "/client/routing-profiles":
            return {"data": profiles}
        if path == "/admin/me/experiment-settings":
            return {}
        if path == "/client/usage/dashboard":
            return {"summary": {}}
        raise AssertionError(path)

    def patch(self, path, *, json, **kwargs):
        writes.append(("PATCH", path, json))
        key.update(json)
        return dict(key)

    def post(self, path, *, json, **kwargs):
        writes.append(("POST", path, json))
        if path.endswith("/assignments"):
            target = next(p for p in profiles if p["id"] == path.split("/")[-2])
            for profile in profiles:
                profile["assigned_api_key_ids"] = [
                    item for item in profile["assigned_api_key_ids"] if item != "one"
                ]
            target["assigned_api_key_ids"].append("one")
            return dict(target)
        key["enabled"] = path.endswith("/unlock")
        return dict(key)

    monkeypatch.setattr(RouterUserClient, "get", get)
    monkeypatch.setattr(RouterUserClient, "patch", patch)
    monkeypatch.setattr(RouterUserClient, "post", post)
    return key, profiles, writes


@pytest.mark.parametrize(
    "args,expected",
    [
        (
            ["rename", "one", "--name", " New name "],
            ("PATCH", "/client/api-keys/one", {"name": "New name"}),
        ),
        (["lock", "one"], ("POST", "/client/api-keys/one/lock", {})),
        (["unlock", "one"], ("POST", "/client/api-keys/one/unlock", {})),
        (
            ["set-strategy", "one", "--routing-profile", "cHeAp"],
            (
                "POST",
                "/client/routing-profiles/cheap/assignments",
                {"api_key_ids": ["one"]},
            ),
        ),
        (
            ["set-strategy", "one", "--routing-profile", "default"],
            (
                "POST",
                "/client/routing-profiles/default/assignments",
                {"api_key_ids": ["one"]},
            ),
        ),
    ],
)
def test_key_commands_and_dry_run(management, args, expected):
    _, _, writes = management
    runner = CliRunner()
    dry = runner.invoke(cli, ["--agent", "router", "keys", *args, "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.output)["data"][0] == dict(
        zip(("method", "path", "body"), expected)
    )
    assert writes == []
    result = runner.invoke(cli, ["--agent", "router", "keys", *args])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["schema_version"] == "1.0"
    assert writes == [expected]


@pytest.mark.parametrize(
    "args",
    [
        ["rename", "one", "--name", " "],
        ["rename", "one", "--name", "x" * 33],
        ["rename", "missing", "--name", "Valid"],
        ["lock", "missing"],
        ["unlock", "missing"],
        ["lock", "../one"],
        ["set-strategy", "one", "--routing-profile", "missing"],
    ],
)
def test_invalid_management_inputs_do_not_write(management, args):
    result = CliRunner().invoke(cli, ["--agent", "router", "keys", *args])
    assert result.exit_code != 0
    assert management[2] == []


def test_strategy_assignment_preserves_other_keys(management):
    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "router",
            "keys",
            "set-strategy",
            "one",
            "--routing-profile",
            "Cheap",
        ],
    )
    assert result.exit_code == 0, result.output
    assert management[1][0]["assigned_api_key_ids"] == ["two"]
    assert management[1][1]["assigned_api_key_ids"] == ["three", "one"]


def test_unavailable_profiles_do_not_write(management, monkeypatch):
    monkeypatch.setattr(router_user, "_routing_profiles", lambda _client: None)
    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "router",
            "keys",
            "set-strategy",
            "one",
            "--routing-profile",
            "Cheap",
        ],
    )
    assert result.exit_code != 0
    assert "aren't available" in result.output
    assert management[2] == []


def test_unlock_server_restrictions_are_not_bypassed(management, monkeypatch):
    management[0]["enabled"] = False

    def denied(self, path, **kwargs):
        raise ApiError(409, '{"error":{"message":"Suspended by workspace admin"}}')

    monkeypatch.setattr(RouterUserClient, "post", denied)
    result = CliRunner().invoke(cli, ["--agent", "router", "keys", "unlock", "one"])
    assert result.exit_code != 0
    assert management[0]["enabled"] is False
    assert management[2] == []


def test_root_flags_work_after_key_command(management):
    result = CliRunner().invoke(
        cli, ["router", "keys", "lock", "one", "--agent", "--no-input", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"][0]["path"] == "/client/api-keys/one/lock"
    assert management[2] == []


def test_human_rename_output(management):
    result = CliRunner().invoke(
        cli, ["--human", "router", "keys", "rename", "one", "--name", "New name"]
    )
    assert result.exit_code == 0, result.output
    assert "Renamed API key to New name." in result.output


def _prompts(monkeypatch, answers):
    answers = iter(answers)
    prompts = []

    def prompt(message, **kwargs):
        prompts.append((message, kwargs))

        class Question:
            def ask(self):
                return next(answers)

        return Question()

    monkeypatch.setattr(router_user, "_interactive_keys", lambda _ctx: True)
    for kind in ("select", "text", "confirm"):
        monkeypatch.setattr(router_user.questionary, kind, prompt)
    return prompts


def test_picker_manages_and_refreshes_selected_key(management, monkeypatch):
    prompts = _prompts(
        monkeypatch,
        [
            "one",
            router_user._PICKER_RENAME,
            "Renamed",
            router_user._PICKER_STRATEGY,
            "cheap",
            router_user._PICKER_LOCK,
            True,
            router_user._PICKER_UNLOCK,
            True,
            router_user._PICKER_BACK,
            router_user._PICKER_BACK,
        ],
    )
    result = CliRunner().invoke(cli, ["--human", "router", "keys"])
    assert result.exit_code == 0, result.output
    assert len(management[2]) == 4
    menus = [kwargs for message, kwargs in prompts if message == "What next?"]
    assert router_user._PICKER_UNLOCK in [c.value for c in menus[3]["choices"]]
    assert router_user._PICKER_LOCK in [c.value for c in menus[4]["choices"]]
    last_keys = [
        kwargs for message, kwargs in prompts if message.startswith("Select an API key")
    ][-1]
    assert "Renamed" in last_keys["choices"][0].title
    assert "Profile: Cheap" in result.output


@pytest.mark.parametrize(
    "action,answer",
    [
        (router_user._PICKER_RENAME, None),
        (router_user._PICKER_STRATEGY, None),
        (router_user._PICKER_LOCK, False),
        (router_user._PICKER_LOCK, None),
    ],
)
def test_picker_cancellation_does_not_write(management, monkeypatch, action, answer):
    _prompts(monkeypatch, ["one", action, answer, router_user._PICKER_DONE])
    result = CliRunner().invoke(cli, ["--human", "router", "keys"])
    assert result.exit_code == 0, result.output
    assert management[2] == []
