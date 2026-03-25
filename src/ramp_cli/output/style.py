"""Visual design system for CLI output.

Implements the ramp-cli terminal UI designs: box-drawing card frames,
animated binary matrix for WAITING, green block art for SUCCESS,
and styled ACCESS DENIED display. Uses Unicode box-drawing characters
and ANSI escape codes — no external dependencies beyond click.
"""

from __future__ import annotations

import base64
import functools
import io
import math
import os
import re
import shutil
import sys
import threading
import time
from typing import Any, Callable, TextIO

import click

# === Width Limits ===
_WIDTH_MIN = 80
_WIDTH_MAX = 120
_TABLE_WIDTH_MAX = 300  # tables need more room for UUIDs & wide data

# === Window Chrome ===
_WIN_BG = (38, 38, 38)  # #262626 — window content background
_SHADOW = (58, 58, 58)  # #3a3a3a — drop shadow
_MARGIN = 2  # horizontal margin chars each side
_SHADOW_W = 1  # shadow width (right edge)
_HEADER_BG = (88, 88, 88)  # #585858 — header row background

# === Symbols ===
DIAMOND_FILLED = "\u25c6"  # ◆  authenticated
DIAMOND_HOLLOW = "\u25c7"  # ◇  not authenticated

# === Box Drawing ===
BOX_H = "\u2500"  # ─
BOX_V = "\u2502"  # │
BOX_TL = "\u250c"  # ┌
BOX_TR = "\u2510"  # ┐
BOX_BL = "\u2514"  # └
BOX_BR = "\u2518"  # ┘

# === Block Characters ===
BLOCKS_DENIED = "\u2591\u2592\u2593\u2588\u2593\u2592"  # ░▒▓█▓▒

DENSITY_WAVE = "░▒▓█"

_LOGO_LINES = ["▙▀▖▝▀▖▛▚▀▖▛▀▖", "▌  ▞▀▌▌▐ ▌▙▄▘", "▘  ▝▀▘▘▝ ▘▌  "]
_LOGO_W = 13
_LOGO_H = 3

# === ANSI Escape Helpers ===
ESC = "\033"


def _color_supported(file: TextIO) -> bool:
    if not hasattr(file, "isatty") or not file.isatty():
        return False
    if os.environ.get("NO_COLOR"):
        return False
    return True


@functools.lru_cache(maxsize=256)
def _fg(r: int, g: int, b: int) -> str:
    return f"{ESC}[38;2;{r};{g};{b}m"


def _bold() -> str:
    return f"{ESC}[1m"


def _reset() -> str:
    return f"{ESC}[0m"


def _reset_fg() -> str:
    """Reset bold/dim + foreground only. Preserves background."""
    return f"{ESC}[22m{ESC}[39m"


def _bg(r: int, g: int, b: int) -> str:
    """24-bit background color."""
    return f"{ESC}[48;2;{r};{g};{b}m"


def _bg_default() -> str:
    """Terminal default background."""
    return f"{ESC}[49m"


def _ansi_visible_len(text: str) -> int:
    """Return visible character count, ignoring ANSI escape sequences."""
    return len(re.sub(r"\033\[[0-9;]*m", "", text))


def _ansi_truncate(text: str, max_visible: int) -> str:
    """Truncate to max_visible printable chars, preserving complete ANSI sequences.

    Appends '...' if truncated, closes with _reset_fg().
    """
    if _ansi_visible_len(text) <= max_visible:
        return text
    # We need to fit in max_visible chars including "..."
    target = max(0, max_visible - 3)
    visible = 0
    i = 0
    result: list[str] = []
    while i < len(text) and visible < target:
        m = re.match(r"\033\[[0-9;]*m", text[i:])
        if m:
            result.append(m.group())
            i += m.end()
            continue
        result.append(text[i])
        visible += 1
        i += 1
    return "".join(result) + "..." + _reset_fg()


def _hide_cursor() -> str:
    return f"{ESC}[?25l"


def _show_cursor() -> str:
    return f"{ESC}[?25h"


def _move_up(n: int) -> str:
    return f"{ESC}[{n}A" if n > 0 else ""


def _move_to(row: int, col: int) -> str:
    """Absolute cursor positioning (1-based)."""
    return f"{ESC}[{row};{col}H"


def _clear_screen() -> str:
    """Clear entire screen and home cursor."""
    return f"{ESC}[2J{ESC}[H"


def _clear_eos() -> str:
    """Clear from cursor to end of screen."""
    return f"{ESC}[J"


def _clear_eol() -> str:
    """Clear from cursor to end of line."""
    return f"{ESC}[K"


def _clear_line() -> str:
    return f"{ESC}[2K"


def _alt_screen_on() -> str:
    return f"{ESC}[?1049h"


def _alt_screen_off() -> str:
    return f"{ESC}[?1049l"


# === Terminal Input ===

_MAX_ESCAPE_DRAIN = 16  # max bytes to drain from an escape sequence


