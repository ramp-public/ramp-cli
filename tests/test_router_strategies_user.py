"""Account-level `ramp router strategies` over the web's routing-profile endpoints."""

import asyncio
import copy
import json

import httpx
import pytest
from click.testing import CliRunner
from textual.widgets import (
    Checkbox,
    DataTable,
    Input,
    OptionList,
    Select,
    SelectionList,
    Tabs,
)

from ramp_cli.client.router_user import RouterUserClient
from ramp_cli.commands import router_user_strategies as router_profiles
from ramp_cli.commands.router_user_strategies import ProfileDraft, save_draft
from ramp_cli.errors import ApiError, RampCLIError
from ramp_cli.main import cli
from ramp_cli.router_ui.app import RouterApp
from ramp_cli.router_ui.service import RouterService
from ramp_cli.router_ui.tabs.coding import CodingSettings
from tests.test_router_harness_cells import choose
from tests.test_router_ui import settle

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
            "slug": "cheap",
            "preset_type": "production-agents",
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
    state = {"profiles": _profiles(), "calls": [], "unavailable": False, "aliases": []}

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
        if path == "/client/api-key-grants":
            return {"data": [], "has_more": False}
        if path == "/admin/models":
            return state.get("inventory", {"data": []})
        if path == "/admin/me/auto-routing-config":
            return state.get("auto", {"candidates": []})
        if path == "/admin/model-aliases":
            return {"data": copy.deepcopy(state["aliases"])}
        if path == "/admin/benchmarks":
            return {
                "max_selections": 3,
                "data": [
                    {"id": "mmlu_pro", "name": "MMLU-Pro"},
                    {"id": "coding_index", "name": "Coding Index"},
                    {"id": "math_index", "name": "Math Index"},
                ],
            }
        if path.startswith("/client/routing-profiles/"):
            return copy.deepcopy(find(path.rsplit("/", 1)[1]))
        raise AssertionError(path)

    def post(self, path, *, json, **kwargs):
        state["calls"].append(("POST", path, json))
        if path == "/admin/model-aliases":
            created = {
                "id": f"alias-{len(state['aliases']) + 1}",
                **copy.deepcopy(json),
            }
            state["aliases"].append(created)
            return copy.deepcopy(created)
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

    def put(self, path, *, json, **kwargs):
        state["calls"].append(("PUT", path, json))
        alias = next(a for a in state["aliases"] if a["id"] == path.rsplit("/", 1)[1])
        alias.update(copy.deepcopy(json))
        return copy.deepcopy(alias)

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
        ("put", put),
    ):
        monkeypatch.setattr(RouterUserClient, name, method)
    return state


def writes(state):
    return [call for call in state["calls"] if call[0] != "GET"]


@pytest.mark.parametrize("existing", [False, True])
def test_harness_coding_settings_save_only_account_controls(router, existing):
    if existing:
        profile = router["profiles"][1]
        profile.update(
            preset_type="coding-agents", provider_routing_preference="favor_cost"
        )
        profile["benchmark_alias"] = "preserve-historical-setting"

    async def run():
        service = RouterService({}, origin="http://localhost:3080")
        service.harnesses = lambda: []
        app = RouterApp(service, page="harnesses")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            page = app.screen
            page.query_one("#coding-cost", Select).value = "off"
            page.query_one("#coding-provider", Select).value = "throughput_only"
            await pilot.pause()
            app.open("keys")
            await pilot.pause()
            app.screen.query_one("#cancel").press()
            await pilot.pause()
            assert app.screen is page
            assert page.query_one(CodingSettings).dirty
            page.query_one("#coding-save").press()
            await settle(app, pilot)
            saved = next(
                p for p in router["profiles"] if p.get("preset_type") == "coding-agents"
            )
            assert saved["cost_efficient_routing_enabled"] is False
            assert saved["provider_routing_preference"] == "throughput_only"
            assert len(writes(router)) == 1
            if existing:
                assert writes(router)[0] == (
                    "PATCH",
                    f"/client/routing-profiles/{CHEAP}",
                    {
                        "cost_efficient_routing_enabled": False,
                        "provider_routing_preference": "throughput_only",
                    },
                )
                assert saved["benchmark_alias"] == "preserve-historical-setting"
                assert saved["assigned_api_key_ids"] == ["k3"]
            else:
                assert writes(router)[0][0] == "POST"
                assert saved["assigned_api_key_ids"] == []
            assert not page.query_one(CodingSettings).dirty
            assert all(
                p.get("preset_type") != "coding-agents" for p in service.key_profiles()
            )
        service.close()

    asyncio.run(run())


