"""Tests for the model list Cursor setup writes: Recommended or All."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

import ramp_cli.commands.router as router_module
from ramp_cli.commands import cursor, cursor_models
from ramp_cli.main import cli
from tests.test_router_cursor import _router_model as _model

# Captured before conftest replaces them for every test.
_REAL_PICK = router_module._pick_cursor_model_scope
_REAL_ENSURE_CLAUDE_ALIASES = router_module._ensure_claude_aliases

MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5", "gpt-6-luna")
MODELS += ("gpt-5", "kimi-k3", "glm-5p3", "mystery-latest")
COMPACT = ("claude-opus-5-5", "claude-sonnet-5-5", "gpt-6-luna", "kimi-k3")
ALL_NAMES = [name.replace("claude-", "ramp-") for name in MODELS]
COMPACT_NAMES = [name.replace("claude-", "ramp-") for name in COMPACT]
# Cursor's list after setup adds every model to the user's "mine" and "gpt-5".
LISTED_WITH_ALL = ["mine", "gpt-5", *(name for name in ALL_NAMES if name != "gpt-5")]
UNATTENDED = ("--human", "--no-input")


def _catalog():
    return [_model(name) for name in MODELS]


def _alias(name, *targets, effort=None):
    """A caller alias row as Router lists it, with router_alias set."""
    block = {
        "name": name,
        "candidates": [
            {"model": target, "provider": "p", "provider_model": target}
            for target in targets
        ],
        **({"reasoning_effort": effort} if effort else {}),
    }
    metadata = router_module._with_router_alias(_model(name).metadata, block)
    return router_module.RouterModel(id=name, metadata=metadata)


def _catalog_with_aliases():
    return [
        *_catalog(),
        # The base alias setup made: the same model as claude-haiku-4-5.
        _alias("ramp-haiku-4-5", "claude-haiku-4-5"),
        # Hand-made: one routing a recommended model, one routing none.
        _alias("my-fast", "kimi-k3"),
        _alias("my-mix", "gpt-5", "glm-5p3"),
        # A pinned effort variant, left to the effort-variant setup.
        _alias("ramp-opus-5-5-high", "claude-opus-5-5", effort="high"),
    ]


class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.fixture
def router(monkeypatch):
    """A fake Router: the catalog, its compact view, and alias creation."""
    state = {"catalog": _catalog(), "compact": COMPACT, "alias_models": []}

    def fetch_models(api_key, *args, **kwargs):
        if kwargs.get("model_view") != "compact":
            return list(state["catalog"])
        if state.get("compact_fails"):
            raise click.ClickException(
                "Ramp Router couldn't validate the key (HTTP 503)."
            )
        return [model for model in state["catalog"] if model.id in state["compact"]]

    def ensure_aliases(api_key, base_url, models, **kwargs):
        state["alias_models"].append([model.id for model in models])
        return {
            model.id.replace("claude-", "ramp-"): model.id
            for model in models
            if model.id.startswith("claude-")
        }

    monkeypatch.setattr(router_module, "_fetch_models", fetch_models)
    monkeypatch.setattr(router_module, "_ensure_claude_aliases", ensure_aliases)
    monkeypatch.setattr(router_module, "_copy_to_clipboard", lambda text: True)
    monkeypatch.setattr(router_module, "_clipboard_available", lambda: True)
    monkeypatch.setattr(router_module, "_fetch_configure_summary", lambda *a, **k: None)
    return state


@pytest.fixture
def cursor_db(monkeypatch, tmp_path, router):
    """A closed Cursor on macOS. The user added "mine" and "gpt-5" themselves."""
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    monkeypatch.setattr(cursor, "encrypt_with_cursor", lambda key, prompt=True: "c")
    path = cursor.state_db_path()
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
        db.execute("INSERT INTO ItemTable VALUES (?, ?)", (cursor._SETTINGS_KEY, "{}"))
    _set_ai_settings(
        path, userAddedModels=["mine", "gpt-5"], modelOverrideEnabled=["mine"]
    )
    return path


def _ai_settings(path):
    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT value FROM ItemTable WHERE key = ?", (cursor._SETTINGS_KEY,)
        ).fetchone()
    return json.loads(row[0]).get("aiSettings", {})


def _set_ai_settings(path, **lists):
    value = {"aiSettings": {**_ai_settings(path), **lists}}
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE ItemTable SET value = ? WHERE key = ?",
            (json.dumps(value), cursor._SETTINGS_KEY),
        )


def _add_to_cursor(path, name):
    """The user lists and turns on a name themselves."""
    settings = _ai_settings(path)
    _set_ai_settings(
        path,
        userAddedModels=[*settings["userAddedModels"], name],
        modelOverrideEnabled=[*settings["modelOverrideEnabled"], name],
    )


def _receipt():
    return json.loads(cursor._receipt_path().read_text())


class _Prompts:
    """Scripted picker and confirm answers; ENTER takes the prompt's default."""

    ENTER = object()

    def __init__(self, monkeypatch, *answers):
        self.answers = list(answers)
        self.asked: list[dict] = []
        monkeypatch.setattr(router_module, "_pick_cursor_model_scope", _REAL_PICK)
        monkeypatch.setattr(router_module, "_can_prompt", lambda ctx, fmt: True)
        monkeypatch.setattr(
            router_module.questionary,
            "select",
            lambda message, choices, default=None, **_: self._ask(
                "select", default, titles=[choice.title for choice in choices]
            ),
        )
        monkeypatch.setattr(
            router_module.questionary,
            "confirm",
            lambda message, default=True, **_: self._ask("confirm", default),
        )

    def _ask(self, kind, default, **details):
        self.asked.append({"kind": kind, "default": default, **details})
        answer = self.answers.pop(0)
        return SimpleNamespace(ask=lambda: default if answer is self.ENTER else answer)

    def of(self, kind):
        return [question for question in self.asked if question["kind"] == kind]


