"""Stationary ASCII branding with a lime routing trace and sign-in success wave.

Use the original wordmark bitmap and Rich text, never cursor-control escapes.
"""

import math
import os
import time
from collections.abc import Callable
from functools import lru_cache
from heapq import heappop, heappush

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.geometry import Region
from textual.strip import Strip
from textual.timer import Timer
from textual.widget import Widget

from ramp_cli.output.style import _LOGO_BMP_H, _LOGO_BMP_W, _sample_logo
from ramp_cli.router_ui.palette import DARK, Palette, blend

_WORDMARK_GLYPHS = " ▘▝▀▖▌▞▛▗▚▐▜▄▙▟▓"
_GHOSTTY_FULL_CELL = chr(0x28FF)
_GHOSTTY_SUCCESS_GLYPHS = tuple(
    chr(0x2800 | mask) for mask in (0x18, 0x3C, 0x7E, 0xFF, 0xFF)
)
_GHOSTTY_QUADRANTS = (0x03, 0x18, 0x44, 0xA0)
_TRACE_DURATIONS = (1.1, 1.6, 2.2, 1.4, 1.2)
_TRACE_PAUSE = 1.3
# Head and tail lengths, as fractions of each stage's route.
_TRACE_HEAD = 0.12
_TRACE_TAIL = 0.6
# Widest run centered across; wider runs are joins, like the a's bowl and stem.
_MAX_STROKE = 9
# Trace colors are quantized to this many steps; one step is invisible.
_TRACE_LEVELS = 32
# Waypoints in bitmap coordinates: r → a → down/up m's middle → p → mark.
_ROUTE_RECIPES = (
    (0, 14, ((3, 30), (3, 18), (6, 12), (12, 12))),
    (16, 33, ((21, 12), (29, 12), (30, 18), (30, 28))),
    (
        39,
        70,
        (
            (42, 30),
            (42, 14),
            (46, 12),
            (52, 15),
            (53, 30),
            (53, 15),
            (59, 12),
            (67, 15),
            (68, 30),
        ),
    ),
    (76, 98, ((79, 12), (85, 12), (94, 15), (94, 24), (96, 25))),
    (104, 139, ((109, 29), (120, 25), (128, 17), (132, 8), (130, 2))),
)


def _is_ghostty_terminal() -> bool:
    return (
        os.environ.get("TERM_PROGRAM", "").casefold() == "ghostty"
        or os.environ.get("TERM", "").casefold() == "xterm-ghostty"
    )


def _ghostty_braille(mask: int) -> str:
    """Expand a 2x2 source mask into a 2x4 Braille-dot cell."""
    dots = 0
    for bit, quadrant_dots in enumerate(_GHOSTTY_QUADRANTS):
        if mask & (1 << bit):
            dots |= quadrant_dots
    return chr(0x2800 | dots)


def _sample_wordmark(lx: float, ly: float) -> bool:
    """Sample the logo bitmap without two isolated pixels below its baseline."""
    px = int(lx * _LOGO_BMP_W)
    py = int(ly * _LOGO_BMP_H)
    if py == 32 and (21 <= px <= 23 or 86 <= px <= 88):
        return False
    return _sample_logo(lx, ly)


@lru_cache(maxsize=32)
def _wordmark_cells(
    width: int,
    rows: int,
    logo_width: float,
    logo_rows: float,
    sample: Callable[[float, float], bool],
) -> tuple[int, ...]:
    """Keep four source samples per cell, preserving small curves and counters.

    The lettering is stationary, so cache its raster rather than resampling it
    on every animation tick. Including the sampler also isolates test stencils.
    """
    cells = []
    for y in range(rows):
        for x in range(width):
            mask = 0
            for bit, (dx, dy) in enumerate(
                ((-0.25, -0.25), (0.25, -0.25), (-0.25, 0.25), (0.25, 0.25))
            ):
                nx = (x + dx - (width - 1) / 2) / logo_width + 0.5
                ny = (y + dy - (rows - 1) / 2) / logo_rows + 0.5
                if 0 <= nx < 1 and 0 <= ny < 1 and sample(nx, ny):
                    mask |= 1 << bit
            cells.append(mask)
    return tuple(cells)


