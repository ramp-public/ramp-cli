"""The caller's Router aliases, as Cursor setup and its effort variants read them.

One place for what both need: the self-service alias listing (read once a
run and passed along), the ``claude-`` to ``ramp-`` name swap, the check
that an alias candidate routes a catalog model, and the check that a row or
an alias is an effort variant.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import httpx

SELF_SERVICE_PATH = "/self-service/model-aliases"
CLAUDE_PREFIX = "claude-"
BASE_ALIAS_PREFIX = "ramp-"


@dataclass
class AliasListing:
    """What GET /self-service/model-aliases returned."""

    aliases: dict[str, dict]
    count: int
    limit: int | None
    # The key owner's user id, read off the aliases; None when there are none.
    owner: str | None = None

    def record(self, alias: dict) -> None:
        """Note an alias this run created, so later steps see it."""
        name = alias.get("name")
        if not isinstance(name, str):
            return
        if name not in self.aliases:
            self.count += 1
        self.aliases[name] = alias


def list_aliases(origin: str, headers: dict) -> AliasListing | None:
    """Read the key owner's aliases; None when Router can't be asked."""
    try:
        response = httpx.get(
            f"{origin}{SELF_SERVICE_PATH}", headers=headers, timeout=10
        )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None
    aliases = {
        alias["name"]: alias
        for alias in payload["data"]
        if isinstance(alias, dict) and isinstance(alias.get("name"), str)
    }
    count = payload.get("alias_count")
    limit = payload.get("alias_limit")
    return AliasListing(
        aliases=aliases,
        count=count
        if isinstance(count, int) and not isinstance(count, bool)
        else len(aliases),
        limit=limit if isinstance(limit, int) and not isinstance(limit, bool) else None,
        owner=next(
            (
                alias["user_id"]
                for alias in aliases.values()
                if isinstance(alias.get("user_id"), str) and alias["user_id"]
            ),
            None,
        ),
    )


def candidate_models(alias: dict) -> list[str]:
    """A self-service alias's ``candidate_models``, strings only."""
    return [
        value for value in alias.get("candidate_models") or [] if isinstance(value, str)
    ]


# === Names ===


def model_stem(request_name: str) -> str:
    """A model's name without its ``claude-`` prefix; others unchanged.

    Cursor never routes ``claude-`` names through its override, so names
    made for Claude models drop the prefix.
    """
    return request_name.removeprefix(CLAUDE_PREFIX)


def base_alias_name(request_name: str) -> str | None:
    """The ``ramp-*`` name Cursor lists a Claude model under; None for others."""
    if not request_name.startswith(CLAUDE_PREFIX):
        return None
    return f"{BASE_ALIAS_PREFIX}{model_stem(request_name)}"


# === Matching candidates to models ===


def candidate_routes_model(candidate: str, model) -> bool:
    """True when an alias candidate routes exactly this catalog model.

    Candidates read ``provider:provider-model`` with an optional trailing
    ``:service-tier``, or a bare model name, and the stored spelling is the
    provider's own, which for a rolling request name such as
    claude-sonnet-4-5 is the dated snapshot it currently points at
    (claude-sonnet-4-5-20250929). Matching against the model's
    provider_model from ``GET /v1/models`` therefore recognizes the aliases
    setup creates, while still refusing lookalikes that are genuinely
    different catalog entries: the separate model claude-haiku-4-5-20251001
    is not a route for claude-haiku-4-5, whose own provider_model carries no
    date.
    """
    request_name = model.metadata.request_name
    provider_model = model.metadata.provider_model or request_name
    for segment in (candidate, candidate.partition(":")[2]):
        if segment and (
            segment == provider_model
            or segment.startswith(f"{provider_model}:")
            or segment == request_name
            or segment.startswith(f"{request_name}:")
        ):
            return True
    return False


def routed_models(candidates: Iterable[str], models: Iterable) -> list:
    """The catalog models any of ``candidates`` routes, in catalog order."""
    values = list(candidates)
    return [
        model
        for model in models
        if any(candidate_routes_model(value, model) for value in values)
    ]


# === Effort variants ===


def pinned_effort(entry) -> str:
    """The effort an alias pins, or "" when it pins none.

    ``entry`` is a ``GET /v1/models`` row (its ``router_alias``, read into
    ``metadata.pinned_effort``) or a self-service alias (its
    ``reasoning_effort``).
    """
    if isinstance(entry, dict):
        effort = entry.get("reasoning_effort")
    else:
        effort = getattr(getattr(entry, "metadata", None), "pinned_effort", "")
    return effort if isinstance(effort, str) else ""


def is_variant(entry) -> bool:
    """Whether a listing row or alias is an effort variant, not a model."""
    return bool(pinned_effort(entry))
