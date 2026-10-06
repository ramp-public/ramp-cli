"""Editing a harness row's cells in place: Enter, arrows, dropdowns, reviews."""

import asyncio
import copy
import json
import sqlite3

import click
import httpx
import pytest
from textual.widgets import DataTable, OptionList

from ramp_cli.commands import cursor, router
from ramp_cli.router_ui import harness
from ramp_cli.router_ui.app import (
    CellMenu,
    Confirm,
    RouterApp,
)
from ramp_cli.router_ui.tabs.harnesses import priced
from tests.test_router_ui import FakeService, settle


async def open_cells(app, pilot, column=1):
    await settle(app, pilot)
    table = app.screen.query_one("#harnesses", DataTable)
    table.focus()
    await pilot.press("enter")
    for _ in range(column - 1):
        await pilot.press("right")
    return table


async def choose(app, pilot, label):
    await settle(app, pilot)
    assert isinstance(app.screen, CellMenu)
    menu = app.screen.query_one(OptionList)
    labels = [str(menu.get_option_at_index(i).prompt) for i in range(menu.option_count)]
    menu.highlighted = labels.index(label)
    await pilot.press("enter")
    await pilot.pause()


def test_cell_dropdown_cancel_and_current_value_send_nothing():
    async def run():
        service = FakeService()
        app = RouterApp(service, page="harnesses")
        async with app.run_test(size=(120, 32)) as pilot:
            table = await open_cells(app, pilot)
            await pilot.press("enter")
            await settle(app, pilot)
            assert isinstance(app.screen, CellMenu)
            await pilot.press("escape")
            assert app.focused is table
            await pilot.press("enter")
            await choose(app, pilot, "Back")
            assert not isinstance(app.screen, (CellMenu, Confirm))
            assert table.cursor_type == "cell"
            assert not service.calls

    asyncio.run(run())


def test_router_cell_disconnects_after_review():
    async def run():
        service = FakeService()
        app = RouterApp(service, page="harnesses")
        async with app.run_test(size=(120, 32)) as pilot:
            await open_cells(app, pilot)
            await pilot.press("enter")
            await choose(app, pilot, "Disconnect?")
            assert isinstance(app.screen, Confirm)
            await pilot.press("escape")
            assert not service.calls
            await pilot.press("enter")
            await choose(app, pilot, "Disconnect?")
            app.screen.query_one("#accept").press()
            await settle(app, pilot)
            assert service.calls == [("command", ["configure", "disconnect", "codex"])]

    asyncio.run(run())


def test_strategy_cell_assigns_a_named_strategy_and_warns_about_shared_keys():
    async def run():
        service = FakeService()
        app = RouterApp(service, page="harnesses")
        async with app.run_test(size=(120, 32)) as pilot:
            await open_cells(app, pilot, column=3)
            await pilot.press("enter")
            await settle(app, pilot)
            menu = app.screen.query_one(OptionList)
            assert str(menu.get_option_at_index(0).prompt) == "Default · default"
            await choose(app, pilot, "Fast")
            assert isinstance(app.screen, Confirm)
            assert "“Fast”" in app.screen.message
            assert "Claude Code use the same API key" in app.screen.message
            app.screen.query_one("#accept").press()
            await settle(app, pilot)
            assert service.calls == [("profile", "codex", "Fast", "connection")]

    asyncio.run(run())


