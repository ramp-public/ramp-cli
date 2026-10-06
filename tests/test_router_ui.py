"""Headless UI contracts and noninteractive compatibility. Never use real auth."""

import asyncio
import copy
import json
import subprocess
import sys
from decimal import Decimal
from threading import Event
from types import SimpleNamespace

import click
import httpx
import pytest
from click.testing import CliRunner
from textual.widgets import (
    Button,
    DataTable,
    Input,
)

from ramp_cli.commands import router
from ramp_cli.commands.router_user_strategies import ProfileDraft
from ramp_cli.main import cli
from ramp_cli.router_ui import launch
from ramp_cli.router_ui.app import (
    Confirm,
    CreateKeyPage,
    HarnessesPage,
    HomePage,
    KeyEditPage,
    KeysPage,
    Notice,
    RouterApp,
    RouterTabs,
    SecretPage,
)
from ramp_cli.router_ui.service import (
    RouterService,
    public_data,
    safe_message,
    safe_text,
)
from ramp_cli.router_ui.terminal import ColorReplyFilter


class FakeService:
    def __init__(self):
        self.client = SimpleNamespace(origin="https://app.router.com")
        self.calls = []
        self.invalidations = 0
        self.key_rows = [{"id": "key-1", "name": "Coding", "enabled": True}]
        self.profile_rows = [
            {
                "id": "default",
                "name": "Default",
                "is_default": True,
                "assigned_api_key_ids": ["key-1"],
                "auto_routing_enabled": None,
            },
            {
                "id": "cheap",
                "name": "Cheap",
                "is_default": False,
                "assigned_api_key_ids": [],
                "cost_efficient_routing_enabled": True,
            },
        ]

    def account(self, days=1, group_by="model"):
        return {"session": {"authenticated": False}, "origin": self.client.origin}

    def invalidate(self):
        self.invalidations += 1

    def prefetch_tabs(self):
        self.calls.append(("prefetch",))

    def harnesses(self):
        return [
            {
                "id": "codex",
                "name": "Codex",
                "detected": True,
                "configured": True,
                "default_model": "gpt-test",
                "routing_strategy": "Flex + Switchyard",
                "api_key": "Codex laptop",
            }
        ]

    def keys(self):
        return [
            {"spend_cap_editable": True, **row} for row in copy.deepcopy(self.key_rows)
        ]

    def key_profiles(self):
        return self.profiles()

    def profiles(self):
        return copy.deepcopy(self.profile_rows)

    def auto_routing_default(self):
        return False

    def key_details(self, key_id, days=7):
        return {
            "key": copy.deepcopy(self.key_rows[0]),
            "days": days,
            "strategies": {},
            "usage": {
                "summary": {"request_count": 10},
                "model_stats": [{"model": "test-model", "request_count": 10}],
            },
        }

    def daily_usage(self, days=7, group_by="model"):
        self.calls.append(("usage", days, group_by))
        return {
            "days": days,
            "group_by": group_by,
            "timezone": "UTC",
            "effective_end_at": "2026-10-03T14:05:00+00:00",
            "summary": {
                "spend_usd": "3.5",
                "cost_savings_usd": "1.25",
                "request_count": 4,
                "total_tokens": 900,
                "average_cost_per_request": "0.875",
                "error_count": 1,
                "p50_latency_ms": 820,
                "p95_latency_ms": 2400,
            },
            "byok_spend_usd": Decimal(0),
            "input_tokens": 800,
            "cached_input_tokens": 200,
            "models": [
                {
                    "id": model,
                    "label": label,
                    "spend_usd": Decimal(spend),
                    "request_count": requests,
                    "total_tokens": 100,
                }
                for model, label, spend, requests in (
                    ("gpt", "GPT Test", "3", 3),
                    ("mini", "Mini Test", "0.4", 1),
                    ("tiny", "Tiny Test", "0.1", 1),
                )
            ],
            "groups": [
                {
                    "id": "gpt",
                    "label": "GPT Test",
                    "spend_usd": Decimal("3"),
                    "request_count": 3,
                    "total_tokens": 600,
                },
                {
                    "id": "Other models",
                    "label": "Other models",
                    "spend_usd": Decimal("0.5"),
                    "request_count": 1,
                    "total_tokens": 300,
                },
            ],
            "series": [
                {
                    "date": f"2026-10-0{day}",
                    "spend_usd": Decimal(spend),
                    "groups": {
                        "gpt": {"spend_usd": Decimal(spend)},
                        "Other models": {"spend_usd": Decimal(0)},
                    },
                }
                for day, spend in ((1, "0"), (2, "3"))
            ]
            + [
                {
                    "date": "2026-10-03",
                    "spend_usd": Decimal("0.5"),
                    "groups": {
                        "gpt": {"spend_usd": Decimal(0)},
                        "Other models": {"spend_usd": Decimal("0.5")},
                    },
                }
            ],
        }

    def create_key(self, name, profile, request_id):
        self.calls.append(("create", name, profile, request_id))
        return {"id": "key-new", "name": name, "secret": "test-once-secret"}

    def mutate_key(self, key_id, action, value=""):
        self.calls.append((action, key_id, value))
        if action in ("lock", "unlock"):
            self.key_rows[0]["enabled"] = action == "unlock"
        elif action == "rename":
            self.key_rows[0]["name"] = value
        return {}

    def delete_key(self, key_id):
        self.calls.append(("delete", key_id, ""))
        self.key_rows = [k for k in self.key_rows if k["id"] != key_id]
        return {}

    def set_key_spend_cap(self, key_id, amount):
        self.calls.append(("spend-cap", key_id, amount))
        self.key_rows[0]["spend_cap_amount_usd"] = amount
        return {}

    def save_profile(self, draft):
        self.calls.append(("save-profile", copy.deepcopy(draft)))
        return {"id": "saved"}

    def delete_profile(self, profile_id):
        self.calls.append(("delete-profile", profile_id))

    def stored_keys(self):
        return getattr(self, "stored", [("test-stored-secret", ("codex",))])

    def cursor_connected(self):
        return getattr(self, "cursor", False)

    def claude_model_view(self):
        return "compact"

    def harness_editor(self, client):
        return {
            "models": [("First model", "first"), ("Second model", "second")],
            "model": getattr(self, "saved_model", "first"),
            "connection_token": "connection",
            "model_note": "",
        }

    def save_harness_model(self, client, model, expected):
        self.calls.append(("model", client, model, expected))
        self.saved_model = model
        return "Saved."

    def harness_routing(self, client):
        return {
            "payload": self.legacy_settings_for(""),
            "connection_token": "connection",
        }

    def save_harness_routing(self, client, changes, expected):
        self.calls.append(("routing", client, changes, expected))
        return {**self.legacy_settings_for(""), **changes}

    def connect(self, clients, **options):
        self.calls.append(("connect", clients, options))
        return "Connected Codex."

    def connect_with_new_key(self, clients, name, request_id, **options):
        self.calls.append(("connect-new-key", clients, name, request_id, options))
        return "Connected Codex."

    def harness_profiles(self, client):
        return {
            "profiles": [
                {"id": "p1", "name": "Default", "is_default": True, "is_current": True},
                {"id": "p2", "name": "Fast", "is_default": False, "is_current": False},
            ],
            "shared_with": ["claude-code"],
            "connection_token": "connection",
        }

    def select_harness_profile(self, client, name, expected):
        self.calls.append(("profile", client, name, expected))
        return {"routing_profile": {"id": "p2", "name": name}}

    def command(self, arguments, **kwargs):
        self.calls.append(("command", arguments))
        return "Completed."

    def subagents(self):
        return {
            "models": [("Served model", "served-id")],
            "tiers": dict.fromkeys(router.SUBAGENT_TIERS),
        }

    def legacy_settings_for(self, _secret):
        return {
            "allow_flex_tier_default": True,
            "switchyard_routing_enabled": False,
            "switchyard": {
                "efficient_models": [
                    {
                        "id": "model-1",
                        "display_name": "Model",
                        "efforts": ["low", "high"],
                    }
                ]
            },
        }

    def save_legacy_settings(self, secret, changes):
        self.calls.append(("legacy", secret, changes))
        return {**self.legacy_settings_for(secret), **changes}


