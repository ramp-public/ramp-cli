"""Shared helpers for the caller's Router aliases."""

from __future__ import annotations

import pytest

import ramp_cli.commands.router as router_module
from ramp_cli.commands import router_aliases
from tests.test_router_cursor import _router_model as _model
from tests.test_router_cursor_models import _alias, _Response


def test_list_aliases_reads_names_count_limit_and_owner(monkeypatch):
    seen = {}

    def get(url, headers=None, timeout=None):
        seen["url"] = url
        return _Response(
            200,
            {
                "data": [
                    {"name": "ramp-opus-5", "candidate_models": ["a:claude-opus-5"]},
                    {"name": "mine", "user_id": "u_1", "candidate_models": []},
                    {"no": "name"},
                ],
                "alias_count": 7,
                "alias_limit": 64,
            },
        )

    monkeypatch.setattr(router_aliases.httpx, "get", get)
    listing = router_aliases.list_aliases("https://r.example", {})
    assert seen["url"] == "https://r.example/self-service/model-aliases"
    assert listing is not None
    assert set(listing.aliases) == {"ramp-opus-5", "mine"}
    assert (listing.count, listing.limit, listing.owner) == (7, 64, "u_1")
    listing.record({"name": "ramp-sonnet-5", "candidate_models": ["claude-sonnet-5"]})
    listing.record({"name": "mine"})
    assert listing.count == 8
    assert "ramp-sonnet-5" in listing.aliases


@pytest.mark.parametrize(
    "response",
    [
        _Response(404, {}),
        _Response(200, []),
        _Response(200, {"data": "x"}),
        _Response(200, ValueError("not json")),
    ],
)
def test_list_aliases_is_none_when_router_cant_say(monkeypatch, response):
    monkeypatch.setattr(router_aliases.httpx, "get", lambda *a, **k: response)
    assert router_aliases.list_aliases("https://r.example", {}) is None


@pytest.mark.parametrize(
    ("candidate", "provider_model", "routes"),
    [
        ("claude-haiku-4-5", None, True),
        ("anthropic:claude-haiku-4-5", None, True),
        ("anthropic:claude-haiku-4-5:standard_only", None, True),
        ("anthropic:claude-haiku-4-5-20251001", None, False),
        ("openai:gpt-4o", None, False),
        # A provider model with a colon of its own.
        (
            "bedrock:anthropic.claude-haiku-4-5-20251001-v1:0",
            "anthropic.claude-haiku-4-5-20251001-v1:0",
            True,
        ),
    ],
)
def test_candidate_routes_model(candidate, provider_model, routes):
    model = _model("claude-haiku-4-5", provider_model=provider_model)
    assert router_aliases.candidate_routes_model(candidate, model) is routes


def test_is_variant_reads_rows_and_aliases_alike():
    pinned = _alias("ramp-opus-5-5-high", "claude-opus-5-5", effort="high")
    assert router_aliases.pinned_effort(pinned) == "high"
    assert router_aliases.is_variant(pinned)
    assert not router_aliases.is_variant(_model("claude-opus-5-5"))
    assert router_aliases.is_variant({"name": "x", "reasoning_effort": "xhigh"})
    assert not router_aliases.is_variant({"name": "x", "reasoning_effort": None})


def test_ensure_claude_aliases_uses_and_updates_a_passed_listing(monkeypatch):
    monkeypatch.setattr(
        router_module.httpx,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("listing re-read")),
    )
    posts = []

    def post(url, headers=None, json=None, timeout=None):
        posts.append(json)
        return _Response(201, {"name": json["name"], "id": "al_1"})

    monkeypatch.setattr(router_module.httpx, "post", post)
    listing = router_aliases.AliasListing(
        aliases={"ramp-opus-5": {"name": "ramp-opus-5", "candidate_models": []}},
        count=1,
        limit=64,
    )
    aliases = router_module._ensure_claude_aliases(
        "key",
        router_module.DEFAULT_ROUTER_BASE_URL,
        [_model("claude-opus-5"), _model("claude-sonnet-5")],
        listing=listing,
    )
    assert aliases == {"ramp-sonnet-5": "claude-sonnet-5"}
    assert posts == [{"name": "ramp-sonnet-5", "candidate_models": ["claude-sonnet-5"]}]
    assert listing.count == 2
    assert listing.aliases["ramp-sonnet-5"]["id"] == "al_1"
