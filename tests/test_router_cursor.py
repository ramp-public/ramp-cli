"""Tests for the guided Cursor setup command."""

from __future__ import annotations

import json
import sqlite3
import stat
import time
import urllib.parse

import pytest
from click.testing import CliRunner

import ramp_cli.commands.router as router_module
from ramp_cli import claude_cowork
from ramp_cli.commands import cursor, router_local_auth, router_repo, router_schedule
from ramp_cli.main import cli


def _router_model(
    identifier, *, request_name=None, display_name=None, provider_model=None
):
    metadata = {
        "schema_version": 1,
        "request_name": request_name or identifier,
        "provider_model": provider_model or request_name or identifier,
        "display_name": display_name or identifier,
        "description": "",
        "listing": {"order": 0},
        "limits": {"context_window": 128000, "max_output_tokens": 16384},
        "capabilities": {
            "modalities": {"input": ["text", "image"]},
            "tools": {"supported": True},
            "reasoning": {"efforts": [], "default_effort": ""},
        },
    }
    return router_module.RouterModel(
        id=identifier,
        metadata=router_module._model_metadata(metadata, identifier),
    )


def _mock_cursor_setup(monkeypatch, *, models=None, clipboard=True, aliases=None):
    monkeypatch.setattr(
        router_module,
        "_fetch_models",
        lambda api_key, **kwargs: (
            models
            if models is not None
            else [
                _router_model("gpt-5.6-sol"),
                _router_model("claude-opus-5"),
            ]
        ),
    )
    monkeypatch.setattr(router_module, "_copy_to_clipboard", lambda text: clipboard)
    monkeypatch.setattr(router_module, "_clipboard_available", lambda: clipboard)
    monkeypatch.setattr(
        router_module,
        "_ensure_claude_aliases",
        lambda api_key, base_url, models: aliases or {},
    )
    monkeypatch.setattr(router_module, "_fetch_configure_summary", lambda *a, **k: None)


def test_cursor_prints_guided_steps_and_keeps_the_key_off_the_terminal(monkeypatch):
    _mock_cursor_setup(monkeypatch)
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "cursor", "--api-key", "router-secret"],
    )
    assert result.exit_code == 0, result.output
    assert "router-secret" not in result.output
    assert "on the clipboard" in result.output
    assert router_module.DEFAULT_ROUTER_BASE_URL in result.output
    assert "Override OpenAI Base URL" in result.output
    assert "gpt-5.6-sol" in result.output
    assert "2 models are available" in result.output


def test_cursor_show_key_prints_the_key(monkeypatch):
    _mock_cursor_setup(monkeypatch, clipboard=False)
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "cursor", "--api-key", "router-secret", "--show-key"],
    )
    assert result.exit_code == 0, result.output
    assert "Router API key: router-secret" in result.output


def test_cursor_without_clipboard_points_at_show_key(monkeypatch):
    _mock_cursor_setup(monkeypatch, clipboard=False)
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "cursor", "--api-key", "router-secret"],
    )
    assert result.exit_code == 0, result.output
    assert "router-secret" not in result.output
    assert "--show-key" in result.output


def test_cursor_json_omits_the_key_unless_shown(monkeypatch):
    _mock_cursor_setup(monkeypatch)
    result = CliRunner().invoke(
        cli,
        ["--agent", "router", "cursor", "--api-key", "router-secret"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"][0]
    assert payload["client"] == "cursor"
    assert payload["base_url"] == router_module.DEFAULT_ROUTER_BASE_URL
    assert payload["models_available"] == 2
    assert payload["suggested_model"] == "gpt-5.6-sol"
    assert "api_key" not in payload
    assert any("Override OpenAI Base URL" in step for step in payload["instructions"])


def test_cursor_json_show_key_includes_the_key(monkeypatch):
    _mock_cursor_setup(monkeypatch)
    result = CliRunner().invoke(
        cli,
        ["--agent", "router", "cursor", "--api-key", "router-secret", "--show-key"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"][0]
    assert payload["api_key"] == "router-secret"


def test_cursor_no_input_without_a_key_fails_fast(monkeypatch):
    _mock_cursor_setup(monkeypatch)
    result = CliRunner().invoke(
        cli,
        ["--human", "--no-input", "router", "cursor"],
    )
    assert result.exit_code != 0
    assert "--api-key" in result.output


def test_cursor_json_without_a_key_fails_fast_instead_of_waiting(monkeypatch):
    _mock_cursor_setup(monkeypatch)
    monkeypatch.setattr(
        router_module,
        "acquire_router_api_key",
        lambda *a, **k: pytest.fail("browser setup must not start for JSON output"),
    )
    result = CliRunner().invoke(cli, ["--agent", "router", "cursor"])
    assert result.exit_code != 0
    assert "--api-key" in result.output


def test_cursor_drops_the_configure_key_env_before_spawning_helpers(monkeypatch):
    _mock_cursor_setup(monkeypatch)
    seen = {}

    def copy(text):
        seen["env"] = router_module.os.environ.get(router_module.CONFIGURE_KEY_ENV)
        return True

    monkeypatch.setattr(router_module, "_copy_to_clipboard", copy)
    monkeypatch.setenv(router_module.CONFIGURE_KEY_ENV, "router-secret")
    result = CliRunner().invoke(cli, ["--human", "router", "cursor"])
    assert result.exit_code == 0, result.output
    assert seen["env"] is None


def test_cursor_blank_api_key_is_a_usage_error(monkeypatch):
    _mock_cursor_setup(monkeypatch)
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "cursor", "--api-key", "   "],
    )
    assert result.exit_code == 2, result.output
    assert "cannot be empty" in result.output


def test_cursor_redirected_human_output_still_reaches_browser_setup(monkeypatch):
    _mock_cursor_setup(monkeypatch)
    monkeypatch.setattr(
        router_module,
        "acquire_router_api_key",
        lambda *a, **k: "router-secret",
    )
    result = CliRunner().invoke(cli, ["--human", "router", "cursor", "--no-browser"])
    assert result.exit_code == 0, result.output
    assert "on the clipboard" in result.output


def test_cursor_refuses_browser_setup_the_clipboard_cannot_receive(monkeypatch):
    _mock_cursor_setup(monkeypatch, clipboard=False)
    monkeypatch.setattr(router_module, "_clipboard_available", lambda: False)
    monkeypatch.setattr(
        router_module,
        "acquire_router_api_key",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("a key was minted with nowhere to go")
        ),
    )
    result = CliRunner().invoke(cli, ["--human", "router", "cursor", "--no-browser"])
    # The refusal happens before the browser opens: no key is created only to
    # be orphaned, and no secret is printed without --show-key opting in.
    assert result.exit_code != 0
    assert "Rerun with --show-key" in result.output
    assert "router-secret" not in result.output


def test_cursor_show_key_still_reaches_browser_setup_without_a_clipboard(monkeypatch):
    _mock_cursor_setup(monkeypatch, clipboard=False)
    monkeypatch.setattr(router_module, "_clipboard_available", lambda: False)
    monkeypatch.setattr(
        router_module,
        "acquire_router_api_key",
        lambda *a, **k: "router-secret",
    )
    result = CliRunner().invoke(
        cli, ["--human", "router", "cursor", "--no-browser", "--show-key"]
    )
    assert result.exit_code == 0, result.output
    assert "Router API key: router-secret" in result.output


def test_cursor_asks_before_printing_a_browser_key_the_clipboard_dropped(monkeypatch):
    # The helper existed at preflight, then delivery failed at copy time.
    _mock_cursor_setup(monkeypatch, clipboard=False)
    monkeypatch.setattr(router_module, "_clipboard_available", lambda: True)
    monkeypatch.setattr(router_module, "_terminal_is_interactive", lambda: True)
    monkeypatch.setattr(
        router_module,
        "acquire_router_api_key",
        lambda *a, **k: "router-secret",
    )
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "cursor", "--no-browser"],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    # Consent is the opt-in: answering yes prints the freshly minted key.
    assert "Print the new key" in result.output
    assert "Router API key: router-secret" in result.output


