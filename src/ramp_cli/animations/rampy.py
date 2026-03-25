"""Rampy mascot skateboarder animation (mode-20 port)."""

from __future__ import annotations

import base64
import io
import math
import os
import shutil
import sys
import time

from ramp_cli.output.style import (
    _WIDTH_MAX,
    _WIDTH_MIN,
    _color_supported,
    _fg,
    _hide_cursor,
    _move_up,
    _reset,
    _show_cursor,
)

# ---------------------------------------------------------------------------
# A. Ramp symbol bitmap — 120×80, rasterized from the SVG checkmark/arrow path.
#    Packed MSB-first, 1 bit per pixel (same scheme as _LOGO_BMP_DATA in style.py).
# ---------------------------------------------------------------------------
_SYMBOL_BMP_W = 120
_SYMBOL_BMP_H = 80
_SYMBOL_BMP_DATA: bytes = base64.b64decode(
    "AAAAAAAAAAAAAAMAAAAAAAAAAAAAAAAAAAOAAAAAAAAAAAAAAAAAAAPgAAAAAAAAAAAAAAAA"
    "AAPwAAAAAAAAAAAAAAAAAAf4AAAAAAAAAAAAAAAAAAf+AAAAAAAAAAAAAAAAAAf/AAAAAAAA"
    "AAAAAAAAAAf/gAAAAAAAAAAAAAAAAAf/wAAAAAAAAAAAAAAAAA//8AAAAAAAAAAAAAAAAA//"
    "+AAAAAAAAAAAAAAAAA///AAAAAAAAAAAAAAAAB///wAAAAAAAAAAAAAAAB///wAAAAAAAAAA"
    "AAAAAB///wAAAAAAAAAAAAAAAD///wAAAAAAAAAAAAAAAD///wAAAAAAAAAAAAAAAH///gAA"
    "AAAAAAAAAAAAAH///gAAAAAAAAAAAAAAAP///gAAAAAAAAAAAAAAAP///gAAAAAAAAAAAAAA"
    "Af///gAAAAAAAAAAAAAAAf///AAAAAAAAAAAAAAAA////AAAAAAAAAAAAAAAA////AAAAAAA"
    "AAAAAAAAB///+AAAAAAAAAAAAAAAD///+AAAAAAAAAAAAAAAD///+AAAAAAAAAAAAAAAH///"
    "8AAAAAAAAAAAAAAAP///8AAAAAAAAAAAAAAAf///4AAAAAAAAAAAAAAAf///4AAAAAAAAAAA"
    "AAAA////4AAAAAAAAAAAAAAB////wAAAAAAAAAAAAAAD////gAAAAAAAAAAAAAAH////gAAA"
    "AAAAAAAAAAAP////AAAAAAAAAAAAAAAf////AAAAAAAAAAAAAAA////+AAAAAAAAAAAAAAB/"
    "///+AAAAAAAAAAAAAAD////8AAAAAAAAAAAAAAH////4AAAAAAAAAAAAAAP////wAAAAAAAA"
    "AAAAAAf////wAAAAAAAAAAAAAA/////gAAAAAAAAAAAAAD/////AAAAAAAAAAAAAAH////+A"
    "AAAAAAAAAAAAAP////8AAAAAAAAAAAAAA/////8AAAAAAAAAAAAAB/////4AAAAAAAAAAAAA"
    "H/////wAAAAAAAAAAAAAP/////gAAAAAAAAAAAAA//////AAAAAAAAAAAAAD/////+AAAAAA"
    "AAAAAAAP/////8AAAAAAAAAAAAA//////wAAAAAAAAAAAAD//////gAAAAAAAAAAAAP/////"
    "/AAAAAAAAAAAAA//////+AAAAAAAAAAAAH//////4AAAAAAAAAAAAf//////wAAAAAAAAAAA"
    "D///////gAAAAAAAAAAA///////+AAAAAAAAAAAH///////8AAAAAAAAAAD////////wAAAA"
    "AAAAAB/////////gAAAAAAAAP/////////+AAAAAAAAA//////////4AH////wAAP///////"
    "//gAf////8AAH////////+AA/////+AAD////////4AD//////AAB////////gAH//////wA"
    "Af//////8AAf//////4AAP//////wAA///////8AAH/////+AAD///////+AAB/////wAAP/"
    "///////gAA////8AAA/////////wAAf///AAAD/////////4AAP//gAAAP/////////+AAD/"
    "AAAAA//////////+"
)
_SYMBOL_BITMAP: tuple[int, ...] = tuple(
    (_SYMBOL_BMP_DATA[i >> 3] >> (7 - (i & 7))) & 1
    for i in range(_SYMBOL_BMP_W * _SYMBOL_BMP_H)
)

