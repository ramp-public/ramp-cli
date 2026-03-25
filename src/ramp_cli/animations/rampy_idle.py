"""Rampy mascot idle animation (mode-19 port) — standing with googly eyes and binary background."""

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

# Leg positions (body-relative)
LEG_H = 3
COL_LEG = (0x3A, 0x3A, 0x3A)
COL_FOOT = (0x1C, 0x1C, 0x1C)


def show_rampy_idle(file=None, duration: float = 10.0) -> None:
    """Animate Rampy standing idle (mode-19 port)."""
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

    try:
        while time.monotonic() < end_time:
            t = time.monotonic() - start

            # Character positioning — centered
            body_x0 = width // 2 - BODY_W // 2
            body_y0 = max(0, (rows - BODY_H - 4) // 2)

            # Derived positions
            leg_top = body_y0 + BODY_H
            foot_y = leg_top + LEG_H

            buf: list[str] = []
            if n > 0:
                buf.append(_move_up(total_lines))

            for y in range(rows):
                row_chars: list[str] = []

                for x in range(width):
                    # Layer 4 — Logo overlay bottom-left
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

                    # Layer 3 — Eyes
                    bx = x - body_x0
                    by = y - body_y0
                    if 0 <= bx < BODY_W and 0 <= by < BODY_H:
                        blink = _blinking(t)
                        le = _render_eye(bx, by, EYE_L, blink)
                        if le:
                            ch, cr, cg, cb = le
                            if use_color:
                                row_chars.append(f"{_fg(cr, cg, cb)}{ch}")
                            else:
                                row_chars.append(ch)
                            continue
                        re = _render_eye(bx, by, EYE_R, blink)
                        if re:
                            ch, cr, cg, cb = re
                            if use_color:
                                row_chars.append(f"{_fg(cr, cg, cb)}{ch}")
                            else:
                                row_chars.append(ch)
                            continue

                    # Layer 2 — Legs
                    if y >= leg_top and y < leg_top + LEG_H:
                        if (x == body_x0 + 9 or x == body_x0 + 10) or (
                            x == body_x0 + 19 or x == body_x0 + 20
                        ):
                            if use_color:
                                row_chars.append(f"{_fg(*COL_LEG)}\u2588")
                            else:
                                row_chars.append("\u2588")
                            continue

                    # Feet (5 chars wide each)
                    if y == foot_y:
                        if (body_x0 + 7 <= x <= body_x0 + 11) or (
                            body_x0 + 18 <= x <= body_x0 + 22
                        ):
                            if use_color:
                                row_chars.append(f"{_fg(*COL_FOOT)}\u2588")
                            else:
                                row_chars.append("\u2588")
                            continue

                    # Layer 1 — Body (Ramp symbol bitmap)
                    if 0 <= bx < BODY_W and 0 <= by < BODY_H:
                        nx = bx / BODY_W
                        ny = by / BODY_H
                        if _sample_symbol(nx, ny):
                            shade = 0.82 + 0.18 * math.sin(nx * 3.5 + ny * 2.5)
                            cr = max(0, min(255, int(BR * shade)))
                            cg = max(0, min(255, int(BG_C * shade)))
                            cb = max(0, min(255, int(BB * shade)))
                            if use_color:
                                row_chars.append(f"{_fg(cr, cg, cb)}\u2588")
                            else:
                                row_chars.append("\u2588")
                            continue

                    # Layer 0 — Binary digit background
                    speed = t * 4
                    w1 = math.sin(x * 0.15 + speed) * math.cos(y * 0.1 + speed * 0.7)
                    w2 = math.sin((x + y) * 0.08 + speed * 1.3)
                    brightness = int(((math.sin((w1 + w2) * 2) + 1) / 2) * 20 + 18)
                    digit = "10"[int(x * 0.5 + y * 0.3 + speed * 2) % 2]
                    if use_color:
                        row_chars.append(
                            f"{_fg(brightness, brightness, brightness)}{digit}"
                        )
                    else:
                        row_chars.append(digit)

                line = "".join(row_chars)
                if use_color:
                    line += _reset()
                buf.append(line + "\n")

            _write("".join(buf))
            n += 1
            time.sleep(1 / 20)
    finally:
        _write(_show_cursor())