def test_cursor_keeps_an_undelivered_browser_key_secret_on_decline(monkeypatch):
    _mock_cursor_setup(monkeypatch, clipboard=False)
    monkeypatch.setattr(router_module, "_clipboard_available", lambda: True)
    monkeypatch.setattr(router_module, "_terminal_is_interactive", lambda: True)
    monkeypatch.setattr(
        router_module,
        "acquire_router_api_key",
        lambda *a, **k: "router-secret",
    )
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "cursor", "--no-browser"],
        input="n\n",
    )
    assert result.exit_code == 0, result.output
    # Declining (or EOF on a non-interactive stream) keeps the key
    # undisclosed and the guidance treats it as unused.
    assert "router-secret" not in result.output
    assert "remains unused" in result.output


def test_a_piped_yes_cannot_authorize_key_disclosure(monkeypatch):
    _mock_cursor_setup(monkeypatch, clipboard=False)
    monkeypatch.setattr(router_module, "_clipboard_available", lambda: True)
    monkeypatch.setattr(router_module, "_terminal_is_interactive", lambda: False)
    monkeypatch.setattr(
        router_module,
        "acquire_router_api_key",
        lambda *a, **k: "router-secret",
    )
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "cursor", "--no-browser"],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    # Consent must come from a person on a terminal; a piped "y" is not one,
    # so the prompt never renders and nothing is disclosed.
    assert "Print the new key" not in result.output
    assert "router-secret" not in result.output
    assert "remains unused" in result.output


def test_redirected_stdout_disables_the_disclosure_prompt(monkeypatch):
    class _Stream:
        def __init__(self, tty):
            self._tty = tty

        def isatty(self):
            return self._tty

    streams = {"stdin": _Stream(True), "stdout": _Stream(False)}
    monkeypatch.setattr(
        router_module.click, "get_text_stream", lambda name: streams[name]
    )
    # An interactive human answering "y" is not enough when stdout is a
    # redirect: the key would land in a file the prompt never mentioned.
    assert router_module._terminal_is_interactive() is False
    streams["stdout"] = _Stream(True)
    assert router_module._terminal_is_interactive() is True


def test_cursor_note_points_custom_deployments_at_their_own_origin(monkeypatch):
    lines = router_module._cursor_note_lines(
        [_router_model("claude-opus-5")],
        {},
        base_url="https://my-router.example.test/v1",
    )
    assert any(
        "https://my-router.example.test/strategies for: claude-opus-5" in line
        for line in lines
    )


def test_cursor_fallback_suggestion_skips_claude_ids(monkeypatch):
    _mock_cursor_setup(
        monkeypatch,
        models=[
            _router_model("claude-opus-5"),
            _router_model("grok-4.6"),
        ],
    )
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "cursor", "--api-key", "router-secret"],
    )
    assert result.exit_code == 0, result.output
    assert "Suggested start: grok-4.6" in result.output


def test_configure_cursor_prints_guided_steps(monkeypatch, tmp_path):
    _mock_cursor_setup(monkeypatch)
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "cursor", "--api-key", "router-secret"],
    )
    assert result.exit_code == 0, result.output
    assert "router-secret" not in result.output
    assert "on the clipboard" in result.output
    assert "Override OpenAI Base URL" in result.output
    assert "gpt-5.6-sol" in result.output
    # Cursor is guided rather than written, so it is not announced as
    # connected the way file-configured agents are.
    assert "Connected to:" not in result.output
    assert "2 models discovered" in result.output
    # The compatibility notice is not exclusive to `ramp router cursor`:
    # configure users get the same backend-transit disclosure.
    assert "the Router key transits Cursor's backend" in result.output


