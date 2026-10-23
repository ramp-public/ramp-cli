"""Agent opt-in composes with configure without extra prompts or credentials."""

import json
import time
from unittest.mock import Mock

import click
import httpx
import pytest
from click.testing import CliRunner

from ramp_cli.client.router_user import RouterUserClient
from ramp_cli.commands import router
from ramp_cli.commands import router_credentials as onboarding
from ramp_cli.errors import RampCLIError
from ramp_cli.main import cli


@pytest.fixture
def flow(monkeypatch, isolated_config):
    monkeypatch.setenv("HOME", str(isolated_config))
    monkeypatch.setattr(router, "_can_prompt", lambda *_: True)
    monkeypatch.setattr(
        router, "_installed_clients", lambda: ("claude-code", "codex", "opencode", "pi")
    )
    monkeypatch.setattr(router, "_codex_app_installed", lambda: False)
    monkeypatch.setattr(onboarding, "_jwt_account", lambda _: ("user-1", "workspace-1"))
    client = Mock()
    client.access_token.return_value = "fixture-token"
    client.status.return_value = {"connectors_authorized": False}
    client.origin = "https://app.router.com"
    factory = Mock(return_value=client)
    monkeypatch.setattr(onboarding, "RouterUserClient", factory)
    picker = Mock(return_value=("claude-code", "codex", "opencode", "pi"))
    monkeypatch.setattr(router, "_pick_clients", picker)
    profiles = Mock(return_value=[{"id": "default-profile", "is_default": True}])
    monkeypatch.setattr(onboarding, "_routing_profiles", profiles)
    create = Mock(return_value={"secret": "fixture-key", "id": "key-1"})
    monkeypatch.setattr(onboarding, "create_named_key", create)

    def get(url, **kwargs):
        if url.endswith("/client/sync-version"):
            return httpx.Response(200, json={"version": "v1"})
        if url.endswith("/models"):
            if kwargs["headers"].get("X-Gateway-Client") == "codex":
                body = {
                    "models": [
                        {
                            "slug": "gpt-5.4",
                            "display_name": "gpt-5.4",
                            "base_instructions": "",
                        }
                    ]
                }
            else:
                body = {
                    "data": [
                        {
                            "id": "gpt-5.4",
                            "router": {
                                "schema_version": 1,
                                "request_name": "gpt-5.4",
                                "display_name": "gpt-5.4",
                                "description": "",
                                "listing": {"order": 0},
                                "limits": {
                                    "context_window": 128000,
                                    "max_output_tokens": 16384,
                                },
                                "capabilities": {
                                    "modalities": {"input": ["text"]},
                                    "tools": {"supported": True},
                                    "reasoning": {"efforts": [], "default_effort": ""},
                                },
                            },
                        }
                    ]
                }
            return httpx.Response(200, json=body, request=httpx.Request("GET", url))
        return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr(router.httpx, "get", get)
    monkeypatch.setattr(router, "_fetch_configure_summary", lambda *_a, **_k: None)
    monkeypatch.setattr(router, "_codex_harness_prompt", lambda *_a, **_k: None)
    monkeypatch.setattr(
        router.codex_desktop,
        "restart_running",
        lambda: pytest.fail("Do not close desktop sessions"),
    )
    writer = Mock(wraps=router._configure_client)
    monkeypatch.setattr(router, "_configure_client", writer)
    monkeypatch.setattr(router.router_repo, "service_supported", lambda: True)
    enable = Mock()
    monkeypatch.setattr(router, "router_agent_enable", enable)
    monkeypatch.setattr(
        router.router_schedule, "background_status", lambda: {"state": "running"}
    )
    monkeypatch.setattr(router.router_repo, "status", lambda: {"healthy": True})
    return {
        "client": client,
        "factory": factory,
        "picker": picker,
        "profiles": profiles,
        "create": create,
        "writer": writer,
        "enable": enable,
    }


def invoke(*args):
    return CliRunner().invoke(
        cli, ["--human", "router", "configure", "--enable-agent", *args]
    )