async def settle(app, pilot):
    for _ in range(3):
        await app.workers.wait_for_complete()
        await pilot.pause()


@pytest.mark.parametrize(
    "page",
    [
        "home",
        "account",
        "harnesses",
        "keys",
        "strategies",
        "disconnect",
        "create-key",
        "legacy",
    ],
)
@pytest.mark.parametrize("size", [(120, 40), (80, 24)])
def test_every_screen_composes_and_remains_inside_one_app(page, size, monkeypatch):
    monkeypatch.setattr(router, "_clients_with_a_receipt", lambda: ("codex",))

    async def run():
        app = RouterApp(FakeService(), page=page)
        async with app.run_test(size=size) as pilot:
            await settle(app, pilot)
            assert not app.busy
            assert app.screen.query_one("#frame").region.width <= size[0]
            assert app.screen.query_one("#navigation").region.width <= size[0]
            assert len(app.screen_stack) == 2
            assert "None" not in app.screen.query_one("#frame").border_title
            tabs = app.screen.query_one("#navigation", RouterTabs)
            if page in ("home", "account"):
                # Signed out, Home preselects its centered Sign in.
                assert app.focused is app.screen.query_one("#home-login")
            else:
                assert app.focused is tabs
            assert tabs.active == f"nav-{app.screen.tab_section}"
            assert not app.workers

    asyncio.run(run())


