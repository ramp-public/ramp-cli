"""Router-native credential lifecycle, isolated from Core and inference keys."""

import base64
import io
import json
import os
import sys
import time
from types import SimpleNamespace

import click
import httpx
import pytest
from click.testing import CliRunner

from ramp_cli.auth import oauth
from ramp_cli.client.router_user import (
    RouterUserClient,
    _atomic_write,
    _target_path,
    selected_origin,
    validate_origin,
)
from ramp_cli.commands import router_user
from ramp_cli.commands.router_user import _menu_footer
from ramp_cli.errors import EXIT_AUTH_REQUIRED, ApiError, RampCLIError
from ramp_cli.main import cli
from ramp_cli.output.style import show_detail_card


def credentials(**changes):
    return {
        "origin": "http://localhost:3080",
        "access_token": "access-old",
        "refresh_token": "refresh-old",
        "token_type": "Bearer",
        "expires_in": 900,
        "refresh_token_expires_in": 3600,
        "issued_at": int(time.time()) - 1000,
        "user": {"id": "owner", "email": "owner@example.com"},
        **changes,
    }


def token_response(**changes):
    return httpx.Response(
        200,
        json={
            **credentials(access_token="access-new", refresh_token="refresh-new"),
            **changes,
        },
    )


def jwt_token(user="owner", business=None, nonce="initial"):
    payload = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "sub": user,
                    "rampBusinessUuid": business,
                    "nonce": nonce,
                }
            ).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    return f"e30.{payload}.signature"


@pytest.mark.parametrize("configured_origin", [None, "matching", "other"])
def test_access_token_is_origin_bound_across_native_lifecycle(
    monkeypatch, capsys, configured_origin
):
    origin = "https://example-router.internal.dev-deploys.eks.dev.ramp.engineering"
    edge_token = "private-access-token"
    if configured_origin:
        monkeypatch.setenv(
            "RAMP_ROUTER_ACCESS_ORIGIN",
            origin + "/"
            if configured_origin == "matching"
            else "https://other.example",
        )
        monkeypatch.setenv("RAMP_ROUTER_ACCESS_TOKEN", edge_token)
    callback = SimpleNamespace(
        redirect_uri="http://localhost:43123/callback",
        state="state",
        code_challenge="challenge",
        _verifier="verifier",
        wait_for_code=lambda *args, **kwargs: "code",
        shutdown=lambda: None,
    )
    monkeypatch.setattr(oauth, "start_pkce_callback", lambda: callback)
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/api/auth/cli/token":
            return token_response()
        if request.url.path == "/api/auth/cli/session":
            return token_response()
        return httpx.Response(200, json={"data": []})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    client = RouterUserClient(origin, no_input=True)
    client._save(credentials(origin=origin))
    client.login(no_browser=True)
    assert client.access_token(force=True) == "access-new"
    client.get("/client/api-keys")
    assert edge_token not in client.path.read_text()
    client.logout()
    # Without a matching Access token, login first probes for Cloudflare Access.
    probe = [] if configured_origin == "matching" else ["/api/auth/cli/session"]
    assert [request.url.path for request in requests] == probe + [
        "/api/auth/cli/token",
        "/api/auth/cli/logout",
        "/api/auth/cli/token",
        "/api/auth/cli/session",
        "/client/api-keys",
        "/api/auth/cli/logout",
    ]
    expected = edge_token if configured_origin == "matching" else None
    assert all(
        request.headers.get("cf-access-token") == expected for request in requests
    )
    calls = requests[len(probe) :]
    assert calls[1].headers["authorization"] == "Bearer refresh-old"
    assert calls[4].headers["authorization"] == "Bearer access-new"
    output = capsys.readouterr()
    assert edge_token not in output.out + output.err


@pytest.mark.parametrize(
    "origin,token",
    [
        ("", "secret"),
        ("https://internal.router.com", ""),
        ("https://internal.router.com", "secret\ninjected"),
        ("https://internal.router.com", "secret\u00e9"),
    ],
)
def test_invalid_access_configuration_fails_before_browser_without_disclosing_token(
    monkeypatch, origin, token
):
    monkeypatch.setenv("RAMP_ROUTER_ACCESS_ORIGIN", origin)
    monkeypatch.setenv("RAMP_ROUTER_ACCESS_TOKEN", token)
    monkeypatch.setattr(
        oauth, "start_pkce_callback", lambda: pytest.fail("Opened callback")
    )
    with pytest.raises(RampCLIError, match="RAMP_ROUTER_ACCESS") as error:
        RouterUserClient("https://internal.router.com").login(no_browser=True)
    if token:
        assert token not in str(error.value)


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example",
        "https://user:pass@example.com",
        "https://example.com/path",
        "https://example.com?token=secret",
    ],
)
def test_reject_unsafe_origins(origin):
    with pytest.raises(Exception, match="Router URL"):
        validate_origin(origin)


def test_private_atomic_persistence_and_separate_profiles():
    client = RouterUserClient("http://localhost:3080")
    client._save(credentials())
    assert client._load()["refresh_token"] == "refresh-old"
    stored = json.loads(client.path.read_text())
    assert stored["refresh_token"] == "refresh-old"
    assert stored["access_token"] == "access-old"
    if os.name != "nt":
        assert client.path.stat().st_mode & 0o077 == 0
    assert RouterUserClient("http://localhost:3080", profile="other")._load() == {}
    assert RouterUserClient("http://localhost:3081")._load() == {}
    assert "refresh_token" not in client.status()


def test_router_target_selection_is_explicit_and_remembered(monkeypatch):
    _atomic_write(_target_path("human"), {"origin": "http://localhost:3080"})
    assert selected_origin(profile="human") == "http://localhost:3080"
    assert selected_origin(profile="agent") == "https://app.router.com"
    monkeypatch.setenv("RAMP_ROUTER_UI_URL", "http://localhost:3081")
    assert selected_origin(profile="human") == "http://localhost:3081"
    assert (
        selected_origin("http://localhost:3082", profile="human")
        == "http://localhost:3082"
    )


def test_refresh_persists_replacement(monkeypatch):
    client = RouterUserClient("http://localhost:3080")
    client._save(credentials())
    seen = []

    def post(_http, url, **kwargs):
        seen.append((url, kwargs["json"]))
        return token_response()

    monkeypatch.setattr(httpx.Client, "post", post)
    assert client.access_token() == "access-new"
    assert client._load()["refresh_token"] == "refresh-new"
    assert seen[0][1]["refresh_token"] == "refresh-old"
    assert client.access_token() == "access-new"
    assert len(seen) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, json={"error": "unavailable"}),
        httpx.Response(409, json={"error": "temporarily_unavailable"}),
    ],
)
def test_transient_errors_preserve_login(monkeypatch, response):
    client = RouterUserClient("http://localhost:3080")
    client._save(credentials())
    monkeypatch.setattr(httpx.Client, "post", lambda *_args, **_kwargs: response)
    with pytest.raises(ApiError):
        client.access_token()
    assert client._load()["refresh_token"] == "refresh-old"


