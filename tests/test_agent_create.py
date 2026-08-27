"""Tests for ``ramp agent create``."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from ramp_cli.main import cli

ROLE_ID = "7c322160-2871-4382-b026-92597ce3ed19"
OWNER_ID = "1f0f2c31-8a06-4d20-9a5f-0d63d7b1e9aa"


@pytest.fixture
def captured_post(monkeypatch):
    calls: list[dict] = []

    def _fake_post(self, path: str, json_body: bytes, headers=None) -> bytes:
        calls.append({"path": path, "body": json.loads(json_body)})
        return json.dumps(
            {
                "id": "8a2c7f2e-5f5e-4f5a-9e2f-2c1c0b9b1a11",
                "name": "Procurement Agent",
                "client_id": "ramp_id_abc",
                "client_secret": "ramp_sec_xyz",
            }
        ).encode()

    monkeypatch.setattr("ramp_cli.client.api.RampClient.post", _fake_post)
    return calls


def test_agent_create_help_exits_zero():
    result = CliRunner().invoke(cli, ["agent", "create", "--help"])
    assert result.exit_code == 0, result.output
    assert "--role-id" in result.output


def test_agent_create_posts_expected_body(captured_post):
    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "agent",
            "create",
            "--name",
            "Procurement Agent",
            "--role-id",
            ROLE_ID,
            "--description",
            "Automates purchase order intake.",
            "--owner-id",
            OWNER_ID,
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured_post == [
        {
            "path": "/developer/v1/agents",
            "body": {
                "name": "Procurement Agent",
                "role_ids": [ROLE_ID],
                "description": "Automates purchase order intake.",
                "owner_id": OWNER_ID,
            },
        }
    ]
    data = json.loads(result.output)
    assert data["data"][0]["client_secret"] == "ramp_sec_xyz"


def test_agent_create_wraps_single_role_id_in_array(captured_post):
    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "agent",
            "create",
            "--name",
            "Agent",
            "--role-id",
            ROLE_ID,
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured_post[0]["body"]["role_ids"] == [ROLE_ID]


def test_agent_create_json_assigns_multiple_roles(captured_post):
    second_role = "2b6f19b0-1c1a-4a2a-9c3d-6d3d0a0f6b21"
    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "agent",
            "create",
            "--name",
            "Agent",
            "--json",
            json.dumps({"role_ids": [ROLE_ID, second_role]}),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured_post[0]["body"]["role_ids"] == [ROLE_ID, second_role]


def test_agent_create_json_body_merges_over_flags(captured_post):
    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "agent",
            "create",
            "--name",
            "Flag Name",
            "--json",
            json.dumps({"name": "JSON Name", "role_ids": [ROLE_ID]}),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured_post[0]["body"] == {"name": "JSON Name", "role_ids": [ROLE_ID]}


def test_agent_create_requires_name(captured_post):
    result = CliRunner().invoke(
        cli,
        ["--env", "sandbox", "agent", "create", "--role-id", ROLE_ID],
    )

    assert result.exit_code != 0
    assert "--name" in result.output
    assert captured_post == []


def test_agent_create_requires_role_id(captured_post):
    result = CliRunner().invoke(
        cli,
        ["--env", "sandbox", "agent", "create", "--name", "Agent"],
    )

    assert result.exit_code != 0
    assert "--role-id" in result.output
    assert captured_post == []


def test_agent_create_rejects_invalid_json(captured_post):
    result = CliRunner().invoke(
        cli,
        [
            "--env",
            "sandbox",
            "agent",
            "create",
            "--name",
            "Agent",
            "--role-id",
            ROLE_ID,
            "--json",
            "{not json",
        ],
    )

    assert result.exit_code != 0
    assert "invalid JSON" in result.output
    assert captured_post == []


def test_agent_create_rejects_invalid_role_id_uuid(captured_post):
    result = CliRunner().invoke(
        cli,
        ["--env", "sandbox", "agent", "create", "--name", "Agent", "--role-id", "nope"],
    )

    assert result.exit_code != 0
    assert captured_post == []


def test_agent_create_dry_run_does_not_send(captured_post):
    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "agent",
            "create",
            "--name",
            "Agent",
            "--role-id",
            ROLE_ID,
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured_post == []
    payload = json.loads(result.output)["data"][0]
    assert payload["dry_run"] is True
    assert payload["method"] == "POST"
    assert payload["url"].endswith("/developer/v1/agents")
    assert payload["body"] == {"name": "Agent", "role_ids": [ROLE_ID]}


def test_agent_create_dry_run_snake_case_flag(captured_post):
    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "--env",
            "sandbox",
            "agent",
            "create",
            "--name",
            "Agent",
            "--role-id",
            ROLE_ID,
            "--dry_run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured_post == []


def test_agent_create_human_output_warns_about_show_once_secret(captured_post):
    result = CliRunner().invoke(
        cli,
        [
            "--env",
            "sandbox",
            "--output",
            "table",
            "agent",
            "create",
            "--name",
            "Agent",
            "--role-id",
            ROLE_ID,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "shown only once" in result.output
    assert "ramp agent login" in result.output
