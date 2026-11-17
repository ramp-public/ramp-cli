"""`router workloads` passes coding workload reads and writes through to Router."""

import json

import pytest
from click.testing import CliRunner

from ramp_cli.client.router_user import RouterUserClient
from ramp_cli.main import cli

PATH = "/client/routing-profiles/coding-workloads"
SAVED = {
    "id": "p-1",
    "slug": "night-owl",
    "model": "night-owl",
    "harness_strategy": "switchyard",
}


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple] = []
    company = {"data": [{"id": "c-1", "slug": "frontier", "model": "org/frontier"}]}

    def get(self, path, **kwargs):
        seen.append(("GET", path, kwargs.get("params")))
        if path == "/admin/benchmarks":
            return {
                "max_selections": 3,
                "data": [{"id": "swe-bench", "name": "SWE-bench"}],
            }
        return company if path == "/client/coding-workloads" else SAVED

    def post(self, path, *, json, **_kwargs):
        seen.append(("POST", path, json))
        return SAVED

    def patch(self, path, *, json, **_kwargs):
        seen.append(("PATCH", path, json))
        return SAVED

    monkeypatch.setattr(RouterUserClient, "get", get)
    monkeypatch.setattr(RouterUserClient, "post", post)
    monkeypatch.setattr(RouterUserClient, "patch", patch)
    return seen


def answer(result) -> dict:
    """Router's answer, from the agent envelope."""
    return json.loads(result.output)["data"][0]


def run(*args):
    return CliRunner().invoke(cli, ["--agent", "router", "workloads", *args])


def test_create_sends_a_switchyard_pair_and_prints_routers_answer(calls):
    result = run("create", "--slug", "night-owl", "--capable-model", "gpt-sol")
    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "POST",
            PATH,
            {"slug": "night-owl", "switchyard_settings": {"capable_model": "gpt-sol"}},
        )
    ]
    assert answer(result) == SAVED


def test_create_sends_a_model_list_in_order(calls):
    result = run("create", "--slug", "fallbacks", "--model", "b", "--model", "a")
    assert result.exit_code == 0, result.output
    assert calls[0][2] == {"slug": "fallbacks", "candidate_models": ["b", "a"]}


def test_edit_passes_any_strategy_through_as_json(calls):
    strategy = {"benchmarks": [{"benchmark_id": "coding_index", "weight": 100}]}
    result = run("edit", "p-1", "--json", json.dumps(strategy))
    assert result.exit_code == 0, result.output
    assert calls == [("PATCH", f"{PATH}/p-1", strategy)]


def test_the_cli_leaves_strategy_validation_to_router(calls):
    # Two strategies at once is Router's to reject; the CLI sends what it was given.
    result = run("edit", "p-1", "--capable-model", "a", "--model", "b")
    assert result.exit_code == 0, result.output
    assert calls[0][2] == {
        "switchyard_settings": {"capable_model": "a"},
        "candidate_models": ["b"],
    }


def test_show_reads_a_personal_workload_or_finds_a_company_one(calls):
    assert answer(run("show", "p-1")) == SAVED
    company = run("show", "c-1", "--company")
    assert company.exit_code == 0, company.output
    assert answer(company)["model"] == "org/frontier"
    assert [call[1] for call in calls] == [f"{PATH}/p-1", "/client/coding-workloads"]
    missing = run("show", "c-404", "--company")
    assert missing.exit_code != 0


@pytest.mark.parametrize("bad", ["../keys", "a/b", "."])
def test_ids_cannot_reach_other_paths(calls, bad):
    assert run("show", bad).exit_code != 0
    assert calls == []


def test_json_must_be_an_object(calls):
    assert run("edit", "p-1", "--json", "[1]").exit_code != 0
    assert run("edit", "p-1", "--json", "{").exit_code != 0
    assert calls == []


def test_benchmarks_passes_routers_list_through(calls):
    result = run("benchmarks")
    assert result.exit_code == 0, result.output
    assert calls == [("GET", "/admin/benchmarks", None)]
    assert answer(result)["max_selections"] == 3


def test_switchyard_models_passes_routers_list_through(calls):
    assert run("switchyard-models").exit_code == 0
    assert run("switchyard-models", "--capable", "openai:gpt-sol").exit_code == 0
    assert calls == [
        ("GET", f"{PATH}/switchyard-models", None),
        ("GET", f"{PATH}/switchyard-models", {"capable_model": "openai:gpt-sol"}),
    ]
