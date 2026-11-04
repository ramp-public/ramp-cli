"""Tests for host-side Claude Desktop Router setup."""

from __future__ import annotations

import json
import os
import plistlib
import stat
import time
from contextlib import contextmanager
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from ramp_cli import __version__, claude_cowork
from ramp_cli.commands import router, router_repo, router_schedule
from ramp_cli.main import cli
from ramp_cli.router_connectors import harnesses


@pytest.fixture
def cowork_host(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_cowork.sys, "platform", "darwin")
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    events = []
    monkeypatch.setattr(claude_cowork, "_ensure_claude_installed", lambda: None)
    monkeypatch.setattr(claude_cowork, "_quit_claude", lambda: events.append("quit"))
    monkeypatch.setattr(claude_cowork, "_claude_is_running", lambda: False)
    monkeypatch.setattr(
        claude_cowork,
        "_launch_claude",
        lambda **_kwargs: events.append("launch"),
    )
    return tmp_path, events


def test_gateway_base_url_removes_only_the_final_v1():
    assert (
        claude_cowork.gateway_base_url("https://router.example/api/v1/")
        == "https://router.example/api"
    )
    assert (
        claude_cowork.gateway_base_url("https://router.example/api")
        == "https://router.example/api"
    )
    with pytest.raises(click.ClickException, match="HTTPS"):
        claude_cowork.gateway_base_url("http://router.example/v1")
    with pytest.raises(click.ClickException, match="valid HTTPS"):
        claude_cowork.gateway_base_url("https://[bad/v1")
    with pytest.raises(click.ClickException, match="valid HTTPS"):
        claude_cowork.gateway_base_url("https://router.example:not-a-port/v1")


def test_is_available_answers_quietly_instead_of_raising(monkeypatch):
    # The configure picker calls this to decide whether Cowork is worth
    # offering, so a host that cannot run the setup must produce False
    # rather than the error preflight would raise.
    monkeypatch.setattr(claude_cowork.sys, "platform", "linux")
    assert claude_cowork.is_available() is False

    monkeypatch.setattr(claude_cowork.sys, "platform", "darwin")
    checked = {}

    def run_quiet(command, *, timeout):
        checked["command"] = command
        return 1

    monkeypatch.setattr(claude_cowork, "_run_quiet", run_quiet)
    assert claude_cowork.is_available() is False
    assert checked["command"] == ["open", "-Ra", "Claude"]

    monkeypatch.setattr(claude_cowork, "_run_quiet", lambda *_a, **_k: 0)
    assert claude_cowork.is_available() is True


def test_successful_configuration_requests_a_fresh_cowork_session(
    cowork_host, monkeypatch
):
    launches = []
    monkeypatch.setattr(
        claude_cowork,
        "_launch_claude",
        lambda **kwargs: launches.append(kwargs),
    )

    claude_cowork.configure("router-secret", "https://router.example/v1")

    assert launches == [{"fresh_cowork": True}]


def test_fresh_cowork_relaunch_uses_the_cowork_deep_link(monkeypatch):
    commands = []

    def record_run(command, **_kwargs):
        commands.append(command)
        return 0

    monkeypatch.setattr(claude_cowork, "_run_quiet", record_run)

    claude_cowork._launch_claude(fresh_cowork=True)

    assert commands == [
        [
            "open",
            "-b",
            claude_cowork.CLAUDE_BUNDLE_ID,
            "claude://cowork/",
        ]
    ]


def test_normal_relaunch_does_not_force_a_new_cowork(monkeypatch):
    commands = []

    def record_run(command, **_kwargs):
        commands.append(command)
        return 0

    monkeypatch.setattr(claude_cowork, "_run_quiet", record_run)

    claude_cowork._launch_claude()

    assert commands == [["open", "-b", claude_cowork.CLAUDE_BUNDLE_ID]]


