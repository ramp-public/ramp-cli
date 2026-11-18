"""Effort-variant names for Cursor: ``ramp-<model>-<effort>``.

Cursor's effort picker has no effect on models that go through Router: they
reach Router with no effort, or always medium, but with the model name
unchanged. An effort variant is a self-service Router alias that routes one
model with Router pinning a higher effort on every request, so Cursor's
picker gets one name per effort.

Variants are offered for Claude models in Cursor's list and for GPT models
on Router's recommended list, at high and xhigh only. A variant follows its
base name: it goes only when the user unticks it, asks for none, its model
leaves Router's catalog, or the run takes its base name out of Cursor.
Anything else keeps it and says why.

Each machine keeps a receipt of the variants it listed, keyed by Router and
key owner; setup and unconfigure delete only what that receipt says this
machine created, so one laptop never breaks another's Cursor, and
``ramp router cursor prune-variants`` handles what is left behind.

Setup decides everything (the selection, the deletes and creates, and
whether they fit Router's alias limit) before anything is written; this
module holds that planning and the Router calls, while the prompts and the
command output live with the commands in ``router.py``.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import click
import httpx

from ramp_cli.commands import router_aliases
from ramp_cli.commands.router_aliases import AliasListing
from ramp_cli.config.settings import config_dir

# Router's own ladder order.
_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# The only levels a variant pins: higher effort. Lower levels aren't worth a
# name, and max is the slowest and costliest, which nobody asked for.
OFFERED_EFFORTS = ("high", "xhigh")
RECEIPT_FILENAME = "router-cursor-variants.json"
# What Router stamps on aliases created through the self-service surface.
SELF_SERVICE_ORIGIN = "self_service"
_ALIAS_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_UNSAFE_NAME_CHARACTERS = re.compile(r"[^a-z0-9._-]")
_MANAGED_KEY = "api_key_managed_by_workspace_admin"
_NAME_CONFLICT = "model_alias_name_conflict"
_REFERENCED = "model_alias_referenced_by_routing_profile"

MANAGED_KEY_NOTE = (
    "This key is managed by your company, so Ramp can't create or remove "
    "effort variants with it."
)
UNSUPPORTED_NOTE = (
    "This Router doesn't support effort variants yet, so none were changed; "
    "the rest of setup is unaffected."
)


def family(model) -> str | None:
    """``claude`` or ``gpt`` for the models that get effort variants."""
    name = model.metadata.request_name
    if name.startswith("claude-"):
        return "claude"
    # gpt-oss is open-weight and served by other hosts, not OpenAI's GPT line.
    if name.startswith("gpt-") and not name.startswith("gpt-oss"):
        return "gpt"
    return None


def variant_stem(model) -> str:
    """The ``<model>`` part of a variant name, the same on every run.

    Claude drops its claude- prefix, as its base alias does, because Cursor
    refuses to route claude- names. GPT keeps its id: Router alias names allow
    a-z, 0-9, ".", "_" and "-", so an id such as gpt-5.6-sol is already
    valid; anything else is replaced by "-".
    """
    return _UNSAFE_NAME_CHARACTERS.sub(
        "-", router_aliases.model_stem(model.metadata.request_name.lower())
    )


def variant_name(model, effort: str) -> str:
    return f"ramp-{variant_stem(model)}-{effort}"


def effort_sort_key(effort: str) -> tuple[int, str]:
    try:
        return (_EFFORT_ORDER.index(effort), effort)
    except ValueError:
        return (len(_EFFORT_ORDER), effort)


def _rank(effort: str | None) -> int:
    return _EFFORT_ORDER.index(effort) if effort in _EFFORT_ORDER else -1


def advertised_efforts(model) -> tuple[str, ...]:
    return tuple(effort for effort, _ in model.metadata.efforts if effort)


def effective_default(model) -> str | None:
    """The level the model runs at when no effort is sent.

    The catalog's default, when the model accepts it. Some rows name a
    default they don't accept (an older gpt-5-pro row says ``none`` but takes
    only ``high``); the lowest level they do accept is what they run at.
    """
    advertised = advertised_efforts(model)
    default = model.metadata.default_effort or None
    if default in advertised or not advertised:
        return default
    known = [effort for effort in advertised if effort in _EFFORT_ORDER]
    return min(known, key=_EFFORT_ORDER.index) if known else default


def offered_efforts(model, pinnable: Iterable[str]) -> tuple[str, ...]:
    """Levels a variant may pin: high and xhigh, above the model's default,
    among those Router says can be pinned (advertised by every candidate,
    within the company's effort limit, and not a level the provider adapter
    would send as a lower one). A model whose default is high gets xhigh."""
    levels = tuple(pinnable)
    default = _rank(effective_default(model))
    return tuple(
        effort
        for effort in OFFERED_EFFORTS
        if effort in levels
        and _rank(effort) > default
        and _ALIAS_NAME_PATTERN.fullmatch(variant_name(model, effort))
    )


def may_have_variants(model) -> bool:
    """Whether the catalog makes the model worth asking Router about."""
    return family(model) is not None and bool(
        offered_efforts(model, advertised_efforts(model))
    )


@dataclass(frozen=True)
class Offer:
    """One effort-variant name setup can add."""

    model: object
    effort: str
    # What Router expands the model to as alias candidates, when it said.
    candidates: tuple[str, ...] | None = None

    @property
    def name(self) -> str:
        return variant_name(self.model, self.effort)

    @property
    def request_name(self) -> str:
        return self.model.metadata.request_name


LEVELS_OK = "ok"
# An older Router has no reasoning-efforts route (404 or 405): it predates
# pinned efforts, so it can't hold variants either.
LEVELS_UNSUPPORTED = "unsupported"
# Router couldn't be asked this time.
LEVELS_UNAVAILABLE = "unavailable"


@dataclass
class EffortLevels:
    """What Router says each model may pin, from
    GET /self-service/model-aliases/reasoning-efforts."""

    pinnable: dict[str, tuple[str, ...]] = field(default_factory=dict)
    ceilings: dict[str, str | None] = field(default_factory=dict)
    # The provider candidates a bare name expands to, as POST stores them.
    candidates: dict[str, tuple[str, ...]] = field(default_factory=dict)
    status: str = LEVELS_OK

    def for_model(self, model) -> tuple[str, ...]:
        return self.pinnable.get(model.metadata.request_name, ())


def eligible(model, recommended_ids: set[str] | None) -> bool:
    """Claude models in scope, and GPT models on Router's recommended list.

    Cursor's own GPT effort picker never reaches Router (every choice arrives
    as medium), so GPT variants are worth a name only for the GPT models
    Router recommends; listing them for every GPT model would bloat the
    picker and the alias budget.
    """
    kind = family(model)
    if kind == "claude":
        return True
    return kind == "gpt" and recommended_ids is not None and model.id in recommended_ids


def namespace_offers(models: Iterable) -> list[Offer]:
    """Every variant name the models could have, whatever Router allows:
    the names setup keeps for variants."""
    return [
        Offer(model, effort)
        for model in models
        if family(model) is not None
        for effort in OFFERED_EFFORTS
    ]


def variant_offers(
    models: Iterable, recommended_ids: set[str] | None, levels: EffortLevels
) -> list[Offer]:
    """Every name setup can offer, in catalog order, lowest effort first.

    Only levels Router says can be pinned: when it couldn't say, nothing is
    offered.
    """
    if levels.status != LEVELS_OK:
        return []
    return [
        Offer(model, effort, levels.candidates.get(model.metadata.request_name))
        for model in models
        if eligible(model, recommended_ids)
        for effort in offered_efforts(model, levels.for_model(model))
    ]


def fetch_effort_levels(origin: str, headers: dict, models: Iterable) -> EffortLevels:
    """Ask Router which levels each model may pin; never raises."""
    names = [model.metadata.request_name for model in models]
    if not names:
        return EffortLevels()
    try:
        response = httpx.get(
            f"{origin}{router_aliases.SELF_SERVICE_PATH}/reasoning-efforts",
            headers=headers,
            params=[("model", name) for name in names],
            timeout=10,
        )
    except httpx.HTTPError:
        return EffortLevels(status=LEVELS_UNAVAILABLE)
    if response.status_code in (404, 405):
        return EffortLevels(status=LEVELS_UNSUPPORTED)
    try:
        payload = response.json() if response.status_code == 200 else None
    except ValueError:
        payload = None
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return EffortLevels(status=LEVELS_UNAVAILABLE)
    levels = EffortLevels()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("model"), str):
            continue
        pinnable = row.get("pinnable_efforts")
        if not isinstance(pinnable, list):
            continue
        levels.pinnable[row["model"]] = tuple(
            value for value in pinnable if isinstance(value, str)
        )
        ceiling = row.get("max_reasoning_effort")
        levels.ceilings[row["model"]] = ceiling if isinstance(ceiling, str) else None
        expanded = row.get("candidate_models")
        if isinstance(expanded, list) and expanded:
            levels.candidates[row["model"]] = tuple(
                value for value in expanded if isinstance(value, str)
            )
    return levels


# === Why a variant isn't offered ===

# Short codes for JSON, the picker's label and the "kept" line's reason.
OFF_RECOMMENDED = "off_recommended_list"
RECOMMENDED_UNAVAILABLE = "recommended_list_unavailable"
ABOVE_LIMIT = "above_company_limit"
NOT_PINNABLE = "not_pinnable"
DEFAULT_RAISED = "default_raised"
NOT_LISTED = "not_in_model_list"

_PICKER_LABELS = {
    OFF_RECOMMENDED: "off the recommended list",
    RECOMMENDED_UNAVAILABLE: "recommended list unavailable",
    ABOVE_LIMIT: "above your company's limit",
    NOT_PINNABLE: "Router can't pin it now",
    DEFAULT_RAISED: "no higher than the default now",
    NOT_LISTED: "not in Cursor's model list",
}


@dataclass(frozen=True)
class Unoffered:
    """Why setup doesn't offer a variant now: a code and a sentence."""

    code: str
    reason: str

    def picker_label(self) -> str:
        return f"({_PICKER_LABELS[self.code]}; untick to remove)"


