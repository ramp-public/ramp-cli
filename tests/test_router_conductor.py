"""Tests for the Conductor Ramp Router setup.

Conductor is a macOS app that spawns the Claude Code and Codex binaries it
vendors, so its Router setup is two wrapper executables plus the settings
keys that point Conductor at them. These tests run entirely against the
sandboxed CONDUCTOR_HOME and RAMP_CONDUCTOR_APP_SUPPORT directories the
shared conftest provides.
"""

from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

import ramp_cli.commands.router as router_module
from ramp_cli import claude_cowork
from ramp_cli.commands import conductor
from ramp_cli.commands.router import DEFAULT_ROUTER_BASE_URL as ROUTER_BASE_URL
from ramp_cli.main import cli


@pytest.fixture(autouse=True)
def _complete_browser_key_setup(monkeypatch, tmp_path_factory):
    """Keep configuration tests focused beyond host-only setup boundaries."""
    monkeypatch.setattr(
        router_module,
        "acquire_router_api_key",
        lambda _url, *, no_browser: "router-secret",
    )
    monkeypatch.setattr(claude_cowork, "preflight", lambda: None)
    monkeypatch.setattr(claude_cowork, "is_available", lambda: False)
    monkeypatch.setenv(
        "RAMP_CLAUDE_DESKTOP_APP_SUPPORT",
        str(tmp_path_factory.mktemp("claude-app-support")),
    )
    # Conductor only runs on macOS; CI does not, and what platform the tests
    # happen to run on must not decide what they assert.
    monkeypatch.setattr(conductor, "host_is_supported", lambda: True)
    # Harness-prompt extraction shells out to a Codex binary; the developer's
    # real install must not run here. The wiring has its own test below.
    monkeypatch.setattr(
        router_module, "_codex_harness_prompt_from", lambda *_args, **_kwargs: None
    )


def _router_metadata(identifier):
    """The description Router publishes for every model it serves."""
    return {
        "schema_version": 1,
        "request_name": identifier,
        "display_name": identifier,
        "description": "",
        "listing": {"order": 0},
        "limits": {"context_window": 128000, "max_output_tokens": 16384},
        "capabilities": {
            "modalities": {"input": ["text", "image"]},
            "tools": {"supported": True},
            "reasoning": {"efforts": [], "default_effort": ""},
        },
    }


