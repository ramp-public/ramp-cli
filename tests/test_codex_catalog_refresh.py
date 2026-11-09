"""Refresh only an identified existing backend; never claim Desktop rendered it."""

import asyncio
import json
import os
import stat
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from ramp_cli import codex_desktop


@pytest.fixture
def backend(tmp_path, monkeypatch):
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps({"models": [{"slug": "new"}, {"slug": "hidden"}]}))
    config = tmp_path / "config.toml"
    config.write_text(
        f"model_catalog_json = {json.dumps(str(catalog))}\n"
        'model_provider = "ramp-router"\n'
        "[model_providers.ramp-router]\n"
        'base_url = "http://127.0.0.1:9/v1"\n'
        'wire_api = "responses"\n'
        "[model_providers.ramp-router.auth]\n"
        'command = "/bin/false"\n'
    )
    data = tomllib.loads(config.read_text())
    state = SimpleNamespace(
        config=config,
        catalog=catalog,
        calls=[],
        response={
            "config": data,
            "layers": [{"name": {"type": "user", "file": str(config)}}],
        },
        pages=[{"data": [{"id": "new"}, {"id": "hidden"}], "nextCursor": None}],
        errors={},
        malformed=None,
        hang=False,
        socket_path=tmp_path / "app-server-control" / "app-server-control.sock",
    )
    original_lstat = Path.lstat

    def lstat(path, *args, **kwargs):
        if path.name == "app-server-control.sock":
            return SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=os.getuid())
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send(self, raw):
            message = json.loads(raw)
            if "id" in message:
                state.calls.append(message)

        async def recv(self):
            if state.hang:
                await asyncio.Future()
            if state.malformed is not None:
                return state.malformed
            message = state.calls[-1]
            method = message["method"]
            if method in state.errors:
                return json.dumps({"id": message["id"], "error": state.errors[method]})
            result = {}
            if method == "config/read":
                result = state.response
            elif method == "model/list":
                result = state.pages.pop(0)
            return json.dumps({"id": message["id"], "result": result})

    def connect(path, **kwargs):
        assert path == str(state.socket_path)
        assert kwargs["proxy"] is None
        return Connection()

    monkeypatch.setattr(codex_desktop, "unix_connect", connect)
    monkeypatch.setattr(
        codex_desktop.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("Refresh must not spawn a process"),
    )
    return state


def refresh(backend):
    return codex_desktop.refresh_catalog(backend.config, backend.catalog)


def test_confirms_full_membership_without_config_or_auth_changes(backend):
    before = backend.config.read_bytes()
    backend.pages = [
        {"data": [{"id": "new"}], "nextCursor": "page-2"},
        {"data": [{"id": "hidden"}], "nextCursor": None},
    ]
    assert refresh(backend) == {
        "status": "backend_confirmed",
        "reason": "catalog_matched",
    }
    assert backend.config.read_bytes() == before
    assert not (backend.config.parent / "auth.json").exists()
    patch = [
        call for call in backend.calls if call["method"] == "modelProvider/config/write"
    ]
    assert [call["params"] for call in patch] == [
        {"settings": {"type": "customGateway", "providerId": "ramp-router"}}
    ]
    assert backend.calls[-1]["params"]["cursor"] == "page-2"


@pytest.mark.parametrize(
    "mismatch", ["home", "provider", "catalog", "endpoint", "auth", "no_layers"]
)
def test_rejects_wrong_backend_before_patch(backend, mismatch):
    effective = backend.response["config"]
    if mismatch == "home":
        backend.response["layers"][0]["name"]["file"] = "/elsewhere/config.toml"
    elif mismatch == "no_layers":
        backend.response.pop("layers")
    elif mismatch == "provider":
        effective["model_provider"] = "openai"
    elif mismatch == "catalog":
        effective["model_catalog_json"] = "/elsewhere/models.json"
    elif mismatch == "endpoint":
        effective["model_providers"]["ramp-router"]["base_url"] = "https://elsewhere/v1"
    else:
        effective["model_providers"]["ramp-router"]["auth"]["command"] = "/another"
    assert refresh(backend) == {"status": "disk_only", "reason": "backend_mismatch"}
    assert [call["method"] for call in backend.calls] == ["initialize", "config/read"]


@pytest.mark.parametrize(
    "code,reason",
    [(-32600, "unsupported_rpc"), (-32601, "unsupported_rpc"), (-32000, "rpc_failed")],
)
def test_rpc_rejection_preserves_saved_catalog_without_exposing_errors(
    backend, code, reason
):
    before = backend.catalog.read_bytes()
    backend.errors["modelProvider/config/write"] = {"code": code, "message": "secret"}
    result = refresh(backend)
    assert result == {"status": "disk_only", "reason": reason}
    assert backend.catalog.read_bytes() == before
    assert "secret" not in str(result)
    assert backend.calls[-1]["method"] == "modelProvider/config/write"