SQUISH = 0.6


def _sample_symbol(nx: float, ny: float) -> bool:
    """Return True if normalised coord (nx, ny) hits a filled pixel in the symbol bitmap."""
    cx = (nx - 0.5) * SQUISH + 0.5
    px = int(cx * _SYMBOL_BMP_W)
    py = int(ny * _SYMBOL_BMP_H)
    if px < 0 or px >= _SYMBOL_BMP_W or py < 0 or py >= _SYMBOL_BMP_H:
        return False
    return _SYMBOL_BITMAP[py * _SYMBOL_BMP_W + px] == 1


# ---------------------------------------------------------------------------
# B. Constants
# ---------------------------------------------------------------------------

# Sprite geometry
SPRITE_W = 42
SPRITE_H = 21
BODY_X0 = 7
BODY_W = 28
BODY_H = 12
BODY_Y0 = 3

# Eye config (body-relative coordinates in 28×12 region)
EYE_L = {"cx": 9, "cy": 5, "rx": 4.5, "ry": 2.5, "px": 8.0, "py": 5.3, "pr": 1.5}
EYE_R = {"cx": 19, "cy": 5, "rx": 4.5, "ry": 2.5, "px": 18.0, "py": 5.3, "pr": 1.5}

# Blink timing
BLINK_T = 3.5
BLINK_D = 0.12

# Body color — olive-gray
BR, BG_C, BB = 138, 138, 106

# Leg positions (sprite-relative)
LEG_Y0, LEG_Y1 = 15, 16
LEG_L_X0, LEG_L_X1 = 16, 17
LEG_R_X0, LEG_R_X1 = 26, 27

# Foot positions (sprite-relative, 5 chars wide)
FOOT_Y = 17
FOOT_L_X0, FOOT_L_X1 = 14, 18
FOOT_R_X0, FOOT_R_X1 = 25, 29

# Board layout
BOARD_DECK_Y = 18
BOARD_UNDER_Y = 19
WHEEL_Y = 20

# Colors (ANSI palette values)
COL_LEG = (0x3A, 0x3A, 0x3A)
COL_FOOT = (0x1C, 0x1C, 0x1C)
COL_DECK = (0x4E, 0x4E, 0x4E)
COL_UNDER = (0x1C, 0x1C, 0x1C)
COL_WHEEL = (0xD7, 0x5F, 0x00)

# "rampy" block-letter logo
LOGO_LINES = [
    "\u2599\u2580\u2596\u259d\u2580\u2596\u259b\u259a\u2580\u2596\u259b\u2580\u2596\u258c \u258c",
    "\u258c  \u259e\u2580\u258c\u258c\u2590 \u258c\u2599\u2584\u2598\u259a\u2584\u258c",
    "\u2598  \u259d\u2580\u2598\u2598\u259d \u2598\u258c  \u2597\u2584\u2598",
]
LOGO_W = 18
LOGO_H = 3

# Half-block mirror map for 180° flip
MIRROR = {
    "\u2590": "\u258c",
    "\u258c": "\u2590",
    "\u259f": "\u2599",
    "\u2599": "\u259f",
    "\u259c": "\u259b",
    "\u259b": "\u259c",
}

# Background density chars
DENSITY_3 = " \u2592\u2593"

# Motion params
TRAVEL = 0.5
PAUSE = 0.1
TOTAL = TRAVEL + PAUSE
ARC = 32


