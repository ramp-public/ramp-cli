"""Rampy mascot surfer animation (mode-21 port)."""

from __future__ import annotations

import io
import math
import os
import shutil
import sys
import time

from ramp_cli.animations.rampy import (
    BB,
    BG_C,
    BODY_H,
    BODY_W,
    BR,
    EYE_L,
    EYE_R,
    LOGO_H,
    LOGO_LINES,
    LOGO_W,
    _blinking,
    _render_eye,
    _sample_symbol,
)
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
# Surfboard colors
# ---------------------------------------------------------------------------
COL_DECK = (0xD7, 0x87, 0x5F)  # warm wood deck
COL_STRIPE = (0xD7, 0x00, 0x00)  # racing stripe
COL_UNDER = (0x87, 0x5F, 0x00)  # board underside/rail
COL_LEG = (0x3A, 0x3A, 0x3A)
COL_FOOT = (0x1C, 0x1C, 0x1C)

# Ocean palette
OCEAN_DEEP = (0, 40, 100)
OCEAN_MID = (20, 80, 160)
OCEAN_LIGHT = (60, 140, 220)
FOAM_WHITE = (220, 235, 255)

# Body geometry — same as skater
BODY_X0 = 7

# Balance wobble
WOBBLE_SPEED = 2.8
WOBBLE_AMP = 1.5


# ---------------------------------------------------------------------------
# Wave functions
# ---------------------------------------------------------------------------


def _wave_height(x: float, t: float, cols: int) -> float:
    """Multi-sine wave height at x position, scrolling with time."""
    nx = x / cols
    speed = t * 3.0
    w1 = math.sin(nx * 4.0 + speed * 0.8) * 0.35
    w2 = math.sin(nx * 7.0 + speed * 1.2 + 1.5) * 0.2
    w3 = math.sin(nx * 15.0 + speed * 2.5) * 0.08
    w4 = math.sin(nx * 2.5 + speed * 0.5 + 0.7) * 0.25
    return w1 + w2 + w3 + w4


def _foam_intensity(x: int, y: int, t: float, cols: int, rows: int) -> float:
    """Foam at wave crests — narrow strip near surface, stronger at crests."""
    wh = _wave_height(x, t, cols)
    wh_next = _wave_height(x + 1, t, cols)
    slope = abs(wh_next - wh) * cols
    ny = y / rows
    wave_surface = 0.45 + wh * 0.4
    dist = ny - wave_surface
    if -0.08 < dist < 0.06:
        surface_foam = max(0, 1.0 - abs(dist) * 20)
        crest_foam = min(1, (slope - 1.5) * 0.5) if slope > 1.5 else 0
        return min(1, surface_foam * 0.7 + crest_foam)
    return 0


# ---------------------------------------------------------------------------
# Render functions
# ---------------------------------------------------------------------------


def _render_body(bx: int, by: int, t: float):
    """Render body pixel at body-relative coords. Returns (char, r, g, b) or None."""
    blink = _blinking(t)
    le = _render_eye(bx, by, EYE_L, blink)
    if le:
        return le
    re = _render_eye(bx, by, EYE_R, blink)
    if re:
        return re

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
    return None


def _lerp(a: tuple, b: tuple, t: float) -> tuple[int, int, int]:
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