def test_transaction_lock_excludes_a_second_holder(cowork_host):
    fcntl = pytest.importorskip("fcntl")
    lock_path = claude_cowork.state_path().parent / ".ramp-router-cowork.lock"

    with claude_cowork.transaction_lock():
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
        with lock_path.open("a+b") as second:
            with pytest.raises(OSError):
                fcntl.flock(second.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    with claude_cowork.transaction_lock():
        pass


def test_transaction_lock_reports_filesystem_failures(cowork_host, monkeypatch):
    lock_path = claude_cowork.state_path().parent / ".ramp-router-cowork.lock"
    real_open = Path.open

    def deny_lock_file(path, *args, **kwargs):
        if path == lock_path:
            raise PermissionError("permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_lock_file)

    with pytest.raises(click.ClickException, match="Could not lock.*permission denied"):
        with claude_cowork.transaction_lock():
            pass


def test_private_json_removes_temporary_file_on_interruption(tmp_path, monkeypatch):
    destination = tmp_path / "profile.json"
    monkeypatch.setattr(
        claude_cowork.os,
        "fsync",
        lambda _fd: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    with pytest.raises(KeyboardInterrupt):
        claude_cowork._write_private_json(
            destination, {"inferenceGatewayApiKey": "router-secret"}
        )

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_configure_and_unconfigure_hold_the_same_transaction_lock(
    cowork_host, monkeypatch
):
    _root, events = cowork_host

    @contextmanager
    def recording_lock():
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")

    monkeypatch.setattr(claude_cowork, "transaction_lock", recording_lock)

    claude_cowork.configure("router-secret", "https://router.example/v1")
    claude_cowork.unconfigure()

    assert events == [
        "lock-enter",
        "quit",
        "launch",
        "lock-exit",
        "lock-enter",
        "quit",
        "launch",
        "lock-exit",
    ]


def test_configure_and_unconfigure_restore_the_previous_claude_setup(cowork_host):
    root, events = cowork_host
    third_party = root / "Claude-3p"
    library = third_party / "configLibrary"
    library.mkdir(parents=True)
    desktop_path = third_party / "claude_desktop_config.json"
    meta_path = library / "_meta.json"
    original_desktop = {
        "deploymentMode": "1p",
        "awaitingSignIn": True,
        "unrelated": "keep",
    }
    original_meta = {
        "appliedId": "old-profile",
        "entries": [{"id": "old-profile", "name": "Old provider"}],
        "unrelated": "keep",
    }
    desktop_path.write_text(json.dumps(original_desktop))
    meta_path.write_text(json.dumps(original_meta))

    profile_path, _ = claude_cowork.configure(
        "router-secret", "https://router.example/v1"
    )

    assert events == ["quit", "launch"]
    desktop = json.loads(desktop_path.read_text())
    assert desktop == {"deploymentMode": "3p", "unrelated": "keep"}
    profile = json.loads(profile_path.read_text())
    assert profile == {
        "inferenceProvider": "gateway",
        "inferenceGatewayBaseUrl": "https://router.example",
        "inferenceCredentialKind": "static",
        "inferenceGatewayApiKey": "router-secret",
        "inferenceGatewayAuthScheme": "bearer",
        "inferenceCustomHeaders": {
            "X-Gateway-Client": "claude-cowork",
            "X-Gateway-Ramp-Cli-Version": __version__,
        },
        "modelDiscoveryEnabled": True,
    }
    meta = json.loads(meta_path.read_text())
    assert meta["entries"][:-1] == original_meta["entries"]
    assert meta["entries"][-1] == {
        "id": profile_path.stem,
        "name": "Ramp Router",
    }
    assert meta["appliedId"] == profile_path.stem
    assert meta["unrelated"] == "keep"
    state_text = claude_cowork.state_path().read_text()
    assert "router-secret" not in state_text
    for private_file in (
        desktop_path,
        meta_path,
        profile_path,
        claude_cowork.state_path(),
    ):
        assert stat.S_IMODE(private_file.stat().st_mode) == 0o600
    assert claude_cowork.configured_api_key() == "router-secret"

    claude_cowork.unconfigure()

    assert events == ["quit", "launch", "quit", "launch"]
    assert json.loads(desktop_path.read_text()) == original_desktop
    assert json.loads(meta_path.read_text()) == original_meta
    assert not profile_path.exists()
    assert not claude_cowork.state_path().exists()


def test_configure_snapshots_settings_after_claude_flushes_on_quit(
    cowork_host, monkeypatch
):
    root, events = cowork_host
    third_party = root / "Claude-3p"
    third_party.mkdir()
    desktop_path = third_party / "claude_desktop_config.json"
    desktop_path.write_text(json.dumps({"deploymentMode": "stale"}))

    def quit_and_flush():
        events.append("quit-and-flush")
        desktop_path.write_text(json.dumps({"deploymentMode": "fresh"}))

    monkeypatch.setattr(claude_cowork, "_quit_claude", quit_and_flush)

    claude_cowork.configure("router-secret", "https://router.example/v1")

    state = json.loads(claude_cowork.state_path().read_text())
    assert state["desktop_config"]["deploymentMode"] == {
        "present": True,
        "value": "fresh",
    }
    assert events == ["quit-and-flush", "launch"]


def test_unconfigure_reads_settings_after_claude_flushes_on_quit(
    cowork_host, monkeypatch
):
    root, events = cowork_host
    claude_cowork.configure("router-secret", "https://router.example/v1")
    desktop_path = root / "Claude-3p" / "claude_desktop_config.json"
    meta_path = root / "Claude-3p" / "configLibrary" / "_meta.json"

    def quit_and_flush():
        events.append("quit-and-flush")
        desktop = json.loads(desktop_path.read_text())
        desktop["flushedOnQuit"] = True
        desktop_path.write_text(json.dumps(desktop))
        meta = json.loads(meta_path.read_text())
        meta["flushedOnQuit"] = True
        meta_path.write_text(json.dumps(meta))

    monkeypatch.setattr(claude_cowork, "_quit_claude", quit_and_flush)

    claude_cowork.unconfigure()

    assert json.loads(desktop_path.read_text()) == {"flushedOnQuit": True}
    assert json.loads(meta_path.read_text()) == {
        "entries": [],
        "flushedOnQuit": True,
    }
    assert events == ["quit", "launch", "quit-and-flush", "launch"]


def test_unconfigure_removes_files_that_configure_created(cowork_host):
    root, _events = cowork_host

    profile_path, _ = claude_cowork.configure(
        "router-secret", "https://router.example/v1"
    )
    claude_cowork.unconfigure()

    assert not (root / "Claude-3p" / "claude_desktop_config.json").exists()
    assert not (root / "Claude-3p" / "configLibrary" / "_meta.json").exists()
    assert not profile_path.exists()


def test_reconfigure_rotates_the_key_without_losing_the_original_snapshot(
    cowork_host,
):
    root, _events = cowork_host
    third_party = root / "Claude-3p"
    third_party.mkdir()
    desktop_path = third_party / "claude_desktop_config.json"
    desktop_path.write_text(json.dumps({"deploymentMode": "1p"}))

    first_profile, _ = claude_cowork.configure(
        "first-secret", "https://router.example/v1"
    )
    second_profile, _ = claude_cowork.configure(
        "second-secret", "https://router.example/v1"
    )

    assert second_profile == first_profile
    assert claude_cowork.configured_api_key() == "second-secret"
    assert "first-secret" not in claude_cowork.state_path().read_text()
    assert "second-secret" not in claude_cowork.state_path().read_text()

    claude_cowork.unconfigure()
    assert json.loads(desktop_path.read_text()) == {"deploymentMode": "1p"}


def test_interrupted_reconfigure_restores_the_previous_receipt_and_profile(
    cowork_host, monkeypatch
):
    _root, events = cowork_host
    profile_path, _ = claude_cowork.configure(
        "first-secret", "https://router.example/v1"
    )
    previous_state = claude_cowork.state_path().read_text()
    real_write = claude_cowork._write_private_json
    interrupted = False

    def interrupt_before_profile(path, value):
        nonlocal interrupted
        if path == profile_path and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        real_write(path, value)

    monkeypatch.setattr(claude_cowork, "_write_private_json", interrupt_before_profile)

    with pytest.raises(KeyboardInterrupt):
        claude_cowork.configure("second-secret", "https://router.example/v1")

    assert claude_cowork.state_path().read_text() == previous_state
    assert claude_cowork.configured_api_key() == "first-secret"
    assert (
        json.loads(profile_path.read_text())["inferenceGatewayApiKey"] == "first-secret"
    )
    assert events == ["quit", "launch", "quit", "launch"]


def test_interrupted_unconfigure_restores_all_router_files(cowork_host, monkeypatch):
    root, events = cowork_host
    third_party = root / "Claude-3p"
    third_party.mkdir()
    library = third_party / "configLibrary"
    library.mkdir()
    desktop_path = third_party / "claude_desktop_config.json"
    meta_path = library / "_meta.json"
    desktop_path.write_text(json.dumps({"deploymentMode": "1p"}))
    meta_path.write_text(json.dumps({"entries": [], "appliedId": ""}))
    profile_path, _ = claude_cowork.configure(
        "router-secret", "https://router.example/v1"
    )
    before = {
        path: path.read_text()
        for path in (
            desktop_path,
            meta_path,
            profile_path,
            claude_cowork.state_path(),
        )
    }
    real_write = claude_cowork._write_private_json
    interrupted = False

    def interrupt_before_meta(path, value):
        nonlocal interrupted
        if path == meta_path and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        real_write(path, value)

    monkeypatch.setattr(claude_cowork, "_write_private_json", interrupt_before_meta)

    with pytest.raises(KeyboardInterrupt):
        claude_cowork.unconfigure()

    for path, contents in before.items():
        assert path.read_text() == contents
    assert claude_cowork.configured_api_key() == "router-secret"
    assert events == ["quit", "launch", "quit", "launch"]


def test_unconfigure_refuses_to_delete_a_profile_the_user_changed(cowork_host):
    _root, events = cowork_host
    profile_path, _ = claude_cowork.configure(
        "router-secret", "https://router.example/v1"
    )
    profile = json.loads(profile_path.read_text())
    profile["custom"] = True
    profile_path.write_text(json.dumps(profile))

    with pytest.raises(click.ClickException, match="changed after setup"):
        claude_cowork.unconfigure()

    assert profile_path.exists()
    assert claude_cowork.state_path().exists()
    # An unowned profile is rejected before interrupting Desktop.
    assert events == ["quit", "launch"]


def test_unconfigure_refuses_to_delete_a_profile_with_a_changed_gateway(cowork_host):
    _root, events = cowork_host
    profile_path, _ = claude_cowork.configure(
        "router-secret", "https://router.example/v1"
    )
    profile = json.loads(profile_path.read_text())
    profile["inferenceGatewayBaseUrl"] = "https://different.example"
    profile_path.write_text(json.dumps(profile))

    with pytest.raises(click.ClickException, match="changed after setup"):
        claude_cowork.unconfigure()

    assert profile_path.exists()
    assert claude_cowork.state_path().exists()
    assert events == ["quit", "launch"]


@pytest.mark.parametrize("recorded_version", ["0.1.0", None])
def test_unconfigure_removes_a_profile_written_by_another_cli_version(
    cowork_host, recorded_version
):
    # The version header is telemetry the CLI wrote, not a user edit: a
    # profile from before the upgrade (or before the header existed) must
    # still be recognized as ours to remove.
    _root, _events = cowork_host
    profile_path, _ = claude_cowork.configure(
        "router-secret", "https://router.example/v1"
    )
    profile = json.loads(profile_path.read_text())
    if recorded_version is None:
        del profile["inferenceCustomHeaders"]["X-Gateway-Ramp-Cli-Version"]
    else:
        profile["inferenceCustomHeaders"]["X-Gateway-Ramp-Cli-Version"] = (
            recorded_version
        )
    profile_path.write_text(json.dumps(profile))

    claude_cowork.unconfigure()

    assert not profile_path.exists()
    assert not claude_cowork.state_path().exists()


def test_unconfigure_cleans_up_an_interrupted_receipt_without_a_profile(cowork_host):
    _root, events = cowork_host
    state = claude_cowork._new_state("router-secret", "https://router.example/v1")
    claude_cowork._write_private_json(claude_cowork.state_path(), state)

    claude_cowork.unconfigure()

    assert not claude_cowork.state_path().exists()
    assert events == ["quit", "launch"]


def test_unconfigure_uses_entry_order_when_the_prior_profile_is_gone(cowork_host):
    root, _events = cowork_host
    library = root / "Claude-3p" / "configLibrary"
    library.mkdir(parents=True)
    meta_path = library / "_meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "appliedId": "missing-profile",
                "entries": [
                    {"id": "first-profile", "name": "First provider"},
                    {"id": "second-profile", "name": "Second provider"},
                ],
            }
        )
    )

    claude_cowork.configure("router-secret", "https://router.example/v1")
    claude_cowork.unconfigure()

    assert json.loads(meta_path.read_text())["appliedId"] == "first-profile"