@pytest.mark.parametrize("experimental", [False, True])
def test_coding_retry_keeps_visible_reverts_after_lost_write_response(
    router, monkeypatch, experimental
):
    profile = router["profiles"][1]
    profile["preset_type"] = (
        "coding-agents-experimental" if experimental else "coding-agents"
    )
    if experimental:
        profile["auto_routing_settings"] = {
            "candidates": [
                {"model": "openai:gpt-6-luna", "description": "Routine coding"},
                {"model": "openai:gpt-6.1-sol", "description": "Complex coding"},
            ]
        }
    field = "auto_routing_enabled" if experimental else "cost_efficient_routing_enabled"
    selector = "#auto" if experimental else "#coding-cost"
    save_selector = "#editor-save" if experimental else "#coding-save"
    patch = RouterUserClient.patch
    attempts = []

    def lose_first_response(client, path, *, json, **kwargs):
        attempts.append(copy.deepcopy(json))
        result = patch(client, path, json=json, **kwargs)
        if len(attempts) == 1:
            raise ApiError(503, '{"error":{"message":"Try again"}}')
        return result

    monkeypatch.setattr(RouterUserClient, "patch", lose_first_response)

    async def run():
        service = RouterService({}, origin="http://localhost:3080")
        service.harnesses = lambda: []
        app = RouterApp(service, page="harnesses")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            if experimental:
                app.screen.query_one("#experimental").press()
                await settle(app, pilot)
            app.screen.query_one(selector, Select).value = (
                False if experimental else "off"
            )
            await pilot.pause()
            app.screen.query_one(save_selector).press()
            await settle(app, pilot)
            app.screen.query_one(selector, Select).value = (
                True if experimental else "on"
            )
            await pilot.pause()
            assert not app.screen.query_one(save_selector).disabled
            app.screen.query_one(save_selector).press()
            await settle(app, pilot)
            assert attempts == [{field: False}, {field: True}]
            assert profile[field] is True
        service.close()

    asyncio.run(run())


def test_coding_create_rechecks_the_singleton_before_writing(router):
    service = RouterService({}, origin="http://localhost:3080")
    router["profiles"][1]["preset_type"] = "coding-agents"
    with pytest.raises(RampCLIError, match="Reopen"):
        service.save_coding_profile(
            "coding-agents", {"cost_efficient_routing_enabled": False}, None
        )
    assert not writes(router)
    service.close()


def test_new_coding_defaults_can_be_saved_without_toggling_a_control(router):
    async def run():
        service = RouterService({}, origin="http://localhost:3080")
        service.harnesses = lambda: []
        app = RouterApp(service, page="harnesses")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            button = app.screen.query_one("#coding-save")
            assert not button.disabled
            button.press()
            await settle(app, pilot)
            assert len(writes(router)) == 1
            saved = router["profiles"][-1]
            assert saved["preset_type"] == "coding-agents"
            assert saved["cost_efficient_routing_enabled"] is True
            assert saved["provider_routing_preference"] == "favor_cost"
            assert button.disabled
            router["unavailable"] = True
            app.screen.action_reload()
            await settle(app, pilot)
            assert app.screen.query_one("#coding-cost", Select).value == "unavailable"
            assert app.screen.query_one("#coding-cost").disabled
            assert app.screen.query_one("#experimental").disabled
            assert app.screen.query_one("#harnesses", DataTable).is_mounted
            router["unavailable"] = False
            app.screen.action_reload()
            await settle(app, pilot)
            assert app.screen.query_one("#coding-cost", Select).value == "on"
            assert not app.screen.query_one("#experimental").disabled
        service.close()

    asyncio.run(run())


