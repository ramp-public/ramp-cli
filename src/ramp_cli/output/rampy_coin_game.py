"""Rampy Coin Chase — interactive terminal game.

Port of game.js from www-financial-cli-ui. Rampy sits on a boat against an NYC
skyline. Press SPACE to jump and collect gold coins scrolling from the right.
Press ESC or Ctrl-C to exit.
"""

from __future__ import annotations

import math
import random
import shutil
import sys

import click

from ramp_cli.animations.rampy import (
    _SYMBOL_BITMAP,
    _SYMBOL_BMP_H,
    _SYMBOL_BMP_W,
    BB,
    BG_C,
    BR,
    _blinking,
    _render_eye,
)
from ramp_cli.output.lifecycle import Lifecycle
from ramp_cli.output.style import (
    _WIDTH_MAX,
    _WIDTH_MIN,
    _clear_eol,
    _color_supported,
    _fg,
    _move_to,
    _reset,
    _scene_hash,
)

# ---------------------------------------------------------------------------
# Game constants
# ---------------------------------------------------------------------------
GAME_ROWS = 25
WATERLINE = 0.48
JUMP_VELOCITY = -30.0
GRAVITY = 60.0
COIN_SPEED = 16.0
COIN_SPAWN_SEC = 1.8

# Scaled-down body for game size (14×6 vs normal 28×12)
G_BODY_W = 14
G_BODY_H = 6
G_SQUISH = 0.6

# Eye positions scaled to game body
G_EYE_L = {"cx": 4, "cy": 2.5, "rx": 2, "ry": 1.2, "px": 3.5, "py": 2.7, "pr": 0.7}
G_EYE_R = {"cx": 10, "cy": 2.5, "rx": 2, "ry": 1.2, "px": 9.5, "py": 2.7, "pr": 0.7}

# Boat dimensions
BOAT_W = 20

# Coin sprite
COIN_W = 10
COIN_H = 6
COIN_CX = 5
COIN_CY = 3
COIN_RX = 5
COIN_RY = 3
COL_COIN = (0xD7, 0xAF, 0x00)  # gold

COL_SCORE = (0xD7, 0xAF, 0x00)

# NYC building silhouettes
BUILDINGS = [
    {"x": 0.06, "w": 0.07, "h": 0.15, "layer": 0},
    {"x": 0.17, "w": 0.09, "h": 0.22, "layer": 0},
    {"x": 0.28, "w": 0.06, "h": 0.14, "layer": 0},
    {"x": 0.42, "w": 0.08, "h": 0.20, "layer": 0},
    {"x": 0.58, "w": 0.07, "h": 0.16, "layer": 0},
    {"x": 0.72, "w": 0.09, "h": 0.18, "layer": 0},
    {"x": 0.85, "w": 0.06, "h": 0.12, "layer": 0},
    {"x": 0.96, "w": 0.08, "h": 0.17, "layer": 0},
    {"x": 0.10, "w": 0.08, "h": 0.28, "layer": 1},
    {"x": 0.30, "w": 0.07, "h": 0.38, "layer": 1},
    {"x": 0.42, "w": 0.06, "h": 0.32, "layer": 1},
    {"x": 0.62, "w": 0.08, "h": 0.25, "layer": 1},
    {"x": 0.78, "w": 0.07, "h": 0.30, "layer": 1},
    {"x": 0.92, "w": 0.06, "h": 0.22, "layer": 1},
    {"x": 0.08, "w": 0.10, "h": 0.25, "layer": 2},
    {"x": 0.38, "w": 0.04, "h": 0.40, "layer": 2},
    {"x": 0.55, "w": 0.07, "h": 0.45, "layer": 2},
    {"x": 0.75, "w": 0.09, "h": 0.28, "layer": 2},
    {"x": 0.93, "w": 0.08, "h": 0.22, "layer": 2},
]