def unoffered(
    model,
    effort: str,
    *,
    in_scope: bool,
    scope_is_recommended: bool,
    recommended_ids: set[str] | None,
    levels: EffortLevels,
    listed: bool = False,
) -> Unoffered:
    """Why this variant isn't offered, for a model in Router's catalog;
    ``listed`` when this machine lists it, so the reason says what changed."""
    label = model.metadata.display_name or model.metadata.request_name
    now = " now" if listed else ""
    off_list = Unoffered(
        OFF_RECOMMENDED,
        f"{label} is no longer on Router's recommended list"
        if listed
        else f"{label} isn't on Router's recommended list",
    )
    if not in_scope:
        if scope_is_recommended:
            return off_list
        return Unoffered(NOT_LISTED, f"{label} isn't in the models Cursor lists")
    if family(model) == "gpt":
        if recommended_ids is None:
            return Unoffered(
                RECOMMENDED_UNAVAILABLE, "Router's recommended list couldn't be read"
            )
        if model.id not in recommended_ids:
            return off_list
    default = effective_default(model)
    if default and _rank(effort) <= _rank(default):
        return Unoffered(DEFAULT_RAISED, f"{label}'s default effort is{now} {default}")
    ceiling = levels.ceilings.get(model.metadata.request_name)
    if ceiling and _rank(effort) > _rank(ceiling):
        return Unoffered(ABOVE_LIMIT, f"your company's effort limit is{now} {ceiling}")
    return Unoffered(NOT_PINNABLE, f"Router can't pin {effort} on {label}{now}")


