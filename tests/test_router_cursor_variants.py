"""Effort-variant names for Cursor (ramp-<model>-<effort>) and their Router aliases."""

from __future__ import annotations

import dataclasses
import json
import sqlite3

import click
import httpx
import pytest
from click.testing import CliRunner

import ramp_cli.commands.router as router_module
from ramp_cli import main as entry
from ramp_cli.commands import cursor, router_repo
from ramp_cli.commands import cursor_variants as cv
from ramp_cli.main import cli

CIPHERTEXT = '{"type":"Buffer","data":[1]}'
ORIGIN = "https://app.router.com"
STRATEGIES = f"{ORIGIN}/strategies"
BASE_URL = "https://api.router.com/v1"
REAL_FETCH_MODELS = router_module._fetch_models
LADDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def _model(
    identifier,
    *,
    efforts=(),
    default="",
    maker=("openai", "OpenAI"),
    display=None,
    provider_model=None,
):
    metadata = {
        "schema_version": 1,
        "request_name": identifier,
        "provider_model": provider_model or identifier,
        "display_name": display or identifier,
        "description": "",
        "listing": {"order": 0},
        "limits": {"context_window": 128000, "max_output_tokens": 16384},
        "capabilities": {
            "modalities": {"input": ["text"]},
            "tools": {"supported": True},
            "reasoning": {
                "efforts": [{"value": value} for value in efforts],
                "default_effort": default,
            },
        },
        "maker": {"id": maker[0], "display_name": maker[1]} if maker else None,
    }
    return router_module.RouterModel(
        id=identifier, metadata=router_module._model_metadata(metadata, identifier)
    )


ANTHROPIC = ("anthropic", "Anthropic")
CLAUDE_LADDER = ("minimal", "low", "medium", "high", "xhigh", "max")
GPT_LADDER = ("none", "low", "medium", "high", "xhigh", "max")


def _catalog():
    return [
        _model(
            "gpt-6-luna", efforts=GPT_LADDER, default="medium", display="GPT-6 Luna"
        ),
        _model(
            "claude-opus-5-5",
            efforts=CLAUDE_LADDER,
            default="medium",
            maker=ANTHROPIC,
            display="Claude Opus 5.5",
        ),
        _model(
            "claude-sonnet-5-5",
            efforts=("low", "medium", "high", "xhigh", "max"),
            default="high",
            maker=ANTHROPIC,
            display="Claude Sonnet 5.5",
        ),
        _model(
            "gemini-4-argon",
            efforts=("low", "medium", "high"),
            default="medium",
            maker=("google", "Google"),
        ),
        _model("grok-4.7", maker=("xai", "xAI")),
        _model(
            "gpt-oss-120b",
            efforts=("low", "medium", "high"),
            default="medium",
            maker=("openai", "OpenAI"),
        ),
        # A GPT model off Router's recommended list: no variants, even on All.
        _model("gpt-5.6-sol", efforts=GPT_LADDER, default="medium"),
        # Reasoning off by default.
        _model(
            "claude-opus-4-8",
            efforts=LADDER,
            default="none",
            maker=ANTHROPIC,
            display="Claude Opus 4.8",
        ),
    ]


COMPACT = {"gpt-6-luna", "claude-opus-5-5", "gemini-4-argon"}
ALL_NAMES = [
    "my-own-model",
    "gpt-6-luna",
    "ramp-opus-5-5",
    "ramp-sonnet-5-5",
    "gemini-4-argon",
    "grok-4.7",
    "gpt-oss-120b",
    "gpt-5.6-sol",
    "ramp-opus-4-8",
]


# A level router#5823 won't pin because the adapter sends it as another.
COLLAPSED = {"claude-sonnet-4-6": ("xhigh",)}


def _levels(models):
    """Router's pinnable levels when every advertised level can be pinned."""
    return cv.EffortLevels(
        pinnable={
            model.metadata.request_name: cv.advertised_efforts(model)
            for model in models
        }
    )


class FakeRouter:
    """Router's self-service alias surface, new or old, for one key owner."""

    def __init__(
        self,
        *,
        effort=True,
        delete=True,
        levels=True,
        limit=64,
        ceiling=None,
        owner="user-1",
        aliases=(),
        models=None,
        origin=ORIGIN,
        managed=False,
        ceiling_after_levels=None,
    ):
        self.origin = origin
        self.ceiling_after_levels = ceiling_after_levels
        # A company-issued key: Router refuses alias writes with it.
        self.managed = managed
        # Aliases a routing profile uses as its benchmark_alias.
        self.referenced: set[str] = set()
        self.effort = effort
        self.delete_supported = delete
        self.levels_supported = levels
        self.limit = limit
        self.ceiling = ceiling
        self.owner = owner
        self.models = models or _catalog
        self.aliases = {alias["name"]: {"user_id": owner, **alias} for alias in aliases}
        self.posts: list[dict] = []
        self.deletes: list[str] = []
        self.gets = 0
        self.requests: list[str] = []

    @staticmethod
    def _response(status, payload=None, url=ORIGIN):
        return httpx.Response(
            status,
            json=payload if payload is not None else {},
            request=httpx.Request("GET", url),
        )

    def _error(self, status, code):
        return self._response(status, {"error": {"code": code, "message": code}})

    @staticmethod
    def expand(name):
        """A bare name's provider candidates, as POST stores them."""
        if name.startswith("claude"):
            return [f"anthropic:{name}"]
        return [f"openai:{name}", f"bedrock:{name}"]

    def _advertised(self, name):
        model = next(
            (model for model in self.models() if model.metadata.request_name == name),
            None,
        )
        return [effort for effort, _ in model.metadata.efforts] if model else None

    def _pinnable(self, name):
        # router#5823 leaves out a level the adapter would send as a lower one.
        advertised = [
            effort
            for effort in self._advertised(name) or []
            if effort not in COLLAPSED.get(name, ())
        ]
        if self.ceiling is None:
            return advertised
        top = LADDER.index(self.ceiling)
        return [effort for effort in advertised if LADDER.index(effort) <= top]

    def get(self, url, headers=None, timeout=None, params=None, **_kwargs):
        self.requests.append(url)
        if url == f"{self.origin}/self-service/model-aliases/reasoning-efforts":
            names = [value for key, value in params if key == "model"]
            if not self.levels_supported:
                return self._error(404, "not_found")
            if any(self._advertised(name) is None for name in names):
                return self._error(422, "invalid_model_alias_candidates")
            response = self._response(
                200,
                {
                    "data": [
                        {
                            "model": name,
                            "candidate_models": self.expand(name),
                            "advertised_efforts": self._advertised(name),
                            "max_reasoning_effort": self.ceiling,
                            "pinnable_efforts": self._pinnable(name),
                        }
                        for name in names
                    ]
                },
            )
            if self.ceiling_after_levels:
                self.ceiling, self.ceiling_after_levels = (
                    self.ceiling_after_levels,
                    None,
                )
            return response
        assert url == f"{self.origin}/self-service/model-aliases", url
        self.gets += 1
        data = []
        for alias in self.aliases.values():
            row = {
                "name": alias["name"],
                "candidate_models": alias["candidate_models"],
                "user_id": alias["user_id"],
            }
            if self.effort:
                row["reasoning_effort"] = alias.get("reasoning_effort")
                row["origin"] = alias.get("origin")
            data.append(row)
        return self._response(
            200,
            {"alias_count": len(data), "alias_limit": self.limit, "data": data},
        )

    def post(self, url, headers=None, json=None, timeout=None, **_kwargs):
        self.requests.append(url)
        assert url == f"{self.origin}/self-service/model-aliases", url
        self.posts.append(json)
        if self.managed:
            return self._error(409, "api_key_managed_by_workspace_admin")
        if "reasoning_effort" in json and not self.effort:
            # FastAPI's extra="forbid" through Router's validation handler.
            return self._error(422, "invalid_request")
        if json["name"] in self.aliases:
            return self._error(409, "model_alias_name_conflict")
        if len(self.aliases) >= self.limit:
            return self._error(422, "model_alias_limit_exceeded")
        effort = json.get("reasoning_effort")
        if effort is not None:
            candidate = json["candidate_models"][0]
            if effort not in (self._advertised(candidate) or []) or effort in (
                COLLAPSED.get(candidate, ())
            ):
                return self._error(422, "invalid_model_alias_reasoning_effort")
            if effort not in self._pinnable(json["candidate_models"][0]):
                return self._error(422, "model_alias_reasoning_effort_exceeds_limit")
        self.aliases[json["name"]] = {
            "name": json["name"],
            "user_id": self.owner,
            "candidate_models": self.expand(json["candidate_models"][0]),
            "reasoning_effort": effort,
            "origin": "self_service" if self.effort else None,
        }
        return self._response(201, self.aliases[json["name"]])

    def delete(self, url, headers=None, timeout=None, **_kwargs):
        self.requests.append(url)
        name = url.rsplit("/", 1)[1]
        self.deletes.append(name)
        if not self.delete_supported:
            return self._error(405, "http_error")
        if self.managed:
            return self._error(409, "api_key_managed_by_workspace_admin")
        alias = self.aliases.get(name)
        if alias is None:
            return self._error(404, "model_alias_not_found")
        if alias.get("origin") != "self_service":
            return self._error(409, "model_alias_not_self_service")
        if name in self.referenced:
            return self._error(409, "model_alias_referenced_by_routing_profile")
        del self.aliases[name]
        return self._response(204)

    def names(self):
        return set(self.aliases)

    def edit_on_dashboard(self, name, models=None):
        """A dashboard save, as router#5823 does it: it clears the
        self-service stamp; the pin stays while the models do, and is cleared
        (never moved to a nearby level) when they change. Returns Router's
        note, if any."""
        alias = self.aliases[name]
        alias["origin"] = None
        if models is None or sorted(models) == sorted(alias["candidate_models"]):
            return None
        alias.update(candidate_models=list(models), reasoning_effort=None)
        return "The models changed, so the pinned reasoning effort was cleared."