def test_configure_cursor_json_contract(monkeypatch, tmp_path):
    _mock_cursor_setup(monkeypatch)
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    result = CliRunner().invoke(
        cli,
        ["--agent", "router", "configure", "cursor", "--api-key", "router-secret"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"][0]
    assert payload["client"] == "cursor"
    assert payload["suggested_model"] == "gpt-5.6-sol"
    assert payload["models_available"] == 2
    assert any("Override OpenAI Base URL" in step for step in payload["instructions"])
    assert "api_key" not in payload
    assert "router-secret" not in result.output


def test_unconfigure_cursor_prints_manual_steps(monkeypatch, tmp_path):
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "unconfigure", "cursor"],
    )
    assert result.exit_code == 0, result.output
    assert "undo it there" in result.output
    assert 'Toggle off "OpenAI API Key"' in result.output
    assert "Removed Ramp Router" not in result.output


def test_configure_announces_the_clipboard_when_another_client_fails(
    monkeypatch, tmp_path
):
    _mock_cursor_setup(monkeypatch)
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))

    def boom(item, api_key, models, **kwargs):
        raise router_module.click.ClickException("codex exploded")

    monkeypatch.setattr(router_module, "_configure_client", boom)
    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "cursor",
            "codex",
            "--api-key",
            "router-secret",
        ],
    )
    assert result.exit_code != 0
    # The key hit the clipboard before Codex failed; the failure message must
    # not leave that unsaid.
    assert "already on the clipboard" in result.output
    assert "Could not configure" in result.output


def test_configure_refuses_a_cursor_only_browser_setup_without_a_clipboard(
    monkeypatch, tmp_path
):
    _mock_cursor_setup(monkeypatch, clipboard=False)
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    monkeypatch.setattr(
        router_module,
        "acquire_router_api_key",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("a key was minted with nowhere to go")
        ),
    )
    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "cursor", "--no-browser"]
    )
    # Cursor-only configure stores the key in no agent config, so with no
    # working clipboard the refusal comes before any key is created.
    assert result.exit_code != 0
    assert "ramp router cursor" in result.output


def test_a_cursor_only_setup_file_survives_a_failed_clipboard_copy(
    monkeypatch, tmp_path
):
    _mock_cursor_setup(monkeypatch, clipboard=False)
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    setup_file = tmp_path / "ramp-router-setup-abc123.json"
    setup_file.write_text(
        json.dumps(
            {
                "version": 1,
                "name": "Ramp Router",
                "base_url": router_module.DEFAULT_ROUTER_BASE_URL,
                "api_key": "router-secret",
            }
        )
    )
    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "cursor",
            "--setup-file",
            str(setup_file),
        ],
    )
    assert result.exit_code == 0, result.output
    # No agent config took the key and the clipboard dropped it, so the
    # setup file is the only copy left; deleting it would strand the user.
    assert setup_file.exists()
    assert "was kept" in result.output


def test_a_covered_setup_file_is_still_deleted_when_the_clipboard_fails(
    monkeypatch, tmp_path
):
    """A file-configured client holds the key, so the setup file may go."""
    _mock_cursor_setup(monkeypatch, clipboard=False)
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    def fake_configure(item, api_key, models, **kwargs):
        path = codex_home / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("configured")
        return path, "gpt-5.6-sol", False

    monkeypatch.setattr(router_module, "_configure_client", fake_configure)
    setup_file = tmp_path / "ramp-router-setup-abc123.json"
    setup_file.write_text(
        json.dumps(
            {
                "version": 1,
                "name": "Ramp Router",
                "base_url": router_module.DEFAULT_ROUTER_BASE_URL,
                "api_key": "router-secret",
            }
        )
    )
    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "router",
            "configure",
            "cursor",
            "codex",
            "--setup-file",
            str(setup_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (codex_home / "config.toml").exists()
    assert not setup_file.exists()


def test_cursor_lists_claude_aliases_when_the_router_creates_them(monkeypatch):
    _mock_cursor_setup(
        monkeypatch,
        models=[_router_model("claude-fable-5"), _router_model("gpt-5.6-sol")],
        aliases={"ramp-fable-5": "claude-fable-5"},
    )
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "cursor", "--api-key", "router-secret"],
    )
    assert result.exit_code == 0, result.output
    assert "add these alias names" in result.output
    assert "fable-5" in result.output
    assert "strategies" not in result.output


def test_cursor_points_at_the_dashboard_without_alias_self_service(monkeypatch):
    _mock_cursor_setup(monkeypatch)
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "cursor", "--api-key", "router-secret"],
    )
    assert result.exit_code == 0, result.output
    assert "strategies" in result.output
    # The hint names the models still waiting on a dashboard-made alias.
    assert "claude-opus-5" in result.output


def test_cursor_dashboard_hint_names_only_unaliased_models(monkeypatch):
    _mock_cursor_setup(
        monkeypatch,
        models=[
            _router_model("claude-fable-5"),
            _router_model("claude-opus-5"),
            _router_model("gpt-5.6-sol"),
        ],
        aliases={"ramp-fable-5": "claude-fable-5"},
    )
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "cursor", "--api-key", "router-secret"],
    )
    assert result.exit_code == 0, result.output
    assert "fable-5 -> claude-fable-5" in result.output
    assert "strategies for: claude-opus-5" in result.output


def test_cursor_note_skips_alias_talk_without_claude_models(monkeypatch):
    _mock_cursor_setup(monkeypatch, models=[_router_model("gpt-5.6-sol")])
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "cursor", "--api-key", "router-secret"],
    )
    assert result.exit_code == 0, result.output
    assert "strategies" not in result.output


def test_cursor_omits_the_suggestion_when_nothing_routable_exists(monkeypatch):
    _mock_cursor_setup(
        monkeypatch,
        models=[_router_model("claude-opus-5")],
        aliases={"ramp-opus-5": "claude-opus-5"},
    )
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "cursor", "--api-key", "router-secret"],
    )
    assert result.exit_code == 0, result.output
    assert "Suggested start" not in result.output


