"""Tests for named credential profiles and active-profile selection."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

import click
import pytest
from click.testing import CliRunner

from ramp_cli.auth import store
from ramp_cli.config import profiles, settings
from ramp_cli.main import cli


def test_switching_profiles_preserves_each_credential(isolated_config):
    store.save_tokens("production", "human-token", "", profile="human")
    store.save_tokens("production", "agent-token", "", profile="agent")
    profiles.activate("agent")
    runner = CliRunner()

    result = runner.invoke(cli, ["--human", "profile", "human"], catch_exceptions=False)

    assert result.exit_code == 0
    assert settings.load().profile == "human"
    assert store.get_tokens("production", profile="human") == ("human-token", "")
    assert store.get_tokens("production", profile="agent") == (
        "agent-token",
        "",
    )


def test_profile_command_reports_and_lists_active_profile(isolated_config):
    store.save_tokens("production", "human-token", "", profile="human")
    profiles.activate("human")
    runner = CliRunner()

    current = runner.invoke(cli, ["--human", "profile"], catch_exceptions=False)
    listed = runner.invoke(cli, ["--human", "profile", "list"], catch_exceptions=False)
    human_line = next(line for line in listed.output.splitlines() if "human" in line)

    assert current.output.strip() == "human"
    assert human_line.startswith("* human")
    assert "not authenticated" not in human_line


def test_profile_list_colors_authenticated_status_green(isolated_config):
    store.save_tokens("production", "agent-token", "", profile="agent")

    result = CliRunner().invoke(
        cli,
        ["--human", "profile", "list"],
        color=True,
        catch_exceptions=False,
    )
    agent_line = next(line for line in result.output.splitlines() if "agent" in line)

    assert click.style("authenticated", fg="green", bold=True) in agent_line


def test_profile_switch_uses_agent_json_output(isolated_config):
    store.save_tokens("production", "human-token", "", profile="human")

    result = CliRunner().invoke(
        cli, ["--agent", "profile", "human"], catch_exceptions=False
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "schema_version": "1.0",
        "data": [{"profile": "human"}],
        "pagination": None,
    }


def test_profile_switch_rejects_unknown_profile(isolated_config):
    result = CliRunner().invoke(cli, ["--human", "profile", "missing"])

    assert result.exit_code == 2
    assert "profile must be 'human' or 'agent'" in result.output


def test_profile_list_hides_legacy_credentials(isolated_config):
    store.save_tokens("production", "legacy-token", "")

    result = CliRunner().invoke(
        cli, ["--human", "profile", "list"], catch_exceptions=False
    )

    assert result.exit_code == 0
    assert "default" not in result.output
    assert "human" in result.output
    assert "agent" in result.output


def test_profile_without_named_login_explains_legacy_fallback(isolated_config):
    result = CliRunner().invoke(cli, ["--human", "profile"], catch_exceptions=False)

    assert result.exit_code == 0
    assert result.output.strip() == (
        "No active profile. Existing credentials remain in use."
    )


def test_profile_logout_clears_only_selected_identity(isolated_config, monkeypatch):
    store.save_tokens("production", "human-token", "", profile="human")
    store.save_tokens("production", "agent-token", "", profile="agent")
    profiles.activate("agent")
    monkeypatch.setenv("RAMP_PROFILE", "human")

    result = CliRunner().invoke(
        cli,
        ["--human", "auth", "logout"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert store.get_tokens("production", profile="human") == ("", "")
    assert store.get_tokens("production", profile="agent") == ("agent-token", "")
    assert settings.load().profile == "agent"


def test_profile_credentials_are_isolated_by_environment(isolated_config):
    store.save_tokens("production", "prod-token", "", profile="human")
    store.save_tokens("sandbox", "sandbox-token", "", profile="human")

    assert store.get_tokens("production", profile="human") == ("prod-token", "")
    assert store.get_tokens("sandbox", profile="human") == ("sandbox-token", "")


def test_profile_resolution_precedence(isolated_config, monkeypatch):
    store.save_tokens("production", "human-token", "", profile="human")
    profiles.activate("human")
    monkeypatch.setenv("RAMP_PROFILE", "agent")

    assert profiles.resolve_profile() == "agent"


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(
            ["--human", "--profile", "human", "profile"],
            id="before-command",
        ),
        pytest.param(
            ["--human", "profile", "--profile", "human"],
            id="after-command",
        ),
    ],
)
def test_one_off_profile_override_does_not_change_default(isolated_config, args):
    store.save_tokens("production", "human-token", "", profile="human")
    store.save_tokens("production", "agent-token", "", profile="agent")
    profiles.activate("agent")

    result = CliRunner().invoke(cli, args, catch_exceptions=False)

    assert result.exit_code == 0
    assert result.output.strip() == "human"
    assert settings.load().profile == "agent"


def test_matching_environment_and_flag_profile_are_allowed(
    isolated_config, monkeypatch
):
    monkeypatch.setenv("RAMP_PROFILE", "human")

    result = CliRunner().invoke(
        cli,
        ["--human", "--profile", "human", "profile"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert result.output.strip() == "human"


def test_environment_profile_rejects_conflicting_one_off_override(
    isolated_config, monkeypatch
):
    monkeypatch.setenv("RAMP_PROFILE", "agent")

    result = CliRunner().invoke(
        cli,
        ["--human", "--profile", "human", "profile"],
    )

    assert result.exit_code == 2
    assert "conflicts with RAMP_PROFILE='agent'" in result.output


def test_profile_environment_override_rejects_arbitrary_names(
    isolated_config, monkeypatch
):
    monkeypatch.setenv("RAMP_PROFILE", "purchasing-agent")

    result = CliRunner().invoke(cli, ["--human", "profile"])

    assert result.exit_code == 2
    assert "profile must be 'human' or 'agent'" in result.output


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("Uppercase", id="uppercase"),
        pytest.param("../agent", id="path-separator"),
        pytest.param("list", id="reserved-command"),
    ],
)
def test_invalid_profile_names_are_rejected_before_storage(isolated_config, name):
    with pytest.raises(ValueError, match="profile names must"):
        store.save_tokens("production", "token", "", profile=name)

    assert not profiles.profiles_path().exists()


def test_concurrent_profile_writes_do_not_lose_credentials(
    isolated_config, monkeypatch
):
    original_load = profiles._load

    def slow_load():
        result = original_load()
        time.sleep(0.01)
        return result

    monkeypatch.setattr(profiles, "_load", slow_load)
    names = [f"agent-{index}" for index in range(8)]

    with ThreadPoolExecutor(max_workers=len(names)) as executor:
        list(
            executor.map(
                lambda name: store.save_tokens(
                    "production", f"{name}-token", "", profile=name
                ),
                names,
            )
        )

    assert {
        name: store.get_tokens("production", profile=name)[0] for name in names
    } == {name: f"{name}-token" for name in names}


def test_profiles_file_permissions_are_private(isolated_config):
    store.save_tokens("production", "token", "", profile="human")

    assert profiles.profiles_path().stat().st_mode & 0o777 == 0o600
