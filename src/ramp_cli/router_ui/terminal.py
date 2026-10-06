"""Read-only OSC color queries through Textual's single terminal input reader.

The input adapter is scoped to this App's POSIX driver, never a global patch.
Textual 8's normal keyboard, mouse, resize, and inline cursor parser remains in
charge after terminal-color replies have been removed from the byte stream.
"""

import os
import re
import selectors
import sys
import threading
import time
import weakref
from codecs import getincrementaldecoder
from types import MethodType

from textual import events
from textual._parser import ParseError
from textual._xterm_parser import XTermParser
from textual.message import Message

from ramp_cli.router_ui.palette import parse_terminal_color

QUERY_COLORS = "\x1b]11;?\x1b\\\x1b]10;?\x1b\\"
_COLOR = re.compile(r"\x1b\](10|11);([^\x07\x1b]*)(?:\x07|\x1b\\)")
_PREFIXES = ("\x1b]10;", "\x1b]11;", "\x1b[200~", "\x1b[201~")


def draws_block_glyphs() -> bool:
    """Whether half and quarter blocks fill their cells exactly.

    Terminal.app draws them from the font, which leaves seams between rows.
    """
    return os.environ.get("TERM_PROGRAM") != "Apple_Terminal"


class TerminalColor(Message):
    def __init__(self, code: int, color: str):
        super().__init__()
        self.code = code
        self.color = color


class ColorReplyFilter:
    """Remove only color replies; preserve keys, escape sequences, and paste."""

    def __init__(self, callback):
        self.callback = callback
        self.pending = ""
        self.pending_since = 0.0
        self.paste = False
        self.discard_color = False

    def feed(self, text: str) -> str:
        text = self.pending + text
        self.pending = ""
        output = []
        while text:
            if self.discard_color:
                boundary = re.search(r"[\x07\x1b]", text)
                if boundary is None:
                    break
                text = text[boundary.start() :]
                if text.startswith("\x07"):
                    self.discard_color = False
                    text = text[1:]
                elif text.startswith("\x1b\\"):
                    self.discard_color = False
                    text = text[2:]
                elif text == "\x1b":
                    self._hold(text)
                    break
                else:
                    # A new escape sequence resynchronizes ordinary keyboard input.
                    self.discard_color = False
            elif text.startswith("\x1b[200~"):
                self.pending_since = 0.0
                self.paste = True
                output.append(text[:6])
                text = text[6:]
            elif text.startswith("\x1b[201~"):
                self.pending_since = 0.0
                self.paste = False
                output.append(text[:6])
                text = text[6:]
            elif not self.paste and (
                text.startswith("\x1b]10;") or text.startswith("\x1b]11;")
            ):
                match = _COLOR.match(text)
                if match:
                    self.pending_since = 0.0
                    color = parse_terminal_color(match[2])
                    if color:
                        self.callback(int(match[1]), color)
                    # A malformed color response isn't a keyboard shortcut either.
                    text = text[match.end() :]
                else:
                    self.pending_since = 0.0
                    escape = text.find("\x1b", 2)
                    if escape >= 0 and text[escape:] != "\x1b":
                        # Drop the malformed reply, not the arrow/paste sequence after it.
                        text = text[escape:]
                    elif len(text) > 128:
                        self.discard_color = True
                        text = text[escape:] if escape >= 0 else ""
                    else:
                        self._hold(text)
                        break
            elif any(prefix.startswith(text) for prefix in _PREFIXES):
                self._hold(text)
                break
            else:
                self.pending_since = 0.0
                output.append(text[0])
                text = text[1:]
        return "".join(output)

    def _hold(self, text: str):
        self.pending = text
        if not self.pending_since:
            self.pending_since = time.monotonic()

    def flush(self, *, force: bool = False) -> str:
        if (
            self.discard_color
            and self.pending == "\x1b"
            and not force
            and time.monotonic() - self.pending_since >= 0.2
        ):
            self.discard_color = False
            self.pending = ""
            self.pending_since = 0.0
            return "\x1b"
        color_fragment = self.pending.startswith("\x1b]") and any(
            prefix.startswith(self.pending) or self.pending.startswith(prefix)
            for prefix in _PREFIXES[:2]
        )
        if color_fragment or self.discard_color:
            # Delayed replies are still protocol data. Never type them into a form.
            if force:
                self.pending = ""
                self.pending_since = 0.0
                self.discard_color = False
            return ""
        delay = 0.025 if self.pending == "\x1b" else 0.2
        if self.pending and (force or time.monotonic() - self.pending_since >= delay):
            pending, self.pending = self.pending, ""
            self.pending_since = 0.0
            return pending
        if not self.pending:
            self.pending_since = 0.0
        return ""


# Poll while a partial sequence awaits its timeout, matching Textual's cadence.
_PENDING_TIMEOUT = 0.05
# With nothing pending, poll only for a stop flag that can't wake select().
_IDLE_TIMEOUT = 0.1


def _close_fds(*fds):
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


class _WakingExitEvent(threading.Event):
    """The driver's exit flag, which also interrupts the input thread's select().

    The thread can block indefinitely while idle instead of polling for shutdown.
    """

    def __init__(self):
        super().__init__()
        self.read_fd, self.write_fd = os.pipe()
        os.set_blocking(self.read_fd, False)
        os.set_blocking(self.write_fd, False)
        # The pipe outlives each input thread (suspend/resume), so close it with the event.
        weakref.finalize(self, _close_fds, self.read_fd, self.write_fd)

    def set(self):
        super().set()
        try:
            os.write(self.write_fd, b"\0")
        except OSError:
            pass

    def drain(self):
        try:
            while os.read(self.read_fd, 64):
                pass
        except OSError:
            pass