def _run(*args, root=("--human",)):
    return CliRunner().invoke(cli, [*root, "router", *args])


def _configure(*flags, root=("--human",)):
    return _run("configure", "cursor", "--api-key", "router-secret", *flags, root=root)


def _ok(result):
    assert result.exit_code == 0, result.output
    return result


def _agent(*flags):
    return json.loads(_ok(_configure(*flags, root=("--agent",))).output)["data"][0]


def _legacy_setup(cursor_db):
    """What setup left before the choice existed: every model, no saved choice."""
    cursor.configure("router-secret", "https://api.router.com/v1", ALL_NAMES)
    assert "model_scope" not in _receipt()


# === The prompt ===


@pytest.mark.parametrize(
    ("before", "default", "titles"),
    [
        # Only a preselected Recommended is labelled the default.
        (
            "first run",
            "compact",
            ["Recommended models (default)", "All available models"],
        ),
        ("saved all", "all", ["Recommended models", "All available models"]),
        # A receipt from before the choice existed counts as All.
        ("legacy", "all", ["Recommended models", "All available models"]),
    ],
)
def test_the_prompt_preselects_the_previous_choice(
    cursor_db, router, monkeypatch, before, default, titles
):
    if before == "saved all":
        _ok(_configure("--cursor-models", "all", root=UNATTENDED))
    elif before == "legacy":
        _legacy_setup(cursor_db)
    prompts = _Prompts(monkeypatch, _Prompts.ENTER)
    _ok(_configure())
    (question,) = prompts.of("select")
    assert (question["default"], question["titles"]) == (default, titles)
    # Enter keeps or starts a list, so nothing is removed or confirmed.
    assert prompts.of("confirm") == []
    assert _receipt()["model_scope"] == {"view": default}


@pytest.mark.parametrize("cancel_at", ["picker", "remove them"])
def test_cancelling_changes_nothing_and_loses_no_key(
    cursor_db, router, monkeypatch, cancel_at
):
    """The picker comes before sign-in, so cancelling it creates no key; a key
    created before "Remove them?" is kept pending for the next run."""
    if cancel_at == "remove them":
        _ok(_configure("--cursor-models", "all", root=UNATTENDED))
    created, pending = [], []
    monkeypatch.setattr(
        router_module,
        "acquire_router_api_key",
        lambda *a, **k: created.append(1) or "new-key",
    )
    monkeypatch.setattr(router_module, "_pick_stored_router_api_key", lambda: None)
    monkeypatch.setattr(
        router_module.router_repo,
        "mark_cursor_key_pending",
        lambda base_url, api_key: pending.append(api_key) or True,
    )
    before = _ai_settings(cursor_db)
    if cancel_at == "picker":
        _Prompts(monkeypatch, None, None)
        assert _run("configure", "cursor").exit_code != 0
        assert _run("cursor").exit_code != 0
        assert (created, router["alias_models"]) == ([], [])
        assert not cursor.has_receipt()
    else:
        _Prompts(monkeypatch, "compact", None)
        assert _run("configure", "cursor").exit_code != 0
        assert pending == ["new-key"]
    assert _ai_settings(cursor_db) == before