def test_key_routing_profiles_reads_with_the_key(monkeypatch):
    captured = {}

    def get(url, *, headers, timeout):
        captured["request"] = (url, headers["Authorization"])
        return httpx.Response(
            200,
            json={"data": [{"id": "p1", "name": "Default", "is_current": True}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("ramp_cli.commands.router.httpx.get", get)
    profiles = router._key_routing_profiles(
        "router-secret", base_url="https://api.router.com/v1"
    )
    assert profiles == [{"id": "p1", "name": "Default", "is_current": True}]
    assert captured["request"][0].endswith("/session-usage/routing-profiles")
    assert captured["request"][1] == "Bearer router-secret"


def test_select_routing_profile_requires_router_confirmation(monkeypatch):
    connection = harness.Connection(
        "codex", None, "secret", "https://api.router.com/v1"
    )

    class Current:
        def __enter__(self):
            return connection

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(harness, "connection", lambda client: Current())
    monkeypatch.setattr(
        router,
        "_select_key_routing_profile",
        lambda key, name, base_url=None: {"routing_profile": {"name": "fast"}},
    )
    assert harness.select_routing_profile("codex", "Fast", connection.token)
    with pytest.raises(click.ClickException, match="changed while you were editing"):
        harness.select_routing_profile("codex", "Fast", "stale")
    monkeypatch.setattr(
        router,
        "_select_key_routing_profile",
        lambda key, name, base_url=None: {"routing_profile": {"name": "Default"}},
    )
    with pytest.raises(click.ClickException, match="did not confirm"):
        harness.select_routing_profile("codex", "Fast", connection.token)


def write_cursor_state(tmp_path, settings):
    storage = tmp_path / "cursor-user" / "globalStorage"
    storage.mkdir(parents=True)
    connection = sqlite3.connect(storage / "state.vscdb")
    connection.execute("CREATE TABLE ItemTable (key TEXT UNIQUE, value BLOB)")
    connection.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        (cursor._SETTINGS_KEY, json.dumps(settings)),
    )
    connection.commit()
    connection.close()


def test_cursor_detection_reads_the_base_url_override(tmp_path):
    assert not cursor.is_installed()
    assert cursor.override_base_url() is None
    write_cursor_state(tmp_path, {"openAIBaseUrl": "https://api.router.com/v1/"})
    assert cursor.is_installed()
    assert cursor.override_base_url() == "https://api.router.com/v1"
    assert router._cursor_routes_to_router()


@pytest.mark.parametrize(
    "settings", [{"openAIBaseUrl": "https://api.openai.com/v1"}, {}, []]
)
def test_cursor_without_a_router_override_is_not_connected(tmp_path, settings):
    write_cursor_state(tmp_path, settings)
    assert cursor.is_installed()
    assert not router._cursor_routes_to_router()


def subagent_service():
    service = FakeService()
    tiers = {"sonnet": "served-id", "opus": None, "haiku": None, "fable": None}
    claude = {
        "id": "claude-code",
        "name": "Claude Code",
        "detected": True,
        "configured": True,
        "default_model": "claude-test",
        "routing_strategy": "Flex",
        "api_key": "Laptop",
        "subagents": tiers,
    }
    rows = [claude, *FakeService.harnesses(service)]
    service.harnesses = lambda: copy.deepcopy(rows)
    service.subagents = lambda: {
        "models": [("Served model", "served-id")],
        "tiers": dict(tiers),
    }
    service.save_subagent = lambda tier, model: service.calls.append(
        ("save_subagent", tier, model)
    )
    return service


def test_subagent_rows_open_only_from_claude_code_cells():
    async def run():
        app = RouterApp(subagent_service(), page="harnesses")
        async with app.run_test(size=(120, 32)) as pilot:
            await settle(app, pilot)
            table = app.screen.query_one("#harnesses", DataTable)
            assert table.row_count == 6
            table.focus()
            # Rows: Up/Down step over the four subagent rows.
            await pilot.press("down")
            assert table.cursor_row == 5
            await pilot.press("up")
            assert table.cursor_row == 0
            # Claude Code's cells reach them, on the model column only.
            await pilot.press("enter")
            await pilot.press("down")
            assert (table.cursor_row, table.cursor_column) == (1, 2)
            await pilot.press("left")
            assert table.cursor_column == 2
            await pilot.press("down", "down", "down", "down")
            assert table.cursor_row == 5
            # Another harness's cells step over them back to Claude Code.
            await pilot.press("up")
            assert table.cursor_row == 0
            await pilot.press("down", "down")
            await pilot.press("escape")
            assert (table.cursor_type, table.cursor_row) == ("row", 0)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("row", "label", "saved"),
    [
        (2, "Served model", ("save_subagent", "opus", "served-id")),
        (1, "Claude Code default", ("save_subagent", "sonnet", None)),
    ],
)
def test_subagent_model_cell_saves_one_tier(row, label, saved):
    async def run():
        service = subagent_service()
        app = RouterApp(service, page="harnesses")
        async with app.run_test(size=(120, 32)) as pilot:
            table = await open_cells(app, pilot)
            await pilot.press(*["down"] * row)
            await pilot.press("enter")
            await choose(app, pilot, label)
            await settle(app, pilot)
            assert service.calls == [saved]
            assert table.cursor_row == row

    asyncio.run(run())


def test_model_choices_show_aligned_input_output_and_cached_prices():
    choices = [("Claude Opus 5.5", "opus"), ("Grok", "grok"), ("Retired", "old")]
    # Router writes 0 for an unstated rate; sub-cent rates keep their digits.
    prices = {"opus": ("5", "25", "0.5"), "grok": ("0.075", "0.3", "0")}
    assert priced(choices, prices) == (
        [
            ("Claude Opus 5.5   $5.00  $25.00   $0.50", "opus"),
            ("Grok" + " " * 13 + "$0.075   $0.30       —", "grok"),
            ("Retired", "old"),
        ],
        "Model" + " " * 16 + "In     Out  Cached",
    )
    assert priced(choices, {}) == (choices, "")
    rates = {"input": "5", "output": "25", "cache_read_input": "0.5"}
    assert router._pricing({"pricing": rates}) == ("5", "25", "0.5")
    assert router._pricing({"pricing": {"input": 5}}) == (None, None, None)
    assert router._pricing(None) == (None, None, None)
