"""Account-level `ramp router strategies` over the web's routing-profile endpoints."""

import copy
import json

import httpx
import pytest
from click.testing import CliRunner

from ramp_cli.client.router_user import RouterUserClient
from ramp_cli.commands import router_user_strategies as router_profiles
from ramp_cli.commands.router_user_strategies import ProfileDraft, save_draft
from ramp_cli.errors import ApiError, RampCLIError
from ramp_cli.main import cli

DEFAULT = "11111111-1111-1111-1111-111111111111"
CHEAP = "22222222-2222-2222-2222-222222222222"


def _profiles():
    return [
        {
            "id": DEFAULT,
            "name": "Account default",
            "is_default": True,
            "cost_efficient_routing_enabled": False,
            "switchyard_routing_enabled": False,
            "auto_routing_enabled": None,
            "assigned_api_key_ids": ["k1", "k2"],
        },
        {
            "id": CHEAP,
            "name": "Cheap",
            "is_default": False,
            "cost_efficient_routing_enabled": True,
            "switchyard_routing_enabled": False,
            "auto_routing_enabled": True,
            "assigned_api_key_ids": ["k3"],
        },
    ]


KEYS = [
    {"id": "k1", "name": "Coding", "enabled": True},
    {"id": "k2", "name": "Ops", "enabled": True},
    {"id": "k3", "name": "Batch", "enabled": False},
]


@pytest.fixture
def router(monkeypatch):
    """Record every Router call; serve profiles and keys from memory."""
    state = {"profiles": _profiles(), "calls": [], "unavailable": False}

    def find(profile_id):
        return next(p for p in state["profiles"] if p["id"] == profile_id)

    def get(self, path, **kwargs):
        state["calls"].append(("GET", path, None))
        if path == "/client/routing-profiles":
            if state["unavailable"]:
                raise ApiError(403, '{"error":{"code":"routing_profiles_unavailable"}}')
            return {"data": copy.deepcopy(state["profiles"])}
        if path == "/client/api-keys":
            return {"data": KEYS, "has_more": False}
        if path.startswith("/client/routing-profiles/"):
            return copy.deepcopy(find(path.rsplit("/", 1)[1]))
        raise AssertionError(path)

    def post(self, path, *, json, **kwargs):
        state["calls"].append(("POST", path, json))
        if path == "/client/routing-profiles":
            created = {
                "id": "33333333-3333-3333-3333-333333333333",
                "is_default": False,
                "cost_efficient_routing_enabled": False,
                "switchyard_routing_enabled": False,
                "auto_routing_enabled": None,
                "assigned_api_key_ids": [],
                **json,
            }
            state["profiles"].append(created)
            return copy.deepcopy(created)
        profile_id = path.split("/")[3]
        for profile in state["profiles"]:
            profile["assigned_api_key_ids"] = [
                key
                for key in profile["assigned_api_key_ids"]
                if key not in json["api_key_ids"]
            ]
        find(profile_id)["assigned_api_key_ids"] += json["api_key_ids"]
        return copy.deepcopy(find(profile_id))

    def patch(self, path, *, json, **kwargs):
        state["calls"].append(("PATCH", path, json))
        profile = find(path.rsplit("/", 1)[1])
        profile.update(json)
        return copy.deepcopy(profile)

    def delete(self, path, *, params=None, **kwargs):
        state["calls"].append(("DELETE", path, params))
        return {}

    monkeypatch.setattr(
        RouterUserClient,
        "status",
        lambda self: {"authenticated": True, "origin": "http://localhost:3080"},
    )
    for name, method in (
        ("get", get),
        ("post", post),
        ("patch", patch),
        ("delete", delete),
    ):
        monkeypatch.setattr(RouterUserClient, name, method)
    return state


def writes(state):
    return [call for call in state["calls"] if call[0] != "GET"]


