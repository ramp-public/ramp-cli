"""A vertical usage chart: stacked spend columns spanning the full width."""

import math
from datetime import datetime
from decimal import Decimal

from rich.cells import cell_len
from rich.text import Text

from ramp_cli.commands.router_user import _money
from ramp_cli.router_ui.palette import readable

# app.router.com's muted chart ramp (periwinkle, rose, teal, plum, olive),
# handed out by spend rank just like the web dashboard.
SERIES_COLORS = ("#7694d2", "#c8808e", "#4c6d66", "#694163", "#a1954f")
EIGHTHS = " ▁▂▃▄▅▆▇"


def series_colors(series: list, palette, by_model: bool = True) -> list[str]:
    """The web ramp in rank order; Today can list more models, so it cycles."""
    return [
        SERIES_COLORS[index % len(SERIES_COLORS)]
        if palette.native
        else readable(
            SERIES_COLORS[index % len(SERIES_COLORS)], palette.background, 1.5
        )
        for index in range(len(series))
    ]


def _slots(width: int, count: int) -> list[int]:
    base, extra = divmod(width, count)
    return [base + (1 if index < extra else 0) for index in range(count)]


def _stack(spends: list, total: Decimal, scale: Decimal, height: int):
    """Whole rows per series bottom-up, plus an eighth-block cap for the rest."""
    if total <= 0 or scale <= 0:
        return [], 0, None
    eighths = max(1, round(height * 8 * total / scale))
    full, remainder = divmod(eighths, 8)
    nonzero = [(index, spend) for index, spend in enumerate(spends) if spend > 0]
    top = nonzero[-1][0] if nonzero else None
    if full < len(nonzero):
        # Too short to give every series a row: the largest ones get them.
        ranked = sorted(nonzero, key=lambda item: -item[1])[:full]
        keep = {index for index, _ in ranked}
        nonzero = [item for item in nonzero if item[0] in keep]
    counts = [max(1, int(full * spend / total)) for _, spend in nonzero]
    if counts:
        # The largest series absorbs rounding so the column keeps its height.
        largest = max(range(len(counts)), key=lambda i: nonzero[i][1])
        counts[largest] = max(1, counts[largest] + full - sum(counts))
    rows = [index for (index, _), count in zip(nonzero, counts) for _ in range(count)]
    return rows, remainder, top


def _place(line: list, start: int, text: str, style: str, floor: int) -> int:
    """Write ``text`` into ``line`` if it fits past ``floor``; return the new floor."""
    start = max(start, floor, 0)
    if start + len(text) > len(line):
        return floor
    for offset, char in enumerate(text):
        line[start + offset] = (char, style)
    return start + len(text) + 1


def _render(lines: list) -> Text:
    text = Text()
    for number, line in enumerate(lines):
        if number:
            text.append("\n")
        run, style = "", None
        for char, cell_style in line:
            if cell_style != style and run:
                text.append(run, style=style)
                run = ""
            run += char
            style = cell_style
        text.append(run, style=style)
    return text


def chart_series(usage: dict) -> list:
    """Today's columns are every model (or key); longer windows' are the groups."""
    if usage.get("days") == 1:
        by_model = usage.get("group_by") == "model"
        rows = usage.get("models") if by_model and usage.get("models") else None
        return [
            row
            for row in rows or usage.get("groups") or []
            if row["spend_usd"] > 0 or row["request_count"]
        ]
    return usage.get("groups") or []


def usage_legend(usage: dict, palette) -> Text:
    """One line of color swatches and names; the marquee scrolls it when long."""
    series = chart_series(usage)
    colors = series_colors(series, palette, usage.get("group_by") == "model")
    legend = Text(no_wrap=True, end="")
    for index, (item, color) in enumerate(zip(series, colors)):
        if index:
            legend.append("  ")
        legend.append("██ ", style=color)
        legend.append(item["label"])
    return legend