# === Flags ===


def parse_variant_flags(values: Iterable[str]) -> list[tuple[str, tuple[str, ...]]]:
    """Read ``MODEL=LEVEL[,LEVEL]`` values; checks their shape only.

    A level other than high or xhigh can never be a variant, so it is as
    malformed as a missing ``=``.
    """
    parsed: list[tuple[str, tuple[str, ...]]] = []
    for value in values:
        model, separator, levels = value.partition("=")
        efforts = tuple(
            dict.fromkeys(
                level.strip().lower() for level in levels.split(",") if level.strip()
            )
        )
        if not separator or not model.strip() or not efforts:
            raise click.BadParameter(
                f"{value!r} should look like MODEL=LEVEL[,LEVEL], for example "
                "opus-5-5=high,xhigh.",
                param_hint="'--effort-variants'",
            )
        wrong = [effort for effort in efforts if effort not in OFFERED_EFFORTS]
        if wrong:
            raise click.BadParameter(
                f"{value!r}: variants pin only high or xhigh, not {', '.join(wrong)}.",
                param_hint="'--effort-variants'",
            )
        parsed.append((model.strip().lower(), efforts))
    return parsed


def _matches(token: str, model) -> bool:
    name = model.metadata.request_name.lower()
    stem = token.removeprefix("ramp-")
    return token == name or stem == name or stem == variant_stem(model)


def flag_models(parsed: list[tuple[str, tuple[str, ...]]], catalog: list) -> list:
    """The catalog model each flag names; raises BadParameter for a model
    Router doesn't have or one that can never have variants."""
    found = []
    for token, efforts in parsed:
        model = next((model for model in catalog if _matches(token, model)), None)
        if model is None:
            raise click.BadParameter(
                f"{token!r} is not a Router model.", param_hint="'--effort-variants'"
            )
        if family(model) is None:
            raise click.BadParameter(
                f"{model.metadata.request_name} is not a Claude or GPT model, so it "
                "has no effort variants.",
                param_hint="'--effort-variants'",
            )
        found.append((model, efforts))
    return found


def resolve_variant_flags(
    found: list,
    offers: list[Offer],
    mine: dict[str, dict],
    why: Callable,
    present: Iterable[str] = (),
    leaving: Iterable[str] = (),
) -> tuple[set[str], list[tuple[str, Unoffered]]]:
    """(the names the flags ask for, the ones skipped and why).

    A name setup offers is taken; so is one this machine already lists,
    even if it isn't offered now, while Router still has it (``present``:
    the listing's alias names) and its base name stays in Cursor (not in
    ``leaving``). Any other level is skipped with a note, and
    the run goes on (and then exits 1, as it couldn't be delivered).
    ``why(model, effort)`` says why a name isn't offered.
    """
    offered = {offer.name for offer in offers}
    on_router = set(present)
    gone = set(leaving)
    chosen: set[str] = set()
    skipped: list[tuple[str, Unoffered]] = []
    for model, efforts in found:
        for effort in efforts:
            name = variant_name(model, effort)
            if name in offered or (
                name in mine and name in on_router and name not in gone
            ):
                chosen.add(name)
            elif not any(name == entry for entry, _ in skipped):
                skipped.append((name, why(model, effort)))
    return chosen, skipped


# === Router's self-service aliases ===


def edited_variants(listing: AliasListing, receipt: dict, origin: str) -> list[str]:
    """This machine's names on this Router and account whose pin a dashboard
    edit cleared, not yet reported by prune; prune leaves them where they
    are. A listing from a Router without pinned efforts has none (see
    ``predates_variants``)."""
    if predates_variants(listing):
        return []
    return sorted(
        name
        for name, entry in account_variants(receipt, origin, listing.owner).items()
        if not entry.get("edited")
        and (alias := listing.aliases.get(name)) is not None
        and pin_cleared(alias)
    )