def _mock_models(monkeypatch, models=None, base_url=ROUTER_BASE_URL):
    models = models or [{"id": "gpt-5.4"}]
    models = [
        {**m, "router": m.get("router", _router_metadata(m["id"]))} for m in models
    ]

    def get(url, *, headers, timeout):
        if url.endswith("/claude-code-statusline") or url.endswith("/codex-cost-hook"):
            return httpx.Response(404, request=httpx.Request("GET", url))
        if url.endswith("/session-usage/usage/balance?include_strategy_settings=true"):
            return httpx.Response(404, request=httpx.Request("GET", url))
        assert url == f"{base_url}/models"
        assert headers["Authorization"] == "Bearer router-secret"
        if headers.get("X-Gateway-Client") == "codex":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "slug": model["id"],
                            "display_name": model["router"]["display_name"],
                            "base_instructions": "",
                        }
                        for model in models
                    ]
                },
                request=httpx.Request("GET", url),
            )
        return httpx.Response(
            200, json={"data": models}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr("ramp_cli.commands.router.httpx.get", get)


def _configure(runner=None):
    runner = runner or CliRunner()
    return runner.invoke(
        cli,
        ["--human", "router", "configure", "conductor", "--api-key", "router-secret"],
    )


def test_configure_conductor_writes_wrappers_and_settings(monkeypatch):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)
    settings = home / "settings.toml"
    settings.write_text(
        '"$schema" = "https://conductor.build/schemas/settings.schema.json"\n'
        "# keep me\n"
        'codex_executable_path = "/usr/local/bin/custom-codex"\n'
        "\n"
        "[git]\n"
        'branch_prefix_type = "github_username"\n'
    )

    result = _configure()

    assert result.exit_code == 0, result.output
    assert "Connected to: Conductor" in result.output
    assert "router-secret" not in result.output
    rewritten = settings.read_text()
    parsed = tomllib.loads(rewritten)
    artifacts = home / "ramp-router"
    assert parsed["claude_code_executable_path"] == str(artifacts / "claude-code")
    assert parsed["codex_executable_path"] == str(artifacts / "codex")
    # Everything that was not an owned launcher key passes through untouched.
    assert "# keep me" in rewritten
    assert parsed["$schema"].startswith("https://conductor.build/")
    assert parsed["git"]["branch_prefix_type"] == "github_username"

    key_path = artifacts / "ramp-router-key"
    assert key_path.read_text() == "router-secret"
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600

    claude_wrapper = (artifacts / "claude-code").read_text()
    assert os.access(artifacts / "claude-code", os.X_OK)
    assert f"export ANTHROPIC_BASE_URL={ROUTER_BASE_URL.removesuffix('/v1')}" in (
        claude_wrapper
    )
    assert "'X-Gateway-Client: claude-code'" in claude_wrapper
    assert str(key_path) in claude_wrapper
    assert "unset ANTHROPIC_API_KEY" in claude_wrapper
    assert str(conductor.vendored_binary("claude")) in claude_wrapper

    codex_home = artifacts / "codex-home"
    codex_wrapper = (artifacts / "codex").read_text()
    assert os.access(artifacts / "codex", os.X_OK)
    # The launch-time CODEX_HOME is parked before the isolated one replaces
    # it, so the sync hook can still find the user's own Codex setup.
    assert (
        'export RAMP_CONDUCTOR_ORIGINAL_CODEX_HOME="${CODEX_HOME:-}"\n'
        f"export CODEX_HOME={codex_home}\n"
    ) in codex_wrapper
    assert str(conductor.vendored_binary("codex")) in codex_wrapper

    codex_config = tomllib.loads((codex_home / "config.toml").read_text())
    assert codex_config["model"] == "gpt-5.4"
    assert codex_config["model_provider"] == "ramp-router"
    provider = codex_config["model_providers"]["ramp-router"]
    assert provider["base_url"] == ROUTER_BASE_URL
    assert provider["wire_api"] == "responses"
    assert provider["auth"]["args"] == [str(key_path)]
    catalog = json.loads((codex_home / "ramp-router-models.json").read_text())
    assert codex_config["model_catalog_json"] == str(
        codex_home / "ramp-router-models.json"
    )
    assert [model["slug"] for model in catalog["models"]] == ["gpt-5.4"]

    state = json.loads((home / "ramp-router-state.json").read_text())
    assert state["base_url"] == ROUTER_BASE_URL
    assert state["settings"]["codex_executable_path"] == {
        "present": True,
        "value": "/usr/local/bin/custom-codex",
    }
    assert state["settings"]["claude_code_executable_path"] == {"present": False}


def test_unconfigure_conductor_restores_prior_launchers(monkeypatch):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)
    settings = home / "settings.toml"
    settings.write_text(
        'codex_executable_path = "/usr/local/bin/custom-codex"\n'
        "\n"
        "[models]\n"
        'default = "gpt-5.4"\n'
    )
    runner = CliRunner()
    assert _configure(runner).exit_code == 0

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "conductor"])

    assert removed.exit_code == 0, removed.output
    assert (
        "Removed Ramp Router and restored your previous settings for: Conductor."
        in removed.output
    )
    parsed = tomllib.loads(settings.read_text())
    assert parsed["codex_executable_path"] == "/usr/local/bin/custom-codex"
    assert "claude_code_executable_path" not in parsed
    assert parsed["models"]["default"] == "gpt-5.4"
    assert not (home / "ramp-router").exists()
    assert not (home / "ramp-router-state.json").exists()


def test_unconfigure_conductor_keeps_a_user_replaced_launcher(monkeypatch):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)
    settings = home / "settings.toml"
    settings.write_text("")
    runner = CliRunner()
    assert _configure(runner).exit_code == 0
    rewritten = settings.read_text().replace(
        json.dumps(str(home / "ramp-router" / "claude-code")), '"/opt/other-claude"'
    )
    settings.write_text(rewritten)

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "conductor"])

    assert removed.exit_code == 0, removed.output
    parsed = tomllib.loads(settings.read_text())
    # The user re-pointed this launcher since configure, so their edit stays.
    assert parsed["claude_code_executable_path"] == "/opt/other-claude"
    assert "codex_executable_path" not in parsed
    assert not (home / "ramp-router-state.json").exists()


def test_unconfigure_conductor_without_a_setup_reports_it(monkeypatch):
    _mock_models(monkeypatch)

    removed = CliRunner().invoke(cli, ["--human", "router", "unconfigure", "conductor"])

    assert removed.exit_code != 0
    assert "Ramp Router is not configured in Conductor." in removed.output