def _variant(name, model, effort, *, origin="self_service", provider="anthropic"):
    return {
        "name": name,
        "candidate_models": [f"{provider}:{model}"],
        "reasoning_effort": effort,
        "origin": origin,
    }


@pytest.fixture
def router(monkeypatch):
    return _fake(monkeypatch)


def _fake(monkeypatch, *aliases, **kwargs):
    fake = FakeRouter(aliases=aliases, **kwargs)
    _use_router(monkeypatch, fake)
    return fake


def _use_router(monkeypatch, fake):
    monkeypatch.setattr(httpx, "get", fake.get)
    monkeypatch.setattr(httpx, "post", fake.post)
    monkeypatch.setattr(httpx, "delete", fake.delete)


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    catalog = _catalog()

    def fetch(api_key, **kwargs):
        if kwargs.get("model_view") == "compact":
            return [model for model in catalog if model.id in COMPACT]
        return catalog

    monkeypatch.setattr(router_module, "_fetch_models", fetch)
    monkeypatch.setattr(router_module, "_copy_to_clipboard", lambda text: True)
    monkeypatch.setattr(router_module, "_clipboard_available", lambda: True)
    monkeypatch.setattr(router_module, "_fetch_configure_summary", lambda *a, **k: None)


@pytest.fixture
def cursor_db(monkeypatch):
    """A seeded Cursor state database that setup can write, as on macOS."""
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(
        cursor, "encrypt_with_cursor", lambda key, prompt=True: CIPHERTEXT
    )
    path = cursor.state_db_path()
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
        db.execute(
            "INSERT INTO ItemTable VALUES (?, ?)",
            (
                cursor._SETTINGS_KEY,
                json.dumps({"aiSettings": {"userAddedModels": ["my-own-model"]}}),
            ),
        )
    return path


def _cursor_list(path):
    with sqlite3.connect(path) as db:
        (value,) = db.execute(
            "SELECT value FROM ItemTable WHERE key = ?", (cursor._SETTINGS_KEY,)
        ).fetchone()
    return json.loads(value)["aiSettings"]["userAddedModels"]


def _enabled(path):
    with sqlite3.connect(path) as db:
        (value,) = db.execute(
            "SELECT value FROM ItemTable WHERE key = ?", (cursor._SETTINGS_KEY,)
        ).fetchone()
    return json.loads(value)["aiSettings"]["modelOverrideEnabled"]


def _configure(*args, human=True, no_input=True):
    flags = ["--human" if human else "--agent"]
    if no_input:
        flags.append("--no-input")
    return CliRunner().invoke(
        cli, [*flags, "router", "configure", "cursor", "--api-key", "k", *args]
    )


def _ok(*args, **kwargs):
    """A configure run that must succeed."""
    result = _configure(*args, **kwargs)
    assert result.exit_code == 0, result.output
    return result


def _cursor_command(*args, human=True, no_input=True):
    flags = ["--human" if human else "--agent"]
    if no_input:
        flags.append("--no-input")
    return CliRunner().invoke(
        cli, [*flags, "router", "cursor", "--api-key", "k", *args]
    )


def _prune(*args, human=True, no_input=True):
    flags = ["--human" if human else "--agent"]
    if no_input:
        flags.append("--no-input")
    return CliRunner().invoke(
        cli, [*flags, "router", "cursor", "prune-variants", "--api-key", "k", *args]
    )


def _unconfigure():
    return CliRunner().invoke(cli, ["--human", "router", "unconfigure", "cursor"])


def _flat(output):
    return " ".join(output.replace("│", " ").split())


def _variant_aliases(fake):
    return {
        name: alias["reasoning_effort"]
        for name, alias in fake.aliases.items()
        if alias.get("reasoning_effort")
    }


def _by_id():
    return {model.id: model for model in _catalog()}


def _entry(model, effort, created=True):
    return {"model": model, "effort": effort, "created": created}


def _serve(monkeypatch, catalog, compact=COMPACT, *, compact_fails=False):
    """What Router's /v1/models returns, full and recommended."""

    def fetch(api_key, **kwargs):
        if kwargs.get("model_view") == "compact":
            if compact_fails:
                raise click.ClickException("Ramp Router is unavailable.")
            return [model for model in catalog if model.id in compact]
        return catalog

    monkeypatch.setattr(router_module, "_fetch_models", fetch)


def _alias_row(name, model):
    """Router listing the caller's own alias as a plain model row."""
    plain = _model(name, maker=None)
    return dataclasses.replace(
        plain, metadata=dataclasses.replace(plain.metadata, alias_candidates=(model,))
    )


def _router_fallback(monkeypatch, model):
    monkeypatch.setattr(
        "ramp_cli.admin_defaults.fallback_model", lambda *_args, **_kwargs: model
    )


INTERNAL_ORIGIN = "https://internal.router.com"
INTERNAL_BASE_URL = "https://internal-api.router.com/v1"


def _route(monkeypatch, *fakes):
    """Send each request to the fake Router whose origin it names."""
    stray: list[str] = []

    def pick(url):
        fake = next((fake for fake in fakes if url.startswith(fake.origin)), None)
        if fake is None:
            stray.append(url)
            raise httpx.ConnectError("no such Router")
        return fake

    monkeypatch.setattr(httpx, "get", lambda url, **kw: pick(url).get(url, **kw))
    monkeypatch.setattr(httpx, "post", lambda url, **kw: pick(url).post(url, **kw))
    monkeypatch.setattr(httpx, "delete", lambda url, **kw: pick(url).delete(url, **kw))
    return stray


def _main(monkeypatch, capsys, *args) -> tuple[int, str]:
    """Run configure cursor through the shipped entry point, which runs the
    CLI outside Click's standalone mode (a Click exit code would be returned
    and dropped there); (exit code, output)."""
    monkeypatch.setattr(entry, "check_for_update", lambda: None)
    monkeypatch.setattr(
        "sys.argv",
        ["ramp", "--human", "--no-input", "router", "configure", "cursor"]
        + ["--api-key", "k", *args],
    )
    try:
        entry.main()
        code = 0
    except SystemExit as exited:
        code = exited.code or 0
    captured = capsys.readouterr()
    return code, _flat(captured.out + captured.err)


class _Answers:
    """Stand-in for questionary prompts, answering in order.

    The model-list question keeps conftest's answer (the saved choice, or
    the recommended list), so only the effort questions are scripted.
    """

    def __init__(self, monkeypatch, *answers):
        self.answers = list(answers)
        self.asked: list[tuple] = []
        monkeypatch.setattr(router_module, "_can_prompt", lambda *_a: True)
        for kind in ("select", "checkbox", "confirm"):
            monkeypatch.setattr(router_module.questionary, kind, self._prompt(kind))

    def _prompt(self, kind):
        def prompt(message, choices=None, **kwargs):
            self.asked.append((kind, message, choices, kwargs))
            answer = self.answers.pop(0)

            class _Question:
                def ask(self):
                    return answer

            return _Question()

        return prompt

    def checked(self, index):
        return {c.value for c in self.asked[index][2] if c.checked}


# === Naming and levels ===


def test_variant_names_and_the_levels_offered():
    by_id = _by_id()
    opus = by_id["claude-opus-5-5"]
    assert cv.variant_name(opus, "high") == "ramp-opus-5-5-high"
    # Dots are valid in Router alias names, so GPT ids keep them.
    assert cv.variant_name(by_id["gpt-5.6-sol"], "xhigh") == "ramp-gpt-5.6-sol-xhigh"
    # Above the effective default (a none default is a real level), and only
    # high or xhigh, whatever the model advertises.
    for name, offered in (
        ("claude-opus-5-5", ("high", "xhigh")),
        ("gpt-6-luna", ("high", "xhigh")),
        ("claude-sonnet-5-5", ("xhigh",)),
        ("claude-opus-4-8", ("high", "xhigh")),
    ):
        model = by_id[name]
        assert cv.offered_efforts(model, cv.advertised_efforts(model)) == offered
    for model in _catalog():
        assert set(cv.offered_efforts(model, LADDER)) <= {"high", "xhigh"}
    # Router's pinnable levels (the company limit) narrow it further.
    assert cv.offered_efforts(opus, ("minimal", "low", "medium", "high")) == ("high",)
    # One accepted level (here behind a mislabelled default) gets nothing.
    pro = _model("gpt-5-pro", efforts=("high",), default="none")
    assert cv.effective_default(pro) == "high"
    assert not cv.may_have_variants(pro)
    assert cv.variant_offers([pro], {"gpt-5-pro"}, _levels([pro])) == []
    # GPT variants only for Router's recommended models.
    offers = cv.variant_offers(_catalog(), COMPACT, _levels(_catalog()))
    assert [offer.name for offer in offers] == [
        "ramp-gpt-6-luna-high",
        "ramp-gpt-6-luna-xhigh",
        "ramp-opus-5-5-high",
        "ramp-opus-5-5-xhigh",
        "ramp-sonnet-5-5-xhigh",
        "ramp-opus-4-8-high",
        "ramp-opus-4-8-xhigh",
    ]
    assert not any(
        offer.name.startswith("ramp-gpt")
        for offer in cv.variant_offers(_catalog(), None, _levels(_catalog()))
    )