def test_unconfigure_preserves_an_absent_applied_id(cowork_host):
    root, _events = cowork_host
    library = root / "Claude-3p" / "configLibrary"
    library.mkdir(parents=True)
    meta_path = library / "_meta.json"
    original_meta = {
        "entries": [{"id": "old-profile", "name": "Old provider"}],
        "unrelated": "keep",
    }
    meta_path.write_text(json.dumps(original_meta))

    claude_cowork.configure("router-secret", "https://router.example/v1")
    claude_cowork.unconfigure()

    assert json.loads(meta_path.read_text()) == original_meta


def test_unconfigure_preserves_metadata_added_after_cli_created_the_library(
    cowork_host,
):
    root, _events = cowork_host
    meta_path = root / "Claude-3p" / "configLibrary" / "_meta.json"

    claude_cowork.configure("router-secret", "https://router.example/v1")
    meta = json.loads(meta_path.read_text())
    meta["newMetadata"] = {"keep": True}
    meta_path.write_text(json.dumps(meta))

    claude_cowork.unconfigure()

    assert json.loads(meta_path.read_text()) == {
        "entries": [],
        "newMetadata": {"keep": True},
    }


def test_unconfigure_uses_a_restore_specific_relaunch_error(cowork_host, monkeypatch):
    _root, _events = cowork_host
    profile_path, _ = claude_cowork.configure(
        "router-secret", "https://router.example/v1"
    )

    def fail_launch(*, failure_message=None):
        raise click.ClickException(failure_message or "wrong setup message")

    monkeypatch.setattr(claude_cowork, "_launch_claude", fail_launch)

    with pytest.raises(click.ClickException, match="removed.*previous Cowork settings"):
        claude_cowork.unconfigure()

    assert not profile_path.exists()
    assert not claude_cowork.state_path().exists()


def test_unconfigure_preserves_shared_settings_after_the_active_profile_changes(
    cowork_host,
):
    root, _events = cowork_host
    third_party = root / "Claude-3p"
    library = third_party / "configLibrary"
    library.mkdir(parents=True)
    desktop_path = third_party / "claude_desktop_config.json"
    meta_path = library / "_meta.json"
    desktop_path.write_text(
        json.dumps({"deploymentMode": "1p", "awaitingSignIn": True})
    )
    meta_path.write_text(
        json.dumps(
            {
                "appliedId": "old-profile",
                "entries": [{"id": "old-profile", "name": "Old provider"}],
            }
        )
    )

    profile_path, _ = claude_cowork.configure(
        "router-secret", "https://router.example/v1"
    )
    desktop_path.write_text(
        json.dumps({"deploymentMode": "3p", "awaitingSignIn": False})
    )
    meta = json.loads(meta_path.read_text())
    meta["appliedId"] = "old-profile"
    meta_path.write_text(json.dumps(meta))

    claude_cowork.unconfigure()

    assert json.loads(desktop_path.read_text()) == {
        "deploymentMode": "3p",
        "awaitingSignIn": False,
    }
    assert json.loads(meta_path.read_text()) == {
        "appliedId": "old-profile",
        "entries": [{"id": "old-profile", "name": "Old provider"}],
    }
    assert not profile_path.exists()
    assert not claude_cowork.state_path().exists()


def test_unconfigure_preserves_shared_values_changed_while_router_is_active(
    cowork_host,
):
    root, _events = cowork_host
    third_party = root / "Claude-3p"
    third_party.mkdir()
    desktop_path = third_party / "claude_desktop_config.json"
    desktop_path.write_text(
        json.dumps({"deploymentMode": "1p", "awaitingSignIn": True})
    )

    claude_cowork.configure("router-secret", "https://router.example/v1")
    desktop_path.write_text(
        json.dumps({"deploymentMode": "managed", "awaitingSignIn": False})
    )

    claude_cowork.unconfigure()

    assert json.loads(desktop_path.read_text()) == {
        "deploymentMode": "managed",
        "awaitingSignIn": False,
    }


@pytest.mark.parametrize("connector_binding", [None, "a" * 32])
def test_failed_configure_rolls_back_partial_files_and_reopens_claude(
    cowork_host, monkeypatch, connector_binding
):
    root, events = cowork_host
    third_party = root / "Claude-3p"
    library = third_party / "configLibrary"
    library.mkdir(parents=True)
    desktop_path = third_party / "claude_desktop_config.json"
    meta_path = library / "_meta.json"
    desktop_path.write_text(json.dumps({"deploymentMode": "1p"}))
    meta_path.write_text(json.dumps({"appliedId": "", "entries": []}))
    real_write = claude_cowork._write_private_json
    failed = False

    def fail_once(path, value):
        nonlocal failed
        if path == meta_path and not failed:
            failed = True
            raise OSError("disk full")
        real_write(path, value)

    monkeypatch.setattr(claude_cowork, "_write_private_json", fail_once)
    monkeypatch.setattr(harnesses, "_executable", lambda: "/usr/local/bin/ramp")

    with pytest.raises(click.ClickException, match="Could not configure"):
        claude_cowork.configure(
            "router-secret",
            "https://router.example/v1",
            connector_binding=connector_binding,
        )

    assert json.loads(desktop_path.read_text()) == {"deploymentMode": "1p"}
    assert json.loads(meta_path.read_text()) == {"appliedId": "", "entries": []}
    assert not claude_cowork.state_path().exists()
    assert list(library.glob("*.json")) == [meta_path]
    assert events == ["quit", "launch"]


def test_setup_file_is_validated_without_exposing_its_key(tmp_path):
    setup_path = tmp_path / "setup.json"
    setup_path.write_text(
        json.dumps(
            {
                "version": 1,
                "name": "Ramp Router",
                "base_url": "https://router.example/v1",
                "api_key": "router-secret",
            }
        )
    )

    assert claude_cowork.read_setup_file(setup_path) == (
        "https://router.example/v1",
        "router-secret",
    )

    setup_path.write_text("{}")
    with pytest.raises(click.ClickException) as error:
        claude_cowork.read_setup_file(setup_path)
    assert "router-secret" not in str(error.value)