def mark_edited(
    receipt: dict, origin: str, owner: str | None, names: Iterable[str]
) -> None:
    """Note that prune reported ``names`` as edited in the dashboard. They
    stay in the receipt until setup has taken them out of Cursor's list."""
    account = find_account(receipt, origin, owner)
    if account is not None:
        for name in names:
            entry = account["variants"].get(name)
            if isinstance(entry, dict):
                entry["edited"] = True


def predates_variants(listing: AliasListing) -> bool:
    """Whether the listing comes from a Router without pinned efforts.

    Such a Router (an older one, or one rolled back) sends no ``origin`` or
    ``reasoning_effort`` on its aliases. Read as "no variants exist", it
    would wipe the receipt, so it reads as unsupported instead.
    """
    return any(
        "origin" not in alias or "reasoning_effort" not in alias
        for alias in listing.aliases.values()
    )


def is_cli_variant(alias: dict) -> bool:
    """Whether the alias looks like one setup made: self-service, with a
    pinned effort that its ``ramp-<model>-<effort>`` name spells out."""
    name = alias.get("name")
    effort = router_aliases.pinned_effort(alias)
    return (
        router_aliases.is_variant(alias)
        and alias.get("origin") == SELF_SERVICE_ORIGIN
        and isinstance(name, str)
        and name.startswith("ramp-")
        and name.endswith(f"-{effort}")
        and len(name) > len(f"ramp--{effort}")
    )


def _routes_offer(alias: dict, offer: Offer) -> bool:
    """Whether the alias routes exactly the variant's model.

    A bare name can expand to several provider candidates; Router says what
    that expansion is (the same one POST stores), and the alias must hold
    exactly it, or else route only this model.
    """
    values = router_aliases.candidate_models(alias)
    if not values:
        return False
    if offer.candidates:
        return set(values) == set(offer.candidates)
    return all(
        router_aliases.candidate_routes_model(value, offer.model) for value in values
    )


def alias_matches(alias: dict, offer: Offer) -> bool:
    """Whether an existing alias is exactly this variant: the model's own
    candidates, and Router pinning the same effort."""
    return router_aliases.pinned_effort(alias) == offer.effort and _routes_offer(
        alias, offer
    )


def mismatch_reason(alias: dict, offer: Offer) -> str:
    """Why an existing alias isn't the variant its name promises."""
    effort = router_aliases.pinned_effort(alias)
    if not effort:
        return "pins no effort, so it runs at the model's default"
    if effort != offer.effort:
        return f"pins {effort}"
    expected = ", ".join(offer.candidates or (offer.request_name,))
    values = router_aliases.candidate_models(alias)
    return f"routes {', '.join(values) or 'nothing'}, not {expected}"


def error_code(response: httpx.Response) -> str | None:
    """Router's error code: ``{"error": {"code": ...}}``."""
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        return error["code"]
    return None


DELETED = "deleted"
NOT_SELF_SERVICE = "not_self_service"
REFERENCED = "referenced"
MANAGED = "managed_key"
UNSUPPORTED = "unsupported"
FAILED = "failed"


def delete_alias(origin: str, headers: dict, name: str) -> str:
    """DELETE one alias. An older Router has no such route (404/405 without
    Router's alias codes), which reads as unsupported, never as an error."""
    try:
        response = httpx.delete(
            f"{origin}{router_aliases.SELF_SERVICE_PATH}/{name}",
            headers=headers,
            timeout=10,
        )
    except httpx.HTTPError:
        return FAILED
    if response.status_code in (200, 202, 204):
        return DELETED
    code = error_code(response)
    if response.status_code == 404 and code == "model_alias_not_found":
        return DELETED
    if response.status_code == 409:
        return {
            "model_alias_not_self_service": NOT_SELF_SERVICE,
            _REFERENCED: REFERENCED,
            _MANAGED_KEY: MANAGED,
        }.get(code or "", FAILED)
    if response.status_code in (404, 405):
        return UNSUPPORTED
    return FAILED


def deletable(entry: dict, alias: dict) -> bool:
    """Whether this machine may delete the alias: it made it, and Router
    still stamps it self-service with the effort it was made with."""
    return (
        bool(entry.get("created"))
        and alias.get("origin") == SELF_SERVICE_ORIGIN
        and router_aliases.pinned_effort(alias) == entry.get("effort")
    )


# === Planning and applying a selection ===


