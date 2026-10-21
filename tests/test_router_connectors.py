import io
import json
import os
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import json5
import pytest
from click.testing import CliRunner

from ramp_cli.auth import oauth
from ramp_cli.client.router_user import RouterUserClient
from ramp_cli.client.router_user import _edge_token as original_edge_token
from ramp_cli.commands import router
from ramp_cli.errors import RampCLIError
from ramp_cli.main import cli
from ramp_cli.router_connectors import auth as credentials
from ramp_cli.router_connectors import bridge, harnesses


def auth(**changes):
    return {
        "resource": "https://api.router.com/v1/mcp",
        "client_id": credentials.CLIENT_ID,
        "scope": "connectors:execute",
        "user_id": "router-user",
        "business_uuid": "business",
        "core_user_uuid": "core-user",
        "session_id": "router-cli:session:initial",
        **changes,
    }


def state(**changes):
    return {
        "access_token": "cli-access-secret",
        "refresh_token": "cli-refresh-secret",
        "token_type": "Bearer",
        "expires_in": 900,
        "refresh_token_expires_in": 3600,
        "issued_at": int(time.time()),
        "connector_authorization": auth(),
        **changes,
    }


def setup(monkeypatch, handler):
    real = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    client = RouterUserClient("https://app.router.com", no_input=True)
    client._save(state())
    return client


def issued(**changes):
    return httpx.Response(
        200,
        json={
            **auth(),
            "access_token": "mcp-access-secret",
            "token_type": "Bearer",
            "expires_in": 300,
            **changes,
        },
    )


def test_combined_login_requests_explicit_consent_and_retains_old_grant_on_incomplete_response(
    monkeypatch,
):
    callback = SimpleNamespace(
        redirect_uri="http://localhost:43123/callback",
        state="state",
        code_challenge="challenge",
        _verifier="verifier",
        wait_for_code=lambda *a, **kw: "code",
        shutdown=lambda: None,
    )
    monkeypatch.setattr(oauth, "start_pkce_callback", lambda: callback)
    urls = []
    monkeypatch.setattr(oauth, "_open_browser", lambda url: urls.append(url) or True)
    client = setup(
        monkeypatch,
        lambda request: httpx.Response(200, json=state(connector_authorization=None)),
    )
    monkeypatch.setattr(client, "ensure_edge_access", lambda **kw: None)
    with pytest.raises(RampCLIError, match="Connector authorization required"):
        client.login(connectors=True)
    query = parse_qs(urlsplit(urls[0]).query)
    assert query["connectors"] == ["1"]
    assert query["resource"] == [auth()["resource"]]
    assert query["client_id"] == [credentials.CLIENT_ID]
    assert client._load()["connector_authorization"] == auth()