def test_tui_profile_creation_settings_and_key_assignment_survive_tab_changes(router):
    async def run():
        service = RouterService({}, origin="http://localhost:3080")
        app = RouterApp(service, page="strategies")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            app.screen.query_one("#create").press()
            await settle(app, pilot)
            app.screen.query_one(OptionList).highlighted = 0
            await pilot.press("enter")
            await settle(app, pilot)
            app.screen.query_one("#name", Input).value = "Coding profile"
            await pilot.pause()
            app.screen.query_one("#provider", Select).value = "favor_throughput"
            app.screen.query_one("#cross-provider", Checkbox).value = False
            await pilot.pause()
            assert not app.screen.query_one("#provider-fields").display
            app.screen.query_one("#cross-provider", Checkbox).value = True
            await pilot.pause()
            assert app.screen.query_one("#provider-fields").display
            assert app.screen.query_one("#provider", Select).value == "favor_throughput"
            app.screen.query_one("#save").press()
            await settle(app, pilot)
            app.screen.query_one("#accept").press()
            await settle(app, pilot)
            profile = router["profiles"][-1]
            assert profile["preset_type"] == "production-agents"
            assert profile["slug"] == "coding-profile"
            assert profile["provider_routing_preference"] == "favor_throughput"
            assert profile["auto_routing_enabled"] is False
            table = app.screen.query_one("#table", DataTable)
            assert str(table.get_row_at(table.cursor_row)[0]) == "Coding profile"

            table.focus()
            await pilot.press("enter", "right", "enter")
            await choose(app, pilot, "Off")
            app.screen.query_one("#accept").press()
            await settle(app, pilot)
            assert profile["cost_efficient_routing_enabled"] is False
            assert writes(router)[-1][2] == {"cost_efficient_routing_enabled": False}

            table.focus()
            table.move_cursor(column=4)
            await pilot.press("enter")
            await settle(app, pilot)
            app.screen.query_one(SelectionList).select("k1")
            await pilot.press("escape")
            await settle(app, pilot)
            app.screen.query_one("#accept").press()
            await settle(app, pilot)
            assert profile["assigned_api_key_ids"] == ["k1"]
            assert str(table.get_row(profile["id"])[4]) == "1"

            await pilot.click("#nav-keys")
            await settle(app, pilot)
            table = app.screen.query_one("#table", DataTable)
            row = table.get_row_index("k1")
            assert "Coding profile" in str(table.get_row("k1")[2])
            table.move_cursor(row=row)
            table.focus()
            await pilot.press("enter")
            table.move_cursor(column=2)
            await pilot.press("enter")
            await choose(app, pilot, "Account default · default")
            app.screen.query_one("#accept").press()
            await settle(app, pilot)
            assert profile["assigned_api_key_ids"] == []
            assert "k1" in router["profiles"][0]["assigned_api_key_ids"]
            assert router["profiles"][1]["assigned_api_key_ids"] == ["k3"]

            await pilot.click("#nav-strategies")
            await settle(app, pilot)
            assert (
                str(app.screen.query_one("#table", DataTable).get_row(profile["id"])[4])
                == "0"
            )
            app.screen.edit_profile(profile)
            await settle(app, pilot)
            app.screen.query_one("#cross-provider", Checkbox).value = False
            app.screen.query_one("#save").press()
            await settle(app, pilot)
            app.screen.query_one("#accept").press()
            await settle(app, pilot)
            assert writes(router)[-1][2] == {"provider_routing_preference": None}
        service.close()

    asyncio.run(run())


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