def test_cursor_json_includes_claude_aliases(monkeypatch):
    _mock_cursor_setup(
        monkeypatch,
        models=[_router_model("claude-fable-5"), _router_model("gpt-5.6-sol")],
        aliases={"ramp-fable-5": "claude-fable-5"},
    )
    result = CliRunner().invoke(
        cli,
        ["--agent", "router", "cursor", "--api-key", "router-secret"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"][0]
    assert payload["claude_aliases"] == {"ramp-fable-5": "claude-fable-5"}


def test_ensure_claude_aliases_creates_missing_names(monkeypatch):
    calls = {"posts": []}

    class _Response:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    def get(url, headers=None, timeout=None):
        assert url.endswith("/self-service/model-aliases")
        return _Response(
            200,
            {
                "data": [
                    {
                        "name": "ramp-opus-5",
                        "candidate_models": ["anthropic:claude-opus-5"],
                    },
                    {"name": "ramp-fable-5", "candidate_models": ["openai:gpt-4o"]},
                ]
            },
        )

    def post(url, headers=None, json=None, timeout=None):
        calls["posts"].append(json)
        return _Response(201, {})

    monkeypatch.setattr(router_module.httpx, "get", get)
    monkeypatch.setattr(router_module.httpx, "post", post)
    models = [
        _router_model("claude-opus-5"),
        _router_model("claude-fable-5"),
        _router_model("claude-sonnet-5"),
        _router_model("gpt-5.6-sol"),
    ]
    aliases = router_module._ensure_claude_aliases(
        "key", router_module.DEFAULT_ROUTER_BASE_URL, models
    )
    # opus-5 already routes there; fable-5 belongs to something else and is
    # left alone; sonnet-5 is created.
    assert aliases == {
        "ramp-opus-5": "claude-opus-5",
        "ramp-sonnet-5": "claude-sonnet-5",
    }
    assert calls["posts"] == [
        {"name": "ramp-sonnet-5", "candidate_models": ["claude-sonnet-5"]}
    ]


def test_ensure_claude_aliases_refuses_lookalike_candidates(monkeypatch):
    """A dated snapshot is a different model; a tier suffix is not."""

    class _Response:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    def get(url, headers=None, timeout=None):
        return _Response(
            200,
            {
                "data": [
                    {
                        "name": "ramp-haiku-4-5",
                        "candidate_models": ["anthropic:claude-haiku-4-5-20251001"],
                    },
                    {
                        "name": "ramp-opus-5",
                        "candidate_models": ["anthropic:claude-opus-5:standard_only"],
                    },
                ]
            },
        )

    monkeypatch.setattr(router_module.httpx, "get", get)
    monkeypatch.setattr(
        router_module.httpx,
        "post",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected POST")),
    )
    aliases = router_module._ensure_claude_aliases(
        "key",
        router_module.DEFAULT_ROUTER_BASE_URL,
        [_router_model("claude-haiku-4-5"), _router_model("claude-opus-5")],
    )
    # haiku-4-5 routes a pinned snapshot, not the floating model, so it is
    # not advertised; opus-5 differs only by service tier, which routes the
    # same model.
    assert aliases == {"ramp-opus-5": "claude-opus-5"}


def test_ensure_claude_aliases_recognizes_rolling_names_by_provider_model(monkeypatch):
    """A rolling request name stores its dated snapshot as the candidate.

    Router qualifies claude-sonnet-4-5 to anthropic:claude-sonnet-4-5-20250929
    at creation, so a re-run must recognize that spelling as this model's own
    alias rather than send the user to the dashboard to recreate a name they
    already hold.
    """

    class _Response:
        status_code = 200

        def json(self):
            return {
                "data": [
                    {
                        "name": "ramp-sonnet-4-5",
                        "candidate_models": ["anthropic:claude-sonnet-4-5-20250929"],
                    }
                ]
            }

    monkeypatch.setattr(router_module.httpx, "get", lambda *a, **k: _Response())
    monkeypatch.setattr(
        router_module.httpx,
        "post",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected POST")),
    )
    aliases = router_module._ensure_claude_aliases(
        "key",
        router_module.DEFAULT_ROUTER_BASE_URL,
        [
            _router_model(
                "claude-sonnet-4-5",
                provider_model="claude-sonnet-4-5-20250929",
            )
        ],
    )
    assert aliases == {"ramp-sonnet-4-5": "claude-sonnet-4-5"}


@pytest.mark.parametrize("payload", [[], None, "aliases"])
def test_ensure_claude_aliases_tolerates_non_dict_payloads(monkeypatch, payload):
    class _Response:
        status_code = 200

        def json(self):
            return payload

    monkeypatch.setattr(router_module.httpx, "get", lambda *a, **k: _Response())
    aliases = router_module._ensure_claude_aliases(
        "key",
        router_module.DEFAULT_ROUTER_BASE_URL,
        [_router_model("claude-fable-5")],
    )
    assert aliases == {}


def test_ensure_claude_aliases_fails_open_on_older_routers(monkeypatch):
    class _Response:
        status_code = 404

        def json(self):
            return {}

    monkeypatch.setattr(router_module.httpx, "get", lambda *a, **k: _Response())
    aliases = router_module._ensure_claude_aliases(
        "key",
        router_module.DEFAULT_ROUTER_BASE_URL,
        [_router_model("claude-fable-5")],
    )
    assert aliases == {}


def _cursor_binding():
    path = router_repo.cursor_binding_path()
    return json.loads(path.read_text()) if path.exists() else None


@pytest.mark.parametrize(
    "command",
    [["router", "cursor"], ["router", "configure", "cursor"]],
)
def test_cursor_setup_binds_its_key_for_transcript_uploads(
    monkeypatch, tmp_path, command
):
    _mock_cursor_setup(monkeypatch)
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    result = CliRunner().invoke(
        cli, ["--human", *command, "--api-key", "router-secret"]
    )
    assert result.exit_code == 0, result.output
    # Tagging is off here: Cursor is never revisited when it is turned on.
    assert not router_repo.enabled()
    binding = _cursor_binding()
    upstream = router_module.DEFAULT_ROUTER_BASE_URL.split("://", 1)[1].split("/")[0]
    assert binding["upstream"] == upstream
    assert binding["user_dir"] == str(cursor.user_dir().absolute())
    # The binding names the credential file, never the local token or the key.
    assert "router-secret" not in json.dumps(binding)
    assert router_local_auth.TOKEN_PREFIX not in json.dumps(binding)
    stored = router_repo.config_dir() / "router-agent-credentials"
    record = json.loads((stored / f"{binding['credential']}.json").read_text())
    assert record == {"upstream": upstream, "api_key": "router-secret"}
    mode = router_repo.cursor_binding_path().stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_cursor_setup_replaces_the_previous_binding(monkeypatch):
    _mock_cursor_setup(monkeypatch)
    for key in ("first-secret", "second-secret"):
        result = CliRunner().invoke(cli, ["router", "cursor", "--api-key", key])
        assert result.exit_code == 0, result.output
    binding = _cursor_binding()
    stored = router_repo.config_dir() / "router-agent-credentials"
    record = json.loads((stored / f"{binding['credential']}.json").read_text())
    assert record["api_key"] == "second-secret"


def test_a_failed_bind_drops_the_stale_binding(monkeypatch):
    _mock_cursor_setup(monkeypatch)
    result = CliRunner().invoke(cli, ["router", "cursor", "--api-key", "old-key"])
    assert result.exit_code == 0, result.output
    assert _cursor_binding() is not None

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(router_local_auth, "register", fail)
    result = CliRunner().invoke(cli, ["router", "cursor", "--api-key", "new-key"])
    # Setup still succeeds; only the uploads stay off.
    assert result.exit_code == 0, result.output
    assert _cursor_binding() is None


def test_unconfigure_cursor_removes_the_binding(monkeypatch, tmp_path):
    _mock_cursor_setup(monkeypatch)
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    result = CliRunner().invoke(cli, ["router", "cursor", "--api-key", "router-secret"])
    assert result.exit_code == 0, result.output
    assert _cursor_binding() is not None
    result = CliRunner().invoke(cli, ["--human", "router", "unconfigure", "cursor"])
    assert result.exit_code == 0, result.output
    assert _cursor_binding() is None


def test_bare_unconfigure_removes_a_pasted_cursor_binding(monkeypatch, tmp_path):
    _mock_cursor_setup(monkeypatch)
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    # `ramp router cursor` only hands out paste-in steps: no restore record.
    result = CliRunner().invoke(cli, ["router", "cursor", "--api-key", "router-secret"])
    assert result.exit_code == 0, result.output
    assert not cursor.has_receipt()
    result = CliRunner().invoke(cli, ["--human", "router", "unconfigure"])
    assert result.exit_code == 0, result.output
    assert _cursor_binding() is None, result.output
    assert 'Toggle off "OpenAI API Key"' in result.output


def test_a_failed_cursor_restore_keeps_the_binding(monkeypatch, tmp_path):
    _mock_cursor_setup(monkeypatch)
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    result = CliRunner().invoke(cli, ["router", "cursor", "--api-key", "router-secret"])
    assert result.exit_code == 0, result.output

    def still_running():
        raise cursor.CursorSettingsError("Quit Cursor first.")

    monkeypatch.setattr(cursor, "unconfigure", still_running)
    CliRunner().invoke(cli, ["--human", "router", "unconfigure", "cursor"])
    # Cursor still sends to Router, so its uploads keep going.
    assert _cursor_binding() is not None


@pytest.mark.parametrize("written", [False, True])
def test_setup_file_deployment_is_what_cursor_gets_and_what_is_bound(
    monkeypatch, tmp_path, written
):
    _mock_cursor_setup(monkeypatch)
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    seen = {}

    def write(api_key, base_url, models, aliases, **kwargs):
        seen["base_url"] = base_url
        return {"models_added": []} if written else None

    monkeypatch.setattr(router_module, "_write_cursor_settings", write)
    setup = tmp_path / "ramp-router-setup-x.json"
    setup.write_text(
        json.dumps(
            {
                "version": 1,
                "name": "Ramp Router",
                "base_url": "https://QA-API.router.com/v1/",
                "api_key": "router-secret",
            }
        )
    )
    result = CliRunner().invoke(
        cli, ["--agent", "router", "configure", "cursor", "--setup-file", str(setup)]
    )
    assert result.exit_code == 0, result.output
    assert urllib.parse.urlsplit(seen["base_url"]).netloc.lower() == "qa-api.router.com"
    assert _cursor_binding()["upstream"] == "qa-api.router.com"
    assert _cursor_binding()["credential"] not in result.output
    if not written:
        assert "https://QA-API.router.com/v1" in result.output


def test_base_url_flag_deployment_is_bound(monkeypatch, tmp_path):
    _mock_cursor_setup(monkeypatch)
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "router",
            "configure",
            "cursor",
            "--base-url",
            "https://internal-api.router.com/v1",
            "--api-key",
            "k",
        ],
    )
    assert result.exit_code == 0, result.output
    assert _cursor_binding()["upstream"] == "internal-api.router.com"
    assert "internal-api.router.com" in result.output


