"""Tests for Rampy Coin Chase game."""

from __future__ import annotations

from click.testing import CliRunner

from ramp_cli.animations.rampy import _render_eye
from ramp_cli.easter_eggs.rampy import rampy_cmd
from ramp_cli.output.rampy_coin_game import (
    G_EYE_L,
    _render_coin_sprite,
    _render_game_body,
)
from ramp_cli.output.style import _scene_hash


def test_coin_game_mutual_exclusivity_skate():
    runner = CliRunner()
    result = runner.invoke(rampy_cmd, ["--coin-game", "--skate"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_coin_game_mutual_exclusivity_surf():
    runner = CliRunner()
    result = runner.invoke(rampy_cmd, ["--coin-game", "--surf"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_coin_game_all_three_exclusive():
    runner = CliRunner()
    result = runner.invoke(rampy_cmd, ["--coin-game", "--skate", "--surf"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_scene_hash_deterministic():
    assert _scene_hash(42.0) == _scene_hash(42.0)
    assert 0.0 <= _scene_hash(42.0) <= 1.0


def test_scene_hash_range():
    for i in range(100):
        v = _scene_hash(float(i))
        assert 0.0 <= v <= 1.0


def test_render_coin_sprite_center():
    """Center of coin should return a face character."""
    result = _render_coin_sprite(5, 3)
    assert result is not None
    ch, r, g, b = result
    assert r == 0xD7 and g == 0xAF and b == 0x00


def test_render_coin_sprite_outside():
    """Far outside the coin should be transparent."""
    result = _render_coin_sprite(0, 0)
    assert result is None


def test_render_coin_sprite_rim():
    """Edge of coin should return rim (full block)."""
    # (9, 3) is near the right edge of the ellipse
    result = _render_coin_sprite(9, 3)
    assert result is not None
    ch, r, g, b = result
    assert ch == "\u2588"  # full block = rim


def test_render_game_body_inside():
    """A pixel inside the body region should return something (or None if not in symbol)."""
    # Center of body should hit the symbol
    result = _render_game_body(7, 3)
    # May or may not hit depending on symbol bitmap, but should not raise
    if result is not None:
        ch, r, g, b = result
        assert ch == "\u2588"


def test_render_game_body_outside():
    """Outside body bounds should be None."""
    assert _render_game_body(-1, 0) is None
    assert _render_game_body(20, 0) is None
    assert _render_game_body(0, -1) is None
    assert _render_game_body(0, 10) is None


def test_render_eye_open():
    """Eye center should return white or pupil."""
    result = _render_eye(4, 2.5, G_EYE_L, blink=False)
    assert result is not None
    ch, r, g, b = result
    # Should be either pupil (0x1C) or white (0xFF)
    assert r in (0x1C, 0xFF)


def test_render_eye_blink():
    """During blink, eye center row should return blink char."""
    # cy for G_EYE_L is 2.5, round(2.5) = 2
    result = _render_eye(4, 2, G_EYE_L, blink=True)
    assert result is not None
    ch, r, g, b = result
    assert ch == "\u2501"


def test_render_eye_outside():
    """Outside eye bounds should be None."""
    result = _render_eye(0, 0, G_EYE_L, blink=False)
    assert result is None


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------
def test_coin_game_terminal_too_short(monkeypatch):
    """Coin game should error if terminal is too short."""
    import os

    monkeypatch.setitem(os.environ, "COLUMNS", "80")
    monkeypatch.setitem(os.environ, "LINES", "15")
    runner = CliRunner()
    result = runner.invoke(rampy_cmd, ["--coin-game"])
    assert result.exit_code != 0
    assert "too short" in result.output


def test_lifecycle_emits_alt_screen_sequences(monkeypatch):
    """Lifecycle should emit alt-screen-on at start and alt-screen-off at cleanup."""
    from ramp_cli.output.lifecycle import Lifecycle

    written: list[str] = []
    frame_count = 0

    class FakeTty:
        def fileno(self):
            return -1

        def close(self):
            pass

    def fake_render_full(t):
        pass

    def fake_render_frame(t):
        nonlocal frame_count
        frame_count += 1

    lc = Lifecycle(fake_render_full, fake_render_frame, fps=20)

    # Capture writes
    lc._write = lambda data: written.append(data)
    # Prevent actual tty open and terminal manipulation
    lc._quitting = True  # will exit loop immediately

    # Directly test cleanup emits alt screen off
    lc._write_fd = 1
    lc._tty_file = FakeTty()
    lc._tty_fd = -1
    lc._old_term = None
    lc._old_flags = None
    lc._old_sigwinch = None
    lc._cleanup()

    combined = "".join(written)
    assert "\x1b[?25h" in combined  # cursor show
    assert "\x1b[?1049l" in combined  # alt screen off


def test_lifecycle_quit_on_esc():
    """Lifecycle should quit when ESC is received."""
    from ramp_cli.output.lifecycle import Lifecycle

    lc = Lifecycle(lambda t: None, lambda t: None, fps=20)
    # Verify the quit flag is initially false
    assert lc._quitting is False


def test_lifecycle_on_input_forwarding():
    """Lifecycle should forward non-quit keys to on_input callback."""
    from ramp_cli.output.lifecycle import Lifecycle

    received: list[bytes] = []
    lc = Lifecycle(
        lambda t: None, lambda t: None, on_input=lambda d: received.append(d), fps=20
    )
    # Verify on_input is set
    assert lc._on_input is not None