def test_lock_requires_confirmation_and_cancel_sends_nothing():
    async def run():
        service = FakeService()
        app = RouterApp(service, page="key", options={"key_id": "key-1"})
        async with app.run_test(size=(100, 35)) as pilot:
            await settle(app, pilot)
            app.screen.lock_key()
            await pilot.pause()
            assert isinstance(app.screen, Confirm)
            await pilot.press("escape")
            await pilot.pause()
            assert service.calls == []
            app.screen.lock_key()
            await pilot.pause()
            await pilot.click("#accept")
            await settle(app, pilot)
            assert service.calls == [("lock", "key-1", "")]
            assert app.screen.query_one("#lock", Button).label.plain == "Unlock"

    asyncio.run(run())


def test_key_creation_secret_is_masked_and_cleared_on_leaving():
    async def run():
        service = FakeService()
        app = RouterApp(service, page="keys")
        async with app.run_test(size=(100, 35)) as pilot:
            await settle(app, pilot)
            app.push(CreateKeyPage())
            await settle(app, pilot)
            app.screen.query_one("#name", Input).value = "New key"
            app.screen.save()
            await settle(app, pilot)
            secret_page = app.screen
            assert isinstance(secret_page, SecretPage)
            assert secret_page.query_one("#secret", Input).password
            assert "secret" not in secret_page.key
            assert service.calls[0][:3] == ("create", "New key", "default")
            secret_page.reveal()
            assert not secret_page.query_one("#secret", Input).password
            app.back()
            await settle(app, pilot)
            assert secret_page.secret == ""
            assert secret_page.secret_input.value == ""
            assert isinstance(app.screen, KeysPage)

    asyncio.run(run())


def test_unsaved_changes_require_discard_on_back_navigation_and_quit():
    async def run():
        app = RouterApp(FakeService())
        async with app.run_test(size=(100, 35)) as pilot:
            await settle(app, pilot)
            app.push(
                KeyEditPage(
                    {"id": "key-1", "name": "Coding", "enabled": True}, "rename"
                )
            )
            await settle(app, pilot)
            page = app.screen
            page.query_one("#name", Input).value = "Unsaved"
            app.open("strategies")
            await pilot.pause()
            assert isinstance(app.screen, Confirm)
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is page
            assert app.screen.query_one("#name", Input).value == "Unsaved"
            await pilot.press("ctrl+q")
            await pilot.pause()
            assert isinstance(app.screen, Confirm)
            await pilot.press("escape")
            await pilot.pause()
            app.screen.action_back()
            await pilot.pause()
            await pilot.click("#accept")
            await settle(app, pilot)
            assert isinstance(app.screen, HomePage)

    asyncio.run(run())


def test_connect_requires_review_and_passes_secret_without_logging_it():
    async def run():
        service = FakeService()
        app = RouterApp(service, page="connect", options={"clients": ("codex",)})
        async with app.run_test(size=(100, 45)) as pilot:
            await settle(app, pilot)
            page = app.screen
            page.query_one("#api-key", Input).value = "test-pasted-secret"
            page.save()
            await pilot.pause()
            assert isinstance(app.screen, Confirm)
            assert service.calls == []
            await pilot.click("#accept")
            await settle(app, pilot)
            assert isinstance(app.screen, Notice)
            assert "Restart Codex" in str(
                app.screen.query_one("#notice-message").render()
            )
            await pilot.press("enter")
            await settle(app, pilot)
            assert isinstance(app.screen, HarnessesPage)
            assert service.calls[0][1] == ["codex"]
            assert service.calls[0][2]["secret"] == "test-pasted-secret"
            status = str(app.screen.query_one("#status").content)
            assert status == "Connected Codex."

    asyncio.run(run())


