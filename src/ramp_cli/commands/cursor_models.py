"""Which Router models Cursor setup lists.

Cursor gets Router's recommended (compact) list or every model.

Router also lists the caller's own aliases as rows. An alias isn't a model:
a base ``ramp-*`` alias row is the Claude model it was made for, a pinned
effort variant is left to the effort-variant setup, and any other alias
follows the catalog rows its candidates name for the recommended list.
Counts never include alias rows.

This module holds the choice and the filtering; the prompts and the command
output live with the commands in ``router.py``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ramp_cli.commands import router_aliases
from ramp_cli.commands.router_aliases import base_alias_name

VIEW_COMPACT = "compact"
VIEW_ALL = "all"


@dataclass(frozen=True)
class ModelScope:
    """Which catalog models Cursor lists: the recommended list, or all."""

    view: str = VIEW_ALL

    def to_json(self) -> dict:
        return {"view": self.view}

    @classmethod
    def from_json(cls, raw: object) -> ModelScope | None:
        if not isinstance(raw, dict) or raw.get("view") not in (
            VIEW_COMPACT,
            VIEW_ALL,
        ):
            return None
        return cls(view=raw["view"])

    def describe(self) -> str:
        if self.view == VIEW_COMPACT:
            return "recommended models"
        return "all available models"


def is_alias(model) -> bool:
    """Whether ``model`` is one of the caller's alias rows."""
    return model.metadata.alias_candidates is not None


def models_only(models: Iterable) -> list:
    return [model for model in models if not is_alias(model)]


def alias_targets(alias, models: Iterable) -> list:
    """The catalog rows an alias's candidates name."""
    return router_aliases.routed_models(
        alias.metadata.alias_candidates or (), models_only(models)
    )


def base_aliases(catalog: list) -> dict[str, str]:
    """The base ``ramp-*`` alias rows in Router's listing, mapped to the
    Claude model each one routes: ``{"ramp-opus-5-5": "claude-opus-5-5"}``.

    A row counts only when its name is the Claude model's ``ramp-`` name and
    every candidate routes that model. An alias the user gave such a name
    but pointed elsewhere stays an alias of its own.
    """
    models = models_only(catalog)
    found: dict[str, str] = {}
    for alias in catalog:
        candidates = alias.metadata.alias_candidates
        if not candidates or router_aliases.is_variant(alias):
            continue
        for model in models:
            name = base_alias_name(model.metadata.request_name)
            if name in (alias.id, alias.metadata.request_name) and all(
                router_aliases.candidate_routes_model(candidate, model)
                for candidate in candidates
            ):
                found[alias.id] = model.metadata.request_name
                break
    return found


def prepare(catalog: list) -> tuple[list, list[str]]:
    """(the rows Cursor's list draws from, alias names it must leave alone).

    Drops base ``ramp-*`` alias rows (``base_aliases``), since their Claude
    row already lists that name, and pinned effort variants, which stay
    where they are.
    """
    namesakes = base_aliases(catalog)
    rows: list = []
    untouched: list[str] = []
    for model in catalog:
        if not is_alias(model):
            rows.append(model)
        elif router_aliases.is_variant(model):
            untouched.append(model.id)
        elif model.id not in namesakes:
            rows.append(model)
    return rows, untouched


def in_scope(models: list, scope: ModelScope, compact_ids: set[str] | None) -> list:
    """The catalog models ``scope`` keeps, in catalog order."""
    if scope.view != VIEW_COMPACT or compact_ids is None:
        return list(models)
    return [
        model
        for model in models
        if model.id in compact_ids
        or (
            is_alias(model)
            and any(target.id in compact_ids for target in alias_targets(model, models))
        )
    ]


def cursor_names(models: Iterable) -> list[str]:
    """Every name Cursor lists for ``models``, in catalog order.

    A Claude model counts under its ``ramp-*`` name whether or not Router
    has that alias yet, so a run that couldn't create it never takes the
    name back out of Cursor.
    """
    names: list[str] = []
    for model in models:
        request_name = model.metadata.request_name
        name = base_alias_name(request_name) or request_name
        if name and name not in names:
            names.append(name)
    return names