def test_router_cursor_env_deployment_is_bound(monkeypatch):
    _mock_cursor_setup(monkeypatch)
    monkeypatch.setenv("RAMP_ROUTER_BASE_URL", "https://router.example.com:8443/v1")
    result = CliRunner().invoke(cli, ["--agent", "router", "cursor", "--api-key", "k"])
    assert result.exit_code == 0, result.output
    assert "https://router.example.com:8443/v1" in result.output
    assert _cursor_binding()["upstream"] == "router.example.com:8443"


def test_removing_the_last_other_client_keeps_the_agent_for_cursor(monkeypatch):
    """Cursor's transcript uploads run in the agent, so Cursor's setup keeps it
    installed when the last other client is removed."""
    monkeypatch.setattr(claude_cowork, "unconfigure", lambda: None)
    removed = []
    monkeypatch.setattr(router_schedule, "uninstall", lambda: removed.append(1))
    args = ["--human", "router", "unconfigure", "cowork"]
    _mock_cursor_setup(monkeypatch)
    setup = CliRunner().invoke(cli, ["router", "cursor", "--api-key", "k"])
    assert setup.exit_code == 0, setup.output
    assert CliRunner().invoke(cli, args).exit_code == 0
    assert removed == []
    router_repo.unbind_cursor()
    assert CliRunner().invoke(cli, args).exit_code == 0
    assert removed == [1]