def test_background_work_is_responsive_and_cannot_be_abandoned():
    async def run():
        service = FakeService()
        app = RouterApp(service)
        release = Event()
        async with app.run_test(size=(100, 35)) as pilot:
            await settle(app, pilot)
            page = app.screen
            page.run("Writing", lambda: release.wait(3))
            await pilot.pause()
            try:
                assert app.busy
                await pilot.resize_terminal(80, 24)
                await pilot.press("escape", "ctrl+q")
                assert app.screen is page
                assert app.busy
                assert not app._exit
            finally:
                release.set()
            await settle(app, pilot)
            assert not app.busy

    asyncio.run(run())


def test_errors_stay_in_the_app_and_allow_retry():
    async def run():
        service = FakeService()
        service.keys = lambda: (_ for _ in ()).throw(click.ClickException("Offline"))
        app = RouterApp(service, page="keys")
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app, pilot)
            assert "Offline" in str(app.screen.query_one("#status").render())
            service.keys = lambda: service.key_rows
            app.screen.action_reload()
            await settle(app, pilot)
            assert app.screen.query_one("#table", DataTable).row_count == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "state",
    [
        {"agent_mode": True},
        {"no_input": True},
        {"quiet": True},
        {"format": "json"},
        {"config_format": "json"},
    ],
)
def test_machine_flags_never_launch_even_with_a_tty(monkeypatch, state):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    context = click.Context(click.Command("router"), obj=state)
    assert not launch.can_launch(context)


def test_interactive_dispatch_and_explicit_noninteractive_ui_error(monkeypatch):
    launches = []
    monkeypatch.setattr(launch, "can_launch", lambda _ctx: True)
    monkeypatch.setattr(RouterApp, "run", lambda app: launches.append(app.initial_page))
    result = CliRunner().invoke(cli, ["--human", "router"])
    assert result.exit_code == 0, result.output
    assert launches == ["home"]
    monkeypatch.setattr(launch, "can_launch", lambda _ctx: False)
    result = CliRunner().invoke(cli, ["--agent", "router", "ui"])
    assert result.exit_code == 2
    assert launches == ["home"]


def test_agent_status_is_still_json_and_ui_module_is_not_imported():
    result = CliRunner().invoke(cli, ["--agent", "router", "account", "status"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["schema_version"] == "1.0"
    # Check a fresh interpreter, since UI tests themselves import Textual.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from click.testing import CliRunner; from ramp_cli.main import cli; "
            "r=CliRunner().invoke(cli,['--agent','router','account','status']); "
            "assert r.exit_code == 0; assert 'textual' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_command_adapter_uses_same_interpreter_noninteractive_and_env_secret(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kwargs: (
            calls.append((args, kwargs))
            or SimpleNamespace(returncode=0, stdout="test-secret\nDone")
        ),
    )
    service = RouterService({"env": "production", "profile": "human"})
    output = service.command(["configure", "connect", "codex"], secret="test-secret")
    arguments, options = calls[0]
    assert arguments[0] == sys.executable
    assert "--no-input" in arguments
    assert "--human" in arguments
    assert "test-secret" not in arguments
    assert options["env"][router.CONFIGURE_KEY_ENV] == "test-secret"
    assert options["stdin"] == subprocess.DEVNULL
    assert "test-secret" not in output


def test_service_reuses_profile_save_and_validates_ownership(monkeypatch):
    service = RouterService({})
    fake = FakeService()
    monkeypatch.setattr(service, "profiles", fake.profiles)
    monkeypatch.setattr(service, "keys", fake.keys)
    draft = ProfileDraft.new()
    draft.name = "New"
    draft.keys = {"foreign"}
    with pytest.raises(click.BadParameter, match="no longer"):
        service.save_profile(draft)


def test_data_is_plain_terminal_text_and_credentials_are_removed():
    assert safe_text("\x1b[31mMalicious\nName") == "[31mMaliciousName"
    assert public_data(
        {"secret": "hidden", "data": {"refresh_token": "hidden", "name": "key"}}
    ) == {"data": {"name": "key"}}