# ---------------------------------------------------------------------------
# Body rendering (scaled)
# ---------------------------------------------------------------------------
def _render_game_body(bx: int, by: int):
    """Render body pixel. Returns (char, r, g, b) or None."""
    if bx < 0 or bx >= G_BODY_W or by < 0 or by >= G_BODY_H:
        return None
    nx = bx / G_BODY_W
    ny = by / G_BODY_H
    # Use _sample_symbol with game squish
    cx = (nx - 0.5) * G_SQUISH + 0.5
    px = int(cx * _SYMBOL_BMP_W)
    py = int(ny * _SYMBOL_BMP_H)
    if px < 0 or px >= _SYMBOL_BMP_W or py < 0 or py >= _SYMBOL_BMP_H:
        return None
    if _SYMBOL_BITMAP[py * _SYMBOL_BMP_W + px] != 1:
        return None
    shade = 0.82 + 0.18 * math.sin(nx * 3.5 + ny * 2.5)
    return (
        "\u2588",
        max(0, min(255, int(BR * shade))),
        max(0, min(255, int(BG_C * shade))),
        max(0, min(255, int(BB * shade))),
    )


# ---------------------------------------------------------------------------
# Coin sprite rendering
# ---------------------------------------------------------------------------
def _render_coin_sprite(lx: int, ly: int):
    """Render coin pixel at local coords. Returns (char, r, g, b) or None."""
    dx = (lx + 0.5 - COIN_CX) / COIN_RX
    dy = (ly + 0.5 - COIN_CY) / COIN_RY
    dist = dx * dx + dy * dy
    if dist > 1.0:
        return None
    if dist > 0.55:
        return ("\u2588", *COL_COIN)  # rim
    if abs(lx - (COIN_CX + 1)) < 0.8:
        return ("\u2593", *COL_COIN)  # relief bar
    return ("\u2591", *COL_COIN)  # face


# ---------------------------------------------------------------------------
# Game pixel renderer
# ---------------------------------------------------------------------------
def _render_game_pixel(
    x: int,
    y: int,
    t: float,
    aw: int,
    ah: int,
    body_x0: int,
    body_y0: int,
    leg_top: int,
    feet_y: int,
    boat_x0: int,
    boat_deck_y: int,
    waterline_row: int,
    blink: bool,
    coins: list,
    score: int,
):
    """Render a single game pixel. Returns (char, r, g, b)."""
    nx = x / aw
    ny = y / ah

    # Score overlay — top-right
    if y == 1:
        score_text = f"SCORE: {score}"
        score_x0 = aw - len(score_text) - 2
        if score_x0 <= x < score_x0 + len(score_text):
            return (score_text[x - score_x0], *COL_SCORE)

    # Eyes + Body
    bx = x - body_x0
    by = y - body_y0
    if 0 <= bx < G_BODY_W and 0 <= by < G_BODY_H:
        le = _render_eye(bx, by, G_EYE_L, blink)
        if le:
            return le
        re = _render_eye(bx, by, G_EYE_R, blink)
        if re:
            return re
        body = _render_game_body(bx, by)
        if body:
            return body

    # Legs
    if leg_top <= y < feet_y:
        if x in (body_x0 + 4, body_x0 + 5, body_x0 + 9, body_x0 + 10):
            return ("\u2588", 0x3A, 0x3A, 0x3A)

    # Feet
    if y == feet_y:
        if (body_x0 + 3 <= x <= body_x0 + 6) or (body_x0 + 8 <= x <= body_x0 + 11):
            return ("\u2588", 0x1C, 0x1C, 0x1C)

    # Coins
    for coin in coins:
        if coin["collected"]:
            continue
        clx = x - round(coin["x"])
        cly = y - coin["y"]
        if 0 <= clx < COIN_W and 0 <= cly < COIN_H:
            cc = _render_coin_sprite(clx, cly)
            if cc:
                return cc

    # Boat deck
    if y == boat_deck_y:
        if x == boat_x0:
            return ("\u2597", 0xD7, 0x87, 0x5F)
        if x == boat_x0 + BOAT_W - 1:
            return ("\u2596", 0xD7, 0x87, 0x5F)
        if boat_x0 < x < boat_x0 + BOAT_W - 1:
            return ("\u2580", 0xD7, 0x87, 0x5F)
    # Boat hull
    if y == boat_deck_y + 1:
        if x == boat_x0 + 2:
            return ("\u259d", 0x87, 0x5F, 0x00)
        if x == boat_x0 + BOAT_W - 3:
            return ("\u2598", 0x87, 0x5F, 0x00)
        if boat_x0 + 2 < x < boat_x0 + BOAT_W - 3:
            return ("\u2580", 0x87, 0x5F, 0x00)

    # Buildings
    for bi in range(len(BUILDINGS) - 1, -1, -1):
        bd = BUILDINGS[bi]
        b_left = bd["x"] - bd["w"] / 2
        b_right = bd["x"] + bd["w"] / 2
        b_top = WATERLINE - bd["h"]
        if nx < b_left or nx > b_right or ny < b_top or ny > WATERLINE:
            continue
        bny = (ny - b_top) / bd["h"]
        bnx = (nx - b_left) / bd["w"]
        floor_idx = int(bny * 15)
        col_idx = int(bnx * 8)
        is_window = floor_idx % 2 == 1 and col_idx % 3 != 0
        window_lit = (
            is_window
            and bd["layer"] >= 1
            and _scene_hash(bi * 1000 + floor_idx * 37 + col_idx * 13 + int(t * 30))
            > 0.4
        )
        if bd["layer"] == 0:
            base_bri = 25 + _scene_hash(bi + 500) * 20
        elif bd["layer"] == 1:
            base_bri = 55 + _scene_hash(bi + 500) * 35
        else:
            base_bri = 100 + _scene_hash(bi + 500) * 70
        if window_lit:
            base_bri = min(255, base_bri + 60)
        bri = max(0, min(255, int(base_bri)))
        if bd["layer"] == 0:
            ch = "\u2591"
        elif bd["layer"] == 1:
            ch = "\u2592"
        elif window_lit:
            ch = "\u2593"
        else:
            ch = "\u2588"
        return (ch, bri, bri, bri)

    # Waterfront
    if WATERLINE - 0.04 <= ny <= WATERLINE:
        return ("\u2593", 0x1C, 0x1C, 0x1C)

    # Water
    if ny > WATERLINE:
        ripple = math.sin(nx * 30 + t * 20) * math.sin(ny * 20 + t * 15)
        bri = int(((ripple + 1) / 2) * 20 + 15)
        r = int(bri * 0.2)
        g = int(bri * 0.5)
        b = min(255, int(bri * 1.3))
        wave_chars = "~\u2248\u2591"
        wi = int(math.sin(nx * 10 + t * 8 + ny * 5) * 1.5 + 1.5) % len(wave_chars)
        return (wave_chars[wi], r, g, b)

    # Stars
    if _scene_hash(x * 7.3 + y * 13.1 + 0.5) > 0.97:
        bri = int(50 + math.sin(t * 30 * _scene_hash(x * 3.1 + y * 5.7)) * 30 + 30)
        bri = max(10, min(255, bri))
        return ("\u00b7", bri, bri, bri)

    # Night sky
    return (" ", 0x0A, 0x0A, 0x0A)