# ---------------------------------------------------------------------------
# C. Sprite renderer
# ---------------------------------------------------------------------------


def _blinking(t: float) -> bool:
    c = t % BLINK_T
    return c < BLINK_D or (0.3 < c < 0.3 + BLINK_D)


def _render_eye(bx: float, by: float, eye: dict, blink: bool):
    """Render eye at body-relative coords. Returns (char, r, g, b) or None."""
    if blink:
        if round(by) == round(eye["cy"]) and abs(bx - eye["cx"]) <= eye["rx"] * 0.8:
            return ("\u2501", 0x3A, 0x3A, 0x3A)
        return None
    edx = (bx - eye["cx"]) / eye["rx"]
    edy = (by - eye["cy"]) / eye["ry"]
    if edx * edx + edy * edy > 1.0:
        return None
    pdx = (bx - eye["px"]) / (eye["pr"] * 1.8)
    pdy = (by - eye["py"]) / eye["pr"]
    if pdx * pdx + pdy * pdy <= 1.0:
        return ("\u2588", 0x1C, 0x1C, 0x1C)
    return ("\u2588", 0xFF, 0xFF, 0xFF)


def _render_sprite(src_x: int, sy: int, t: float):
    """Render sprite pixel. Returns (char, r, g, b) or None for transparent."""
    # Layer 5 — Eyes (top priority)
    bx = src_x - BODY_X0
    by = sy - BODY_Y0
    if 0 <= bx < BODY_W and 0 <= by < BODY_H:
        blink = _blinking(t)
        le = _render_eye(bx, by, EYE_L, blink)
        if le:
            return le
        re = _render_eye(bx, by, EYE_R, blink)
        if re:
            return re

        # Layer 4 — Body (Ramp symbol bitmap with olive-gray shading)
        nx = bx / BODY_W
        ny = by / BODY_H
        if _sample_symbol(nx, ny):
            shade = 0.82 + 0.18 * math.sin(nx * 3.5 + ny * 2.5)
            return (
                "\u2588",
                max(0, min(255, int(BR * shade))),
                max(0, min(255, int(BG_C * shade))),
                max(0, min(255, int(BB * shade))),
            )

    # Layer 3 — Legs
    if LEG_Y0 <= sy <= LEG_Y1:
        if (LEG_L_X0 <= src_x <= LEG_L_X1) or (LEG_R_X0 <= src_x <= LEG_R_X1):
            return ("\u2588", *COL_LEG)

    # Layer 2 — Feet
    if sy == FOOT_Y:
        if (FOOT_L_X0 <= src_x <= FOOT_L_X1) or (FOOT_R_X0 <= src_x <= FOOT_R_X1):
            return ("\u2588", *COL_FOOT)

    # Layer 1 — Board deck
    if sy == BOARD_DECK_Y:
        if src_x == 0:
            return ("\u259f", *COL_DECK)
        if src_x == SPRITE_W - 1:
            return ("\u2599", *COL_DECK)
        if 0 < src_x < SPRITE_W - 1:
            return ("\u2588", *COL_DECK)

    # Layer 0b — Board underside
    if sy == BOARD_UNDER_Y:
        if src_x == 0:
            return ("\u259c", *COL_UNDER)
        if src_x == SPRITE_W - 1:
            return ("\u259b", *COL_UNDER)
        if 0 < src_x < SPRITE_W - 1:
            return ("\u2588", *COL_UNDER)

    # Layer 0a — Wheels
    if sy == WHEEL_Y:
        if src_x in (2, 3, SPRITE_W - 4, SPRITE_W - 3):
            return ("\u25cf", *COL_WHEEL)

    return None


# ---------------------------------------------------------------------------
# D. Main loop
# ---------------------------------------------------------------------------


