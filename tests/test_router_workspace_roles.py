"""Workspace roles: admin-issued, suspended and budget-locked keys, key offers,
and the admin's suggested harness models."""

import asyncio

import click
import pytest
from textual.widgets import Button, DataTable, OptionList, SelectionList

from ramp_cli.commands.router_user_strategies import ProfileDraft
from ramp_cli.router_ui import service as service_module
from ramp_cli.router_ui.app import (
    CellMenu,
    ChoiceDialog,
    Confirm,
    KeysPage,
    RouterApp,
    SecretPage,
    StrategiesPage,
)
from ramp_cli.router_ui.service import RouterService, key_restriction
from tests.test_router_harness_cells import open_cells as open_harness_cells
from tests.test_router_key_cells import open_cell
from tests.test_router_strategy_cells import open_cells as open_strategy_cells
from tests.test_router_ui import FakeService, settle


def status(page) -> str:
    return str(page.query_one("#status").content)


MANAGED = {
    "id": "key-1",
    "name": "Issued",
    "enabled": True,
    "managed_by_workspace_admin": True,
}


def locked(reason: str) -> dict:
    return {"name": "K", "enabled": False, "disabled_reason": reason}


@pytest.mark.parametrize(
    ("key", "action", "reason"),
    [
        (MANAGED, "rename", "issued by your organization's admin"),
        (MANAGED, "set-strategy", "issued by your organization's admin"),
        (locked("member_budget"), "unlock", "organization budget is used up"),
        (locked("workspace_admin_suspended"), "unlock", "suspended by your"),
        (locked("business_access_revoked"), "unlock", "no longer have access"),
        (locked("spend_cap"), "unlock", "spend cap"),
    ],
)
def test_key_restriction_names_why_router_refuses(key, action, reason):
    assert reason in key_restriction(key, action)


@pytest.mark.parametrize(
    ("key", "action"),
    [
        ({"name": "K", "enabled": True}, "lock"),
        # Revoked access still lets the owner tidy up the key itself.
        (locked("business_access_revoked"), "rename"),
    ],
)
def test_key_restriction_allows_what_router_allows(key, action):
    assert key_restriction(key, action) is None


def test_service_refuses_owner_mutations_on_admin_issued_keys(monkeypatch):
    sent = []
    service = RouterService.__new__(RouterService)
    service._client = type(
        "Client",
        (),
        {
            "post": lambda self, path, **kwargs: sent.append(path),
            "patch": lambda self, path, **kwargs: sent.append(path),
            "delete": lambda self, path: sent.append(path),
        },
    )()
    service.invalidate = lambda: None
    monkeypatch.setattr(service_module, "_owned_key", lambda _client, _id: MANAGED)
    with pytest.raises(click.ClickException, match="organization's admin"):
        service.mutate_key("key-1", "rename", "New")
    with pytest.raises(click.ClickException, match="organization's admin"):
        service.delete_key("key-1")
    assert sent == []


def test_strategy_edits_never_move_admin_issued_keys(monkeypatch):
    service = RouterService.__new__(RouterService)
    service.invalidate = lambda: None
    service.profiles = lambda: [
        {"id": "default", "is_default": True},
        {"id": "cheap", "assigned_api_key_ids": ["key-1"]},
    ]
    service.keys = lambda: [MANAGED, {"id": "key-2", "name": "Mine"}]
    for name in ("save_draft", "delete_profile"):
        monkeypatch.setattr(
            service_module,
            name,
            lambda *args: pytest.fail("nothing is sent when a managed key moves"),
        )
    # Saving a strategy that takes the key, before any write...
    draft = ProfileDraft.new()
    draft.name = "Cheap"
    draft.keys = {"key-1", "key-2"}
    with pytest.raises(click.BadParameter, match="organization's admin"):
        RouterService.save_profile.__wrapped__(service, draft)
    # ...and deleting one that holds it, which moves it to the default.
    with pytest.raises(click.BadParameter, match="Ask your admin to move it"):
        RouterService.delete_profile.__wrapped__(service, "cheap")