def test_incomplete_rotation_fails_closed(monkeypatch):
    client = RouterUserClient("http://localhost:3080")
    client._save(credentials())
    monkeypatch.setattr(
        httpx.Client, "post", lambda *_args, **_kwargs: token_response(refresh_token="")
    )
    with pytest.raises(RampCLIError, match="incomplete"):
        client.access_token()
    assert client._load() == {}


def test_reauth_is_explicit_for_noninteractive_clients(monkeypatch):
    client = RouterUserClient("http://localhost:3080", no_input=True)
    client._save(credentials())
    monkeypatch.setattr(
        httpx.Client,
        "post",
        lambda *_args, **_kwargs: httpx.Response(
            401, json={"code": "RAMP_REAUTH_REQUIRED"}
        ),
    )
    with pytest.raises(RampCLIError, match="browser reauthorization"):
        client.access_token()
    assert client._load()["refresh_token"] == "refresh-old"


def test_revocation_clears_native_login_only(monkeypatch):
    client = RouterUserClient("http://localhost:3080")
    client._save(credentials())
    monkeypatch.setattr(
        httpx.Client,
        "post",
        lambda *_args, **_kwargs: httpx.Response(
            401, json={"code": "RAMP_ACCESS_REVOKED"}
        ),
    )
    with pytest.raises(RampCLIError, match="revoked"):
        client.access_token()
    assert client._load() == {}


def test_recover_then_resume_without_locking_browser_flow(monkeypatch):
    client = RouterUserClient("http://localhost:3080")
    client._save(credentials())
    monkeypatch.setattr(
        httpx.Client,
        "post",
        lambda *_args, **_kwargs: httpx.Response(
            401, json={"code": "RAMP_REAUTH_REQUIRED"}
        ),
    )

    def login():
        client._save(credentials(access_token="recovered", issued_at=int(time.time())))

    monkeypatch.setattr(client, "login", login)
    assert client.access_token() == "recovered"


def test_agent_status_uses_standard_envelope(monkeypatch):
    monkeypatch.setenv("RAMP_ROUTER_UI_URL", "http://localhost:3080")
    result = CliRunner().invoke(cli, ["--agent", "router", "status"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["schema_version"] == "1.0"
    assert data["data"][0]["authenticated"] is False


def test_agent_mode_does_not_start_browser_login():
    result = CliRunner().invoke(cli, ["--agent", "router", "login"])
    assert result.exit_code == 2


def test_human_keys_list_is_compact_and_counted(monkeypatch):
    payload = {
        "data": [
            {
                "id": "key-one",
                "name": "Coding",
                "enabled": True,
                "current_spend_usd": "12.34",
            },
            {
                "id": "key-two",
                "name": None,
                "enabled": False,
                "core_business_uuid": "verbose-metadata",
            },
        ],
        "has_more": False,
        "next_offset": None,
    }
    monkeypatch.setattr(RouterUserClient, "get", lambda *_args, **_kwargs: payload)
    result = CliRunner().invoke(cli, ["--human", "router", "keys", "list"])
    assert result.exit_code == 0, result.output
    assert "2 API keys" in result.output
    assert (
        "NAME" in result.output and "ENABLED" in result.output and "ID" in result.output
    )
    assert "Coding" in result.output and "(unnamed)" in result.output
    assert "key-one" in result.output and "key-two" in result.output
    assert "Yes" in result.output and "No" in result.output
    assert "current_spend_usd" not in result.output
    assert "verbose-metadata" not in result.output


def test_keys_json_keeps_full_response(monkeypatch):
    payload = {
        "data": [{"id": "key-one", "enabled": True, "billing_mode": "unmetered"}],
        "has_more": False,
    }
    monkeypatch.setattr(RouterUserClient, "get", lambda *_args, **_kwargs: payload)
    result = CliRunner().invoke(cli, ["--output", "json", "router", "keys", "list"])
    assert result.exit_code == 0
    assert json.loads(result.output) == payload


def test_partial_key_page_does_not_claim_a_total(monkeypatch):
    payload = {
        "data": [{"id": "key-one", "name": "Coding", "enabled": True}],
        "has_more": True,
        "next_offset": 3,
    }
    monkeypatch.setattr(RouterUserClient, "get", lambda *_args, **_kwargs: payload)
    result = CliRunner().invoke(
        cli, ["--human", "router", "keys", "list", "--offset", "2"]
    )
    assert result.exit_code == 0
    assert "Showing 1 API key (offset 2)" in result.output
    assert "--offset 3" in result.output


def test_empty_keys_list_has_no_empty_table(monkeypatch):
    monkeypatch.setattr(
        RouterUserClient,
        "get",
        lambda *_args, **_kwargs: {"data": [], "has_more": False},
    )
    result = CliRunner().invoke(cli, ["--human", "router", "keys", "list"])
    assert result.exit_code == 0
    assert result.output.strip() == "0 API keys"


def test_router_footer_aligns_credits_and_account(monkeypatch):

    monkeypatch.setattr(
        "ramp_cli.commands.router_user.shutil.get_terminal_size",
        lambda *_args, **_kwargs: os.terminal_size((100, 24)),
    )
    monkeypatch.setattr(
        RouterUserClient,
        "status",
        lambda self: {
            "authenticated": True,
            "access_token_expired": False,
            "user": {"email": "ada@example.com"},
        },
    )
    calls = []
    checked = []

    def access_token(self, **kwargs):
        checked.append(kwargs)
        return "checked-token"

    monkeypatch.setattr(RouterUserClient, "access_token", access_token)

    def get(self, path, **kwargs):
        calls.append((path, kwargs))
        return {
            "snapshot": {"remaining_credit_usd": "12.34"},
            "billed_to_business": False,
        }

    monkeypatch.setattr(RouterUserClient, "get", get)
    ctx = click.Context(
        click.Command("router"), obj={"profile": None, "no_input": False}
    )
    line = _menu_footer(ctx)
    assert len(line.rstrip("\n")) == 100
    assert line.startswith("Credits: $12.34")
    assert line.endswith("Account: ada@example.com\n")
    assert calls == [("/client/billing", {"timeout": 2, "refresh": False})]
    assert checked == [{"check_session": True, "recover": False, "timeout": 2}]


def test_footer_never_renews_expired_tokens_or_opens_browser(monkeypatch):
    monkeypatch.setattr(
        RouterUserClient,
        "status",
        lambda self: {
            "authenticated": True,
            "access_token_expired": True,
            "user": {"email": "ada@example.com"},
        },
    )
    monkeypatch.setattr(
        RouterUserClient,
        "access_token",
        lambda *_args, **_kwargs: pytest.fail("Help must not renew tokens"),
    )
    monkeypatch.setattr(
        RouterUserClient,
        "get",
        lambda *_args, **_kwargs: pytest.fail("Help must not renew tokens"),
    )
    result = CliRunner().invoke(cli, ["router", "--help"])
    assert result.exit_code == 0
    assert "Account: Not Logged In" in result.output
    assert "Credits:" not in result.output
    assert "ada@example.com" not in result.output


@pytest.mark.parametrize(
    "code,status",
    [
        ("RAMP_REAUTH_REQUIRED", 401),
        ("RAMP_ACCESS_REVOKED", 401),
        ("RAMP_SESSION_UNLINKED", 401),
        ("invalid_grant", 400),
    ],
)
def test_footer_hides_cached_account_when_session_check_rejects_login(
    monkeypatch, code, status
):
    monkeypatch.setenv("RAMP_ROUTER_UI_URL", "http://localhost:3080")
    client = RouterUserClient("http://localhost:3080")
    client._save(credentials(issued_at=int(time.time())))
    requests = []

    def handle(request):
        requests.append(request.url.path)
        assert request.url.path == "/api/auth/cli/session"
        return httpx.Response(status, json={"code": code})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    monkeypatch.setattr(
        oauth, "_open_browser", lambda *_a: pytest.fail("Help must not open a browser")
    )
    result = CliRunner().invoke(cli, ["router", "--help"])
    assert result.exit_code == 0, result.output
    assert "Account: Not Logged In" in result.output
    assert "Credits:" not in result.output
    assert "owner@example.com" not in result.output
    assert requests == ["/api/auth/cli/session"]
    if code == "RAMP_REAUTH_REQUIRED":
        assert client._load()["refresh_token"] == "refresh-old"


def test_footer_retains_verified_account_when_only_billing_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        RouterUserClient,
        "status",
        lambda self: {
            "authenticated": True,
            "access_token_expired": False,
            "user": {"email": "ada@example.com"},
        },
    )
    monkeypatch.setattr(RouterUserClient, "access_token", lambda *_a, **_kw: "checked")

    def unavailable(*_args, **_kwargs):
        raise ApiError(503, "Billing temporarily unavailable")

    monkeypatch.setattr(RouterUserClient, "get", unavailable)
    ctx = click.Context(click.Command("router"), obj={"no_input": False})
    line = _menu_footer(ctx)
    assert "Account: ada@example.com" in line
    assert "Credits: Unavailable" in line


def test_footer_business_wallet_and_unlimited_come_from_server(monkeypatch):

    monkeypatch.setattr(
        RouterUserClient,
        "status",
        lambda self: {
            "authenticated": True,
            "access_token_expired": False,
            "user": {"email": "ada@example.com"},
        },
    )
    monkeypatch.setattr(RouterUserClient, "access_token", lambda *_a, **_kw: "checked")
    billing = {
        "billed_to_business": True,
        "business_wallet": {"remaining_credit_usd": "125"},
        "snapshot": {"remaining_credit_usd": "5"},
    }
    monkeypatch.setattr(RouterUserClient, "get", lambda *_args, **_kwargs: billing)
    ctx = click.Context(
        click.Command("router"), obj={"profile": None, "no_input": False}
    )
    assert "Credits: $125.00" in _menu_footer(ctx)
    billing["ramp_employee_session_unlimited"] = True
    assert "Credits: Unlimited" in _menu_footer(ctx)


def test_keys_default_lists_all_pages_without_prompt_in_agent_mode(monkeypatch):
    calls = []

    def get(self, path, **kwargs):
        calls.append(kwargs["params"])
        if kwargs["params"]["offset"] == 0:
            return {
                "data": [{"id": "one", "name": "One", "enabled": True}],
                "has_more": True,
                "next_offset": 100,
            }
        return {
            "data": [{"id": "two", "name": "Two", "enabled": False}],
            "has_more": False,
        }

    monkeypatch.setattr(RouterUserClient, "get", get)
    monkeypatch.setattr(
        "ramp_cli.commands.router_user.questionary.select",
        lambda *_args, **_kwargs: pytest.fail("Agent mode must not prompt"),
    )
    result = CliRunner().invoke(cli, ["--agent", "router", "keys"])
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.output)["data"][0]["data"]) == 2
    assert calls == [{"limit": 100, "offset": 0}, {"limit": 100, "offset": 100}]