def test_experimental_preset_saves_model_pair_and_auto_without_legacy_toggle(router):
    router["inventory"] = {
        "data": [
            {
                "id": name,
                "provider": "openai",
                "provider_model": name,
                "display_name": name,
                "status": "active",
                "capabilities": ["tools", "reasoning"],
                "surfaces": ["responses"],
                "pricing": {"input_per_1m": "1", "output_per_1m": price},
                "requestable_models": [],
            }
            for name, price in (("gpt-6-luna", "1"), ("gpt-6.1-sol", "10"))
        ],
        # The class gate can differ from the caller-visible price.
        "switchyard_class_gate_output_per_1m": {"openai:gpt-6-luna": "3"},
    }
    router["auto"] = {
        "candidates": [
            {"model": "openai:gpt-6-luna", "description": "Routine coding"},
            {"model": "openai:gpt-6.1-sol", "description": "Complex coding"},
        ]
    }

    async def run():
        service = RouterService({}, origin="http://localhost:3080")
        service.harnesses = lambda: []
        app = RouterApp(service, page="harnesses")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            app.screen.query_one("#experimental").press()
            await settle(app, pilot)
            form = app.screen
            assert form.query_one("#switchyard", Select).value is True
            assert form.query_one("#auto", Select).value is True
            form.query_one(Tabs).active = "auto-tab"
            await pilot.resize_terminal(94, 32)
            await pilot.pause()
            assert (
                form.query_one("#editor-save").region.bottom
                <= form.query_one("#experimental-dialog").content_region.bottom
            )
            await pilot.resize_terminal(120, 40)
            form.query_one("#auto-model-1", Select).value = Select.NULL
            await pilot.pause()
            assert form.query_one("#editor-save").disabled
            with pytest.raises(ValueError, match="at least 2 different"):
                form.collect()
            form.query_one("#auto-model-1", Select).value = "openai:gpt-6-luna"
            with pytest.raises(ValueError, match="at least 2 different"):
                form.collect()
            form.query_one("#auto-model-1", Select).value = "openai:gpt-6.1-sol"
            form.query_one("#auto-description-0", Input).value = ""
            with pytest.raises(ValueError, match="Describe"):
                form.collect()
            form.query_one("#auto-description-0", Input).value = "Routine coding"
            form.query_one("#auto", Select).value = False
            form.query_one("#efficient", Select).value = Select.NULL
            with pytest.raises(ValueError, match="both an efficient and a frontier"):
                form.collect()
            form.query_one("#efficient", Select).value = "openai:gpt-6-luna"
            form.query_one("#auto", Select).value = True
            prices = app.screen.options["inventory"][
                "switchyard_class_gate_output_per_1m"
            ]
            prices["openai:gpt-6-luna"] = "6"
            with pytest.raises(ValueError, match="at least 2×"):
                form.collect()
            assert not writes(router)
            prices["openai:gpt-6-luna"] = "3"
            form.update_state()
            form.query_one("#editor-save").press()
            await settle(app, pilot)
            profile = router["profiles"][-1]
            assert profile["preset_type"] == "coding-agents-experimental"
            assert profile["switchyard_routing_enabled"] is False
            assert profile["auto_routing_enabled"] is True
            pair = profile["switchyard_settings"]
            assert pair["efficient_model"] == "openai:gpt-6-luna"
            assert pair["capable_model"] == "openai:gpt-6.1-sol"
            assert len(writes(router)) == 1  # No implicit key assignments.
            assert len(profile["auto_routing_settings"]["candidates"]) == 2

            profile["auto_routing_enabled"] = None
            profile["auto_routing_settings"] = None
            service.auto_routing_default = lambda: True
            service.invalidate()

            # Reopening resolves the saved singleton, without a key-assignment flow.
            app.screen.query_one("#experimental").press()
            await settle(app, pilot)
            # Switching the V2 control off withdraws the stored pair, leaving Auto on.
            app.screen.query_one("#switchyard", Select).value = False
            await pilot.pause()
            app.screen.query_one("#editor-save").press()
            await settle(app, pilot)
            assert writes(router)[-1][2] == {
                "switchyard_routing_enabled": False,
                "switchyard_settings": {
                    "efficient_model": None,
                    "capable_model": None,
                    "effort": None,
                    "sensitivity_config_name": "balanced",
                },
            }
            assert profile["auto_routing_enabled"] is None
            assert profile["auto_routing_settings"] is None

            assert len(writes(router)) == 2
            app.screen.query_one("#experimental").press()
            await settle(app, pilot)
            app.screen.query_one("#experimental-tabs", Tabs).active = "auto-tab"
            await pilot.pause()
            app.screen.query_one("#up-1").press()
            await pilot.pause()
            app.screen.query_one("#editor-save").press()
            await settle(app, pilot)
            assert writes(router)[-1][2] == {
                "auto_routing_settings": {
                    "candidates": list(reversed(router["auto"]["candidates"]))
                }
            }
        service.close()

    asyncio.run(run())