def grants_service(pages, claimed=None):
    gets, posts = [], []

    class Client:
        def get(self, path, *, params=None, **kwargs):
            gets.append((path, params))
            page = pages[len(gets) - 1]
            if isinstance(page, Exception):
                raise page
            return page

        def post(self, path, *, json, **kwargs):
            posts.append((path, json))
            if isinstance(claimed, Exception):
                raise claimed
            return claimed or {"id": "new", "name": "Issued", "secret": "once"}

    service = RouterService.__new__(RouterService)
    service._client = Client()
    service.invalidate = lambda: None
    return service, gets, posts


ONE_OFFER = {"data": [{"id": "g1", "status": "pending"}], "has_more": False}


def test_key_grants_page_through_and_keep_only_pending_offers():
    service, gets, _ = grants_service(
        [
            {
                "data": [{"id": "a", "status": "pending"}],
                "has_more": True,
                "next_cursor": "c1",
            },
            {
                "data": [
                    {"id": "b", "status": "expired"},
                    {"id": "c", "status": "pending"},
                ],
                "has_more": False,
                "next_cursor": None,
            },
        ]
    )
    grants = RouterService.key_grants.__wrapped__(service)
    assert [grant["id"] for grant in grants] == ["a", "c"]
    assert gets == [
        ("/client/api-key-grants", {"limit": 100}),
        ("/client/api-key-grants", {"limit": 100, "cursor": "c1"}),
    ]


@pytest.mark.parametrize(
    "page",
    # Routers without offers, and answers that make no sense, read as none.
    [service_module.ApiError(404, "Not found"), {"data": 7, "has_more": False}],
)
def test_key_grants_fail_open(page):
    service, _, _ = grants_service([page])
    assert RouterService.key_grants.__wrapped__(service) == []


def test_claim_posts_to_the_offer_and_rejects_a_stale_one():
    service, _, posts = grants_service([ONE_OFFER, ONE_OFFER])
    assert RouterService.claim_grant.__wrapped__(service, "g1")["secret"] == "once"
    assert posts == [("/client/api-key-grants/g1/claim", {})]
    with pytest.raises(click.BadParameter, match="no longer available"):
        RouterService.claim_grant.__wrapped__(service, "gone")


def test_claim_with_a_lost_answer_says_it_may_have_gone_through():
    failure = service_module.ApiError(502, "Bad gateway")
    service, _, _ = grants_service([ONE_OFFER], claimed=failure)
    with pytest.raises(service_module.RampCLIError, match="may have gone through"):
        RouterService.claim_grant.__wrapped__(service, "g1")


def managed_service():
    service = FakeService()
    service.key_rows = [dict(MANAGED)]
    return service


def test_key_cells_skip_what_cant_be_changed():
    async def run():
        service = FakeService()
        service.key_rows.append(
            {"id": "org", "name": "Org", "enabled": True, "spend_cap_editable": False}
        )
        app = RouterApp(service, page="keys")
        async with app.run_test(size=(120, 32)) as pilot:
            table = await open_cell(app, pilot, 0)
            await pilot.press("escape")
            # Your own key: the cursor stops at its spend cap, never at spend
            # or the creation date.
            await pilot.press(*["right"] * 6)
            assert table.cursor_coordinate.column == 3
            # An organization key you can't cap stops at its routing profile.
            await pilot.press("down", *["right"] * 6)
            assert table.cursor_coordinate == (1, 2)

    asyncio.run(run())


@pytest.mark.parametrize("column", [0, 1, 2])
def test_admin_issued_key_cells_explain_instead_of_offering_changes(column):
    async def run():
        service = managed_service()
        app = RouterApp(service, page="keys")
        async with app.run_test(size=(120, 32)) as pilot:
            await open_cell(app, pilot, column)
            assert isinstance(app.screen, KeysPage)
            assert "issued by your organization's admin" in status(app.screen)
            assert service.calls == []

    asyncio.run(run())


def test_key_page_disables_actions_on_an_admin_issued_key():
    async def run():
        service = managed_service()
        app = RouterApp(service, page="keys")
        async with app.run_test(size=(120, 32)) as pilot:
            await settle(app, pilot)
            app.screen.open_entry("key-1")
            await settle(app, pilot)
            for button in ("#rename", "#assignment", "#lock"):
                assert app.screen.query_one(button, Button).disabled
            assert "issued by your organization's admin" in status(app.screen)

    asyncio.run(run())


