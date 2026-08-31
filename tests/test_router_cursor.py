"""Tests for the guided Cursor setup command."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

import ramp_cli.commands.router as router_module
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


def test_bare_unconfigure_offers_cursor_when_installed(monkeypatch, tmp_path):
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    monkeypatch.setattr(router_module, "_clients_with_a_receipt", lambda: ())
    monkeypatch.setattr(router_module, "_cursor_is_installed", lambda: True)
    monkeypatch.setattr(router_module, "_can_draw_picker", lambda _ctx: True)
    captured = {}

    class Prompt:
        def ask(self):
            return ["cursor"]

    def checkbox(message, **kwargs):
        captured.update(kwargs)
        return Prompt()

    monkeypatch.setattr(router_module.questionary, "checkbox", checkbox)
    result = CliRunner().invoke(cli, ["--human", "router", "unconfigure"])
    assert result.exit_code == 0, result.output
    # A Cursor-only setup left no receipt, so an installed Cursor is the
    # evidence the picker runs on; picking it prints the removal steps
    # instead of claiming Router is not configured anywhere.
    assert any(
        getattr(choice, "value", None) == "cursor" for choice in captured["choices"]
    )
    assert 'Toggle off "OpenAI API Key"' in result.output


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
