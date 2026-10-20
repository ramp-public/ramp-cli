"""Routing V2 profile form, with preset-specific strategies."""

import re
from copy import deepcopy

import httpx
from rich.text import Text
from textual import on
from textual.containers import Vertical
from textual.widgets import Checkbox, Input, Label, Select, Static

from ramp_cli.commands.router_user_strategies import (
    AUTO_ROUTING_CHOICES,
    ProfileDraft,
    _validate_name,
)
from ramp_cli.errors import ApiError, RampCLIError
from ramp_cli.router_ui.benchmarks import BenchmarkFields
from ramp_cli.router_ui.pages import FormPage
from ramp_cli.router_ui.profiles import (
    AUTO,
    BENCHMARK,
    COST,
    EXPERIMENTAL,
    PRESETS,
    PROVIDER,
    PROVIDERS,
    SWITCHYARD,
    controls_for,
    has_switchyard_pair,
    model_options,
    new_draft,
    pair_error,
    slug_from_name,
    switchyard_settings,
)
from ramp_cli.router_ui.service import safe_text
from ramp_cli.router_ui.widgets import Button


class ProfilePage(FormPage):
    page_title = "routing profile"

    def __init__(
        self, profiles: list[dict], *, profile: dict | None = None, preset=None
    ):
        super().__init__()
        self.profiles = profiles
        self.profile = profile
        self.draft = (
            ProfileDraft.from_profile(profile)
            if profile
            else new_draft(preset, profiles)
        )
        self.controls = controls_for(profile) if profile else set(preset.controls)
        self.preset = preset or next(
            (p for p in PRESETS if p.key == self.draft.preset_type), None
        )
        self.options: dict = {}
        self.initial: dict = {}
        self.slug_edited = False
        self.suggested_slug = self.draft.slug
        self.uncertain = False
        self.custom_candidates: list[dict] | None = None
        self.benchmark_saved = False

    def content(self):
        settings = self.draft.settings
        yield Static(
            self.preset.label if self.preset else "Custom profile",
            classes="heading",
            markup=False,
        )
        yield Label("Name")
        yield Input(self.draft.name, id="name", max_length=64)
        yield Label("Routing profile slug · immutable after save")
        yield Input(
            self.draft.slug or "",
            id="slug",
            disabled=self.profile is not None,
            max_length=64,
        )
        if self.draft.preset_type == EXPERIMENTAL:
            yield Static(
                "One per account. Exposes switchyard and auto-jev-routing in coding harness model pickers.",
                markup=False,
            )
        if COST in self.controls:
            yield Checkbox(
                "Cost-efficient routing (Flex)",
                value=bool(settings.get(COST)),
                id="cost",
            )
        if PROVIDER in self.controls:
            yield Checkbox(
                "Cross-provider routing",
                value=settings.get(PROVIDER) is not None,
                id="cross-provider",
            )
            with Vertical(id="provider-fields"):
                yield Label("Provider preference")
                yield Select(
                    [(label, value) for label, value in PROVIDERS if value],
                    value=settings.get(PROVIDER) or "favor_cost",
                    allow_blank=False,
                    id="provider",
                )
            yield Static(
                "Uses your account's provider preference when off.",
                id="provider-inherited",
                markup=False,
            )
        if SWITCHYARD in self.controls:
            on = (
                bool(settings.get(SWITCHYARD))
                or has_switchyard_pair(settings)
                or (self.profile is None and self.draft.preset_type == EXPERIMENTAL)
            )
            yield Checkbox("Switchyard", value=on, id="switchyard")
            with Vertical(id="switchyard-fields"):
                yield Label("Efficient model")
                yield Select([], id="efficient", prompt="Built-in default")
                yield Label("Frontier model")
                yield Select([], id="capable", prompt="Select a frontier model")
                yield Label("Switchyard sensitivity")
                yield Select(
                    [
                        (s.title(), s)
                        for s in ("aggressive", "balanced", "conservative", "strict")
                    ],
                    value=(settings.get("switchyard_settings") or {}).get(
                        "sensitivity_config_name"
                    )
                    or "balanced",
                    allow_blank=False,
                    id="sensitivity",
                )
        if AUTO in self.controls:
            yield Label("Auto Jev Routing")
            value = settings.get(AUTO)
            yield Select(
                [("Account default", "account"), ("On", "on"), ("Off", "off")],
                value="account" if value is None else "on" if value else "off",
                allow_blank=False,
                id="auto",
            )
            with Vertical(id="auto-fields"):
                yield Checkbox(
                    "Use a profile-specific model list",
                    value=settings.get("auto_routing_settings") is not None,
                    id="custom-auto",
                )
                yield Static(
                    "2–6 models in order; the first is the default. Each needs a description (up to 300 characters).",
                    markup=False,
                )
                with Vertical(id="auto-models"):
                    for index in range(6):
                        yield Label(f"Auto model {index + 1}")
                        yield Select([], id=f"auto-model-{index}", prompt="Unused")
                        yield Input(
                            id=f"auto-description-{index}",
                            placeholder="When to use this model",
                            max_length=300,
                        )
        if BENCHMARK in self.controls:
            yield Checkbox(
                "Benchmark routing",
                value=bool(settings.get(BENCHMARK)) or self.profile is None,
                id="benchmark",
            )
            yield Static("", id="benchmark-model", markup=False)
            yield BenchmarkFields()
            yield Static("", id="benchmark-save-status", markup=False)

    def actions(self):
        yield Button("Save" if self.profile else "Create", id="save", variant="primary")

    def load(self):
        self.run(
            "Loading profile settings",
            lambda: self.service.profile_options(tuple(sorted(self.controls))),
            self.loaded,
            read=True,
        )

    def set_options(
        self, selector: str, choices: list[tuple[str, str]], value: str | None
    ):
        if value and value not in {v for _, v in choices}:
            choices = [*choices, (f"{value} · saved value", value)]
        widget = self.query_one(selector, Select)
        widget.set_options([(Text(safe_text(label)), key) for label, key in choices])
        widget.value = value or Select.NULL

    def loaded(self, options: dict):
        self.run_worker(self.apply_options(options))

    async def apply_options(self, options: dict):
        self.options = options
        inventory = options.get("inventory") or {}
        settings = self.draft.settings
        if SWITCHYARD in self.controls:
            pair = settings.get("switchyard_settings") or {}
            efficient = pair.get("efficient_model")
            capable = pair.get("capable_model")
            efficient_options = model_options(inventory, "efficient")
            capable_options = model_options(inventory, "capable")
            if not self.profile:
                efficient = (
                    "openai:gpt-6-luna"
                    if "openai:gpt-6-luna" in {value for _, value in efficient_options}
                    else None
                )
                capable = (
                    "openai:gpt-6.1-sol"
                    if "openai:gpt-6.1-sol" in {value for _, value in capable_options}
                    else None
                )
                if pair_error(
                    inventory, efficient or "openai:gpt-5.6-luna", capable or ""
                ):
                    capable = None
            self.set_options("#efficient", efficient_options, efficient)
            self.set_options("#capable", capable_options, capable)
        if AUTO in self.controls:
            configuration = (
                settings.get("auto_routing_settings") or options.get("auto") or {}
            )
            self.populate_auto(configuration.get("candidates") or [])
        if BENCHMARK in self.controls:
            await self.query_one(BenchmarkFields).configure(
                options.get("benchmarks") or {}, self.saved_benchmarks()
            )
        self.update_visibility()
        self.remember()
        self.initial = dict(self.values())
        if options.get("errors"):
            self.message(
                "Couldn't load "
                + ", ".join(options["errors"])
                + ". Existing settings are preserved; reopen to retry.",
                error=True,
            )

    @on(Checkbox.Changed)
    @on(Select.Changed)
    def field_changed(self):
        if self.is_mounted:
            self.update_visibility()

    def populate_auto(self, candidates: list[dict]):
        choices = model_options(self.options.get("inventory") or {}, "auto")
        for index in range(6):
            candidate = candidates[index] if index < len(candidates) else {}
            self.set_options(f"#auto-model-{index}", choices, candidate.get("model"))
            self.query_one(f"#auto-description-{index}", Input).value = (
                candidate.get("description") or ""
            )

    def auto_candidates(self) -> list[dict]:
        candidates = []
        for index in range(6):
            model = self.selected(f"#auto-model-{index}")
            description = self.query_one(
                f"#auto-description-{index}", Input
            ).value.strip()
            if model or description:
                candidates.append({"model": model, "description": description})
        return candidates

    @on(Checkbox.Changed, "#custom-auto")
    def auto_source_changed(self, event):
        if not self.initial:
            return
        if event.value:
            if self.custom_candidates is not None:
                self.populate_auto(self.custom_candidates)
        else:
            self.custom_candidates = self.auto_candidates()
            self.populate_auto((self.options.get("auto") or {}).get("candidates") or [])

    @on(Input.Changed, "#name")
    def name_changed(self, event):
        if (
            not self.profile
            and self.initial
            and not self.slug_edited
            and event.value != self.draft.name
        ):
            self.suggested_slug = slug_from_name(event.value)
            self.query_one("#slug", Input).value = self.suggested_slug

    @on(Input.Changed, "#slug")
    def slug_changed(self, event):
        if self.initial and event.value != self.suggested_slug:
            self.slug_edited = True
        if self.initial and BENCHMARK in self.controls:
            fields = self.query_one(BenchmarkFields)
            if not self.profile and not fields.edited:
                fields.populate(self.saved_benchmarks())
            self.update_visibility()

    def saved_benchmarks(self) -> list[dict]:
        aliases = (self.options.get("aliases") or {}).get("data") or []
        source = next(
            (a for a in aliases if a["name"] == self.draft.settings.get(BENCHMARK)),
            None,
        )
        target = next(
            (a for a in aliases if a["name"] == self.query_one("#slug", Input).value),
            None,
        )
        return (
            (source or {}).get("benchmarks") or (target or {}).get("benchmarks") or []
        )

    def benchmark_changes(self, draft: ProfileDraft) -> list[dict] | None:
        fields = self.query_one(BenchmarkFields)
        if not self.query_one("#benchmark", Checkbox).value:
            draft.settings[BENCHMARK] = None
            return None
        if self.profile and self.draft.settings.get(BENCHMARK) and not fields.edited:
            return None
        if {"aliases", "benchmarks"} & set(self.options.get("errors", [])):
            raise ValueError(
                "Benchmark routing could not be loaded. Reopen the profile to retry."
            )
        alias = next(
            (
                a
                for a in (self.options.get("aliases") or {}).get("data", [])
                if a["name"] == draft.slug
            ),
            None,
        )
        if alias is not None and not alias.get("benchmarks"):
            raise ValueError(
                "This slug is already used by a non-benchmark alias. Resolve the alias conflict or choose another profile slug."
            )
        selections = fields.collect()
        draft.settings[BENCHMARK] = draft.slug
        return selections

    def selected(self, selector: str) -> str | None:
        value = self.query_one(selector, Select).value
        return None if value is Select.NULL else str(value)

    def update_visibility(self):
        for control, section in (
            ("cross-provider", "provider-fields"),
            ("switchyard", "switchyard-fields"),
            ("benchmark", "benchmark-fields"),
        ):
            if self.query(f"#{control}"):
                self.query_one(f"#{section}").display = self.query_one(
                    f"#{control}", Checkbox
                ).value
        if PROVIDER in self.controls:
            self.query_one("#provider-inherited").display = not self.query_one(
                "#cross-provider", Checkbox
            ).value
        if BENCHMARK in self.controls:
            enabled = self.query_one("#benchmark", Checkbox).value
            hint = self.query_one("#benchmark-model", Static)
            hint.display = enabled
            slug = self.query_one("#slug", Input).value
            hint.update(
                f"Select {safe_text(slug) or 'the profile slug'} in your harness model picker to use benchmark routing."
            )
            status = self.query_one("#benchmark-save-status", Static)
            status.display = self.benchmark_saved
            status.update(
                "Benchmark changes are already saved and may affect live requests. Leaving does not undo them; retry to finish saving the profile."
            )
        if AUTO in self.controls:
            self.query_one("#auto-fields").display = self.selected("#auto") != "off"
            custom = self.query_one("#custom-auto", Checkbox).value
            for widget in self.query("#auto-models Select, #auto-models Input"):
                widget.disabled = not custom

    def changed(self, *fields: str) -> bool:
        values = dict(self.values())
        return self.profile is None or any(
            values.get(field) != self.initial.get(field) for field in fields
        )

    def collect(self) -> ProfileDraft:
        draft = deepcopy(self.draft)
        draft.name = self.query_one("#name", Input).value.strip()
        valid = _validate_name(draft.name)
        if valid is not True:
            raise ValueError(str(valid))
        others = [p for p in self.profiles if str(p["id"]) != draft.profile_id]
        if draft.name.casefold() in {p["name"].strip().casefold() for p in others}:
            raise ValueError("A routing profile with this name already exists.")
        if not self.profile:
            draft.slug = self.query_one("#slug", Input).value.strip()
            if (
                not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", draft.slug)
                or len(draft.slug) > 64
            ):
                raise ValueError(
                    "Use a slug of lowercase letters and digits separated by single hyphens (up to 64 characters)."
                )
            if draft.slug in {p.get("slug") for p in others}:
                raise ValueError("This routing profile slug is already in use.")
        if COST in self.controls:
            draft.settings[COST] = self.query_one("#cost", Checkbox).value
        if PROVIDER in self.controls:
            draft.settings[PROVIDER] = (
                self.selected("#provider")
                if self.query_one("#cross-provider", Checkbox).value
                else None
            )
        if AUTO in self.controls and self.changed("auto"):
            draft.settings[AUTO] = AUTO_ROUTING_CHOICES[self.selected("#auto")]
        if (
            draft.preset_type == EXPERIMENTAL
            and self.query_one("#switchyard", Checkbox).value
            and not (self.selected("#efficient") and self.selected("#capable"))
        ):
            raise ValueError(
                "Experimental Switchyard requires both an efficient and a frontier model, even with Auto off."
            )
        if SWITCHYARD in self.controls and self.changed(
            "switchyard", "efficient", "capable", "sensitivity"
        ):
            on = self.query_one("#switchyard", Checkbox).value
            efficient, capable = self.selected("#efficient"), self.selected("#capable")
            if on:
                was_on = bool(
                    self.draft.settings.get(SWITCHYARD)
                ) or has_switchyard_pair(self.draft.settings)
                if (
                    self.profile is None
                    or (
                        draft.preset_type in ("coding-agents", EXPERIMENTAL)
                        and not was_on
                    )
                ) and not capable:
                    raise ValueError("Select a frontier model for Switchyard.")
                if draft.settings.get(AUTO) and not (efficient and capable):
                    raise ValueError(
                        "Switchyard alongside Auto requires both an efficient and a frontier model."
                    )
                if error := pair_error(
                    self.options.get("inventory") or {},
                    efficient or "openai:gpt-5.6-luna",
                    capable or "",
                ):
                    raise ValueError(error)
            draft.settings["switchyard_settings"] = switchyard_settings(
                draft.settings,
                on=on,
                efficient=efficient,
                capable=capable,
                sensitivity=self.selected("#sensitivity"),
            )
            draft.settings[SWITCHYARD] = on and not (efficient and capable)
        if draft.settings.get(SWITCHYARD) and draft.settings.get(AUTO):
            raise ValueError(
                "Choose a Switchyard model pair or turn off Auto; the legacy Switchyard toggle cannot run alongside Auto."
            )
        auto_fields = [
            "custom-auto",
            *(
                f"auto-{part}-{i}"
                for i in range(6)
                for part in ("model", "description")
            ),
        ]
        if AUTO in self.controls and self.changed("auto", *auto_fields):
            candidates = self.auto_candidates()
            complete = 2 <= len(candidates) <= 6 and all(
                c["model"] and c["description"] for c in candidates
            )
            distinct = len({c["model"] for c in candidates}) == len(candidates)
            if draft.settings.get(AUTO) and not complete:
                raise ValueError(
                    "Auto requires 2–6 models, each with a description. Configure the list or turn Auto off."
                )
            if draft.settings.get(AUTO) and not distinct:
                raise ValueError("Auto models must be distinct.")
            if self.changed(*auto_fields):
                if not self.query_one("#custom-auto", Checkbox).value:
                    draft.settings["auto_routing_settings"] = None
                elif complete and distinct:
                    draft.settings["auto_routing_settings"] = {"candidates": candidates}
                # An incomplete draft must not overwrite the saved list when Auto is off.
        if BENCHMARK in self.controls:
            self.benchmark_changes(draft)
        return draft

    @on(Button.Pressed, "#save")
    def save(self):
        if not self.initial or self.uncertain:
            if self.uncertain:
                self.message(
                    "Creation may already have saved. Reopen Routing Profiles before creating another.",
                    error=True,
                )
            return
        try:
            draft = self.collect()
            benchmarks = (
                self.benchmark_changes(draft) if BENCHMARK in self.controls else None
            )
        except ValueError as error:
            self.message(str(error), error=True)
            return
        if self.profile and not draft.patch_body() and benchmarks is None:
            self.router_app.back(force=True)
            return
        submitted_values = dict(self.values())

        def alias_saved(changed: bool):
            self.benchmark_saved |= changed
            self.query_one(BenchmarkFields).remember_saved(benchmarks)
            self.update_visibility()

        def retain_failed_draft():
            self.draft = draft
            # The retained draft contains the attempted values. A subsequent
            # revert must be collected relative to those controls, not opening values.
            self.initial = submitted_values

        def persist():
            if benchmarks is not None:
                changed = self.service.save_profile_benchmarks(draft.slug, benchmarks)
                self.app.call_from_thread(alias_saved, changed)
            try:
                return self.service.save_profile(draft)
            except Exception as error:
                self.app.call_from_thread(retain_failed_draft)
                if not self.profile and (
                    isinstance(error, httpx.TransportError)
                    or isinstance(error, ApiError)
                    and error.status_code >= 500
                ):
                    self.uncertain = True
                    raise RampCLIError(
                        "Profile creation could not be confirmed; it may already have saved. Reopen Routing Profiles before creating another."
                    ) from error
                raise

        def saved(_result):
            for page in self.app.screen_stack:
                if hasattr(page, "select_name"):
                    page.select_name = draft.name
            self.router_app.back(force=True)

        count = len(draft.keys)
        message = f"Save {safe_text(draft.name)}? Changes apply to its {count} assigned API keys and requests selecting this profile."
        if draft.preset_type == EXPERIMENTAL:
            message += (
                " This also updates the account's Switchyard and Auto model entries."
            )
        if benchmarks is not None:
            message += " Benchmark weights are saved first and also affect requests using this profile's model alias."
        self.router_app.confirm(
            message,
            lambda: self.run("Saving routing profile", persist, saved),
            "Save",
            focus_accept=True,
        )