# The ids below mirror Router's actual rename history for one model: the
# selector state written before a rename keeps the old id, and Claude sizes
# unrecognized ids at its 200k unknown-model fallback instead of the model's
# real window.
CURRENT_MODEL_IDS = (
    "claude-opus-5",
    "claude-fable-router-5-6-sol-419255[1m]",
    "claude-fable-router-model-1e3b36d63ed19af0[1m]",
    "claude-fable-router-model-44968622fb48d054",
)


def _account_settings_path(root: Path) -> Path:
    return (
        root
        / "Claude-3p"
        / "local-agent-mode-sessions"
        / "1bcd7467"
        / "00000000"
        / "cowork_account_settings.json"
    )


def _write_stale_selector_state(root: Path) -> Path:
    path = _account_settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "__created_at": "2026-08-15T22:23:31.085Z",
                "__model_selector_state": {
                    "cowork": {"model": "claude-router-5-6-sol-419255"},
                    "code": {
                        "model": "claude-router-5-6-sol-419255[1m]",
                        "thinking_by_model": {
                            "claude-router-model-1e3b36d63ed19af0": {
                                "type": "effort",
                                "effort": "high",
                            }
                        },
                    },
                },
            }
        )
    )
    return path


@pytest.mark.parametrize(
    ("persisted", "expected"),
    [
        # Already current: nothing to do.
        ("claude-fable-router-5-6-sol-419255[1m]", None),
        # Same id without the 1M marker: restore the marker.
        (
            "claude-fable-router-5-6-sol-419255",
            "claude-fable-router-5-6-sol-419255[1m]",
        ),
        # Renamed prefix: join on the catalog hash the id ends with.
        ("claude-router-5-6-sol-419255", "claude-fable-router-5-6-sol-419255[1m]"),
        # A longer digest generation of the same catalog hash still joins.
        (
            "claude-router-accounts-fireworks-models-kimi-k3-1e3b36[1m]",
            "claude-fable-router-model-1e3b36d63ed19af0[1m]",
        ),
        # Anthropic ids have no hash token and never remap.
        ("claude-opus-4-8", None),
        # An id Router no longer serves at all is left alone.
        ("claude-router-model-ffffff", None),
    ],
)
def test_resolve_current_model_id_joins_on_the_catalog_hash(persisted, expected):
    assert (
        claude_cowork._resolve_current_model_id(persisted, CURRENT_MODEL_IDS)
        == expected
    )


def test_resolve_current_model_id_refuses_an_ambiguous_join():
    current = (
        "claude-fable-router-model-419255aaaaaaaaaa[1m]",
        "claude-fable-router-5-6-sol-419255[1m]",
    )
    assert (
        claude_cowork._resolve_current_model_id("claude-router-5-6-sol-419255", current)
        is None
    )


def test_configure_migrates_stale_model_selections(cowork_host):
    tmp_path, _events = cowork_host
    settings_path = _write_stale_selector_state(tmp_path)

    _, migrated = claude_cowork.configure(
        "router-secret",
        "https://router.example/v1",
        model_ids=CURRENT_MODEL_IDS,
    )

    selector = json.loads(settings_path.read_text())["__model_selector_state"]
    assert selector["cowork"]["model"] == "claude-fable-router-5-6-sol-419255[1m]"
    assert selector["code"]["model"] == "claude-fable-router-5-6-sol-419255[1m]"
    assert selector["code"]["thinking_by_model"] == {
        "claude-fable-router-model-1e3b36d63ed19af0[1m]": {
            "type": "effort",
            "effort": "high",
        }
    }
    assert [(change["surface"], change["field"]) for change in migrated] == [
        ("cowork", "model"),
        ("code", "model"),
        ("code", "thinking_by_model"),
    ]
    assert all(change["path"] == str(settings_path) for change in migrated)


def test_configure_without_model_ids_leaves_selections_alone(cowork_host):
    tmp_path, _events = cowork_host
    settings_path = _write_stale_selector_state(tmp_path)
    before = settings_path.read_text()

    claude_cowork.configure("router-secret", "https://router.example/v1")

    assert settings_path.read_text() == before


def test_configure_skips_selector_documents_it_cannot_read(cowork_host):
    tmp_path, _events = cowork_host
    broken = _account_settings_path(tmp_path)
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("not json")

    _, migrated = claude_cowork.configure(
        "router-secret",
        "https://router.example/v1",
        model_ids=CURRENT_MODEL_IDS,
    )

    assert migrated == ()
    assert broken.read_text() == "not json"


def test_migrate_model_selections_waits_for_claude_to_close(cowork_host, monkeypatch):
    tmp_path, _events = cowork_host
    settings_path = _write_stale_selector_state(tmp_path)
    before = settings_path.read_text()
    monkeypatch.setattr(claude_cowork, "_claude_is_running", lambda: True)

    outcome = claude_cowork.migrate_model_selections(CURRENT_MODEL_IDS)

    assert outcome.skipped_while_running
    assert outcome.migrated == ()
    assert settings_path.read_text() == before


def test_migrate_model_selections_heals_stale_ids_while_claude_is_closed(
    cowork_host, monkeypatch
):
    tmp_path, _events = cowork_host
    settings_path = _write_stale_selector_state(tmp_path)
    monkeypatch.setattr(claude_cowork, "_claude_is_running", lambda: False)

    outcome = claude_cowork.migrate_model_selections(CURRENT_MODEL_IDS)

    assert not outcome.skipped_while_running
    assert len(outcome.migrated) == 3
    selector = json.loads(settings_path.read_text())["__model_selector_state"]
    assert selector["cowork"]["model"] == "claude-fable-router-5-6-sol-419255[1m]"


def test_resolve_current_model_id_never_hash_joins_anthropic_dated_ids():
    # Anthropic's dated ids end in an all-hex segment too; only ids carrying
    # Router's generated naming scheme may join on the catalog hash.
    current = ("claude-fable-router-model-20250514abc[1m]",)
    assert (
        claude_cowork._resolve_current_model_id("claude-opus-4-20250514", current)
        is None
    )
    # And a Router-shaped stale id never joins to a non-Router candidate.
    assert (
        claude_cowork._resolve_current_model_id(
            "claude-router-model-20250514", ("claude-opus-4-20250514abc",)
        )
        is None
    )


def test_migrate_model_selections_aborts_when_claude_launches_mid_pass(
    cowork_host, monkeypatch
):
    tmp_path, _events = cowork_host
    settings_path = _write_stale_selector_state(tmp_path)
    before = settings_path.read_text()
    # Not running at the outer gate, then running again by the pre-write check.
    answers = iter([False, True])
    monkeypatch.setattr(
        claude_cowork, "_claude_is_running", lambda: next(answers, True)
    )

    outcome = claude_cowork.migrate_model_selections(CURRENT_MODEL_IDS)

    assert outcome.skipped_while_running
    assert outcome.migrated == ()
    assert settings_path.read_text() == before


def test_configure_aborts_the_migration_when_claude_relaunches_mid_pass(
    cowork_host, monkeypatch
):
    # A manual Desktop launch between configure's quit and the selector
    # rewrite aborts the remaining writes; configure itself still succeeds.
    tmp_path, _events = cowork_host
    settings_path = _write_stale_selector_state(tmp_path)
    before = settings_path.read_text()
    monkeypatch.setattr(claude_cowork, "_claude_is_running", lambda: True)

    profile_path, migrated = claude_cowork.configure(
        "router-secret",
        "https://router.example/v1",
        model_ids=CURRENT_MODEL_IDS,
    )

    assert profile_path.exists()
    assert migrated == ()
    assert settings_path.read_text() == before