@pytest.mark.parametrize(
    ("flags", "answers", "removed"),
    [
        ((), ("compact", True), True),
        # A flag skips the picker but an interactive run still confirms;
        # Enter there is No.
        (("--cursor-models", "compact"), (_Prompts.ENTER,), False),
    ],
)
def test_an_interactive_narrowing_confirms_first(
    cursor_db, router, monkeypatch, flags, answers, removed
):
    router["catalog"] = _catalog_with_aliases()
    _ok(_configure("--cursor-models", "all", root=UNATTENDED))
    # A variant name an effort-variant setup listed is left where it is.
    receipt = _receipt()
    receipt["models_added"].append("ramp-opus-5-5-high")
    cursor._write_receipt(receipt)
    _add_to_cursor(cursor_db, "ramp-opus-5-5-high")
    before = _ai_settings(cursor_db)["userAddedModels"]
    prompts = _Prompts(monkeypatch, *answers)
    result = _ok(_configure(*flags))
    # ramp-haiku-4-5 is a base alias row, so it counts as a model.
    assert (
        "This removes 4 models and 1 alias from Cursor: ramp-haiku-4-5, glm-5p3, "
        "mystery-latest, my-mix, gpt-5." in result.output
    )
    (confirm,) = prompts.of("confirm")
    assert confirm["default"] is False
    assert len(prompts.of("select")) == (0 if flags else 1)
    names = _ai_settings(cursor_db)["userAddedModels"]
    if removed:
        assert not {"glm-5p3", "ramp-haiku-4-5", "my-mix"} & set(names)
        assert {"ramp-opus-5-5-high", "my-fast"} <= set(names)
        assert _receipt()["model_scope"] == {"view": "compact"}
    else:
        assert names == before
        # Declining keeps the previous choice too.
        assert _receipt()["model_scope"] == {"view": "all"}


# === Flags ===


@pytest.mark.parametrize(
    ("flags", "root", "expected"),
    [
        (("--cursor-models", "compact"), UNATTENDED, COMPACT_NAMES),
        (("--cursor-models", "COMPACT"), UNATTENDED, COMPACT_NAMES),
        (("--cursor-models", "all"), UNATTENDED, ALL_NAMES),
        # Without a flag or a prompt, a first run lists every model.
        ((), UNATTENDED, ALL_NAMES),
        ((), ("--agent",), ALL_NAMES),
    ],
)
def test_cursor_models_flag(cursor_db, router, flags, root, expected):
    _ok(_configure(*flags, root=root))
    added = [n for n in _ai_settings(cursor_db)["userAddedModels"] if n != "mine"]
    # gpt-5 was the user's own row; setup only turned it on.
    assert [n for n in added if n != "gpt-5"] == [n for n in expected if n != "gpt-5"]
    view = "compact" if expected == COMPACT_NAMES else "all"
    assert _receipt()["model_scope"] == {"view": view}


def test_cursor_flags_need_cursor():
    result = CliRunner().invoke(
        cli,
        ["--human", "router", "configure", "codex", "--cursor-models", "all"],
    )
    assert result.exit_code == 2
    assert "apply to Cursor" in result.output


# === Defaults ===


@pytest.mark.parametrize(
    ("before", "view"), [("compact", "compact"), ("legacy", "all")]
)
def test_a_non_interactive_rerun_keeps_the_saved_choice_and_removes_nothing(
    cursor_db, router, before, view
):
    if before == "legacy":
        _legacy_setup(cursor_db)
    else:
        _ok(_configure("--cursor-models", before, root=UNATTENDED))
    listed = _ai_settings(cursor_db)["userAddedModels"]
    # Router's recommended list moves on: kimi-k3 out, glm-5p3 in.
    router["compact"] = (
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "gpt-6-luna",
        "glm-5p3",
    )
    _ok(_configure(root=UNATTENDED))
    names = _ai_settings(cursor_db)["userAddedModels"]
    # No flag asked for a change, so nothing was taken out.
    assert set(listed) <= set(names)
    assert "glm-5p3" in names
    assert _receipt()["model_scope"] == {"view": view}


# === Reconcile ===


