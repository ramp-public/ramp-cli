"""Claude Desktop profiles whose key Desktop sealed: synthetic, no Keychain."""

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


@pytest.mark.parametrize("legacy", [False, True])
def test_sealed_key_resolves_to_the_setup_key(sealed, monkeypatch, legacy):
    if legacy:
        # Receipts from earlier builds may carry fields this CLI ignores now.
        state = json.loads(desktop.state_path().read_text())
        state.update(
            {
                "desktop_launched_at": 1.0,
                "sealed_marker": desktop._credential_marker("other"),
                "sealed_verified": False,
            }
        )
        desktop.state_path().write_text(json.dumps(state))
    monkeypatch.setattr(desktop, "_quit_claude", lambda: pytest.fail("must not quit"))
    before = sealed.read_bytes()
    assert desktop.configured_router_connection()[0] == "test-secret"
    desktop.refresh_agent_routing(restart=True)
    assert sealed.read_bytes() == before


def test_endpoint_edit_is_still_rejected(sealed):
    profile = json.loads(sealed.read_text())
    profile["inferenceGatewayBaseUrl"] = "https://other.example"
    sealed.write_text(json.dumps(profile))
    with pytest.raises(click.ClickException, match="changed after setup"):
        desktop.configured_router_connection()


def test_missing_profile_keeps_restore_available(sealed):
    sealed.unlink()
    desktop.unconfigure()
    assert not desktop.state_path().exists()


def test_guarded_app_reconnect_accepts_a_sealed_profile(sealed, monkeypatch):
    monkeypatch.setenv("RAMP_ROUTER_DESKTOP_REQUIRE_OWNED", "1")
    desktop.configure("replacement", "https://router.example/v1")
    assert desktop.configured_api_key() == "replacement"


def test_guarded_app_reconnect_refuses_a_foreign_key_before_quit(sealed, monkeypatch):
    profile = json.loads(sealed.read_text())
    profile["inferenceGatewayApiKey"] = "foreign-key"
    sealed.write_text(json.dumps(profile))
    monkeypatch.setenv("RAMP_ROUTER_DESKTOP_REQUIRE_OWNED", "1")
    monkeypatch.setattr(desktop, "_quit_claude", lambda: pytest.fail("must not quit"))
    with pytest.raises(click.ClickException, match="changed after setup"):
        desktop.configure("replacement", "https://router.example/v1")


def test_guarded_app_reconnect_checks_desktop_shutdown_flush(sealed, monkeypatch):
    monkeypatch.setenv("RAMP_ROUTER_DESKTOP_REQUIRE_OWNED", "1")

    def quit_with_foreign_edit():
        profile = json.loads(sealed.read_text())
        profile["inferenceGatewayApiKey"] = "foreign-key"
        sealed.write_text(json.dumps(profile))

    monkeypatch.setattr(desktop, "_quit_claude", quit_with_foreign_edit)
    with pytest.raises(click.ClickException, match="changed after setup"):
        desktop.configure("replacement", "https://router.example/v1")
    assert json.loads(sealed.read_text())["inferenceGatewayApiKey"] == "foreign-key"


def test_the_native_verification_command_is_gone():
    result = CliRunner().invoke(cli, ["router", "configure", "desktop-credential"])
    # It now reads as an unknown harness name, a usage error.
    assert result.exit_code == 2
    assert "'desktop-credential' is not one of" in result.output