def test_key_picker_inspects_selected_key_and_handles_missing_analytics(monkeypatch):
    key = {
        "id": "selected",
        "name": "Coding",
        "enabled": True,
        "current_spend_usd": "12.5",
    }
    seen = []

    def get(self, path, **kwargs):
        if path == "/client/api-keys":
            return {"data": [key], "has_more": False}
        if path == "/client/routing-profiles":
            return {"data": []}
        if path == "/admin/me/experiment-settings":
            return {"switchyard_routing_enabled": True}
        seen.append(kwargs["params"])
        raise ApiError(503, '{"error":{"code":"analytics_unavailable"}}')

    class Picker:
        def ask(self):
            return "selected"

    monkeypatch.setattr(RouterUserClient, "get", get)
    monkeypatch.setattr(
        "ramp_cli.commands.router_user._interactive_keys", lambda _ctx: True
    )
    monkeypatch.setattr(
        "ramp_cli.commands.router_user.questionary.select",
        lambda *_args, **_kwargs: Picker(),
    )
    result = CliRunner().invoke(cli, ["--human", "router", "keys"])
    assert result.exit_code == 0, result.output
    assert "Coding" in result.output and "$12.50" in result.output
    assert "analytics not configured" in result.output
    assert "Requests" not in result.output
    assert json.loads(seen[0]["filters"]) == {"key": ["selected"]}


def test_show_key_reports_real_usage_summary(monkeypatch):
    def get(self, path, **kwargs):
        if path == "/client/api-keys":
            return {
                "data": [{"id": "one", "name": "Coding", "enabled": True}],
                "has_more": False,
            }
        if path == "/client/routing-profiles":
            return {"data": []}
        if path == "/admin/me/experiment-settings":
            return {}
        return {
            "summary": {
                "request_count": 123,
                "total_tokens": 4567,
                "spend_usd": "1.23",
                "cost_savings_usd": 0,
                "p95_latency_ms": 0,
            }
        }

    monkeypatch.setattr(RouterUserClient, "get", get)
    result = CliRunner().invoke(cli, ["--human", "router", "keys", "show", "one"])
    assert result.exit_code == 0, result.output
    assert (
        "123" in result.output and "4,567" in result.output and "$1.23" in result.output
    )


def test_show_unknown_key_never_queries_stats(monkeypatch):
    calls = []

    def get(self, path, **kwargs):
        calls.append(path)
        return {"data": [], "has_more": False}

    monkeypatch.setattr(RouterUserClient, "get", get)
    result = CliRunner().invoke(cli, ["router", "keys", "show", "someone-elses-key"])
    assert result.exit_code == 2
    assert calls == ["/client/api-keys"]