def test_narrowing_and_widening_touch_only_what_setup_added(cursor_db, router):
    _ok(_configure("--cursor-models", "all", root=UNATTENDED))
    assert _ai_settings(cursor_db)["userAddedModels"] == LISTED_WITH_ALL
    # Setup turned on the user's own gpt-5 row.
    assert "gpt-5" in _receipt()["enabled_added"]

    _ok(_configure("--cursor-models", "compact", root=UNATTENDED))
    settings = _ai_settings(cursor_db)
    # The user's own gpt-5 row stays; setup only turns its toggle back off.
    assert settings["userAddedModels"] == ["mine", "gpt-5", *COMPACT_NAMES]
    assert settings["modelOverrideEnabled"] == ["mine", *COMPACT_NAMES]
    receipt = _receipt()
    assert receipt["models_added"] == COMPACT_NAMES
    assert "gpt-5" not in receipt["enabled_added"]

    _ok(_configure("--cursor-models", "all", root=UNATTENDED))
    settings = _ai_settings(cursor_db)
    assert sorted(settings["userAddedModels"]) == sorted(LISTED_WITH_ALL)
    assert {"mine", "ramp-haiku-4-5"} <= set(settings["modelOverrideEnabled"])

    # Unconfigure still restores exactly the user's own state.
    assert cursor.unconfigure() is True
    settings = _ai_settings(cursor_db)
    assert settings["userAddedModels"] == ["mine", "gpt-5"]
    assert settings["modelOverrideEnabled"] == ["mine"]


def test_narrowing_never_touches_the_users_own_rows_or_toggles(cursor_db, router):
    # The user had turned glm-5p3 on before setup added its row.
    _set_ai_settings(
        cursor_db, userAddedModels=["mine"], modelOverrideEnabled=["mine", "glm-5p3"]
    )
    _ok(_configure("--cursor-models", "all", root=UNATTENDED))
    assert "glm-5p3" not in _receipt()["enabled_added"]
    # And added a name of their own after setup.
    _add_to_cursor(cursor_db, "my-own-alias")
    _ok(_configure("--cursor-models", "compact", root=UNATTENDED))
    settings = _ai_settings(cursor_db)
    assert "glm-5p3" not in settings["userAddedModels"]
    assert "glm-5p3" in settings["modelOverrideEnabled"]
    assert "my-own-alias" in settings["userAddedModels"]
    assert "my-own-alias" in settings["modelOverrideEnabled"]
    assert cursor.unconfigure() is True
    assert _ai_settings(cursor_db)["modelOverrideEnabled"] == [
        "mine",
        "glm-5p3",
        "my-own-alias",
    ]


def test_a_failed_cursor_write_keeps_the_removed_names_in_the_receipt(
    cursor_db, router, monkeypatch
):
    _ok(_configure("--cursor-models", "all", root=UNATTENDED))
    before = _ai_settings(cursor_db)

    def fail(*args, **kwargs):
        raise cursor.CursorSettingsError("disk full")

    monkeypatch.setattr(cursor, "_write", fail)
    _configure("--cursor-models", "compact", root=UNATTENDED)
    # Cursor still lists them, so the receipt still says setup added them.
    assert _ai_settings(cursor_db) == before
    receipt = _receipt()
    assert "glm-5p3" in receipt["models_added"]
    assert "gpt-5" in receipt["enabled_added"]


def test_a_failed_receipt_prune_still_reports_cursor_as_set_up(
    cursor_db, router, monkeypatch
):
    _ok(_configure("--cursor-models", "all", root=UNATTENDED))
    real_write = cursor._write_receipt
    calls = []

    def write(receipt):
        calls.append(receipt)
        if len(calls) == 2:
            raise cursor.CursorSettingsError(
                "Cursor's restore record could not be saved."
            )
        real_write(receipt)

    monkeypatch.setattr(cursor, "_write_receipt", write)
    bound = []
    monkeypatch.setattr(
        router_module.router_repo,
        "bind_cursor",
        lambda base_url, api_key, user_dir: bound.append(api_key),
    )
    payload = _agent("--cursor-models", "compact")
    assert len(calls) == 2
    assert payload["settings_written"] is True
    assert bound == ["router-secret"]
    assert "glm-5p3" not in _ai_settings(cursor_db)["userAddedModels"]
    # The receipt still names what setup added, so unconfigure removes it all,
    # and it holds the new choice, so the next unattended run keeps it.
    assert "glm-5p3" in _receipt()["models_added"]
    assert _receipt()["model_scope"] == {"view": "compact"}
    monkeypatch.setattr(cursor, "_write_receipt", real_write)
    _ok(_configure(root=UNATTENDED))
    assert "glm-5p3" not in _ai_settings(cursor_db)["userAddedModels"]
    assert cursor.unconfigure() is True
    assert _ai_settings(cursor_db)["userAddedModels"] == ["mine", "gpt-5"]


