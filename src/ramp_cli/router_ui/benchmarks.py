"""Weighted benchmark controls for Routing V2 profiles."""

from copy import deepcopy

from rich.text import Text
from textual import on
from textual.containers import Vertical
from textual.widgets import Input, Label, Select, Static

from ramp_cli.router_ui.service import safe_text


def normalized_weights(selections: list[dict]) -> list[dict]:
    if not selections or sum(s["weight"] for s in selections) == 100:
        return deepcopy(selections)
    # Match V2: reserve one point per row, then assign the largest remainders.
    total = sum(s["weight"] for s in selections)
    shares = [(100 - len(selections)) * s["weight"] / total for s in selections]
    weights = [int(share) + 1 for share in shares]
    order = sorted(range(len(shares)), key=lambda i: -(shares[i] % 1))
    for index in order[: 100 - sum(weights)]:
        weights[index] += 1
    return [{**s, "weight": weight} for s, weight in zip(selections, weights)]


class BenchmarkFields(Vertical):
    DEFAULT_CSS = """
    BenchmarkFields, BenchmarkFields .benchmark-row { height: auto; }
    """

    def __init__(self):
        super().__init__(id="benchmark-fields")
        self.initial: list[dict] = []
        self.raw: list[dict] = []
        self.count = 0
        self.limit = 3
        self.catalog: list[dict] = []
        self.ready = False

    async def configure(self, catalog: dict, selections: list[dict]):
        self.limit = catalog.get("max_selections", 3)
        self.catalog = catalog.get("data") or []
        choices = [(Text(safe_text(b["name"])), b["id"]) for b in self.catalog]
        await self.mount(
            Static(
                f"Choose up to {self.limit} benchmarks. Weights must total 100%. Clear a selection to remove it.",
                markup=False,
            ),
            *(
                Vertical(
                    Label(f"Benchmark {index + 1}"),
                    Select(choices, id=f"benchmark-{index}", prompt="Unused"),
                    Label(f"Weight {index + 1} (%)"),
                    Input(
                        "100" if index == 0 else "1",
                        type="integer",
                        id=f"weight-{index}",
                    ),
                    classes="benchmark-row",
                )
                for index in range(max(self.limit, len(selections)))
            ),
            Static("", id="benchmark-total", markup=False),
        )
        self.populate(selections)
        self.ready = True

    def populate(self, selections: list[dict]):
        self.raw = deepcopy(selections)
        self.initial = normalized_weights(selections)
        choices = [(Text(safe_text(b["name"])), b["id"]) for b in self.catalog]
        for selection in selections:
            value = selection["benchmark_id"]
            if value not in {key for _, key in choices}:
                choices.append((Text(safe_text(f"{value} · saved value")), value))
        with self.prevent(Select.Changed, Input.Changed):
            for index, select in enumerate(self.query(Select)):
                selection = self.initial[index] if index < len(self.initial) else {}
                select.set_options(choices)
                select.value = selection.get("benchmark_id") or Select.NULL
                self.query_one(f"#weight-{index}", Input).value = str(
                    selection.get("weight", 100 if index == 0 else 1)
                )
        self.count = len(self.initial)
        self.refresh_total()

    def selections(self) -> list[dict]:
        selections = []
        for index, select in enumerate(self.query(Select)):
            if select.value is Select.NULL:
                continue
            value = self.query_one(f"#weight-{index}", Input).value
            try:
                weight = int(value)
            except ValueError:
                weight = 0
            selections.append(
                {
                    "benchmark_id": str(select.value),
                    "weight": weight,
                }
            )
        return selections

    @property
    def edited(self) -> bool:
        return self.selections() != self.initial

    def remember_saved(self, selections: list[dict]):
        self.raw = deepcopy(selections)
        self.initial = self.selections()

    def collect(self) -> list[dict]:
        selections = self.selections()
        if not 1 <= len(selections) <= self.limit:
            raise ValueError(
                f"Choose 1–{self.limit} benchmarks or turn Benchmark routing off."
            )
        ids = [s["benchmark_id"] for s in selections]
        if len(set(ids)) != len(ids):
            raise ValueError("Choose distinct benchmarks.")
        if (
            any(s["weight"] < 1 or s["weight"] > 100 for s in selections)
            or sum(s["weight"] for s in selections) != 100
        ):
            raise ValueError(
                "Benchmark weights must be positive integers totaling 100%."
            )
        # Unchanged percentages must not rewrite a saved ratio such as 1:49.
        return deepcopy(self.raw if self.raw and not self.edited else selections)

    @on(Select.Changed)
    @on(Input.Changed)
    def selection_changed(self, event):
        if not self.ready:
            return
        control = event.select if isinstance(event, Select.Changed) else event.input
        if event.value != control.value:
            return
        selected = [
            i for i, s in enumerate(self.query(Select)) if s.value is not Select.NULL
        ]
        with self.prevent(Input.Changed):
            if isinstance(event, Select.Changed) and len(selected) != self.count:
                if len(selected) == 1:
                    self.query_one(f"#weight-{selected[0]}", Input).value = "100"
                elif len(selected) == 2 and self.count == 1:
                    added = int(event.select.id.removeprefix("benchmark-"))
                    for index in selected:
                        self.query_one(f"#weight-{index}", Input).value = (
                            "1" if index == added else "99"
                        )
                self.count = len(selected)
            elif (
                isinstance(event, Input.Changed)
                and len(selected) == 2
                and event.value.isdigit()
            ):
                index = int(event.input.id.removeprefix("weight-"))
                if index in selected:
                    weight = min(99, max(1, int(event.value)))
                    other = next(i for i in selected if i != index)
                    event.input.value = str(weight)
                    self.query_one(f"#weight-{other}", Input).value = str(100 - weight)
        self.refresh_total()

    def refresh_total(self):
        total = sum(s["weight"] for s in self.selections())
        self.query_one("#benchmark-total", Static).update(f"Total: {total}%")
        for index, select in enumerate(self.query(Select)):
            self.query_one(f"#weight-{index}", Input).disabled = (
                select.value is Select.NULL
            )