def test_pinned_alias_rows_are_read_as_pinned(monkeypatch):
    def row(name, **extra):
        return {
            "id": name,
            "router": {
                "schema_version": 1,
                "request_name": name,
                "limits": {"context_window": 1000, "max_output_tokens": 100},
            },
            **extra,
        }

    rows = [
        row("ramp-opus-5-5-high", router_alias={"reasoning_effort": "high"}),
        row("gpt-6-luna"),
    ]
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: httpx.Response(
            200, json={"data": rows}, request=httpx.Request("GET", ORIGIN)
        ),
    )
    models = REAL_FETCH_MODELS("k", base_url=f"{ORIGIN}/v1")
    assert [model.metadata.pinned_effort for model in models] == ["high", ""]


def test_variant_rows_never_join_the_base_list(router, cursor_db, monkeypatch):
    catalog = _catalog()
    pinned = dataclasses.replace(
        catalog[1].metadata,
        request_name="ramp-opus-5-5-xhigh",
        alias_candidates=(catalog[1].id,),
        pinned_effort="xhigh",
    )
    # A pinned alias row, and a name this machine set up still listed while
    # Router catches up.
    rows = [
        *catalog,
        router_module.RouterModel(id="ramp-opus-5-5-xhigh", metadata=pinned),
        _model("ramp-gpt-6-luna-high"),
    ]
    cv.write_receipt(
        {
            "accounts": [
                {
                    "origin": ORIGIN,
                    "owner": "user-1",
                    "variants": {"ramp-gpt-6-luna-high": _entry("gpt-6-luna", "high")},
                }
            ]
        }
    )
    _serve(monkeypatch, rows)
    _ok()
    names = _cursor_list(cursor_db)
    assert "ramp-opus-5-5-xhigh" not in names
    assert "ramp-gpt-6-luna-high" not in names


# === Flags ===


def test_flags_create_variants_after_their_base_names(router, cursor_db):
    _ok(
        "--effort-variants",
        "ramp-opus-5-5=high,xhigh",
        "--effort-variants",
        "gpt-6-luna=high",
    )
    assert _variant_aliases(router) == {
        "ramp-opus-5-5-high": "high",
        "ramp-opus-5-5-xhigh": "xhigh",
        "ramp-gpt-6-luna-high": "high",
    }
    post = next(p for p in router.posts if p["name"] == "ramp-opus-5-5-high")
    assert post == {
        "name": "ramp-opus-5-5-high",
        "candidate_models": ["claude-opus-5-5"],
        "reasoning_effort": "high",
    }
    names = _cursor_list(cursor_db)
    assert names.index("ramp-opus-5-5-high") == names.index("ramp-opus-5-5") + 1
    assert names.index("ramp-gpt-6-luna-high") == names.index("gpt-6-luna") + 1
    # The receipt names this Router and key owner.
    assert cv.read_receipt()["accounts"] == [
        {
            "origin": ORIGIN,
            "owner": "user-1",
            "base_url": BASE_URL,
            "variants": {
                "ramp-gpt-6-luna-high": _entry("gpt-6-luna", "high"),
                "ramp-opus-5-5-high": _entry("claude-opus-5-5", "high"),
                "ramp-opus-5-5-xhigh": _entry("claude-opus-5-5", "xhigh"),
            },
        }
    ]


@pytest.mark.parametrize("token", ["claude-opus-5-5", "ramp-opus-5-5", "opus-5-5"])
def test_flag_accepts_every_spelling_of_a_claude_model(router, cursor_db, token):
    _ok("--effort-variants", f"{token}=xhigh")
    assert _variant_aliases(router) == {"ramp-opus-5-5-xhigh": "xhigh"}


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("opus-5-5=max", "variants pin only high or xhigh, not max"),
        ("opus-5-5=medium", "variants pin only high or xhigh, not medium"),
        ("opus-4-8=minimal,high", "variants pin only high or xhigh, not minimal"),
        ("gemini-4-argon=high", "is not a Claude or GPT model"),
        ("opus-9=high", "'opus-9' is not a Router model"),
        ("opus-5-5", "MODEL=LEVEL"),
        ("=high", "MODEL=LEVEL"),
    ],
)
def test_malformed_flags_and_unknown_models_exit_2(router, cursor_db, value, message):
    result = _configure("--effort-variants", value)
    assert result.exit_code == 2, result.output
    assert message in _flat(result.output)
    # Nothing was created or written before the flag was rejected.
    assert router.posts == []
    assert not cursor.has_receipt()


def test_flags_conflict_and_require_cursor(router):
    both = _configure("--effort-variants", "opus-5-5=high", "--no-effort-variants")
    assert both.exit_code == 2
    assert "cannot be combined" in both.output
    elsewhere = CliRunner().invoke(
        cli,
        ["--no-input", "router", "configure", "codex", "--api-key", "k"]
        + ["--cursor-models", "all"],
    )
    assert elsewhere.exit_code == 2
    assert "apply to Cursor" in elsewhere.output


def _first_run(*args):
    def setup(monkeypatch, capsys, fake):
        code, output = _main(monkeypatch, capsys, *args)
        assert code == 0, output

    return setup


def _lower_ceiling(monkeypatch, capsys, fake):
    fake.ceiling = "high"


def _serve_sonnet_4_6(monkeypatch, capsys, fake):
    sonnet_4_6 = _model(
        "claude-sonnet-4-6",
        efforts=("low", "medium", "high", "xhigh", "max"),
        default="medium",
        maker=ANTHROPIC,
        display="Claude Sonnet 4.6",
    )
    catalog = [*_catalog(), sonnet_4_6]
    _serve(monkeypatch, catalog)
    fake.models = lambda: catalog


def _suggest_sol(monkeypatch, capsys, fake):
    _router_fallback(monkeypatch, "gpt-5.6-sol")


def _deleted_then_lowered(monkeypatch, capsys, fake):
    _first_run("--effort-variants", "opus-5-5=xhigh")(monkeypatch, capsys, fake)
    del fake.aliases["ramp-opus-5-5-xhigh"]
    fake.ceiling = "high"


@pytest.mark.parametrize(
    ("setup", "args", "name", "reason", "deletes"),
    [
        pytest.param(
            None,
            ("--effort-variants", "sonnet-5-5=high"),
            "ramp-sonnet-5-5-high",
            "Claude Sonnet 5.5's default effort is high",
            [],
            id="at-the-default",
        ),
        pytest.param(
            None,
            ("--effort-variants", "gpt-5.6-sol=high"),
            "ramp-gpt-5.6-sol-high",
            "gpt-5.6-sol isn't on Router's recommended list",
            [],
            id="gpt-off-the-recommended-list",
        ),
        pytest.param(
            _lower_ceiling,
            ("--effort-variants", "opus-5-5=xhigh"),
            "ramp-opus-5-5-xhigh",
            "your company's effort limit is high",
            [],
            id="above-the-company-limit",
        ),
        pytest.param(
            None,
            ("--cursor-models", "compact", "--effort-variants", "sonnet-5-5=xhigh"),
            "ramp-sonnet-5-5-xhigh",
            "Claude Sonnet 5.5 isn't on Router's recommended list",
            [],
            id="outside-the-model-list",
        ),
        pytest.param(
            # Router leaves xhigh out of pinnable_efforts: it would go out as
            # high.
            _serve_sonnet_4_6,
            ("--cursor-models", "all", "--effort-variants", "sonnet-4-6=xhigh"),
            "ramp-sonnet-4-6-xhigh",
            "Router can't pin xhigh on Claude Sonnet 4.6",
            [],
            id="a-level-router-would-collapse",
        ),
        pytest.param(
            # Listed as the suggested model, but not recommended.
            _suggest_sol,
            ("--cursor-models", "compact", "--effort-variants", "gpt-5.6-sol=high"),
            "ramp-gpt-5.6-sol-high",
            "gpt-5.6-sol isn't on Router's recommended list",
            [],
            id="suggested-gpt-off-the-recommended-list",
        ),
        pytest.param(
            # This machine listed it, but it is gone from Router.
            _deleted_then_lowered,
            ("--effort-variants", "opus-5-5=xhigh"),
            "ramp-opus-5-5-xhigh",
            "your company's effort limit is high",
            [],
            id="deleted-on-router-and-above-the-limit",
        ),
        pytest.param(
            # Recommended takes Sonnet's base name out of Cursor; its named
            # variant goes with it.
            _first_run(
                "--cursor-models", "all", "--effort-variants", "sonnet-5-5=xhigh"
            ),
            ("--cursor-models", "compact", "--effort-variants", "sonnet-5-5=xhigh"),
            "ramp-sonnet-5-5-xhigh",
            "Claude Sonnet 5.5 isn't on Router's recommended list",
            ["ramp-sonnet-5-5-xhigh"],
            id="its-base-name-leaves-cursor",
        ),
    ],
)
def test_a_flag_level_setup_does_not_offer_is_skipped_and_exits_1(
    monkeypatch, capsys, cursor_db, setup, args, name, reason, deletes
):
    # Through the shipped entry point: what Router offers changes, so the
    # rest of setup goes ahead and only the exit code says it wasn't all done.
    fake = _fake(monkeypatch)
    if setup:
        setup(monkeypatch, capsys, fake)
    posts = len(fake.posts)
    code, output = _main(monkeypatch, capsys, *args)
    assert code == 1, output
    assert f"Skipped {name}: {reason}." in output
    assert "not every effort variant asked for could be set up" in output
    assert not any(post["name"] == name for post in fake.posts[posts:])
    assert fake.deletes == deletes
    assert name not in _cursor_list(cursor_db)
    assert "ramp-opus-5-5" in _cursor_list(cursor_db)