# === Router's recommended list fails to load ===


def test_a_failed_recommended_fetch_lists_all_models_this_run(
    cursor_db, router, monkeypatch
):
    _ok(_configure("--cursor-models", "all", root=UNATTENDED))
    router["compact_fails"] = True
    configured = []

    def configure_client(item, api_key, models, **kwargs):
        configured.append((item, len(models)))
        return cursor_db.parent / f"{item}.toml", None, False

    monkeypatch.setattr(router_module, "_configure_client", configure_client)
    flags = ("codex", "--cursor-models", "compact", "--api-key", "router-secret")
    result = _ok(_run("configure", "cursor", *flags, root=("--agent",)))
    assert "couldn't validate the key" not in result.output
    # The other agents are still set up, with every model.
    assert configured == [("codex", len(_catalog()))]
    (payload,) = (
        item
        for item in json.loads(result.output)["data"][0]["clients"]
        if item["client"] == "cursor"
    )
    assert payload["model_notes"] == [
        "Router's recommended list couldn't be loaded, so Cursor lists all models "
        "this run and nothing is removed. The recommended list stays your choice "
        "for the next run."
    ]
    assert payload["model_scope"] == {"view": "all"}
    assert payload["model_names_removed"] == []
    assert _ai_settings(cursor_db)["userAddedModels"] == LISTED_WITH_ALL
    # The choice is kept for the next run, not replaced by this run's fallback.
    assert payload["model_scope_saved"] == {"view": "compact"}
    assert _receipt()["model_scope"] == {"view": "compact"}


@pytest.mark.parametrize(
    ("platform", "command"),
    [
        ("linux", ("cursor",)),
        # macOS, but Cursor has no settings to write: setup falls back to steps.
        ("darwin", ("configure", "cursor")),
    ],
)
def test_a_run_that_saves_nothing_never_claims_a_fallback_choice(
    router, monkeypatch, tmp_path, platform, command
):
    monkeypatch.setattr("sys.platform", platform)
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path))
    router["compact_fails"] = True
    command += ("--api-key", "router-secret", "--cursor-models", "compact")
    payload = json.loads(_ok(_run(*command, root=("--agent",))).output)["data"][0]
    if platform == "darwin":
        assert payload["settings_written"] is False
    assert payload["model_scope"] == {"view": "all"}
    assert "model_scope_saved" not in payload
    output = " ".join(_ok(_run(*command)).output.split())
    assert "Router's recommended list couldn't be loaded" in output
    assert "stays your choice" not in output


# === Paste-in steps and the suggested model ===


def _router_fallback(monkeypatch, model):
    monkeypatch.setattr(
        "ramp_cli.admin_defaults.fallback_model", lambda *_args, **_kwargs: model
    )


@pytest.mark.parametrize(
    "command",
    [
        ("cursor", "--api-key", "router-secret"),
        ("configure", "cursor", "--api-key", "router-secret"),
    ],
)
def test_paste_in_steps_name_only_models_in_scope(router, monkeypatch, command):
    monkeypatch.setattr("sys.platform", "linux")
    _router_fallback(monkeypatch, "glm-5p3")
    result = _ok(_run(*command, "--cursor-models", "compact"))
    output = " ".join(result.output.split())
    # The recommended models, plus Router's fallback the steps pick.
    assert (
        "Add these model names: ramp-opus-5-5, ramp-sonnet-5-5, gpt-6-luna, kimi-k3, "
        "glm-5p3." in output
    )
    assert "Pick glm-5p3 in Cursor's chat." in output
    assert "ramp-haiku-4-5" not in output
    assert all("claude-haiku-4-5" not in models for models in router["alias_models"])


@pytest.mark.parametrize(
    ("fallback", "extra", "suggested"),
    [
        ("glm-5p3", None, "glm-5p3"),
        (None, router_module.DEFAULT_MODEL, router_module.DEFAULT_MODEL),
        # A Claude fallback is listed by its Router alias.
        ("claude-haiku-4-5", None, "ramp-haiku-4-5"),
    ],
)
def test_narrowing_keeps_the_suggested_model_listed(
    cursor_db, router, monkeypatch, fallback, extra, suggested
):
    if extra:
        router["catalog"] = [*_catalog(), _model(extra)]
    _ok(_configure("--cursor-models", "all", root=UNATTENDED))
    _router_fallback(monkeypatch, fallback)
    payload = _agent("--cursor-models", "compact")
    assert payload["suggested_model"] == suggested
    assert payload["models_in_scope"] == len(COMPACT) + 1
    assert suggested in _ai_settings(cursor_db)["userAddedModels"]
    assert suggested not in payload["model_names_removed"]


