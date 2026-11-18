"""configure cursor on a macOS terminal: no paste-in steps, and no lost keys."""

import json

import pytest
from click.testing import CliRunner

from ramp_cli.commands import cursor, router_repo
from ramp_cli.commands import router as router_module
from ramp_cli.main import cli
from tests.test_cursor_settings import (  # noqa: F401
    BY_HAND,
    OPEN,
    SETTINGS_ERROR,
    _settings,
    cursor_db,
    mac_setup,
)
from tests.test_router_cursor import _router_model

REUSED = "Cursor: reusing the Router key from your last unfinished setup."
MANUAL_REUSED = "Reusing the Router key from your last unfinished Cursor setup."
BASE_URL = "https://api.router.com/v1"


@pytest.fixture
def mac(mac_setup, monkeypatch):  # noqa: F811
    """A person at a macOS terminal, with Cursor closed."""
    monkeypatch.setattr(
        router_module,
        "_can_prompt",
        lambda ctx, fmt: fmt != "json" and not ctx.obj["no_input"],
    )
    return mac_setup


@pytest.fixture
def clipboard(monkeypatch):
    copies = []

    def copy(text):
        copies.append(text)
        return True

    monkeypatch.setattr(router_module, "_copy_to_clipboard", copy)
    return copies


def _unreadable_settings(monkeypatch):
    def encrypt(key, prompt=True):
        raise cursor.CursorSettingsError("Cursor's settings could not be read.")

    monkeypatch.setattr(cursor, "encrypt_with_cursor", encrypt)


def _reopened_during_setup(monkeypatch):
    # Closed when setup starts, open again by the time it writes.
    checks = iter([[]])
    monkeypatch.setattr(cursor, "running_pids", lambda: next(checks, [4242]))
    monkeypatch.setattr(
        cursor, "wait_until_quit", lambda *a, **k: pytest.fail("waited for Cursor")
    )


@pytest.mark.parametrize(
    ("break_setup", "reason"),
    [
        (_unreadable_settings, SETTINGS_ERROR),
        (_reopened_during_setup, OPEN),
    ],
    ids=["settings-error", "reopened"],
)
def test_a_macos_terminal_fallback_prints_no_steps(
    mac, monkeypatch, clipboard, break_setup, reason
):
    break_setup(monkeypatch)
    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "cursor", "--api-key", "k"]
    )
    assert result.exit_code == router_module.CURSOR_NOT_SET_UP_EXIT_CODE == 1
    assert result.output.splitlines()[-2:] == [reason, BY_HAND]
    assert not any(
        line.lstrip().startswith("1. ") for line in result.output.splitlines()
    )
    assert "Override OpenAI Base URL" not in result.output
    assert clipboard == []
    # Nothing changed, so there is nothing to restore, and it isn't an error.
    assert "unconfigure" not in result.output
    assert "Error:" not in result.output


class Browser:
    """Browser setup, counting the keys it creates."""

    def __init__(self, monkeypatch):
        self.created = []
        monkeypatch.setattr(router_module, "_pick_stored_router_api_key", lambda: None)
        monkeypatch.setattr(router_module, "acquire_router_api_key", self.acquire)

    def acquire(self, *args, **kwargs):
        key = f"browser-key-{len(self.created) + 1}"
        self.created.append(key)
        return key


def _configure(*flags, root=("--human",)):
    return CliRunner().invoke(
        cli, [*root, "router", "configure", "cursor", "--no-browser", *flags]
    )


def _unfinished_setup(monkeypatch):
    """A first run that created a key, then couldn't write Cursor's settings."""
    browser = Browser(monkeypatch)
    _unreadable_settings(monkeypatch)
    assert _configure().exit_code == 1
    return browser