def test_configured_gateway_base_url_rejects_endpoint_drift(cowork_host):
    tmp_path, _events = cowork_host
    profile_path, _ = claude_cowork.configure(
        "router-secret", "https://router.example/v1"
    )
    assert claude_cowork.configured_gateway_base_url() == "https://router.example"

    profile = json.loads(profile_path.read_text())
    profile["inferenceGatewayBaseUrl"] = "https://elsewhere.example"
    profile_path.write_text(json.dumps(profile))

    with pytest.raises(click.ClickException) as error:
        claude_cowork.configured_gateway_base_url()
    assert "endpoint changed after setup" in error.value.message


def test_migrate_gateway_base_url_keeps_the_original_restore_snapshot(cowork_host):
    _, events = cowork_host
    profile_path, _ = claude_cowork.configure(
        "router-secret", "https://router-api.ramp.com/v1"
    )
    original_state = json.loads(claude_cowork.state_path().read_text())
    original_profile = json.loads(profile_path.read_text())
    events.clear()

    assert claude_cowork.migrate_gateway_base_url(
        "https://router-api.ramp.com", "https://api.router.com"
    )
    assert events == []
    assert claude_cowork.configured_gateway_base_url() == "https://api.router.com"
    assert json.loads(profile_path.read_text()) == {
        **original_profile,
        "inferenceGatewayBaseUrl": "https://api.router.com",
    }
    state = json.loads(claude_cowork.state_path().read_text())
    receipt = {"credential_marker", "credential_ref", "desktop_launched_at"}
    receipt.add("sealed_marker")
    assert {k: v for k, v in state.items() if k not in receipt} == {
        **{k: v for k, v in original_state.items() if k not in receipt},
        "gateway_base_url": "https://api.router.com",
    }
    # The key's local token is re-registered for the new host.
    assert state["credential_ref"] != original_state["credential_ref"]
    assert claude_cowork.configured_router_connection()[0] == "router-secret"
    assert not claude_cowork.migrate_gateway_base_url(
        "https://router-api.ramp.com", "https://api.router.com"
    )


def test_migrate_gateway_base_url_skips_running_or_edited_profiles(
    cowork_host, monkeypatch
):
    _, events = cowork_host
    profile_path, _ = claude_cowork.configure(
        "router-secret", "https://router-api.ramp.com/v1"
    )
    events.clear()
    before = profile_path.read_text()
    monkeypatch.setattr(claude_cowork, "_claude_is_running", lambda: True)
    assert not claude_cowork.migrate_gateway_base_url(
        "https://router-api.ramp.com", "https://api.router.com"
    )
    assert profile_path.read_text() == before
    assert events == []

    monkeypatch.setattr(claude_cowork, "_claude_is_running", lambda: False)
    profile = json.loads(before)
    profile["inferenceGatewayBaseUrl"] = "https://user.example"
    profile_path.write_text(json.dumps(profile))
    with pytest.raises(click.ClickException, match="profile changed after setup"):
        claude_cowork.migrate_gateway_base_url(
            "https://router-api.ramp.com", "https://api.router.com"
        )
    assert json.loads(profile_path.read_text()) == profile


def test_migrate_gateway_base_url_restores_profile_when_receipt_write_fails(
    cowork_host, monkeypatch
):
    profile_path, _ = claude_cowork.configure(
        "router-secret", "https://router-api.ramp.com/v1"
    )
    original_profile = profile_path.read_text()
    original_state = claude_cowork.state_path().read_text()
    write = claude_cowork._write_private_json
    failed = False

    def fail_once(path, value):
        nonlocal failed
        if path == claude_cowork.state_path() and not failed:
            failed = True
            raise OSError("disk full")
        write(path, value)

    monkeypatch.setattr(claude_cowork, "_write_private_json", fail_once)
    with pytest.raises(click.ClickException, match="Could not migrate"):
        claude_cowork.migrate_gateway_base_url(
            "https://router-api.ramp.com", "https://api.router.com"
        )
    assert profile_path.read_text() == original_profile
    assert claude_cowork.state_path().read_text() == original_state


def _write_code_session(
    root: Path,
    data_dir: str,
    scope: str,
    session_id: str,
    cli_session_id: str,
    *,
    title: str,
    last_activity_at: int,
) -> Path:
    path = root / data_dir / "claude-code-sessions" / scope / f"local_{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    config = root / data_dir / "config.json"
    if not config.exists():
        config.write_text(json.dumps({"lastKnownAccountUuid": scope.split("/")[0]}))
    path.write_text(
        json.dumps(
            {
                "sessionId": f"local_{session_id}",
                "cliSessionId": cli_session_id,
                "title": title,
                "lastActivityAt": last_activity_at,
            }
        )
    )
    return path


def _sync_as(setup: str | None = "acct") -> int:
    """Sync as if Router was set up from ``setup`` (None: no receipt)."""
    return claude_cowork._sync_code_sessions(
        {"first_party_account": setup} if setup else None
    )


def _code_session_titles(root: Path, data_dir: str) -> dict[str, str]:
    return {
        entry["cliSessionId"]: entry["title"]
        for entry in (
            json.loads(path.read_text())
            for path in (root / data_dir / "claude-code-sessions").glob(
                "*/*/local_*.json"
            )
        )
    }


def test_configure_and_unconfigure_carry_code_sessions_across_modes(cowork_host):
    root, _events = cowork_host
    _write_code_session(
        root, "Claude", "acct/org", "a", "cli-a", title="Before", last_activity_at=1
    )
    _write_code_session(
        root,
        "Claude-3p",
        "3p/org",
        "z",
        "cli-z",
        title="Old 3p",
        last_activity_at=1,
    )

    claude_cowork.configure("router-secret", "https://router.example/v1")

    assert _code_session_titles(root, "Claude-3p") == {
        "cli-a": "Before",
        "cli-z": "Old 3p",
    }
    copied = root / "Claude-3p/claude-code-sessions/3p/org/local_a.json"
    assert stat.S_IMODE(copied.stat().st_mode) == 0o600

    # Work continues in third-party mode, then Router is removed.
    _write_code_session(
        root,
        "Claude-3p",
        "3p/org",
        "b",
        "cli-b",
        title="During",
        last_activity_at=2,
    )
    _write_code_session(
        root,
        "Claude-3p",
        "3p/org",
        "a",
        "cli-a",
        title="Renamed",
        last_activity_at=3,
    )

    claude_cowork.unconfigure()

    assert _code_session_titles(root, "Claude") == {
        "cli-a": "Renamed",
        "cli-b": "During",
    }
    assert claude_cowork._session_owners()["cli-z"] == ""
    # Entries already present are updated in place rather than duplicated.
    assert (
        len(list((root / "Claude/claude-code-sessions").glob("*/*/local_a.json"))) == 1
    )


@pytest.mark.parametrize("sync", ["refresh", "unconfigure"])
def test_first_setup_records_empty_session_baseline_before_router_sign_in(
    cowork_host, sync
):
    root, _events = cowork_host
    (root / "Claude/claude-code-sessions/acct/org").mkdir(parents=True)
    (root / "Claude/config.json").write_text('{"lastKnownAccountUuid": "acct"}')

    claude_cowork.configure("router-secret", "https://router.example/v1")

    assert not (root / "Claude-3p/config.json").exists()
    assert claude_cowork._session_owners() == {}
    # Desktop's first Router launch creates its account and a new session.
    _write_code_session(
        root, "Claude-3p", "3p/org", "n", "cli-n", title="New", last_activity_at=1
    )
    if sync == "refresh":
        assert claude_cowork.sync_code_sessions() == 1
    else:
        claude_cowork.unconfigure()

    assert _code_session_titles(root, "Claude") == {"cli-n": "New"}
    assert claude_cowork._session_owners() == {"cli-n": "acct"}