def test_research_preset_requires_benchmark_choice_or_explicit_opt_out(router):
    async def run():
        service = RouterService({}, origin="http://localhost:3080")
        app = RouterApp(service, page="strategies")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            app.screen.query_one("#create").press()
            await settle(app, pilot)
            app.screen.query_one(OptionList).highlighted = 3
            await pilot.press("enter")
            await settle(app, pilot)
            app.screen.query_one("#save").press()
            await settle(app, pilot)
            assert "Choose 1–3 benchmarks" in str(
                app.screen.query_one("#status").render()
            )
            assert not writes(router)
            app.screen.query_one("#benchmark", Checkbox).value = False
            app.screen.query_one("#save").press()
            await settle(app, pilot)
            app.screen.query_one("#accept").press()
            await settle(app, pilot)
            assert router["profiles"][-1]["preset_type"] == "knowledge-work"
        service.close()

    asyncio.run(run())


def test_benchmark_create_validates_weights_and_retries_partial_saves(
    router, monkeypatch
):
    post = RouterUserClient.post
    failures = {"alias": True, "profile": True}

    def failing_post(client, path, **kwargs):
        if path == "/admin/model-aliases" and failures["alias"]:
            raise ApiError(422, '{"error":{"message":"Alias unavailable"}}')
        if path == "/client/routing-profiles" and failures["profile"]:
            raise ApiError(422, '{"error":{"message":"Profile unavailable"}}')
        return post(client, path, **kwargs)

    monkeypatch.setattr(RouterUserClient, "post", failing_post)

    async def run():
        service = RouterService({}, origin="http://localhost:3080")
        app = RouterApp(service, page="strategies")
        async with app.run_test(size=(120, 50)) as pilot:
            await settle(app, pilot)
            app.screen.query_one("#create").press()
            await settle(app, pilot)
            app.screen.query_one(OptionList).highlighted = 3
            await pilot.press("enter")
            await settle(app, pilot)
            form = app.screen
            form.query_one("#slug", Input).value = "my-research"
            form.query_one("#benchmark-0", Select).value = "mmlu_pro"
            form.query_one("#benchmark-1", Select).value = "coding_index"
            await pilot.pause()
            form.query_one("#weight-0", Input).value = "30"
            await pilot.pause()
            assert form.query_one("#weight-1", Input).value == "70"
            form.query_one("#benchmark-2", Select).value = "math_index"
            await pilot.pause()
            with pytest.raises(ValueError, match="totaling 100"):
                form.collect()
            form.query_one("#benchmark-2", Select).value = Select.NULL
            form.query_one("#benchmark-1", Select).value = "mmlu_pro"
            await pilot.pause()
            with pytest.raises(ValueError, match="distinct"):
                form.collect()
            form.query_one("#benchmark-1", Select).value = "coding_index"
            await pilot.pause()
            for phase in ("alias", "profile", "retry", "success"):
                if phase == "profile":
                    failures["alias"] = False
                if phase == "success":
                    failures["profile"] = False
                form.query_one("#save").press()
                await settle(app, pilot)
                app.screen.query_one("#accept").press()
                await settle(app, pilot)
                if phase == "alias":
                    assert not writes(router)
                    assert not form.uncertain
                elif phase != "success":
                    assert len(writes(router)) == 1
                    assert form.query_one("#benchmark-save-status").display
                    assert not form.uncertain
            assert writes(router)[0] == (
                "POST",
                "/admin/model-aliases",
                {
                    "name": "my-research",
                    "benchmarks": [
                        {"benchmark_id": "mmlu_pro", "weight": 30},
                        {"benchmark_id": "coding_index", "weight": 70},
                    ],
                },
            )
            assert len(writes(router)) == 2
            assert router["profiles"][-1]["benchmark_alias"] == "my-research"
        service.close()

    asyncio.run(run())