# === Base aliases ===


@pytest.mark.parametrize(
    ("status", "view", "created", "listed", "unlisted"),
    [
        # Setup makes base aliases only for the Claude models it lists.
        (200, "compact", ["ramp-sonnet-5-5"], {"ramp-opus-5-5"}, {"ramp-haiku-4-5"}),
        # When the lookup fails, an alias Router's listing shows stays listed;
        # nothing names the others yet.
        (503, "all", [], {"ramp-opus-5-5"}, {"ramp-sonnet-5-5"}),
    ],
)
def test_base_aliases_with_the_real_alias_lookup(
    cursor_db, router, monkeypatch, status, view, created, listed, unlisted
):
    router["catalog"] = [*_catalog(), _alias("ramp-opus-5-5", "claude-opus-5-5")]
    monkeypatch.setattr(
        router_module, "_ensure_claude_aliases", _REAL_ENSURE_CLAUDE_ALIASES
    )
    aliases = {"data": [{"name": "ramp-opus-5-5"}]}
    monkeypatch.setattr(
        router_module.router_aliases.httpx,
        "get",
        lambda *a, **k: _Response(status, aliases),
    )
    posts = []
    monkeypatch.setattr(
        router_module.httpx,
        "post",
        lambda url, json=None, **k: posts.append(json["name"]) or _Response(201, {}),
    )
    _ok(_configure("--cursor-models", view, root=UNATTENDED))
    assert posts == created
    names = set(_ai_settings(cursor_db)["userAddedModels"])
    assert listed <= names
    assert not unlisted & names


# === The caller's alias rows in Router's listing ===


@pytest.mark.parametrize(
    ("view", "aliases", "listed", "unlisted"),
    [
        ("all", 2, {"ramp-haiku-4-5", "my-fast", "my-mix"}, {"ramp-opus-5-5-high"}),
        # Recommended keeps an alias when it keeps one of its models.
        ("compact", 1, {"my-fast"}, {"my-mix", "ramp-haiku-4-5", "ramp-opus-5-5-high"}),
    ],
)
def test_alias_rows_follow_their_models_and_are_counted_apart(
    cursor_db, router, view, aliases, listed, unlisted
):
    router["catalog"] = _catalog_with_aliases()
    payload = _agent("--cursor-models", view)
    assert payload["models_in_scope"] == len(ALL_NAMES if view == "all" else COMPACT)
    # Every catalog row, even when the list is narrowed.
    assert payload["models_available"] == len(_catalog_with_aliases())
    assert payload["aliases_in_scope"] == aliases
    names = _ai_settings(cursor_db)["userAddedModels"]
    assert listed <= set(names)
    assert not unlisted & set(names)
    # The base alias row is its Claude model, listed once at most.
    assert names.count("ramp-haiku-4-5") <= 1


def test_a_base_shaped_alias_routing_another_model_stays_an_alias(cursor_db, router):
    router["catalog"] = [*_catalog(), _alias("ramp-haiku-4-5", "kimi-k3")]
    _ok(_configure("--cursor-models", "all", root=UNATTENDED))
    payload = _agent("--cursor-models", "compact")
    # It routes kimi-k3, which is recommended, so Recommended keeps it.
    assert payload["aliases_in_scope"] == 1
    assert "ramp-haiku-4-5" not in payload["model_names_removed"]
    assert "ramp-haiku-4-5" in _ai_settings(cursor_db)["userAddedModels"]


# === Units ===


def test_model_scope_round_trips_and_refuses_bad_json():
    scope = cursor_models.ModelScope(view="compact")
    assert cursor_models.ModelScope.from_json(scope.to_json()) == scope
    for raw in (None, {}, {"view": "some"}):
        assert cursor_models.ModelScope.from_json(raw) is None
    # A provider list saved by a pre-release build reads as its view.
    assert cursor_models.ModelScope.from_json(
        {"view": "all", "makers": ["openai"]}
    ) == cursor_models.ModelScope(view="all")
