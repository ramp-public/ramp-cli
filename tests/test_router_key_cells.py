"""Editing an API key row's cells in place: status, routing profile, spend cap."""

import asyncio

import pytest
from textual.widgets import DataTable, Input, OptionList

from ramp_cli.router_ui import service as service_module
from ramp_cli.router_ui.app import (
    CellInput,
    CellMenu,
    ChoiceDialog,
    Confirm,
    RouterApp,
    parse_spend_cap,
)
from ramp_cli.router_ui.service import RouterService
from tests.test_router_ui import FakeService, settle


async def open_cell(app, pilot, column):
    await settle(app, pilot)
    table = app.screen.query_one("#table", DataTable)
    table.focus()
    await pilot.press("enter")
    for _ in range(column):
        await pilot.press("right")
    assert table.cursor_coordinate.column == column
    await pilot.press("enter")
    await settle(app, pilot)
    return table


def menu_labels(app):
    menu = app.screen.query_one(OptionList)
    return [str(menu.get_option_at_index(i).prompt) for i in range(menu.option_count)]


async def choose(app, pilot, label):
    assert isinstance(app.screen, CellMenu)
    app.screen.query_one(OptionList).highlighted = menu_labels(app).index(label)
    await pilot.press("enter")
    await pilot.pause()


def test_name_cell_deletes_only_after_confirmation():
    async def run():
        service = FakeService()
        app = RouterApp(service, page="keys")
        async with app.run_test(size=(120, 32)) as pilot:
            await open_cell(app, pilot, 0)
            await choose(app, pilot, "Delete Key?")
            await settle(app, pilot)
            assert isinstance(app.screen, Confirm)
            assert "cannot be undone" in app.screen.message
            # Cancel is focused, so a stray Enter keeps the key.
            await pilot.press("enter")
            await settle(app, pilot)
            assert service.calls == []
            await pilot.press("enter")
            await settle(app, pilot)
            await choose(app, pilot, "Delete Key?")
            await settle(app, pilot)
            await pilot.click("#accept")
            await settle(app, pilot)
            assert service.calls == [("delete", "key-1", "")]
            assert app.screen.query_one("#table", DataTable).row_count == 0

    asyncio.run(run())


def choice_labels(app):
    assert isinstance(app.screen, ChoiceDialog)
    menu = app.screen.query_one(OptionList)
    return [str(menu.get_option_at_index(i).prompt) for i in range(menu.option_count)]


async def answer(app, pilot, label):
    app.screen.query_one(OptionList).highlighted = choice_labels(app).index(label)
    await pilot.press("enter")
    await settle(app, pilot)


def key_in_use():
    service = FakeService()
    service.key_rows[0]["last_four"] = "1234"
    service.key_rows.append({"id": "key-2", "name": "Spare", "last_four": "9999"})
    service.stored = [
        ("secret-1234", ("codex", "claude-code")),
        ("secret-9999", ("opencode",)),
    ]
    return service


def test_deleting_a_key_in_use_alerts_and_backing_out_keeps_everything():
    async def run():
        service = key_in_use()
        app = RouterApp(service, page="keys")
        async with app.run_test(size=(120, 40)) as pilot:
            await open_cell(app, pilot, 0)
            await choose(app, pilot, "Delete Key?")
            await settle(app, pilot)
            message = app.screen.message
            assert "Codex" in message and "Claude Code" in message
            assert "Coding" in message
            assert choice_labels(app)[0] == "Switch to another key"
            assert choice_labels(app)[-1] == "Back"
            await answer(app, pilot, "Back")
            assert not isinstance(app.screen, (ChoiceDialog, Confirm))
            assert service.calls == []

    asyncio.run(run())


def test_service_delete_key_sends_delete(monkeypatch):
    sent = []
    service = RouterService.__new__(RouterService)
    service._client = type(
        "Client", (), {"delete": lambda self, path: sent.append(path)}
    )()
    service.invalidate = lambda: None
    monkeypatch.setattr(service_module, "_owned_key", lambda _client, _id: {"id": "k"})
    service.delete_key("k")
    assert sent == ["/client/api-keys/k"]


def test_spend_cap_back_sends_nothing_and_blank_removes_the_cap():
    async def run():
        service = FakeService()
        service.key_rows[0]["spend_cap_amount_usd"] = "25"
        app = RouterApp(service, page="keys")
        async with app.run_test(size=(120, 32)) as pilot:
            await open_cell(app, pilot, 3)
            await pilot.click("#cell-back")
            await pilot.pause()
            assert not isinstance(app.screen, CellInput)
            await pilot.press("enter")
            await settle(app, pilot)
            await pilot.press("escape")
            await pilot.pause()
            assert service.calls == []
            await pilot.press("enter")
            await settle(app, pilot)
            app.screen.query_one("#cell-value", Input).value = ""
            await pilot.press("enter")
            await pilot.pause()
            assert "Remove" in app.screen.message
            await pilot.click("#accept")
            await settle(app, pilot)
            assert service.calls == [("spend-cap", "key-1", None)]
            assert (
                str(app.screen.query_one("#table", DataTable).get_row_at(0)[3])
                == "No Cap"
            )

    asyncio.run(run())


def admin_service(monkeypatch, keys, admin_of=()):
    gets, patches = [], []

    def get(_self, path):
        gets.append(path)
        if path.startswith("/admin/workspaces/"):
            if path.rsplit("/", 1)[1] not in admin_of:
                raise service_module.ApiError(404, "Not found")
            return {"id": path.rsplit("/", 1)[1]}
        raise AssertionError(path)

    service = RouterService.__new__(RouterService)
    service._client = type(
        "Client",
        (),
        {"get": get, "patch": lambda self, path, json: patches.append((path, json))},
    )()
    service.invalidate = lambda: None
    monkeypatch.setattr(service_module, "_all_keys", lambda _client: keys)
    monkeypatch.setattr(service_module, "_routing_profiles", lambda _client: [])
    monkeypatch.setattr(
        service_module,
        "_owned_key",
        lambda _client, key_id: next(k for k in keys if k["id"] == key_id),
    )
    return service, gets, patches


def test_service_marks_organization_caps_editable_only_for_its_admins(monkeypatch):
    keys = [
        {"id": "mine", "core_business_uuid": None},
        {"id": "org", "core_business_uuid": "biz-admin"},
        {"id": "other", "core_business_uuid": "biz-member"},
    ]
    service, gets, _ = admin_service(monkeypatch, keys, admin_of={"biz-admin"})
    rows = RouterService.keys.__wrapped__(service)
    assert {row["id"]: row["spend_cap_editable"] for row in rows} == {
        "mine": True,
        "org": True,
        "other": False,
    }
    assert sorted(gets) == [
        "/admin/workspaces/biz-admin",
        "/admin/workspaces/biz-member",
    ]


def test_service_sets_organization_caps_through_the_admin_api(monkeypatch):
    keys = [
        {
            "id": "org",
            "core_business_uuid": "biz",
            "spend_cap_amount_usd": "5",
            "spend_cap_frequency": "monthly",
        }
    ]
    service, _, patches = admin_service(monkeypatch, keys, admin_of={"biz"})
    service.set_key_spend_cap("org", "20")
    service.set_key_spend_cap("org", None)
    assert patches == [
        (
            "/admin/workspaces/biz/api-keys/org/spend-cap",
            {"spend_cap_amount_usd": "20", "spend_cap_frequency": "monthly"},
        ),
        (
            "/admin/workspaces/biz/api-keys/org/spend-cap",
            {"spend_cap_amount_usd": None, "spend_cap_frequency": None},
        ),
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("", None), ("  ", None), ("10", "10"), ("$1,000.50", "1000.5"), ("1e2", "100")],
)
def test_parse_spend_cap(text, expected):
    assert parse_spend_cap(text) == expected


@pytest.mark.parametrize(
    "text", ["0", "-5", "abc", "nan", "12345678901", "0.12345678901"]
)
def test_parse_spend_cap_rejects(text):
    with pytest.raises(ValueError):
        parse_spend_cap(text)


@pytest.mark.parametrize(
    ("key", "amount", "body"),
    [
        (
            {"spend_cap_amount_usd": "5", "spend_cap_frequency": "monthly"},
            "20",
            {"spend_cap_amount_usd": "20", "spend_cap_frequency": "monthly"},
        ),
        (
            {"spend_cap_amount_usd": None},
            "20",
            {"spend_cap_amount_usd": "20", "spend_cap_frequency": "lifetime"},
        ),
        (
            {"spend_cap_amount_usd": "5", "spend_cap_frequency": "daily"},
            None,
            {"spend_cap_amount_usd": None, "spend_cap_frequency": None},
        ),
        ({"lifetime_spend_cap_usd": "5"}, "9", {"lifetime_spend_cap_usd": "9"}),
    ],
)
def test_service_spend_cap_keeps_the_frequency(monkeypatch, key, amount, body):
    sent = []
    service = RouterService.__new__(RouterService)
    service._client = type(
        "Client", (), {"patch": lambda self, path, json: sent.append((path, json))}
    )()
    service.invalidate = lambda: None
    monkeypatch.setattr(
        service_module, "_owned_key", lambda _client, _id: {"id": "k", **key}
    )
    service.set_key_spend_cap("k", amount)
    assert sent == [("/client/api-keys/k", body)]
