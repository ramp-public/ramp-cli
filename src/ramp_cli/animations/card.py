"""Card flip animation (standalone, full-bleed — enhanced mode-10 port)."""

from __future__ import annotations

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
    _sample_logo,
    _show_cursor,
)

# ---------------------------------------------------------------------------
# A. Card element constants
# ---------------------------------------------------------------------------


_OUTSIDE = 0
_BODY = 1
_DETAIL = 2
_LOGO = 3
_STRIPE = 4
_CVV = 5


def _rounded_corner_check(nx: float, ny: float, r: float = 0.08) -> bool:
    """Return True if (nx, ny) is inside the card (with rounded corners)."""
    if nx < 0.0 or nx > 1.0 or ny < 0.0 or ny > 1.0:
        return False
    cx = max(r, min(1.0 - r, nx))
    cy = max(r, min(1.0 - r, ny))
    return math.sqrt((nx - cx) ** 2 + (ny - cy) ** 2) <= r


def _sample_card_front(nx: float, ny: float) -> int:
    """Sample front face of card. Returns _OUTSIDE/_BODY/_DETAIL/_LOGO."""
    if not _rounded_corner_check(nx, ny):
        return _OUTSIDE

    # Ramp wordmark logo — upper portion, wide band
    if 0.08 < nx < 0.92 and 0.08 < ny < 0.44:
        lx = (nx - 0.08) / 0.84
        ly = (ny - 0.08) / 0.36
        if _sample_logo(lx, ly):
            return _LOGO

    # Chip — left side, middle
    if 0.07 < nx < 0.24 and 0.50 < ny < 0.75:
        return _DETAIL

    # Card number (4 groups across)
    if 0.07 < nx < 0.93 and 0.78 < ny < 0.88:
        return _DETAIL

    # Name / expiry row
    if 0.07 < nx < 0.68 and 0.91 < ny < 0.97:
        return _DETAIL

    return _BODY


def _sample_card_back(nx: float, ny: float) -> int:
    """Sample back face of card. Returns _OUTSIDE/_BODY/_DETAIL/_STRIPE/_CVV."""
    if not _rounded_corner_check(nx, ny):
        return _OUTSIDE

    # Magnetic stripe — full width, upper band
    if 0.0 <= nx <= 1.0 and 0.17 < ny < 0.37:
        return _STRIPE

    # Signature strip
    if 0.07 < nx < 0.73 and 0.48 < ny < 0.62:
        return _DETAIL

    # CVV block
    if 0.76 < nx < 0.93 and 0.48 < ny < 0.62:
        return _CVV

    return _BODY


# ---------------------------------------------------------------------------
# B. Main loop — full-bleed, enhanced wave shading from mode-10.ts
# ---------------------------------------------------------------------------


def show_card(file=None, duration: float = 10.0) -> None:
    """Animate 3D card flip with enhanced wave effects (full-bleed)."""
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
    rows = min(22, ts.lines - 2)

    # Card dimensions in terminal chars
    CARD_W = min(44, width - 4)
    CARD_H = min(20, rows - 2)

    total_lines = rows
    _write(_hide_cursor() + "\n" * total_lines + _move_up(total_lines))

    start = time.monotonic()
    end_time = start + duration
    n = 0

    try:
        while time.monotonic() < end_time:
            t = (time.monotonic() - start) * 1000.0 * 0.0001

            angle = t * 8.0
            cos_a = math.cos(angle)
            abs_cos = abs(cos_a)
            front = cos_a >= 0

            # Foreshortened card width
            eff_w = max(2, int(CARD_W * abs_cos))
            mid_x = width // 2
            mid_y = rows // 2
            card_left = mid_x - eff_w // 2
            card_right = card_left + eff_w
            card_top = mid_y - CARD_H // 2
            card_bot = card_top + CARD_H

            # Enhanced wave effects from mode-10.ts
            wave1 = math.sin(t * 3.0) * 0.5
            wave2 = math.cos(t * 2.1 + 1.0) * 0.3
            wave3 = math.sin(t * 4.7 + 2.0) * 0.2

            buf: list[str] = []
            if n > 0:
                buf.append(_move_up(total_lines))

            for y in range(rows):
                row_chars: list[str] = []
                for x in range(width):
                    # Background: scrolling binary digits with enhanced wave
                    scroll = int(t * 80)
                    bg_wave = math.sin(x * 0.3 + y * 0.2 + t * 2 + wave1) * math.cos(
                        y * 0.25 + t * 1.5 + wave2
                    )
                    bg_bri = int(((bg_wave + 1) / 2) * 30 + 18)
                    bg_char = "1" if ((x + scroll) * 7 + y * 13) % 2 == 0 else "0"

                    in_card = card_top <= y < card_bot and card_left <= x < card_right
                    if in_card:
                        nx = (x - card_left) / max(1, eff_w - 1)
                        ny = (y - card_top) / max(1, CARD_H - 1)
                        if not front:
                            nx = 1.0 - nx

                        shade = abs_cos
                        # Body shading from mode-10: shade + wave3 interaction via sin()
                        shade_mod = shade * (
                            0.85 + 0.15 * math.sin(nx * 3.0 + wave3 * 5.0)
                        )
                        val = (
                            _sample_card_front(nx, ny)
                            if front
                            else _sample_card_back(nx, ny)
                        )

                        if val == _OUTSIDE:
                            if use_color:
                                row_chars.append(
                                    f"{_fg(bg_bri, bg_bri, bg_bri)}{bg_char}"
                                )
                            else:
                                row_chars.append(bg_char)
                        elif val == _BODY:
                            bri = int(shade_mod * (0x76 - 0x3A) + 0x3A)
                            if use_color:
                                row_chars.append(f"{_fg(bri, bri, bri)}\u2591")
                            else:
                                row_chars.append("\u2591")
                        elif val == _DETAIL:
                            bri = int(shade_mod * (0xDA - 0x9E) + 0x9E)
                            if use_color:
                                row_chars.append(f"{_fg(bri, bri, bri)}\u2593")
                            else:
                                row_chars.append("\u2593")
                        elif val == _LOGO:
                            wave_logo = math.sin(x * 0.08 + t * 4 + wave1) * math.cos(
                                y * 0.12 + t * 3 + wave2
                            )
                            wn = (wave_logo + 1) / 2
                            lr = max(0, min(255, int(wn * 40 + 188)))
                            lg = max(0, min(255, int(wn * 30 + 210)))
                            lb = max(0, min(255, int(wn * 20 + 15)))
                            if use_color:
                                row_chars.append(f"{_fg(lr, lg, lb)}\u2588")
                            else:
                                row_chars.append("\u2588")
                        elif val == _STRIPE:
                            bri = int(shade_mod * (0x30 - 0x26) + 0x26)
                            if use_color:
                                row_chars.append(f"{_fg(bri, bri, bri)}\u2588")
                            else:
                                row_chars.append("\u2588")
                        elif val == _CVV:
                            bri = int(shade_mod * (0xDA - 0x9E) + 0x9E)
                            if use_color:
                                row_chars.append(f"{_fg(bri, bri, bri)}\u2592")
                            else:
                                row_chars.append("\u2592")
                    else:
                        if use_color:
                            row_chars.append(f"{_fg(bg_bri, bg_bri, bg_bri)}{bg_char}")
                        else:
                            row_chars.append(bg_char)

                line = "".join(row_chars)
                if use_color:
                    line += _reset()
                buf.append(line + "\n")

            _write("".join(buf))
            n += 1
            time.sleep(1 / 20)
    finally:
        _write(_show_cursor())