def _clamp(v: int) -> int:
    return max(0, min(255, v))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def show_rampy_surf(file=None, duration: float = 10.0) -> None:
    """Animate Rampy surfer (mode-21 port)."""
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
    rows = max(1, min(28, ts.lines - 2))

    total_lines = rows
    _write(_hide_cursor() + "\n" * total_lines + _move_up(total_lines))

    start = time.monotonic()
    end_time = start + duration
    n = 0

    # Surfboard geometry
    board_w = 36
    leg_h = 2

    try:
        while time.monotonic() < end_time:
            t = time.monotonic() - start

            # Character positioning — centered with wobble
            wobble = math.sin(t * WOBBLE_SPEED) * WOBBLE_AMP
            body_x0 = width // 2 - BODY_W // 2 + int(wobble)
            # Vertical — upper-middle area with gentle bob
            vert_bob = math.sin(t * 2.2) * 1.2
            body_y0 = max(0, int(rows * 0.18 + vert_bob))

            # Derived positions
            leg_top = body_y0 + BODY_H
            foot_y = leg_top + leg_h
            board_y = foot_y + 1
            board_x0 = body_x0 + BODY_W // 2 - board_w // 2

            buf: list[str] = []
            if n > 0:
                buf.append(_move_up(total_lines))

            for y in range(rows):
                row_chars: list[str] = []

                for x in range(width):
                    cell = None

                    # Layer 6 — Logo overlay bottom-left
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

                    # Layer 5 — Eyes + Body
                    bx = x - body_x0
                    by = y - body_y0
                    if 0 <= bx < BODY_W and 0 <= by < BODY_H:
                        cell = _render_body(bx, by, t)
                        if cell:
                            ch, cr, cg, cb = cell
                            if use_color:
                                row_chars.append(f"{_fg(cr, cg, cb)}{ch}")
                            else:
                                row_chars.append(ch)
                            continue

                    # Layer 4 — Legs
                    if y >= leg_top and y < leg_top + leg_h:
                        if (x == body_x0 + 9 or x == body_x0 + 10) or (
                            x == body_x0 + 19 or x == body_x0 + 20
                        ):
                            if use_color:
                                row_chars.append(f"{_fg(*COL_LEG)}\u2588")
                            else:
                                row_chars.append("\u2588")
                            continue

                    # Layer 3 — Feet (4 chars wide each)
                    if y == foot_y:
                        if (body_x0 + 8 <= x <= body_x0 + 11) or (
                            body_x0 + 18 <= x <= body_x0 + 21
                        ):
                            if use_color:
                                row_chars.append(f"{_fg(*COL_FOOT)}\u2588")
                            else:
                                row_chars.append("\u2588")
                            continue

                    # Layer 2 — Surfboard deck
                    if y == board_y:
                        if x == board_x0:
                            ch = "\u259f"  # ▟
                            if use_color:
                                row_chars.append(f"{_fg(*COL_DECK)}{ch}")
                            else:
                                row_chars.append(ch)
                            continue
                        elif x == board_x0 + board_w - 1:
                            ch = "\u2599"  # ▙
                            if use_color:
                                row_chars.append(f"{_fg(*COL_DECK)}{ch}")
                            else:
                                row_chars.append(ch)
                            continue
                        elif board_x0 < x < board_x0 + board_w - 1:
                            board_mid = board_x0 + board_w // 2
                            if board_mid - 1 <= x <= board_mid + 1:
                                if use_color:
                                    row_chars.append(f"{_fg(*COL_STRIPE)}\u2588")
                                else:
                                    row_chars.append("\u2588")
                            else:
                                if use_color:
                                    row_chars.append(f"{_fg(*COL_DECK)}\u2588")
                                else:
                                    row_chars.append("\u2588")
                            continue

                    # Layer 1 — Board underside
                    if y == board_y + 1:
                        if x == board_x0 + 1:
                            if use_color:
                                row_chars.append(f"{_fg(*COL_UNDER)}\u259c")  # ▜
                            else:
                                row_chars.append("\u259c")
                            continue
                        elif x == board_x0 + board_w - 2:
                            if use_color:
                                row_chars.append(f"{_fg(*COL_UNDER)}\u259b")  # ▛
                            else:
                                row_chars.append("\u259b")
                            continue
                        elif board_x0 + 1 < x < board_x0 + board_w - 2:
                            if use_color:
                                row_chars.append(f"{_fg(*COL_UNDER)}\u2580")  # ▀
                            else:
                                row_chars.append("\u2580")
                            continue

                    # Layer 0 — Spray particles
                    ny_pos = y / rows
                    wh = _wave_height(x, t, width)
                    wave_surface = 0.45 + wh * 0.4
                    above_surface = wave_surface - ny_pos

                    spray_check = math.sin(x * 13.7 + y * 7.3 + t * 20) * math.cos(
                        x * 5.1 + t * 15
                    )
                    if (
                        above_surface > 0
                        and above_surface < 0.15
                        and spray_check > 0.92
                    ):
                        alpha = (1.0 - above_surface / 0.15) * 0.8
                        sb = int(180 + alpha * 75)
                        if use_color:
                            row_chars.append(f"{_fg(sb, sb, min(255, sb + 10))}\u00b7")
                        else:
                            row_chars.append("\u00b7")
                        continue

                    # Ocean background
                    foam = _foam_intensity(x, y, t, width, rows)
                    dist_below = ny_pos - wave_surface

                    if ny_pos < wave_surface - 0.05:
                        # Sky
                        sky_b = 18 + ny_pos * 30
                        sr = _clamp(int(sky_b * 0.4))
                        sg = _clamp(int(sky_b * 0.5))
                        sb = _clamp(int(sky_b * 0.9))
                        if use_color:
                            row_chars.append(f"{_fg(sr, sg, sb)} ")
                        else:
                            row_chars.append(" ")
                        continue

                    if foam > 0.3:
                        # Foam
                        fc = math.sin(x * 3.7 + y * 2.1 + t * 8) * 0.5 + 0.5
                        fr = _clamp(
                            int(FOAM_WHITE[0] * foam + OCEAN_LIGHT[0] * (1 - foam))
                        )
                        fg_c = _clamp(
                            int(FOAM_WHITE[1] * foam + OCEAN_LIGHT[1] * (1 - foam))
                        )
                        fb = _clamp(
                            int(FOAM_WHITE[2] * foam + OCEAN_LIGHT[2] * (1 - foam))
                        )
                        foam_ch = (
                            "\u2591" if fc > 0.6 else "\u2592" if fc > 0.3 else "\u2593"
                        )
                        if use_color:
                            row_chars.append(f"{_fg(fr, fg_c, fb)}{foam_ch}")
                        else:
                            row_chars.append(foam_ch)
                        continue

                    # Underwater gradient
                    depth = min(1, max(0, dist_below * 3))
                    wave_pat = math.sin(x * 0.2 + y * 0.3 + t * 2.5) * math.cos(
                        y * 0.15 + t * 1.8
                    )
                    wave_bright = (math.sin(wave_pat * 2) + 1) / 2

                    lr = OCEAN_LIGHT[0] + (OCEAN_DEEP[0] - OCEAN_LIGHT[0]) * depth
                    lg = OCEAN_LIGHT[1] + (OCEAN_DEEP[1] - OCEAN_LIGHT[1]) * depth
                    lb = OCEAN_LIGHT[2] + (OCEAN_DEEP[2] - OCEAN_LIGHT[2]) * depth

                    mix = wave_bright * 0.3
                    wr = _clamp(int(lr * (1 - mix) + OCEAN_MID[0] * mix))
                    wg = _clamp(int(lg * (1 - mix) + OCEAN_MID[1] * mix))
                    wb = _clamp(int(lb * (1 - mix) + OCEAN_MID[2] * mix))

                    char_idx = int(depth * 2.5 + wave_bright * 1.5)
                    wave_chars = "~\u2248\u2591\u2592\u2593"
                    w_ch = wave_chars[min(char_idx, len(wave_chars) - 1)]

                    if use_color:
                        row_chars.append(f"{_fg(wr, wg, wb)}{w_ch}")
                    else:
                        row_chars.append(w_ch)

                line = "".join(row_chars)
                if use_color:
                    line += _reset()
                buf.append(line + "\n")

            _write("".join(buf))
            n += 1
            time.sleep(1 / 20)
    finally:
        _write(_show_cursor())