def test_shared_issuance_and_same_user_relogin_never_retargets_binding(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        assert request.url.path == "/api/auth/cli/mcp-token"
        assert request.headers["authorization"] == "Bearer cli-refresh-secret"
        assert json.loads(request.content) == {
            k: auth()[k] for k in credentials.IDENTITY_FIELDS
        }
        return issued()

    client = setup(monkeypatch, handle)
    binding = credentials.create_binding(client)
    with ThreadPoolExecutor(max_workers=4) as pool:
        tokens = list(
            pool.map(
                lambda _: credentials.issue(
                    client, credentials.authorization(client), binding
                ),
                range(4),
            )
        )
    assert tokens == ["mcp-access-secret"] * 4
    assert len(requests) == 1
    assert "secret" not in credentials._binding_path(binding).read_text()
    client._save(
        state(connector_authorization=auth(session_id="router-cli:session:replacement"))
    )
    old = credentials.load_binding(binding)
    with pytest.raises(RampCLIError, match="authorization changed"):
        credentials.issue(client, old["authorization"], binding)
    assert len(requests) == 1
    client._clear()
    with pytest.raises(RampCLIError, match="Sign in"):
        credentials.issue(client, old["authorization"], binding)


def test_issuance_finishes_pending_cli_rotation_and_uses_new_secret(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/api/auth/cli/token":
            body = json.loads(request.content)
            assert body["next_refresh_token"] == "cli.next-secret"
            return httpx.Response(200, json=state(refresh_token="cli.next-secret"))
        assert request.headers["authorization"] == "Bearer cli.next-secret"
        return issued()

    client = setup(monkeypatch, handle)
    client._save(state(pending_refresh_token="cli.next-secret"))
    credentials.create_binding(client)
    assert len(requests) == 2
    assert "pending_refresh_token" not in client._load()


@pytest.mark.parametrize(
    "change",
    [
        {"resource": "https://evil.example/v1/mcp"},
        {"session_id": "another-session"},
        {"business_uuid": "another-business"},
        {"core_user_uuid": "another-core-user"},
        {"user_id": "another-router-user"},
        {"client_id": "another-client"},
        {"expires_in": 301},
    ],
)
def test_issuance_rejects_identity_or_lifetime_mismatch_without_disclosing_secrets(
    monkeypatch, change
):
    client = setup(monkeypatch, lambda request: issued(**change))
    with pytest.raises(RampCLIError) as error:
        credentials.create_binding(client)
    assert "secret" not in str(error.value)


def test_legacy_grant_fails_without_browser_or_exchange(monkeypatch):
    client = setup(
        monkeypatch, lambda request: pytest.fail("Network request from legacy grant")
    )
    client._save(state(connector_authorization=None))
    monkeypatch.setattr(
        oauth, "_open_browser", lambda url: pytest.fail("Browser opened")
    )
    with pytest.raises(RampCLIError, match="Connector authorization required") as error:
        credentials.create_binding(client)
    assert error.value.code == 4


@pytest.mark.parametrize("harness", harnesses.HARNESSES)
@pytest.mark.parametrize(
    "executable_name", ["ramp", "ramp-darwin-arm64", "ramp-windows-amd64.exe"]
)
def test_registration_preserves_settings_rebinds_only_owned_entry_and_disable_is_scoped(
    monkeypatch, tmp_path, harness, executable_name
):
    path = tmp_path / ("config.toml" if harness == "codex" else "config.json")
    monkeypatch.setattr(harnesses, "config_path", lambda _: path)
    executable = tmp_path / "bin" / executable_name
    executable.parent.mkdir()
    executable.touch(mode=0o700)
    monkeypatch.setattr(harnesses.shutil, "which", lambda _: None)
    monkeypatch.setattr(sys, "argv", [str(executable)])
    key = (
        "mcp_servers"
        if harness == "codex"
        else "mcp"
        if harness == "opencode"
        else "mcpServers"
    )
    unrelated = {"command": "slack", "args": ["keep"]}
    if harness == "codex":
        path.write_text(
            '# keep this comment\nmodel = "custom"\n[mcp_servers.slack]\ncommand = "slack"\nargs = ["keep"]\n'
        )
    else:
        path.write_text(json.dumps({"model": "custom", key: {"slack": unrelated}}))

    def parse():
        return (
            tomllib.loads(path.read_text())
            if harness == "codex"
            else json.loads(path.read_text())
        )

    initial = parse()
    harnesses.enable(harness, "a" * 32)
    harnesses.enable(harness, "b" * 32)
    assert harnesses.status(harness)["binding_id"] == "b" * 32
    assert parse()[key]["slack"] == unrelated
    assert "secret" not in path.read_text()
    harnesses.disable(harness)
    assert parse() == initial
    if harness == "codex":
        assert "# keep this comment" in path.read_text()
    else:
        data = parse()
        data[key]["ramp-connectors"] = {"command": "third-party", "args": []}
        path.write_text(json.dumps(data))
        before = path.read_text()
        with pytest.raises(RampCLIError, match="not CLI-owned"):
            harnesses.enable(harness, "a" * 32)
        with pytest.raises(RampCLIError, match="not CLI-owned"):
            harnesses.disable(harness)
        assert path.read_text() == before


@pytest.mark.parametrize("enabled", [True, False])
def test_codex_registration_preserves_concurrent_config_changes(
    monkeypatch, tmp_path, enabled
):
    path = tmp_path / "config.toml"
    path.write_text('model = "original"\n')
    monkeypatch.setattr(harnesses, "config_path", lambda _: path)
    monkeypatch.setattr(harnesses, "_executable", lambda: "/usr/local/bin/ramp")
    if not enabled:
        harnesses.enable("codex", "a" * 32)
    original_lock = router.codex_config_lock
    saved = []

    @contextmanager
    def concurrent_update(lock_path):
        with original_lock(lock_path):
            # Another writer commits before this transaction acquires the lock.
            path.write_text(path.read_text().replace('"original"', '"concurrent"'))
            yield
            saved.append(tomllib.loads(path.read_text()))

    monkeypatch.setattr(router, "codex_config_lock", concurrent_update)
    if enabled:
        harnesses.enable("codex", "a" * 32)
    else:
        harnesses.disable("codex")

    data = tomllib.loads(path.read_text())
    assert data["model"] == "concurrent"
    assert ("ramp-connectors" in data.get("mcp_servers", {})) is enabled
    assert saved == [data]


def test_jsonc_registration_preserves_unrelated_source_and_custom_path(
    monkeypatch, tmp_path
):
    path = tmp_path / "custom.jsonc"
    monkeypatch.setenv("OPENCODE_CONFIG", str(path))
    monkeypatch.setattr(harnesses, "_executable", lambda: "/usr/local/bin/ramp")
    unrelated = '/* provider comment */\n  provider: {name: "custom"},'
    slack = '// keep slack\n    slack: {type: "remote", url: "https://slack.example/mcp"}, // trailing comment'
    path.write_text("{\n" + unrelated + "\n  mcp: {\n" + slack + "\n  },\n}\n")
    before = json5.loads(path.read_text())
    harnesses.enable("opencode", "a" * 32)
    harnesses.enable("opencode", "b" * 32)
    assert unrelated in path.read_text()
    assert slack in path.read_text()
    harnesses.disable("opencode")
    assert json5.loads(path.read_text()) == before
    assert unrelated in path.read_text()
    assert slack in path.read_text()


def test_inline_codex_mcp_configuration_is_not_lossily_rewritten(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    text = 'model = "custom"\nmcp_servers = {slack = {command = "slack"}}\n'
    path.write_text(text)
    monkeypatch.setattr(harnesses, "config_path", lambda _: path)
    monkeypatch.setattr(harnesses, "_executable", lambda: "/usr/local/bin/ramp")
    with pytest.raises(RampCLIError, match="Cannot safely edit"):
        harnesses.enable("codex", "a" * 32)
    assert path.read_text() == text


def test_bridge_startup_failure_leaves_stdout_empty():
    result = CliRunner().invoke(
        cli, ["router", "connectors", "bridge", "--binding", "a" * 32]
    )
    assert result.exit_code == 4
    assert result.stdout == ""
    assert "authorization" in result.stderr


def test_passive_edge_auth_never_invokes_browser_capable_helper(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("ramp_cli.client.router_user._edge_tokens", {})
    monkeypatch.setattr("ramp_cli.client.router_user._edge_token", original_edge_token)
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **kw: pytest.fail("Launched cloudflared")
    )
    cache = tmp_path / ".cloudflared"
    cache.mkdir()
    (cache / "internal.router.com-app-token").write_text("e30.eyJleHAiOjF9.signature")
    client = RouterUserClient(
        "https://internal.router.com", no_input=True, passive_edge=True
    )
    assert client._access_headers() == {}


def test_status_distinguishes_replaced_binding_and_cli_json_contract(
    monkeypatch, tmp_path
):
    client = setup(monkeypatch, lambda request: issued())
    binding = credentials.create_binding(client)
    monkeypatch.setattr(
        harnesses,
        "status",
        lambda h: {"harness": h, "state": "enabled", "binding_id": binding},
    )
    assert credentials.status(client)["harnesses"][0]["state"] == "enabled"
    client._save(
        state(connector_authorization=auth(session_id="router-cli:session:new"))
    )
    assert credentials.status(client)["harnesses"][0]["state"] == "conflict"
    result = CliRunner().invoke(
        cli, ["router", "connectors", "status", "--output", "json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["harnesses"][0]["state"] == "conflict"


def test_bridge_forwards_json_and_sse_preserves_session_and_does_not_follow_redirects(
    monkeypatch, capsys
):
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/api/auth/cli/mcp-token":
            return issued()
        payload = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer mcp-access-secret"
        if payload.get("method") == "initialize":
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "mcp-session-id": "session",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"protocolVersion": "2025-03-26"},
                },
            )
        assert request.headers["mcp-session-id"] == "session"
        assert request.headers["mcp-protocol-version"] == "2025-03-26"
        if payload.get("method") == "tools/list":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=': heartbeat\n\ndata: {"jsonrpc":"2.0","id":2,"result":{"tools":[]}}\n\n',
            )
        return httpx.Response(307, headers={"location": "https://evil.example/token"})

    client = setup(monkeypatch, handle)
    binding = credentials.create_binding(client)
    instance = bridge.Bridge(binding)
    try:
        instance.send({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        instance.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        instance.send({"jsonrpc": "2.0", "id": 3, "method": "redirect"})
    finally:
        instance.close()
    output = capsys.readouterr()
    messages = [json.loads(line) for line in output.out.splitlines()]
    assert [m["id"] for m in messages] == [1, 2, 3]
    assert messages[1]["result"] == {"tools": []}
    assert messages[2]["error"]["code"] == -32000
    assert "secret" not in output.out + output.err
    assert all(request.url.host != "evil.example" for request in requests)


def test_stdio_parse_errors_are_protocol_messages_not_cli_output(monkeypatch, capsys):
    client = setup(monkeypatch, lambda request: issued())
    binding = credentials.create_binding(client)
    monkeypatch.setattr(
        "sys.stdin", SimpleNamespace(buffer=io.BytesIO(b"not json\n[]\n"))
    )
    bridge.run(binding)
    output = capsys.readouterr()
    assert [json.loads(line)["error"]["code"] for line in output.out.splitlines()] == [
        -32700,
        -32600,
    ]


def test_worker_cache_write_failure_replies_without_disclosing_secrets(
    monkeypatch, capsys
):
    def handle(request):
        if request.url.path == "/api/auth/cli/mcp-token":
            return issued()
        assert json.loads(request.content)["method"] == "initialize"
        cache_path.unlink()
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"protocolVersion": "2025-03-26"},
            },
        )

    client = setup(monkeypatch, handle)
    binding = credentials.create_binding(client)
    cache_path = credentials._directory() / f"token-{binding}.json"

    def fail_write(*args):
        raise OSError("Failed to write private-token-value")

    monkeypatch.setattr(credentials, "_atomic_write", fail_write)
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    stream = b"".join(json.dumps(message).encode() + b"\n" for message in messages)
    monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=io.BytesIO(stream)))
    bridge.run(binding)

    output = capsys.readouterr()
    responses = [json.loads(line) for line in output.out.splitlines()]
    assert [response["id"] for response in responses] == [1, 2]
    assert "result" in responses[0]
    assert responses[1]["error"]["code"] == -32000
    assert output.err
    assert "private-token-value" not in output.out + output.err


def test_credential_files_are_private(monkeypatch):
    client = setup(monkeypatch, lambda request: issued())
    credentials.create_binding(client)
    if os.name != "nt":
        assert all(
            path.stat().st_mode & 0o077 == 0
            for path in credentials._directory().glob("*.json")
        )


def test_saturated_bridge_keeps_cancellation_unblocked(monkeypatch):
    cancelled = Event()
    timed_out = []
    output = []

    def send(message):
        if message.get("method") == "notifications/cancelled":
            cancelled.set()
        elif not cancelled.wait(timeout=2):
            timed_out.append(message["id"])

    instance = SimpleNamespace(send=send, emit=output.append, close=lambda: None)
    monkeypatch.setattr(bridge, "Bridge", lambda _: instance)
    messages = [{"jsonrpc": "2.0", "id": i, "method": "tools/call"} for i in range(17)]
    messages.append(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 0},
        }
    )
    stream = b"".join(json.dumps(message).encode() + b"\n" for message in messages)
    monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=io.BytesIO(stream)))
    bridge.run("unused")
    assert not timed_out
    assert len(output) == 1 and output[0]["id"] == 16
    assert output[0]["error"]["code"] == -32000