def test_setup_signs_in_once_picks_once_creates_once_and_configures(
    flow, isolated_config
):
    result = invoke()
    assert result.exit_code == 0, result.output
    flow["client"].access_token.assert_called_once_with(check_session=True)
    flow["client"].login.assert_not_called()  # auth owns first login and renewal
    flow["picker"].assert_called_once()
    flow["create"].assert_called_once()
    assert flow["create"].call_args.args[1:3] == (
        "Ramp CLI",
        {"id": "default-profile", "is_default": True},
    )
    assert all(call.args[1] == "fixture-key" for call in flow["writer"].call_args_list)
    flow["enable"].assert_called_once()
    assert "fixture-key" not in result.output
    saved = list((isolated_config / "ramp/router-setup").glob("*.json"))
    assert len(saved) == 1 and saved[0].stat().st_mode & 0o777 == 0o600
    assert invoke().exit_code == 0
    assert flow["create"].call_count == 1
    assert flow["profiles"].call_count == 1
    flow["client"].close.assert_called()


def test_agent_enabled_only_after_success(flow, monkeypatch):
    events = []
    flow["writer"].side_effect = lambda *_a, **_k: (
        events.append("configure")
        or (router._client_config_path(_a[0]), "fixture-model", False)
    )

    # ctx.invoke requires a Click command, not the test mock.
    @click.command()
    @click.option("--port", "helper_port", default=None)
    def enable(helper_port):
        events.append("enable")

    monkeypatch.setattr(router, "router_agent_enable", enable)
    result = invoke("--enable-agent")
    assert result.exit_code == 0, result.output
    assert events == ["configure"] * 4 + ["enable"]


def test_partial_configuration_does_not_enable_and_reuses_key_on_retry(flow):
    flow["writer"].side_effect = click.ClickException("Pi write failed")
    result = invoke("--enable-agent")
    assert result.exit_code != 0
    flow["enable"].assert_not_called()
    flow["writer"].side_effect = None
    assert invoke().exit_code == 0
    assert flow["create"].call_count == 1


def test_lost_create_response_reuses_durable_idempotency_identity(flow):
    flow["create"].side_effect = [
        click.ClickException("connection lost"),
        {"secret": "fixture-key"},
    ]
    assert invoke().exit_code != 0
    assert invoke().exit_code == 0
    assert (
        flow["create"].call_args_list[0].args[3]
        == flow["create"].call_args_list[1].args[3]
    )


def test_cancellation_creates_no_key_or_configuration(flow):
    flow["picker"].return_value = ()
    assert invoke().exit_code != 0
    flow["create"].assert_not_called()
    flow["writer"].assert_not_called()


def test_account_and_workspace_switch_do_not_reuse_old_secret(flow, monkeypatch):
    for identity in (
        ("user-1", "workspace-1"),
        ("user-2", "workspace-1"),
        ("user-1", "workspace-2"),
    ):
        monkeypatch.setattr(
            onboarding, "_jwt_account", lambda _, identity=identity: identity
        )
        assert invoke().exit_code == 0
    assert flow["create"].call_count == 3


@pytest.mark.parametrize(
    "failure",
    [
        "not_interactive",
        "no_agent",
        "opt_out",
        "mismatched_origin",
        "invalid_url",
        "windows",
    ],
)
def test_invalid_setup_fails_before_login_or_writes(flow, monkeypatch, failure):
    if failure == "not_interactive":
        monkeypatch.setattr(router, "_can_prompt", lambda *_: False)
    elif failure == "no_agent":
        monkeypatch.setattr(router.router_repo, "service_supported", lambda: False)
    elif failure == "opt_out":
        monkeypatch.setenv(router.SYNC_OPT_OUT_ENV, "1")
    elif failure == "mismatched_origin":
        monkeypatch.setenv("RAMP_ROUTER_UI_URL", "https://unrelated.example")
    elif failure == "invalid_url":
        monkeypatch.setenv("RAMP_ROUTER_BASE_URL", "http://remote.example/v1")
    else:
        monkeypatch.setattr(router.sys, "platform", "win32")
    assert invoke("--enable-agent").exit_code != 0
    flow["factory"].assert_not_called()
    flow["create"].assert_not_called()
    flow["writer"].assert_not_called()


@pytest.mark.parametrize("args", [["--agent"], ["--no-input"], ["--output", "json"]])
def test_machine_modes_reject_setup(flow, args):
    result = CliRunner().invoke(cli, [*args, "router", "configure", "--enable-agent"])
    assert result.exit_code != 0
    flow["factory"].assert_not_called()