def test_reduced_motion_freezes_routing_trace():
    async def run():
        app = RouterApp(FakeService())
        async with app.run_test(size=(100, 38)) as pilot:
            await settle(app, pilot)
            app.reduced_motion = True
            art = app.screen.query_one("#auth-animation")
            art.draw()
            first = list(art.strips)
            assert first
            art.started -= 100
            art.draw()
            assert art.strips == first

    asyncio.run(run())


def test_unknown_key_create_retries_same_inputs_with_same_id_only():
    async def run():
        service = FakeService()
        attempts = []

        def create(name, profile, request_id):
            attempts.append((name, profile, request_id))
            raise httpx.ReadTimeout("Response lost")

        service.create_key = create
        app = RouterApp(service, page="create-key")
        async with app.run_test(size=(100, 35)) as pilot:
            await settle(app, pilot)
            page = app.screen
            page.query_one("#name", Input).value = "First"
            page.save()
            await settle(app, pilot)
            page.save()
            await settle(app, pilot)
            assert attempts[0] == attempts[1]
            page.query_one("#name", Input).value = "Changed"
            page.save()
            await settle(app, pilot)
            assert len(attempts) == 2
            assert "earlier create" in str(page.query_one("#status").render())

    asyncio.run(run())


def test_login_progress_can_cancel_without_stdout_or_token_exchange(
    monkeypatch, capsys
):
    from ramp_cli.auth import oauth  # noqa: PLC0415
    from ramp_cli.client.router_user import RouterUserClient  # noqa: PLC0415

    calls = []
    callback = SimpleNamespace(
        redirect_uri="http://localhost/callback",
        state="state",
        code_challenge="challenge",
        _event=Event(),
        shutdown=lambda: calls.append("shutdown"),
    )
    monkeypatch.setattr(oauth, "start_pkce_callback", lambda: callback)
    monkeypatch.setattr(oauth, "_open_browser", lambda _url: True)
    monkeypatch.setattr(RouterUserClient, "_edge_protected", lambda _self: False)
    monkeypatch.setattr(
        httpx.Client,
        "post",
        lambda *_a, **_kw: pytest.fail("Exchanged a cancelled grant"),
    )
    cancel = Event()
    cancel.set()
    messages = []
    with pytest.raises(click.ClickException, match="cancelled"):
        RouterUserClient("https://app.router.com").login(
            progress=messages.append, cancel=cancel
        )
    assert messages == ["Approve the sign-in in your browser to continue..."]
    assert calls == ["shutdown"]
    assert capsys.readouterr() == ("", "")


def test_ui_account_login_cancel_keeps_navigation_alive():
    async def run():
        service = FakeService()

        def login(_origin, progress, cancel, **kwargs):
            progress("Waiting in your browser")
            cancel.wait(3)
            raise click.ClickException("Cancelled")

        service.login = login
        app = RouterApp(service, page="account")
        async with app.run_test(size=(100, 35)) as pilot:
            await settle(app, pilot)
            page = app.screen
            page.login()
            await pilot.pause()
            assert app.busy
            assert not page.query_one("#cancel-login", Button).disabled
            page.cancel_login()
            await settle(app, pilot)
            assert not app.busy
            assert not page.query_one("#login", Button).disabled
            app.open("keys")
            await settle(app, pilot)
            assert isinstance(app.screen, KeysPage)

    asyncio.run(run())


def test_cursor_deployment_overrides_are_validated_and_kept_out_of_shell(monkeypatch):
    service = RouterService({}, origin="https://app.example")
    seen = []
    monkeypatch.setattr(
        service,
        "command",
        lambda arguments, **kwargs: seen.append((arguments, kwargs)) or "Done",
    )
    service.connect(
        ["cursor"],
        secret="key",
        base_url="https://app.example/v1",
        ui_url="https://app.example",
    )
    assert seen[0][0] == ["cursor"]
    assert seen[0][1]["variables"] == {
        "RAMP_ROUTER_BASE_URL": "https://app.example/v1",
        "RAMP_ROUTER_UI_URL": "https://app.example",
    }
    with pytest.raises(click.BadParameter):
        service.connect(["cursor"], secret="key", base_url="http://evil.example/v1")
    assert len(seen) == 1


def test_messages_keep_indentation_but_never_replay_terminal_controls():
    assert safe_message("\x1b[31mError\x1b[0m\n  Details\x07") == "Error\n  Details"