def _cursor_override(base_url):
    """Write Cursor's OpenAI base-URL override where Cursor keeps it."""
    path = cursor.state_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS ItemTable (key TEXT PRIMARY KEY, value BLOB)"
        )
        db.execute(
            "INSERT OR REPLACE INTO ItemTable VALUES (?, ?)",
            (cursor._SETTINGS_KEY, json.dumps({"openAIBaseUrl": base_url})),
        )


def _agent_status(monkeypatch, *, supported=True):
    monkeypatch.setattr(router_repo, "service_supported", lambda: supported)
    monkeypatch.setattr(router_repo, "health", lambda *a: None)
    human = CliRunner().invoke(cli, ["--human", "router", "agent", "status"])
    assert human.exit_code == 0, human.output
    agent = CliRunner().invoke(cli, ["--agent", "router", "agent", "status"])
    assert agent.exit_code == 0, agent.output
    payload = json.loads(agent.output)["data"][0]["repository_tagging"]["cursor"]
    return human.output, payload


def _bind(monkeypatch, *args):
    _mock_cursor_setup(monkeypatch)
    result = CliRunner().invoke(cli, ["router", "cursor", "--api-key", "k", *args])
    assert result.exit_code == 0, result.output


def test_status_says_nothing_about_cursor_when_it_is_not_on_router(monkeypatch):
    _cursor_override("https://api.openai.com/v1")
    output, payload = _agent_status(monkeypatch)
    assert payload is None
    assert "Cursor" not in output


def test_status_asks_cursor_users_from_before_the_binding_to_rerun_setup(
    monkeypatch,
):
    _cursor_override(router_module.DEFAULT_ROUTER_BASE_URL)
    output, payload = _agent_status(monkeypatch)
    assert payload == {"state": "needs_setup"}
    assert "Cursor repository tagging: not set up" in output
    assert "ramp router configure cursor" in output


def test_status_reports_a_bound_cursor_and_when_uploads_start(monkeypatch):
    _bind(monkeypatch)
    _cursor_override(router_module.DEFAULT_ROUTER_BASE_URL)
    output, payload = _agent_status(monkeypatch)
    assert payload["state"] == "bound"
    assert payload["uploading"] is False
    assert "uploads start once repository tagging is enabled" in output

    router_repo._save_settings({"enabled": True})
    output, payload = _agent_status(monkeypatch)
    assert payload["uploading"] is True
    assert payload["last_upload"] is None
    assert "Cursor repository tagging: set up." in output
    assert "Last Cursor upload" not in output

    # The agent records each upload attempt (cursor.rs record_upload).
    router_repo.cursor_upload_status_path().write_text(
        json.dumps(
            {
                "at": int(time.time()),
                "records": 2,
                "outcome": "rejected",
                "http_status": 401,
            }
        )
    )
    output, payload = _agent_status(monkeypatch)
    assert payload["last_upload"]["outcome"] == "rejected"
    assert "(not accepted, HTTP 401)" in output


def test_a_binding_without_a_cursor_override_still_reports_bound(monkeypatch):
    # Cursor's settings can be unreadable; the binding alone still earns a line.
    _bind(monkeypatch)
    _, payload = _agent_status(monkeypatch)
    assert payload["state"] == "bound"


def test_status_flags_a_binding_for_another_deployment_as_stale(monkeypatch):
    _bind(monkeypatch)
    _cursor_override("https://qa-api.router.com/v1")
    output, payload = _agent_status(monkeypatch)
    assert payload["state"] == "stale"
    assert payload["cursor_upstream"] == "qa-api.router.com"
    assert "out of date" in output
    assert "ramp router configure cursor" in output


def test_status_flags_a_missing_credential_as_broken(monkeypatch):
    _bind(monkeypatch)
    binding = _cursor_binding()
    stored = router_repo.config_dir() / "router-agent-credentials"
    (stored / f"{binding['credential']}.json").unlink()
    output, payload = _agent_status(monkeypatch)
    assert payload["state"] == "broken"
    assert payload["reason"] == "credential_missing"
    assert "the Router key it uploads with is missing" in output


@pytest.mark.parametrize("contents", ["not json", "{}", '{"upstream": 3}'])
def test_status_flags_an_unreadable_binding_as_broken(monkeypatch, contents):
    _bind(monkeypatch)
    router_local_auth.write_private(router_repo.cursor_binding_path(), contents)
    _, payload = _agent_status(monkeypatch)
    assert payload == {"state": "broken", "reason": "binding_unreadable"}


def test_a_world_readable_binding_is_broken_as_the_agent_would_find_it(
    monkeypatch,
):
    _bind(monkeypatch)
    router_repo.cursor_binding_path().chmod(0o644)
    _, payload = _agent_status(monkeypatch)
    assert payload["reason"] == "binding_unreadable"