def test_bare_configure_skips_conductor_where_it_is_not_installed(monkeypatch):
    _mock_models(monkeypatch)
    # Nothing that could look like a Codex binary: the bare run also
    # configures Codex, whose harness-prompt extraction would otherwise run
    # the developer's real codex.
    monkeypatch.setattr(router_module.shutil, "which", lambda _name: None)
    assert not conductor.is_installed()

    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "--api-key", "router-secret"]
    )

    assert result.exit_code == 0, result.output
    assert "Conductor" not in result.output
    assert not conductor.conductor_home().exists()


def test_bare_configure_includes_conductor_where_it_is_installed(monkeypatch):
    _mock_models(monkeypatch)
    monkeypatch.setattr(router_module.shutil, "which", lambda _name: None)
    conductor.conductor_home().mkdir(parents=True)
    (conductor.conductor_home() / "settings.toml").write_text("")
    assert conductor.is_installed()

    result = CliRunner().invoke(
        cli, ["--human", "router", "configure", "--api-key", "router-secret"]
    )

    assert result.exit_code == 0, result.output
    assert "Conductor" in result.output
    assert (conductor.conductor_home() / "ramp-router-state.json").exists()


def test_picker_offers_conductor_only_when_installed(monkeypatch):
    _mock_models(monkeypatch)
    monkeypatch.setattr(router_module, "_can_draw_picker", lambda _ctx: True)
    offered = {}

    def pick(question, candidates, titles=None):
        offered["candidates"] = candidates
        # Stop after the offer is captured: proceeding would configure real
        # agents, and the offer is all this test is about.
        raise SystemExit(0)

    monkeypatch.setattr(router_module, "_pick_clients", pick)
    monkeypatch.setattr(router_module, "_installed_clients", lambda: ("codex",))
    # Cursor detection reads the real /Applications; what this developer has
    # installed must not decide what the test asserts.
    monkeypatch.setattr(router_module, "_cursor_is_installed", lambda: False)

    runner = CliRunner()
    runner.invoke(cli, ["--human", "router", "configure"])
    assert "conductor" not in offered["candidates"]

    conductor.conductor_home().mkdir(parents=True)
    (conductor.conductor_home() / "settings.toml").write_text("")
    runner.invoke(cli, ["--human", "router", "configure"])
    assert offered["candidates"] == ("codex", "conductor")


def test_refresh_reapplies_the_conductor_setup(monkeypatch):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)
    runner = CliRunner()
    assert _configure(runner).exit_code == 0
    key_path = home / "ramp-router" / "ramp-router-key"
    key_path.write_text("rotated-secret")
    monkeypatch.setattr(
        router_module,
        "acquire_router_api_key",
        lambda _url, *, no_browser: pytest.fail("refresh must reuse the stored key"),
    )

    def get(url, *, headers, timeout):
        assert headers["Authorization"] == "Bearer rotated-secret"
        if headers.get("X-Gateway-Client") == "codex":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "slug": "gpt-5.4",
                            "display_name": "gpt-5.4",
                            "base_instructions": "",
                        }
                    ]
                },
                request=httpx.Request("GET", url),
            )
        return httpx.Response(
            200,
            json={"data": [{"id": "gpt-5.4", "router": _router_metadata("gpt-5.4")}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("ramp_cli.commands.router.httpx.get", get)

    refreshed = runner.invoke(cli, ["--human", "router", "refresh"])

    assert refreshed.exit_code == 0, refreshed.output
    assert "Conductor" in refreshed.output
    assert key_path.read_text() == "rotated-secret"


def test_configure_conductor_requires_macos(monkeypatch):
    _mock_models(monkeypatch)
    monkeypatch.setattr(conductor, "host_is_supported", lambda: False)
    monkeypatch.setattr(
        router_module,
        "acquire_router_api_key",
        lambda _url, *, no_browser: pytest.fail(
            "an unsupported host must be rejected before a key is created"
        ),
    )

    result = CliRunner().invoke(cli, ["--human", "router", "configure", "conductor"])

    assert result.exit_code != 0
    assert "Conductor is only available on macOS." in result.output


def test_configure_conductor_handles_quoted_launcher_keys(monkeypatch):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)
    settings = home / "settings.toml"
    settings.write_text('"codex_executable_path" = "/usr/local/bin/custom-codex"\n')

    result = _configure()

    assert result.exit_code == 0, result.output
    parsed = tomllib.loads(settings.read_text())
    artifacts = home / "ramp-router"
    assert parsed["codex_executable_path"] == str(artifacts / "codex")
    state = json.loads((home / "ramp-router-state.json").read_text())
    assert state["settings"]["codex_executable_path"] == {
        "present": True,
        "value": "/usr/local/bin/custom-codex",
    }


def test_unconfigure_conductor_fails_closed_on_a_corrupt_receipt(monkeypatch):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)
    runner = CliRunner()
    assert _configure(runner).exit_code == 0
    (home / "ramp-router-state.json").write_text("not json")

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "conductor"])

    assert removed.exit_code != 0
    assert "receipt" in removed.output
    # Failing closed leaves the working setup in place for the repair path.
    parsed = tomllib.loads((home / "settings.toml").read_text())
    assert parsed["codex_executable_path"] == str(home / "ramp-router" / "codex")
    assert (home / "ramp-router" / "codex").exists()


