"""Router trusts the Codex hooks it writes, and only those."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import textwrap
import tomllib

import pytest
from click.testing import CliRunner

from ramp_cli import codex_hook_trust
from ramp_cli.commands import router as router_module
from ramp_cli.main import cli

from .test_router import _mock_models

# conftest replaces this for every test so none starts a real Codex.
_real_trust = codex_hook_trust.trust_router_hooks

_SNAKE = {"sessionStart": "session_start", "stop": "stop"}
_TABLE = {"sessionStart": "SessionStart", "stop": "Stop"}


def _fake_app_server(tmp_path, hooks, *, fail=None):
    """A stand-in `codex app-server` that answers from ``hooks`` and logs writes."""
    log = tmp_path / "writes.jsonl"
    script = tmp_path / "codex"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json, sys
            hooks = {hooks!r}
            fail = {fail!r}
            for line in sys.stdin:
                message = json.loads(line)
                method = message.get("method")
                if "id" not in message:
                    continue
                if method == fail:
                    reply = {{"error": {{"code": -32601, "message": "no"}}}}
                elif method == "hooks/list":
                    reply = {{"result": {{"data": [{{"cwd": "/", "hooks": hooks}}]}}}}
                elif method == "config/batchWrite":
                    with open({str(log)!r}, "a") as out:
                        out.write(json.dumps(message["params"]) + "\\n")
                    reply = {{"result": {{"status": "ok"}}}}
                else:
                    reply = {{"result": {{}}}}
                print(json.dumps({{"method": "noise"}}), flush=True)
                print(json.dumps({{"id": message["id"], **reply}}), flush=True)
            """
        )
    )
    script.chmod(0o755)
    return script, log


def _hook(config_path, event, command, *, index=0, status="untrusted", **extra):
    return {
        "key": f"{config_path}:{_SNAKE[event]}:{index}:0",
        "currentHash": f"sha256:{event}{index}",
        "trustStatus": status,
        "eventName": event,
        "handlerType": "command",
        "command": command,
        "source": "user",
        "sourcePath": str(config_path),
        "isManaged": False,
        **extra,
    }