def test_setup_records_existing_router_sessions_without_a_router_account(cowork_host):
    root, _events = cowork_host
    _write_code_session(
        root, "Claude", "acct/org", "a", "cli-a", title="Mine", last_activity_at=1
    )
    _write_code_session(
        root, "Claude-3p", "3p/org", "a", "cli-a", title="Mine", last_activity_at=1
    )
    _write_code_session(
        root, "Claude-3p", "3p/org", "z", "cli-z", title="Old", last_activity_at=1
    )
    (root / "Claude-3p/config.json").unlink()

    claude_cowork.configure("router-secret", "https://router.example/v1")

    assert not (root / "Claude-3p/config.json").exists()
    assert claude_cowork._session_owners() == {"cli-a": "acct", "cli-z": ""}
    _write_code_session(
        root, "Claude-3p", "3p/org", "n", "cli-n", title="New", last_activity_at=2
    )
    assert claude_cowork.sync_code_sessions() == 1
    assert _code_session_titles(root, "Claude") == {"cli-a": "Mine", "cli-n": "New"}
    assert claude_cowork._session_owners() == {
        "cli-a": "acct",
        "cli-z": "",
        "cli-n": "acct",
    }


def test_code_session_sync_keeps_the_most_recently_active_entry(cowork_host):
    root, _events = cowork_host
    newer = _write_code_session(
        root, "Claude", "acct/org", "a", "cli-a", title="Newer", last_activity_at=5
    )
    _write_code_session(
        root, "Claude-3p", "3p/org", "a", "cli-a", title="Older", last_activity_at=4
    )

    assert _sync_as() == 1
    assert json.loads(newer.read_text())["title"] == "Newer"
    assert _code_session_titles(root, "Claude-3p") == {"cli-a": "Newer"}
    assert _sync_as() == 0


def test_code_session_sync_needs_a_signed_in_target(cowork_host):
    root, _events = cowork_host
    _write_code_session(
        root, "Claude-3p", "3p/org", "z", "cli-z", title="3p", last_activity_at=1
    )
    claude_cowork._write_private_json(
        claude_cowork._session_owners_path(), {"cli-z": "acct"}
    )

    assert _sync_as() == 0
    assert not (root / "Claude" / "claude-code-sessions").exists()

    (root / "Claude/claude-code-sessions/acct/org").mkdir(parents=True)
    (root / "Claude/config.json").write_text('{"lastKnownAccountUuid": "acct"}')
    assert _sync_as() == 1
    assert _code_session_titles(root, "Claude") == {"cli-z": "3p"}


def test_code_session_sync_stays_within_the_configured_account(cowork_host):
    root, _events = cowork_host
    account = "6ffc11fb-0c8a-4c03-81f3-76e09540b507"
    (root / "Claude").mkdir()
    (root / "Claude/config.json").write_text(
        json.dumps({"lastKnownAccountUuid": account})
    )
    _write_code_session(
        root, "Claude", "other/org", "o", "cli-o", title="Other", last_activity_at=9
    )
    _write_code_session(
        root, "Claude", f"{account}/org", "a", "cli-a", title="Mine", last_activity_at=1
    )
    _write_code_session(
        root, "Claude-3p", "3p/org", "z", "cli-z", title="3p", last_activity_at=1
    )
    # Mirrored while Router was configured from the other account.
    _write_code_session(
        root, "Claude-3p", "3p/org", "o", "cli-o", title="Other", last_activity_at=9
    )

    # Without a setup, Router sessions have no owner yet and stay put.
    assert _sync_as(None) == 0
    # "3p" predates any recorded owner and has no first-party copy,
    # so its owner is unknown and it stays in Router mode.
    assert _sync_as(account) == 1
    assert _code_session_titles(root, "Claude-3p")["cli-a"] == "Mine"
    # A Router session made after that belongs to the setup's account.
    _write_code_session(
        root, "Claude-3p", "3p/org", "n", "cli-n", title="New", last_activity_at=2
    )
    assert _sync_as(account) == 1
    mine = root / f"Claude/claude-code-sessions/{account}/org"
    assert sorted(p.name for p in mine.iterdir()) == ["local_a.json", "local_n.json"]


@pytest.mark.parametrize("ownership_file", [None, "not json"])
def test_older_router_only_sessions_stay_unowned_with_one_account(
    cowork_host, ownership_file
):
    root, _events = cowork_host
    # A's first-party folder is gone; only B has sessions on this Mac.
    _write_code_session(
        root, "Claude", "b/org", "b", "cli-b", title="B's", last_activity_at=1
    )
    _write_code_session(
        root, "Claude-3p", "3p/org", "a", "cli-a", title="Under A", last_activity_at=1
    )
    if ownership_file is not None:
        claude_cowork._session_owners_path().write_text(ownership_file)

    assert _sync_as("b") == 1
    assert claude_cowork._session_owners() == {"cli-a": "", "cli-b": "b"}
    assert _code_session_titles(root, "Claude") == {"cli-b": "B's"}
    assert _code_session_titles(root, "Claude-3p") == {
        "cli-a": "Under A",
        "cli-b": "B's",
    }
    # Recording an unknown owner keeps later passes from claiming it, too.
    assert _sync_as("b") == 0
    assert _sync_as(None) == 0
    assert claude_cowork._session_owners()["cli-a"] == ""
    assert _code_session_titles(root, "Claude") == {"cli-b": "B's"}


def test_router_sessions_only_go_back_to_the_account_they_were_made_under(
    cowork_host,
):
    root, _events = cowork_host
    config = root / "Claude/config.json"
    _write_code_session(
        root, "Claude", "a/org", "x", "cli-x", title="A's", last_activity_at=1
    )
    (root / "Claude/claude-code-sessions/b/org").mkdir(parents=True)
    # Establish ownership before the new Router session is made under A.
    _write_code_session(
        root, "Claude-3p", "3p/org", "x", "cli-x", title="A's", last_activity_at=1
    )
    assert _sync_as("a") == 0
    _write_code_session(
        root, "Claude-3p", "3p/org", "1", "cli-1", title="Under A", last_activity_at=1
    )
    assert _sync_as("a") == 1

    # Router is set up again from B, and a new Router session is made.
    config.write_text('{"lastKnownAccountUuid": "b"}')
    _write_code_session(
        root, "Claude-3p", "3p/org", "2", "cli-2", title="Under B", last_activity_at=1
    )
    assert _sync_as("b") == 1
    # First-party mode signs back in to A: B's session never lands there.
    config.write_text('{"lastKnownAccountUuid": "a"}')
    assert _sync_as("b") == 0

    def names(account):
        folder = root / f"Claude/claude-code-sessions/{account}/org"
        return sorted(p.name for p in folder.iterdir())

    assert names("a") == ["local_1.json", "local_x.json"]
    assert names("b") == ["local_2.json"]


def test_reconfiguring_keeps_the_account_router_was_set_up_from(cowork_host):
    root, _events = cowork_host
    config = root / "Claude/config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('{"lastKnownAccountUuid": "first"}')
    claude_cowork.configure("router-secret", "https://router.example/v1")
    config.write_text('{"lastKnownAccountUuid": "second"}')
    claude_cowork.configure("router-secret", "https://router.example/v1")

    state = json.loads(claude_cowork.state_path().read_text())
    assert state["first_party_account"] == "first"