@dataclass
class VariantOutcome:
    """What setup decided for effort variants, and then what it did."""

    # Variant name -> (request name, effort) for every name Cursor lists.
    advertised: dict[str, tuple[str, str]] = field(default_factory=dict)
    # The receipt entries this account keeps (more are added as created).
    entries: dict[str, dict] = field(default_factory=dict)
    to_delete: list[str] = field(default_factory=list)
    to_create: list[Offer] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    # Deleted from Router, and so out of Cursor.
    removed: list[str] = field(default_factory=list)
    # (name, code) for names this machine listed that leave Cursor but that
    # this run doesn't delete: another machine made it, it was changed on
    # Router's dashboard, or it was already deleted there.
    unlisted: list[tuple[str, str]] = field(default_factory=list)
    # (name, why) for names already taken by an alias that isn't the variant.
    conflicts: list[tuple[str, str]] = field(default_factory=list)
    # Conflicting names the user asked for (not just found in Router's list).
    asked_conflicts: set[str] = field(default_factory=set)
    # This machine's names whose pin a dashboard edit cleared (conflicts too).
    edited: list[str] = field(default_factory=list)
    # Variants kept though not offered now, and flag names skipped.
    retained: list[tuple[str, Unoffered]] = field(default_factory=list)
    skipped: list[tuple[str, Unoffered]] = field(default_factory=list)
    # Set when the selection didn't fit Router's alias limit.
    over_budget: dict | None = None
    over_ceiling: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    # Deselected names a routing profile still uses: they stay on Router.
    referenced: list[str] = field(default_factory=list)
    unsupported: bool = False
    managed_key: bool = False
    # False when Router's aliases or levels couldn't be read: nothing changed.
    listed: bool = True
    levels_unavailable: bool = False

    def undelivered(self) -> bool:
        """Whether something asked for couldn't be done. A flag level that
        isn't offered now, and that this machine doesn't list, is one."""
        return bool(
            self.asked_conflicts
            or self.skipped
            or self.over_budget
            or self.over_ceiling
            or self.rejected
            or self.failed
            or self.referenced
            or self.unsupported
            or self.managed_key
            or not self.listed
            or self.levels_unavailable
        )

    def to_json(self, also_listed: Iterable[str] = ()) -> dict:
        payload: dict = {
            "names": sorted({*self.advertised, *also_listed}),
            "created": self.created,
            "removed": self.removed,
        }
        if self.unlisted:
            payload["unlisted"] = [
                {"name": name, "reason": code} for name, code in self.unlisted
            ]
        if self.conflicts:
            payload["conflicts"] = [
                {"name": name, "reason": reason} for name, reason in self.conflicts
            ]
        for key, rows in (("kept", self.retained), ("skipped", self.skipped)):
            if rows:
                payload[key] = [
                    {"name": name, "reason": why.code} for name, why in rows
                ]
        for key in ("over_budget", "over_ceiling", "rejected", "failed", "referenced"):
            if getattr(self, key):
                payload[key] = getattr(self, key)
        for key in ("unsupported", "managed_key", "levels_unavailable"):
            if getattr(self, key):
                payload[key] = True
        if not self.listed:
            payload["router_unreachable"] = True
        return payload

    def note_lines(self, strategies_url: str, *, flags: bool = False) -> list[str]:
        lines: list[str] = []
        if self.removed:
            lines.append(
                "Removed from Cursor and your Router account: "
                + ", ".join(self.removed)
            )
        for name, code in self.unlisted:
            lines.append(f"Removed {name} from Cursor{UNLISTED_REASONS[code]}")
        if self.unsupported:
            lines.append(UNSUPPORTED_NOTE)
        if self.managed_key:
            lines.append(MANAGED_KEY_NOTE)
        if not self.listed:
            lines.append(
                "Router's alias list couldn't be read, so effort variants were "
                "left as they were."
            )
        if self.levels_unavailable:
            lines.append(
                "Router's effort levels couldn't be read, so effort variants were "
                "left as they were."
            )
        if self.over_budget:
            # A flag run that doesn't fit stops before this; a picked one
            # goes ahead without variant changes.
            lines.append(
                budget_message(self.over_budget, strategies_url, interactive=True)
            )
        for name, reason in self.conflicts:
            if name in self.edited:
                lines.append(
                    f"{name} was edited in the Router dashboard and no longer "
                    f"pins an effort, so Cursor won't list it. Delete it at "
                    f"{strategies_url} or rename it."
                )
                continue
            lines.append(
                f"Left out {name}: Router already has an alias with that "
                f"name that {reason}. Delete or rename it at {strategies_url}, "
                "then run this again and pick it."
            )
        how = (
            "run setup again without it in --effort-variants"
            if flags
            else "run setup again and untick it"
        )
        for name, why in self.retained:
            lines.append(
                f"Kept {name}: {why.reason}, but the name still works. To remove "
                f"it, {how}."
            )
        for name, why in self.skipped:
            lines.append(f"Skipped {name}: {why.reason}.")
        for name in self.referenced:
            lines.append(
                f"{name} stays on your Router account: a routing profile uses it."
            )
        if self.over_ceiling:
            lines.append(
                "Your company's effort limit doesn't allow: "
                + ", ".join(self.over_ceiling)
            )
        if self.rejected:
            lines.append(
                "Router didn't accept these effort levels: " + ", ".join(self.rejected)
            )
        if self.failed:
            lines.append(
                "These effort variants couldn't be changed; run this again to "
                "retry: " + ", ".join(self.failed)
            )
        return lines


# A dashboard edit that changes an alias's models clears its pin (router#5823):
# the name no longer pins the effort it spells out.
EDITED_IN_DASHBOARD = "was edited in the Router dashboard and no longer pins an effort"