def _stroke_route(
    width: int, ink: frozenset[int], lettering: tuple[int, ...], start: int, end: int
) -> tuple[int, ...]:
    """Connect waypoints through ink only, favoring the centers of thick strokes."""
    queue = [(0.0, start)]
    distances = {start: 0.0}
    previous = {}
    while queue:
        distance, current = heappop(queue)
        if current == end:
            route = [end]
            while route[-1] != start:
                route.append(previous[route[-1]])
            return tuple(reversed(route))
        if distance > distances[current]:
            continue
        x, y = current % width, current // width
        for dx, dy in (
            (-1, 0),
            (1, 0),
            (0, -1),
            (0, 1),
            (-1, -1),
            (1, -1),
            (-1, 1),
            (1, 1),
        ):
            if not 0 <= x + dx < width:
                continue
            neighbor = (y + dy) * width + x + dx
            if neighbor not in ink:
                continue
            cost = (
                distance
                + math.hypot(dx, 2 * dy)
                + (0.4 if lettering[neighbor] != 15 else 0)
            )
            if cost < distances.get(neighbor, math.inf):
                distances[neighbor] = cost
                previous[neighbor] = current
                heappush(queue, (cost, neighbor))
    return ()


def _stroke_middle(width: int, lettering: tuple[int, ...], x: int, y: int) -> int:
    """The ink-weighted middle column of the stroke through (x, y)."""
    row = y * width
    left = right = x
    while left > 0 and lettering[row + left - 1]:
        left -= 1
    while right < width - 1 and lettering[row + right + 1]:
        right += 1
    if right - left + 1 > _MAX_STROKE:
        return x
    # Edge cells are often partly inked, so weigh each by its quadrants.
    weights = [
        (cx, bin(lettering[row + cx]).count("1")) for cx in range(left, right + 1)
    ]
    return math.floor(
        sum(cx * w for cx, w in weights) / sum(w for _, w in weights) + 0.5
    )