def test_configure_conductor_preserves_multiline_string_bodies(monkeypatch):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)
    settings = home / "settings.toml"
    # Root-level multiline strings whose bodies look like a table header and
    # an owned launcher line: valid TOML the parser treats as prose, so the
    # rewrite must neither drop those lines nor end the root scope on them.
    note = 'note = """\n[git]\nclaude_code_executable_path = "user prose"\n"""\n'
    settings.write_text(note + "[git]\n" + 'branch_prefix_type = "github_username"\n')

    result = _configure()

    assert result.exit_code == 0, result.output
    rewritten = settings.read_text()
    assert rewritten.startswith(note)
    parsed = tomllib.loads(rewritten)
    assert parsed["note"] == '[git]\nclaude_code_executable_path = "user prose"\n'
    artifacts = home / "ramp-router"
    assert parsed["claude_code_executable_path"] == str(artifacts / "claude-code")
    assert parsed["codex_executable_path"] == str(artifacts / "codex")
    assert parsed["git"]["branch_prefix_type"] == "github_username"


def test_configure_conductor_preserves_the_settings_file_mode(monkeypatch):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)
    settings = home / "settings.toml"
    settings.write_text("")
    settings.chmod(0o600)
    runner = CliRunner()

    assert _configure(runner).exit_code == 0
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "conductor"])
    assert removed.exit_code == 0, removed.output
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600

    # A settings file this command creates gets the ordinary default mode.
    settings.unlink()
    assert _configure(runner).exit_code == 0
    assert stat.S_IMODE(settings.stat().st_mode) == 0o644


def test_configure_conductor_preserves_escaped_multiline_delimiters(monkeypatch):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)
    settings = home / "settings.toml"
    # A basic multiline string may escape its own delimiter; the body after
    # it is still prose, including a line that looks like an owned key.
    note = 'note = """\nquote \\"""\ncodex_executable_path = "prose"\n"""\n'
    settings.write_text(note)

    result = _configure()

    assert result.exit_code == 0, result.output
    parsed = tomllib.loads(settings.read_text())
    assert parsed["note"] == 'quote """\ncodex_executable_path = "prose"\n'
    assert parsed["codex_executable_path"] == str(home / "ramp-router" / "codex")


def test_settings_rewrite_refuses_to_alter_unowned_values(monkeypatch, tmp_path):
    # Whatever a future TOML construct does to the line filter, the parser
    # is the judge: a rendered document whose unowned values differ is
    # refused rather than written.
    monkeypatch.setattr(
        router_module,
        "_render_conductor_settings",
        lambda _text, values: "".join(
            f"{key} = {json.dumps(value)}\n" for key, value in values.items()
        ),
    )

    with pytest.raises(Exception) as excinfo:
        router_module._verified_conductor_settings(
            'keep = "me"\n',
            {
                "claude_code_executable_path": "/w/claude",
                "codex_executable_path": "/w/codex",
            },
            tmp_path / "settings.toml",
        )

    assert "would alter settings this setup does not own" in str(excinfo.value)


def test_configure_conductor_ignores_delimiters_inside_comments(monkeypatch):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)
    settings = home / "settings.toml"
    settings.write_text(
        '# Multiline strings use """ like this: """text"""\n'
        "[git]\n"
        'branch_prefix_type = "github_username"\n'
    )

    result = _configure()

    assert result.exit_code == 0, result.output
    parsed = tomllib.loads(settings.read_text())
    assert parsed["codex_executable_path"] == str(home / "ramp-router" / "codex")
    assert parsed["git"]["branch_prefix_type"] == "github_username"


