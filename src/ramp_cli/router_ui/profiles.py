"""Routing V2 preset and model-selection rules shared by the TUI editors."""

import math
import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass

from ramp_cli.commands.router_user_strategies import ProfileDraft

COST = "cost_efficient_routing_enabled"
PROVIDER = "provider_routing_preference"
SWITCHYARD = "switchyard_routing_enabled"
AUTO = "auto_routing_enabled"
BENCHMARK = "benchmark_alias"
EXPERIMENTAL = "coding-agents-experimental"
CODING = "coding-agents"
CODING_PRESETS = frozenset((CODING, EXPERIMENTAL))
PROVIDERS = (
    ("Account default", ""),
    ("Cheapest", "cost_only"),
    ("Prefer cheaper", "favor_cost"),
    ("Prefer faster", "favor_throughput"),
    ("Fastest", "throughput_only"),
)


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    slug: str
    controls: tuple[str, ...]
    provider: str | None = None


# Match Router's profile-presets.ts. Provenance is immutable after creation.
PRESETS = (
    Preset(
        "production-agents",
        "General Purpose Agents",
        "general-agent",
        (COST, PROVIDER),
        "favor_cost",
    ),
    Preset("low-latency", "Low latency", "low-latency", (PROVIDER,), "throughput_only"),
    Preset(
        "coding-agents",
        "Coding agents",
        "coding-agents",
        (COST, PROVIDER),
        "favor_cost",
    ),
    Preset(
        EXPERIMENTAL, "Coding agents (Experimental)", EXPERIMENTAL, (SWITCHYARD, AUTO)
    ),
    Preset("automations", "Automations", "automations", (COST, PROVIDER), "cost_only"),
    Preset(
        "knowledge-work",
        "Research & analysis",
        "research-analysis",
        (COST, PROVIDER, BENCHMARK),
        "favor_cost",
    ),
)


def has_switchyard_pair(profile: dict) -> bool:
    settings = profile.get("switchyard_settings") or {}
    return bool(settings.get("efficient_model") and settings.get("capable_model"))


def coding_profile(profiles: list[dict], preset_type: str) -> dict | None:
    return next((p for p in profiles if p.get("preset_type") == preset_type), None)


def coding_draft(profiles: list[dict], preset_type: str) -> ProfileDraft:
    profile = coding_profile(profiles, preset_type)
    if profile is not None:
        return ProfileDraft.from_profile(profile)
    return new_draft(next(p for p in PRESETS if p.key == preset_type), profiles)


def controls_for(profile: dict) -> set[str]:
    if not profile.get("slug"):
        return set()
    preset = next((p for p in PRESETS if p.key == profile.get("preset_type")), None)
    controls = set(
        preset.controls if preset else (COST, PROVIDER, SWITCHYARD, BENCHMARK)
    )
    controls.update(
        field
        for field in (COST, PROVIDER, SWITCHYARD, AUTO, BENCHMARK)
        if profile.get(field)
    )
    if has_switchyard_pair(profile):
        controls.add(SWITCHYARD)
    return controls


def unique(base: str, taken: list[str], separator: str) -> str:
    existing = {value.strip().casefold() for value in taken}
    value, suffix = base, 2
    while value.casefold() in existing:
        value = f"{base}{separator}{suffix}"
        suffix += 1
    return value


def slug_from_name(name: str) -> str:
    normalized = "".join(
        c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c)
    )
    return re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")[:64].rstrip("-")


def new_draft(preset: Preset, profiles: list[dict]) -> ProfileDraft:
    draft = ProfileDraft.new()
    draft.name = unique(preset.label, [p["name"] for p in profiles], " ")
    draft.slug = unique(preset.slug, [p.get("slug") or "" for p in profiles], "-")
    draft.preset_type = preset.key
    draft.settings = {
        COST: COST in preset.controls,
        PROVIDER: preset.provider,
        SWITCHYARD: False,
        "switchyard_settings": {
            "efficient_model": None,
            "capable_model": None,
            "effort": None,
            "sensitivity_config_name": None,
        },
        AUTO: AUTO in preset.controls,
        "auto_routing_settings": None,
        "shadow_models_enabled": False,
        "shadow_settings": None,
    }
    return draft