def test_draft_tracks_changes_and_keeps_switchyard_and_auto_routing_exclusive():
    draft = ProfileDraft.from_profile(_profiles()[1])
    assert draft.changes() == [] and draft.patch_body() == {}
    draft.set_setting("switchyard_routing_enabled", True)
    assert draft.settings["auto_routing_enabled"] is False
    draft.keys.add("k1")
    assert draft.patch_body() == {
        "switchyard_routing_enabled": True,
        "auto_routing_enabled": False,
    }
    assert draft.changes() == ["NVIDIA NeMo Switchyard", "Jev Auto Routing", "API keys"]


@pytest.mark.parametrize(
    "desired,server_keys,expected",
    [
        ({"k3"}, {"k2", "k3"}, {"k2", "k3"}),
        ({"k3"}, set(), set()),
        ({"k1"}, {"k2", "k3"}, {"k1", "k2"}),
        ({"k1", "k3"}, {"k1", "k2", "k3"}, {"k1", "k2", "k3"}),
        (set(), {"k2"}, {"k2"}),
    ],
    ids=[
        "untouched-addition",
        "untouched-removal",
        "local-add-and-remove",
        "addition-already-applied",
        "removal-already-applied",
    ],
)
def test_rebase_merges_only_local_key_edits(desired, server_keys, expected):
    profile = _profiles()[1]
    draft = ProfileDraft.from_profile(profile)
    draft.keys = desired
    draft.rebase({**profile, "assigned_api_key_ids": list(server_keys)})
    assert draft.original_keys == server_keys
    assert draft.keys == expected
    assert draft.added_keys == expected - server_keys
    assert draft.removed_keys == server_keys - expected


def test_repeated_rebase_does_not_turn_remote_key_changes_into_local_edits():
    profile = _profiles()[1]
    draft = ProfileDraft.from_profile(profile)
    draft.keys.add("k1")
    draft.rebase({**profile, "assigned_api_key_ids": ["k2", "k3"]})
    draft.rebase({**profile, "assigned_api_key_ids": ["k3"]})
    assert draft.keys == {"k1", "k3"}
    assert draft.added_keys == {"k1"}
    assert draft.removed_keys == set()


def test_save_sends_settings_then_key_moves_and_returns_fresh_profile(router):
    draft = ProfileDraft.from_profile(_profiles()[1])
    draft.set_setting("cost_efficient_routing_enabled", False)
    draft.keys = {"k1"}  # add k1, remove k3 (back to default)
    saved = save_draft(RouterUserClient.__new__(RouterUserClient), draft, DEFAULT)
    assert writes(router) == [
        (
            "PATCH",
            f"/client/routing-profiles/{CHEAP}",
            {"cost_efficient_routing_enabled": False},
        ),
        (
            "POST",
            f"/client/routing-profiles/{CHEAP}/assignments",
            {"api_key_ids": ["k1"]},
        ),
        (
            "POST",
            f"/client/routing-profiles/{DEFAULT}/assignments",
            {"api_key_ids": ["k3"]},
        ),
    ]
    assert saved["assigned_api_key_ids"] == ["k1"]


def test_partial_save_rebases_settings_before_user_reverts(router, monkeypatch):
    client = RouterUserClient.__new__(RouterUserClient)
    draft = ProfileDraft.from_profile(_profiles()[1])
    draft.set_setting("cost_efficient_routing_enabled", False)
    draft.keys.add("k1")
    post = client.post

    def fail(*_a, **_k):
        raise ApiError(503, "assignment failed")

    monkeypatch.setattr(client, "post", fail)
    with pytest.raises(ApiError):
        save_draft(client, draft, DEFAULT)
    assert draft.original_settings["cost_efficient_routing_enabled"] is False
    assert draft.added_keys == {"k1"}
    draft.set_setting("cost_efficient_routing_enabled", True)
    monkeypatch.setattr(client, "post", post)
    saved = save_draft(client, draft, DEFAULT)
    assert saved["cost_efficient_routing_enabled"] is True
    assert [call[2] for call in writes(router) if call[0] == "PATCH"] == [
        {"cost_efficient_routing_enabled": False},
        {"cost_efficient_routing_enabled": True},
    ]
    assert draft.changes() == []