def test_configure_conductor_writes_through_a_symlinked_settings_file(
    monkeypatch, tmp_path
):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    target = dotfiles / "conductor-settings.toml"
    target.write_text('codex_executable_path = "/usr/local/bin/custom-codex"\n')
    settings = home / "settings.toml"
    settings.symlink_to(target)
    runner = CliRunner()

    assert _configure(runner).exit_code == 0
    assert settings.is_symlink()
    assert tomllib.loads(target.read_text())["codex_executable_path"] == str(
        home / "ramp-router" / "codex"
    )

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "conductor"])
    assert removed.exit_code == 0, removed.output
    assert settings.is_symlink()
    assert tomllib.loads(target.read_text()) == {
        "codex_executable_path": "/usr/local/bin/custom-codex"
    }


def test_conductor_codex_home_registers_session_sync(monkeypatch):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)

    assert _configure().exit_code == 0

    config = tomllib.loads(
        (home / "ramp-router" / "codex-home" / "config.toml").read_text()
    )
    [group] = config["hooks"]["SessionStart"]
    assert group["matcher"] == "startup|resume"
    [hook] = group["hooks"]
    # The vendored Codex hosts this home, so the codex-shaped hook keeps the
    # generated catalog, prompt, and default model fresh between sessions —
    # and the sync is handed the user's own Codex home, since the wrapper's
    # CODEX_HOME would otherwise steer the detached refresh at this one.
    standalone_home = router_module._codex_config_path().parent
    assert hook["command"] == (
        "[ -x /opt/ramp-cli/bin/ramp ] && "
        'CODEX_HOME="${RAMP_CONDUCTOR_ORIGINAL_CODEX_HOME:-'
        f'{standalone_home}}}" /opt/ramp-cli/bin/ramp '
        "router sync --hook --client codex || true"
    )
    state = json.loads((home / "ramp-router-state.json").read_text())
    assert state["standalone_codex_home"] == str(standalone_home)


def test_unconfigure_keeps_a_dangling_user_symlink(monkeypatch, tmp_path):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    target = dotfiles / "conductor-settings.toml"
    settings = home / "settings.toml"
    # The user's link exists before configure; its target does not yet.
    settings.symlink_to(target)
    runner = CliRunner()

    assert _configure(runner).exit_code == 0
    assert target.exists()
    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "conductor"])

    assert removed.exit_code == 0, removed.output
    # The link is the user's; only the launcher keys leave its target.
    assert settings.is_symlink()
    assert tomllib.loads(target.read_text()) == {}


def test_sync_hook_never_points_at_the_isolated_home_itself(monkeypatch):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)
    codex_home = home / "ramp-router" / "codex-home"
    runner = CliRunner()
    # A first configure from the user's shell records their standalone home.
    assert _configure(runner).exit_code == 0
    recorded = router_module._codex_config_path().parent
    # A reconfigure from inside a Conductor session: the wrapper's CODEX_HOME
    # already names the isolated home this very setup writes, so the recorded
    # standalone home is carried forward rather than the isolated one.
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert _configure(runner).exit_code == 0

    config = tomllib.loads((codex_home / "config.toml").read_text())
    [hook] = config["hooks"]["SessionStart"][0]["hooks"]
    assert f":-{recorded}}}" in hook["command"]
    assert str(codex_home) not in hook["command"].split("&&", 1)[1]

    # With no record at all, the wrapper's parked value answers, then the
    # default home.
    (home / "ramp-router-state.json").unlink()
    monkeypatch.setenv("RAMP_CONDUCTOR_ORIGINAL_CODEX_HOME", "/custom/codex-home")
    assert _configure(runner).exit_code == 0
    config = tomllib.loads((codex_home / "config.toml").read_text())
    assert (
        ":-/custom/codex-home}"
        in config["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    )
    (home / "ramp-router-state.json").unlink()
    monkeypatch.delenv("RAMP_CONDUCTOR_ORIGINAL_CODEX_HOME")
    assert _configure(runner).exit_code == 0
    config = tomllib.loads((codex_home / "config.toml").read_text())
    assert (
        f":-{Path.home() / '.codex'}}}"
        in (config["hooks"]["SessionStart"][0]["hooks"][0]["command"])
    )


def test_unconfigure_restores_an_absent_settings_file(monkeypatch):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    assert not home.exists()
    runner = CliRunner()

    # An explicit preconfigure on a machine that never had Conductor.
    assert _configure(runner).exit_code == 0
    assert (home / "settings.toml").exists()
    assert conductor.is_installed()

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "conductor"])

    assert removed.exit_code == 0, removed.output
    # The file was this command's creation, so its absence is restored. The
    # lock file stays (replacing it would split the lock), and detection
    # knows a directory holding only that file is not a Conductor.
    assert not (home / "settings.toml").exists()
    assert {entry.name for entry in home.iterdir()} == {conductor.LOCK_FILENAME}
    assert not conductor.is_installed()