def show_rampy(file=None, duration: float = 10.0) -> None:
    """Animate Rampy skateboarder (mode-20 port)."""
    file = file or sys.stdout
    use_color = _color_supported(file)

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

    ts = shutil.get_terminal_size((80, 24))
    width = max(_WIDTH_MIN, min(ts.columns, _WIDTH_MAX))
    # Fixed ideal size, but never exceed terminal height
    rows = max(1, min(28, ts.lines - 2))
    arc = ARC

    total_lines = rows
    _write(_hide_cursor() + "\n" * total_lines + _move_up(total_lines))

    start = time.monotonic()
    end_time = start + duration
    n = 0

    try:
        while time.monotonic() < end_time:
            t = (time.monotonic() - start) * 1000.0 * 0.0001

            # Sprite motion — diagonal R-to-L with ollie arc
            cycle_t = t % TOTAL
            in_travel = cycle_t < TRAVEL

            if in_travel:
                progress = cycle_t / TRAVEL
                dist = width + SPRITE_W * 3
                center_x = width + SPRITE_W - progress * dist
                # Position so full sprite is visible near ollie peak
                base_y = rows - SPRITE_H + arc - 2
                ollie = math.sin(progress * math.pi) * arc
                sprite_y = int(base_y - ollie)

                # 180° flip — cosine foreshortening at midpoint
                if 0.25 < progress < 0.55:
                    fp = (progress - 0.25) / 0.3
                    cos_flip = math.cos(fp * math.pi)
                elif progress >= 0.55:
                    cos_flip = -1.0
                else:
                    cos_flip = 1.0
                abs_flip = abs(cos_flip)
            else:
                # Pause phase — sprite off screen
                sprite_y = -100
                cos_flip = 1.0
                abs_flip = 1.0
                center_x = -100.0

            buf: list[str] = []
            if n > 0:
                buf.append(_move_up(total_lines))

            for y in range(rows):
                row_chars: list[str] = []

                for x in range(width):
                    cell = None

                    # Logo overlay — bottom-left
                    logo_start_y = rows - LOGO_H
                    if y >= logo_start_y and x < LOGO_W:
                        ch = (
                            LOGO_LINES[y - logo_start_y][x]
                            if x < len(LOGO_LINES[y - logo_start_y])
                            else " "
                        )
                        if ch != " ":
                            if use_color:
                                row_chars.append(f"{_fg(255, 255, 255)}{ch}")
                            else:
                                row_chars.append(ch)
                            continue

                    # Sprite
                    if in_travel:
                        sy = y - sprite_y
                        if 0 <= sy < SPRITE_H and abs_flip > 0.03:
                            half_vis_w = (SPRITE_W / 2) * abs_flip
                            rel_x = x - center_x
                            if abs(rel_x) <= half_vis_w:
                                norm_x = rel_x / half_vis_w
                                src_x = int((norm_x * 0.5 + 0.5) * (SPRITE_W - 1))
                                if cos_flip < 0:
                                    src_x = SPRITE_W - 1 - src_x
                                if 0 <= src_x < SPRITE_W:
                                    cell = _render_sprite(src_x, sy, t)
                                    if cell:
                                        ch, cr, cg, cb = cell
                                        if cos_flip < 0:
                                            ch = MIRROR.get(ch, ch)
                                        if use_color:
                                            row_chars.append(f"{_fg(cr, cg, cb)}{ch}")
                                        else:
                                            row_chars.append(ch)
                                        continue

                    # Background — subtle grayscale density wave
                    mid = max(rows * 0.5, 0.5)
                    d = 1 if y < mid else -1
                    f = t * 100 * d
                    ci = round(abs(x + y + f)) % len(DENSITY_3)
                    dy = abs(y - mid) / max(mid, 1)
                    wave = math.sin(dy * 3.14 + t * 2) * math.cos(x * 0.06 + t * 1.5)
                    brightness = int(((math.sin(wave) + 1) / 2) * 20 + 18)
                    bg_ch = DENSITY_3[ci]
                    if use_color:
                        row_chars.append(
                            f"{_fg(brightness, brightness, brightness)}{bg_ch}"
                        )
                    else:
                        row_chars.append(bg_ch)

                line = "".join(row_chars)
                if use_color:
                    line += _reset()
                buf.append(line + "\n")

            _write("".join(buf))
            n += 1
            time.sleep(1 / 20)
    finally:
        _write(_show_cursor())