@pytest.mark.parametrize(
    "remote_profile,remote_key,desired,expected",
    [
        (CHEAP, "k2", {"k1", "k3"}, {"k1", "k2", "k3"}),
        (DEFAULT, "k3", {"k1", "k3"}, {"k1"}),
        (CHEAP, "k2", {"k1"}, {"k1", "k2"}),
    ],
    ids=["preserve-remote-add", "preserve-remote-remove", "preserve-local-remove"],
)
def test_failed_save_retry_preserves_concurrent_key_changes(
    router, monkeypatch, remote_profile, remote_key, desired, expected
):
    client = RouterUserClient.__new__(RouterUserClient)
    draft = ProfileDraft.from_profile(_profiles()[1])
    draft.set_setting("cost_efficient_routing_enabled", False)
    draft.keys = desired
    post = client.post

    def fail_after_concurrent_assignment(*_a, **_k):
        post(
            f"/client/routing-profiles/{remote_profile}/assignments",
            json={"api_key_ids": [remote_key]},
        )
        raise ApiError(503, "assignment failed")

    monkeypatch.setattr(client, "post", fail_after_concurrent_assignment)
    with pytest.raises(ApiError):
        save_draft(client, draft, DEFAULT)
    assert draft.keys == expected
    before_retry = len(writes(router))
    monkeypatch.setattr(client, "post", post)
    saved = save_draft(client, draft, DEFAULT)
    assert set(saved["assigned_api_key_ids"]) == expected
    expected_writes = [
        (
            "POST",
            f"/client/routing-profiles/{CHEAP}/assignments",
            {"api_key_ids": ["k1"]},
        )
    ]
    if "k3" not in desired:
        expected_writes.append(
            (
                "POST",
                f"/client/routing-profiles/{DEFAULT}/assignments",
                {"api_key_ids": ["k3"]},
            )
        )
    assert writes(router)[before_retry:] == expected_writes
    assert draft.changes() == []


def test_partial_create_retry_reuses_created_profile(router, monkeypatch):
    client = RouterUserClient.__new__(RouterUserClient)
    draft = ProfileDraft.new()
    draft.name = "New"
    draft.keys = {"k1"}
    post = client.post

    def fail_assignment(path, **kwargs):
        if path.endswith("/assignments"):
            raise ApiError(503, "assignment failed")
        return post(path, **kwargs)

    monkeypatch.setattr(client, "post", fail_assignment)
    with pytest.raises(ApiError):
        save_draft(client, draft, DEFAULT)
    created_id = draft.profile_id
    assert created_id is not None
    assert draft.original_name == "New"
    assert draft.added_keys == {"k1"}
    monkeypatch.setattr(client, "post", post)
    saved = save_draft(client, draft, DEFAULT)
    assert saved["id"] == created_id
    assert sum(call[1] == "/client/routing-profiles" for call in writes(router)) == 1
    assert draft.changes() == []


@pytest.mark.parametrize("applied_before_failure", [False, True])
def test_partial_assignment_batches_retry_only_remaining_keys(
    router, monkeypatch, applied_before_failure
):
    monkeypatch.setattr(router_profiles, "_ASSIGN_BATCH", 1)
    client = RouterUserClient.__new__(RouterUserClient)
    draft = ProfileDraft.from_profile(_profiles()[1])
    draft.keys.update({"k1", "k2"})
    post = client.post

    def fail_second_batch(path, **kwargs):
        if kwargs["json"]["api_key_ids"] == ["k2"]:
            if applied_before_failure:
                post(path, **kwargs)
            raise ApiError(503, "assignment failed")
        return post(path, **kwargs)

    monkeypatch.setattr(client, "post", fail_second_batch)
    with pytest.raises(ApiError):
        save_draft(client, draft, DEFAULT)
    assert draft.added_keys == (set() if applied_before_failure else {"k2"})
    monkeypatch.setattr(client, "post", post)
    saved = save_draft(client, draft, DEFAULT)
    assert set(saved["assigned_api_key_ids"]) == {"k1", "k2", "k3"}
    assert sum(call[2] == {"api_key_ids": ["k1"]} for call in writes(router)) == 1