def test_router_refusing_a_level_on_create_exits_1(monkeypatch, cursor_db):
    # The company limit is lowered between Router's answer and the create.
    fake = _fake(monkeypatch, ceiling_after_levels="high")
    result = _configure("--effort-variants", "opus-5-5=high,xhigh", human=False)
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)["data"][0]
    assert payload["effort_variants"]["over_ceiling"] == ["ramp-opus-5-5-xhigh"]
    assert _variant_aliases(fake) == {"ramp-opus-5-5-high": "high"}
    assert "ramp-opus-5-5-high" in _cursor_list(cursor_db)
    assert "ramp-opus-5-5-xhigh" not in _cursor_list(cursor_db)


def test_only_variants_this_machine_made_are_deleted(monkeypatch, cursor_db):
    # Made by setup on another machine.
    fake = _fake(
        monkeypatch, _variant("ramp-opus-5-5-xhigh", "claude-opus-5-5", "xhigh")
    )
    _ok("--effort-variants", "opus-5-5=high")
    assert "ramp-opus-5-5-xhigh" not in _cursor_list(cursor_db)
    # Picking it here reuses it without taking ownership.
    _ok("--effort-variants", "opus-5-5=high,xhigh")
    assert "ramp-opus-5-5-xhigh" in _cursor_list(cursor_db)
    _ok("--no-effort-variants")
    assert fake.deletes == ["ramp-opus-5-5-high"]
    # Base aliases stay; both names leave Cursor's list.
    assert {"ramp-opus-5-5-xhigh", "ramp-opus-5-5"} <= fake.names()
    assert not {"ramp-opus-5-5-high", "ramp-opus-5-5-xhigh"} & set(
        _cursor_list(cursor_db)
    )
    assert not cv.receipt_path().exists()


# === Unattended runs and reconciling ===


def test_unattended_runs_add_none_then_keep_the_saved_names(router, cursor_db):
    first = _ok(human=False)
    payload = json.loads(first.output)["data"][0]
    assert payload["effort_variants"] == {"names": [], "created": [], "removed": []}
    assert _cursor_list(cursor_db) == ALL_NAMES
    assert not cv.receipt_path().exists()
    # Router stores gpt-6-luna as an OpenAI and a Bedrock candidate; the
    # expanded alias still matches, so a rerun neither recreates nor deletes it.
    _ok("--cursor-models", "compact", "--effort-variants", "gpt-6-luna=high")
    posts = len(router.posts)
    _ok()
    assert _variant_aliases(router) == {"ramp-gpt-6-luna-high": "high"}
    assert len(router.posts) == posts
    assert router.deletes == []
    assert "ramp-gpt-6-luna-high" in _cursor_list(cursor_db)
    assert "grok-4.7" not in _cursor_list(cursor_db)


def test_reconcile_creates_and_deletes_within_the_alias_limit(router, cursor_db):
    _ok(
        "--effort-variants",
        "opus-5-5=high,xhigh",
        "--effort-variants",
        "sonnet-5-5=xhigh",
    )
    # Three base aliases and three variants: the swap fits only because the
    # deletes free room.
    router.limit = 6
    router.posts.clear()
    _ok("--effort-variants", "opus-5-5=high", "--effort-variants", "gpt-6-luna=xhigh")
    assert sorted(router.deletes) == ["ramp-opus-5-5-xhigh", "ramp-sonnet-5-5-xhigh"]
    assert [post["name"] for post in router.posts] == ["ramp-gpt-6-luna-xhigh"]
    names = _cursor_list(cursor_db)
    assert "ramp-opus-5-5-xhigh" not in names
    assert "ramp-gpt-6-luna-xhigh" in names


def _drop_opus_4_8(monkeypatch):
    _serve(monkeypatch, [m for m in _catalog() if m.id != "claude-opus-4-8"])


@pytest.mark.parametrize(
    ("first", "change", "args", "gone"),
    [
        # Recommended takes Sonnet's base name out of Cursor.
        ("sonnet-5-5=xhigh", None, ("--cursor-models", "compact"), "sonnet-5-5-xhigh"),
        # Opus 4.8 leaves Router's catalog.
        ("opus-4-8=high", _drop_opus_4_8, (), "opus-4-8-high"),
    ],
    ids=["base-name-leaves-cursor", "model-leaves-the-catalog"],
)
def test_a_variant_follows_its_model_out(
    monkeypatch, router, cursor_db, first, change, args, gone
):
    _ok("--cursor-models", "all", "--effort-variants", "opus-5-5=high")
    _ok("--effort-variants", "opus-5-5=high", "--effort-variants", first)
    if change:
        change(monkeypatch)
    _ok(*args)
    assert router.deletes == [f"ramp-{gone}"]
    assert f"ramp-{gone}" not in _cursor_list(cursor_db)
    assert "ramp-opus-5-5-high" in _cursor_list(cursor_db)
    # Base aliases stay on Router, as before.
    assert "ramp-sonnet-5-5" in router.names()


def _off_recommended(model):
    def change(monkeypatch, router):
        _serve(monkeypatch, _catalog(), COMPACT - {model})

    return change


def _recommended_fails(monkeypatch, router):
    _serve(monkeypatch, _catalog(), compact_fails=True)


def _ceiling_high(monkeypatch, router):
    router.ceiling = "high"


def _default_raised(monkeypatch, router):
    opus = _model(
        "claude-opus-5-5",
        efforts=CLAUDE_LADDER,
        default="high",
        maker=ANTHROPIC,
        display="Claude Opus 5.5",
    )
    _serve(
        monkeypatch,
        [opus if model.id == opus.id else model for model in _catalog()],
    )


@pytest.mark.parametrize(
    ("view", "flag", "change", "reason"),
    [
        (
            "compact",
            "opus-5-5=xhigh",
            _off_recommended("claude-opus-5-5"),
            cv.OFF_RECOMMENDED,
        ),
        ("all", "gpt-6-luna=xhigh", _off_recommended("gpt-6-luna"), cv.OFF_RECOMMENDED),
        ("all", "gpt-6-luna=high", _recommended_fails, cv.RECOMMENDED_UNAVAILABLE),
        ("all", "opus-5-5=xhigh", _ceiling_high, cv.ABOVE_LIMIT),
        ("all", "opus-5-5=high", _default_raised, cv.DEFAULT_RAISED),
        # Listed all models instead; Opus is still offered there.
        ("compact", "opus-5-5=high", _recommended_fails, None),
    ],
    ids=[
        "off-recommended",
        "gpt-off-recommended",
        "recommended-list-unreadable",
        "company-limit-lowered",
        "default-raised",
        "recommended-list-unreadable-on-recommended",
    ],
)
def test_a_listed_variant_setup_no_longer_offers_is_kept_with_a_reason(
    monkeypatch, router, cursor_db, view, flag, change, reason
):
    _ok("--cursor-models", view, "--effort-variants", flag)
    name = next(iter(_variant_aliases(router)))
    change(monkeypatch, router)
    result = _ok(human=False)
    variants = json.loads(result.output)["data"][0]["effort_variants"]
    assert router.deletes == []
    assert name in variants["names"]
    assert name in _cursor_list(cursor_db)
    assert variants.get("kept") == (
        [{"name": name, "reason": reason}] if reason else None
    )


def test_a_kept_variant_stays_until_it_is_unticked(monkeypatch, router, cursor_db):
    _ok("--cursor-models", "compact", "--effort-variants", "opus-5-5=xhigh")
    # Opus leaves the recommended list; the user keeps its base name.
    _off_recommended("claude-opus-5-5")(monkeypatch, router)
    monkeypatch.setattr(router_module, "_confirm_cursor_removals", lambda *a: False)
    # Not offered now, but this machine lists it, so a flag naming it keeps it.
    kept = _ok("--effort-variants", "opus-5-5=xhigh")
    assert "without it in --effort-variants" in _flat(kept.output)
    # The picker lists it ticked. At the alias limit, an added name changes
    # nothing, and the kept one is neither deleted nor forgotten.
    router.limit = len(router.aliases)
    answers = _Answers(
        monkeypatch, True, ["ramp-gpt-6-luna-high", "ramp-opus-5-5-xhigh"]
    )
    result = _ok(no_input=False)
    choice = next(c for c in answers.asked[1][2] if c.value == "ramp-opus-5-5-xhigh")
    assert choice.checked
    assert choice.title.endswith("(off the recommended list; untick to remove)")
    assert "No effort variants were changed." in _flat(result.output)
    assert "ramp-gpt-6-luna-high" not in router.names()
    assert router.deletes == []
    assert "ramp-opus-5-5-xhigh" in _cursor_list(cursor_db)
    # Unticking it removes it.
    _Answers(monkeypatch, True, [])
    _ok(no_input=False)
    assert router.deletes == ["ramp-opus-5-5-xhigh"]