def usage_chart(
    usage: dict, palette, width: int, height: int, progress: float = 1.0
) -> Text:
    """Today puts each model (or key) in its own column; longer windows stack days.

    ``progress`` below 1 draws every bar that fraction of its height against
    the final axis, for the intro animation.
    """
    muted = palette.rich("muted")
    by_model = usage.get("group_by") == "model"
    series = chart_series(usage)
    if usage.get("days") == 1:
        if not series:
            return Text("No usage yet today.", style=muted)
        columns = [
            (
                row["label"],
                [
                    row["spend_usd"] if j == i else Decimal(0)
                    for j in range(len(series))
                ],
            )
            for i, row in enumerate(series)
        ]
        labels = [[label] for label, _ in columns]
    else:
        if not series:
            return Text("No usage in this window.", style=muted)
        days = usage.get("series") or []
        columns = [
            (day["date"], [day["groups"][g["id"]]["spend_usd"] for g in series])
            for day in days
        ]
        dates = [datetime.fromisoformat(day["date"]) for day in days]
        labels = [
            [date.strftime(form) for form in ("%a %b %d", "%b %d", "%d")]
            for date in dates
        ]
    colors = series_colors(series, palette, by_model)
    totals = [sum(spends, Decimal(0)) for _, spends in columns]
    scale = max(totals, default=Decimal(0))
    # Room for a value row, its gap above the bar, the baseline, and the axis.
    plot = max(4, height - 4)
    ticks = {0: _money(scale), plot - 1: _money(0)}
    if plot >= 7:
        ticks[plot // 2] = _money(scale * (plot - 1 - plot // 2) / (plot - 1))
    gutter = max(len(tick) for tick in ticks.values()) + 1
    inner = max(len(columns), width - gutter - 1)
    # Every column is equally wide; leftover cells widen the gaps, not the bars.
    count = len(columns)
    gap = 0 if inner < 2 * count else max(1, int(inner / count / 4))
    bar = max(1, (inner - (count - 1) * gap) // count)
    slots = [bar + extra for extra in _slots(inner - bar * count, count)]
    # Rows: values and a gap above the plot, the plot, then the baseline.
    grid = [[(" ", None)] * inner for _ in range(plot + 3)]
    grown = Decimal(str(round(progress, 4)))
    left = 0
    for (_, spends), total, slot in zip(columns, totals, slots):
        start = left + (slot - bar) // 2
        rows, remainder, top = _stack(
            [spend * grown for spend in spends], total * grown, scale, plot
        )
        for level, index in enumerate(rows):
            for x in range(start, start + bar):
                grid[plot + 1 - level][x] = ("█", colors[index])
        cap = plot + 1 - len(rows)
        if remainder and top is not None:
            for x in range(start, start + bar):
                grid[cap][x] = (EIGHTHS[remainder], colors[top])
            cap -= 1
        value = _money(total) if total else ""
        if value and len(value) <= slot and cap >= 1:
            _place(grid[cap - 1], left + (slot - len(value)) // 2, value, muted, left)
        left += slot
    grid[-1] = [("─", muted)] * inner
    # Axis labels use the longest format that fits; dates too wide for every
    # column label every few, counted back so today is always labeled.
    narrowest = min(slots)
    form = next(
        (
            index
            for index in range(len(labels[0]))
            if all(len(options[index]) < narrowest for options in labels)
        ),
        len(labels[0]) - 1,
    )
    widest = max(len(options[min(form, len(options) - 1)]) for options in labels)
    stride = 1 if len(labels[0]) == 1 else math.ceil((widest + 1) / narrowest)
    axis = [(" ", None)] * inner
    floor, left = 0, 0
    for index, (options, slot) in enumerate(zip(labels, slots)):
        label = options[min(form, len(options) - 1)]
        if cell_len(label) >= slot and len(options) == 1:
            label = label[: max(1, slot - 2)] + "…" if slot > 2 else ""
        if label and (len(labels) - 1 - index) % stride == 0:
            floor = _place(axis, left + (slot - len(label)) // 2, label, muted, floor)
        left += slot
    lines = []
    for row, cells in enumerate(grid):
        tick = ticks.get(row - 2, "")
        edge = "┤" if row - 2 in ticks else "└" if row == len(grid) - 1 else "│"
        if row < 2:
            edge = " "
        lines.append([(char, muted) for char in tick.rjust(gutter - 1) + " " + edge])
        lines[-1].extend(cells)
    lines.append([(" ", None)] * (gutter + 1) + axis)
    return _render(lines)