def test_partial_save_stops_when_server_state_cannot_be_reloaded(router, monkeypatch):
    client = RouterUserClient.__new__(RouterUserClient)
    draft = ProfileDraft.from_profile(_profiles()[1])
    draft.set_setting("cost_efficient_routing_enabled", False)
    draft.keys.add("k1")

    def fail(*_a, **_k):
        raise ApiError(503, "unavailable")

    monkeypatch.setattr(client, "post", fail)
    monkeypatch.setattr(client, "get", fail)
    with pytest.raises(RampCLIError, match="Editing stopped"):
        save_draft(client, draft, DEFAULT)


def test_partial_removal_rebases_key_assignments(router, monkeypatch):
    client = RouterUserClient.__new__(RouterUserClient)
    # Exercise non-default removal using a profile with multiple assigned keys.
    profile = router["profiles"][1]
    profile["assigned_api_key_ids"] = ["k1", "k2", "k3"]
    router["profiles"][0]["assigned_api_key_ids"] = []
    draft = ProfileDraft.from_profile(profile)
    draft.keys = {"k3"}
    monkeypatch.setattr(router_profiles, "_ASSIGN_BATCH", 1)
    post = client.post

    def fail_second_removal(path, **kwargs):
        if kwargs["json"]["api_key_ids"] == ["k2"]:
            raise ApiError(503, "assignment failed")
        return post(path, **kwargs)

    monkeypatch.setattr(client, "post", fail_second_removal)
    with pytest.raises(ApiError):
        save_draft(client, draft, DEFAULT)
    assert draft.removed_keys == {"k2"}
    monkeypatch.setattr(client, "post", post)
    saved = save_draft(client, draft, DEFAULT)
    assert saved["assigned_api_key_ids"] == ["k3"]
    assert sum(call[2] == {"api_key_ids": ["k1"]} for call in writes(router)) == 1


def test_create_command_creates_strategy_and_moves_keys(router):
    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "router",
            "strategies",
            "create",
            "--name",
            "Fast",
            "--switchyard",
            "--key",
            "k2",
        ],
    )
    assert result.exit_code == 0, result.output
    assert writes(router) == [
        (
            "POST",
            "/client/routing-profiles",
            {
                "name": "Fast",
                "cost_efficient_routing_enabled": False,
                "switchyard_routing_enabled": True,
            },
        ),
        (
            "POST",
            "/client/routing-profiles/33333333-3333-3333-3333-333333333333/assignments",
            {"api_key_ids": ["k2"]},
        ),
    ]
    data = json.loads(result.output)["data"][0]
    assert data["name"] == "Fast" and data["assigned_api_key_ids"] == ["k2"]


def test_create_rejects_unowned_keys_conflicts_and_reserved_names(router):
    runner = CliRunner()
    unowned = runner.invoke(
        cli,
        ["--agent", "router", "strategies", "create", "--name", "X", "--key", "nope"],
    )
    assert unowned.exit_code != 0 and "Not one of your API keys" in unowned.output
    both = runner.invoke(
        cli,
        [
            "--agent",
            "router",
            "strategies",
            "create",
            "--name",
            "X",
            "--switchyard",
            "--auto-routing",
            "on",
        ],
    )
    assert both.exit_code != 0 and "can't both be on" in both.output
    reserved = runner.invoke(
        cli, ["--agent", "router", "strategies", "create", "--name", "account default"]
    )
    assert reserved.exit_code != 0 and "reserved" in reserved.output
    missing = runner.invoke(cli, ["--no-input", "router", "strategies", "create"])
    assert missing.exit_code != 0 and "--name" in missing.output
    assert writes(router) == []