def test_a_flag_set_reports_what_it_removed_unlisted_kept_and_skipped(
    monkeypatch, cursor_db
):
    # Sonnet's xhigh variant is another machine's; this one reuses it.
    fake = _fake(
        monkeypatch, _variant("ramp-sonnet-5-5-xhigh", "claude-sonnet-5-5", "xhigh")
    )
    _ok(
        "--cursor-models",
        "all",
        "--effort-variants",
        "opus-5-5=high,xhigh",
        "--effort-variants",
        "sonnet-5-5=xhigh",
    )
    # Opus's xhigh is above the new company limit, but named, so kept.
    fake.ceiling = "high"
    result = _configure(
        "--effort-variants",
        "opus-5-5=xhigh",
        "--effort-variants",
        "sonnet-5-5=high",
        human=False,
    )
    assert result.exit_code == 1, result.output
    variants = json.loads(result.output)["data"][0]["effort_variants"]
    assert variants["names"] == ["ramp-opus-5-5-xhigh"]
    assert variants["created"] == []
    assert variants["removed"] == ["ramp-opus-5-5-high"]
    assert variants["unlisted"] == [
        {"name": "ramp-sonnet-5-5-xhigh", "reason": cv.UNLISTED_ANOTHER_MACHINE}
    ]
    assert variants["kept"] == [
        {"name": "ramp-opus-5-5-xhigh", "reason": cv.ABOVE_LIMIT}
    ]
    assert [row["name"] for row in variants["skipped"]] == ["ramp-sonnet-5-5-high"]
    assert not {"ramp-opus-5-5-high", "ramp-sonnet-5-5-xhigh"} & set(
        _cursor_list(cursor_db)
    )
    assert "ramp-sonnet-5-5-xhigh" in fake.names()


def test_a_variant_deleted_on_the_dashboard_leaves_cursor_or_is_made_again(
    router, cursor_db
):
    _ok("--effort-variants", "opus-5-5=high")
    del router.aliases["ramp-opus-5-5-high"]
    # Asked for again: made again.
    _ok("--effort-variants", "opus-5-5=high")
    assert "ramp-opus-5-5-high" in router.names()
    assert "ramp-opus-5-5-high" in _cursor_list(cursor_db)
    # Otherwise it leaves Cursor.
    del router.aliases["ramp-opus-5-5-high"]
    result = _ok(human=False)
    variants = json.loads(result.output)["data"][0]["effort_variants"]
    assert variants["unlisted"] == [
        {"name": "ramp-opus-5-5-high", "reason": cv.UNLISTED_GONE}
    ]
    assert "ramp-opus-5-5-high" not in _cursor_list(cursor_db)


def test_the_recommended_list_is_read_once(monkeypatch, router, cursor_db):
    catalog = _catalog()
    views = []

    def fetch(api_key, **kwargs):
        views.append(kwargs.get("model_view"))
        if kwargs.get("model_view") == "compact":
            return [model for model in catalog if model.id in COMPACT]
        return catalog

    monkeypatch.setattr(router_module, "_fetch_models", fetch)
    _ok("--cursor-models", "compact", "--effort-variants", "gpt-6-luna=high")
    assert views.count("compact") == 1


# === Aliases already on Router ===


def test_an_existing_alias_is_used_only_when_it_is_exactly_the_variant(
    monkeypatch, cursor_db
):
    fake = _fake(
        monkeypatch,  # Hand-made with the right model but no pinned effort, so both
        # ran at medium.
        _variant("ramp-opus-5-5-high", "claude-opus-5-5", None, origin=None),
        _variant(
            "ramp-gpt-6-luna-high",
            "gpt-6-luna",
            None,
            origin=None,
            provider="openai",
        ),
        # Pins the right effort but routes another model.
        _variant("ramp-opus-5-5-xhigh", "claude-haiku-5-5", "xhigh", origin=None),
        # Hand-made, and exactly the variant: used as it is.
        _variant("ramp-sonnet-5-5-xhigh", "claude-sonnet-5-5", "xhigh", origin=None),
    )
    result = _configure(
        "--effort-variants",
        "opus-5-5=high,xhigh",
        "--effort-variants",
        "gpt-6-luna=high",
        "--effort-variants",
        "sonnet-5-5=xhigh",
        human=False,
    )
    # A name asked for and left out is a request not delivered.
    assert result.exit_code == 1, result.output
    conflicts = json.loads(result.output)["data"][0]["effort_variants"]["conflicts"]
    assert {
        "name": "ramp-opus-5-5-high",
        "reason": "pins no effort, so it runs at the model's default",
    } in conflicts
    assert {row["name"] for row in conflicts} == {
        "ramp-opus-5-5-high",
        "ramp-gpt-6-luna-high",
        "ramp-opus-5-5-xhigh",
    }
    assert not any("reasoning_effort" in post for post in fake.posts)
    names = _cursor_list(cursor_db)
    assert not {row["name"] for row in conflicts} & set(names)
    assert "ramp-sonnet-5-5-xhigh" in names
    # None of them is ever deleted. The hand-made match was listed, and
    # leaving it out of the flags takes it out of Cursor, not off Router.
    assert _configure("--effort-variants", "opus-5-5=high").exit_code == 1
    assert fake.deletes == []
    assert "ramp-sonnet-5-5-xhigh" not in _cursor_list(cursor_db)
    assert {row["name"] for row in conflicts} | {"ramp-sonnet-5-5-xhigh"} <= (
        fake.names()
    )


@pytest.mark.parametrize(("view", "picked"), [("compact", False), ("all", True)])
def test_an_unpinned_alias_row_never_reaches_cursor(
    monkeypatch, cursor_db, view, picked
):
    row = _alias_row("ramp-opus-5-5-high", "claude-opus-5-5")
    _serve(monkeypatch, [*_catalog(), row], {*COMPACT, row.id})
    fake = _fake(
        monkeypatch,
        _variant("ramp-opus-5-5-high", "claude-opus-5-5", None, origin=None),
    )
    flags = ["--effort-variants", "opus-5-5=high"] if picked else []
    result = _configure("--cursor-models", view, *flags)
    assert result.exit_code == (1 if picked else 0), result.output
    assert "ramp-opus-5-5-high" not in _cursor_list(cursor_db)
    # Nor is it counted among the aliases Cursor lists.
    assert "of your aliases" not in result.output
    # A re-run keeps it out too, and never saves it as a selection.
    _ok()
    assert "ramp-opus-5-5-high" not in _cursor_list(cursor_db)
    assert "ramp-opus-5-5-high" not in cv.receipt_names(cv.read_receipt())
    assert fake.deletes == []


# === The alias limit ===


@pytest.mark.parametrize(
    ("limit", "first", "args", "have", "needs"),
    [
        # Swapping high for three names needs 4 - 1 + 3.
        (
            5,
            ("--effort-variants", "opus-5-5=high"),
            (
                "--effort-variants",
                "opus-5-5=xhigh",
                "--effort-variants",
                "sonnet-5-5=xhigh",
                "--effort-variants",
                "opus-4-8=high",
            ),
            4,
            6,
        ),
        # The base aliases a first run still has to make count.
        (4, None, ("--effort-variants", "opus-5-5=high,xhigh"), 0, 5),
        # So does the suggested model's, which joins the recommended list.
        (
            3,
            None,
            ("--cursor-models", "compact", "--effort-variants", "opus-5-5=high,xhigh"),
            0,
            4,
        ),
    ],
    ids=["swap", "first-run", "suggested-model"],
)
def test_a_flag_run_over_the_alias_limit_writes_nothing(
    monkeypatch, cursor_db, limit, first, args, have, needs
):
    _router_fallback(monkeypatch, "claude-sonnet-5-5")
    fake = _fake(monkeypatch, limit=limit)
    if first:
        _ok(*first)
    posts, before = len(fake.posts), _cursor_list(cursor_db)
    result = _configure(*args)
    # Checked while planning, so nothing at all was written.
    assert result.exit_code == 1, result.output
    assert (
        f"These effort variants don't fit: Router allows {limit} aliases, you have "
        f"{have}, and this run needs {needs}. Nothing was changed."
        in _flat(result.output)
    )
    assert len(fake.posts) == posts
    assert fake.deletes == []
    assert _cursor_list(cursor_db) == before


def test_variant_deletes_free_room_before_base_aliases_are_made(monkeypatch, cursor_db):
    # Recommended: Opus's base alias and two variants fill the limit.
    fake = _fake(monkeypatch, limit=3)
    _ok("--cursor-models", "compact", "--effort-variants", "opus-5-5=high,xhigh")
    # All models, keeping no variant: the deletes make room for Sonnet's and
    # Opus 4.8's base aliases.
    _ok("--cursor-models", "all", "--no-effort-variants")
    assert set(fake.names()) == {"ramp-opus-5-5", "ramp-sonnet-5-5", "ramp-opus-4-8"}
    assert {"ramp-sonnet-5-5", "ramp-opus-4-8"} <= set(_cursor_list(cursor_db))