def test_code_session_sync_only_adds_while_desktop_runs(cowork_host, monkeypatch):
    root, _events = cowork_host
    _write_code_session(
        root, "Claude", "acct/org", "a", "cli-a", title="Newer", last_activity_at=5
    )
    _write_code_session(
        root, "Claude", "acct/org", "b", "cli-b", title="Missing", last_activity_at=1
    )
    _write_code_session(
        root, "Claude-3p", "3p/org", "a", "cli-a", title="Older", last_activity_at=4
    )
    monkeypatch.setattr(claude_cowork, "_claude_is_running", lambda: True)

    assert _sync_as() == 1
    assert _code_session_titles(root, "Claude-3p") == {
        "cli-a": "Older",
        "cli-b": "Missing",
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://router.example/v1",
        "http://127.0.0.1:47317/v1",
        "http://127.0.0.1:1234/u/api.router.com",
        "http://localhost:47317/u/api.router.com",
        "http://127.0.0.1:47317/u/api.router.com?x=1",
        "http://user@127.0.0.1:47317/u/api.router.com",
    ],
)
def test_desktop_http_only_for_managed_loopback_with_local_token(url):
    with pytest.raises(click.ClickException, match="HTTPS"):
        claude_cowork._profile("ramp-local-test", url)
    with pytest.raises(click.ClickException, match="HTTPS"):
        claude_cowork._profile(
            "router-secret", "http://127.0.0.1:47317/u/api.router.com"
        )


@pytest.mark.parametrize("interactive", [True, False])
@pytest.mark.parametrize("command", ["enable", "disable"])
def test_agent_command_restarts_desktop_only_for_route_change(
    cowork_host, monkeypatch, command, interactive
):
    _root, events = cowork_host
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(router_repo, "ensure_running", lambda: True)
    monkeypatch.setattr(router_repo, "service_supported", lambda: True)
    router_repo._save_settings({"enabled": command == "disable"})
    claude_cowork.configure("router-secret", "https://api.router.com/v1")
    events.clear()
    running = [True]

    def quit_app():
        events.append("quit")
        running[0] = False

    def launch_app(**_kwargs):
        events.append("launch")
        running[0] = True

    monkeypatch.setattr(claude_cowork, "_claude_is_running", lambda: running[0])
    monkeypatch.setattr(claude_cowork, "_quit_claude", quit_app)
    monkeypatch.setattr(claude_cowork, "_launch_claude", launch_app)
    monkeypatch.setattr(router, "configured_router_clients", lambda: [])
    monkeypatch.setattr(router, "_scheduled_sync_connections", lambda: [object()])
    monkeypatch.setattr(router, "_print_background_status", lambda _ctx: None)
    monkeypatch.setattr(router, "_can_draw_picker", lambda _ctx: interactive)
    args = ["--agent", "router", "agent", command]

    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0, result.output
    if not interactive:  # MDM: an open Desktop is never quit
        assert events == []
        assert router_repo.is_wrapped(claude_cowork.configured_gateway_base_url()) == (
            command == "disable"
        )
        return
    assert events == ["quit", "launch"]
    assert running[0]
    assert router_repo.is_wrapped(claude_cowork.configured_gateway_base_url()) == (
        command == "enable"
    )
    assert claude_cowork.configured_router_connection()[0] == "router-secret"
    events.clear()
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert events == []


def test_listener_stays_for_any_profile_on_the_helper(tmp_path):
    helper = "http://127.0.0.1:47317/u/api.router.com"
    codex = tmp_path / "codex/config.toml"
    codex.parent.mkdir(parents=True)
    codex.write_text(f'[model_providers.other]\nbase_url = "{helper}/v1"\n')
    assert router.clients_using_repo_helper() == ["codex"]
    codex.write_text(
        '[model_providers.other]\nbase_url = "https://api.router.com/v1"\n'
    )
    # A Desktop profile the user copied from ours still uses the helper.
    copy = claude_cowork._config_library_path() / "copy.json"
    copy.parent.mkdir(parents=True)
    copy.write_text(json.dumps({"inferenceGatewayBaseUrl": helper}))
    assert router.clients_using_repo_helper() == ["cowork"]


def test_last_unconfigure_keeps_agent_while_a_profile_uses_it(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_cowork, "unconfigure", lambda: None)
    removed = []
    monkeypatch.setattr(router_schedule, "uninstall", lambda: removed.append(1))
    codex = tmp_path / "codex/config.toml"
    codex.parent.mkdir(parents=True)
    codex.write_text(
        '[model_providers.other]\nbase_url = "http://127.0.0.1:47317/u/api.router.com/v1"\n'
    )
    args = ["--human", "router", "unconfigure", "cowork"]
    assert CliRunner().invoke(cli, args).exit_code == 0
    assert removed == []
    codex.unlink()
    assert CliRunner().invoke(cli, args).exit_code == 0
    assert removed == [1]


@pytest.fixture
def tagging(cowork_host, monkeypatch):
    monkeypatch.setattr(router_repo, "ensure_running", lambda: True)
    router_repo._save_settings({"enabled": True})


def test_agent_routing_keeps_a_real_v1_in_the_gateway_path(tagging):
    router_repo._save_settings({"enabled": False})
    claude_cowork.configure("router-secret", "https://api.router.com/v1/v1")
    assert claude_cowork.configured_gateway_base_url() == "https://api.router.com/v1"

    router_repo._save_settings({"enabled": True})
    claude_cowork.refresh_agent_routing()
    endpoint = claude_cowork.configured_gateway_base_url()
    assert endpoint == "http://127.0.0.1:47317/u/api.router.com/v1"

    router_repo._save_settings({"enabled": False})
    claude_cowork.refresh_agent_routing()
    assert claude_cowork.configured_gateway_base_url() == "https://api.router.com/v1"


def test_a_key_sealed_by_desktop_still_belongs_to_the_cli(tagging, monkeypatch):
    router_repo._save_settings({"enabled": False})
    profile_path, _ = claude_cowork.configure(
        "router-secret", "https://api.router.com/v1"
    )
    launched = time.time()
    monkeypatch.setattr(claude_cowork, "_claude_started_at", lambda: launched)
    plaintext = claude_cowork.configured_api_key()

    def seal(value, after_launch=1):
        nonlocal plaintext
        # Desktop seals the key at rest while it launches.
        profile = json.loads(profile_path.read_text())
        if not profile["inferenceGatewayApiKey"].startswith("__sealed__:"):
            plaintext = profile["inferenceGatewayApiKey"]
        profile["inferenceGatewayApiKey"] = f"__sealed__:v1:{value}"
        profile_path.write_text(json.dumps(profile))
        os.utime(profile_path, (launched + after_launch, launched + after_launch))

    # A key sealed long after launch was entered in Desktop, not written here.
    seal("edited-later", after_launch=600)
    with pytest.raises(click.ClickException, match="changed after setup"):
        claude_cowork.configured_api_key()
    seal("first")
    # Even the expected launch timing needs proof of plaintext ownership.
    request = claude_cowork.desktop_credential_request()
    claude_cowork.verify_desktop_credential(
        {"challenge": request["challenge"], "key": plaintext}
    )
    connection = ("router-secret", "https://api.router.com")
    assert claude_cowork.configured_router_connection() == connection
    router_repo._save_settings({"enabled": True})
    claude_cowork.refresh_agent_routing()
    assert router_repo.is_wrapped(claude_cowork.configured_gateway_base_url())
    # Desktop was closed for that rewrite and opened by someone else: the
    # profile could have been edited before this seal, so it isn't adopted.
    launched = time.time()
    seal("edited-while-closed")
    with pytest.raises(click.ClickException, match="changed after setup"):
        claude_cowork.configured_api_key()
    claude_cowork.configure("router-secret", "https://api.router.com/v1")
    launched = time.time()
    seal("second")
    request = claude_cowork.desktop_credential_request()
    claude_cowork.verify_desktop_credential(
        {"challenge": request["challenge"], "key": plaintext}
    )
    assert claude_cowork.configured_router_connection() == connection
    seal("someone-elses")
    with pytest.raises(click.ClickException, match="changed after setup"):
        claude_cowork.configured_api_key()
    seal("second")
    claude_cowork.unconfigure()
    assert not profile_path.exists()