def test_default_strategy_cannot_be_renamed_lose_keys_or_be_deleted(router):
    runner = CliRunner()
    rename = runner.invoke(
        cli,
        [
            "--agent",
            "router",
            "strategies",
            "edit",
            "Account default",
            "--name",
            "Mine",
        ],
    )
    assert rename.exit_code != 0 and "can't be renamed" in rename.output
    remove = runner.invoke(
        cli, ["--agent", "router", "strategies", "edit", DEFAULT, "--remove-key", "k1"]
    )
    assert remove.exit_code != 0 and "joining another strategy" in remove.output
    delete = runner.invoke(
        cli, ["--agent", "router", "strategies", "delete", "account default", "--yes"]
    )
    assert delete.exit_code != 0 and "can't be deleted" in delete.output
    assert writes(router) == []


def test_edit_moves_keys_between_strategies(router):
    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "router",
            "strategies",
            "edit",
            "cheap",
            "--add-key",
            "k1",
            "--remove-key",
            "k3",
        ],
    )
    assert result.exit_code == 0, result.output
    assert writes(router) == [
        (
            "POST",
            f"/client/routing-profiles/{CHEAP}/assignments",
            {"api_key_ids": ["k1"]},
        ),
        (
            "POST",
            f"/client/routing-profiles/{DEFAULT}/assignments",
            {"api_key_ids": ["k3"]},
        ),
    ]


def test_edit_dry_run_lists_requests_without_sending(router):
    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "router",
            "strategies",
            "edit",
            "Cheap",
            "--no-cost-efficient",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"] == [
        {
            "method": "PATCH",
            "path": f"/client/routing-profiles/{CHEAP}",
            "body": {"cost_efficient_routing_enabled": False},
        }
    ]
    assert writes(router) == []