@pytest.mark.parametrize("ids", [["old"], ["new"], ["new", "hidden", "old"]])
def test_patch_success_is_insufficient_without_exact_membership(backend, ids):
    backend.pages = [{"data": [{"id": item} for item in ids]}]
    assert refresh(backend) == {"status": "disk_only", "reason": "catalog_mismatch"}


def test_repeated_cursor_is_not_confirmation(backend):
    backend.pages = [
        {"data": [{"id": "new"}], "nextCursor": "same"},
        {"data": [{"id": "hidden"}], "nextCursor": "same"},
    ]
    assert refresh(backend) == {"status": "disk_only", "reason": "invalid_response"}


@pytest.mark.parametrize("raw", ["garbage", "[]", "null"])
def test_malformed_reply_is_a_disk_only_result(backend, raw):
    backend.malformed = raw
    assert refresh(backend) == {"status": "disk_only", "reason": "invalid_response"}


def test_nonresponsive_backend_is_bounded(backend):
    backend.hang = True
    assert refresh(backend) == {"status": "disk_only", "reason": "timeout"}


def test_missing_transport_never_connects(tmp_path, monkeypatch):
    # Real filesystem, no synthetic socket lstat.
    config = tmp_path / "config.toml"
    catalog = tmp_path / "models.json"
    catalog.write_text('{"models": [{"slug": "new"}]}')
    config.write_text(
        f"model_catalog_json = {json.dumps(str(catalog))}\n"
        'model_provider = "ramp-router"\n'
        '[model_providers.ramp-router]\nbase_url = "http://localhost/v1"\n'
        '[model_providers.ramp-router.auth]\ncommand = "/bin/false"\n'
    )
    monkeypatch.setattr(
        codex_desktop, "unix_connect", lambda *args, **kwargs: pytest.fail("No socket")
    )
    assert codex_desktop.refresh_catalog(config, catalog) == {
        "status": "disk_only",
        "reason": "transport_missing",
    }


def test_unreachable_socket_is_disk_only(backend, monkeypatch):
    def connect(*args, **kwargs):
        raise ConnectionRefusedError("private config value")

    monkeypatch.setattr(codex_desktop, "unix_connect", connect)
    assert refresh(backend) == {"status": "disk_only", "reason": "backend_unreachable"}


def test_unexpected_backend_error_is_disk_only(backend, monkeypatch):
    def connect(*args, **kwargs):
        raise RuntimeError("private backend detail")

    monkeypatch.setattr(codex_desktop, "unix_connect", connect)
    assert refresh(backend) == {"status": "disk_only", "reason": "unexpected_error"}


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_process_interrupts_are_not_swallowed(backend, monkeypatch, interrupt):
    def connect(*args, **kwargs):
        raise interrupt()

    monkeypatch.setattr(codex_desktop, "unix_connect", connect)
    with pytest.raises(interrupt):
        refresh(backend)


def test_unix_socket_must_belong_to_current_user(backend, monkeypatch):
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda *args, **kwargs: SimpleNamespace(
            st_mode=stat.S_IFSOCK, st_uid=os.getuid() + 1
        ),
    )
    assert refresh(backend) == {"status": "disk_only", "reason": "backend_mismatch"}
    assert backend.calls == []


def test_no_desktop_rendering_claim(backend):
    result = refresh(backend)
    message = codex_desktop.catalog_refresh_message(result)
    assert "backend confirmed" in message
    assert "Desktop attachment and picker rendering are unverified" in message
    assert "Restart Codex" in codex_desktop.catalog_refresh_message(
        {"status": "disk_only", "reason": "unsupported_rpc"}
    )


def test_effective_provider_defaults_do_not_prevent_identity_check(backend):
    provider = backend.response["config"]["model_providers"]["ramp-router"]
    provider["experimental_bearer_token"] = None
    provider["auth"].update({"cwd": "/server/default", "refresh_interval_ms": 300000})
    assert refresh(backend)["status"] == "backend_confirmed"


def test_same_user_daemon_socket_symlink_is_supported(backend, monkeypatch):
    published = backend.socket_path
    backend.socket_path = backend.config.parent / "short.sock"
    original_resolve = Path.resolve
    original_stat = Path.stat

    monkeypatch.setattr(
        Path,
        "lstat",
        lambda *args, **kwargs: SimpleNamespace(
            st_mode=stat.S_IFLNK, st_uid=os.getuid()
        ),
    )

    def resolve(path, *args, **kwargs):
        if path == published:
            return backend.socket_path
        return original_resolve(path, *args, **kwargs)

    def target_stat(path, *args, **kwargs):
        if path == backend.socket_path:
            return SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=os.getuid())
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(Path, "stat", target_stat)
    assert refresh(backend)["status"] == "backend_confirmed"