def pin_cleared(alias: dict) -> bool:
    """Whether Router no longer pins any effort on this alias."""
    return not router_aliases.pinned_effort(alias)


# How a name this run takes out of Cursor, without deleting it, says why.
UNLISTED_ANOTHER_MACHINE = "another_machine"
UNLISTED_EDITED = "edited"
UNLISTED_GONE = "deleted_on_router"
UNLISTED_REASONS = {
    UNLISTED_ANOTHER_MACHINE: "; it stays on your Router account: another "
    "machine created it.",
    UNLISTED_EDITED: "; it stays on your Router account: it was changed on "
    "Router's dashboard.",
    UNLISTED_GONE: ": it was already deleted from your Router account.",
}


def budget_message(budget: dict, strategies_url: str, interactive: bool) -> str:
    changed = (
        "No effort variants were changed." if interactive else ("Nothing was changed.")
    )
    return (
        f"These effort variants don't fit: Router allows {budget['alias_limit']} "
        f"aliases, you have {budget['alias_count']}, and this run needs "
        f"{budget['needed']}. {changed} Remove aliases you don't use at "
        f"{strategies_url}, or ask for fewer names."
    )


def saved_selection(
    listing: AliasListing, offers: list[Offer], mine: dict[str, dict]
) -> set[str]:
    """The offered variants a re-run starts from: every name this machine's
    receipt lists whose alias still matches exactly. A dashboard save that
    changed nothing clears the self-service stamp but keeps the name."""
    offered = {offer.name: offer for offer in offers}
    return {
        name
        for name, entry in mine.items()
        if name in offered
        and entry_in_cursor(entry)
        and (alias := listing.aliases.get(name)) is not None
        and alias_matches(alias, offered[name])
    }


@dataclass(frozen=True)
class Kept:
    """A receipt variant this run doesn't offer, and why, with the offer it
    would be to check the alias against."""

    offer: Offer
    why: Unoffered


def plan_variants(
    listing: AliasListing,
    offers: list[Offer],
    mine: dict[str, dict],
    *,
    desired: set[str] | None,
    kept: dict[str, Kept],
    follow_out: set[str],
    listed: Iterable[str] = (),
    namespace: Iterable[Offer] = (),
) -> VariantOutcome:
    """Decide the deletes and creates for a selection; changes nothing.

    ``desired`` is an explicit selection (the picker's answer, the flags, or
    nothing for --no-effort-variants); None keeps what this machine lists.
    ``mine`` is this machine's receipt for this Router and account; only
    names it records as created here are ever deleted (see ``deletable``).
    ``kept`` are its names that aren't offered now but stay unless the
    selection leaves them out; ``follow_out`` are its names whose model left
    Router's catalog or whose base name this run takes out of Cursor.
    An existing alias is reused only when it is exactly the variant;
    anything else under that name is a conflict and stays out of Cursor,
    whether it was picked or Router lists it as a model (``listed``).
    ``namespace`` is every variant name the listed models could have, offered
    or not: Router's alias under one that doesn't pin the effort it spells
    out is a conflict too, whatever the selection. A name that left Cursor
    while its delete didn't happen is deleted unless it is asked for again.
    """
    out = VariantOutcome()
    offered = {offer.name: offer for offer in offers}
    explicit = desired is not None
    wanted = desired or set()
    for name, entry in mine.items():
        alias = listing.aliases.get(name)
        if alias is None:
            # Gone from Router: it leaves the receipt and Cursor, unless it
            # is asked for again (then it is made again below).
            if name not in wanted and entry_in_cursor(entry):
                out.unlisted.append((name, UNLISTED_GONE))
            continue
        if pin_cleared(alias):
            # Edited in the dashboard: a conflict, never deleted or listed,
            # and out of the receipt once reported.
            out.edited.append(name)
            out.conflicts.append((name, EDITED_IN_DASHBOARD))
            if name in wanted:
                out.asked_conflicts.add(name)
            continue
        if (
            name in follow_out
            or (explicit and name not in wanted)
            or (not entry_in_cursor(entry) and name not in wanted)
        ):
            if deletable(entry, alias):
                out.to_delete.append(name)
            else:
                # Another machine made it, or it was changed on the
                # dashboard: it stays on Router and only leaves Cursor.
                out.unlisted.append(
                    (
                        name,
                        UNLISTED_EDITED
                        if entry.get("created")
                        else UNLISTED_ANOTHER_MACHINE,
                    )
                )
            continue
        offer = offered.get(name) or (kept[name].offer if name in kept else None)
        if offer is None:
            continue
        if alias_matches(alias, offer):
            out.entries[name] = listed_entry(entry)
            out.advertised[name] = (offer.request_name, offer.effort)
            if name in kept:
                out.retained.append((name, kept[name].why))
        elif explicit:
            out.conflicts.append((name, mismatch_reason(alias, offer)))
            out.asked_conflicts.add(name)
    for offer in offers:
        if offer.name not in wanted or (
            offer.name in mine and offer.name in listing.aliases
        ):
            continue
        alias = listing.aliases.get(offer.name)
        if alias is None:
            out.to_create.append(offer)
        elif alias_matches(alias, offer):
            # Another machine's, or made on the dashboard: reused, not owned.
            out.entries[offer.name] = _entry(offer, created=False)
            out.advertised[offer.name] = (offer.request_name, offer.effort)
        else:
            out.conflicts.append((offer.name, mismatch_reason(alias, offer)))
            out.asked_conflicts.add(offer.name)
    listed_names = set(listed)
    taken = {name for name, _ in out.conflicts}
    for offer in offers:
        alias = listing.aliases.get(offer.name)
        if (
            offer.name in listed_names
            and offer.name not in out.advertised
            and offer.name not in taken
            and offer.name not in mine
            and alias is not None
            and not alias_matches(alias, offer)
        ):
            # Router lists the user's own alias under a variant's name as a
            # model: it isn't that variant, so it is a conflict too.
            out.conflicts.append((offer.name, mismatch_reason(alias, offer)))
            taken.add(offer.name)
    for offer in namespace:
        alias = listing.aliases.get(offer.name)
        if (
            offer.name in listed_names
            and offer.name not in offered
            and offer.name not in out.advertised
            and offer.name not in taken
            and offer.name not in mine
            and alias is not None
            and router_aliases.pinned_effort(alias) != offer.effort
        ):
            # Not offered this run (or no variants were asked for), but the
            # name still promises an effort the alias doesn't pin: listed as
            # a plain model, it would mislead.
            out.conflicts.append((offer.name, mismatch_reason(alias, offer)))
            taken.add(offer.name)
    return out