def test_delete_moves_keys_to_default_and_requires_confirmation(router):
    runner = CliRunner()
    unconfirmed = runner.invoke(
        cli, ["--no-input", "router", "strategies", "delete", "Cheap"]
    )
    assert unconfirmed.exit_code != 0 and "--yes" in unconfirmed.output
    result = runner.invoke(
        cli, ["--agent", "router", "strategies", "delete", "Cheap", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert writes(router) == [
        ("DELETE", f"/client/routing-profiles/{CHEAP}", {"reassign_to": DEFAULT})
    ]


def test_accounts_without_strategies_get_a_friendly_message(router):
    router["unavailable"] = True
    result = CliRunner().invoke(
        cli, ["--human", "router", "strategies", "list", "--account"]
    )
    assert result.exit_code != 0
    assert str(result.exception) == (
        "Strategies aren't available for your Router account yet."
    )


def test_human_list_shows_strategy_summary(router):
    result = CliRunner().invoke(
        cli, ["--human", "router", "strategies", "list", "--account"]
    )
    assert result.exit_code == 0, result.output
    assert "2 strategies" in result.output
    assert "Account default (default)" in result.output
    assert "Cost-efficient routing, Jev Auto Routing" in result.output


class _Answer:
    def __init__(self, value):
        self.value = value

    def ask(self):
        return self.value


def test_interactive_editor_requires_explicit_save_and_guards_unsaved_changes(
    router, monkeypatch
):
    prompts = []
    selects = iter(
        [
            CHEAP,  # choose the Cheap strategy
            "cost_efficient_routing_enabled",  # toggle it off (unsaved)
            router_profiles._BACK,  # try to leave: asked to discard
            router_profiles._KEYS,  # stay; edit keys
            router_profiles._SAVE,  # explicitly save
            router_profiles._BACK,  # leave the strategy list
        ]
    )

    def select(message, choices, **kwargs):
        prompts.append(
            (
                message,
                {
                    getattr(c, "value", None): getattr(c, "disabled", None)
                    for c in choices
                },
            )
        )
        return _Answer(next(selects))

    confirms = iter([False])  # don't discard
    monkeypatch.setattr(router_profiles, "_interactive_keys", lambda _ctx: True)
    monkeypatch.setattr(router_profiles.questionary, "select", select)
    monkeypatch.setattr(
        router_profiles.questionary, "confirm", lambda *a, **k: _Answer(next(confirms))
    )
    monkeypatch.setattr(
        router_profiles.questionary, "checkbox", lambda *a, **k: _Answer(["k3", "k1"])
    )
    result = CliRunner().invoke(cli, ["--human", "router", "strategies", "--account"])
    assert result.exit_code == 0, result.output
    editor = [options for message, options in prompts if message == "Edit Cheap"]
    assert editor[0][router_profiles._SAVE] == "No changes yet"
    assert editor[1][router_profiles._SAVE] is None
    assert writes(router) == [
        (
            "PATCH",
            f"/client/routing-profiles/{CHEAP}",
            {"cost_efficient_routing_enabled": False},
        ),
        (
            "POST",
            f"/client/routing-profiles/{CHEAP}/assignments",
            {"api_key_ids": ["k1"]},
        ),
    ]
    assert "Saved Cheap." in result.output


def test_interactive_back_discards_when_confirmed(router, monkeypatch):
    selects = iter(
        [
            CHEAP,
            "switchyard_routing_enabled",
            router_profiles._BACK,
            router_profiles._BACK,
        ]
    )
    monkeypatch.setattr(router_profiles, "_interactive_keys", lambda _ctx: True)
    monkeypatch.setattr(
        router_profiles.questionary, "select", lambda *a, **k: _Answer(next(selects))
    )
    monkeypatch.setattr(
        router_profiles.questionary, "confirm", lambda *a, **k: _Answer(True)
    )
    result = CliRunner().invoke(cli, ["--human", "router", "strategies", "--account"])
    assert result.exit_code == 0, result.output
    assert next(selects, None) is None  # every prompt was consumed
    assert writes(router) == []


def test_strategies_keep_api_key_toggles_when_not_signed_in(router, monkeypatch):
    monkeypatch.setattr(
        RouterUserClient, "status", lambda self: {"authenticated": False}
    )
    seen = []
    monkeypatch.setattr(
        "ramp_cli.commands.router._resolve_strategy_api_key",
        lambda ctx, api_key, fmt: seen.append(api_key) or "rk-test",
    )
    monkeypatch.setattr(
        "ramp_cli.commands.router._strategy_settings_request",
        lambda key, changes=None, base_url=None: {"allow_flex_tier_default": True},
    )
    monkeypatch.setattr(
        "ramp_cli.commands.router._print_strategy_settings",
        lambda fmt, payload: seen.append(payload),
    )
    result = CliRunner().invoke(cli, ["--agent", "router", "strategies", "list"])
    assert result.exit_code == 0, result.output
    assert seen == [None, {"allow_flex_tier_default": True}]
    assert router["calls"] == []


def test_explicit_api_key_uses_key_toggles_even_when_signed_in(router, monkeypatch):
    monkeypatch.setattr(
        "ramp_cli.commands.router._resolve_strategy_api_key",
        lambda ctx, api_key, fmt: api_key,
    )
    monkeypatch.setattr(
        "ramp_cli.commands.router._strategy_settings_request",
        lambda key, changes=None, base_url=None: {"key": key},
    )
    monkeypatch.setattr(
        "ramp_cli.commands.router._print_strategy_settings",
        lambda fmt, payload: print(json.dumps(payload)),
    )
    result = CliRunner().invoke(
        cli, ["--agent", "router", "strategies", "list", "--api-key", "rk-explicit"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"key": "rk-explicit"}
    assert router["calls"] == []


@pytest.mark.parametrize("mode", [["--agent"], ["--output", "json"]])
@pytest.mark.parametrize("subcommand", [[], ["list"]])
def test_machine_strategy_contract_does_not_change_after_login(
    router, monkeypatch, mode, subcommand
):
    logged_in = False
    monkeypatch.setattr(
        RouterUserClient, "status", lambda self: {"authenticated": logged_in}
    )
    monkeypatch.setattr(
        "ramp_cli.commands.router._resolve_strategy_api_key",
        lambda *_a: "stored-key",
    )
    monkeypatch.setattr(
        "ramp_cli.commands.router._strategy_settings_request",
        lambda *_a, **_kw: {
            "allow_flex_tier_default": True,
            "switchyard_routing_enabled": False,
        },
    )
    args = [*mode, "router", "strategies", *subcommand]
    before = CliRunner().invoke(cli, args)
    logged_in = True
    after = CliRunner().invoke(cli, args)
    assert before.exit_code == after.exit_code == 0, (before.output, after.output)
    assert json.loads(before.output) == json.loads(after.output)
    assert "strategies" in json.loads(after.output)["data"][0]
    assert router["calls"] == []


@pytest.mark.parametrize(
    "args", [["--account"], ["--account", "list"], ["list", "--account"]]
)
def test_machine_profiles_require_explicit_account_mode(router, args):
    result = CliRunner().invoke(cli, ["--agent", "router", "strategies", *args])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"] == _profiles()


@pytest.mark.parametrize(
    "args",
    [
        ["--account", "--api-key", "key"],
        ["--account", "list", "--api-key", "key"],
        ["--api-key", "key", "list", "--account"],
        ["list", "--account", "--api-key", "key"],
    ],
)
def test_account_and_api_key_modes_are_mutually_exclusive(router, args):
    result = CliRunner().invoke(cli, ["--agent", "router", "strategies", *args])
    assert result.exit_code == 2, result.output
    assert "--account cannot be combined with --api-key" in result.output
    assert router["calls"] == []


def test_account_mode_cannot_silently_enable_stored_key_settings(router):
    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "router",
            "strategies",
            "--account",
            "enable",
            "cost-efficient-routing",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "use strategies edit" in result.output
    assert router["calls"] == []


def test_unreachable_router_names_the_url(monkeypatch):
    client = RouterUserClient.__new__(RouterUserClient)
    client.origin = "http://localhost:3080"

    def boom(*args, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(RouterUserClient, "_send", boom)
    with pytest.raises(
        RampCLIError, match="Can't reach Router at http://localhost:3080"
    ):
        client.get("/client/api-keys")


def test_account_flag_gives_signed_out_terminal_user_account_strategies(
    router, monkeypatch
):
    monkeypatch.setattr(
        RouterUserClient, "status", lambda self: {"authenticated": False}
    )
    real_client = router_profiles._client

    def terminal_client(ctx, ui_url=None):
        client = real_client(ctx, ui_url)
        client.interactive = True
        return client

    monkeypatch.setattr(router_profiles, "_client", terminal_client)
    monkeypatch.setattr(
        "ramp_cli.commands.router._resolve_strategy_api_key",
        lambda *_a: pytest.fail("stored-key strategies must not run"),
    )
    monkeypatch.setattr(router_profiles, "_interactive_keys", lambda _ctx: False)
    result = CliRunner().invoke(cli, ["--human", "router", "strategies", "--account"])
    assert result.exit_code == 0, result.output
    assert ("GET", "/client/routing-profiles", None) in router["calls"]
    assert "2 strategies" in result.output


def _edit(*args):
    return CliRunner().invoke(
        cli, ["--agent", "router", "strategies", "edit", CHEAP, *args]
    )