@pytest.mark.parametrize(
    ("flag", "exit_code"),
    [("opus-9=high", 2), ("opus-5-5=high,xhigh", 1)],
    ids=["unknown-model", "over-the-alias-limit"],
)
def test_a_key_created_for_a_run_a_flag_stops_is_kept(
    monkeypatch, cursor_db, flag, exit_code
):
    # Room for the base aliases and one variant, not two.
    _use_router(monkeypatch, FakeRouter(limit=4))
    created = []

    def acquire(*args, **kwargs):
        created.append(f"browser-key-{len(created) + 1}")
        return created[-1]

    monkeypatch.setattr(router_module, "_pick_stored_router_api_key", lambda: None)
    monkeypatch.setattr(router_module, "acquire_router_api_key", acquire)

    def run(*args):
        return CliRunner().invoke(
            cli, ["--human", "router", "configure", "cursor", "--no-browser", *args]
        )

    stopped = run("--effort-variants", flag)
    assert stopped.exit_code == exit_code, stopped.output
    line = (
        "Cursor setup stopped before anything was written. The Router key it "
        "created is kept, and the next 'ramp router configure cursor' uses it."
    )
    assert stopped.output.count(line) == 1
    assert router_repo.pending_cursor_key(BASE_URL) == "browser-key-1"
    assert _cursor_list(cursor_db) == ["my-own-model"]
    # The next Cursor setup uses that key instead of making another.
    rerun = run("--effort-variants", "opus-5-5=high")
    assert rerun.exit_code == 0, rerun.output
    assert created == ["browser-key-1"]
    assert router_repo.pending_cursor_key(BASE_URL) is None


def test_variant_changes_never_remove_the_suggested_model(
    monkeypatch, router, cursor_db
):
    _router_fallback(monkeypatch, "claude-sonnet-5-5")
    _ok("--cursor-models", "all", "--effort-variants", "sonnet-5-5=xhigh")
    # Narrowing keeps the suggested model, so its variant stays too.
    _ok("--cursor-models", "compact")
    assert router.deletes == []
    assert {"ramp-sonnet-5-5", "ramp-sonnet-5-5-xhigh"} <= set(_cursor_list(cursor_db))
    # Removing every variant leaves the suggested model listed.
    _ok("--no-effort-variants")
    assert router.deletes == ["ramp-sonnet-5-5-xhigh"]
    assert "ramp-sonnet-5-5" in _cursor_list(cursor_db)
    assert "ramp-sonnet-5-5" in router.names()


# === Accounts and other Routers ===


def test_receipt_is_kept_per_router_account(monkeypatch, cursor_db):
    first = FakeRouter(owner="user-1")
    _use_router(monkeypatch, first)
    _ok("--effort-variants", "opus-5-5=high")
    # The same machine, signed in to Router as someone else.
    second = FakeRouter(owner="user-2")
    _use_router(monkeypatch, second)
    _ok("--no-effort-variants")
    assert second.deletes == []
    assert "ramp-opus-5-5-high" not in _cursor_list(cursor_db)
    _ok("--effort-variants", "gpt-6-luna=xhigh")
    accounts = {
        account["owner"]: sorted(account["variants"])
        for account in cv.read_receipt()["accounts"]
    }
    assert accounts == {
        "user-1": ["ramp-opus-5-5-high"],
        "user-2": ["ramp-gpt-6-luna-xhigh"],
    }
    # Back on the first account, its variant is still this machine's.
    _use_router(monkeypatch, first)
    _ok("--no-effort-variants")
    assert first.deletes == ["ramp-opus-5-5-high"]


def test_a_levels_timeout_keeps_names_only_for_the_same_key(monkeypatch, cursor_db):
    fake = _fake(monkeypatch)
    _ok("--effort-variants", "opus-5-5=high")

    def get(url, **kwargs):
        if url.endswith("/reasoning-efforts"):
            raise httpx.ReadTimeout("slow")
        return fake.get(url, **kwargs)

    monkeypatch.setattr(httpx, "get", get)
    # The key Cursor already sends: its account's names stay.
    _ok()
    assert "ramp-opus-5-5-high" in _cursor_list(cursor_db)
    # Another account's key on the same Router: those names aren't its own.
    fake.owner = "user-2"
    other = CliRunner().invoke(
        cli,
        ["--human", "--no-input", "router", "configure", "cursor", "--api-key", "k2"],
    )
    assert other.exit_code == 0, other.output
    assert "ramp-opus-5-5-high" not in _cursor_list(cursor_db)
    assert fake.deletes == []


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_moving_cursor_to_another_router_leaves_the_old_names_for_prune(
    monkeypatch, cursor_db, platform
):
    # Off macOS the printed steps are the setup, as with 'ramp router cursor'.
    monkeypatch.setattr("sys.platform", platform)
    here = FakeRouter()
    elsewhere = FakeRouter(origin=INTERNAL_ORIGIN)
    _route(monkeypatch, here, elsewhere)
    _ok("--effort-variants", "opus-5-5=high")
    # The same name on the other Router doesn't keep this one in use.
    monkeypatch.setenv("RAMP_ROUTER_BASE_URL", INTERNAL_BASE_URL)
    moved = _ok("--effort-variants", "opus-5-5=high")
    assert f"setup made it on {ORIGIN}, which Cursor isn't set up with now" in (
        _flat(moved.output)
    )
    accounts = {a["origin"]: a for a in cv.read_receipt()["accounts"]}
    assert cv.in_cursor(accounts[INTERNAL_ORIGIN])
    assert accounts[ORIGIN]["in_cursor"] is False
    assert here.deletes == []
    # Said once.
    again = _ok("--effort-variants", "opus-5-5=high")
    assert "which Cursor isn't set up with now" not in again.output
    # Back on the first Router, prune-variants there finds it.
    monkeypatch.delenv("RAMP_ROUTER_BASE_URL")
    assert "ramp-opus-5-5-high" in _prune().output


# === prune-variants ===


def test_prune_lists_orphans_and_deletes_only_after_confirmation(
    monkeypatch, cursor_db
):
    fake = _fake(
        monkeypatch,  # Left by another or a wiped machine.
        _variant("ramp-opus-5-5-xhigh", "claude-opus-5-5", "xhigh"),
        # Hand-made: not prunable.
        _variant("ramp-sonnet-5-5-xhigh", "claude-sonnet-5-5", "xhigh", origin=None),
    )
    _ok("--effort-variants", "opus-5-5=high")
    listed = _prune()
    assert listed.exit_code == 0, listed.output
    assert "ramp-opus-5-5-xhigh  anthropic:claude-opus-5-5 at xhigh" in listed.output
    assert "ramp-opus-5-5-high" not in listed.output
    assert "ramp-sonnet-5-5-xhigh" not in listed.output
    assert fake.deletes == []
    declined = _Answers(monkeypatch, False)
    assert _prune(no_input=False).exit_code == 0
    assert declined.asked[0][3]["default"] is False
    assert fake.deletes == []
    payload = json.loads(_prune("--yes", human=False).output)["data"][0]
    assert payload["deleted"] == ["ramp-opus-5-5-xhigh"]
    assert {"ramp-opus-5-5-high", "ramp-sonnet-5-5-xhigh"} <= fake.names()