@pytest.mark.parametrize("applied", [False, True])
def test_retry_collects_reverted_auto_toggle_after_failed_profile_save(
    router, monkeypatch, applied
):
    profile = router["profiles"][1]
    profile["auto_routing_settings"] = {
        "candidates": [
            {"model": "openai:small", "description": "Routine"},
            {"model": "openai:large", "description": "Complex"},
        ]
    }
    patch = RouterUserClient.patch
    attempts = []

    def fail_once(client, path, *, json, **kwargs):
        attempts.append(copy.deepcopy(json))
        if len(attempts) == 1:
            if applied:
                profile.update(json)
            raise ApiError(503, '{"error":{"message":"Try again"}}')
        return patch(client, path, json=json, **kwargs)

    monkeypatch.setattr(RouterUserClient, "patch", fail_once)

    async def run():
        service = RouterService({}, origin="http://localhost:3080")
        app = RouterApp(service, page="strategies")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            app.screen.edit_profile(profile)
            await settle(app, pilot)
            form = app.screen
            form.query_one("#auto", Select).value = "off"
            form.query_one("#save").press()
            await settle(app, pilot)
            app.screen.query_one("#accept").press()
            await settle(app, pilot)
            assert attempts == [{"auto_routing_enabled": False}]
            form.query_one("#auto", Select).value = "on"
            form.query_one("#name", Input).value = "Renamed"
            form.query_one("#save").press()
            await settle(app, pilot)
            app.screen.query_one("#accept").press()
            await settle(app, pilot)
            assert profile["auto_routing_enabled"] is True
            assert attempts[-1] == {
                "name": "Renamed",
                **({"auto_routing_enabled": True} if applied else {}),
            }
        service.close()

    asyncio.run(run())


def test_retry_restores_reverted_benchmark_weights_after_partial_save(
    router, monkeypatch
):
    original = [
        {"benchmark_id": "mmlu_pro", "weight": 30},
        {"benchmark_id": "coding_index", "weight": 70},
    ]
    router["aliases"] = [
        {
            "id": "research-alias",
            "name": "research",
            "benchmarks": copy.deepcopy(original),
        }
    ]
    profile = router["profiles"][1]
    profile.update(
        slug="research",
        preset_type="knowledge-work",
        benchmark_alias="research",
        auto_routing_enabled=False,
    )
    patch = RouterUserClient.patch
    attempts = []

    def fail_once(client, path, *, json, **kwargs):
        attempts.append(copy.deepcopy(json))
        if len(attempts) == 1:
            raise ApiError(422, '{"error":{"message":"Profile unavailable"}}')
        return patch(client, path, json=json, **kwargs)

    monkeypatch.setattr(RouterUserClient, "patch", fail_once)

    async def run():
        service = RouterService({}, origin="http://localhost:3080")
        app = RouterApp(service, page="strategies")
        async with app.run_test(size=(120, 50)) as pilot:
            await settle(app, pilot)
            app.screen.edit_profile(profile)
            await settle(app, pilot)
            form = app.screen
            form.query_one("#weight-0", Input).value = "60"
            form.query_one("#name", Input).value = "Renamed research"
            await pilot.pause()
            form.query_one("#save").press()
            await settle(app, pilot)
            app.screen.query_one("#accept").press()
            await settle(app, pilot)
            assert router["aliases"][0]["benchmarks"] == [
                {"benchmark_id": "mmlu_pro", "weight": 60},
                {"benchmark_id": "coding_index", "weight": 40},
            ]
            assert form.query_one("#benchmark-save-status").display
            form.query_one("#weight-0", Input).value = "30"
            await pilot.pause()
            form.query_one("#save").press()
            await settle(app, pilot)
            app.screen.query_one("#accept").press()
            await settle(app, pilot)
            assert router["aliases"][0]["benchmarks"] == original
            assert profile["name"] == "Renamed research"
            assert [method for method, _, _ in writes(router)] == [
                "PUT",
                "PUT",
                "PATCH",
            ]
        service.close()

    asyncio.run(run())


