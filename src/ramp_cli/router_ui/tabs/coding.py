from copy import deepcopy

import httpx
from rich.text import Text
from textual import events, on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.timer import Timer
from textual.widgets import Button as TextualButton
from textual.widgets import Input, Select, Static, Tab, Tabs

from ramp_cli.errors import ApiError, RampCLIError
from ramp_cli.router_ui.profiles import (
    AUTO,
    CODING,
    COST,
    EXPERIMENTAL,
    PROVIDER,
    PROVIDERS,
    SWITCHYARD,
    coding_draft,
    has_switchyard_pair,
    model_options,
    pair_error,
    switchyard_settings,
)
from ramp_cli.router_ui.service import safe_message, safe_text
from ramp_cli.router_ui.widgets import Button, InlineModal


class CompactSelect(Select):
    DEFAULT_CSS = """
    CompactSelect { height: 1; min-height: 1; margin: 0; padding: 0; border: none; }
    CompactSelect > SelectCurrent { height: 1; min-height: 1; padding: 0 1; border: none; }
    CompactSelect:focus > SelectCurrent, CompactSelect.-expanded > SelectCurrent { border: none; }
    """


class CodingSettings(Vertical):
    DEFAULT_CSS = """
    CodingSettings { height: auto; margin: 0 1 1 1; }
    CodingSettings .coding-heading { color: $router-accent; text-style: bold; height: 1; }
    CodingSettings #coding-scope { margin: 1 0; color: $router-muted; }
    CodingSettings .coding-row { height: 1; }
    CodingSettings .coding-label { width: 30; height: 1; }
    CodingSettings CompactSelect { width: 26; }
    CodingSettings CompactSelect > SelectCurrent { background: $router-surface; color: $router-surface-foreground; }
    CodingSettings CompactSelect:focus > SelectCurrent { background: $router-active; color: $router-active-accent; }
    CodingSettings #coding-actions { height: 3; margin-top: 1; }
    CodingSettings #coding-actions Button { width: 17; min-width: 0; margin-right: 2; }
    CodingSettings #coding-status { height: 3; width: 1fr; content-align: left middle; color: $router-muted; }
    CodingSettings #experimental-row { height: 1; margin-top: 1; }
    CodingSettings #experimental { height: 1; min-height: 1; width: 26; min-width: 0; border: none; padding: 0 1; background: $router-surface; color: $router-surface-foreground; }
    CodingSettings #experimental-note { margin-top: 1; color: $router-muted; }
    """

    def __init__(self):
        super().__init__()
        self.profiles = None
        self.draft = None
        self.baseline = None
        self.attempted = set()

    def compose(self):
        yield Static("Global Coding Settings", classes="coding-heading")
        yield Static(
            "Shared across recognized coding harnesses for this Router account.",
            id="coding-scope",
        )
        with Horizontal(classes="coding-row"):
            yield Static("Cost-efficient routing", classes="coding-label")
            yield CompactSelect(
                [("Loading…", "loading")],
                id="coding-cost",
                prompt="Unavailable",
                allow_blank=False,
                disabled=True,
            )
        with Horizontal(classes="coding-row"):
            yield Static("Cross-provider routing", classes="coding-label")
            yield CompactSelect(
                [("Loading…", "loading")],
                id="coding-provider",
                prompt="Unavailable",
                allow_blank=False,
                disabled=True,
            )
        with Horizontal(id="coding-actions"):
            yield Button("Save changes", id="coding-save", disabled=True)
            yield Button("Discard", id="coding-discard", disabled=True)
            yield Static("Loading settings…", id="coding-status", markup=False)
        with Horizontal(id="experimental-row"):
            yield Static("Experimental models", classes="coding-label")
            yield TextualButton("Configure…", id="experimental", disabled=True)
        yield Static(
            "Adds Switchyard and Auto to your model picker alongside your usual models.",
            id="experimental-note",
        )

    def values(self):
        return {
            COST: self.query_one("#coding-cost", Select).value == "on",
            PROVIDER: self.query_one("#coding-provider", Select).value or None,
        }

    @property
    def dirty(self):
        return self.baseline is not None and (
            self.values() != self.baseline or bool(self.attempted)
        )

    def load(self):
        if self.dirty:
            return

        def read():
            try:
                return self.app.service.profiles(), None
            except (ApiError, RampCLIError, httpx.HTTPError) as error:
                return None, safe_message(error)

        self.screen.run("Loading coding settings", read, self.loaded, read=True)

    def loaded(self, result):
        profiles, error = result
        self.screen.message("")
        self.profiles = profiles
        self.query_one("#experimental").disabled = profiles is None
        if profiles is None:
            self.baseline = None
            self.draft = None
            for selector in (
                "#coding-cost",
                "#coding-provider",
                "#coding-save",
                "#coding-discard",
            ):
                self.query_one(selector).disabled = True
            for selector in ("#coding-cost", "#coding-provider"):
                self.query_one(selector, Select).set_options(
                    [("Unavailable", "unavailable")]
                )
            self.query_one("#coding-status", Static).update(
                error or "Couldn't load settings. Reload to retry."
            )
            return
        self.draft = coding_draft(profiles, CODING)
        legacy = self.draft.profile_id is not None and not self.draft.slug
        self.query_one("#coding-cost", Select).set_options(
            [("Enabled", "on"), ("Disabled", "off")]
        )
        self.query_one("#coding-provider", Select).set_options(
            [
                ("Use account default", ""),
                *[(label, value) for label, value in PROVIDERS if value],
            ]
        )
        for selector in ("#coding-cost", "#coding-provider"):
            self.query_one(selector).disabled = legacy
        self.query_one("#coding-cost", Select).value = (
            "on" if self.draft.settings[COST] else "off"
        )
        self.query_one("#coding-provider", Select).value = (
            self.draft.settings.get(PROVIDER) or ""
        )
        self.baseline = self.values()
        self.attempted.clear()
        self.query_one("#coding-status", Static).update(
            "Manage this legacy coding profile in Routing Profiles."
            if legacy
            else "Save to apply these account defaults"
            if self.draft.profile_id is None
            else "No unsaved changes"
        )
        self.update_actions()

    def update_actions(self):
        self.query_one("#coding-save").disabled = not (
            self.dirty or self.draft is not None and self.draft.profile_id is None
        )
        self.query_one("#coding-discard").disabled = not self.dirty

    @on(Select.Changed)
    def changed(self, event):
        event.stop()
        if self.baseline is not None:
            self.update_actions()
            if self.dirty:
                self.query_one("#coding-status", Static).update(
                    "Unsaved changes · account-wide"
                )

    @on(Button.Pressed, "#coding-discard")
    def discard(self):
        self.attempted.clear()
        self.baseline = None
        self.load()

    @on(Button.Pressed, "#coding-save")
    def save(self):
        if (
            self.app.busy
            or self.draft is None
            or (not self.dirty and self.draft.profile_id is not None)
        ):
            return
        changes = {
            key: value
            for key, value in self.values().items()
            if value != self.baseline[key] or key in self.attempted
        }

        def write():
            try:
                return self.app.service.save_coding_profile(
                    CODING, changes, self.draft.profile_id
                )
            except Exception:
                # A failed response may follow a successful write. Collect visible
                # reverts too on retry, rather than mistaking them for a no-op.
                # Textual inspects callbacks; set.update has no signature on Python 3.12.
                self.app.call_from_thread(lambda: self.attempted.update(changes))
                raise

        def saved(profile):
            profiles = [p for p in self.profiles if p.get("preset_type") != CODING]
            self.loaded(([*profiles, profile], None))
            self.query_one("#coding-status", Static).update(
                "Saved account coding settings"
            )

        self.screen.run("Saving coding settings", write, saved)

    @on(Button.Pressed, "#experimental")
    def configure(self):
        if self.profiles is not None and not self.app.busy:
            self.app.push_screen(
                ExperimentalModelsPage(), lambda saved: self.load() if saved else None
            )


