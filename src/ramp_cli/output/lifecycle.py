"""Terminal lifecycle manager — alt screen, raw mode, animation loop, input, resize.

Port of createLifecycle from www-financial-cli-ui/scripts/cli/lib/app.js.
Manages the full terminal lifecycle so game/animation code only needs to
provide three callbacks: render_full, render_frame, and on_input.
"""

from __future__ import annotations

import fcntl
import os
import select
import signal
import sys
import termios
import time
import tty
from typing import Callable

from ramp_cli.output.style import (
    _alt_screen_off,
    _alt_screen_on,
    _clear_screen,
    _hide_cursor,
    _show_cursor,
)

RESIZE_DEBOUNCE_S = 0.05
DEFAULT_FPS = 20


class Lifecycle:
    """Manages alt screen, raw mode, animation loop, input, and resize.

    Parameters
    ----------
    render_full : callable(t: float) -> None
        Full-screen render on start and after resize. *t* is seconds since start.
    render_frame : callable(t: float) -> None
        Per-frame animation update. *t* is seconds since start.
    on_input : callable(data: bytes) -> None, optional
        Called for each keypress that is not ESC or Ctrl-C.
    fps : int
        Target frames per second.
    """

    def __init__(
        self,
        render_full: Callable[[float], None],
        render_frame: Callable[[float], None],
        on_input: Callable[[bytes], None] | None = None,
        fps: int = DEFAULT_FPS,
    ) -> None:
        self._render_full = render_full
        self._render_frame = render_frame
        self._on_input = on_input
        self._fps = fps
        self._quitting = False
        self._resize_pending = False
        self._tty_file: object | None = None
        self._tty_fd: int = -1
        self._old_term: list | None = None
        self._old_flags: int | None = None
        self._old_sigwinch: object | None = None
        self._write_fd: int = -1

    def _write(self, data: str) -> None:
        encoded = data.encode()
        fd = self._write_fd
        offset = 0
        while offset < len(encoded):
            offset += os.write(fd, encoded[offset:])

    def _on_resize(self, signum: int, frame: object) -> None:
        self._resize_pending = True

    def start(self) -> None:
        """Block until quit. Manages alt screen, raw mode, loop, input, resize."""
        self._write_fd = sys.stdout.fileno()

        # Open /dev/tty directly — works even when Click replaces stdin
        self._tty_file = open("/dev/tty", "rb", buffering=0)  # noqa: SIM115
        self._tty_fd = self._tty_file.fileno()
        self._old_term = termios.tcgetattr(self._tty_fd)
        self._old_flags = fcntl.fcntl(self._tty_fd, fcntl.F_GETFL)

        try:
            tty.setraw(self._tty_fd)
            fcntl.fcntl(self._tty_fd, fcntl.F_SETFL, self._old_flags | os.O_NONBLOCK)

            # Install resize handler
            self._old_sigwinch = signal.getsignal(signal.SIGWINCH)
            signal.signal(signal.SIGWINCH, self._on_resize)

            # Enter alt screen
            self._write(_alt_screen_on() + _hide_cursor() + _clear_screen())

            # Initial full render
            self._render_full(0.0)

            t0 = time.monotonic()
            frame_interval = 1.0 / self._fps

            while not self._quitting:
                frame_start = time.monotonic()
                t = frame_start - t0

                # Handle resize
                if self._resize_pending:
                    self._resize_pending = False
                    self._write(_clear_screen())
                    self._render_full(t)

                # Read input (non-blocking)
                try:
                    readable, _, _ = select.select([self._tty_fd], [], [], 0)
                    if readable:
                        data = os.read(self._tty_fd, 32)
                        if data:
                            # ESC (alone) or Ctrl-C → quit
                            if data[0] == 27 and len(data) == 1:
                                break
                            if data[0] == 3:
                                break
                            if self._on_input:
                                self._on_input(data)
                except OSError:
                    pass

                # Render frame
                self._render_frame(t)

                # Sleep for remaining frame budget to maintain consistent pacing
                remaining = frame_interval - (time.monotonic() - frame_start)
                if remaining > 0:
                    time.sleep(remaining)

        finally:
            self._quitting = True
            self._cleanup()

    def _cleanup(self) -> None:
        """Restore terminal state."""
        # Restore SIGWINCH
        if self._old_sigwinch is not None:
            signal.signal(signal.SIGWINCH, self._old_sigwinch)

        # Restore termios and fcntl
        if self._old_term is not None:
            termios.tcsetattr(self._tty_fd, termios.TCSADRAIN, self._old_term)
        if self._old_flags is not None:
            fcntl.fcntl(self._tty_fd, fcntl.F_SETFL, self._old_flags)

        # Close tty
        if self._tty_file is not None:
            self._tty_file.close()

        # Restore screen
        try:
            self._write(_show_cursor() + _alt_screen_off())
        except OSError:
            pass
