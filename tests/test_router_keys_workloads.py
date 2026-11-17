"""`router keys workloads --harness` passes through Router's coding-routing read."""

import json
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from ramp_cli.client.router_user import RouterUserClient
from ramp_cli.commands import router
from ramp_cli.main import cli
from ramp_cli.router_ui import harness

SECRET = "sk-harness-secret"
BASE_URL = "https://api.example/v1"
ROUTER_ANSWER = {
    "object": "coding_routing",
    "client": "claude-code",
    "model": "router-ab12",
    "applied": {"source": "workload", "id": "p-daily", "name": "Daily"},
    "company_workloads_required": False,
    "options": [
        {
            "kind": "global_settings",
            "name": "Global settings",
            "model": "claude-opus-5-5",
            "current": False,
        },
        {
            "kind": "workload",
            "id": "p-daily",
            "name": "Daily",
            "model": "router-ab12",
            "current": True,
        },
    ],
}


@pytest.fixture
def env(monkeypatch):
    state = {"requests": [], "status": 200, "body": ROUTER_ANSWER}

    def connected(client):
        if client not in ("claude-code", "cowork"):
            raise router.click.UsageError("not connected")
        return harness.Connection(client, Path("settings.json"), SECRET, BASE_URL)

    def get(url, *, params, headers, timeout):
        state["requests"].append((url, params, headers.get("Authorization")))
        return httpx.Response(
            state["status"], json=state["body"], request=httpx.Request("GET", url)
        )

    def no_login(*_args, **_kwargs):
        raise AssertionError("--harness must not use the Router web login")

    monkeypatch.setattr(harness, "connected", connected)
    monkeypatch.setattr(router, "_configured_model", lambda client, path: "router-ab12")
    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(RouterUserClient, "get", no_login)
    return state


def run(*args):
    return CliRunner().invoke(cli, ["--agent", "router", "keys", "workloads", *args])


def test_asks_router_with_the_harness_key_and_model(env):
    result = run("--harness", "claude-code")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"][0]
    assert data == {**ROUTER_ANSWER, "harness": "claude-code"}
    assert env["requests"] == [
        (
            f"{BASE_URL}/client/coding-routing",
            {"client": "claude-code", "model": "router-ab12"},
            f"Bearer {SECRET}",
        )
    ]
    assert SECRET not in result.output


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (404, "can't report coding workloads yet"),
        (401, "API key wasn't accepted"),
        (500, "couldn't report coding workloads"),
    ],
)
def test_router_failures_are_explained(env, status, message):
    env["status"] = status
    result = run("--harness", "claude-code")
    assert result.exit_code != 0
    assert message in result.output


def test_app_managed_harnesses_are_refused(env):
    result = run("--harness", "desktop")
    assert result.exit_code == 2
    assert "inside its app" in result.output
    assert env["requests"] == []