def test_unconfigure_keeps_a_created_settings_file_that_gained_preferences(
    monkeypatch,
):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    runner = CliRunner()
    assert _configure(runner).exit_code == 0
    settings = home / "settings.toml"
    # Conductor (or the user) added preferences to the file this command
    # created, without touching either launcher key.
    settings.write_text(settings.read_text() + '\n[git]\nbranch_prefix = "neel"\n')

    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "conductor"])

    assert removed.exit_code == 0, removed.output
    parsed = tomllib.loads(settings.read_text())
    assert parsed == {"git": {"branch_prefix": "neel"}}


def test_a_pending_receipt_never_enrolls_refresh(monkeypatch):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)
    runner = CliRunner()
    assert _configure(runner).exit_code == 0
    state_path = home / "ramp-router-state.json"
    promoted = json.loads(state_path.read_text())
    assert "pending" not in promoted
    # A configure toward a new generation interrupted after publishing its
    # receipt but before the credential beside it was replaced.
    state_path.write_text(json.dumps({**promoted, "pending": True}))

    assert "conductor" not in router_module.configured_router_clients()
    assert router_module._stored_router_api_key_choices() == []
    refreshed = runner.invoke(cli, ["--human", "router", "refresh"])
    assert "Ramp Router is not configured in any coding agent." in refreshed.output

    # Explicit configure repairs the interrupted attempt and promotes it.
    assert _configure(runner).exit_code == 0
    assert "pending" not in json.loads(state_path.read_text())
    assert router_module.configured_router_clients() == ("conductor",)


def test_refresh_rejects_a_receipt_without_an_endpoint(monkeypatch):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)
    runner = CliRunner()
    assert _configure(runner).exit_code == 0
    state_path = home / "ramp-router-state.json"
    state = json.loads(state_path.read_text())
    del state["base_url"]
    state_path.write_text(json.dumps(state))

    # The stored key must never be transmitted to a fallback endpoint the
    # receipt cannot vouch for: enrollment refuses the receipt outright, and
    # the endpoint reader fails closed for any caller that reaches it anyway.
    assert "conductor" not in router_module.configured_router_clients()
    with pytest.raises(Exception) as excinfo:
        router_module._stored_router_base_url("conductor", conductor.settings_path())
    assert "Could not read the Router endpoint" in str(excinfo.value)


def test_refresh_keeps_a_user_replaced_launcher(monkeypatch):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)
    runner = CliRunner()
    assert _configure(runner).exit_code == 0
    settings = home / "settings.toml"
    rewritten = settings.read_text().replace(
        json.dumps(str(home / "ramp-router" / "claude-code")), '"/opt/other-claude"'
    )
    settings.write_text(rewritten)

    refreshed = runner.invoke(cli, ["--human", "router", "refresh"])

    assert refreshed.exit_code == 0, refreshed.output
    parsed = tomllib.loads(settings.read_text())
    # Refresh keeps artifacts fresh but never re-imposes a launcher the user
    # deliberately pointed elsewhere; only an explicit configure does.
    assert parsed["claude_code_executable_path"] == "/opt/other-claude"
    assert parsed["codex_executable_path"] == str(home / "ramp-router" / "codex")


def test_refresh_keeps_a_user_deleted_launcher(monkeypatch):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)
    runner = CliRunner()
    assert _configure(runner).exit_code == 0
    settings = home / "settings.toml"
    settings.write_text(
        "".join(
            line
            for line in settings.read_text().splitlines(keepends=True)
            if not line.startswith("claude_code_executable_path")
        )
    )

    refreshed = runner.invoke(cli, ["--human", "router", "refresh"])

    assert refreshed.exit_code == 0, refreshed.output
    parsed = tomllib.loads(settings.read_text())
    # The user removed this launcher line; refresh must not reverse that.
    assert "claude_code_executable_path" not in parsed
    assert parsed["codex_executable_path"] == str(home / "ramp-router" / "codex")