def test_hidden_login_origin_and_browserless_options_are_preserved():
    async def run():
        service = FakeService()
        calls = []
        service.login = lambda origin, progress, cancel, on_callback, **options: (
            calls.append((origin, options))
        )
        app = RouterApp(
            service,
            page="account",
            options={
                "origin": "https://router.example",
                "no_browser": True,
                "timeout": 23,
                "sign_in": True,
            },
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app, pilot)
            # An explicit sign-in authorizes afresh rather than reuse a grant.
            assert calls == [
                (
                    "https://router.example",
                    {"no_browser": True, "timeout": 23, "reuse": False},
                )
            ]
            assert not app.screen.query("#origin")

    asyncio.run(run())


@pytest.mark.parametrize(
    "arguments",
    [
        ["ui", "--height", "18"],
        ["ui", "--inline", "--height", "0"],
        ["ui", "--inline", "--height", "100"],
        ["--height", "18"],
        ["--inline", "--height", "100"],
    ],
)
def test_invalid_inline_options_are_usage_errors(arguments):
    result = CliRunner().invoke(cli, ["router", *arguments])
    assert result.exit_code == 2, result.output


def test_agent_inline_request_never_imports_or_runs_the_ui(monkeypatch):
    monkeypatch.setattr(RouterApp, "run", lambda *a, **kw: pytest.fail("Opened a TUI"))
    result = CliRunner().invoke(cli, ["--agent", "router", "ui", "--inline"])
    assert result.exit_code == 2, result.output


def test_windows_inline_is_rejected_instead_of_silently_using_full_screen(monkeypatch):
    monkeypatch.setattr(launch, "can_launch", lambda _ctx: True)
    monkeypatch.setattr(launch.sys, "platform", "win32")
    result = CliRunner().invoke(cli, ["--human", "router", "ui", "--inline"])
    assert result.exit_code == 2, result.output
    assert "Omit --inline on Windows" in result.output


# Terminal color replies never leak into keyboard input.


@pytest.mark.parametrize("end", ["\x07", "\x1b\\"])
def test_fragmented_color_replies_never_become_keys_and_other_input_survives(end):
    seen = []
    reader = ColorReplyFilter(lambda code, color: seen.append((code, color)))
    text = (
        "abc\x1b[A\x1b]11;rgb:ffff/ffff/ffff"
        + end
        + "\x1b]10;rgb:0000/0000/0000"
        + end
        + "界\r"
    )
    result = "".join(reader.feed(char) for char in text) + reader.flush(force=True)
    assert result == "abc\x1b[A界\r"
    assert seen == [(11, "#ffffff"), (10, "#000000")]


def test_delayed_reply_is_not_flushed_as_keyboard_input_and_can_complete():
    seen = []
    reader = ColorReplyFilter(lambda *args: seen.append(args))
    assert reader.feed("\x1b]11;rgb:ffff/") == ""
    reader.pending_since -= 10
    assert reader.flush() == ""
    assert reader.feed("ffff/ffff\x1b\\x") == "x"
    assert seen == [(11, "#ffffff")]
    assert reader.feed("\x1b]10;") == ""
    assert reader.flush(force=True) == ""


@pytest.mark.parametrize("end", ["\x07", "\x1b\\"])
def test_oversized_or_broken_replies_are_discarded_and_navigation_resynchronizes(end):
    reader = ColorReplyFilter(
        lambda *args: pytest.fail("malformed reply cannot change theme")
    )
    assert reader.feed("\x1b]11;" + "x" * 256) == ""
    assert reader.feed("more" + end + "\x1b[C") == "\x1b[C"
    assert reader.feed("\x1b]10;broken\x1b[A") == "\x1b[A"
    assert reader.feed("\x1b]11;" + "x" * 256) == ""
    assert reader.feed("\x1b") == ""
    reader.pending_since -= 10
    assert reader.flush() == "\x1b"


def test_pasted_osc_text_unknown_protocols_and_escape_keys_are_preserved():
    seen = []
    reader = ColorReplyFilter(lambda *args: seen.append(args))
    paste = "\x1b[200~literal \x1b]11;rgb:ffff/ffff/ffff\x07 text\x1b[201~"
    unknown = "\x1b]52;clipboard\x07"
    result = "".join(reader.feed(char) for char in paste + unknown) + reader.flush(
        force=True
    )
    assert result == paste + unknown
    assert not seen
    assert reader.feed("\x1b") == ""
    assert reader.flush(force=True) == "\x1b"