def test_json_keeps_the_paste_in_fields(mac, monkeypatch, clipboard):
    _unreadable_settings(monkeypatch)
    result = CliRunner().invoke(
        cli, ["--agent", "router", "configure", "cursor", "--api-key", "k"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"][0]
    # JSON has nothing "below" to point at.
    assert payload["fallback_message"] == SETTINGS_ERROR
    assert payload["api_key_on_clipboard"] is True
    assert payload["instructions"][0].startswith("Open Cursor Settings (Cmd+Shift+J)")
    assert payload["note_lines"]
    for internal in ("steps_shown", "fallback_summary", "summary", "api_key_reused"):
        assert internal not in payload
    assert clipboard == ["k"]


def _encrypting(monkeypatch):
    encrypted = []

    def encrypt(key, prompt=True):
        encrypted.append(key)
        return '{"type":"Buffer","data":[1]}'

    monkeypatch.setattr(cursor, "encrypt_with_cursor", encrypt)
    return encrypted


def test_a_key_created_for_an_unfinished_setup_is_reused(mac, monkeypatch, clipboard):
    browser = _unfinished_setup(monkeypatch)
    assert router_repo.pending_cursor_key(BASE_URL) == "browser-key-1"
    assert clipboard == []
    # The agent must not upload Cursor's tags while Cursor isn't on Router.
    assert not router_repo.cursor_bound()

    encrypted = _encrypting(monkeypatch)
    second = _configure()
    assert second.exit_code == 0, second.output
    assert REUSED in second.output
    # No second key: the one from the first run went into Cursor.
    assert browser.created == ["browser-key-1"]
    assert encrypted == ["browser-key-1"]
    assert router_repo.pending_cursor_key(BASE_URL) is None
    assert router_repo.cursor_bound()


def _unreachable(monkeypatch):
    def fetch(*args, **kwargs):
        raise router_module.click.ClickException("We couldn't reach Ramp Router.")

    monkeypatch.setattr(router_module, "_fetch_models", fetch)


@pytest.mark.parametrize(
    "fail_again",
    [lambda monkeypatch: None, _unreachable],
    ids=["falls-back-again", "router-unreachable"],
)
def test_a_rerun_that_fails_again_keeps_the_key_pending(
    mac, monkeypatch, clipboard, fail_again
):
    browser = _unfinished_setup(monkeypatch)
    fail_again(monkeypatch)
    assert _configure().exit_code == 1
    # Not rejected: the next run can still use it.
    assert browser.created == ["browser-key-1"]
    assert router_repo.pending_cursor_key(BASE_URL) == "browser-key-1"


def _reject(monkeypatch, *rejected):
    def fetch(api_key, **kwargs):
        if api_key in rejected:
            raise router_module.RouterKeyRejectedError(
                "That API key wasn't accepted by Ramp Router."
            )
        return [_router_model("gpt-5.6-sol")]

    monkeypatch.setattr(router_module, "_fetch_models", fetch)


@pytest.mark.parametrize(
    "command",
    [["configure", "cursor"], ["cursor"]],
    ids=["configure", "manual"],
)
def test_a_rejected_pending_key_is_replaced_with_a_new_one(
    mac, monkeypatch, clipboard, command
):
    browser = _unfinished_setup(monkeypatch)
    _reject(monkeypatch, "browser-key-1")
    _encrypting(monkeypatch)
    result = CliRunner().invoke(cli, ["--human", "router", *command, "--no-browser"])
    assert result.exit_code == 0, result.output
    assert REUSED not in result.output
    assert MANUAL_REUSED not in result.output
    assert browser.created == ["browser-key-1", "browser-key-2"]
    assert router_repo.pending_cursor_key(BASE_URL) is None


@pytest.mark.parametrize(
    "extra", [["codex"], ["--enable-agent"]], ids=["other-agents", "enable-agent"]
)
def test_only_a_cursor_only_run_reuses_the_pending_key(
    mac, monkeypatch, clipboard, extra
):
    _unfinished_setup(monkeypatch)
    seen = []
    monkeypatch.setattr(
        router_repo,
        "pending_cursor_key",
        lambda base_url: seen.append(base_url) or "browser-key-1",
    )
    monkeypatch.setattr(
        router_module,
        "_configure_client",
        lambda *a, **k: pytest.fail("reached another agent"),
    )

    def sign_in(*args, **kwargs):
        raise router_module.click.ClickException("signed in")

    monkeypatch.setattr(router_module, "acquire_router_api_key", sign_in)
    monkeypatch.setattr(router_module.router_credentials, "acquire_key", sign_in)
    monkeypatch.setattr(router_repo, "service_supported", lambda: True)
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "cursor", *extra, "--no-browser"],
    )
    # Each went to its own sign-in instead.
    assert "signed in" in result.output, result.output
    assert seen == []