def test_a_custom_deployment_needs_tagging_enabled_against_its_host(monkeypatch):
    monkeypatch.setenv("RAMP_ROUTER_BASE_URL", "https://router.example.com:8443/v1")
    _bind(monkeypatch)
    output, payload = _agent_status(monkeypatch)
    assert payload["state"] == "broken"
    assert payload["reason"] == "host_not_allowed"
    assert "router.example.com:8443 is not a Router host" in output
    assert "ramp router agent enable" in output
    # What `ramp router agent enable` records for a custom deployment.
    router_repo._save_settings({"enabled": True, "hosts": ["router.example.com"]})
    _, payload = _agent_status(monkeypatch)
    assert payload["state"] == "bound"


def test_status_skips_the_cursor_line_without_the_native_agent(monkeypatch):
    _cursor_override(router_module.DEFAULT_ROUTER_BASE_URL)
    output, payload = _agent_status(monkeypatch, supported=False)
    assert payload == {"state": "needs_setup"}
    assert "Cursor" not in output


def _stored_credential(binding):
    directory = router_repo.config_dir() / "router-agent-credentials"
    return directory / f"{binding['credential']}.json"


@pytest.mark.parametrize("contents", ["[]", "null", '"k"'])
def test_a_credential_that_is_not_an_object_is_broken(monkeypatch, contents):
    _bind(monkeypatch)
    router_local_auth.write_private(_stored_credential(_cursor_binding()), contents)
    _, payload = _agent_status(monkeypatch)
    assert payload["reason"] == "credential_missing"


def test_a_key_that_cannot_be_a_header_is_broken(monkeypatch):
    # cursor.rs credential() drops it: every upload would fail to build.
    _bind(monkeypatch)
    binding = _cursor_binding()
    router_local_auth.write_private(
        _stored_credential(binding),
        json.dumps({"upstream": binding["upstream"], "api_key": "sk-\ncursor"}),
    )
    _, payload = _agent_status(monkeypatch)
    assert payload["reason"] == "credential_missing"


@pytest.mark.parametrize(
    "upstream",
    [
        "api.router.com:99999",
        "api.router.com:x",
        "api.router.com:+1",
        "[api.router.com",
    ],
)
def test_an_upstream_the_agent_cannot_parse_is_not_allowed(monkeypatch, upstream):
    # proxy.rs default_upstream refuses these, so the agent never uploads.
    _bind(monkeypatch)
    binding = _cursor_binding()
    router_local_auth.write_private(
        _stored_credential(binding),
        json.dumps({"upstream": upstream, "api_key": "k"}),
    )
    router_local_auth.write_private(
        router_repo.cursor_binding_path(), json.dumps({**binding, "upstream": upstream})
    )
    _, payload = _agent_status(monkeypatch)
    assert payload["reason"] == "host_not_allowed"


@pytest.mark.parametrize(
    "override",
    [
        "https://API.Router.com/v1/",
        "https://api.router.com./v1",
        "https://api.router.com:443/v1",
        "https://api.router.com",
        "http://127.0.0.1:8765/u/api.router.com/v1",
    ],
)
def test_the_same_router_spelled_differently_is_not_stale(monkeypatch, override):
    _bind(monkeypatch)
    _cursor_override(override)
    _, payload = _agent_status(monkeypatch)
    assert payload["state"] == "bound"


def test_a_stale_line_never_shows_userinfo(monkeypatch):
    _bind(monkeypatch)
    _cursor_override("https://user:secret@qa-api.router.com:8443/v1")
    output, payload = _agent_status(monkeypatch)
    assert payload["state"] == "stale"
    assert payload["cursor_upstream"] == "qa-api.router.com:8443"
    assert "secret" not in output


def test_a_port_change_is_stale(monkeypatch):
    _bind(monkeypatch)
    _cursor_override("https://api.router.com:8443/v1")
    _, payload = _agent_status(monkeypatch)
    assert payload["state"] == "stale"


def test_an_upload_from_before_the_binding_is_not_shown(monkeypatch):
    _bind(monkeypatch)
    router_repo._save_settings({"enabled": True})
    status = router_repo.cursor_upload_status_path()
    bound_at = int(router_repo.cursor_binding_path().stat().st_mtime)
    status.write_text(json.dumps({"at": bound_at - 60, "outcome": "rejected"}))
    _, payload = _agent_status(monkeypatch)
    assert payload["last_upload"] is None
    status.write_text(json.dumps({"at": bound_at, "outcome": "ok", "records": 1}))
    output, payload = _agent_status(monkeypatch)
    assert payload["last_upload"]["outcome"] == "ok"
    assert "(accepted)" in output


@pytest.mark.parametrize("at", [10**20, -1, True, 1.5])
def test_a_corrupt_upload_time_does_not_break_status(monkeypatch, at):
    _bind(monkeypatch)
    router_repo._save_settings({"enabled": True})
    router_repo.cursor_upload_status_path().write_text(
        json.dumps({"at": at, "outcome": "ok"})
    )
    _, payload = _agent_status(monkeypatch)
    assert payload["last_upload"] is None


def test_a_malformed_cursor_url_is_not_a_crash(monkeypatch):
    # A URL Cursor cannot reach Router through: it no longer uses Router.
    _bind(monkeypatch)
    _cursor_override("https://[api.router.com/v1")
    _, payload = _agent_status(monkeypatch)
    assert payload["state"] == "unused"


@pytest.mark.parametrize("override", [None, "https://api.openai.com/v1"])
def test_a_binding_cursor_no_longer_uses_is_flagged(monkeypatch, override):
    """After Cursor's override is cleared or moved off Router (as the TUI's
    disconnect asks), the agent would keep uploading with the old key."""
    _bind(monkeypatch)
    _cursor_override(override)
    output, payload = _agent_status(monkeypatch)
    assert payload["state"] == "unused"
    # Tagging is off, so nothing is being sent.
    assert "inactive" in output and "still uploading" not in output
    assert "finish entering the values in Cursor's settings" in output
    assert "ramp router unconfigure cursor" in output
    router_repo._save_settings({"enabled": True})
    output, _ = _agent_status(monkeypatch)
    assert "still uploading, but Cursor isn't using Router" in output