def test_benchmark_edits_migrate_legacy_binding_without_rewriting_shared_alias(router):
    raw = [
        {"benchmark_id": "mmlu_pro", "weight": 1},
        {"benchmark_id": "coding_index", "weight": 49},
    ]
    router["aliases"] = [
        {"id": "legacy-alias", "name": "legacy-research", "benchmarks": raw}
    ]
    profile = router["profiles"][1]
    profile.update(
        slug="research",
        preset_type="knowledge-work",
        benchmark_alias="legacy-research",
        auto_routing_enabled=False,
    )

    async def run():
        service = RouterService({}, origin="http://localhost:3080")
        app = RouterApp(service, page="strategies")
        async with app.run_test(size=(120, 50)) as pilot:
            await settle(app, pilot)
            app.screen.edit_profile(profile)
            await settle(app, pilot)
            form = app.screen
            assert form.query_one("#weight-0", Input).value == "3"
            assert form.query_one("#weight-1", Input).value == "97"
            form.query_one("#name", Input).value = "Research"
            form.query_one("#save").press()
            await settle(app, pilot)
            app.screen.query_one("#accept").press()
            await settle(app, pilot)
            assert writes(router) == [
                ("PATCH", f"/client/routing-profiles/{CHEAP}", {"name": "Research"})
            ]
            assert router["aliases"][0]["benchmarks"] == raw

            app.screen.edit_profile(profile)
            await settle(app, pilot)
            app.screen.query_one("#benchmark-0", Select).value = Select.NULL
            await pilot.pause()
            app.screen.query_one("#save").press()
            await settle(app, pilot)
            app.screen.query_one("#accept").press()
            await settle(app, pilot)
            assert writes(router)[1:] == [
                (
                    "POST",
                    "/admin/model-aliases",
                    {
                        "name": "research",
                        "benchmarks": [{"benchmark_id": "coding_index", "weight": 100}],
                    },
                ),
                (
                    "PATCH",
                    f"/client/routing-profiles/{CHEAP}",
                    {"benchmark_alias": "research"},
                ),
            ]
            assert router["aliases"][0]["benchmarks"] == raw

            app.screen.edit_profile(profile)
            await settle(app, pilot)
            app.screen.query_one("#benchmark-0", Select).value = "math_index"
            await pilot.pause()
            app.screen.query_one("#save").press()
            await settle(app, pilot)
            app.screen.query_one("#accept").press()
            await settle(app, pilot)
            assert writes(router)[-1] == (
                "PUT",
                "/admin/model-aliases/alias-2",
                {"benchmarks": [{"benchmark_id": "math_index", "weight": 100}]},
            )
            assert len(writes(router)) == 4

            app.screen.edit_profile(profile)
            await settle(app, pilot)
            app.screen.query_one("#benchmark", Checkbox).value = False
            app.screen.query_one("#save").press()
            await settle(app, pilot)
            app.screen.query_one("#accept").press()
            await settle(app, pilot)
            assert writes(router)[-1][2] == {"benchmark_alias": None}
            assert len(router["aliases"]) == 2
        service.close()

    asyncio.run(run())