def test_manual_setup_hands_over_the_pending_key(mac, monkeypatch, clipboard):
    browser = _unfinished_setup(monkeypatch)
    result = CliRunner().invoke(cli, ["--human", "router", "cursor"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    # After the header, not before it.
    assert (
        lines.index(MANUAL_REUSED)
        == lines.index("Connecting Ramp Router to Cursor") + 1
    )
    assert browser.created == ["browser-key-1"]
    assert clipboard == ["browser-key-1"]
    # Handed over, so it is bound now and no longer pending.
    assert router_repo.pending_cursor_key(BASE_URL) is None
    assert router_repo.cursor_bound()


@pytest.mark.parametrize("flag", ["--agent", "--no-input"])
def test_unattended_manual_setup_leaves_the_pending_key_alone(
    mac, monkeypatch, clipboard, flag
):
    _unfinished_setup(monkeypatch)
    result = CliRunner().invoke(cli, [flag, "router", "cursor"])
    assert result.exit_code == 2, result.output
    assert "Pass --api-key" in result.output
    assert router_repo.pending_cursor_key(BASE_URL) == "browser-key-1"


def test_a_pending_key_belongs_to_its_router_and_goes_with_unconfigure(
    mac, monkeypatch, clipboard
):
    _unfinished_setup(monkeypatch)
    assert router_repo.pending_cursor_key("https://router.example.com/v1") is None
    assert router_repo.pending_cursor_key(BASE_URL) == "browser-key-1"
    result = CliRunner().invoke(cli, ["--human", "router", "unconfigure", "cursor"])
    assert result.exit_code == 0, result.output
    assert router_repo.pending_cursor_key(BASE_URL) is None


def test_a_supplied_key_is_not_kept(mac, monkeypatch, clipboard):
    _unreadable_settings(monkeypatch)
    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "cursor", "--api-key", "k"]
    )
    assert result.exit_code == 1, result.output
    assert router_repo.pending_cursor_key(BASE_URL) is None


def test_the_steps_are_shown_when_a_new_key_cannot_be_kept(mac, monkeypatch, clipboard):
    Browser(monkeypatch)
    _unreadable_settings(monkeypatch)
    monkeypatch.setattr(router_repo, "mark_cursor_key_pending", lambda *a: False)
    result = _configure()
    assert result.exit_code == 0, result.output
    # Without a way to reuse it, the new key must still reach the user.
    lines = result.output.splitlines()
    assert lines.index(SETTINGS_ERROR) < lines.index("Finish in Cursor:")
    assert clipboard == ["browser-key-1"]
    assert router_repo.cursor_bound()


@pytest.mark.parametrize("cursor_written", [False, True], ids=["fell-back", "written"])
def test_another_agent_failing_still_reports_on_cursor(
    mac, monkeypatch, cursor_written
):
    if not cursor_written:
        _unreadable_settings(monkeypatch)

    def configure_client(item, api_key, models, **kwargs):
        raise router_module.click.ClickException("Codex broke.")

    monkeypatch.setattr(router_module, "_configure_client", configure_client)
    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "cursor", "codex", "--api-key", "k"]
    )
    assert result.exit_code == 1, result.output
    assert "Codex broke." in result.output
    if cursor_written:
        assert "Cursor: set up. Open Cursor" in result.output
    else:
        lines = result.output.splitlines()
        assert lines[lines.index(SETTINGS_ERROR) + 1] == BY_HAND


def test_a_pending_key_the_clipboard_couldnt_take_stays_for_show_key(mac, monkeypatch):
    monkeypatch.setattr(router_module, "_copy_to_clipboard", lambda text: True)
    browser = _unfinished_setup(monkeypatch)
    # No clipboard this time: the steps say to rerun with --show-key.
    monkeypatch.setattr(router_module, "_copy_to_clipboard", lambda text: False)
    failed = CliRunner().invoke(cli, ["--human", "router", "cursor"])
    assert failed.exit_code == 0, failed.output
    assert "rerun with --show-key" in failed.output
    assert router_repo.pending_cursor_key(BASE_URL) == "browser-key-1"
    shown = CliRunner().invoke(cli, ["--human", "router", "cursor", "--show-key"])
    assert shown.exit_code == 0, shown.output
    # The same key, printed, and no new one created.
    assert "Router API key: browser-key-1" in shown.output
    assert browser.created == ["browser-key-1"]
    assert router_repo.pending_cursor_key(BASE_URL) is None


def test_steps_that_couldnt_copy_a_reused_key_keep_it_pending(mac, monkeypatch):
    monkeypatch.setattr(router_module, "_copy_to_clipboard", lambda text: True)
    _unfinished_setup(monkeypatch)
    # Shown steps, as when the key couldn't be kept, but no clipboard.
    monkeypatch.setattr(router_module, "_copy_to_clipboard", lambda text: False)
    monkeypatch.setattr(router_repo, "mark_cursor_key_pending", lambda *a: False)
    result = _configure()
    assert result.exit_code == 0, result.output
    assert "ramp router cursor --show-key" in result.output
    assert router_repo.pending_cursor_key(BASE_URL) == "browser-key-1"