def _read_key() -> str:
    """Read a single keypress in raw mode.

    Returns 'left', 'right', 'up', 'down', 'enter', 'esc', 'q', or the char.
    Shared primitive used by interactive views (paginator, etc.).
    """
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = os.read(fd, 1)
        if ch == b"\x1b":
            if select.select([fd], [], [], 0.3)[0]:
                ch2 = os.read(fd, 1)
                if ch2 in (b"[", b"O") and select.select([fd], [], [], 0.3)[0]:
                    ch3 = os.read(fd, 1)
                    for _ in range(_MAX_ESCAPE_DRAIN):
                        if not select.select([fd], [], [], 0.01)[0]:
                            break
                        os.read(fd, 1)
                    if ch3 == b"C":
                        return "right"
                    if ch3 == b"D":
                        return "left"
                    if ch3 == b"A":
                        return "up"
                    if ch3 == b"B":
                        return "down"
                for _ in range(_MAX_ESCAPE_DRAIN):
                    if not select.select([fd], [], [], 0.01)[0]:
                        break
                    os.read(fd, 1)
            return "esc"
        if ch == b"\r" or ch == b"\n":
            return "enter"
        if ch == b"q":
            return "q"
        if ch == b"\x03":  # Ctrl+C
            return "esc"
        return ch.decode("utf-8", errors="replace")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# === Layout Helpers ===


def _term_width() -> int:
    return shutil.get_terminal_size((80, 24)).columns


def _frame_top(title: str, width: int, use_color: bool = True) -> str:
    """Build: ┌─── title ───...───┐"""
    inner = width - 2
    title_display = f" {title} "
    left_pad = 3
    right_pad = inner - left_pad - len(title_display)
    if right_pad < 1:
        right_pad = 1
    if use_color:
        return (
            f"{BOX_TL}{BOX_H * left_pad}"
            f"{_bold()}{title_display}{_reset()}"
            f"{BOX_H * right_pad}{BOX_TR}"
        )
    return f"{BOX_TL}{BOX_H * left_pad}{title_display}{BOX_H * right_pad}{BOX_TR}"


def _frame_top_ansi(title_ansi: str, title_visible_len: int, width: int) -> str:
    """Build frame top with pre-colored title (ANSI codes already embedded)."""
    inner = width - 2
    left_pad = 3
    right_pad = inner - left_pad - title_visible_len - 2  # -2 for spaces
    if right_pad < 1:
        right_pad = 1
    return f"{BOX_TL}{BOX_H * left_pad} {title_ansi} {BOX_H * right_pad}{BOX_TR}"


def _frame_bottom(width: int) -> str:
    return f"{BOX_BL}{BOX_H * (width - 2)}{BOX_BR}"


def _frame_row(text: str, width: int) -> str:
    """Build a frame row with plain text, padded to width."""
    inner = width - 4
    return f"{BOX_V} {text[:inner]:<{inner}} {BOX_V}"


def _frame_row_ansi(text: str, visible_len: int, width: int) -> str:
    """Build a frame row with ANSI-colored text, padded by visible length."""
    inner = width - 4
    padding = " " * max(0, inner - visible_len)
    return f"{BOX_V} {text}{padding} {BOX_V}"


# === Window Shadow + Framing ===


def _window_wrap(lines: list[str], width: int) -> list[str]:
    """Wrap rendered content lines with margin and 1ch offset hard shadow.

    Matches the spec: box-shadow: 1ch 1ch 0 0 (right + bottom, no blur).
    - Right edge: ▐ on every line except the first (offset 1 row down)
    - Bottom edge: ▄ row offset 1 char right
    """
    margin = " " * _MARGIN
    shadow_fg = _fg(*_SHADOW)
    result: list[str] = []
    result.append("")  # blank line above

    for i, line in enumerate(lines):
        if i == 0:
            result.append(f"{margin}{line}")
        else:
            result.append(f"{margin}{line}{shadow_fg}\u2590{_reset()}")

    # Bottom shadow: ▄ chars, offset 1 char right, width chars wide
    shadow_bar = "\u2584" * width
    result.append(f"{margin} {shadow_fg}{shadow_bar}{_reset()}")
    result.append("")  # blank line below

    return result


# === Content Styling ===


def _gradient_text(
    text: str,
    start: tuple[int, int, int] = (255, 255, 255),
    end: tuple[int, int, int] = (180, 180, 180),
) -> str:
    """Per-character linear RGB interpolation. White->light-gray default."""
    if not text:
        return text
    n = max(len(text) - 1, 1)
    chars: list[str] = []
    for i, ch in enumerate(text):
        t = i / n
        r = int(start[0] + (end[0] - start[0]) * t)
        g = int(start[1] + (end[1] - start[1]) * t)
        b = int(start[2] + (end[2] - start[2]) * t)
        chars.append(f"{_fg(r, g, b)}{ch}")
    chars.append(_reset_fg())
    return "".join(chars)


def _render_button(hotkey: str, label: str) -> str:
    """Render styled hotkey button: [hotkey bg] [label bg]."""
    return (
        f"{_bg(138, 138, 138)}{_fg(255, 255, 255)} {hotkey} {_reset()}"
        f"{_bg(78, 78, 78)}{_fg(168, 168, 168)} {label.upper()} {_reset()}"
    )


# === Public: Simple Helpers ===


def env_label(env: str) -> str:
    """Normalize environment name: 'sandbox' -> 'Sandbox'."""
    return env.capitalize()


def status_line(authenticated: bool) -> str:
    """Return '◆ authenticated' or '◇ not authenticated'."""
    if authenticated:
        return f"{DIAMOND_FILLED} authenticated"
    return f"{DIAMOND_HOLLOW} not authenticated"