@lru_cache(maxsize=32)
def _routing_paths(
    width: int,
    rows: int,
    logo_width: float,
    logo_rows: float,
    lettering: tuple[int, ...],
) -> tuple[tuple[tuple[int, ...], ...], ...]:
    """One route: r → a → down/up m's middle → p's right edge → mark's base.

    Stems run straight down their middles; curves route through the ink.
    """
    stages = []
    for stage, (left, right, waypoints) in enumerate(_ROUTE_RECIPES):
        ink = frozenset(
            index
            for index, mask in enumerate(lettering)
            if mask
            and left - 1
            <= ((index % width - (width - 1) / 2) / logo_width + 0.5) * _LOGO_BMP_W
            <= right + 1
        )
        if not ink:
            stages.append(())
            continue
        cells = []
        for sx, sy in waypoints:
            tx = (width - 1) / 2 + ((sx + 0.5) / _LOGO_BMP_W - 0.5) * logo_width
            ty = (rows - 1) / 2 + ((sy + 0.5) / _LOGO_BMP_H - 0.5) * logo_rows
            target = min(
                ink,
                key=lambda index: (
                    (index % width - tx) ** 2 + 4 * (index // width - ty) ** 2,
                    index,
                ),
            )
            x, y = target % width, target // width
            # Stems start and turn at the very bottom, even a half-inked row.
            if sy >= 28:
                while (y + 1) * width + x in ink:
                    y += 1
            cells.append([x, y])
        # Straight legs share one x: the stroke's middle halfway along.
        straight = [
            abs(b[0] - a[0]) * 2 <= abs(b[1] - a[1]) for a, b in zip(cells, cells[1:])
        ]
        for n, leg in enumerate(straight):
            if leg:
                a, b = cells[n], cells[n + 1]
                a[0] = b[0] = _stroke_middle(width, lettering, a[0], (a[1] + b[1]) // 2)
        if stage == 0:
            # The r's arm runs out to its tip.
            x, y = cells[-1]
            while x + 1 < width and y * width + x + 1 in ink:
                x += 1
            cells[-1] = [x - 1, y]
        route = [cells[0][1] * width + cells[0][0]]
        for (ax, ay), (bx, by), leg in zip(cells, cells[1:], straight):
            step = 1 if by > ay else -1
            line = [y * width + ax for y in range(ay + step, by + step, step)]
            end = by * width + bx
            if leg and ax == bx and all(index in ink for index in line):
                route.extend(line)
            elif end in ink and route[-1] != end:
                route.extend(_stroke_route(width, ink, lettering, route[-1], end)[1:])
        # The m's middle is meant to be revisited; only drop repeats in place.
        path = tuple(
            index for n, index in enumerate(route) if n == 0 or index != route[n - 1]
        )
        stages.append((path,) if len(path) > 1 else ())
    return tuple(stages)


@lru_cache(maxsize=96)
def _path_positions(width: int, path: tuple[int, ...]) -> tuple[tuple[int, float], ...]:
    lengths = [0.0]
    for first, second in zip(path, path[1:]):
        lengths.append(
            lengths[-1]
            + math.hypot(
                second % width - first % width, 2 * (second // width - first // width)
            )
        )
    total = lengths[-1] or 1.0
    return tuple((index, length / total) for index, length in zip(path, lengths))


def _trace_idle(elapsed: float) -> bool:
    """Whether the loop is in its closing pause, after the last stage fades out.

    Every frame in that window is the same unlit wordmark.
    """
    traced = sum(_TRACE_DURATIONS)
    tick = elapsed % (traced + _TRACE_PAUSE)
    return tick > traced + _TRACE_TAIL * _TRACE_DURATIONS[-1]


def _routing_intensities(
    width: int, paths: tuple[tuple[tuple[int, ...], ...], ...], elapsed: float
) -> dict[int, float]:
    """A single forward-moving head; only already-traversed cells form the tail."""
    tick = elapsed % (sum(_TRACE_DURATIONS) + _TRACE_PAUSE)
    start = 0.0
    lit = {}
    for stage, (branches, duration) in enumerate(zip(paths, _TRACE_DURATIONS)):
        progress = (tick - start) / duration
        start += duration
        if not 0 <= progress <= 1 + _TRACE_TAIL:
            continue
        for path in branches:
            positions = _path_positions(width, path)
            travelled = [
                (index, position)
                for index, position in positions
                if position <= min(progress, 1.0)
            ]
            for index, position in travelled:
                lag = progress - position
                head = math.exp(-((lag / _TRACE_HEAD) ** 2))
                trail = max(0.0, 1 - lag / _TRACE_TAIL) ** 2
                intensity = max(head, trail)
                if intensity > 0.005:
                    lit[index] = max(lit.get(index, 0.0), intensity)
            # Never prelight the next step.
            if progress <= 1 and travelled:
                lit[travelled[-1][0]] = 1.0
        # Settle only on the endpoint; don't relight the path in reverse.
        if stage == len(paths) - 1 and 0.9 <= progress <= 1 + _TRACE_TAIL:
            glow = (
                0.45 * math.sin(math.pi * (progress - 0.9) / (0.1 + _TRACE_TAIL)) ** 2
            )
            for path in branches:
                index = path[-1]
                if progress >= 1:
                    lit[index] = max(lit.get(index, 0.0), glow)
    return lit


def _logo_size(width: int, rows: int) -> tuple[float, float]:
    # Keep the wordmark's size and original aspect ratio. Terminal cells are
    # roughly twice as tall as they are wide.
    logo_aspect = _LOGO_BMP_W / _LOGO_BMP_H
    logo_width = min(
        max(1, width - 3) * 0.60, max(1, rows - 2) * 0.65 * 2 * logo_aspect
    )
    return logo_width, logo_width / (2 * logo_aspect)


def wordmark_bottom(width: int, rows: int) -> int:
    """The last frame row the wordmark inks; traces only travel through ink."""
    width, rows = max(1, width), max(1, rows)
    lettering = _wordmark_cells(width, rows, *_logo_size(width, rows), _sample_wordmark)
    for y in range(rows - 1, -1, -1):
        if any(lettering[y * width : (y + 1) * width]):
            return y
    return rows - 1


def animation_frame(
    width: int,
    rows: int,
    elapsed: float,
    *,
    success: bool = False,
    palette: Palette = DARK,
    ghostty: bool = False,
) -> Text:
    width = max(1, width)
    rows = max(1, rows)
    frame = Text(no_wrap=True)
    clock = elapsed * 0.1
    logo_width, logo_rows = _logo_size(width, rows)
    lettering = (
        ()
        if success
        else _wordmark_cells(width, rows, logo_width, logo_rows, _sample_wordmark)
    )
    paths = (
        () if success else _routing_paths(width, rows, logo_width, logo_rows, lettering)
    )
    trace = {} if success else _routing_intensities(width, paths, elapsed)
    base = palette.animation_shade(0.5, True)
    accent = palette.animation_accent(0.5)
    for y in range(rows):
        for x in range(width):
            if success:
                middle = max(1, rows // 2)
                direction = 1 if y < middle else -1
                glyphs = _GHOSTTY_SUCCESS_GLYPHS if ghostty else "▒▒▓▓█"
                char = glyphs[
                    round(abs(x + y + int(400 * clock) * direction)) % len(glyphs)
                ]
                distance = abs(y - middle) / middle
                wave = (
                    math.sin(
                        math.sin(math.pi * distance + 6 * clock)
                        * math.cos(x * 0.1 + 4 * clock)
                        + math.sin(x * 0.07 + 2 * distance + 5 * clock)
                    )
                    + 1
                ) / 2
                color = (
                    palette.rich("success")
                    if palette.native
                    else blend(palette.background, palette.success, 0.55 + wave * 0.45)
                )
            else:
                logo = lettering[y * width + x]
                if logo:
                    char = _ghostty_braille(logo) if ghostty else _WORDMARK_GLYPHS[logo]
                    intensity = trace.get(y * width + x, 0.0)
                    color = (
                        (accent if intensity > 0.25 else base)
                        if palette.native
                        else blend(base, accent, intensity)
                    )
                    if intensity > 0.85 and logo == 15 and not ghostty:
                        char = "█"
                else:
                    char, color = " ", palette.rich("background")
            frame.append(char, style=color)
        if y < rows - 1:
            frame.append("\n")
    return frame


def screen_shows(screen) -> bool:
    """Whether a screen is on top or shows through translucent screens above it.

    Stricter than `Screen.is_current`, which always counts the screen just below
    the top one, even under an opaque page.
    """
    try:
        stack = screen.app.screen_stack
    except Exception:
        return False
    for above in reversed(stack):
        if above is screen:
            return True
        if above.styles.background.a >= 1:
            return False
    return False


class _WordmarkFrames:
    """The wordmark at one size: cached unlit rows, rebuilt only where lit."""

    def __init__(self, full, width, rows, palette, ghostty):
        self.full, self.width, self.ghostty = full, width, ghostty
        self.left = (full - width) // 2
        self.right = full - width - self.left
        logo_width, logo_rows = _logo_size(width, rows)
        self.lettering = _wordmark_cells(
            width, rows, logo_width, logo_rows, _sample_wordmark
        )
        self.paths = _routing_paths(width, rows, logo_width, logo_rows, self.lettering)
        self.glyphs = [
            (_ghostty_braille(mask) if ghostty else _WORDMARK_GLYPHS[mask])
            if mask
            else " "
            for mask in self.lettering
        ]
        base = palette.animation_shade(0.5, True)
        accent = palette.animation_accent(0.5)
        # No background: the widget's live one is applied as rows are drawn.
        self.blank = Style.parse(palette.rich("background"))
        self.levels = [
            Style.parse(
                (accent if level / _TRACE_LEVELS > 0.25 else base)
                if palette.native
                else blend(base, accent, level / _TRACE_LEVELS)
            )
            for level in range(_TRACE_LEVELS + 1)
        ]
        self.base = [self.row(y, None) for y in range(rows)]

    def row(self, y: int, lit: dict[int, float] | None) -> Strip:
        segments = []
        if self.left:
            segments.append(Segment(" " * self.left, self.blank))
        text, style = [], None
        for x in range(self.width):
            index = y * self.width + x
            mask = self.lettering[index]
            if not mask:
                char, cell = " ", self.blank
            else:
                intensity = lit.get(x, 0.0) if lit else 0.0
                char = self.glyphs[index]
                if intensity > 0.85 and mask == 15 and not self.ghostty:
                    char = "█"
                cell = self.levels[round(intensity * _TRACE_LEVELS)]
            if cell is not style:
                if text:
                    segments.append(Segment("".join(text), style))
                text, style = [char], cell
            else:
                text.append(char)
        if text:
            segments.append(Segment("".join(text), style))
        if self.right:
            segments.append(Segment(" " * self.right, self.blank))
        return Strip(segments, self.full)

    def lit_rows(self, elapsed: float) -> dict[int, tuple[dict[int, float], tuple]]:
        """Lit cells by row, each with a signature that changes iff the row does."""
        rows: dict[int, dict[int, float]] = {}
        for index, intensity in _routing_intensities(
            self.width, self.paths, elapsed
        ).items():
            rows.setdefault(index // self.width, {})[index % self.width] = intensity
        return {
            y: (
                cells,
                tuple(
                    sorted(
                        (x, round(value * _TRACE_LEVELS), value > 0.85)
                        for x, value in cells.items()
                    )
                ),
            )
            for y, cells in rows.items()
        }


class BrandAnimation(Widget):
    DEFAULT_CSS = """
    BrandAnimation { height: 8; margin: 1 0; overflow: hidden; }
    """

    def __init__(
        self,
        *,
        id: str | None = None,
        rows: int = 8,
        ambient: bool = False,
        max_width: int = 72,
    ):
        super().__init__(id=id)
        self.display = False
        self.started = 0.0
        self.success = False
        self.rows = rows
        self.ambient = ambient
        self.max_width = max_width
        self.timer: Timer | None = None
        self.frame_key: tuple | None = None
        self.frames: _WordmarkFrames | None = None
        self.frames_key: tuple | None = None
        self.strips: list[Strip] = []
        self.lit: dict[int, tuple] = {}
        # When the terminal lost focus; the trace clock stands still until refocus.
        self.blurred_at: float | None = None

    def on_mount(self):
        self.timer = self.set_interval(1 / 15, self.draw, pause=True)
        self.app.screen_change_signal.subscribe(self, lambda _screen: self.sync())
        self.sync()

    def sync(self):
        """Tick only while frames can change on screen.

        Reduced motion draws one static frame; the sign-in wave still ticks so it
        can end on time. Resize and palette changes redraw directly. Nothing
        ticks while the terminal is unfocused; the trace resumes where it left
        off, while a sign-in wave that ran out meanwhile ends on refocus.
        """
        if self.timer is None:
            return
        focused = getattr(self.app, "app_focus", True)
        if not focused and self.blurred_at is None:
            self.blurred_at = time.monotonic()
        elif focused and self.blurred_at is not None:
            if not self.success:
                self.started += time.monotonic() - self.blurred_at
            self.blurred_at = None
        try:
            visible = screen_shows(self.screen) and self.is_on_screen
        except Exception:
            visible = False
        running = (
            self.display
            and focused
            and visible
            and (self.success or not getattr(self.app, "reduced_motion", False))
        )
        self.timer.resume() if running else self.timer.pause()

    def on_resize(self):
        # Also sent when the widget is shown again after being hidden.
        self.sync()
        self.draw()

    def on_show(self):
        self.sync()

    def on_hide(self):
        if self.timer is not None:
            self.timer.pause()

    def start(self, *, success: bool = False):
        self.success = success
        self.styles.height = 3 if success else self.rows
        self.started = time.monotonic()
        self.blurred_at = None
        self.display = True
        self.sync()
        self.draw()

    def stop(self):
        self.display = False
        self.frame_key = None
        self.sync()

    def render_line(self, y: int) -> Strip:
        # Like Static, take the background from the current styles every draw,
        # so theme and terminal color changes never leave a stale block.
        if y < len(self.strips):
            return self.strips[y].apply_style(self.rich_style)
        return Strip.blank(self.content_size.width, self.rich_style)

    def draw(self):
        if not self.display or not self.is_mounted:
            return
        elapsed = (self.blurred_at or time.monotonic()) - self.started
        if self.success and elapsed > 1.2:
            if self.ambient:
                self.start()
            else:
                self.stop()
            return
        reduced = getattr(self.app, "reduced_motion", False)
        full = self.content_size.width
        width = full if self.success else min(full, self.max_width)
        rows = 3 if self.success else self.rows
        elapsed = 1.2 if reduced else elapsed
        palette = self.app.palette
        ghostty = _is_ghostty_terminal()
        # Skip identical frames: reduced motion and the loop's closing pause.
        phase = None if not self.success and _trace_idle(elapsed) else elapsed
        key = (width, rows, self.success, palette, ghostty, phase, full)
        if key == self.frame_key:
            return
        self.frame_key = key
        if self.success:
            # The short sign-in wave changes every cell, so draw it whole.
            self.frames_key = None
            frame = animation_frame(
                width, rows, elapsed, success=True, palette=palette, ghostty=ghostty
            )
            console = self.app.console
            self.strips = [
                Strip(line.render(console), width) for line in frame.split("\n")
            ]
            self.refresh()
            return
        frames_key = (full, width, rows, palette, ghostty)
        if frames_key != self.frames_key:
            self.frames_key = frames_key
            self.frames = _WordmarkFrames(*frames_key)
            self.strips = list(self.frames.base)
            self.lit = {}
            self.refresh()
        frames = self.frames
        lit = frames.lit_rows(elapsed)
        top, left = self.gutter.top, self.gutter.left
        for y in self.lit.keys() | lit.keys():
            cells, signature = lit.get(y, (None, None))
            if signature == self.lit.get(y):
                continue
            self.strips[y] = frames.row(y, cells) if cells else frames.base[y]
            # Only rows the trace changed are re-rendered and re-composited.
            self.refresh(Region(left, top + y, full, 1))
        self.lit = {y: signature for y, (_, signature) in lit.items()}