# Router's reviewed efficient pool, intersected with the signed-in user's inventory.
EFFICIENT_MODELS = {
    "deepseek-v4-flash",
    "deepseek-v4-pro-0813",
    "glm-5p3-flash",
    "kimi-k2p7-code",
    "openai:gpt-5.6-luna",
    "openai:gpt-6-luna",
}


def model_value(model: dict, kind: str) -> str:
    concrete = f"{model['provider']}:{model['provider_model']}"
    if kind == "auto":
        return concrete
    route = next(
        (
            r.get("route")
            for r in model.get("requestable_models", [])
            if r.get("route") and r.get("name") == r["route"]
        ),
        None,
    )
    return (
        route
        if route and (kind == "capable" or route in EFFICIENT_MODELS)
        else concrete
    )


def finite_price(value) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return math.nan


def model_options(inventory: dict, kind: str) -> list[tuple[str, str]]:
    representatives = {}
    for model in inventory.get("data", []):
        key = (
            model.get("public_model_id")
            or model.get("id")
            or model_value(model, "auto")
        )
        if key not in representatives or model.get("id") == model.get(
            "public_model_representative"
        ):
            representatives[key] = model
    choices = {}
    for model in representatives.values():
        pricing = model.get("pricing") or {}
        if (
            model.get("status") != "active"
            or model.get("is_date_pinned")
            or not all(
                math.isfinite(finite_price(pricing.get(field)))
                for field in ("input_per_1m", "output_per_1m")
            )
        ):
            continue
        value = model_value(model, kind)
        if kind == "auto":
            if model.get("provider") == "typesafe" or (
                model.get("provider") == "modal"
                and model.get("provider_model") in ("cygnet", "modal/cygnet")
            ):
                continue
        else:
            if not {"tools", "reasoning"} <= set(model.get("capabilities") or []):
                continue
            if kind == "efficient" and value not in EFFICIENT_MODELS:
                continue
            if (
                kind == "capable"
                and "surfaces" in model
                and "responses" not in model["surfaces"]
            ):
                continue
        choices[value] = f"{model.get('display_name') or value} · {value}"
    return [(label, value) for value, label in choices.items()]


def pair_error(inventory: dict, efficient: str, capable: str) -> str | None:
    def entries(value):
        models = inventory.get("data", [])
        concrete = [m for m in models if model_value(m, "auto") == value]
        routed = [
            m
            for m in models
            if any(r.get("route") == value for r in m.get("requestable_models", []))
        ]
        return concrete or sorted(
            routed,
            key=lambda m: (
                not any(r.get("name") == value for r in m.get("requestable_models", []))
            ),
        )

    low, high = entries(efficient), entries(capable)
    if not low or not high:
        return None  # Unknown inventory entries are validated by Router on save.
    if {model_value(m, "auto") for m in low} & {model_value(m, "auto") for m in high}:
        return "Choose different efficient and frontier models."
    price = finite_price(
        (inventory.get("switchyard_class_gate_output_per_1m") or {}).get(
            efficient, low[0]["pricing"]["output_per_1m"]
        )
    )
    if math.isfinite(price) and not all(
        finite_price(m["pricing"]["output_per_1m"]) >= 2 * price for m in high
    ):
        return "The frontier model must cost at least 2× the efficient model on output tokens."
    return None


def switchyard_settings(
    settings: dict,
    *,
    on: bool,
    efficient: str | None,
    capable: str | None,
    sensitivity: str,
) -> dict:
    result = deepcopy(settings.get("switchyard_settings") or {})
    result.update(
        efficient_model=efficient if on else None,
        capable_model=capable if on else None,
        sensitivity_config_name=sensitivity,
    )
    result.setdefault("effort", None)
    if not on or efficient != (settings.get("switchyard_settings") or {}).get(
        "efficient_model"
    ):
        result["effort"] = None
    return result
