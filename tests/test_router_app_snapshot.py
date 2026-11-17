"""`router app snapshot` streams one refresh's reads from one process."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from ramp_cli.client.router_user import RouterUserClient
from ramp_cli.commands import router_user
from ramp_cli.commands.router import (
    DEFAULT_ROUTER_BASE_URL,
    LEGACY_ROUTER_BASE_URL,
    ROUTER_UI_URL_ENV,
    router_base_url,
)
from ramp_cli.main import cli
from ramp_cli.router_ui import harness as harness_module
from ramp_cli.router_ui.service import RouterService

HARNESSES = [
    {"harness": "codex", "installed": True, "connected": True},
    {"harness": "conductor", "installed": True, "connected": False},
    {"harness": "hermes", "installed": False, "connected": False},
]


@pytest.fixture
def reads(monkeypatch):
    seen = []
    session = {
        "origin": "https://router.test",
        "authenticated": True,
        "user": {"id": "u"},
        "access_token_expired": False,
    }
    monkeypatch.setattr(RouterUserClient, "status", lambda self: dict(session))
    monkeypatch.setattr(harness_module, "summaries", lambda: HARNESSES)

    def account(self, days, group_by, client, last=None):
        seen.append(("usage", days, group_by, client))
        if client == "conductor":
            return {
                "session": session,
                "usage_error": "Usage analytics are unavailable.",
            }
        usage = {"days": days, "group_by": group_by}
        return {"session": session, "usage": usage, "billing": None}

    def workloads(harness):
        seen.append(("workloads", harness))
        return {"object": "coding_routing", "harness": harness}

    monkeypatch.setattr(RouterService, "account", account)
    monkeypatch.setattr(router_user, "_harness_workloads", workloads)
    return seen, session


def run(*args):
    result = CliRunner().invoke(
        cli, ["--no-input", "--output", "json", "router", "app", "snapshot", *args]
    )
    assert result.exit_code == 0, result.output
    return [json.loads(line) for line in result.output.splitlines()]


def test_local_reads_come_first_and_done_last(reads):
    lines = run(
        "--usage-days", "1", "--usage-days", "7", "--totals-days", "1",
        "--harness-days", "1", "--workloads",
    )  # fmt: skip
    assert [line["section"] for line in lines[:2]] == ["status", "harnesses"]
    assert lines[-1] == {"section": "done"}
    body = {
        (ln["section"], ln.get("days"), ln.get("harness")): ln for ln in lines[2:-1]
    }
    # Every installed or connected harness's usage; workloads only where connected.
    assert set(body) == {
        ("usage", 1, None),
        ("usage", 7, None),
        ("harness_totals", 1, None),
        ("harness_usage", 1, "codex"),
        ("harness_usage", 1, "conductor"),
        ("workloads", None, "codex"),
    }
    assert body[("harness_totals", 1, None)]["data"]["group_by"] == "harness"
    assert body[("workloads", None, "codex")]["data"]["object"] == "coding_routing"


def test_a_failing_read_reports_itself_and_the_rest_still_arrive(reads):
    lines = run("--harness-days", "1", "--workloads")
    failed = next(line for line in lines if line.get("harness") == "conductor")
    assert failed["error"] == {"message": "Usage analytics are unavailable."}
    assert "data" not in failed
    assert any(line["section"] == "workloads" and "data" in line for line in lines)


def test_signed_out_stops_after_the_local_reads(reads):
    seen, session = reads
    session["authenticated"] = False
    lines = run("--harness-days", "1", "--workloads")
    assert [line["section"] for line in lines] == ["status", "harnesses", "done"]
    assert seen == []


def test_the_same_read_is_asked_once(reads):
    seen, _ = reads
    run("--usage-days", "1", "--usage-days", "1", "--harness-days", "1",
        "--harness-days", "1", "--workloads")  # fmt: skip
    assert seen.count(("usage", 1, "model", None)) == 1
    assert seen.count(("usage", 1, "model", "codex")) == 1
    assert seen.count(("workloads", "codex")) == 1


def test_workload_details_follow_each_list_and_share_one_company_read(
    reads, monkeypatch
):
    seen, _ = reads
    options = [
        {"kind": "global_settings", "name": "Global settings"},
        {"kind": "company_workload", "id": "c-1"},
        {"kind": "company_workload", "id": "c-2"},
        {"kind": "workload", "id": "p-1"},
    ]
    monkeypatch.setattr(
        router_user, "_harness_workloads", lambda h: {"harness": h, "options": options}
    )

    def get(self, path, **_kwargs):
        seen.append(("GET", path))
        if path == "/client/coding-workloads":
            return {"data": [{"id": "c-1", "slug": "a"}, {"id": "c-2", "slug": "b"}]}
        return {"id": path.rsplit("/", 1)[-1]}

    monkeypatch.setattr(RouterUserClient, "get", get)
    monkeypatch.setattr(
        harness_module,
        "connected",
        lambda h: SimpleNamespace(base_url=router_base_url()),
    )
    lines = run("--workloads", "--workload-details")
    details = {
        line["id"]: line["data"] for line in lines if line["section"] == "workload"
    }
    assert details == {"c-1": {"id": "c-1", "slug": "a"}, "c-2": {"id": "c-2", "slug": "b"},
                       "p-1": {"id": "p-1"}}  # fmt: skip
    assert seen.count(("GET", "/client/coding-workloads")) == 1
    assert lines[-1] == {"section": "done"}


def test_workload_details_are_not_read_from_another_router(reads, monkeypatch):
    seen, _ = reads
    options = [
        {"kind": "company_workload", "id": "c-1"},
        {"kind": "workload", "id": "p-1"},
    ]
    monkeypatch.setattr(
        router_user, "_harness_workloads", lambda h: {"harness": h, "options": options}
    )
    monkeypatch.setattr(
        RouterUserClient, "get", lambda self, path, **_: seen.append(("GET", path))
    )
    # The harness is connected to a dev Router; the CLI's login is another one.
    monkeypatch.setattr(
        harness_module,
        "connected",
        lambda h: SimpleNamespace(base_url="https://router.dev.example/v1"),
    )
    lines = run("--workloads", "--workload-details")
    errors = {
        line["id"]: line["error"]["message"]
        for line in lines
        if line["section"] == "workload"
    }
    assert set(errors) == {"c-1", "p-1"}
    assert all("different Router" in message for message in errors.values())
    assert not [entry for entry in seen if entry[0] == "GET"]
    assert lines[-1] == {"section": "done"}


def _detail_lines(monkeypatch, seen, base_url):
    options = [{"kind": "workload", "id": "p-1"}]
    monkeypatch.setattr(
        router_user, "_harness_workloads", lambda h: {"harness": h, "options": options}
    )

    def get(self, path, **_kwargs):
        seen.append(("GET", path, self.origin))
        return {"id": path.rsplit("/", 1)[-1]}

    monkeypatch.setattr(RouterUserClient, "get", get)
    monkeypatch.setattr(
        harness_module, "connected", lambda h: SimpleNamespace(base_url=base_url)
    )
    return [
        line for line in run("--workloads", "--workload-details")
        if line["section"] == "workload"
    ]  # fmt: skip


def test_a_legacy_production_connection_reads_production_details(reads, monkeypatch):
    seen, _ = reads
    monkeypatch.delenv(ROUTER_UI_URL_ENV, raising=False)
    lines = _detail_lines(monkeypatch, seen, LEGACY_ROUTER_BASE_URL)
    assert lines == [{"section": "workload", "id": "p-1", "data": {"id": "p-1"}}]


def test_a_production_connection_is_not_read_through_another_login(reads, monkeypatch):
    seen, _ = reads
    # The login is QA's; a UI URL alone leaves the API-key default at production.
    monkeypatch.setenv(ROUTER_UI_URL_ENV, "https://qa.router.com")
    lines = _detail_lines(monkeypatch, seen, DEFAULT_ROUTER_BASE_URL)
    assert [line["id"] for line in lines] == ["p-1"]
    assert "different Router" in lines[0]["error"]["message"]
    assert not [entry for entry in seen if entry[0] == "GET"]