@pytest.mark.parametrize("alias_state", ["missing", "repurposed", "offline"])
def test_unrelated_profile_edit_preserves_unavailable_benchmark_binding(
    router, monkeypatch, alias_state
):
    profile = router["profiles"][1]
    profile.update(benchmark_alias="saved-alias", auto_routing_enabled=False)
    if alias_state == "repurposed":
        router["aliases"] = [{"id": "old", "name": "saved-alias", "benchmarks": []}]
    elif alias_state == "offline":
        get = RouterUserClient.get

        def offline(client, path, **kwargs):
            if path in ("/admin/benchmarks", "/admin/model-aliases"):
                raise httpx.ReadTimeout("offline")
            return get(client, path, **kwargs)

        monkeypatch.setattr(RouterUserClient, "get", offline)

    async def run():
        service = RouterService({}, origin="http://localhost:3080")
        app = RouterApp(service, page="strategies")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            app.screen.edit_profile(profile)
            await settle(app, pilot)
            app.screen.query_one("#name", Input).value = "Renamed"
            app.screen.query_one("#save").press()
            await settle(app, pilot)
            app.screen.query_one("#accept").press()
            await settle(app, pilot)
            assert writes(router) == [
                ("PATCH", f"/client/routing-profiles/{CHEAP}", {"name": "Renamed"})
            ]
        service.close()

    asyncio.run(run())


def test_benchmark_slug_collision_and_unchanged_ratio_reuse(router):
    router["aliases"] = [
        {"id": "collision", "name": "research-analysis", "benchmarks": []},
        {
            "id": "weighted",
            "name": "weighted",
            "benchmarks": [
                {"benchmark_id": "mmlu_pro", "weight": 1},
                {"benchmark_id": "coding_index", "weight": 49},
            ],
        },
    ]

    async def run():
        service = RouterService({}, origin="http://localhost:3080")
        app = RouterApp(service, page="strategies")
        async with app.run_test(size=(120, 50)) as pilot:
            await settle(app, pilot)
            app.screen.query_one("#create").press()
            await settle(app, pilot)
            app.screen.query_one(OptionList).highlighted = 5
            await pilot.press("enter")
            await settle(app, pilot)
            with pytest.raises(ValueError, match="non-benchmark alias"):
                app.screen.collect()
            app.screen.query_one("#slug", Input).value = "weighted"
            await pilot.pause()
            assert app.screen.query_one("#weight-0", Input).value == "3"
            app.screen.query_one("#save").press()
            await settle(app, pilot)
            app.screen.query_one("#accept").press()
            await settle(app, pilot)
            assert len(writes(router)) == 1
            assert writes(router)[0][1] == "/client/routing-profiles"
            assert router["profiles"][-1]["benchmark_alias"] == "weighted"
        # A stale picker must never overwrite an alias repurposed in the dashboard.
        with pytest.raises(RampCLIError, match="non-benchmark alias"):
            service.save_profile_benchmarks(
                "research-analysis", [{"benchmark_id": "mmlu_pro", "weight": 100}]
            )
        service.close()

    asyncio.run(run())


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


def test_rebase_preserves_unedited_dashboard_settings_after_a_partial_save():
    profile = {
        **_profiles()[1],
        "provider_routing_preference": "favor_cost",
        "shadow_settings": {"model": "original"},
    }
    draft = ProfileDraft.from_profile(profile)
    draft.set_setting("cost_efficient_routing_enabled", False)
    draft.rebase(
        {
            **profile,
            "name": "Dashboard rename",
            "provider_routing_preference": "throughput_only",
            "shadow_settings": {"model": "updated"},
        }
    )
    assert draft.name == "Dashboard rename"
    assert draft.settings["provider_routing_preference"] == "throughput_only"
    assert draft.settings["shadow_settings"] == {"model": "updated"}
    assert draft.patch_body() == {"cost_efficient_routing_enabled": False}


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
        "Routing profiles aren't available for your Router account yet."
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
