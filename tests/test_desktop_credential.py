"""Native credential recovery: synthetic profiles only; no macOS Keychain access."""

import json

import click
import pytest
from click.testing import CliRunner

from ramp_cli import claude_cowork as desktop
from ramp_cli.main import cli


@pytest.fixture
def sealed(tmp_path, monkeypatch):
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    monkeypatch.setattr(desktop, "preflight", lambda: None)
    monkeypatch.setattr(desktop, "_claude_is_running", lambda: False)
    monkeypatch.setattr(desktop, "_quit_claude", lambda: None)
    monkeypatch.setattr(desktop, "_launch_claude", lambda **kw: None)
    path, _ = desktop.configure("test-secret", "https://router.example/v1")
    profile = json.loads(path.read_text())
    profile["inferenceGatewayApiKey"] = "__sealed__:v1:synthetic-ciphertext"
    path.write_text(json.dumps(profile))
    return path


def proof(key="test-secret"):
    return {"challenge": desktop.desktop_credential_request()["challenge"], "key": key}


@pytest.mark.parametrize("legacy", [False, True])
def test_recovers_old_receipts_and_survives_process_relaunch(
    sealed, monkeypatch, legacy
):
    if legacy:
        state = json.loads(desktop.state_path().read_text())
        for key in [
            "credential_ref",
            "desktop_launched_at",
            "sealed_marker",
            "sealed_verified",
        ]:
            state.pop(key, None)
        desktop.state_path().write_text(json.dumps(state))
    profile_before = sealed.read_bytes()
    assert desktop.verify_desktop_credential(proof()) == {
        "version": 1,
        "status": "ready",
    }
    assert sealed.read_bytes() == profile_before
    monkeypatch.setattr(desktop, "_claude_started_at", lambda: None)
    assert desktop.desktop_credential_request()["status"] == "ready"
    assert "test-secret" not in desktop.state_path().read_text()
    desktop.unconfigure()
    assert not sealed.exists()


def test_wrong_key_leaves_everything_unchanged(sealed):
    before = desktop.state_path().read_bytes(), sealed.read_bytes()
    with pytest.raises(click.ClickException, match="changed after setup"):
        desktop.verify_desktop_credential(proof("foreign-key"))
    assert (desktop.state_path().read_bytes(), sealed.read_bytes()) == before


@pytest.mark.parametrize("changed", ["key", "endpoint", "receipt", "deleted"])
def test_change_while_dialog_open_requires_new_approval(sealed, changed):
    pending = proof()
    if changed == "deleted":
        desktop.state_path().unlink()
    elif changed == "receipt":
        state = json.loads(desktop.state_path().read_text())
        state["credential_marker"] = desktop._credential_marker("different-key")
        desktop.state_path().write_text(json.dumps(state))
    else:
        profile = json.loads(sealed.read_text())
        field = (
            "inferenceGatewayApiKey" if changed == "key" else "inferenceGatewayBaseUrl"
        )
        profile[field] = (
            "__sealed__:v1:different" if changed == "key" else "https://other.example"
        )
        sealed.write_text(json.dumps(profile))
    before = sealed.read_bytes()
    with pytest.raises(click.ClickException, match="changed"):
        desktop.verify_desktop_credential(pending)
    assert sealed.read_bytes() == before


def test_endpoint_edit_is_rejected_before_permission(sealed):
    profile = json.loads(sealed.read_text())
    profile["inferenceGatewayBaseUrl"] = "https://other.example"
    sealed.write_text(json.dumps(profile))
    with pytest.raises(click.ClickException, match="changed after setup"):
        desktop.desktop_credential_request()


def test_unverified_seal_never_quits_or_accepts_launch_timing(sealed, monkeypatch):
    monkeypatch.setattr(desktop, "_quit_claude", lambda: pytest.fail("must not quit"))
    monkeypatch.setattr(
        desktop, "_claude_started_at", lambda: pytest.fail("timing is not proof")
    )
    with pytest.raises(click.ClickException, match="changed after setup"):
        desktop.unconfigure()
    assert desktop.desktop_credential_request()["status"] == "verification_required"


def test_timing_only_receipt_must_be_verified_again(sealed):
    state = json.loads(desktop.state_path().read_text())
    state["sealed_marker"] = desktop._credential_marker(
        "__sealed__:v1:synthetic-ciphertext"
    )
    state.pop("sealed_verified", None)
    desktop.state_path().write_text(json.dumps(state))
    assert desktop.desktop_credential_request()["status"] == "verification_required"


def test_lost_receipt_after_uninstall_does_not_adopt_profile(sealed):
    desktop.state_path().unlink()
    before = sealed.read_bytes()
    assert desktop.desktop_credential_request()["status"] == "not_configured"
    assert sealed.read_bytes() == before


def test_missing_profile_keeps_restore_available(sealed):
    sealed.unlink()
    assert desktop.desktop_credential_request()["status"] == "ready"
    desktop.unconfigure()
    assert not desktop.state_path().exists()


def test_guarded_app_reconnect_refuses_foreign_key_before_quit(sealed, monkeypatch):
    monkeypatch.setenv("RAMP_ROUTER_DESKTOP_REQUIRE_OWNED", "1")
    monkeypatch.setattr(desktop, "_quit_claude", lambda: pytest.fail("must not quit"))
    with pytest.raises(click.ClickException, match="changed after setup"):
        desktop.configure("replacement", "https://router.example/v1")


def test_guarded_app_reconnect_checks_desktop_shutdown_flush(sealed, monkeypatch):
    desktop.verify_desktop_credential(proof())
    monkeypatch.setenv("RAMP_ROUTER_DESKTOP_REQUIRE_OWNED", "1")

    def quit_with_foreign_edit():
        profile = json.loads(sealed.read_text())
        profile["inferenceGatewayApiKey"] = "foreign-key"
        sealed.write_text(json.dumps(profile))

    monkeypatch.setattr(desktop, "_quit_claude", quit_with_foreign_edit)
    with pytest.raises(click.ClickException, match="changed after setup"):
        desktop.configure("replacement", "https://router.example/v1")
    assert json.loads(sealed.read_text())["inferenceGatewayApiKey"] == "foreign-key"


def test_cli_pipe_handshake_never_echoes_plaintext(sealed):
    runner = CliRunner()
    command = ["--output", "json", "router", "configure", "desktop-credential"]
    result = runner.invoke(cli, command)
    assert result.exit_code == 0, result.output
    request = json.loads(result.output)
    assert request["status"] == "verification_required"
    result = runner.invoke(cli, command + ["--verify"], input=json.dumps(proof()))
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "ready"
    assert "test-secret" not in result.output


@pytest.mark.parametrize(
    "payload",
    ["not json", "[]", '"test-secret"', "x" * 65537],
    ids=["invalid", "array", "string", "oversize"],
)
def test_invalid_pipe_input_never_echoed(sealed, payload):
    result = CliRunner().invoke(
        cli,
        ["--output", "json", "router", "configure", "desktop-credential", "--verify"],
        input=payload,
    )
    assert result.exit_code != 0
    assert "Invalid desktop credential" in result.output
    assert "test-secret" not in result.output