@pytest.mark.parametrize("changed_on_quit", [False, True])
def test_unconfigure_revalidates_a_verified_seal_after_quitting_desktop(
    cowork_host, monkeypatch, changed_on_quit
):
    profile_path, _ = claude_cowork.configure(
        "router-secret", "https://router.example/v1"
    )
    launched = time.time()
    running = True
    monkeypatch.setattr(claude_cowork, "_claude_is_running", lambda: running)
    monkeypatch.setattr(
        claude_cowork, "_claude_started_at", lambda: launched if running else None
    )
    profile = json.loads(profile_path.read_text())
    profile["inferenceGatewayApiKey"] = "__sealed__:v1:ours"
    profile_path.write_text(json.dumps(profile))
    os.utime(profile_path, (launched + 1, launched + 1))

    def quit_desktop():
        nonlocal running
        running = False
        if changed_on_quit:
            profile["inferenceGatewayApiKey"] = "__sealed__:v1:someone-elses"
            profile_path.write_text(json.dumps(profile))

    monkeypatch.setattr(claude_cowork, "_quit_claude", quit_desktop)
    assert claude_cowork._read_state()["sealed_marker"] is None
    request = claude_cowork.desktop_credential_request()
    claude_cowork.verify_desktop_credential(
        {"challenge": request["challenge"], "key": "router-secret"}
    )
    if changed_on_quit:
        with pytest.raises(click.ClickException, match="changed after setup"):
            claude_cowork.unconfigure()
        assert profile_path.exists()
        assert claude_cowork.state_path().exists()
    else:
        claude_cowork.unconfigure()
        assert not profile_path.exists()
        assert not claude_cowork.state_path().exists()


def test_a_rotated_key_never_falls_back_to_the_previous_ref(cowork_host, monkeypatch):
    profile_path, _ = claude_cowork.configure(
        "first-secret", "https://router.example/v1"
    )

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(claude_cowork.router_local_auth, "register", fail)
    claude_cowork.configure("second-secret", "https://router.example/v1")
    profile = json.loads(profile_path.read_text())
    profile["inferenceGatewayApiKey"] = "__sealed__:v1:second"
    profile_path.write_text(json.dumps(profile))

    with pytest.raises(click.ClickException, match="changed after setup"):
        claude_cowork.configured_api_key()


def test_ssh_remote_sessions_keep_desktop_on_the_direct_route(
    tagging, tmp_path, monkeypatch
):
    preferences = tmp_path / "preferences"
    preferences.mkdir()
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_PREFERENCES", str(preferences))
    plist = preferences / "com.anthropic.claudefordesktop.plist"
    plist.write_bytes(plistlib.dumps({"sshHostAllowlist": ["dev.example.com"]}))

    claude_cowork.configure("router-secret", "https://api.router.com/v1")

    # The SSH host cannot reach a gateway on this device's loopback.
    assert claude_cowork.configured_gateway_base_url() == "https://api.router.com"
    assert claude_cowork.configured_api_key() == "router-secret"

    plist.write_bytes(plistlib.dumps({"sshHostAllowlist": []}))
    claude_cowork.refresh_agent_routing()
    assert router_repo.is_wrapped(claude_cowork.configured_gateway_base_url())


@pytest.mark.skipif(os.geteuid() == 0, reason="root can list any directory")
def test_listener_stays_when_the_profile_library_cannot_be_listed(cowork_host):
    library = claude_cowork._config_library_path()
    library.mkdir(parents=True)
    library.chmod(0o300)  # known profiles stay readable; listing fails
    try:
        assert router.clients_using_repo_helper() == ["cowork"]
    finally:
        library.chmod(0o700)


def test_last_unconfigure_clears_the_manifest_under_the_sync_lock(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(claude_cowork, "unconfigure", lambda: None)
    monkeypatch.setattr(router, "clients_using_repo_helper", lambda: ["codex"])
    events = []

    @contextmanager
    def sync_lock(wait=0):
        events.append(("lock", wait))
        yield True
        events.append("unlock")

    monkeypatch.setattr(router, "sync_lock", sync_lock)
    monkeypatch.setattr(
        router_schedule, "clear_manifest", lambda: events.append("clear")
    )
    args = ["--human", "router", "unconfigure", "cowork"]
    assert CliRunner().invoke(cli, args).exit_code == 0
    assert events == [("lock", router_schedule.REMOVE_WAIT_SECONDS), "clear", "unlock"]


@pytest.mark.parametrize("output_mode", ["--human", "--agent"])
def test_last_unconfigure_reports_manifest_cleanup_when_sync_lock_times_out(
    tmp_path, monkeypatch, output_mode
):
    monkeypatch.setattr(claude_cowork, "unconfigure", lambda: None)
    monkeypatch.setattr(router, "clients_using_repo_helper", lambda: ["codex"])
    events = []

    @contextmanager
    def sync_lock(wait=0):
        events.append(("lock", wait))
        try:
            yield False
        finally:
            events.append("unlock")

    monkeypatch.setattr(router, "sync_lock", sync_lock)
    monkeypatch.setattr(
        router_schedule, "clear_manifest", lambda: events.append("clear")
    )
    monkeypatch.setattr(
        router_schedule, "uninstall", lambda: events.append("uninstall")
    )

    result = CliRunner().invoke(cli, [output_mode, "router", "unconfigure", "cowork"])

    assert result.exit_code != 0, result.output
    assert "settings were restored" in result.output
    assert "manifest cleanup is incomplete" in result.output
    assert "sync lock" in result.output
    assert "Removed Ramp Router" not in result.output
    assert events == [("lock", router_schedule.REMOVE_WAIT_SECONDS), "unlock"]


def test_background_refresh_moves_desktop_off_the_agent_for_ssh(
    tagging, tmp_path, monkeypatch
):
    claude_cowork.configure("router-secret", "https://api.router.com/v1")
    assert router_repo.is_wrapped(claude_cowork.configured_gateway_base_url())
    preferences = tmp_path / "preferences"
    preferences.mkdir()
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_PREFERENCES", str(preferences))
    plist = preferences / "com.anthropic.claudefordesktop.plist"
    plist.write_bytes(plistlib.dumps({"sshHostAllowlist": ["dev.example.com"]}))
    monkeypatch.setattr(claude_cowork, "migrate_model_selections", lambda _ids: None)

    router._refresh_cowork_model_selections(models_only=True)

    assert claude_cowork.configured_gateway_base_url() == "https://api.router.com"
    assert claude_cowork.configured_api_key() == "router-secret"

    # Once the preferences allow it again, the opted-in route comes back.
    plist.write_bytes(plistlib.dumps({}))
    router._refresh_cowork_model_selections(models_only=True)
    assert router_repo.is_wrapped(claude_cowork.configured_gateway_base_url())

    # An open Desktop is never moved, or quit, by a background refresh.
    plist.write_bytes(plistlib.dumps({"sshHostAllowlist": ["dev.example.com"]}))
    monkeypatch.setattr(claude_cowork, "_claude_is_running", lambda: True)
    router._refresh_cowork_model_selections(models_only=True)
    assert router_repo.is_wrapped(claude_cowork.configured_gateway_base_url())


def test_truncated_desktop_preferences_keep_the_direct_route(tmp_path, monkeypatch):
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_PREFERENCES", str(tmp_path))
    plist = tmp_path / "com.anthropic.claudefordesktop.plist"
    plist.write_bytes(plistlib.dumps({"sshHostAllowlist": []})[:60])
    assert claude_cowork.ssh_remote_sessions_enabled()