class AutoCandidate(Vertical):
    def __init__(self, index, options, candidate=None):
        super().__init__(id=f"candidate-{index}", classes="candidate")
        self.index, self.options, self.candidate = index, options, candidate or {}

    def compose(self):
        with Horizontal(classes="candidate-title"):
            yield Static(
                f"Model {self.index + 1}" + (" · Default" if self.index == 0 else ""),
                classes="candidate-label",
            )
            yield TextualButton(
                "↑",
                id=f"up-{self.index}",
                classes="candidate-action",
                disabled=self.index == 0,
            )
            yield TextualButton(
                "Remove", id=f"remove-{self.index}", classes="candidate-action remove"
            )
        with Horizontal(classes="field"):
            yield Static("Model", classes="field-label")
            yield CompactSelect(
                self.options, id=f"auto-model-{self.index}", prompt="Choose a model"
            )
        with Horizontal(classes="field"):
            yield Static("Use for", classes="field-label")
            yield Input(
                self.candidate.get("description") or "",
                id=f"auto-description-{self.index}",
                placeholder="Describe the tasks this model is good at",
                max_length=300,
            )


class ExperimentalModelsPage(InlineModal):
    BINDINGS = [Binding("escape", "back", "Cancel")]
    DEFAULT_CSS = """
    ExperimentalModelsPage { align: center middle; background: $router-overlay; }
    ExperimentalModelsPage #experimental-dialog { width: 106; max-width: 94%; height: auto; max-height: 94%; padding: 1 3; border: solid $router-frame-border; background: $router-background; }
    ExperimentalModelsPage #experimental-title { height: 1; color: $router-accent; text-style: bold; }
    ExperimentalModelsPage #experimental-scope { margin-top: 1; color: $router-muted; }
    ExperimentalModelsPage #experimental-tabs { height: 2; margin: 1 0; background: $router-background; }
    ExperimentalModelsPage #experimental-tabs Tab { background: $router-background; color: $router-muted; }
    ExperimentalModelsPage #experimental-tabs Tab.-active { color: $router-accent; text-style: bold; }
    ExperimentalModelsPage #experimental-tabs Underline { background: $router-background; }
    ExperimentalModelsPage #experimental-tabs Underline > .underline--bar { color: $router-accent; }
    ExperimentalModelsPage #editor-body, ExperimentalModelsPage .section, ExperimentalModelsPage #switchyard-fields, ExperimentalModelsPage #auto-fields, ExperimentalModelsPage #candidates, ExperimentalModelsPage .candidate { height: auto; }
    ExperimentalModelsPage .intro { margin-bottom: 1; color: $router-muted; }
    ExperimentalModelsPage .field { height: 1; }
    ExperimentalModelsPage .field-label { width: 27; height: 1; }
    ExperimentalModelsPage CompactSelect { width: 1fr; }
    ExperimentalModelsPage CompactSelect > SelectCurrent { background: $router-surface; color: $router-surface-foreground; }
    ExperimentalModelsPage CompactSelect:focus > SelectCurrent { background: $router-active; color: $router-active-accent; }
    ExperimentalModelsPage .field-help { margin: 1 0 0 27; color: $router-muted; }
    ExperimentalModelsPage .section-heading, ExperimentalModelsPage .candidate-title { height: 1; margin-top: 1; text-style: bold; }
    ExperimentalModelsPage .candidate-label { height: 1; width: 1fr; }
    ExperimentalModelsPage Input { height: 1; min-height: 1; padding: 0 1; margin: 0; border: none; background: $router-surface; color: $router-surface-foreground; }
    ExperimentalModelsPage Input:focus { border: none; background: $router-active; }
    ExperimentalModelsPage .compact-action { height: 1; min-height: 1; min-width: 0; border: none; padding: 0 1; margin: 1 0 0 0; width: auto; background: $router-surface; color: $router-surface-foreground; }
    ExperimentalModelsPage .candidate-action { height: 1; min-height: 1; min-width: 0; border: none; padding: 0 1; margin: 0 0 0 1; width: 5; background: $router-surface; color: $router-surface-foreground; }
    ExperimentalModelsPage .candidate-action.remove { width: 10; }
    ExperimentalModelsPage #advanced-fields { display: none; height: auto; margin-top: 1; }
    ExperimentalModelsPage .usage-note { margin-top: 1; color: $router-muted; }
    ExperimentalModelsPage #auto-panel { display: none; }
    ExperimentalModelsPage #editor-footer { height: auto; margin-top: 1; }
    ExperimentalModelsPage #editor-status { color: $router-muted; margin-bottom: 1; }
    ExperimentalModelsPage #editor-status.error { color: $router-error; }
    ExperimentalModelsPage #editor-status.invalid { color: $router-accent; }
    ExperimentalModelsPage #editor-actions { height: 3; }
    ExperimentalModelsPage #editor-actions .spacer { width: 1fr; }
    ExperimentalModelsPage #editor-actions Button { width: 19; min-width: 0; margin: 0 0 0 1; }
    """

    def __init__(self):
        super().__init__()
        self.content_shown = True
        self.loading_timer: Timer | None = None
        self.draft = None
        self.options = {}
        self.baseline = None
        self.initial = {}
        self.attempted = set()
        self.count = 2
        self.populating = False
        self.blocked = False

    def compose(self):
        with Vertical(id="experimental-dialog"):
            yield Static("Experimental Models", id="experimental-title")
            yield Static(
                "Shared across your harnesses. Adds choices alongside your usual models.",
                id="experimental-scope",
            )
            yield Tabs(
                Tab("Switchyard", id="switchyard-tab"),
                Tab("Auto", id="auto-tab"),
                id="experimental-tabs",
            )
            with VerticalScroll(id="editor-body", disabled=True):
                with Vertical(id="switchyard-panel", classes="section"):
                    yield Static(
                        "Use an efficient model for simpler coding steps and a frontier model for harder ones.",
                        classes="intro",
                    )
                    with Horizontal(classes="field"):
                        yield Static("Switchyard", classes="field-label")
                        yield CompactSelect(
                            [("Enabled", True), ("Disabled", False)],
                            id="switchyard",
                            allow_blank=False,
                            value=False,
                        )
                    with Vertical(id="switchyard-fields"):
                        yield Static("Model Pair", classes="section-heading")
                        with Horizontal(classes="field"):
                            yield Static("Efficient model", classes="field-label")
                            yield CompactSelect(
                                [], id="efficient", prompt="Choose an efficient model"
                            )
                        with Horizontal(classes="field"):
                            yield Static("Frontier model", classes="field-label")
                            yield CompactSelect(
                                [], id="capable", prompt="Choose a frontier model"
                            )
                        yield Static("Both models are required.", classes="field-help")
                        yield TextualButton(
                            "› Advanced · Balanced sensitivity",
                            id="advanced",
                            classes="compact-action",
                        )
                        with Vertical(id="advanced-fields"):
                            with Horizontal(classes="field"):
                                yield Static(
                                    "Switching sensitivity", classes="field-label"
                                )
                                yield CompactSelect(
                                    [
                                        (s.title(), s)
                                        for s in (
                                            "aggressive",
                                            "balanced",
                                            "conservative",
                                            "strict",
                                        )
                                    ],
                                    id="sensitivity",
                                    value="balanced",
                                    allow_blank=False,
                                )
                    yield Static(
                        "Choose Switchyard in a harness’s model picker to use it.",
                        classes="usage-note",
                    )
                with Vertical(id="auto-panel", classes="section"):
                    yield Static(
                        "Router chooses between your models for each request, using the descriptions below.",
                        classes="intro",
                    )
                    with Horizontal(classes="field"):
                        yield Static("Auto", classes="field-label")
                        yield CompactSelect(
                            [("Enabled", True), ("Disabled", False)],
                            id="auto",
                            prompt="Couldn't load setting",
                        )
                    with Vertical(id="auto-fields"):
                        yield Vertical(id="candidates")
                        yield TextualButton(
                            "+ Add model", id="add-model", classes="compact-action"
                        )
                        yield Static(
                            "At least 2 different models, each with a description. Up to 6 models.",
                            classes="usage-note",
                        )
                    yield Static(
                        "Choose auto-jev-routing in a harness’s model picker to use it.",
                        classes="usage-note",
                    )
            with Vertical(id="editor-footer"):
                yield Static("Loading settings…", id="editor-status", markup=False)
                with Horizontal(id="editor-actions"):
                    yield Static("", classes="spacer")
                    yield Button("Cancel", id="editor-cancel")
                    yield Button("Save changes", id="editor-save", disabled=True)

    def on_mount(self):
        self.query_one("#editor-cancel").focus()
        self.call_after_refresh(self.load)

    def on_resize(self, event: events.Resize):
        self.call_after_refresh(self.fit_body)

    def fit_body(self):
        # Deferred refreshes can run during removal, when is_mounted is still true
        # but the dialog's children are gone. display also excludes closing nodes.
        if self.is_mounted and self.display:
            dialog = self.query_one("#experimental-dialog")
            chrome = dialog.styles.gutter.height + sum(
                w.outer_size.height + w.styles.margin.height
                for w in dialog.children
                if w.id != "editor-body"
            )
            self.query_one("#editor-body").styles.max_height = max(
                3, int(self.size.height * 0.94) - chrome
            )

    def run(self, label, operation, complete=None, *, read=False):
        self.app.run_operation(self, label, operation, complete, read=read)

    def restore_focus(self, widget):
        def focus():
            if widget.is_mounted and widget.focusable and widget in self.focus_chain:
                widget.focus()

        self.call_after_refresh(focus)

    def message(self, message, *, error=False):
        status = self.query_one("#editor-status", Static)
        status.set_class(error, "error")
        status.update(safe_message(message))

    def load(self):
        self.run(
            "Loading experimental models",
            lambda: (
                self.app.service.profiles(),
                self.app.service.profile_options((SWITCHYARD, AUTO)),
                self.app.service.auto_routing_default(),
            ),
            lambda result: self.run_worker(self.loaded(*result)),
            read=True,
        )

    def set_options(self, selector, options, value):
        if value and value not in {key for _, key in options}:
            options = [*options, (f"{value} · saved value", value)]
        widget = self.query_one(selector, Select)
        widget.set_options([(Text(safe_text(label)), key) for label, key in options])
        widget.value = value if value is not None else Select.NULL

    async def loaded(self, profiles, options, auto_default):
        self.populating = True
        self.options = options
        self.draft = coding_draft(profiles, EXPERIMENTAL)
        if self.draft.profile_id is not None and not self.draft.slug:
            self.blocked = True
            self.message(
                "This legacy coding profile is read-only. Manage it in Routing Profiles."
            )
            return
        settings = self.draft.settings
        pair = settings.get("switchyard_settings") or {}
        inventory = options.get("inventory") or {}
        efficient, capable = pair.get("efficient_model"), pair.get("capable_model")
        low, high = (
            model_options(inventory, "efficient"),
            model_options(inventory, "capable"),
        )
        if self.draft.profile_id is None:
            efficient = (
                "openai:gpt-6-luna"
                if "openai:gpt-6-luna" in {v for _, v in low}
                else None
            )
            capable = (
                "openai:gpt-6.1-sol"
                if "openai:gpt-6.1-sol" in {v for _, v in high}
                else None
            )
            if efficient and capable and pair_error(inventory, efficient, capable):
                capable = None
        self.set_options("#efficient", low, efficient)
        self.set_options("#capable", high, capable)
        self.query_one("#sensitivity", Select).value = (
            pair.get("sensitivity_config_name") or "balanced"
        )
        self.query_one("#switchyard", Select).value = (
            bool(settings.get(SWITCHYARD))
            or has_switchyard_pair(settings)
            or self.draft.profile_id is None
        )
        auto = settings.get(AUTO)
        self.query_one("#auto", Select).value = (
            auto
            if auto is not None
            else auto_default
            if auto_default is not None
            else Select.NULL
        )
        await self.populate_candidates(
            (settings.get("auto_routing_settings") or options.get("auto") or {}).get(
                "candidates"
            )
            or []
        )
        self.initial = self.values()
        self.baseline = deepcopy(self.initial)
        self.populating = False
        self.query_one("#editor-body").disabled = False
        self.update_state()
        if options.get("errors"):
            self.message(
                "Some model options couldn't load. Reopen to retry; saved settings are preserved.",
                error=True,
            )
        self.call_after_refresh(self.fit_body)
        self.query_one("#switchyard").focus()

    async def populate_candidates(self, candidates):
        self.populating = True
        self.count = max(2, len(candidates))
        container = self.query_one("#candidates")
        await container.remove_children()
        choices = model_options(self.options.get("inventory") or {}, "auto")
        await container.mount(
            *(
                AutoCandidate(i, choices, candidates[i] if i < len(candidates) else {})
                for i in range(self.count)
            )
        )
        for i in range(self.count):
            self.set_options(
                f"#auto-model-{i}",
                choices,
                (candidates[i] if i < len(candidates) else {}).get("model"),
            )
            self.query_one(f"#remove-{i}").disabled = self.count <= 2
        self.populating = False

    def selected(self, selector):
        value = self.query_one(selector, Select).value
        return None if value is Select.NULL else value

    def candidates(self):
        return [
            {
                "model": self.selected(f"#auto-model-{i}"),
                "description": self.query_one(
                    f"#auto-description-{i}", Input
                ).value.strip(),
            }
            for i in range(self.count)
        ]

    def values(self):
        return {
            **{
                key: self.selected(f"#{key}")
                for key in ("switchyard", "efficient", "capable", "sensitivity", "auto")
            },
            "candidates": self.candidates(),
        }

    def changed(self, *fields):
        values = self.values()
        return any(values[key] != self.initial.get(key) for key in fields)

    def collect(self):
        values = self.values()
        settings = deepcopy(self.draft.settings)
        changes = {}
        pair_changed = (
            self.changed("switchyard", "efficient", "capable", "sensitivity")
            or self.draft.profile_id is None
            or bool(self.attempted & {SWITCHYARD, "switchyard_settings"})
        )
        if values["switchyard"]:
            if not values["efficient"] or not values["capable"]:
                raise ValueError(
                    "Choose both an efficient and a frontier model to save Switchyard."
                )
            if pair_changed and (
                error := pair_error(
                    self.options.get("inventory") or {},
                    values["efficient"],
                    values["capable"],
                )
            ):
                raise ValueError(error)
        if pair_changed:
            if values["switchyard"] and "inventory" in self.options.get("errors", []):
                raise ValueError(
                    "Reopen to load the model inventory before changing Switchyard."
                )
            changes["switchyard_settings"] = switchyard_settings(
                settings,
                on=values["switchyard"],
                efficient=values["efficient"],
                capable=values["capable"],
                sensitivity=values["sensitivity"],
            )
            # The pair exposes the virtual model; the legacy request toggle stays off.
            changes[SWITCHYARD] = False
        if values["auto"] is None:
            raise ValueError(
                "Couldn't load Auto. Choose Enabled or Disabled, or reopen to retry."
            )
        if values["auto"]:
            models = [c["model"] for c in values["candidates"]]
            if (
                not 2 <= len(models) <= 6
                or not all(models)
                or len(set(models)) != len(models)
            ):
                raise ValueError("Choose at least 2 different models to save Auto.")
            if any(not c["description"] for c in values["candidates"]):
                raise ValueError("Describe what each Auto model should be used for.")
        if (
            self.changed("auto")
            or AUTO in self.attempted
            or self.draft.profile_id is None
        ):
            changes[AUTO] = values["auto"]
        if (
            self.changed("candidates")
            or "auto_routing_settings" in self.attempted
            or self.draft.profile_id is None
        ):
            if values["auto"]:
                if "inventory" in self.options.get("errors", []):
                    raise ValueError(
                        "Reopen to load model options before changing Auto models."
                    )
                changes["auto_routing_settings"] = {"candidates": values["candidates"]}
        # Untouched inherited lists/toggles and historical settings remain untouched.
        return changes

    @property
    def dirty(self):
        return self.baseline is not None and (
            self.values() != self.baseline or bool(self.attempted)
        )

    @property
    def unsaved(self):
        return self.dirty

    def update_state(self):
        if self.populating or self.baseline is None or self.blocked:
            return
        self.query_one("#switchyard-fields").display = bool(
            self.selected("#switchyard")
        )
        self.query_one("#auto-fields").display = self.selected("#auto") is not False
        self.query_one("#add-model").disabled = self.count >= 6
        sensitivity = self.selected("#sensitivity")
        expanded = self.query_one("#advanced-fields").display
        self.query_one(
            "#advanced", TextualButton
        ).label = f"{'⌄' if expanded else '›'} Advanced · {str(sensitivity).title()} sensitivity"
        try:
            self.collect()
        except ValueError as error:
            self.message(str(error))
            self.query_one("#editor-status").add_class("invalid")
            self.query_one("#editor-save").disabled = True
        else:
            self.query_one("#editor-status").remove_class("invalid", "error")
            self.message(
                "Unsaved changes · applies to your account"
                if self.dirty
                else "Save to add these models to your account"
                if self.draft.profile_id is None
                else "No unsaved changes"
            )
            self.query_one("#editor-save").disabled = not (
                self.dirty or self.draft.profile_id is None
            )
        self.call_after_refresh(self.fit_body)

    @on(Select.Changed)
    @on(Input.Changed)
    def fields_changed(self, event):
        event.stop()
        self.update_state()

    @on(Tabs.TabActivated, "#experimental-tabs")
    def select_section(self, event):
        event.stop()
        switchyard = event.tab.id == "switchyard-tab"
        self.query_one("#switchyard-panel").display = switchyard
        self.query_one("#auto-panel").display = not switchyard
        self.call_after_refresh(self.fit_body)

    @on(TextualButton.Pressed)
    async def pressed(self, event):
        name = event.button.id or ""
        if name == "advanced":
            event.stop()
            fields = self.query_one("#advanced-fields")
            fields.display = not fields.display
            self.update_state()
        elif name == "add-model" or name.startswith(("remove-", "up-")):
            event.stop()
            candidates = self.candidates()
            if name == "add-model" and len(candidates) < 6:
                candidates.append({})
            elif name.startswith("remove-") and len(candidates) > 2:
                candidates.pop(int(name.split("-")[1]))
            elif name.startswith("up-"):
                index = int(name.split("-")[1])
                if index > 0:
                    candidates[index - 1], candidates[index] = (
                        candidates[index],
                        candidates[index - 1],
                    )
            await self.populate_candidates(candidates)
            self.update_state()
            self.query_one("#add-model").focus()

    @on(Button.Pressed, "#editor-cancel")
    def action_back(self):
        if self.app.busy:
            self.app.notice_busy()
        elif self.dirty:
            self.app.confirm(
                "Discard unsaved changes?", lambda: self.dismiss(None), "Discard"
            )
        else:
            self.dismiss(None)

    @on(Button.Pressed, "#editor-save")
    def save(self):
        if self.app.busy or self.draft is None or self.blocked:
            return
        try:
            changes = self.collect()
        except ValueError as error:
            self.message(str(error))
            return

        def write():
            try:
                return self.app.service.save_coding_profile(
                    EXPERIMENTAL, changes, self.draft.profile_id
                )
            except Exception:
                self.app.call_from_thread(lambda: self.attempted.update(changes))
                raise

        self.run(
            "Saving experimental models", write, lambda _profile: self.dismiss(True)
        )