def colored_status_line(authenticated: bool, use_color: bool = True) -> str:
    """Authenticated: bright green bold. Not authenticated: dim gray."""
    text = status_line(authenticated)
    if not use_color:
        return text
    if authenticated:
        return f"{_bold()}{_fg(0, 255, 0)}{text}{_reset()}"
    return f"{_fg(80, 80, 80)}{text}{_reset()}"


def show_status_box(envs: list[tuple[str, bool]], file: TextIO | None = None) -> None:
    """Print a framed status card showing auth state per environment.

    envs: list of (label, is_authenticated) tuples.
    """
    file = file or sys.stdout
    use_color = _color_supported(file)
    width = max(_WIDTH_MIN, min(_term_width(), _WIDTH_MAX))
    inner = width - 4

    top = _frame_top("Status", width, use_color)
    bottom = _frame_bottom(width)

    lines = [top]
    for label, authenticated in envs:
        status = colored_status_line(authenticated, use_color)
        label_part = f"{label:<12}"
        if use_color:
            row_content = f"{label_part} {status}"
            visible_len = 12 + 1 + len(status_line(authenticated))
            padding = " " * max(0, inner - visible_len)
            lines.append(f"{BOX_V} {row_content}{padding} {BOX_V}")
        else:
            row = f"{label_part} {status}"
            lines.append(f"{BOX_V} {row[:inner]:<{inner}} {BOX_V}")
    lines.append(bottom)

    file.write("\n" + "\n".join(lines) + "\n\n")
    file.flush()


def header(text: str) -> None:
    """Print bold header with separator to stdout."""
    click.echo(click.style(text, bold=True))
    click.echo(BOX_H * len(text))


# === NYC Skyline (mode-14 port) ===


def _scene_hash(n: float) -> float:
    """Deterministic pseudo-random float in [0,1). Port of sceneHash() from utilities.ts."""
    return ((math.sin(n * 127.1 + 311.7) * 43758.5453) % 1 + 1) % 1


_NYC_WATERLINE = 0.767
_NYC_BUILDINGS: list[dict] = [
    {"x": 0.06, "w": 0.07, "h": 0.22, "layer": 0},
    {"x": 0.17, "w": 0.09, "h": 0.28, "layer": 0},
    {"x": 0.28, "w": 0.06, "h": 0.18, "layer": 0},
    {"x": 0.42, "w": 0.08, "h": 0.25, "layer": 0},
    {"x": 0.58, "w": 0.07, "h": 0.20, "layer": 0},
    {"x": 0.72, "w": 0.09, "h": 0.24, "layer": 0},
    {"x": 0.85, "w": 0.06, "h": 0.16, "layer": 0},
    {"x": 0.96, "w": 0.08, "h": 0.22, "layer": 0},
    {"x": 0.10, "w": 0.08, "h": 0.35, "layer": 1, "type": "stepped"},
    {"x": 0.30, "w": 0.07, "h": 0.48, "layer": 1, "type": "empire"},
    {"x": 0.42, "w": 0.06, "h": 0.40, "layer": 1, "type": "chrysler"},
    {"x": 0.62, "w": 0.08, "h": 0.32, "layer": 1, "type": "artdeco"},
    {"x": 0.78, "w": 0.07, "h": 0.36, "layer": 1, "type": "stepped"},
    {"x": 0.92, "w": 0.06, "h": 0.28, "layer": 1},
    {"x": 0.08, "w": 0.10, "h": 0.32, "layer": 2},
    {"x": 0.38, "w": 0.035, "h": 0.52, "layer": 2, "type": "432park"},
    {"x": 0.55, "w": 0.07, "h": 0.58, "layer": 2, "type": "freedom"},
    {"x": 0.75, "w": 0.09, "h": 0.35, "layer": 2},
    {"x": 0.93, "w": 0.08, "h": 0.28, "layer": 2},
]


# Ramp wordmark bitmap — 140×40, rasterized from the actual SVG path (same as
# reference utilities.ts initLogoBitmap). Packed MSB-first, 1 bit per pixel.
_LOGO_BMP_W = 140
_LOGO_BMP_H = 40
_LOGO_BMP_DATA: bytes = base64.b64decode(
    "AAAAAAAAAAAAAAAAAAAAACAAAAAAAAAAAAAAAAAAAAAAAwAAAAAAAAAAAAAAAAAAAAAAOAAA"
    "AAAAAAAAAAAAAAAAAAADwAAAAAAAAAAAAAAAAAAAAAB+AAAAAAAAAAAAAAAAAAAAAAfwAAAA"
    "AAAAAAAAAAAAAAAAAH8AAAAAAAAAAAAAAAAAAAAAD/AAAAAAAAAAAAAAAAAAAAAA/g/H4H+A"
    "Hx+A/gD4fwAAAAAP4Pz+H/4B8/4f8A+f/AAAAAH+D9/D//Aff/P/gPv/4AAAAB/g//x//4H/"
    "/3/8D///AAAAA/wP/4/h+B/3///g/9/wAAAAf8D/yPwPwfwf4H4P8D+AAAAH/A/wB4B8H4D+"
    "B+D8AfwAAAD/gP4AEAfB+A/APg/AD8AAAB/4D8AAAPwfAPwD4PgA/AAAA/8A/AAA/8HwD8A+"
    "D4AHwAAAf+APwAH//B8A/APg+AB8AAAP/gD8AH/3wfAPwD4PgAfgAAH/wA/AD/h8HwD8A+D4"
    "AHwAAD/4APwB/AfB8A/APg+AB8AAD/8AD8AfAHwfAPwD4PgAfAAD//AA/APwB8HwD8A+D8AP"
    "wAD//AAPwD8A/B8A/APg/AD8AH//gAD8A/APwfAPwD4P4B+A///x/w/APwH8HwD8A+D/B/gP"
    "//4/+PwB/P/B8A/APg///wB//4f/z8Af//wfAPwD4Pv/4AP/4f/+/AD/98HwD8A+D5/8AB/4"
    "P///wAP+fB8A/APg+P+AAPwP//8AAAcAAAAAAAAPg4AAAAAAAAAAAAAAAAAAAAD4AAAAAAAA"
    "AAAAAAAAAAAAAA+AAAAAAAAAAAAAAAAAAAAAAPgAAAAAAAAAAAAAAAAAAAAAD4AAAAAAAAAA"
    "AAAAAAAAAAAA+AAAAAAAAAAAAAAAAAAAAAAPgAAAAAAAAAAAAAAAAAAAAAD4AAAAAAAAAA=="
)
_LOGO_BITMAP: tuple[int, ...] = tuple(
    (_LOGO_BMP_DATA[i >> 3] >> (7 - (i & 7))) & 1
    for i in range(_LOGO_BMP_W * _LOGO_BMP_H)
)