def test_open_code_jsonc_is_single_writer_path(flow, monkeypatch, isolated_config):
    path = isolated_config / "opencode/opencode.jsonc"
    path.parent.mkdir()
    path.write_text('{/*keep*/ "theme":"user"}')
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    flow["picker"].return_value = ("opencode",)
    assert invoke().exit_code == 0
    assert router._json_config_path("opencode") == path
    assert not path.with_suffix(".json").exists()


def test_ambiguous_opencode_is_rejected_before_key_creation(
    flow, monkeypatch, isolated_config
):
    folder = isolated_config / "opencode"
    folder.mkdir()
    for name in ("opencode.json", "opencode.jsonc"):
        (folder / name).write_text("{}")
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    flow["picker"].return_value = ("opencode",)
    result = invoke()
    assert result.exit_code != 0 and "Multiple OpenCode" in result.output
    flow["create"].assert_not_called()
    flow["writer"].assert_not_called()


def test_insecure_saved_key_fails_closed(flow, isolated_config):
    assert invoke().exit_code == 0
    path = next((isolated_config / "ramp/router-setup").glob("*.json"))
    path.chmod(0o644)
    assert invoke().exit_code != 0
    assert flow["create"].call_count == 1


def test_symlink_key_state_fails_closed(tmp_path):
    target = tmp_path / "target"
    target.write_text(json.dumps({"request_id": "x", "secret": "secret"}))
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(click.ClickException):
        onboarding._read_state(link)


def test_first_login_opens_only_one_browser_flow(flow):
    flow["client"].access_token.side_effect = [
        RampCLIError("signed out", code=4),
        "fixture-token",
    ]
    assert invoke().exit_code == 0
    flow["client"].login.assert_called_once_with(no_browser=False)
    assert flow["picker"].call_count == 1


def test_expired_connector_grant_never_replaced_with_ordinary_login(flow):
    flow["client"].status.return_value = {"connectors_authorized": True}
    flow["client"].access_token.side_effect = RampCLIError("renewal required", code=4)
    result = invoke()
    assert result.exit_code != 0 and "connector sign-in" in result.output
    flow["client"].login.assert_not_called()
    flow["create"].assert_not_called()
    flow["writer"].assert_not_called()


@pytest.mark.parametrize("connectors", [False, True])
def test_expired_access_silently_refreshes_without_browser(monkeypatch, connectors):
    client = RouterUserClient("https://app.router.com", no_input=True)
    authorization = {"resource": "fixture-connectors"} if connectors else None
    client._save(
        {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "issued_at": int(time.time()) - 3600,
            "expires_in": 900,
            "refresh_token_expires_in": 86400,
            "connector_authorization": authorization,
        }
    )
    login = Mock(side_effect=AssertionError("silent refresh must not open a browser"))
    monkeypatch.setattr(client, "login", login)
    attempts = []

    def post(_http, url, **kwargs):
        assert url == "https://app.router.com/api/auth/cli/token"
        body = kwargs["json"]
        assert body["grant_type"] == "refresh_token"
        assert body["refresh_token"] == "old-refresh"
        attempts.append(body)
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": body["next_refresh_token"],
                "token_type": "Bearer",
                "expires_in": 900,
                "refresh_token_expires_in": 86400,
                "connector_authorization": authorization,
            },
        )

    monkeypatch.setattr(httpx.Client, "post", post)
    try:
        assert onboarding.sign_in(client) == "new-access"
        assert len(attempts) == 1
        assert client._load()["refresh_token"] == attempts[0]["next_refresh_token"]
        assert client._load()["connector_authorization"] == authorization
        login.assert_not_called()
    finally:
        client.close()


def test_pi_setup_is_not_blocked_by_unrelated_ambiguous_opencode(
    flow, monkeypatch, isolated_config
):
    folder = isolated_config / "opencode"
    folder.mkdir()
    for name in ("opencode.json", "opencode.jsonc"):
        (folder / name).write_text("{}")
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    flow["picker"].return_value = ("pi",)
    result = invoke()
    assert result.exit_code == 0, result.output
    flow["create"].assert_called_once()
    flow["writer"].assert_called_once()
    assert router.configured_router_clients() == ("pi",)
    assert router._clients_with_a_receipt() == ("pi",)