def check_budget(
    out: VariantOutcome,
    listing: AliasListing,
    mine: dict[str, dict],
    base_to_create: int,
) -> None:
    """All or nothing: when the creates don't fit Router's alias limit,
    nothing is created or deleted and every name this machine listed that
    Router still has stays, as it was."""
    if listing.limit is None or not out.to_create:
        return
    needed = listing.count + base_to_create - len(out.to_delete) + len(out.to_create)
    if needed <= listing.limit:
        return
    out.over_budget = {
        "alias_count": listing.count,
        "alias_limit": listing.limit,
        "needed": needed,
        "new_names": [offer.name for offer in out.to_create],
    }
    out.to_create = []
    out.to_delete = []
    out.asked_conflicts = set()
    # Nothing changes, so only names Router no longer has leave Cursor.
    out.unlisted = [
        (name, code) for name, code in out.unlisted if code == UNLISTED_GONE
    ]
    out.entries = {}
    out.advertised = {}
    out.retained = []
    for name, entry in mine.items():
        alias = listing.aliases.get(name)
        if alias is None:
            continue
        out.entries[name] = entry
        if (
            entry_in_cursor(entry)
            and router_aliases.pinned_effort(alias) == entry.get("effort")
            and isinstance(entry.get("model"), str)
        ):
            out.advertised[name] = (entry["model"], entry["effort"])


def apply_variants(
    origin: str, headers: dict, out: VariantOutcome, mine: dict[str, dict]
) -> str | None:
    """Make the planned deletes and creates; returns the key owner a create
    reported, if any. Each failure is recorded, never raised."""
    owner = None
    # A name whose delete didn't happen still leaves Cursor; it stays
    # recorded, as out of Cursor, so the next run or prune deletes it.
    for name in out.to_delete:
        if out.unsupported or out.managed_key:
            out.entries[name] = _left_cursor(mine[name])
            continue
        verdict = delete_alias(origin, headers, name)
        if verdict == DELETED:
            out.removed.append(name)
        elif verdict == REFERENCED:
            out.referenced.append(name)
        elif verdict == MANAGED:
            out.managed_key = True
            out.entries[name] = _left_cursor(mine[name])
        elif verdict == UNSUPPORTED:
            out.unsupported = True
            out.entries[name] = _left_cursor(mine[name])
        elif verdict == FAILED:
            out.failed.append(name)
            out.entries[name] = _left_cursor(mine[name])
        elif verdict == NOT_SELF_SERVICE:
            # Changed on the dashboard since: it is the user's.
            out.unlisted.append((name, UNLISTED_EDITED))
    for offer in out.to_create:
        if out.unsupported or out.managed_key:
            break
        try:
            response = httpx.post(
                f"{origin}{router_aliases.SELF_SERVICE_PATH}",
                headers=headers,
                json={
                    "name": offer.name,
                    "candidate_models": [offer.request_name],
                    "reasoning_effort": offer.effort,
                },
                timeout=10,
            )
        except httpx.HTTPError:
            out.failed.append(offer.name)
            continue
        if response.status_code == 201:
            out.created.append(offer.name)
            out.advertised[offer.name] = (offer.request_name, offer.effort)
            out.entries[offer.name] = _entry(offer, created=True)
            owner = owner or _owner_of(response)
            continue
        code = error_code(response)
        if code == _MANAGED_KEY:
            out.managed_key = True
        elif code == _NAME_CONFLICT:
            out.conflicts.append((offer.name, "was created meanwhile"))
            out.asked_conflicts.add(offer.name)
        elif code == "model_alias_reasoning_effort_exceeds_limit":
            out.over_ceiling.append(offer.name)
        elif code == "invalid_model_alias_reasoning_effort":
            out.rejected.append(offer.name)
        elif response.status_code == 422 and code in (None, "invalid_request"):
            # An older Router forbids unknown fields, so reasoning_effort
            # fails request validation: variants aren't available there.
            out.unsupported = True
        else:
            # Includes model_alias_limit_exceeded: another client took the
            # room after the budget check.
            out.failed.append(offer.name)
    out.to_create = []
    out.to_delete = []
    return owner


