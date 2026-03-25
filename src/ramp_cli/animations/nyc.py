"""NYC skyline animation (standalone, full-bleed — mode-14 port)."""

from __future__ import annotations

import io
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
    _nyc_pixel,
    _reset,
    _show_cursor,
)

# ---------------------------------------------------------------------------
# Main loop — full-bleed (no box frame), same pattern as rampy_animation.py
# ---------------------------------------------------------------------------


def show_nyc(file=None, duration: float = 10.0) -> None:
    """Animate NYC skyline full-bleed."""
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

    total_lines = rows
    _write(_hide_cursor() + "\n" * total_lines + _move_up(total_lines))

    start = time.monotonic()
    end_time = start + duration
    n = 0

    try:
        while time.monotonic() < end_time:
            t = time.monotonic() * 0.5

            buf: list[str] = []
            if n > 0:
                buf.append(_move_up(total_lines))

            for y in range(rows):
                row_chars: list[str] = []
                for x in range(width):
                    char, r, g, b = _nyc_pixel(x, y, t, width, rows)
                    if use_color:
                        row_chars.append(f"{_fg(r, g, b)}{char}")
                    else:
                        row_chars.append(char)
                line = "".join(row_chars)
                if use_color:
                    line += _reset()
                buf.append(line + "\n")

            _write("".join(buf))
            n += 1
            time.sleep(1 / 15)
    finally:
        _write(_show_cursor())