# ---------------------------------------------------------------------------
# Main game loop
# ---------------------------------------------------------------------------
MIN_GAME_LINES = 20  # Minimum terminal height for the coin game


def show_coin_game(file=None) -> None:
    """Run the Rampy Coin Chase game."""
    file = file or sys.stdout
    use_color = _color_supported(file)

    ts = shutil.get_terminal_size((80, 24))
    if ts.lines < MIN_GAME_LINES:
        raise click.ClickException(
            f"Terminal too short for coin game ({ts.lines} lines). "
            f"Minimum {MIN_GAME_LINES} lines required — resize your terminal and try again."
        )

    # Game state
    rampy_jump_y = 0.0
    rampy_velocity = 0.0
    is_grounded = True
    coins: list[dict] = []
    score = 0
    last_coin_spawn = 0.0
    prev_time = 0.0

    def _get_layout():
        ts = shutil.get_terminal_size((80, 24))
        width = max(_WIDTH_MIN, min(ts.columns, _WIDTH_MAX))
        rows = max(10, min(GAME_ROWS, ts.lines - 2))
        return width, rows

    def render_full(t: float) -> None:
        """Full-screen render: header + game rows + footer."""
        width, rows = _get_layout()
        buf: list[str] = []

        # Header line (row 1)
        header = "RAMPY COIN CHASE [SPACE jump | ESC quit]"
        if use_color:
            buf.append(
                f"{_move_to(1, 1)}{_fg(0xD7, 0xAF, 0x00)}{header[:width]}{_reset()}{_clear_eol()}"
            )
        else:
            buf.append(f"{_move_to(1, 1)}{header[:width]}{_clear_eol()}")

        # Game rows (rows 2 .. rows+1)
        _render_game_rows(buf, t, width, rows)

        # Footer (row rows+2)
        footer = f"Score: {score}"
        footer_row = rows + 2
        if use_color:
            buf.append(
                f"{_move_to(footer_row, 1)}{_fg(0xD7, 0xAF, 0x00)}{footer}{_reset()}{_clear_eol()}"
            )
        else:
            buf.append(f"{_move_to(footer_row, 1)}{footer}{_clear_eol()}")

        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    def _render_game_rows(buf: list[str], t: float, width: int, rows: int) -> None:
        nonlocal rampy_jump_y, rampy_velocity, is_grounded
        nonlocal coins, score, last_coin_spawn, prev_time

        dt = min(t - prev_time, 0.05) if prev_time > 0 else 0.0
        prev_time = t

        # --- Physics ---
        if not is_grounded:
            rampy_velocity += GRAVITY * dt
            rampy_jump_y += rampy_velocity * dt
            if rampy_jump_y >= 0:
                rampy_jump_y = 0.0
                rampy_velocity = 0.0
                is_grounded = True

        # --- Layout ---
        waterline_row = int(rows * WATERLINE)
        boat_deck_y = waterline_row + 9
        base_feet_y = boat_deck_y - 1
        base_leg_top = base_feet_y - 2
        base_body_y0 = base_leg_top - G_BODY_H
        jump_offset = int(rampy_jump_y)
        body_y0 = base_body_y0 + jump_offset
        leg_top = base_leg_top + jump_offset
        feet_y = base_feet_y + jump_offset
        body_x0 = int(width * 0.2)
        boat_x0 = body_x0 + G_BODY_W // 2 - BOAT_W // 2

        # --- Spawn coins ---
        if t - last_coin_spawn > COIN_SPAWN_SEC:
            standing_body_top = base_leg_top - G_BODY_H
            max_coin_y = standing_body_top - COIN_H
            min_coin_y = 1
            if max_coin_y > min_coin_y:
                coins.append(
                    {
                        "x": float(width + 2),
                        "y": min_coin_y + random.randint(0, max_coin_y - min_coin_y),
                        "collected": False,
                    }
                )
            last_coin_spawn = t

        # --- Move coins ---
        for coin in coins:
            coin["x"] -= COIN_SPEED * dt

        # --- Collision detection ---
        rampy_top = body_y0
        rampy_bot = feet_y + 1
        rampy_left = body_x0
        rampy_right = body_x0 + G_BODY_W
        for coin in coins:
            if coin["collected"]:
                continue
            coin_left = round(coin["x"])
            coin_right = coin_left + COIN_W
            coin_top = coin["y"]
            coin_bot = coin["y"] + COIN_H
            if (
                coin_right >= rampy_left
                and coin_left <= rampy_right
                and coin_bot >= rampy_top
                and coin_top <= rampy_bot
            ):
                coin["collected"] = True
                score += 1

        # --- Cull coins ---
        coins = [c for c in coins if c["x"] > -COIN_W and not c["collected"]]

        # --- Render ---
        blink = _blinking(t)
        for y in range(rows):
            row_chars: list[str] = []
            for x in range(width):
                cell = _render_game_pixel(
                    x,
                    y,
                    t,
                    width,
                    rows,
                    body_x0,
                    body_y0,
                    leg_top,
                    feet_y,
                    boat_x0,
                    boat_deck_y,
                    waterline_row,
                    blink,
                    coins,
                    score,
                )
                ch, cr, cg, cb = cell
                if use_color:
                    row_chars.append(f"{_fg(cr, cg, cb)}{ch}")
                else:
                    row_chars.append(ch)
            line = "".join(row_chars)
            if use_color:
                line += _reset()
            buf.append(f"{_move_to(y + 2, 1)}{line}{_clear_eol()}")

    def render_frame(t: float) -> None:
        """Per-frame update — physics, coins, render."""
        width, rows = _get_layout()
        buf: list[str] = []

        _render_game_rows(buf, t, width, rows)

        # Update footer
        footer = f"Score: {score}"
        footer_row = rows + 2
        if use_color:
            buf.append(
                f"{_move_to(footer_row, 1)}{_fg(0xD7, 0xAF, 0x00)}{footer}{_reset()}{_clear_eol()}"
            )
        else:
            buf.append(f"{_move_to(footer_row, 1)}{footer}{_clear_eol()}")

        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    def on_input(data: bytes) -> None:
        nonlocal rampy_velocity, is_grounded
        if data[0] == 0x20 and is_grounded:  # SPACE
            rampy_velocity = JUMP_VELOCITY
            is_grounded = False

    Lifecycle(render_full, render_frame, on_input, fps=20).start()