def _sample_logo(lx: float, ly: float) -> bool:
    """Return True if normalised logo coord (lx, ly) hits a filled pixel."""
    px = int(lx * _LOGO_BMP_W)
    py = int(ly * _LOGO_BMP_H)
    if px < 0 or px >= _LOGO_BMP_W or py < 0 or py >= _LOGO_BMP_H:
        return False
    return _LOGO_BITMAP[py * _LOGO_BMP_W + px] == 1


def _nyc_pixel(
    x: int, y: int, t: float, cols: int, rows: int
) -> tuple[str, int, int, int]:
    """Compute a single NYC skyline pixel. Returns (char, r, g, b)."""
    nx = x / cols
    ny = y / rows

    # --- Ramp logo overlay (port of sampleLogo / logo positioning from mode-14.ts) ---
    _m = max(cols, rows)
    _sx = (2.0 * (x - cols / 2.0)) / _m
    _sy = (2.0 * (y - rows / 2.0)) / _m / 0.5  # aspect = 0.5
    _logo_region = 1.4
    _logo_h = _logo_region / (70.06 / 20.0)  # logoAR = 70.06/20
    _lx = (_sx + _logo_region / 2.0) / _logo_region
    _ly = (_sy + _logo_h / 2.0) / _logo_h
    if 0.0 <= _lx <= 1.0 and 0.0 <= _ly <= 1.0 and _sample_logo(_lx, _ly):
        wave = math.sin(x * 0.08 + t * 4.0) * math.cos(y * 0.12 + t * 3.0)
        n = (wave + 1.0) / 2.0
        return (
            "\u2588",  # █
            max(0, min(255, int(n * 40 + 188))),  # R 188–228
            max(0, min(255, int(n * 30 + 210))),  # G 210–240
            max(0, min(255, int(n * 20 + 15))),  # B  15–35
        )

    # Reflection: mirror y below waterline with sine x distortion
    is_reflection = ny > _NYC_WATERLINE
    s_nx = nx
    s_ny = ny
    if is_reflection:
        s_ny = _NYC_WATERLINE - (ny - _NYC_WATERLINE)
        s_nx = nx + math.sin(ny * 25 + t * 3) * 0.02

    # Iterate last-to-first so foreground (layer 2) occludes background (layer 0)
    for bi in range(len(_NYC_BUILDINGS) - 1, -1, -1):
        bd = _NYC_BUILDINGS[bi]
        b_left = bd["x"] - bd["w"] / 2
        b_right = bd["x"] + bd["w"] / 2
        b_top = _NYC_WATERLINE - bd["h"]

        if s_nx < b_left or s_nx > b_right or s_ny < b_top or s_ny > _NYC_WATERLINE:
            continue

        bny = (s_ny - b_top) / bd["h"]
        bnx = (s_nx - b_left) / bd["w"]
        cd = abs(bnx - 0.5)

        # Landmark shaped tops
        in_building = True
        btype = bd.get("type")
        if btype == "empire":
            if bny < 0.05:
                in_building = cd < 0.04
            elif bny < 0.12:
                in_building = cd < 0.15
            elif bny < 0.22:
                in_building = cd < 0.25
            elif bny < 0.35:
                in_building = cd < 0.35
        elif btype == "chrysler":
            if bny < 0.03:
                in_building = cd < 0.05
            elif bny < 0.18:
                in_building = cd < 0.05 + ((bny - 0.03) / 0.15) * 0.45
        elif btype == "freedom":
            if bny < 0.08:
                in_building = cd < 0.03
            elif bny < 0.4:
                in_building = cd < 0.1 + ((bny - 0.08) / 0.32) * 0.4
        elif btype == "stepped":
            if bny < 0.15:
                in_building = cd < 0.3
            elif bny < 0.3:
                in_building = cd < 0.4
        elif btype == "artdeco":
            if bny < 0.1:
                in_building = cd < 0.25
            elif bny < 0.2:
                in_building = cd < 0.35
        elif btype == "432park":
            pass  # uniform width, no shaped top

        if not in_building:
            continue

        # Window grid: every other floor, skip every 3rd col
        floor_idx = int(bny * 20)
        col_idx = int(bnx * 8)
        layer = bd["layer"]
        is_window_spot = floor_idx % 2 == 1 and col_idx % 3 != 0
        window_lit = (
            is_window_spot
            and layer >= 1
            and _scene_hash(bi * 1000 + floor_idx * 37 + col_idx * 13 + int(t * 3))
            > 0.4
        )

        # Layer brightness: 0=dim, 1=medium, 2=bright. Lit windows +60. Reflection x0.3
        if layer == 0:
            base_bri = 25 + _scene_hash(bi + 500) * 20
        elif layer == 1:
            base_bri = 55 + _scene_hash(bi + 500) * 35
        else:
            base_bri = 100 + _scene_hash(bi + 500) * 70
        if window_lit:
            base_bri = min(255, base_bri + 60)
        if is_reflection:
            base_bri *= 0.3
        bri = max(0, min(255, int(base_bri)))

        if is_reflection:
            char = "\u2591"  # ░
        elif layer == 0:
            char = "\u2591"  # ░
        elif layer == 1:
            char = "\u2592"  # ▒
        elif window_lit:
            char = "\u2593"  # ▓
        else:
            char = "\u2588"  # █

        return (char, bri, bri, bri)

    # Waterfront ground strip
    if _NYC_WATERLINE - 0.06 <= ny <= _NYC_WATERLINE:
        return ("\u2593", 28, 28, 28)  # ▓ dark strip

    # Open water ripples
    if is_reflection:
        ripple = math.sin(nx * 30 + t * 2) * math.sin(ny * 20 + t * 1.5)
        bri = int(((ripple + 1) / 2) * 10 + 6)
        return ("\u2591", bri, bri, bri)  # ░

    # Stars
    if _scene_hash(x * 7.3 + y * 13.1 + 0.5) > 0.98:
        bri = int(50 + math.sin(t * _scene_hash(x * 3.1 + y * 5.7) * 4) * 30 + 30)
        bri = max(10, min(255, bri))
        return ("\u263c", bri, bri, bri)  # ☼ matches reference implementation

    # Sky — black
    return (" ", 0, 0, 0)