def _input_pending(parser, colors) -> bool:
    """Whether tick() or flush() has a partial sequence to time out."""
    return bool(colors.pending) or getattr(parser, "_timeout_time", True) is not None


def _color_input_thread(driver):
    parser = XTermParser(driver._debug)
    colors = ColorReplyFilter(
        lambda code, color: driver.process_message(TerminalColor(code, color))
    )
    decode = getincrementaldecoder("utf-8")().decode

    def dispatch(messages):
        for message in messages:
            if driver.is_inline and isinstance(message, events.CursorPosition):
                driver.cursor_origin = (message.x, message.y)
            else:
                driver.process_message(message)

    wake = (
        driver.exit_event if isinstance(driver.exit_event, _WakingExitEvent) else None
    )
    with selectors.SelectSelector() as selector:
        selector.register(driver.fileno, selectors.EVENT_READ)
        if wake is not None:
            # A byte left by a previous stop (suspend/resume) is stale.
            wake.drain()
            selector.register(wake.read_fd, selectors.EVENT_READ, data="wake")
        eof = False
        try:
            while not driver.exit_event.is_set() and not eof:
                # Block while idle; time out only to expire a partial sequence.
                if _input_pending(parser, colors):
                    timeout = _PENDING_TIMEOUT
                else:
                    timeout = None if wake is not None else _IDLE_TIMEOUT
                for key, mask in selector.select(timeout):
                    if key.data == "wake":
                        wake.drain()
                        continue
                    if mask & selectors.EVENT_READ:
                        data = os.read(driver.fileno, 4096)
                        if not data:
                            eof = True
                            break
                        filtered = colors.feed(decode(data))
                        if filtered:
                            dispatch(parser.feed(filtered))
                pending = colors.flush()
                if pending:
                    dispatch(parser.feed(pending))
                dispatch(parser.tick())
            # Consume replies already waiting during shutdown, just as Textual does.
            if wake is not None:
                selector.unregister(wake.read_fd)
            for _, mask in selector.select(0.1):
                if mask & selectors.EVENT_READ:
                    filtered = colors.feed(
                        decode(os.read(driver.fileno, 4096), final=True)
                    )
                    if filtered:
                        dispatch(parser.feed(filtered))
        finally:
            pending = colors.flush(force=True)
            if pending:
                dispatch(parser.feed(pending))
            try:
                list(parser.feed(""))
            except (EOFError, ParseError):
                pass


def install_color_detection(driver) -> bool:
    if sys.platform == "win32" or driver.is_headless or driver.is_web:
        return False
    from textual.drivers.linux_driver import LinuxDriver  # noqa: PLC0415
    from textual.drivers.linux_inline_driver import LinuxInlineDriver  # noqa: PLC0415

    if (
        not isinstance(driver, (LinuxDriver, LinuxInlineDriver))
        or not os.isatty(driver.fileno)
        or not driver._file.isatty()
    ):
        return False
    driver.exit_event = _WakingExitEvent()
    driver.run_input_thread = MethodType(_color_input_thread, driver)
    return True


def max_height_rows() -> int | None:
    """Optional full-screen box cap in rows from RAMP_ROUTER_MAX_HEIGHT.

    Uncapped by default so the box fills the terminal. Rows, not pixels:
    pixel sizes are unreliable (Retina doubling, tmux/SSH reporting nothing).
    """
    try:
        value = int(os.environ.get("RAMP_ROUTER_MAX_HEIGHT", "").strip())
    except ValueError:
        return None
    return value if value > 0 else None


# Cells are about twice as tall as wide, so rows * CELL_ASPECT ~ pixel height
# in column units.
CELL_ASPECT = 2.0
# Tallest box allowed, as height / width. 1.0 keeps it no taller than square.
MAX_PORTRAIT = 1.0
MIN_ROWS = 24


def cell_aspect() -> float:
    """Measured cell height / width from TIOCGWINSZ, else CELL_ASPECT.

    Fonts vary (often ~2.2), so a fixed guess leaves the box visibly tall.
    Terminals that report no pixels (tmux, some SSH) get the guess.
    """
    try:
        import fcntl  # noqa: PLC0415
        import struct  # noqa: PLC0415
        import termios  # noqa: PLC0415

        rows, cols, xpixel, ypixel = struct.unpack(
            "HHHH",
            fcntl.ioctl(sys.__stdout__.fileno(), termios.TIOCGWINSZ, b"\0" * 8),
        )
    except (ImportError, OSError, AttributeError, ValueError):
        return CELL_ASPECT
    if not (rows and cols and xpixel and ypixel):
        return CELL_ASPECT
    aspect = (ypixel / rows) / (xpixel / cols)
    # Discard nonsense reports rather than squashing the box.
    return aspect if 1.0 <= aspect <= 3.0 else CELL_ASPECT


def fit_rows(columns: int, cell: float | None = None) -> int | None:
    """Row cap for a terminal `columns` wide: the env cap and the aspect cap.

    RAMP_ROUTER_MAX_ASPECT overrides MAX_PORTRAIT; 0 or "off" disables it.
    """
    caps = [rows for rows in (max_height_rows(),) if rows]
    raw = os.environ.get("RAMP_ROUTER_MAX_ASPECT", "").strip()
    try:
        aspect = float(raw) if raw else MAX_PORTRAIT
    except ValueError:
        aspect = 0.0 if raw.lower() == "off" else MAX_PORTRAIT
    if aspect > 0:
        caps.append(max(MIN_ROWS, int(columns * aspect / (cell or cell_aspect()))))
    return min(caps) if caps else None