def test_an_unreadable_cursor_database_is_not_taken_as_unused(monkeypatch):
    _bind(monkeypatch)
    path = cursor.state_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a database")
    _, payload = _agent_status(monkeypatch)
    assert payload["state"] == "bound"


def test_a_custom_router_deployment_is_not_taken_as_unused(monkeypatch):
    monkeypatch.setenv("RAMP_ROUTER_BASE_URL", "https://router.example.com/v1")
    _bind(monkeypatch)
    router_repo._save_settings({"hosts": ["router.example.com"]})
    # Not one of the known deployments, and wrapped by the local helper.
    monkeypatch.delenv("RAMP_ROUTER_BASE_URL")
    _cursor_override("http://127.0.0.1:8765/u/router.example.com/v1")
    _, payload = _agent_status(monkeypatch)
    assert payload["state"] == "bound"


@pytest.mark.parametrize(
    "override", ["https://API.Router.com/v1", "https://api.router.com:443/v1"]
)
def test_a_router_url_spelled_differently_still_asks_for_setup(monkeypatch, override):
    _cursor_override(override)
    _, payload = _agent_status(monkeypatch)
    assert payload == {"state": "needs_setup"}


def test_status_reads_the_cursor_the_binding_names(monkeypatch, tmp_path):
    _bind(monkeypatch)
    _cursor_override(router_module.DEFAULT_ROUTER_BASE_URL)
    # Status run without the override configure saw reads the bound Cursor,
    # not this process's default one.
    monkeypatch.setenv("RAMP_CURSOR_USER_DIR", str(tmp_path / "other-cursor"))
    _cursor_override("https://api.openai.com/v1")
    _, payload = _agent_status(monkeypatch)
    assert payload["state"] == "bound"


def test_status_without_a_binding_needs_no_posix_file_checks(monkeypatch):
    """The private-file checks need os.getuid, which Windows lacks, so status
    without a binding must never reach them."""
    router_repo.config_dir().mkdir(parents=True, exist_ok=True)
    _cursor_override("https://api.openai.com/v1")

    def no_private_reads(path):
        raise AssertionError(f"read_private({path}) without a binding")

    monkeypatch.setattr(router_local_auth, "read_private", no_private_reads)
    _, payload = _agent_status(monkeypatch)
    assert payload is None


def _cursor_team_settings(**settings):
    """Write Cursor's cached team admin settings where Cursor keeps them."""
    path = cursor.state_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS ItemTable (key TEXT PRIMARY KEY, value BLOB)"
        )
        db.execute(
            "INSERT OR REPLACE INTO ItemTable VALUES (?, ?)",
            ("adminSettings.cached", json.dumps(settings)),
        )


@pytest.mark.parametrize("key", cursor._ADMIN_SETTINGS_KEYS)
def test_a_team_that_blocks_personal_keys_is_detected(key):
    path = cursor.state_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
        db.execute(
            "INSERT INTO ItemTable VALUES (?, ?)",
            (key, json.dumps({"byokDisabled": True})),
        )
    assert cursor.personal_keys_blocked()


@pytest.mark.parametrize("value", ['{"byokDisabled": false}', "{}", "not json", "[]"])
def test_personal_keys_count_as_allowed_unless_cursor_says_so(value):
    path = cursor.state_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
        db.execute("INSERT INTO ItemTable VALUES ('adminSettings.cached', ?)", (value,))
    assert not cursor.personal_keys_blocked()
    path.unlink()
    assert not cursor.personal_keys_blocked()


def test_router_cursor_warns_when_the_team_blocks_personal_keys(monkeypatch):
    _mock_cursor_setup(monkeypatch)
    _cursor_team_settings(byokDisabled=True)
    human = CliRunner().invoke(cli, ["--human", "router", "cursor", "--api-key", "k"])
    assert human.exit_code == 0, human.output
    assert "Your Cursor team blocks personal API keys" in human.output
    agent = CliRunner().invoke(cli, ["--agent", "router", "cursor", "--api-key", "k"])
    assert json.loads(agent.output)["data"][0]["personal_keys_blocked"] is True


def test_router_cursor_says_nothing_when_personal_keys_are_allowed(monkeypatch):
    _mock_cursor_setup(monkeypatch)
    _cursor_team_settings(byokDisabled=False)
    human = CliRunner().invoke(cli, ["--human", "router", "cursor", "--api-key", "k"])
    assert "blocks personal API keys" not in human.output
    agent = CliRunner().invoke(cli, ["--agent", "router", "cursor", "--api-key", "k"])
    assert "personal_keys_blocked" not in json.loads(agent.output)["data"][0]


@pytest.mark.parametrize("written", [None, {"models_added": []}])
def test_configure_cursor_warns_when_the_team_blocks_personal_keys(
    monkeypatch, tmp_path, written
):
    _mock_cursor_setup(monkeypatch)
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    monkeypatch.setattr(
        router_module, "_write_cursor_settings", lambda *a, **k: written
    )
    _cursor_team_settings(byokDisabled=True)
    args = ["router", "configure", "cursor", "--api-key", "k"]
    human = CliRunner().invoke(cli, ["--human", *args])
    assert human.exit_code == 0, human.output
    assert "Your Cursor team blocks personal API keys" in human.output
    agent = CliRunner().invoke(cli, ["--agent", *args])
    assert json.loads(agent.output)["data"][0]["personal_keys_blocked"] is True


def test_status_says_when_the_team_blocks_personal_keys(monkeypatch):
    _bind(monkeypatch)
    _cursor_override(router_module.DEFAULT_ROUTER_BASE_URL)
    _cursor_team_settings(byokDisabled=True)
    output, payload = _agent_status(monkeypatch)
    assert payload["personal_keys_blocked"] is True
    assert "Your Cursor team blocks personal API keys" in output