def _entry(offer: Offer, *, created: bool) -> dict:
    return {"model": offer.request_name, "effort": offer.effort, "created": created}


def _owner_of(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    owner = payload.get("user_id") if isinstance(payload, dict) else None
    return owner if isinstance(owner, str) and owner else None


def orphaned_variants(listing: AliasListing, receipt: dict, origin: str) -> list[dict]:
    """Setup-looking variants on this account that this machine's Cursor
    doesn't list: made on another machine, left behind by a wiped one, or
    left on this Router when Cursor was moved to another."""
    mine = set(account_variants(receipt, origin, listing.owner, listed_only=True))
    return [
        alias
        for name, alias in sorted(listing.aliases.items())
        if is_cli_variant(alias) and name not in mine
    ]


# === This machine's receipt of the variants it listed ===


def receipt_path() -> Path:
    return config_dir() / RECEIPT_FILENAME


def read_receipt() -> dict:
    """``{"accounts": [{"origin", "owner", "base_url", "in_cursor",
    "variants": {name: {"model", "effort", "created"}}}]}``.

    ``base_url`` is the Router the account's key was used against, to tell
    it from the one Cursor is bound to now; ``in_cursor`` is false once
    Cursor moved to another Router or account and stopped listing them.
    """
    try:
        receipt = json.loads(receipt_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"accounts": []}
    accounts = receipt.get("accounts") if isinstance(receipt, dict) else None
    if not isinstance(accounts, list):
        return {"accounts": []}
    return {
        "accounts": [
            account
            for account in accounts
            if isinstance(account, dict)
            and isinstance(account.get("origin"), str)
            and isinstance(account.get("owner"), str)
            and isinstance(account.get("variants"), dict)
        ]
    }


def write_receipt(receipt: dict) -> None:
    """Save the receipt, or remove it when it lists nothing; best effort."""
    accounts = [account for account in receipt["accounts"] if account["variants"]]
    if not accounts:
        clear_receipt()
        return
    _write_json(receipt_path(), {"version": 1, "accounts": accounts})


def clear_receipt() -> None:
    try:
        receipt_path().unlink(missing_ok=True)
    except OSError:
        pass


def in_cursor(account: dict) -> bool:
    return account.get("in_cursor") is not False


def entry_in_cursor(entry: dict) -> bool:
    """Whether Cursor lists this name: false once it left Cursor's list but
    stays recorded for a delete that hasn't happened yet."""
    return entry.get("in_cursor") is not False


def _left_cursor(entry: dict) -> dict:
    return {**entry, "in_cursor": False}


def listed_entry(entry: dict) -> dict:
    return {key: value for key, value in entry.items() if key != "in_cursor"}


def find_account(receipt: dict, origin: str, owner: str | None) -> dict | None:
    if owner is None:
        return None
    return next(
        (
            account
            for account in receipt["accounts"]
            if account["origin"] == origin and account["owner"] == owner
        ),
        None,
    )


def account_variants(
    receipt: dict, origin: str, owner: str | None, *, listed_only: bool = False
) -> dict[str, dict]:
    """The receipt's variants for one Router and account; with
    ``listed_only``, only while Cursor still lists them."""
    account = find_account(receipt, origin, owner)
    if account is None or (listed_only and not in_cursor(account)):
        return {}
    return {
        name: entry
        for name, entry in account["variants"].items()
        if isinstance(entry, dict) and (not listed_only or entry_in_cursor(entry))
    }


def set_account_variants(
    receipt: dict,
    origin: str,
    owner: str | None,
    variants: dict[str, dict],
    *,
    base_url: str | None = None,
) -> None:
    """Record what Cursor lists for this Router and account."""
    if owner is None:
        return
    previous = find_account(receipt, origin, owner) or {}
    receipt["accounts"] = [
        account for account in receipt["accounts"] if account is not previous
    ]
    account = {"origin": origin, "owner": owner, "variants": variants}
    if base_url or previous.get("base_url"):
        account["base_url"] = base_url or previous["base_url"]
    receipt["accounts"].append(account)


def listed_names(receipt: dict) -> set[str]:
    """Every variant name the receipt says Cursor lists, on any Router."""
    return {
        name
        for account in receipt["accounts"]
        if in_cursor(account)
        for name, entry in account["variants"].items()
        if not isinstance(entry, dict) or entry_in_cursor(entry)
    }


def receipt_names(receipt: dict) -> set[str]:
    """Every variant name the receipt records, listed in Cursor or not."""
    return {name for account in receipt["accounts"] for name in account["variants"]}


def _write_json(path: Path, value: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass
