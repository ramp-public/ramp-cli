"""Tests for the admin default-model lookup against Router."""

from __future__ import annotations

import httpx
import pytest

from ramp_cli import __version__, admin_defaults

ORIGIN = "https://dash.example"
URL = f"{ORIGIN}/self-service/coding-agent-settings"


def _settings_get(monkeypatch, responder):
    """Patch httpx.get to answer the settings endpoint and record each call."""
    calls = []

    def get(url, *, headers, timeout):
        calls.append((url, headers, timeout))
        return responder(url)

    monkeypatch.setattr(admin_defaults.httpx, "get", get)
    return calls


def _json(status, body):
    return lambda url: httpx.Response(
        status, json=body, request=httpx.Request("GET", url)
    )


def test_reads_the_effective_model_for_the_harness(monkeypatch):
    calls = _settings_get(
        monkeypatch,
        _json(
            200,
            {
                "version": 3,
                "harnesses": {
                    "codex": {
                        "model": "gpt-5.6-sol",
                        "effective_model": " GPT-6 Sol ",
                        "effective_model_source": "workspace",
                        "status": "available",
                    },
                    "pi": {"model": None, "effective_model": None, "status": "unset"},
                },
            },
        ),
    )
    assert admin_defaults.admin_default_model("codex", "secret", ORIGIN + "/") == (
        "GPT-6 Sol"
    )
    # Listed, but nothing set.
    assert admin_defaults.admin_default_model("pi", "secret", ORIGIN) is None
    # Not listed at all: Router does not support a default for it (yet).
    assert admin_defaults.admin_default_model("claude-code", "secret", ORIGIN) is None
    ((url, headers, timeout),) = calls
    assert url == URL
    assert headers == {
        "Authorization": "Bearer secret",
        "X-Gateway-Ramp-Cli-Version": __version__,
        "User-Agent": f"ramp-cli/{__version__}",
    }
    assert timeout == admin_defaults.DEFAULT_TIMEOUT_SECONDS == 2.0


@pytest.mark.parametrize(
    "responder",
    [
        # Older Routers without the endpoint, and revoked business access.
        _json(404, {"detail": "Not Found"}),
        _json(403, {"detail": "Forbidden"}),
        _json(500, {}),
        # Malformed bodies of every shape.
        lambda url: httpx.Response(
            200, text="<html>", request=httpx.Request("GET", url)
        ),
        _json(200, []),
        _json(200, {"harnesses": "codex"}),
        _json(200, {"harnesses": {"codex": "gpt"}}),
        _json(200, {"harnesses": {"codex": {"effective_model": 7}}}),
        _json(200, {"harnesses": {"codex": {"effective_model": ""}}}),
        _json(200, {"harnesses": {"codex": {"model": "set-but-disallowed"}}}),
        # Network trouble.
        lambda url: (_ for _ in ()).throw(httpx.ConnectTimeout("slow")),
        lambda url: (_ for _ in ()).throw(httpx.ConnectError("down")),
    ],
)
def test_fails_open_to_no_default(monkeypatch, responder):
    _settings_get(monkeypatch, responder)
    assert admin_defaults.admin_default_model("codex", "secret", ORIGIN) is None


def test_no_key_means_no_request(monkeypatch):
    calls = _settings_get(monkeypatch, _json(200, {"harnesses": {}}))
    assert admin_defaults.admin_default_model("codex", "", ORIGIN) is None
    assert calls == []


def test_caches_per_key_and_origin_until_the_ttl_passes(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(admin_defaults.time, "monotonic", lambda: now[0])
    answers = iter(
        [
            {"harnesses": {"codex": {"effective_model": "first"}}},
            {"harnesses": {"codex": {"effective_model": "second"}}},
            {"harnesses": {"codex": {"effective_model": "third"}}},
        ]
    )
    calls = _settings_get(monkeypatch, lambda url: _json(200, next(answers))(url))

    assert admin_defaults.admin_default_model("codex", "k1", ORIGIN) == "first"
    # The same connection reuses the response, whichever harness asks.
    assert admin_defaults.admin_default_model("codex", "k1", ORIGIN) == "first"
    assert admin_defaults.admin_default_model("pi", "k1", ORIGIN) is None
    assert admin_defaults.coding_agent_settings("k1", ORIGIN) == {
        "codex": {"effective_model": "first"}
    }
    assert len(calls) == 1
    # Another key, or another origin, is another connection.
    assert admin_defaults.admin_default_model("codex", "k2", ORIGIN) == "second"
    assert len(calls) == 2
    now[0] += admin_defaults.CACHE_TTL_SECONDS - 1
    assert admin_defaults.admin_default_model("codex", "k1", ORIGIN) == "first"
    assert len(calls) == 2
    now[0] += 1
    assert admin_defaults.admin_default_model("codex", "k1", ORIGIN) == "third"
    assert len(calls) == 3


def test_failures_are_cached_too_and_clear_cache_forgets_them(monkeypatch):
    responses = iter(
        [_json(404, {}), _json(200, {"harnesses": {"codex": {"effective_model": "m"}}})]
    )
    calls = _settings_get(monkeypatch, lambda url: next(responses)(url))
    assert admin_defaults.admin_default_model("codex", "k", ORIGIN) is None
    assert admin_defaults.admin_default_model("codex", "k", ORIGIN) is None
    assert len(calls) == 1
    admin_defaults.clear_cache()
    assert admin_defaults.admin_default_model("codex", "k", ORIGIN) == "m"
    assert len(calls) == 2
