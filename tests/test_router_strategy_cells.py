"""Editing a strategy row's cells in place, the same way harness rows work."""

import asyncio

import httpx
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    OptionList,
    Select,
    SelectionList,
)

from ramp_cli.router_ui.app import (
    CellMenu,
    Confirm,
    RouterApp,
    StrategiesPage,
)
from ramp_cli.router_ui.dialogs import Notice
from ramp_cli.router_ui.tabs.profile import ProfilePage
from tests.test_router_harness_cells import choose
from tests.test_router_harness_cells import open_cells as open_harness_cells
from tests.test_router_ui import FakeService, settle


async def open_cells(app, pilot, column=1):
    await settle(app, pilot)
    table = app.screen.query_one("#table", DataTable)
    table.focus()
    await pilot.press("enter")
    for _ in range(column - 1):
        await pilot.press("right")
    return table


def test_choosing_the_current_value_sends_nothing():
    async def run():
        service = FakeService()
        app = RouterApp(service, page="strategies")
        async with app.run_test(size=(120, 32)) as pilot:
            await open_cheap(app, pilot, column=1)
            await pilot.press("enter")
            await choose(app, pilot, "On")
            assert not isinstance(app.screen, (CellMenu, Confirm))
            assert not service.calls

    asyncio.run(run())


async def open_cheap(app, pilot, column=0):
    """Cells of the second row, a strategy the user made, from its name."""
    await settle(app, pilot)
    table = app.screen.query_one("#table", DataTable)
    table.focus()
    table.move_cursor(row=1)
    await pilot.press("enter")
    for _ in range(column):
        await pilot.press("right")
    return table


def status(app) -> str:
    return str(app.screen.query_one("#status").render())


def test_presets_and_legacy_profiles_restrict_strategy_edits():
    async def run():
        service = FakeService()
        app = RouterApp(service, page="strategies")
        async with app.run_test(size=(120, 32)) as pilot:
            await open_cheap(app, pilot, column=2)
            await pilot.press("enter")
            await settle(app, pilot)
            assert isinstance(app.screen, StrategiesPage)
            assert "isn't offered by this preset" in status(app)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0, column=1)
            await pilot.press("enter")
            await settle(app, pilot)
            assert "Legacy profiles are read-only" in status(app)
            assert not service.calls

    asyncio.run(run())


def test_saved_pair_edit_preserves_auto_and_clears_both_tiers_when_disabled():
    async def run():
        service = FakeService()
        service.profile_rows[1]["switchyard_settings"] = {
            "efficient_model": "efficient-model",
            "capable_model": "capable-model",
            "effort": "high",
            "sensitivity_config_name": None,
        }
        service.profile_rows[1]["auto_routing_enabled"] = True
        app = RouterApp(service, page="strategies")
        async with app.run_test(size=(120, 32)) as pilot:
            table = await open_cheap(app, pilot, column=2)
            assert str(table.get_row("cheap")[2]) == "Model pair"
            await pilot.press("enter")
            await settle(app, pilot)
            assert isinstance(app.screen, ProfilePage)
            assert app.screen.query_one("#switchyard", Checkbox).value
            app.screen.query_one("#sensitivity", Select).value = "strict"
            assert app.screen.collect().patch_body() == {
                "switchyard_settings": {
                    "efficient_model": "efficient-model",
                    "capable_model": "capable-model",
                    "effort": "high",
                    "sensitivity_config_name": "strict",
                }
            }
            app.screen.query_one("#switchyard", Checkbox).value = False
            assert app.screen.collect().patch_body() == {
                "switchyard_settings": {
                    "efficient_model": None,
                    "capable_model": None,
                    "effort": None,
                    "sensitivity_config_name": "strict",
                }
            }
            assert not service.calls

    asyncio.run(run())


def test_api_keys_checklist_closed_unchanged_sends_nothing():
    async def run():
        service = FakeService()
        app = RouterApp(service, page="strategies")
        async with app.run_test(size=(120, 32)) as pilot:
            await open_cheap(app, pilot, column=4)
            await pilot.press("enter")
            await settle(app, pilot)
            checklist = app.screen.query_one(SelectionList)
            assert not checklist.get_option_at_index(0).disabled
            await pilot.press("escape")
            await settle(app, pilot)
            assert isinstance(app.screen, StrategiesPage)
            assert not service.calls

    asyncio.run(run())


def test_name_cell_deletes_the_strategy_and_says_where_its_keys_go():
    async def run():
        service = FakeService()
        service.profile_rows[1]["assigned_api_key_ids"] = ["key-2", "key-3"]
        app = RouterApp(service, page="strategies")
        async with app.run_test(size=(120, 32)) as pilot:
            await open_cheap(app, pilot)
            await pilot.press("enter")
            await choose(app, pilot, "Delete Profile?")
            await settle(app, pilot)
            assert isinstance(app.screen, Confirm)
            assert str(app.screen.message) == (
                "Delete Cheap? Its 2 keys will move to your default routing profile."
            )
            app.screen.query_one("#accept").press()
            await settle(app, pilot)
            assert service.calls == [("delete-profile", "cheap")]
            assert "Deleted Cheap." in status(app)

    asyncio.run(run())


def test_uncertain_create_warns_before_another_attempt():
    async def run():
        service = FakeService()
        service.save_profile = lambda _draft: (_ for _ in ()).throw(
            httpx.ReadTimeout("Lost")
        )
        app = RouterApp(service, page="strategies")
        async with app.run_test(size=(120, 32)) as pilot:
            await settle(app, pilot)
            app.screen.query_one("#create", Button).press()
            await settle(app, pilot)
            app.screen.query_one(OptionList).highlighted = 0
            await pilot.press("enter")
            await settle(app, pilot)
            app.screen.query_one("#name", Input).value = "New strategy"
            app.screen.query_one("#save", Button).press()
            await settle(app, pilot)
            app.screen.query_one("#accept").press()
            await settle(app, pilot)
            assert "may already have saved" in status(app)
            app.screen.query_one("#save", Button).press()
            await settle(app, pilot)
            assert isinstance(app.screen, ProfilePage)

    asyncio.run(run())


class FailingReload(FakeService):
    def harnesses(self, *args, **kwargs):
        if self.calls:
            raise RuntimeError("offline")
        return super().harnesses(*args, **kwargs)


def test_a_failed_reload_still_reports_the_change_went_through():
    async def run():
        service = FailingReload()
        app = RouterApp(service, page="harnesses")
        async with app.run_test(size=(120, 32)) as pilot:
            await open_harness_cells(app, pilot)
            await pilot.press("enter")
            await choose(app, pilot, "Disconnect?")
            app.screen.query_one("#accept").press()
            await settle(app, pilot)
            assert service.calls == [("command", ["configure", "disconnect", "codex"])]
            assert isinstance(app.screen, Notice)
            assert "Restart Codex" in str(
                app.screen.query_one("#notice-message").render()
            )
            await pilot.press("enter")
            await settle(app, pilot)
            assert "Refreshing failed" in str(app.screen.query_one("#status").render())

    asyncio.run(run())