def test_a_torn_down_receipt_never_enrolls_refresh(monkeypatch):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)
    runner = CliRunner()
    assert _configure(runner).exit_code == 0
    state_path = home / "ramp-router-state.json"
    state = json.loads(state_path.read_text())
    # An unconfigure interrupted after its teardown mark but before receipt
    # removal: the mark alone must keep every background refresh out.
    state_path.write_text(json.dumps({**state, "unconfigured": True}))

    assert "conductor" not in router_module.configured_router_clients()
    settings_before = (home / "settings.toml").read_text()
    refreshed = runner.invoke(cli, ["--human", "router", "refresh"])
    assert (home / "settings.toml").read_text() == settings_before
    assert "Ramp Router is not configured in any coding agent." in refreshed.output

    # A rerun of unconfigure still finishes the interrupted cleanup.
    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "conductor"])
    assert removed.exit_code == 0, removed.output
    assert not state_path.exists()
    assert not (home / "ramp-router").exists()


def test_an_incomplete_receipt_never_enrolls_refresh(monkeypatch):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)
    runner = CliRunner()
    assert _configure(runner).exit_code == 0
    state_path = home / "ramp-router-state.json"
    state = json.loads(state_path.read_text())
    # A receipt without the restore snapshot: refresh over it would record
    # the live Router wrappers as the user's previous launchers.
    del state["settings"]
    state_path.write_text(json.dumps(state))

    assert "conductor" not in router_module.configured_router_clients()
    settings_before = (home / "settings.toml").read_text()
    refreshed = runner.invoke(cli, ["--human", "router", "refresh"])
    assert (home / "settings.toml").read_text() == settings_before
    assert "Ramp Router is not configured in any coding agent." in refreshed.output


def test_repair_configure_never_snapshots_the_wrappers_as_previous(monkeypatch):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)
    runner = CliRunner()
    assert _configure(runner).exit_code == 0
    # The receipt is lost while the wrappers stay active in the settings.
    (home / "ramp-router-state.json").unlink()

    assert _configure(runner).exit_code == 0
    removed = runner.invoke(cli, ["--human", "router", "unconfigure", "conductor"])

    assert removed.exit_code == 0, removed.output
    parsed = tomllib.loads((home / "settings.toml").read_text())
    # The repair run had no record of what stood before Router, so the
    # launchers restore to absent — never to the deleted wrappers.
    assert "claude_code_executable_path" not in parsed
    assert "codex_executable_path" not in parsed


def test_refresh_will_not_resurrect_an_unconfigured_setup(monkeypatch):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)

    with pytest.raises(Exception) as excinfo:
        router_module._configure_conductor(
            conductor.settings_path(),
            "router-secret",
            "gpt-5.4",
            require_receipt=True,
        )

    assert "no longer configured" in str(excinfo.value)
    assert not (home / "ramp-router").exists()


def test_configure_conductor_writes_the_vendored_harness_prompt(monkeypatch):
    _mock_models(monkeypatch)
    home = conductor.conductor_home()
    home.mkdir(parents=True)
    captured = {}

    def harness_prompt(executable, default_model, *, bundled=False):
        captured["request"] = (executable, default_model, bundled)
        return "VENDORED HARNESS PROMPT"

    monkeypatch.setattr(router_module, "_codex_harness_prompt_from", harness_prompt)
    bin_dir = conductor.app_support_dir() / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "codex").write_text("#!/bin/sh\n")

    result = _configure()

    assert result.exit_code == 0, result.output
    codex_home = home / "ramp-router" / "codex-home"
    instructions = codex_home / "ramp-router-instructions.md"
    assert instructions.read_text() == "VENDORED HARNESS PROMPT"
    config = tomllib.loads((codex_home / "config.toml").read_text())
    assert config["model_instructions_file"] == str(instructions)
    # The prompt comes from the binary Conductor actually spawns, and from
    # its bundled catalog: a Router-configured binary reports empty
    # instructions from the ordinary read.
    assert captured["request"] == (conductor.vendored_binary("codex"), "gpt-5.4", True)
