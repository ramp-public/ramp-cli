"""Tests for host-side Claude Cowork Router setup."""

from __future__ import annotations

import json
import stat
from contextlib import contextmanager
from pathlib import Path

import click
import pytest

from ramp_cli import claude_cowork


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
        "inferenceCustomHeaders": {"X-Gateway-Client": "claude-cowork"},
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
    # The profile is read only after Claude stops, then the app is reopened.
    assert events == ["quit", "launch", "quit", "launch"]


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
    assert events == ["quit", "launch", "quit", "launch"]


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


def test_failed_configure_rolls_back_partial_files_and_reopens_claude(
    cowork_host, monkeypatch
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

    with pytest.raises(click.ClickException, match="Could not configure"):
        claude_cowork.configure("router-secret", "https://router.example/v1")

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