def test_claiming_the_one_offer_confirms_then_shows_its_secret_once():
    async def run():
        service = FakeService()
        service.grant_rows = [
            {"id": "g1", "name": "Team key", "status": "pending"},
        ]
        app = RouterApp(service, page="keys")
        async with app.run_test(size=(120, 32)) as pilot:
            await settle(app, pilot)
            assert "offered you a key" in status(app.screen)
            claim = app.screen.query_one("#claim", Button)
            # It takes the key hints' place beside the status.
            assert claim.display and claim.parent.id == "status-row"
            assert not app.screen.query_one("#keys").display
            claim.press()
            await settle(app, pilot)
            assert isinstance(app.screen, Confirm)
            assert "only they can change them" in app.screen.message
            # Claim is the default, so Enter accepts the offer.
            assert app.focused.id == "accept"
            await pilot.press("enter")
            await settle(app, pilot)
            assert service.calls == [("claim", "g1")]
            assert isinstance(app.screen, SecretPage)
            assert app.screen.secret == "test-claimed-secret"
            # Copy is the default, so Enter copies.
            assert app.focused.id == "copy"
            warning = str(app.screen.query_one("#secret-warning").content)
            assert "shown only once" in warning
            app.back()
            await settle(app, pilot)
            assert isinstance(app.screen, KeysPage)
            assert not app.screen.query_one("#claim", Button).display
            assert app.screen.query_one("#keys").display
            rows = app.screen.query_one("#table", DataTable).row_count
            assert rows == 2

    asyncio.run(run())


def test_the_secret_page_never_leaves_on_an_arrow():
    async def run():
        service = FakeService()
        app = RouterApp(service, page="keys")
        async with app.run_test(size=(120, 32)) as pilot:
            await settle(app, pilot)
            app.push(SecretPage({"id": "k", "name": "K", "secret": "s"}))
            await settle(app, pilot)
            page = app.screen
            for target in (page.query_one("#copy"), page.query_one("#navigation")):
                target.focus()
                for key in ("left", "right", "right"):
                    await pilot.press(key)
                    await settle(app, pilot)
                    assert app.screen is page
            # Choosing another tab still can, once confirmed.
            app.open("home")
            await settle(app, pilot)
            assert isinstance(app.screen, Confirm)
            assert "won't be shown again" in app.screen.message

    asyncio.run(run())


def test_claim_button_leaves_with_the_offer_message_and_comes_back():
    async def run():
        service = managed_service()
        service.grant_rows = [{"id": "g1", "name": "Team", "status": "pending"}]
        app = RouterApp(service, page="keys")
        async with app.run_test(size=(120, 32)) as pilot:
            await settle(app, pilot)
            claim = app.screen.query_one("#claim", Button)
            assert claim.display and str(claim.label) == "Claim Key"
            # A cell's explanation replaces the offer, so the button goes too.
            await open_cell(app, pilot, 1)
            assert "issued by your organization's admin" in status(app.screen)
            assert not claim.display
            assert app.screen.query_one("#keys").display
            # Back to rows: the offer and its button return.
            await pilot.press("escape")
            await settle(app, pilot)
            assert "offered you a key" in status(app.screen)
            assert claim.display

    asyncio.run(run())


def test_several_offers_are_chosen_from_a_list():
    async def run():
        service = FakeService()
        service.grant_rows = [
            {"id": "g1", "name": "First", "status": "pending"},
            {"id": "g2", "name": "Second", "status": "pending"},
        ]
        app = RouterApp(service, page="keys")
        async with app.run_test(size=(120, 32)) as pilot:
            await settle(app, pilot)
            assert "offered you 2 keys" in status(app.screen)
            assert str(app.screen.query_one("#claim", Button).label) == "Claim Keys"
            app.screen.query_one("#claim", Button).press()
            await settle(app, pilot)
            assert isinstance(app.screen, ChoiceDialog)
            menu = app.screen.query_one(OptionList)
            labels = [
                str(menu.get_option_at_index(i).prompt)
                for i in range(menu.option_count)
            ]
            assert labels == ["First · No Cap", "Second · No Cap", "Back"]
            menu.highlighted = 1
            await pilot.press("enter")
            await settle(app, pilot)
            assert isinstance(app.screen, Confirm)
            assert "Second" in app.screen.message
            assert service.calls == []

    asyncio.run(run())