def test_prune_never_sends_the_saved_key_to_another_router(router, monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")

    def prune():
        return CliRunner().invoke(
            cli, ["--human", "--no-input", "router", "cursor", "prune-variants"]
        )

    monkeypatch.setattr(router_repo, "cursor_bound_api_key", lambda: None)
    missing = prune()
    assert missing.exit_code == 2
    assert "Pass --api-key" in missing.output
    # Cursor was set up against an internal Router; this environment's
    # Router is the default one.
    monkeypatch.setattr(router_repo, "cursor_bound_api_key", lambda: "bound")
    monkeypatch.setattr(
        router_repo, "cursor_bound_upstream", lambda: "internal-api.router.com"
    )
    cv.write_receipt(
        {
            "accounts": [
                {
                    "origin": INTERNAL_ORIGIN,
                    "owner": "user-1",
                    "base_url": INTERNAL_BASE_URL,
                    "variants": {
                        "ramp-opus-5-5-high": _entry("claude-opus-5-5", "high")
                    },
                }
            ]
        }
    )
    refused = prune()
    assert refused.exit_code == 2, refused.output
    assert router.requests == []
    assert (
        "Cursor's saved Router key belongs to internal-api.router.com, not "
        "api.router.com, so it isn't sent here. To prune there, run "
        f"'RAMP_ROUTER_BASE_URL={INTERNAL_BASE_URL} ramp router "
        "cursor prune-variants'; to prune here, pass --api-key."
        in _flat(refused.output)
    )
    # A key passed by hand is the caller's choice, as is the saved key for
    # its own Router.
    assert _prune("--yes").exit_code == 0
    monkeypatch.setattr(router_repo, "cursor_bound_upstream", lambda: "api.router.com")
    assert prune().exit_code == 0


def test_a_rolled_back_router_changes_nothing(monkeypatch, cursor_db):
    fake = _fake(monkeypatch)
    _ok("--effort-variants", "opus-5-5=high")
    before = cv.read_receipt()
    # Rolled back: the listing has no origin or reasoning_effort.
    fake.effort = False
    pruned = _prune("--yes")
    assert pruned.exit_code == 0, pruned.output
    assert "doesn't support effort variants yet" in _flat(pruned.output)
    payload = json.loads(_ok(human=False).output)["data"][0]
    assert payload["effort_variants"]["names"] == ["ramp-opus-5-5-high"]
    assert "ramp-opus-5-5-high" in _cursor_list(cursor_db)
    assert cv.read_receipt() == before
    # Restored: nothing this machine lists reads as an orphan.
    fake.effort = True
    assert "No effort-variant aliases to prune." in _prune("--yes").output
    assert fake.deletes == []


# === Older Router ===


@pytest.mark.parametrize(
    "old",
    [
        # No pinned efforts at all: said before anyone is asked.
        {"effort": False, "delete": False, "levels": False},
        # Levels answered, but creates refuse reasoning_effort.
        {"effort": False},
    ],
    ids=["no-pinned-efforts", "create-refuses-the-effort"],
)
def test_an_older_router_says_so_once_and_setup_goes_ahead(monkeypatch, cursor_db, old):
    fake = _fake(monkeypatch, **old)
    result = _configure("--effort-variants", "opus-5-5=high,xhigh")
    # The flags asked for what can't be made.
    assert result.exit_code == 1, result.output
    assert _flat(result.output).count("doesn't support effort variants yet") == 1
    # At most one attempt.
    assert len([p for p in fake.posts if "reasoning_effort" in p]) <= 1
    assert "ramp-opus-5-5" in _cursor_list(cursor_db)
    assert "ramp-opus-5-5-high" not in _cursor_list(cursor_db)
    if old.get("levels") is False:
        answers = _Answers(monkeypatch)
        _ok(no_input=False)
        assert answers.asked == []


@pytest.mark.parametrize("status", [404, 405])
def test_old_router_without_delete_keeps_variants_and_finishes(
    monkeypatch, cursor_db, status
):
    fake = _fake(monkeypatch)
    _ok("--effort-variants", "opus-5-5=high,xhigh")

    def no_route(url, headers=None, timeout=None, **_kwargs):
        fake.deletes.append(url.rsplit("/", 1)[1])
        code = "not_found" if status == 404 else "http_error"
        return FakeRouter._response(status, {"error": {"code": code}})

    monkeypatch.setattr(httpx, "delete", no_route)
    result = _ok("--no-effort-variants")
    assert "doesn't support effort variants yet" in result.output
    # The first refusal stops further attempts, and a later run retries.
    assert len(fake.deletes) == 1
    assert cv.receipt_names(cv.read_receipt()) == {
        "ramp-opus-5-5-high",
        "ramp-opus-5-5-xhigh",
    }


# === Failed deletes and writes ===


def test_a_company_managed_key_gets_one_note(monkeypatch, capsys, cursor_db):
    fake = _fake(monkeypatch, managed=True)
    code, output = _main(
        monkeypatch, capsys, "--effort-variants", "opus-5-5=high,xhigh"
    )
    assert code == 1, output
    assert (
        output.count(
            "This key is managed by your company, so Ramp can't create or remove "
            "effort variants with it."
        )
        == 1
    )
    assert "run this again" not in output
    # One attempt, then none.
    assert len([p for p in fake.posts if "reasoning_effort" in p]) == 1


def test_a_variant_a_routing_profile_uses_stays_on_router(router, cursor_db):
    _ok("--effort-variants", "opus-5-5=high")
    router.referenced.add("ramp-opus-5-5-high")
    # The delete couldn't be done, so the flag run says so in its exit code.
    assert _configure("--no-effort-variants").exit_code == 1
    assert "ramp-opus-5-5-high" in router.names()
    assert "ramp-opus-5-5-high" not in _cursor_list(cursor_db)
    assert not cv.receipt_path().exists()
    # Left behind, prune finds it, and can't delete it either.
    result = _prune("--yes", human=False)
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)["data"][0]
    assert payload["kept"] == [{"name": "ramp-opus-5-5-high", "reason": "referenced"}]


def test_a_failed_delete_beside_a_kept_sibling_is_retried_not_relisted(
    monkeypatch, router, cursor_db
):
    _ok("--effort-variants", "opus-5-5=high,xhigh")
    original = router.delete

    def flaky(url, **kwargs):
        if url.endswith("/ramp-opus-5-5-high"):
            router.deletes.append("ramp-opus-5-5-high")
            raise httpx.ConnectError("down")
        return original(url, **kwargs)

    monkeypatch.setattr(httpx, "delete", flaky)
    assert _configure("--effort-variants", "opus-5-5=xhigh").exit_code == 1
    assert "ramp-opus-5-5-high" not in _cursor_list(cursor_db)
    entries = cv.account_variants(cv.read_receipt(), ORIGIN, "user-1")
    assert entries["ramp-opus-5-5-high"]["in_cursor"] is False
    assert "in_cursor" not in entries["ramp-opus-5-5-xhigh"]
    # Prune sees it as left behind, though its sibling is still listed.
    monkeypatch.setattr(httpx, "delete", original)
    assert "ramp-opus-5-5-high" in _prune().output
    # An unattended setup retries the delete instead of listing it again.
    _ok()
    assert "ramp-opus-5-5-high" not in _cursor_list(cursor_db)
    assert "ramp-opus-5-5-high" not in router.names()
    assert "ramp-opus-5-5-xhigh" in _cursor_list(cursor_db)
    assert cv.receipt_names(cv.read_receipt()) == {"ramp-opus-5-5-xhigh"}


def test_the_receipt_settles_only_after_cursors_list_is_written(
    monkeypatch, router, cursor_db
):
    _ok("--effort-variants", "opus-5-5=high,xhigh")
    real = cursor.configure

    def fail(*args, **kwargs):
        raise cursor.CursorSettingsError("disk full")

    monkeypatch.setattr(cursor, "configure", fail)
    failed = _configure("--effort-variants", "opus-5-5=high")
    assert router.deletes == ["ramp-opus-5-5-xhigh"]
    # Cursor still lists it, so the receipt still names it, never as made
    # here again.
    assert "ramp-opus-5-5-xhigh" in _cursor_list(cursor_db)
    entries = cv.read_receipt()["accounts"][0]["variants"]
    assert entries["ramp-opus-5-5-xhigh"]["created"] is False, failed.output
    assert "ramp-opus-5-5-xhigh" in cv.listed_names(cv.read_receipt())
    monkeypatch.setattr(cursor, "configure", real)
    _ok("--effort-variants", "opus-5-5=high")
    assert "ramp-opus-5-5-xhigh" not in _cursor_list(cursor_db)
    assert cv.receipt_names(cv.read_receipt()) == {"ramp-opus-5-5-high"}


# === Dashboard edits ===

EDITED_LINE = (
    "ramp-opus-5-5-high was edited in the Router dashboard and no longer pins "
    f"an effort, so Cursor won't list it. Delete it at {ORIGIN}/strategies or "
    "rename it."
)


def _edit_models(router, name="ramp-opus-5-5-high"):
    note = router.edit_on_dashboard(
        name, models=["anthropic:claude-opus-5-5", "bedrock:claude-opus-5-5"]
    )
    assert note is not None
    assert router.aliases[name]["reasoning_effort"] is None


def test_a_no_change_dashboard_save_keeps_the_pin_and_makes_it_the_users(
    router, cursor_db
):
    _ok("--effort-variants", "opus-5-5=high")
    # Saved on the dashboard without changes: the pin stays, origin goes.
    assert router.edit_on_dashboard("ramp-opus-5-5-high") is None
    result = _ok()
    assert "edited in the Router dashboard" not in result.output
    assert "ramp-opus-5-5-high" in _cursor_list(cursor_db)
    # Unticking it then only takes it out of Cursor: it's the user's now.
    _ok("--no-effort-variants")
    assert router.deletes == []
    assert "ramp-opus-5-5-high" not in _cursor_list(cursor_db)


def test_a_cleared_pin_is_a_conflict(monkeypatch, router, cursor_db):
    _ok("--cursor-models", "all", "--effort-variants", "opus-5-5=high")
    _edit_models(router)
    # Router lists the unpinned alias as a plain model row.
    _serve(
        monkeypatch, [*_catalog(), _alias_row("ramp-opus-5-5-high", "claude-opus-5-5")]
    )
    # A flag run that asks for it exits 1, like other conflicts.
    result = _configure("--effort-variants", "opus-5-5=high", human=False)
    assert result.exit_code == 1, result.output
    data = json.loads(result.output)["data"][0]
    assert EDITED_LINE in data["model_notes"]
    variants = data["effort_variants"]
    assert {"name": "ramp-opus-5-5-high", "reason": cv.EDITED_IN_DASHBOARD} in (
        variants["conflicts"]
    )
    assert variants["removed"] == []
    # Reported once, it leaves the receipt; no later run, with or without
    # variants, lists it as a plain model either.
    assert not cv.receipt_path().exists()
    for args in ((), ("--no-effort-variants",), ("--no-effort-variants",)):
        _ok(*args)
        assert "ramp-opus-5-5-high" not in _cursor_list(cursor_db)
    assert router.deletes == []