# === Public: Animated WAITING ===


def start_waiting_animation(
    command: str,
    file: TextIO | None = None,
    mode: str = "binary",
    title: str | None = None,
) -> Callable[[], None]:
    """Start animated WAITING box. Returns a stop callable.

    The animation runs in a background thread, continuously updating
    the WAITING frame in place. Call the returned function to stop it.
    The last frame remains visible on screen.

    Args:
        command: Label shown in the frame title (prefixed with [WAITING]).
        file: Output stream (default stderr).
        mode: ``"binary"`` for the binary matrix, ``"nyc"`` for the NYC skyline.
        title: Override the full frame title (skips the [WAITING] prefix).
    """
    file = file or sys.stderr
    use_color = _color_supported(file)
    stop_event = threading.Event()
    started = threading.Event()

    # Get raw file descriptor for atomic writes (bypasses Python buffering)
    try:
        fd = file.fileno()
    except (AttributeError, io.UnsupportedOperation):
        fd = None  # StringIO in tests — fall back to file.write()

    def _write(data: str) -> None:
        if fd is not None:
            encoded = data.encode()
            offset = 0
            while offset < len(encoded):
                offset += os.write(fd, encoded[offset:])
        else:
            file.write(data)
            file.flush()

    def _run() -> None:
        width = max(_WIDTH_MIN, min(_term_width(), _WIDTH_MAX))
        _title = title if title is not None else f"[WAITING] {command}"
        top = _frame_top(_title, width, use_color)
        bottom = _frame_bottom(width)

        if mode == "nyc":
            rows = 20
            inner = width - 4
            total_lines = rows + 2

            _write(_hide_cursor() + "\n" * total_lines + _move_up(total_lines))
            n = 0
            started.set()

            while not stop_event.is_set():
                t = time.monotonic() * 0.5
                buf: list[str] = []

                if n > 0:
                    buf.append(_move_up(total_lines))

                buf.append("\r" + _clear_line() + top + "\n")

                for y in range(rows):
                    row_chars: list[str] = []
                    for x in range(inner):
                        char, r, g, b = _nyc_pixel(x, y, t, inner, rows)
                        if use_color:
                            row_chars.append(f"{_fg(r, g, b)}{char}")
                        else:
                            row_chars.append(char)
                    content = "".join(row_chars)
                    if use_color:
                        content += _reset()
                    buf.append(f"\r{_clear_line()}{BOX_V} {content} {BOX_V}\n")

                buf.append(f"\r{_clear_line()}{bottom}\n")

                _write("".join(buf))
                n += 1
                stop_event.wait(1 / 15)  # ~15fps for skyline
        else:
            rows = 14
            inner = width - 4
            total_lines = rows + 2

            _write(_hide_cursor() + "\n" * total_lines + _move_up(total_lines))
            n = 0
            started.set()

            while not stop_event.is_set():
                t = time.monotonic() * 0.1
                s = 8 * t
                buf: list[str] = []

                if n > 0:
                    buf.append(_move_up(total_lines))

                buf.append(_clear_line() + top + "\n")

                for y in range(rows):
                    chars: list[str] = []
                    for x in range(inner):
                        c = "10"[int(0.5 * x + 0.3 * y + 2 * s) % 2]
                        chars.append(c)
                    text = "".join(chars)
                    if use_color:
                        d = math.sin(0.15 * (inner // 2) + s) * math.cos(
                            0.1 * y + 0.7 * s
                        )
                        iv = math.sin((inner // 2 + y) * 0.08 + 1.3 * s)
                        brt = int(((math.sin(2 * (d + iv)) + 1) / 2) * 160 + 70)
                        brt = max(70, min(230, brt))
                        colored = f"{_fg(brt, brt, brt)}{text}{_reset()}"
                        buf.append(f"{_clear_line()}{BOX_V} {colored} {BOX_V}\n")
                    else:
                        buf.append(f"{_clear_line()}{_frame_row(text, width)}\n")

                buf.append(f"{_clear_line()}{bottom}\n")

                _write("".join(buf))
                n += 1
                stop_event.wait(1 / 20)  # ~20fps

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    started.wait()

    def stop() -> None:
        stop_event.set()
        thread.join(timeout=2)
        _write(_show_cursor())

    return stop


# === Public: SUCCESS Frame ===


def show_success(
    command: str, file: TextIO | None = None, duration: float = 5.0
) -> None:
    """Animate the NYC skyline with Ramp logo for `duration` seconds, then leave final frame.

    Uses an inline render loop (no background thread) to avoid threading/cursor issues.
    """
    file = file or sys.stderr
    use_color = _color_supported(file)
    width = max(_WIDTH_MIN, min(_term_width(), _WIDTH_MAX))
    # Cap rows so total_lines never exceeds available terminal height (prevents cascading)
    term_lines = shutil.get_terminal_size((80, 24)).lines
    rows = min(28, max(12, term_lines - 8))
    inner = width - 4
    total_lines = rows + 2

    top = _frame_top(f"[SUCCESS] {command}", width, use_color)
    bottom = _frame_bottom(width)

    try:
        fd = file.fileno()
    except (AttributeError, io.UnsupportedOperation):
        fd = None

    def _write(data: str) -> None:
        if fd is not None:
            encoded = data.encode()
            offset = 0
            while offset < len(encoded):
                offset += os.write(fd, encoded[offset:])
        else:
            file.write(data)
            file.flush()

    _write(_hide_cursor() + "\n" * total_lines + _move_up(total_lines))
    end_time = time.monotonic() + duration
    n = 0
    try:
        while time.monotonic() < end_time:
            t = time.monotonic() * 0.5
            buf: list[str] = []
            if n > 0:
                buf.append(_move_up(total_lines))
            buf.append(_clear_line() + top + "\n")
            for y in range(rows):
                row_chars: list[str] = []
                for x in range(inner):
                    char, r, g, b = _nyc_pixel(x, y, t, inner, rows)
                    if use_color:
                        row_chars.append(f"{_fg(r, g, b)}{char}")
                    else:
                        row_chars.append(char)
                content = "".join(row_chars)
                if use_color:
                    content += _reset()
                buf.append(f"{_clear_line()}{BOX_V} {content} {BOX_V}\n")
            buf.append(f"{_clear_line()}{bottom}\n")
            _write("".join(buf))
            n += 1
            time.sleep(1 / 15)
    finally:
        _write(_show_cursor())


# === Public: ACCESS DENIED ===


def access_denied(command: str, env: str) -> None:
    """Print framed ACCESS DENIED block to stderr."""
    file = sys.stderr
    use_color = _color_supported(file)
    width = max(_WIDTH_MIN, min(_term_width(), _WIDTH_MAX))
    inner = width - 4

    # Card 1: ACCESS DENIED with visual pattern
    top1 = _frame_top("ACCESS DENIED", width, use_color)
    bottom1 = _frame_bottom(width)

    buf: list[str] = ["\n", top1, "\n"]

    denied_rows = 6
    for y in range(denied_rows):
        if use_color:
            chars: list[str] = []
            for x in range(inner):
                idx = (x + y * 3) % len(BLOCKS_DENIED)
                char = BLOCKS_DENIED[idx]
                r = 180 + int(40 * math.sin(0.1 * x + 0.2 * y))
                r = max(140, min(220, r))
                chars.append(f"{_fg(r, 30, 25)}{char}")
            content = "".join(chars) + _reset()
            buf.append(f"{BOX_V} {content} {BOX_V}\n")
        else:
            pattern = BLOCKS_DENIED * (inner // len(BLOCKS_DENIED) + 1)
            offset = y * 3
            shifted = pattern[offset : offset + inner]
            buf.append(_frame_row(shifted, width) + "\n")

    buf.append(bottom1 + "\n\n")

    # Card 2: Command name + error message
    top2 = _frame_top(command, width, use_color)
    bottom2 = _frame_bottom(width)

    msg = (
        f"You need to be authenticated to use this command. "
        f"Try ramp auth login --env {env}."
    )

    buf.append(top2 + "\n")

    # Word-wrap message to fit inside frame
    words = msg.split()
    lines: list[str] = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        if len(test) <= inner:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)

    for line in lines:
        buf.append(_frame_row(line, width) + "\n")

    buf.append(bottom2 + "\n")

    file.write("".join(buf))
    file.flush()


# === Public: Strip Wave Banner ===


def _build_strip_wave_str(
    rows: int = 4, width: int = 80, use_color: bool = True
) -> str:
    """Build the strip-wave banner as a string (used by show_strip_wave and BoxHelpFormatter)."""
    t = 1.5  # fixed time for static frame
    lines = []
    for y in range(rows):
        row_chars = []
        for x in range(width):
            # Logo overlay — bottom-left, last _LOGO_H rows
            # Covers the entire logo bounding box so wave chars never
            # bleed into the gaps between letterforms.
            logo_start_y = rows - _LOGO_H
            if y >= logo_start_y and x < _LOGO_W:
                li = y - logo_start_y
                row_str = _LOGO_LINES[li]
                ch = row_str[x] if x < len(row_str) else " "
                if ch and ch != " ":
                    row_chars.append(f"{_fg(255, 255, 255)}{ch}" if use_color else ch)
                else:
                    row_chars.append(" ")
                continue
            # Mode-17 strip wave
            o = math.sin(y * math.sin(t) * 0.2 + x * 0.04 + t) * 20
            i = int(round(abs(x + y + o))) % len(DENSITY_WAVE)
            wave = math.sin(x * 0.08 + t * 4) * math.cos(y * 0.12 + t * 3) + math.sin(
                (x - y) * 0.06 + t * 5
            )
            bri = int(((math.sin(wave) + 1) / 2) * 205 + 50)
            char = DENSITY_WAVE[i]
            if use_color:
                r = int(bri * 0.894)
                g = int(bri * 0.949)
                b = int(bri * 0.129)
                row_chars.append(f"{_fg(r, g, b)}{char}")
            else:
                row_chars.append(char)
        line = "".join(row_chars)
        lines.append((line + _reset()) if use_color else line)
    return "\n".join(lines) + "\n"


def show_strip_wave(file: TextIO | None = None, rows: int = 4) -> None:
    """Print a static strip-wave banner in brand yellow with Ramp logo bottom-left."""
    file = file or sys.stdout
    use_color = _color_supported(file)
    width = max(_WIDTH_MIN, min(_term_width(), _WIDTH_MAX))
    file.write(_build_strip_wave_str(rows=rows, width=width, use_color=use_color))
    file.flush()


# === Public: Table Card ===


_SELECT_BG = (55, 55, 70)  # selected row — brighter than _WIN_BG
_SELECT_MARKER_COLOR = (228, 242, 33)  # Ramp yellow for ▶ marker


def show_table_card(
    title: str,
    headers: list[str],
    rows: list[dict[str, str]],
    file: TextIO | None = None,
    selected_row: int | None = None,
) -> None:
    """Print a framed table card with aligned columns.

    If selected_row is set, that row is highlighted with a distinct
    background and a ▶ marker.
    """
    file = file or sys.stdout
    use_color = _color_supported(file)
    available = _term_width() - _MARGIN - _SHADOW_W

    # Compute column widths from data first so we know the natural table size.
    col_widths = []
    for h in headers:
        max_data = max((len(str(r.get(h, ""))) for r in rows), default=0)
        col_widths.append(max(len(h), max_data))

    # Use a wider cap for tables so UUIDs (36 chars) and other wide data aren't
    # truncated unnecessarily. The frame still respects the terminal width.
    sep_count = len(headers) - 1
    natural = sum(col_widths) + sep_count * 2 + 4  # +4 for frame & padding
    width = min(max(_WIDTH_MIN, available), max(natural, _WIDTH_MIN), _TABLE_WIDTH_MAX)
    inner = width - 4  # content width inside frame (2 for box chars, 2 for spaces)

    # Clamp total width so all columns fit; distribute cuts proportionally.
    # Floor each column at 10 chars so truncated values remain readable.
    _COL_MIN = 10
    total = sum(col_widths) + sep_count * 2  # 2 spaces between cols
    if total > inner:
        excess = total - inner
        # Trim widest columns first
        for _ in range(excess):
            max_idx = col_widths.index(max(col_widths))
            if col_widths[max_idx] > _COL_MIN:
                col_widths[max_idx] -= 1

    def _truncate(s: str, w: int) -> str:
        s = str(s)
        if len(s) <= w:
            return s
        return s[: w - 1] + "\u2026"

    top = _frame_top(title, width, use_color)
    bottom = _frame_bottom(width)

    # Header row
    header_cells = [
        f"{h[: col_widths[i]]:<{col_widths[i]}}" for i, h in enumerate(headers)
    ]
    header_line = "  ".join(header_cells)

    buf_lines: list[str] = [top]
    if use_color:
        # Header with background color — no separator line needed
        hdr_content = f"{_bg(*_HEADER_BG)}{_bold()}{_fg(255, 255, 255)} {header_line:<{inner}} {_reset()}"
        buf_lines.append(f"{BOX_V}{hdr_content}{BOX_V}")
    else:
        buf_lines.append(f"{BOX_V} {header_line:<{inner}} {BOX_V}")
        # Plain-mode separator
        sep_cells = [BOX_H * col_widths[i] for i in range(len(headers))]
        sep_line = ("" + BOX_H + BOX_H).join(sep_cells)
        buf_lines.append(f"{BOX_V} {sep_line:<{inner}} {BOX_V}")

    for row_idx, row in enumerate(rows):
        cells = [
            f"{_truncate(row.get(h, ''), col_widths[i]):<{col_widths[i]}}"
            for i, h in enumerate(headers)
        ]
        row_line = "  ".join(cells)
        is_selected = selected_row is not None and row_idx == selected_row

        if use_color:
            if is_selected:
                marker = (
                    f"{_fg(*_SELECT_MARKER_COLOR)}\u25b6{_reset()}{_bg(*_SELECT_BG)} "
                )
                buf_lines.append(
                    f"{BOX_V}{_bg(*_SELECT_BG)}{marker}{row_line:<{inner - 2}} {_reset()}{BOX_V}"
                )
            else:
                buf_lines.append(
                    f"{BOX_V}{_bg(*_WIN_BG)} {row_line:<{inner}} {_reset()}{BOX_V}"
                )
        else:
            if is_selected:
                buf_lines.append(f"{BOX_V}> {row_line:<{inner - 2}} {BOX_V}")
            else:
                buf_lines.append(f"{BOX_V} {row_line:<{inner}} {BOX_V}")

    buf_lines.append(bottom)

    if use_color:
        wrapped = _window_wrap(buf_lines, width)
        file.write("\n".join(wrapped) + "\n")
    else:
        file.write("\n".join(buf_lines) + "\n")
    file.flush()


# === Public: Detail Card ===

_STATUS_GREEN = {"ACTIVE", "OPEN", "APPROVED", "ENABLED", "SYNCED"}
_STATUS_GRAY = {"CLOSED", "PAID", "DISABLED", "INACTIVE", "TERMINATED", "REJECTED"}


def show_detail_card(
    title: str,
    fields: dict[str, Any],
    file: TextIO | None = None,
) -> None:
    """Print a framed detail card with gradient values and status coloring."""
    file = file or sys.stdout
    use_color = _color_supported(file)
    available = _term_width() - _MARGIN - _SHADOW_W
    width = min(max(_WIDTH_MIN, available), _WIDTH_MAX)
    inner = width - 4

    if not use_color:
        # Plain key:value output
        click.echo(_frame_top(title, width, use_color=False), file=file)
        for k, v in fields.items():
            label = f"{k + ':':<25s}"
            if isinstance(v, dict):
                click.echo(_frame_row(f"{label} {{...}}", width), file=file)
            elif isinstance(v, list):
                if not v:
                    click.echo(_frame_row(f"{label} (none)", width), file=file)
                else:
                    click.echo(
                        _frame_row(f"{label} [{len(v)} items]", width), file=file
                    )
            else:
                click.echo(_frame_row(f"{label} {v}", width), file=file)
        click.echo(_frame_bottom(width), file=file)
        return

    # Compute label width for alignment
    label_w = max((len(k) for k in fields), default=10) + 1  # +1 for colon

    top = _frame_top(title, width, use_color)
    bottom = _frame_bottom(width)
    buf_lines: list[str] = [top]

    def _detail_row(content_ansi: str, vis_len: int) -> str:
        """Build a detail row: BOX_V + WIN_BG + content + pad + reset + BOX_V."""
        pad = " " * max(0, inner - vis_len)
        return f"{BOX_V}{_bg(*_WIN_BG)} {content_ansi}{pad} {_reset()}{BOX_V}"

    def _render_fields(flds: dict[str, Any], indent: int = 0) -> None:
        prefix = " " * indent
        for k, v in flds.items():
            key_str = f"{k}:"
            label = f"{prefix}{_bold()}{key_str:<{label_w - indent}}{_reset_fg()}"
            label_vis = label_w

            if isinstance(v, dict):
                # Sub-card heading: just the key, padded to full width
                buf_lines.append(_detail_row(label, label_vis))
                if indent < 4:  # depth limit 2
                    _render_fields(v, indent + 2)
                continue

            if isinstance(v, list):
                if not v:
                    val_part = f"{_fg(100, 100, 100)}(none){_reset_fg()}"
                    vis_len = label_vis + 1 + 6
                else:
                    val_part = f"[{len(v)} items]"
                    vis_len = label_vis + 1 + len(val_part)
                buf_lines.append(_detail_row(f"{label} {val_part}", vis_len))
                continue

            val_str = str(v) if v is not None else ""

            # Truncate values that would overflow the frame
            max_val_w = inner - label_vis - 1  # 1 for space between label and value
            if len(val_str) > max_val_w and max_val_w > 3:
                val_str = val_str[: max_val_w - 3] + "..."

            # Status coloring
            is_status_field = k.lower() in (
                "status",
                "approval_status",
                "state",
                "sync_status",
            )
            if is_status_field and val_str.upper() in _STATUS_GREEN:
                val_display = f"{_fg(0, 200, 0)}{val_str}{_reset_fg()}"
            elif is_status_field and val_str.upper() in _STATUS_GRAY:
                val_display = f"{_fg(100, 100, 100)}{val_str}{_reset_fg()}"
            else:
                val_display = _gradient_text(val_str)

            vis_len = label_vis + 1 + len(val_str)
            buf_lines.append(_detail_row(f"{label} {val_display}", vis_len))

    _render_fields(fields)
    buf_lines.append(bottom)

    wrapped = _window_wrap(buf_lines, width)
    file.write("\n".join(wrapped) + "\n")
    file.flush()