def test_trusts_only_the_exact_commands_router_wrote(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("")
    ours = _hook(config_path, "sessionStart", "ramp-sync")
    already = _hook(config_path, "stop", "ramp-cost", index=0, status="trusted")
    hooks = [
        ours,
        already,
        # Same command under another event, a user's own hook, and hooks
        # from other sources all stay for the user to review.
        _hook(config_path, "stop", "ramp-sync", index=1),
        _hook(config_path, "stop", "user-own", index=2),
        _hook(config_path, "stop", "ramp-cost", index=3, source="project"),
        _hook(config_path, "stop", "ramp-cost", index=4, isManaged=True),
        _hook(
            config_path,
            "stop",
            "ramp-cost",
            index=5,
            sourcePath=str(tmp_path / "other.toml"),
        ),
    ]
    codex, log = _fake_app_server(tmp_path, hooks)

    trusted = _real_trust(
        codex, config_path, {("sessionStart", "ramp-sync"), ("stop", "ramp-cost")}
    )

    assert trusted == {
        ours["key"]: ours["currentHash"],
        already["key"]: already["currentHash"],
    }
    (write,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert write == {
        "filePath": str(config_path),
        "edits": [
            {
                "keyPath": "hooks.state",
                "mergeStrategy": "upsert",
                "value": {ours["key"]: {"trusted_hash": ours["currentHash"]}},
            }
        ],
    }


def test_nothing_is_written_when_router_hooks_are_already_trusted(tmp_path):
    config_path = tmp_path / "config.toml"
    hook = _hook(config_path, "stop", "ramp-cost", status="trusted")
    codex, log = _fake_app_server(tmp_path, [hook])
    assert _real_trust(codex, config_path, {("stop", "ramp-cost")}) == {
        hook["key"]: hook["currentHash"]
    }
    assert not log.exists()


def test_a_retained_hook_keeps_recorded_trust_but_is_never_newly_trusted(tmp_path):
    config_path = tmp_path / "config.toml"
    kept = _hook(config_path, "stop", "ramp-cost", status="trusted")
    unreviewed = _hook(config_path, "stop", "ramp-cost", index=1)
    edited = _hook(config_path, "stop", "ramp-cost", index=2, status="trusted")
    codex, log = _fake_app_server(tmp_path, [kept, unreviewed, edited])
    recorded = {kept["key"]: kept["currentHash"], edited["key"]: "sha256:before"}

    trusted = _real_trust(codex, config_path, set(), {("stop", "ramp-cost")}, recorded)

    assert trusted == {kept["key"]: kept["currentHash"]}
    assert not log.exists()


@pytest.mark.parametrize("fail", ["hooks/list", "initialize"])
def test_an_older_codex_leaves_trust_unavailable(tmp_path, fail):
    config_path = tmp_path / "config.toml"
    codex, _ = _fake_app_server(tmp_path, [], fail=fail)
    with pytest.raises(codex_hook_trust.TrustUnavailable):
        _real_trust(codex, config_path, {("stop", "ramp-cost")})


def test_a_failed_initialize_reaps_the_app_server(tmp_path, monkeypatch):
    codex, _ = _fake_app_server(tmp_path, [], fail="initialize")
    started = []
    real_popen = codex_hook_trust.subprocess.Popen

    def popen(*args, **kwargs):
        started.append(real_popen(*args, **kwargs))
        return started[-1]

    monkeypatch.setattr(codex_hook_trust.subprocess, "Popen", popen)
    with pytest.raises(codex_hook_trust.TrustUnavailable):
        _real_trust(codex, tmp_path / "config.toml", {("stop", "ramp-cost")})
    (process,) = started
    assert process.returncode is not None
    assert process.stdin.closed and process.stdout.closed


def test_a_broken_stdin_pipe_does_not_mask_trust_unavailable(tmp_path, monkeypatch):
    # The server exits without reading: initialize's write fails, and its
    # bytes stay buffered, so closing stdin raises BrokenPipeError again.
    codex = tmp_path / "codex"
    codex.write_text(f"#!{sys.executable}\n")
    codex.chmod(0o755)
    closed = []

    class _Stdin:
        def write(self, data):
            raise BrokenPipeError

        def flush(self):
            raise BrokenPipeError

        def close(self):
            raise BrokenPipeError

    real_popen = codex_hook_trust.subprocess.Popen

    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        process.stdin.close()
        process.stdin = _Stdin()
        real_close = process.stdout.close
        process.stdout.close = lambda: (closed.append(True), real_close())
        return process

    monkeypatch.setattr(codex_hook_trust.subprocess, "Popen", popen)
    with pytest.raises(codex_hook_trust.TrustUnavailable):
        _real_trust(codex, tmp_path / "config.toml", {("stop", "ramp-cost")})
    assert closed


def test_a_missing_codex_leaves_trust_unavailable(tmp_path):
    with pytest.raises(codex_hook_trust.TrustUnavailable):
        _real_trust(tmp_path / "missing", tmp_path / "config.toml", {("stop", "x")})
    with pytest.raises(codex_hook_trust.TrustUnavailable):
        _real_trust(None, tmp_path / "config.toml", {("stop", "x")})


@pytest.mark.skipif(shutil.which("codex") is None, reason="needs the Codex CLI")
def test_the_installed_codex_records_trust_for_router_hooks_only(tmp_path):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text(
        "[[hooks.SessionStart]]\n"
        'hooks = [{ type = "command", command = "echo ramp", '
        'statusMessage = "Ramp Router: refreshing model settings" }]\n'
        "\n[[hooks.Stop]]\n"
        'hooks = [{ type = "command", command = "echo user" }]\n'
    )
    trusted = _real_trust(
        shutil.which("codex"), config_path, {("sessionStart", "echo ramp")}
    )
    assert len(trusted) == 1
    state = tomllib.loads(config_path.read_text())["hooks"]["state"]
    assert {key: entry["trusted_hash"] for key, entry in state.items()} == trusted

    with codex_hook_trust._AppServer(shutil.which("codex"), codex_home) as server:
        listed = server.request("hooks/list", {"cwds": [str(codex_home)]})
    hooks = listed["data"][0]["hooks"]
    assert {h["command"]: h["trustStatus"] for h in hooks} == {
        "echo ramp": "trusted",
        "echo user": "untrusted",
    }
    assert {h["statusMessage"] for h in hooks} == {
        "Ramp Router: refreshing model settings",
        None,
    }


def _codex_like_trust(calls):
    """Trust the way Codex does: one [hooks.state."<key>"] table per handler."""

    def trust(_executable, config_path, commands, retained=frozenset(), recorded=None):
        calls.append(commands)
        config = tomllib.loads(config_path.read_text())
        state = config.get("hooks", {}).get("state", {})
        trusted, added = {}, ""
        for event, command in sorted(commands | retained):
            for group_index, group in enumerate(config["hooks"].get(_TABLE[event], [])):
                for handler_index, handler in enumerate(group["hooks"]):
                    if handler["command"] != command:
                        continue
                    key = f"{config_path}:{_SNAKE[event]}:{group_index}:{handler_index}"
                    digest = (
                        "sha256:"
                        + hashlib.sha256(
                            f"{key}{json.dumps(handler)}".encode()
                        ).hexdigest()
                    )
                    current = state.get(key, {}).get("trusted_hash")
                    if (event, command) not in commands:
                        if current == digest == (recorded or {}).get(key):
                            trusted[key] = digest
                        continue
                    trusted[key] = digest
                    if current != digest:
                        added += f'\n[hooks.state."{key}"]\ntrusted_hash = "{digest}"\n'
        config_path.write_text(config_path.read_text() + added)
        return trusted

    return trust


def _state(codex_home):
    return json.loads((codex_home / "ramp-router-state.json").read_text())


def test_configure_trusts_router_hooks_and_unconfigure_removes_that_trust(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch, [{"id": "a"}])
    calls = []
    monkeypatch.setattr(
        router_module.codex_hook_trust, "trust_router_hooks", _codex_like_trust(calls)
    )
    configured = CliRunner().invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )
    assert configured.exit_code == 0, configured.output
    config_path = codex_home / "config.toml"
    config = tomllib.loads(config_path.read_text())

    # Each Router hook carries a label for Codex's review list.
    labels = {
        handler["statusMessage"]
        for event in ("SessionStart", "Stop")
        for group in config["hooks"][event]
        for handler in group["hooks"]
    }
    assert labels == {
        router_module.CODEX_SYNC_HOOK_LABEL,
        router_module.CODEX_STOP_SYNC_HOOK_LABEL,
        router_module.CODEX_COST_HOOK_LABEL,
    }
    (commands,) = calls
    assert {event for event, _ in commands} == {"sessionStart", "stop"}
    recorded = _state(codex_home)[router_module.CODEX_HOOK_TRUST_STATE_KEY]
    assert recorded == {
        key: entry["trusted_hash"] for key, entry in config["hooks"]["state"].items()
    }
    assert len(recorded) == 3
    assert "has not trusted" not in configured.output
    assert router_module._codex_hooks_awaiting_trust(config_path) == []

    # The user's own hook and its trust are not Router's to remove.
    config_path.write_text(
        config_path.read_text()
        + '\n[[hooks.Stop]]\nhooks = [{ type = "command", command = "echo mine" }]\n'
        + '\n[hooks.state."/elsewhere:stop:0:0"]\ntrusted_hash = "sha256:mine"\n'
    )
    unconfigured = CliRunner().invoke(cli, ["router", "unconfigure", "codex"])
    assert unconfigured.exit_code == 0, unconfigured.output
    restored = tomllib.loads(config_path.read_text())
    assert restored["hooks"]["state"] == {
        "/elsewhere:stop:0:0": {"trusted_hash": "sha256:mine"}
    }
    assert restored["hooks"]["Stop"] == [
        {"hooks": [{"type": "command", "command": "echo mine"}]}
    ]
    assert "SessionStart" not in restored["hooks"]


def test_unconfigure_leaves_no_empty_hooks_state_table(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model_provider = "openai"\n')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch, [{"id": "a"}])
    trust = _codex_like_trust([])

    def trust_with_header(executable, config_path, *args):
        # Codex writes a bare [hooks.state] header above its first entry.
        config_path.write_text(config_path.read_text() + "\n[hooks.state]\n")
        return trust(executable, config_path, *args)

    monkeypatch.setattr(
        router_module.codex_hook_trust, "trust_router_hooks", trust_with_header
    )
    configured = CliRunner().invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )
    assert configured.exit_code == 0, configured.output
    unconfigured = CliRunner().invoke(cli, ["router", "unconfigure", "codex"])
    assert unconfigured.exit_code == 0, unconfigured.output
    assert tomllib.loads((codex_home / "config.toml").read_text()) == {
        "model_provider": "openai",
        "mcp_servers": {"ramp": {"url": "https://mcp.ramp.com/mcp"}},
    }


def test_reconfigure_replaces_trust_for_hooks_that_moved(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch, [{"id": "a"}])
    monkeypatch.setattr(
        router_module.codex_hook_trust, "trust_router_hooks", _codex_like_trust([])
    )
    runner = CliRunner()
    args = ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    assert runner.invoke(cli, args).exit_code == 0
    first = _state(codex_home)[router_module.CODEX_HOOK_TRUST_STATE_KEY]

    # A user Stop group ahead of Router's shifts Router's handler keys.
    config_path = codex_home / "config.toml"
    config_path.write_text(
        '[[hooks.Stop]]\nhooks = [{ type = "command", command = "echo mine" }]\n\n'
        + config_path.read_text()
    )
    assert runner.invoke(cli, args).exit_code == 0
    second = _state(codex_home)[router_module.CODEX_HOOK_TRUST_STATE_KEY]
    assert second != first
    state = tomllib.loads(config_path.read_text())["hooks"]["state"]
    assert {key: entry["trusted_hash"] for key, entry in state.items()} == second


def test_configure_still_reports_untrusted_hooks_when_codex_cannot_be_asked(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch, [{"id": "a"}])
    configured = CliRunner().invoke(
        cli, ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    )
    assert configured.exit_code == 0, configured.output
    assert "Codex has not trusted Ramp's hooks yet" in configured.output
    assert router_module.CODEX_HOOK_TRUST_STATE_KEY not in _state(codex_home)


def test_unconfigure_keeps_a_users_disabled_hook_setting(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch, [{"id": "a"}])
    monkeypatch.setattr(
        router_module.codex_hook_trust, "trust_router_hooks", _codex_like_trust([])
    )
    args = ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    assert CliRunner().invoke(cli, args).exit_code == 0
    config_path = codex_home / "config.toml"
    # A state table with no trusted_hash is not one Router recorded.
    config_path.write_text(
        config_path.read_text()
        + '\n[hooks.state."/elsewhere:stop:0:0"]\nenabled = false\n'
    )
    unconfigured = CliRunner().invoke(cli, ["router", "unconfigure", "codex"])
    assert unconfigured.exit_code == 0, unconfigured.output
    restored = tomllib.loads(config_path.read_text())
    assert restored["hooks"]["state"] == {"/elsewhere:stop:0:0": {"enabled": False}}


def test_a_model_change_keeps_trust_for_router_hooks(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch, [{"id": "a"}, {"id": "b"}])
    monkeypatch.setattr(
        router_module.codex_hook_trust, "trust_router_hooks", _codex_like_trust([])
    )
    args = ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    assert CliRunner().invoke(cli, args).exit_code == 0
    config_path = codex_home / "config.toml"
    recorded = _state(codex_home)[router_module.CODEX_HOOK_TRUST_STATE_KEY]
    assert len(recorded) == 3

    monkeypatch.setattr(
        router_module,
        "_fetch_codex_catalog",
        lambda *args, **kwargs: {"models": [{"slug": "a"}, {"slug": "b"}]},
    )
    monkeypatch.setattr(
        router_module.codex_hook_trust,
        "trust_router_hooks",
        lambda *args: pytest.fail("a model change must not reconcile hook trust"),
    )
    router_module._configure_codex(
        config_path,
        "router-secret",
        router_module._fetch_models("router-secret"),
        base_url=router_module._stored_router_base_url("codex", config_path),
        selected_model="b",
        strict_model=True,
    )
    assert _state(codex_home)[router_module.CODEX_HOOK_TRUST_STATE_KEY] == recorded
    state = tomllib.loads(config_path.read_text())["hooks"]["state"]
    assert {key: entry["trusted_hash"] for key, entry in state.items()} == recorded


def test_a_skipped_cost_hook_download_keeps_its_trust(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _mock_models(monkeypatch, [{"id": "a"}])
    monkeypatch.setattr(
        router_module.codex_hook_trust, "trust_router_hooks", _codex_like_trust([])
    )
    args = ["--human", "router", "configure", "codex", "--api-key", "router-secret"]
    assert CliRunner().invoke(cli, args).exit_code == 0
    recorded = _state(codex_home)[router_module.CODEX_HOOK_TRUST_STATE_KEY]
    assert len(recorded) == 3

    # The download fails; the installed script and its Stop group are kept,
    # even when the command configure would write now has changed.
    monkeypatch.setattr(router_module, "_install_cost_script", lambda *a: False)
    monkeypatch.setattr(
        router_module, "_codex_cost_hook_command", lambda *a: pytest.fail("rebuilt")
    )
    assert CliRunner().invoke(cli, args).exit_code == 0
    assert _state(codex_home)[router_module.CODEX_HOOK_TRUST_STATE_KEY] == recorded
    state = tomllib.loads((codex_home / "config.toml").read_text())["hooks"]["state"]
    assert {key: entry["trusted_hash"] for key, entry in state.items()} == recorded


def test_trust_removal_keeps_a_users_other_hook_settings():
    recorded = {"/home/config.toml:stop:0:0": "sha256:ours"}
    config = (
        'model = "a"\n\n[hooks.state."/home/config.toml:stop:0:0"]\n'
        'enabled = false\ntrusted_hash = "sha256:ours"\n'
    )
    assert tomllib.loads(router_module._without_codex_hook_trust(config, recorded)) == {
        "model": "a",
        "hooks": {"state": {"/home/config.toml:stop:0:0": {"enabled": False}}},
    }


def test_trust_removal_ignores_hooks_that_are_not_a_table():
    recorded = {"/home/config.toml:stop:0:0": "sha256:ours"}
    config = "[[hooks]]\nx = 1\n"
    assert router_module._without_codex_hook_trust(config, recorded) == config
    with_ours = (
        'model = "a"\n\n[hooks.state."/home/config.toml:stop:0:0"]\n'
        'trusted_hash = "sha256:ours"\n'
    )
    assert router_module._without_codex_hook_trust(with_ours, recorded) == (
        'model = "a"\n'
    )


def test_trust_recorded_under_the_resolved_path_counts_for_a_symlinked_home(
    tmp_path,
):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real)
    command = router_module.session_sync_hook_command("codex")
    if command is None:
        pytest.skip("no session sync command on this platform")
    (real / "config.toml").write_text(
        router_module._render_codex_sync_hook_group(command)
        + f'\n[hooks.state."{real / "config.toml"}:session_start:0:0"]\n'
        + 'trusted_hash = "sha256:x"\n'
    )
    assert router_module._codex_hooks_awaiting_trust(linked / "config.toml") == []
    (real / "config.toml").write_text(
        router_module._render_codex_sync_hook_group(command)
    )
    assert router_module._codex_hooks_awaiting_trust(linked / "config.toml") == [
        "SessionStart"
    ]