def test_prune_leaves_an_edited_alias_for_setup_to_take_out(router, cursor_db):
    _ok("--effort-variants", "opus-5-5=high")
    _edit_models(router)
    result = _prune("--yes")
    assert result.exit_code == 0, result.output
    assert "Left ramp-opus-5-5-high in place" in _flat(result.output)
    # Said once.
    assert "edited" not in json.loads(_prune("--yes", human=False).output)["data"][0]
    # Prune doesn't touch Cursor's list, so the receipt keeps the name for
    # setup to take out, and only then forget.
    assert "ramp-opus-5-5-high" in _cursor_list(cursor_db)
    _ok("--no-effort-variants")
    assert "ramp-opus-5-5-high" not in _cursor_list(cursor_db)
    assert not cv.receipt_path().exists()
    assert router.deletes == []


# === Unconfigure ===


def test_unconfigure_removes_only_what_this_machine_made(router, cursor_db):
    # Another machine's variant, one a routing profile uses, and one edited
    # in the dashboard all stay.
    router.aliases["ramp-sonnet-5-5-xhigh"] = {
        "user_id": "user-1",
        **_variant("ramp-sonnet-5-5-xhigh", "claude-sonnet-5-5", "xhigh"),
    }
    _ok(
        "--cursor-models",
        "all",
        "--effort-variants",
        "opus-5-5=high,xhigh",
        "--effort-variants",
        "opus-4-8=high",
        "--effort-variants",
        "sonnet-5-5=xhigh",
    )
    router.referenced.add("ramp-opus-5-5-xhigh")
    _edit_models(router, "ramp-opus-4-8-high")
    result = _unconfigure()
    assert result.exit_code == 0, result.output
    output = _flat(result.output)
    assert router.deletes == ["ramp-opus-5-5-high", "ramp-opus-5-5-xhigh"]
    assert {
        "ramp-sonnet-5-5-xhigh",
        "ramp-opus-5-5-xhigh",
        "ramp-opus-4-8-high",
        "ramp-opus-5-5",
    } <= router.names()
    for name, why in (
        ("ramp-sonnet-5-5-xhigh", "another machine created it"),
        ("ramp-opus-5-5-xhigh", "a routing profile uses it"),
        ("ramp-opus-4-8-high", "it was edited in the Router dashboard"),
    ):
        assert f"{name} stays on your Router account: {why}" in output
    assert _cursor_list(cursor_db) == ["my-own-model"]
    assert not cv.receipt_path().exists()


def test_unconfigure_leaves_another_accounts_variants(monkeypatch, cursor_db):
    fake = _fake(monkeypatch, owner="user-1")
    _ok("--effort-variants", "opus-5-5=high")
    # The bound key now belongs to someone else.
    fake.owner = "user-2"
    for alias in fake.aliases.values():
        alias["user_id"] = "user-2"
    result = _unconfigure()
    assert result.exit_code == 0, result.output
    assert fake.deletes == []
    assert "prune-variants" in _flat(result.output)


def test_unconfigure_without_variants_never_calls_router(router, cursor_db):
    _ok("--cursor-models", "compact")
    gets = router.gets
    assert _unconfigure().exit_code == 0
    assert router.gets == gets
    assert router.deletes == []


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_unconfigure_never_sends_the_key_to_another_router(
    monkeypatch, cursor_db, platform
):
    monkeypatch.setattr("sys.platform", platform)
    here = FakeRouter()
    elsewhere = FakeRouter(origin=INTERNAL_ORIGIN)
    stray = _route(monkeypatch, here, elsewhere)
    _ok("--effort-variants", "opus-5-5=high")
    # An earlier setup against another Router made one there.
    receipt = cv.read_receipt()
    receipt["accounts"].append(
        {
            "origin": INTERNAL_ORIGIN,
            "owner": "user-1",
            "base_url": INTERNAL_BASE_URL,
            "in_cursor": False,
            "variants": {"ramp-opus-5-5-xhigh": _entry("claude-opus-5-5", "xhigh")},
        }
    )
    cv.write_receipt(receipt)
    elsewhere.aliases["ramp-opus-5-5-xhigh"] = {
        "user_id": "user-1",
        **_variant("ramp-opus-5-5-xhigh", "claude-opus-5-5", "xhigh"),
    }
    monkeypatch.setattr(router_repo, "cursor_bound_api_key", lambda: "k")
    monkeypatch.setattr(router_repo, "cursor_bound_upstream", lambda: "api.router.com")
    result = _unconfigure()
    assert result.exit_code == 0, result.output
    assert here.deletes == ["ramp-opus-5-5-high"]
    # No request at all reached the other Router.
    assert elsewhere.requests == []
    assert stray == []
    command = (
        f"ramp router cursor prune-variants --api-key KEY --base-url {INTERNAL_BASE_URL}"
        if platform == "win32"
        else f"RAMP_ROUTER_BASE_URL={INTERNAL_BASE_URL} ramp router cursor prune-variants --api-key KEY"
    )
    assert f"To delete them, run '{command}'" in _flat(result.output)
    if platform == "win32":
        assert _prune("--base-url", INTERNAL_BASE_URL).exit_code == 0
        assert elsewhere.requests
        assert stray == []


# === Prompts and paste-in steps ===


RECOMMENDED_OFFERS = [
    "ramp-gpt-6-luna-high",
    "ramp-gpt-6-luna-xhigh",
    "ramp-opus-5-5-high",
    "ramp-opus-5-5-xhigh",
]


def test_yes_lists_the_names_with_nothing_selected(monkeypatch, cursor_db):
    # Another machine's variant is on the account already.
    fake = _fake(
        monkeypatch, _variant("ramp-opus-5-5-xhigh", "claude-opus-5-5", "xhigh")
    )
    answers = _Answers(monkeypatch, True, ["ramp-opus-5-5-high"])
    _ok(no_input=False)
    choices = answers.asked[1][2]
    # The recommended list: Opus and GPT-6 Luna, higher levels only.
    assert [c.value for c in choices] == RECOMMENDED_OFFERS
    assert answers.checked(1) == set()
    titles = {c.value: c.title for c in choices}
    assert titles["ramp-opus-5-5-xhigh"].endswith("(already on your Router account)")
    assert not titles["ramp-opus-5-5-high"].endswith(")")
    assert _variant_aliases(fake) == {
        "ramp-opus-5-5-xhigh": "xhigh",
        "ramp-opus-5-5-high": "high",
    }


def test_one_gate_first_then_keep_or_change_the_saved_names(
    router, cursor_db, monkeypatch
):
    declined = _Answers(monkeypatch, False)
    _ok(no_input=False)
    kind, message, _choices, kwargs = declined.asked[0]
    assert (kind, message) == (
        "confirm",
        "Add model names with a higher reasoning effort?",
    )
    assert kwargs["default"] is False
    # No: nothing else is asked and nothing is created.
    assert len(declined.asked) == 1
    assert _variant_aliases(router) == {}
    _Answers(monkeypatch, True, ["ramp-opus-5-5-high", "ramp-gpt-6-luna-high"])
    _ok(no_input=False)
    # On a rerun, No keeps them as they are.
    kept = _Answers(monkeypatch, False)
    _ok(no_input=False)
    assert kept.asked[0][1] == (
        "Change the higher-effort model names? "
        "(now: ramp-gpt-6-luna-high, ramp-opus-5-5-high)"
    )
    assert router.deletes == []
    # Yes starts from them; unticking removes.
    changed = _Answers(monkeypatch, True, ["ramp-opus-5-5-high"])
    _ok(no_input=False)
    assert changed.checked(1) == {"ramp-opus-5-5-high", "ramp-gpt-6-luna-high"}
    assert router.deletes == ["ramp-gpt-6-luna-high"]
    assert "ramp-gpt-6-luna-high" not in _cursor_list(cursor_db)


def test_paste_in_steps_ask_the_same_question_and_list_the_names(router, monkeypatch):
    answers = _Answers(monkeypatch, True, ["ramp-opus-5-5-high"])
    added = _cursor_command(no_input=False)
    assert added.exit_code == 0, added.output
    assert [kind for kind, *_ in answers.asked] == ["confirm", "checkbox"]
    assert _variant_aliases(router) == {"ramp-opus-5-5-high": "high"}
    assert (
        "ramp-opus-5-5-high" in _flat(added.output).split("Add these model names:")[1]
    )
    removed = _cursor_command("--no-effort-variants")
    assert removed.exit_code == 0, removed.output
    assert router.deletes == ["ramp-opus-5-5-high"]
    assert "Remove these if you added them earlier: ramp-opus-5-5-high." in _flat(
        removed.output
    )


def test_automatic_setup_removes_a_variant_name_added_by_hand(router, cursor_db):
    assert _cursor_command("--effort-variants", "opus-5-5=high").exit_code == 0
    # What the paste-in steps have the user do: add the name and turn it on.
    with sqlite3.connect(cursor_db) as db:
        (value,) = db.execute(
            "SELECT value FROM ItemTable WHERE key = ?", (cursor._SETTINGS_KEY,)
        ).fetchone()
        settings = json.loads(value)
        for key in ("userAddedModels", "modelOverrideEnabled"):
            settings["aiSettings"].setdefault(key, []).append("ramp-opus-5-5-high")
        db.execute(
            "UPDATE ItemTable SET value = ? WHERE key = ?",
            (json.dumps(settings), cursor._SETTINGS_KEY),
        )
    _ok("--no-effort-variants")
    assert router.deletes == ["ramp-opus-5-5-high"]
    assert "ramp-opus-5-5-high" not in _cursor_list(cursor_db)
    assert "ramp-opus-5-5-high" not in _enabled(cursor_db)
    assert "my-own-model" in _cursor_list(cursor_db)