@pytest.mark.parametrize(
    "tagging",
    [
        {"healthy": False},
        {
            "healthy": True,
            "refresh_failures": [{"client": "pi", "error": "write failed"}],
        },
    ],
)
def test_service_not_healthy_is_not_reported_as_success(flow, monkeypatch, tagging):
    @click.command()
    @click.option("--port", "helper_port", default=None)
    def enable(helper_port):
        pass

    monkeypatch.setattr(router, "router_agent_enable", enable)
    monkeypatch.setattr(router.router_repo, "status", lambda: tagging)
    result = invoke("--enable-agent")
    assert result.exit_code != 0 and "agent is not running" in result.output
    assert "Router setup complete" not in result.output


@pytest.mark.parametrize("ambiguous_opencode", [False, True])
def test_actual_writers_keep_notes_jsonc_and_one_prompt(
    flow, monkeypatch, isolated_config, ambiguous_opencode
):

    monkeypatch.setattr(router, "_codex_harness_prompt", lambda *_a, **_k: None)
    monkeypatch.setattr(router, "_fetch_configure_summary", lambda *_a, **_k: None)
    monkeypatch.setattr(
        router,
        "_codex_hook_trust_note_lines",
        lambda *_: ["Fixture: trust the Codex hooks"],
    )

    def forbidden(*_a, **_k):
        pytest.fail(
            "setup must not launch another picker, browser, or restart desktop apps"
        )

    monkeypatch.setattr(router, "_pick_claude_models", forbidden)
    monkeypatch.setattr(router, "_pick_stored_router_api_key", forbidden)
    monkeypatch.setattr(router, "acquire_router_api_key", forbidden)
    monkeypatch.setattr(router.router_ui, "launch", forbidden)
    monkeypatch.setattr(router.codex_desktop, "restart_running", forbidden)
    monkeypatch.setenv("RAMP_OPENCODE_MAJOR", "2")
    path = isolated_config / "opencode/opencode.jsonc"
    path.parent.mkdir()
    path.write_text(
        '{ /* a user comment */ "theme": "user", "model": "other-provider/model" }'
    )
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    clients = onboarding.HARNESSES
    if ambiguous_opencode:
        path.with_suffix(".json").write_text("{}")
        clients = tuple(h for h in clients if h != "opencode")
        flow["picker"].return_value = clients
    result = invoke()
    assert result.exit_code == 0, result.output
    assert flow["picker"].call_count == 1
    assert path.exists() and path.with_suffix(".json").exists() == ambiguous_opencode
    assert "user" in path.read_text()
    assert "Fixture: trust the Codex hooks" in result.output
    if not ambiguous_opencode:
        assert "other-provider/model" in result.output
    assert "fixture-key" not in result.output
    assert "unconfigure" in result.output
    for harness in clients:
        assert (
            router._stored_router_api_key(harness, router._client_config_path(harness))
            == "fixture-key"
        )
    if ambiguous_opencode:
        assert "opencode" not in router.configured_router_clients()
        configured = CliRunner().invoke(
            cli,
            [
                "--human",
                "router",
                "configure",
                "pi",
                "--api-key",
                "fixture-key",
                "--no-browser",
            ],
        )
        assert configured.exit_code == 0, configured.output
        refreshed = CliRunner().invoke(cli, ["--human", "router", "refresh"])
        assert refreshed.exit_code == 0, refreshed.output
        router.router_schedule.enable_background()
        scheduled = CliRunner().invoke(
            cli, ["--human", "router", "sync", "--scheduled"]
        )
        assert scheduled.exit_code == 0, scheduled.output
        assert router.router_schedule.load_state().get("last_full_refresh")
        monkeypatch.setattr(router.router_ui, "launch", lambda *_a, **_k: False)
        monkeypatch.setattr(router, "_can_prompt", lambda *_a: False)
        restored = CliRunner().invoke(cli, ["--agent", "router", "unconfigure"])
        assert restored.exit_code == 0, restored.output
        for harness in clients:
            assert not (
                router._client_config_path(harness).parent / "ramp-router-state.json"
            ).exists()
        assert path.with_suffix(".json").read_text() == "{}"
        assert "other-provider/model" in path.read_text()