def test_a_failed_claim_explains_and_rereads_the_offers():
    async def run():
        service = FakeService()
        service.grant_rows = [
            {"id": "g1", "name": "First", "status": "pending"},
            {"id": "g2", "name": "Second", "status": "pending"},
        ]

        def claim(grant_id):
            # Claimed elsewhere meanwhile: Router has only the other offer left.
            service.grant_rows = [g for g in service.grant_rows if g["id"] != grant_id]
            raise click.BadParameter("That key offer is no longer available.")

        service.claim_grant = claim
        app = RouterApp(service, page="keys")
        async with app.run_test(size=(120, 32)) as pilot:
            await settle(app, pilot)
            app.screen.confirm_claim(service.grant_rows[0])
            await settle(app, pilot)
            await pilot.press("enter")
            await settle(app, pilot)
            assert isinstance(app.screen, KeysPage)
            assert "no longer available" in status(app.screen)
            assert [g["id"] for g in app.screen.grants] == ["g2"]

    asyncio.run(run())


def test_a_member_with_no_keys_yet_can_claim_their_first():
    async def run():
        service = FakeService()
        service.key_rows = []
        service.grant_rows = [{"id": "g1", "name": "Team", "status": "pending"}]
        app = RouterApp(service, page="keys")
        async with app.run_test(size=(120, 32)) as pilot:
            await settle(app, pilot)
            assert "offered you a key" in status(app.screen)
            assert app.screen.query_one("#claim", Button).display

    asyncio.run(run())


def test_a_failed_reread_still_shows_why_the_claim_failed():
    async def run():
        service = FakeService()
        service.grant_rows = [{"id": "g1", "name": "Team", "status": "pending"}]
        app = RouterApp(service, page="keys")
        async with app.run_test(size=(120, 32)) as pilot:
            await settle(app, pilot)

            def offline():
                raise service_module.RampCLIError("offline")

            def claim(grant_id):
                service.keys = offline
                raise service_module.RampCLIError("The claim may have gone through.")

            service.claim_grant = claim
            app.screen.confirm_claim(service.grant_rows[0])
            await settle(app, pilot)
            await pilot.press("enter")
            await settle(app, pilot)
            assert "may have gone through" in status(app.screen)

    asyncio.run(run())


def test_strategy_checklist_keeps_admin_issued_keys_where_they_are():
    async def run():
        service = FakeService()
        service.key_rows.append({**MANAGED, "id": "key-2"})
        app = RouterApp(service, page="strategies")
        async with app.run_test(size=(120, 32)) as pilot:
            await settle(app, pilot)
            await open_strategy_cells(app, pilot, column=4)
            # The non-default strategy, so only the managed key is locked.
            await pilot.press("down")
            await pilot.press("enter")
            await settle(app, pilot)
            checklist = app.screen.query_one(SelectionList)
            options = [
                checklist.get_option_at_index(i) for i in range(checklist.option_count)
            ]
            assert [option.disabled for option in options] == [False, True]
            assert "Set by your admin" in str(options[1].prompt)
            await pilot.press("escape")
            await settle(app, pilot)
            assert isinstance(app.screen, StrategiesPage)
            assert not service.calls

    asyncio.run(run())


def test_model_cell_marks_the_organization_default():
    async def run():
        service = FakeService()
        original = service.harness_editor
        service.harness_editor = lambda client: {
            **original(client),
            "organization_default": "second",
        }
        app = RouterApp(service, page="harnesses")
        async with app.run_test(size=(120, 32)) as pilot:
            await open_harness_cells(app, pilot, column=2)
            await pilot.press("enter")
            await settle(app, pilot)
            assert isinstance(app.screen, CellMenu)
            menu = app.screen.query_one(OptionList)
            labels = [
                str(menu.get_option_at_index(i).prompt)
                for i in range(menu.option_count)
            ]
            assert "Second model · Org default" in labels
            assert "Your organization suggests Second model." in status(
                app.screen_stack[-2]
            )
            assert not service.calls

    asyncio.run(run())
