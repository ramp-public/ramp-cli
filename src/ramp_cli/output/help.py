"""Custom Click help formatter with box-drawing frames."""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
from typing import Iterator

import click


def _wrap_text(text: str, width: int) -> list[str]:
    """Word-wrap text to the given width."""
    if not text:
        return [""]
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = current + " " + word if current else word
        if len(candidate) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def _framed_line(box_v: str, content: str, inner: int) -> str:
    """Build a framed line: │ content padded to inner width │"""
    visible = len(content)
    trail = " " * max(0, inner - visible)
    return f"{box_v} {content}{trail} {box_v}\n"


class BoxHelpFormatter(click.HelpFormatter):
    """HelpFormatter that wraps Options/Commands sections in box-drawing frames,
    and prepends a strip-wave banner when stdout is a TTY."""

    _suppress_wave: bool = False

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._section_active = False
        self._section_title: str | None = None
        self._section_rows: list[tuple[str, str]] = []

    @contextlib.contextmanager
    def section(self, name: str) -> Iterator[None]:
        self._section_title = name
        self._section_rows = []
        self._section_active = True
        try:
            yield
        finally:
            self._section_active = False
            self._flush_section()

    def write_dl(self, rows, col_max: int = 30, col_spacing: int = 2) -> None:
        rows = list(rows)
        if self._section_active:
            self._section_rows.extend(rows)
        else:
            super().write_dl(rows, col_max=col_max, col_spacing=col_spacing)

    def indent(self) -> None:
        if not self._section_active:
            super().indent()

    def dedent(self) -> None:
        if not self._section_active:
            super().dedent()

    def _flush_section(self) -> None:
        if not self._section_title:
            return
        from ramp_cli.output.style import (
            _WIDTH_MAX,
            _WIDTH_MIN,
            BOX_V,
            _fg,
            _frame_bottom,
            _frame_top,
            _reset,
        )

        use_color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        width = max(
            _WIDTH_MIN, min(shutil.get_terminal_size((80, 24)).columns, _WIDTH_MAX)
        )
        inner = width - 4

        rows = [(f, s) for f, s in self._section_rows if f != "-h, --help"]
        if not rows:
            self._section_title = None
            self._section_rows = []
            return

        col1_max = min(max(len(r[0]) for r in rows), inner // 2)
        col_gap = 4
        offset = col1_max + col_gap
        desc_width = inner - offset

        self.write("\n")
        self.write(_frame_top(self._section_title, width, use_color) + "\n")
        self.write(f"{BOX_V} {'':<{inner}} {BOX_V}\n")

        for first, second in rows:
            # Truncate the name if it exceeds the frame
            if len(first) > inner:
                first = first[: inner - 1] + "\u2026"

            pad = max(1, offset - len(first))
            desc_lines = _wrap_text(second, desc_width)

            # First line: name + first description line
            if use_color:
                name = f"{_fg(228, 242, 33)}{first}{_reset()}"
                desc = f"{_fg(140, 140, 140)}{desc_lines[0]}{_reset()}"
                visible = len(first) + pad + len(desc_lines[0])
                trail = " " * max(0, inner - visible)
                self.write(f"{BOX_V} {name}{' ' * pad}{desc}{trail} {BOX_V}\n")
            else:
                line = first + (" " * pad) + desc_lines[0]
                self.write(f"{BOX_V} {line:<{inner}} {BOX_V}\n")

            # Continuation lines
            indent_str = " " * offset
            for dl in desc_lines[1:]:
                if use_color:
                    dl_colored = f"{_fg(140, 140, 140)}{dl}{_reset()}"
                    visible = len(indent_str) + len(dl)
                    trail = " " * max(0, inner - visible)
                    self.write(f"{BOX_V} {indent_str}{dl_colored}{trail} {BOX_V}\n")
                else:
                    self.write(f"{BOX_V} {indent_str + dl:<{inner}} {BOX_V}\n")

        self.write(f"{BOX_V} {'':<{inner}} {BOX_V}\n")
        self.write(_frame_bottom(width) + "\n")

        self._section_title = None
        self._section_rows = []

    def write_usage(self, prog: str, args: str = "", prefix: str | None = None) -> None:
        from ramp_cli.output.style import (
            _WIDTH_MAX,
            _WIDTH_MIN,
            BOX_V,
            _frame_bottom,
            _frame_top,
        )

        use_color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        width = max(
            _WIDTH_MIN, min(shutil.get_terminal_size((80, 24)).columns, _WIDTH_MAX)
        )
        inner = width - 4
        usage_text = f"{prog} {args}".strip()
        self.write(_frame_top("Usage", width, use_color) + "\n")
        self.write(f"{BOX_V} {'':<{inner}} {BOX_V}\n")
        for line in usage_text.splitlines():
            line = line.strip()
            if line:
                self.write(f"{BOX_V} {line:<{inner}} {BOX_V}\n")
        self.write(f"{BOX_V} {'':<{inner}} {BOX_V}\n")
        self.write(_frame_bottom(width) + "\n")

    def getvalue(self) -> str:
        self._flush_section()
        result = super().getvalue()
        from ramp_cli.output.formatter import is_quiet

        if (
            not BoxHelpFormatter._suppress_wave
            and not is_quiet()
            and sys.stdout.isatty()
            and not os.environ.get("NO_COLOR")
        ):
            from ramp_cli.output.style import (
                _WIDTH_MAX,
                _WIDTH_MIN,
                _build_strip_wave_str,
            )

            width = max(
                _WIDTH_MIN, min(shutil.get_terminal_size((80, 24)).columns, _WIDTH_MAX)
            )
            wave = _build_strip_wave_str(rows=4, width=width, use_color=True)
            return wave + "\n" + result
        return result


def make_box_formatter(ctx: click.Context, is_agent_mode: bool) -> click.HelpFormatter:
    """Create the appropriate help formatter based on mode."""
    if is_agent_mode:
        return click.HelpFormatter(
            width=ctx.terminal_width,
            max_width=ctx.max_content_width,
        )
    return BoxHelpFormatter(
        width=ctx.terminal_width,
        max_width=ctx.max_content_width,
    )


def suppress_help_text(
    self: click.Command,
    ctx: click.Context,
    formatter: click.HelpFormatter,
) -> None:
    del self, ctx, formatter
    return None