def test_default_keys_rejects_nonadvancing_pagination(monkeypatch):
    monkeypatch.setattr(
        RouterUserClient,
        "get",
        lambda *_args, **_kwargs: {"data": [], "has_more": True, "next_offset": 0},
    )
    result = CliRunner().invoke(cli, ["router", "keys"])
    assert result.exit_code != 0
    assert "invalid API-key pagination" in str(result.exception)


def test_plain_detail_card_has_no_gradient_background_or_shadow(monkeypatch):
    class TerminalBuffer(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    output = TerminalBuffer()
    show_detail_card(
        "Key details", {"ID": "key-one", "Enabled": "No"}, output, plain=True
    )
    rendered = output.getvalue()
    assert "key-one" in rendered and "Enabled:" in rendered
    assert "\x1b[" not in rendered
    assert "▄" not in rendered and "▐" not in rendered
    lines = rendered.splitlines()
    assert lines[0].startswith("┌") and lines[-1].startswith("└")
    assert len({len(line) for line in lines}) == 1


def test_default_detail_card_styling_is_unchanged(monkeypatch):
    class TerminalBuffer(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    output = TerminalBuffer()
    show_detail_card("Other screen", {"ID": "key-one"}, output)
    rendered = output.getvalue()
    assert "\x1b[38;2;" in rendered
    assert "\x1b[48;2;" in rendered
    assert "▄" in rendered and "▐" in rendered


def test_key_detail_sections_use_plain_style_even_in_color_mode(monkeypatch):
    monkeypatch.setattr("ramp_cli.output.style._color_supported", lambda _file: True)

    def get(self, path, **kwargs):
        if path == "/client/api-keys":
            return {
                "data": [{"id": "one", "name": "Coding", "enabled": True}],
                "has_more": False,
            }
        if path == "/client/routing-profiles":
            return {"data": []}
        if path == "/admin/me/experiment-settings":
            return {"switchyard_routing_enabled": True}
        return {
            "summary": {"request_count": 2, "total_tokens": 300, "spend_usd": "0.25"}
        }

    monkeypatch.setattr(RouterUserClient, "get", get)
    result = CliRunner().invoke(
        cli, ["--human", "router", "keys", "show", "one"], color=True
    )
    assert result.exit_code == 0, result.output
    assert (
        "Coding" in result.output
        and "Usage · last 7 days" in result.output
        and "Strategies" in result.output
    )
    assert "\x1b[38;2;" not in result.output and "\x1b[48;2;" not in result.output
    assert "▄" not in result.output and "▐" not in result.output


def _strategy_client(monkeypatch, profiles, settings, calls=None):
    keys = [{"id": "one", "name": "Coding", "enabled": True}]

    def get(self, path, **kwargs):
        if calls is not None:
            calls.append(path)
        if path == "/client/api-keys":
            return {"data": keys, "has_more": False}
        if path == "/client/routing-profiles":
            if isinstance(profiles, Exception):
                raise profiles
            return {"data": profiles}
        if path == "/admin/me/experiment-settings":
            return settings
        raise AssertionError(path)

    monkeypatch.setattr(RouterUserClient, "get", get)


def test_key_strategies_follow_assigned_routing_profile(monkeypatch):
    _strategy_client(
        monkeypatch,
        [
            {"id": "p0", "name": "Other", "assigned_api_key_ids": ["two"]},
            {
                "id": "p1",
                "name": "Cheap",
                "is_default": False,
                "assigned_api_key_ids": ["one"],
                "cost_efficient_routing_enabled": True,
                "switchyard_routing_enabled": False,
                "auto_routing_enabled": None,
                "shadow_models_enabled": None,
            },
        ],
        {"auto_routing_enabled": True, "allow_flex_tier_default": False},
    )
    result = CliRunner().invoke(cli, ["--agent", "router", "keys", "strategies", "one"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"][0]
    assert data["source"] == "routing_profile"
    assert data["routing_profile"]["name"] == "Cheap"
    assert data["allow_flex_tier_default"] is True
    assert data["auto_routing_enabled"] is True


def test_key_strategies_fall_back_to_account_defaults_outside_rollout(monkeypatch):
    _strategy_client(
        monkeypatch,
        ApiError(403, '{"error":{"code":"routing_profiles_unavailable"}}'),
        {
            "allow_flex_tier_default": True,
            "switchyard_routing_enabled": True,
            "auto_routing_enabled": False,
            "shadow_models_enabled": False,
        },
    )
    result = CliRunner().invoke(cli, ["--agent", "router", "keys", "strategies", "one"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"][0]
    assert data["source"] == "account_defaults"
    assert data["routing_profile"] is None
    assert data["switchyard_routing_enabled"] is True


def test_key_strategies_reject_keys_outside_the_account(monkeypatch):
    calls: list[str] = []
    _strategy_client(monkeypatch, [], {}, calls)
    result = CliRunner().invoke(
        cli, ["--agent", "router", "keys", "strategies", "missing"]
    )
    assert result.exit_code != 0
    assert "not found in your account" in result.output
    assert calls == ["/client/api-keys"]


def test_key_picker_back_navigation_returns_to_list_and_exits_cleanly(monkeypatch):
    key = {"id": "selected", "name": "Coding", "enabled": True}
    detail_calls = []

    def get(self, path, **kwargs):
        if path == "/client/api-keys":
            return {"data": [key], "has_more": False}
        if path == "/client/routing-profiles":
            return {"data": []}
        if path == "/admin/me/experiment-settings":
            return {}
        detail_calls.append(path)
        return {"summary": {"request_count": 1}}

    prompts = []
    answers = iter(["selected", router_user._PICKER_BACK, router_user._PICKER_BACK])

    def select(message, choices, **_kwargs):
        prompts.append((message, [choice.value for choice in choices]))

        class Picker:
            def ask(self):
                return next(answers)

        return Picker()

    monkeypatch.setattr(RouterUserClient, "get", get)
    monkeypatch.setattr(router_user, "_interactive_keys", lambda _ctx: True)
    monkeypatch.setattr(router_user.questionary, "select", select)
    result = CliRunner().invoke(cli, ["--human", "router", "keys"])
    assert result.exit_code == 0, result.output
    assert "StopIteration" not in result.output
    assert [message for message, _ in prompts] == [
        "Select an API key (1 key)",
        "What next?",
        "Select an API key (1 key)",
    ]
    assert router_user._PICKER_BACK in prompts[0][1] and None not in prompts[0][1]
    assert detail_calls == ["/client/usage/dashboard"]


_PROFILES = [
    {
        "id": "11111111-1111-1111-1111-111111111111",
        "name": "Account default",
        "is_default": True,
        "cost_efficient_routing_enabled": False,
        "switchyard_routing_enabled": False,
        "auto_routing_enabled": None,
    },
    {
        "id": "22222222-2222-2222-2222-222222222222",
        "name": "Cheap",
        "is_default": False,
        "cost_efficient_routing_enabled": True,
        "switchyard_routing_enabled": False,
        "auto_routing_enabled": True,
    },
]


def _create_client(monkeypatch, profiles=_PROFILES, clipboard=True):
    posts = []

    def get(self, path, **kwargs):
        if path == "/client/routing-profiles":
            if profiles is None:
                raise ApiError(403, '{"error":{"code":"routing_profiles_unavailable"}}')
            return {"data": profiles}
        if path == "/client/api-keys":
            return {"data": [], "has_more": False}
        raise AssertionError(path)

    def post(self, path, *, json, idempotency_key, **kwargs):
        posts.append((path, json, idempotency_key))
        return {
            "id": "new",
            "name": json["name"],
            "secret": "sk-secret",
            "replayed": False,
        }

    monkeypatch.setattr(RouterUserClient, "get", get)
    monkeypatch.setattr(RouterUserClient, "post", post)
    monkeypatch.setattr(
        "ramp_cli.commands.router._copy_to_clipboard", lambda _text: clipboard
    )
    return posts


def test_create_key_uses_named_profile_and_keeps_secret_off_screen(monkeypatch):
    posts = _create_client(monkeypatch)
    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "keys",
            "create",
            "--name",
            " Coding ",
            "--routing-profile",
            "cheap",
        ],
    )
    assert result.exit_code == 0, result.output
    path, body, idempotency_key = posts[0]
    assert path == "/client/api-keys"
    assert body == {"name": "Coding", "routing_profile_id": _PROFILES[1]["id"]}
    assert idempotency_key
    assert "Routing strategy: Cheap" in result.output
    assert "on the clipboard" in result.output and "sk-secret" not in result.output


def test_create_key_outside_profile_rollout_uses_account_strategies(monkeypatch):
    posts = _create_client(monkeypatch, profiles=None)
    result = CliRunner().invoke(
        cli, ["--agent", "router", "keys", "create", "--name", "Coding"]
    )
    assert result.exit_code == 0, result.output
    assert posts[0][1] == {"name": "Coding"}
    data = json.loads(result.output)["data"][0]
    assert data["routing_strategy"] == "Account defaults"
    assert data["api_key_on_clipboard"] is True and "secret" not in data

    rejected = CliRunner().invoke(
        cli,
        [
            "--agent",
            "router",
            "keys",
            "create",
            "--name",
            "Coding",
            "--routing-profile",
            "Cheap",
        ],
    )
    assert rejected.exit_code != 0 and "aren't available" in rejected.output
    assert len(posts) == 1


def test_create_key_requires_name_without_prompts(monkeypatch):
    posts = _create_client(monkeypatch)
    result = CliRunner().invoke(cli, ["--no-input", "router", "keys", "create"])
    assert result.exit_code != 0 and "--name" in result.output
    too_long = CliRunner().invoke(
        cli, ["--agent", "router", "keys", "create", "--name", "x" * 33]
    )
    assert too_long.exit_code != 0 and "32 characters" in too_long.output
    assert posts == []


def test_create_key_withholds_secret_when_clipboard_is_unavailable(monkeypatch):
    _create_client(monkeypatch, clipboard=False)
    result = CliRunner().invoke(
        cli, ["--agent", "router", "keys", "create", "--name", "Coding"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"][0]
    assert "secret" not in data and data["api_key_on_clipboard"] is False
    assert "--show-key" in data["secret_withheld"]
    assert "sk-secret" not in result.output


def test_create_key_dry_run_sends_nothing(monkeypatch):
    posts = _create_client(monkeypatch)
    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "router",
            "keys",
            "create",
            "--name",
            "Coding",
            "--routing-profile",
            "Cheap",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"][0]
    assert data["body"]["routing_profile_id"] == _PROFILES[1]["id"]
    assert posts == []


def test_create_key_interactive_prompts_for_name_and_strategy(monkeypatch):
    posts = _create_client(monkeypatch)
    seen = {}

    class Answer:
        def __init__(self, value):
            self.value = value

        def ask(self):
            return self.value

    def select(message, choices, **kwargs):
        seen["choices"] = [choice.title for choice in choices]
        seen["default"] = kwargs["default"].value
        return Answer(_PROFILES[1]["id"])

    monkeypatch.setattr(router_user, "_interactive_keys", lambda _ctx: True)
    monkeypatch.setattr(
        router_user.questionary, "text", lambda *a, **k: Answer("Coding")
    )
    monkeypatch.setattr(router_user.questionary, "select", select)
    result = CliRunner().invoke(cli, ["--human", "router", "keys", "create"])
    assert result.exit_code == 0, result.output
    assert posts[0][1] == {"name": "Coding", "routing_profile_id": _PROFILES[1]["id"]}
    assert seen["default"] == _PROFILES[0]["id"]
    assert seen["choices"] == [
        "Account default (default) · Jev Auto Routing: account default",
        "Cheap · Flex, Jev Auto Routing",
    ]


def test_human_status_login_and_logout_render_account_cards(monkeypatch):
    signed_in = {
        "origin": "http://localhost:3080",
        "authenticated": True,
        "user": {"id": "user-1", "email": "ada@local-cli.example"},
        "access_token_expired": False,
    }
    monkeypatch.setattr(RouterUserClient, "status", lambda self: signed_in)
    monkeypatch.setattr(RouterUserClient, "login", lambda self, **_kw: signed_in)
    monkeypatch.setattr(RouterUserClient, "logout", lambda self: None)

    status = CliRunner().invoke(cli, ["--human", "router", "status"])
    assert status.exit_code == 0, status.output
    assert "Router account" in status.output
    assert (
        "ada@local-cli.example" in status.output and "localhost:3080" in status.output
    )
    assert "Signed in" in status.output
    assert "user-1" not in status.output and "access_token_expired" not in status.output

    login = CliRunner().invoke(cli, ["--human", "router", "login"])
    assert login.exit_code == 0, login.output
    assert "Signed in to Router" in login.output

    logout = CliRunner().invoke(cli, ["--human", "router", "logout"])
    assert logout.exit_code == 0, logout.output
    assert "Signed out of Router" in logout.output and "success" not in logout.output

    monkeypatch.setattr(
        RouterUserClient,
        "status",
        lambda self: {**signed_in, "authenticated": False, "user": None},
    )
    signed_out = CliRunner().invoke(cli, ["--human", "router", "status"])
    assert (
        "Not signed in" in signed_out.output
        and "ramp router login" in signed_out.output
    )


def _fake_login(client, *, access_token="access-signed-in"):
    calls = []

    def login(**_kwargs):
        calls.append("login")
        client._save(
            credentials(
                access_token=access_token,
                refresh_token="refresh-signed-in",
                issued_at=int(time.time()),
            )
        )
        return client.status()

    client.login = login
    return calls


def test_interactive_signed_out_user_is_signed_in_automatically(capsys):
    client = RouterUserClient("http://localhost:3080", interactive=True)
    calls = _fake_login(client)
    assert client.access_token() == "access-signed-in"
    assert calls == ["login"]
    assert "Opening your browser to sign in" in capsys.readouterr().err


@pytest.mark.parametrize(
    "client_options",
    [{}, {"interactive": True, "no_input": True}],
)
def test_non_interactive_signed_out_user_gets_login_hint(client_options):
    client = RouterUserClient("http://localhost:3080", **client_options)
    calls = _fake_login(client)
    with pytest.raises(RampCLIError, match="Run 'ramp router login'"):
        client.access_token()
    assert calls == []


def test_interactive_expired_login_signs_in_again(monkeypatch):
    client = RouterUserClient("http://localhost:3080", interactive=True)
    client._save(credentials())
    calls = _fake_login(client)
    monkeypatch.setattr(
        httpx.Client,
        "post",
        lambda *_a, **_k: httpx.Response(400, json={"error": "invalid_grant"}),
    )
    assert client.access_token() == "access-signed-in"
    assert calls == ["login"]


def test_revoked_business_access_signs_in_and_retries_once(monkeypatch):
    client = RouterUserClient("http://localhost:3080", interactive=True)
    original, recovered = jwt_token(), jwt_token(nonce="recovered")
    client._save(credentials(access_token=original, issued_at=int(time.time())))
    calls = _fake_login(client, access_token=recovered)
    seen = []

    def request(_http, method, url, **kwargs):
        token = kwargs["headers"]["Authorization"]
        seen.append(token)
        if token == "Bearer " + original:
            return httpx.Response(
                403, json={"error": {"code": "business_access_revoked"}}
            )
        return httpx.Response(201, json={"id": "new"})

    monkeypatch.setattr(httpx.Client, "request", request)
    monkeypatch.setattr(
        httpx.Client,
        "post",
        lambda *_a, **_k: token_response(access_token=original),
    )
    assert client.post(
        "/client/api-keys", json={"name": "Test"}, idempotency_key="idem"
    ) == {"id": "new"}
    assert calls == ["login"]
    assert seen == ["Bearer " + original, "Bearer " + recovered]


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize(
    "recovered", [jwt_token("other"), jwt_token(business="other-business"), "unknown"]
)
def test_write_retry_never_crosses_user_or_business(monkeypatch, status, recovered):
    client = RouterUserClient("http://localhost:3080", interactive=True)
    tokens = iter([jwt_token(), recovered])
    monkeypatch.setattr(client, "access_token", lambda **_kw: next(tokens))
    monkeypatch.setattr(client, "recover_login", lambda *_a: recovered)
    sent = []

    def request(_http, _method, _url, **kwargs):
        sent.append(kwargs["headers"]["Authorization"])
        return httpx.Response(
            status, json={"error": {"code": "business_access_revoked"}}
        )

    monkeypatch.setattr(httpx.Client, "request", request)
    with pytest.raises(RampCLIError, match="write was not retried"):
        client.post(
            "/client/api-keys", json={"name": "Example"}, idempotency_key="test"
        )
    assert len(sent) == 1


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "recovered", [jwt_token("other"), jwt_token(business="other-business"), "unknown"]
)
def test_write_preflight_never_crosses_user_or_business(monkeypatch, method, recovered):
    client = RouterUserClient("http://localhost:3080", interactive=True)
    client._save(credentials(access_token=jwt_token(), issued_at=int(time.time())))
    monkeypatch.setattr(
        httpx.Client,
        "post",
        lambda *_a, **_k: httpx.Response(400, json={"code": "RAMP_REAUTH_REQUIRED"}),
    )
    monkeypatch.setattr(client, "recover_login", lambda *_a: recovered)
    monkeypatch.setattr(
        httpx.Client, "request", lambda *_a, **_k: pytest.fail("write must not be sent")
    )
    with pytest.raises(RampCLIError, match="request was not sent"):
        client._send(method, "/client/routing-profiles", json={"name": "Example"})


def test_write_preflight_allows_same_account_recovery(monkeypatch):
    client = RouterUserClient("http://localhost:3080", interactive=True)
    client._save(credentials(access_token=jwt_token(), issued_at=int(time.time())))
    recovered = jwt_token(nonce="recovered")
    monkeypatch.setattr(
        httpx.Client,
        "post",
        lambda *_a, **_k: httpx.Response(400, json={"code": "RAMP_REAUTH_REQUIRED"}),
    )
    monkeypatch.setattr(client, "recover_login", lambda *_a: recovered)
    sent = []

    def request(_http, _method, _url, **kwargs):
        sent.append(kwargs["headers"]["Authorization"])
        return httpx.Response(201, json={"id": "created"})

    monkeypatch.setattr(httpx.Client, "request", request)
    assert client.post("/client/routing-profiles", json={"name": "Example"}) == {
        "id": "created"
    }
    assert sent == ["Bearer " + recovered]


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_requests_keep_account_from_prior_read(monkeypatch, method):
    client = RouterUserClient("http://localhost:3080", interactive=True)
    first, other = jwt_token(), jwt_token("other")
    client._save(credentials(access_token=first))
    tokens = iter([first, other])
    monkeypatch.setattr(client, "access_token", lambda **_kw: next(tokens))
    sent = []

    def request(_http, method, _url, **kwargs):
        sent.append(method)
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(httpx.Client, "request", request)
    client.get("/client/routing-profiles")
    # Simulate a different process replacing the saved login between requests.
    client._save(credentials(access_token=other))
    with pytest.raises(RampCLIError, match="request was not sent"):
        client._send(method, "/client/routing-profiles")
    assert sent == ["GET"]


def test_first_write_can_sign_in_when_no_account_was_selected(monkeypatch):
    client = RouterUserClient("http://localhost:3080", interactive=True)
    monkeypatch.setattr(client, "recover_login", lambda *_a: jwt_token())
    monkeypatch.setattr(
        httpx.Client,
        "request",
        lambda *_a, **_k: httpx.Response(201, json={"id": "new"}),
    )
    assert client.post("/client/routing-profiles", json={"name": "Example"}) == {
        "id": "new"
    }


def test_lost_refresh_response_keeps_durable_candidate_and_retries_it(monkeypatch):
    client = RouterUserClient("http://localhost:3080", no_input=True)
    client._save(credentials())
    seen = []

    def post(_http, _url, **kwargs):
        body = kwargs["json"]
        persisted = client._load()
        assert persisted["pending_refresh_token"] == body["next_refresh_token"]
        assert persisted["refresh_token"] == "refresh-old"
        seen.append(body)
        if len(seen) == 1:
            raise httpx.ReadTimeout("Response lost after server committed")
        return token_response(refresh_token=body["next_refresh_token"])

    monkeypatch.setattr(httpx.Client, "post", post)
    with pytest.raises(httpx.ReadTimeout):
        client.access_token()
    assert client.access_token(check_session=True) == "access-new"
    assert seen[0] == seen[1]
    assert client._load()["refresh_token"] == seen[0]["next_refresh_token"]
    assert "pending_refresh_token" not in client._load()


def test_pending_refresh_is_saved_before_any_http_request(monkeypatch):
    client = RouterUserClient("http://localhost:3080")
    client._save(credentials())

    def failed(*_a):
        raise OSError("Cannot persist pending refresh")

    monkeypatch.setattr(client, "_save", failed)
    monkeypatch.setattr(
        httpx.Client,
        "post",
        lambda *_a, **_kw: pytest.fail("Consumed token before durable save"),
    )
    with pytest.raises(OSError, match="pending refresh"):
        client.access_token()


def test_revoked_business_access_without_terminal_explains_next_step(monkeypatch):
    client = RouterUserClient("http://localhost:3080")
    client._save(credentials(issued_at=int(time.time())))
    calls = _fake_login(client)
    monkeypatch.setattr(
        httpx.Client,
        "post",
        lambda *_a, **_k: token_response(access_token="access-old"),
    )
    monkeypatch.setattr(
        httpx.Client,
        "request",
        lambda *_a, **_k: httpx.Response(
            403, json={"error": {"code": "business_access_revoked"}}
        ),
    )
    with pytest.raises(RampCLIError, match="access to this Ramp business has ended"):
        client.get("/client/api-keys")
    assert calls == []


def test_management_request_checks_session_despite_valid_cached_token(monkeypatch):
    client = RouterUserClient("http://localhost:3080", no_input=True)
    client._save(credentials(issued_at=int(time.time())))
    seen = []
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def post(_http, url, **kwargs):
        seen.append(url)
        assert kwargs["headers"]["Authorization"] == "Bearer refresh-old"
        return token_response(access_token="checked")

    monkeypatch.setattr(httpx.Client, "post", post)
    monkeypatch.setattr(
        httpx.Client,
        "request",
        lambda _http, _method, _url, **kw: httpx.Response(
            200, json={"bearer": kw["headers"]["Authorization"]}
        ),
    )
    session = client.origin + "/api/auth/cli/session"
    assert client.get("/client/api-keys") == {"bearer": "Bearer checked"}
    # Reads reuse a passed check for a minute, with the token it returned.
    clock[0] += 59
    assert client.get("/client/api-keys") == {"bearer": "Bearer checked"}
    assert seen == [session]
    clock[0] += 1
    assert client.get("/client/api-keys") == {"bearer": "Bearer checked"}
    assert seen == [session] * 2
    assert client._load()["refresh_token"] == "refresh-old"
    assert client._load()["access_token"] == "access-old"


def test_writes_check_the_session_even_right_after_a_read(monkeypatch):
    stored, checked = jwt_token(nonce="stored"), jwt_token(nonce="checked")
    client = RouterUserClient("http://localhost:3080", no_input=True)
    client._save(credentials(access_token=stored, issued_at=int(time.time())))
    seen = []

    def post(_http, url, **kwargs):
        seen.append(url)
        return token_response(access_token=checked)

    monkeypatch.setattr(httpx.Client, "post", post)
    monkeypatch.setattr(
        httpx.Client, "request", lambda *_a, **_kw: httpx.Response(200, json={})
    )
    session = client.origin + "/api/auth/cli/session"
    client.get("/client/api-keys")
    client.get("/client/api-keys")
    assert seen == [session]
    client.patch("/client/api-keys/k", json={"name": "Renamed"})
    client.delete("/client/api-keys/k")
    assert seen == [session] * 3


def test_a_new_sign_in_ends_reuse_of_the_old_check(monkeypatch):
    client = RouterUserClient("http://localhost:3080", no_input=True)
    client._save(credentials(issued_at=int(time.time())))
    responses = [token_response(access_token="checked")]

    monkeypatch.setattr(httpx.Client, "post", lambda *_a, **_kw: responses.pop(0))
    monkeypatch.setattr(
        httpx.Client, "request", lambda *_a, **_kw: httpx.Response(200, json={})
    )
    client.get("/client/api-keys")
    # Signing in again replaces the refresh token, so the old check no longer
    # counts; Router's answer to the new check is the one that applies.
    client._save(credentials(refresh_token="refresh-other", issued_at=int(time.time())))
    responses.append(httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(RampCLIError, match="revoked"):
        client.get("/client/api-keys")
    assert client._session_check is None


def test_requests_reuse_one_connection_pool_until_closed(monkeypatch):
    client = RouterUserClient("http://localhost:3080", no_input=True)
    client._save(
        credentials(access_token=jwt_token(nonce="stored"), issued_at=int(time.time()))
    )
    pools = []
    real_client = httpx.Client

    def handle(request):
        if request.url.path == "/api/auth/cli/session":
            return token_response(access_token=jwt_token(nonce="checked"))
        return httpx.Response(200, json={"data": []})

    def make(**kwargs):
        pools.append(real_client(transport=httpx.MockTransport(handle), **kwargs))
        return pools[-1]

    monkeypatch.setattr(httpx, "Client", make)
    client.get("/client/api-keys")
    client.patch("/client/api-keys/k", json={"name": "Renamed"})
    assert len(pools) == 1
    client.close()
    assert pools[0].is_closed


def test_session_check_reuse_never_outlives_the_token_it_returned(monkeypatch):
    client = RouterUserClient("http://localhost:3080", no_input=True)
    client._save(credentials(issued_at=int(time.time())))
    seen = []
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def post(_http, url, **_kwargs):
        seen.append(url)
        # This token has 40 seconds left, so it can be reused for only 10.
        return token_response(access_token="checked", expires_in=40)

    monkeypatch.setattr(httpx.Client, "post", post)
    monkeypatch.setattr(
        httpx.Client, "request", lambda *_a, **_kw: httpx.Response(200, json={})
    )
    client.get("/client/api-keys")
    clock[0] += 9
    client.get("/client/api-keys")
    assert len(seen) == 1
    clock[0] += 1
    client.get("/client/api-keys")
    assert len(seen) == 2


def test_a_closed_client_never_reopens_its_connections():
    client = RouterUserClient("http://localhost:3080", no_input=True)
    client._pool()
    client.close()
    with pytest.raises(RampCLIError, match="closed"):
        client._pool()


def test_the_connection_pool_keeps_no_cookies():
    client = RouterUserClient("http://localhost:3080", no_input=True)
    pool = client._pool()
    pool.cookies.extract_cookies(
        httpx.Response(
            200,
            headers={"set-cookie": "session=x; Path=/"},
            request=httpx.Request("GET", "http://localhost:3080/api/auth/cli/session"),
        )
    )
    assert not pool.cookies
    client.close()


def test_cli_session_revocation_blocks_management_before_sending_cached_jwt(
    monkeypatch,
):
    client = RouterUserClient("http://localhost:3080", no_input=True)
    client._save(credentials(issued_at=int(time.time())))
    monkeypatch.setattr(
        httpx.Client,
        "post",
        lambda *_a, **_kw: httpx.Response(400, json={"error": "invalid_grant"}),
    )
    monkeypatch.setattr(
        httpx.Client,
        "request",
        lambda *_a, **_kw: pytest.fail("Sent cached JWT"),
    )
    with pytest.raises(RampCLIError, match="revoked"):
        client.get("/client/api-keys")
    assert client._load() == {}


def test_footer_session_check_never_opens_browser(monkeypatch):
    client = RouterUserClient("http://localhost:3080", interactive=True)
    client._save(credentials(issued_at=int(time.time())))
    monkeypatch.setattr(
        httpx.Client,
        "post",
        lambda *_a, **_kw: httpx.Response(401, json={"code": "RAMP_REAUTH_REQUIRED"}),
    )
    monkeypatch.setattr(
        client, "login", lambda: pytest.fail("Opened browser from help")
    )
    with pytest.raises(RampCLIError, match="reauthorization"):
        client.get("/client/billing", refresh=False, timeout=2)


def test_footer_checks_near_expiry_token_without_rotating(monkeypatch):
    client = RouterUserClient("http://localhost:3080")
    client._save(credentials(issued_at=int(time.time()) - 890))
    seen = []

    def post(_http, url, **_kwargs):
        seen.append(url)
        return token_response()

    monkeypatch.setattr(httpx.Client, "post", post)
    monkeypatch.setattr(
        httpx.Client, "request", lambda *_a, **_kw: httpx.Response(200, json={})
    )
    client.get("/client/billing", refresh=False, timeout=2)
    assert seen == [client.origin + "/api/auth/cli/session"]
    assert client._load()["refresh_token"] == "refresh-old"


def test_file_reads_do_not_rewrite_credentials(monkeypatch):
    client = RouterUserClient("http://localhost:3080")
    _atomic_write(client.path, credentials())
    monkeypatch.setattr(
        client, "_save", lambda *_a: pytest.fail("Reading rewrote credentials")
    )
    assert client._load()["refresh_token"] == "refresh-old"


def test_old_keychain_metadata_requires_fresh_login():
    client = RouterUserClient("http://localhost:3080")
    metadata = {
        key: value
        for key, value in credentials().items()
        if key not in ("access_token", "refresh_token")
    }
    _atomic_write(client.path, metadata)
    assert not client.status()["authenticated"]


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_file_storage_has_no_platform_specific_backend(monkeypatch, platform):
    client = RouterUserClient("http://localhost:3080")
    monkeypatch.setattr(sys, "platform", platform)
    client._save(credentials())
    assert client._load()["refresh_token"] == "refresh-old"


def test_failed_atomic_write_preserves_existing_credentials(monkeypatch):
    client = RouterUserClient("http://localhost:3080")
    client._save(credentials())

    def failed(*_args):
        raise OSError("Atomic replacement failed")

    monkeypatch.setattr(os, "replace", failed)
    with pytest.raises(OSError, match="replacement failed"):
        client._save(credentials(refresh_token="replacement"))
    assert client._load()["refresh_token"] == "refresh-old"
    assert list(client.path.parent.iterdir()) == [client.path]


def test_logout_removes_private_credentials(monkeypatch):
    client = RouterUserClient("http://localhost:3080")
    client._save(credentials())
    monkeypatch.setattr(httpx.Client, "post", lambda *_a, **_kw: httpx.Response(200))
    client.logout()
    assert not client.path.exists()
    assert client._load() == {}


def test_interactive_keys_lists_keys_once_in_the_picker(monkeypatch):
    keys = [{"id": "a", "name": "Coding", "enabled": True}]
    monkeypatch.setattr(
        RouterUserClient,
        "get",
        lambda self, path, **kw: {"data": keys, "has_more": False},
    )
    monkeypatch.setattr(router_user, "_interactive_keys", lambda _ctx: True)

    class Back:
        def ask(self):
            return router_user._PICKER_BACK

    monkeypatch.setattr(router_user.questionary, "select", lambda *a, **k: Back())
    result = CliRunner().invoke(cli, ["--human", "router", "keys"])
    assert result.exit_code == 0, result.output
    assert "Coding" not in result.output and "ENABLED" not in result.output.upper()


def test_non_interactive_keys_still_prints_the_table(monkeypatch):
    keys = [{"id": "a", "name": "Coding", "enabled": True}]
    monkeypatch.setattr(
        RouterUserClient,
        "get",
        lambda self, path, **kw: {"data": keys, "has_more": False},
    )
    result = CliRunner().invoke(cli, ["--human", "router", "keys"])
    assert result.exit_code == 0, result.output
    assert "1 API key" in result.output and "Coding" in result.output


def _behind_access(monkeypatch, handle):
    """Send every request this test makes to ``handle``."""
    real_client = httpx.Client

    def make(**kwargs):
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "Client", make)


def _access_redirect(request):
    return httpx.Response(
        302, headers={"location": "https://ramp.cloudflareaccess.com/cdn-cgi/access"}
    )


def test_an_access_redirect_asks_for_router_login_instead_of_failing(monkeypatch):
    client = RouterUserClient("http://localhost:3080", no_input=True)
    client._save(credentials(issued_at=int(time.time())))
    _behind_access(monkeypatch, _access_redirect)
    with pytest.raises(RampCLIError, match="ramp router login") as raised:
        client.get("/client/api-keys")
    assert raised.value.code == EXIT_AUTH_REQUIRED


def test_access_sign_in_needs_cloudflared(monkeypatch):
    client = RouterUserClient("http://localhost:3080", no_input=True)
    _behind_access(monkeypatch, _access_redirect)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    with pytest.raises(RampCLIError, match="Install cloudflared") as raised:
        client.ensure_edge_access()
    assert raised.value.code == EXIT_AUTH_REQUIRED


def test_a_public_router_never_starts_cloudflared(monkeypatch):
    client = RouterUserClient("http://localhost:3080", no_input=True)
    _behind_access(monkeypatch, lambda request: httpx.Response(401))
    monkeypatch.setattr(
        "subprocess.Popen", lambda *_a, **_kw: pytest.fail("ran cloudflared")
    )
    client.ensure_edge_access()


def test_cloudflared_tokens_go_only_to_their_own_router(monkeypatch):
    monkeypatch.setattr(
        "ramp_cli.client.router_user._edge_token",
        lambda origin: (
            "edge.token.value" if origin == "http://localhost:3080" else None
        ),
    )
    assert RouterUserClient(
        "http://localhost:3080", no_input=True
    )._access_headers() == {"cf-access-token": "edge.token.value"}
    assert (
        RouterUserClient("http://localhost:4000", no_input=True)._access_headers() == {}
    )


def test_a_pasted_callback_address_finishes_a_pending_sign_in():
    callback = oauth.start_pkce_callback()
    try:
        with pytest.raises(click.ClickException, match="isn't the sign-in callback"):
            callback.submit_url("https://app.router.com/")
        with pytest.raises(click.ClickException, match="different sign-in"):
            callback.submit_url(f"{callback.redirect_uri}?state=other&code=abc")
        callback.submit_url(
            f" {callback.redirect_uri}?state={callback.state}&code=abc "
        )
        assert callback.wait_for_code(1, timeout_message="timed out") == "abc"
    finally:
        callback.shutdown()
