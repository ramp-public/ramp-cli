"""One semantic palette for CSS, Rich renderables, and animation frames."""

import re
from dataclasses import dataclass, fields

MODES = ("auto", "dark", "light", "terminal")


def _rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))


def luminance(color: str) -> float:
    channels = [value / 255 for value in _rgb(color)]
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return sum(
        value * weight
        for value, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True)
    )


def contrast(first: str, second: str) -> float:
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def blend(first: str, second: str, amount: float) -> str:
    return "#" + "".join(
        f"{round(left * (1 - amount) + right * amount):02x}"
        for left, right in zip(_rgb(first), _rgb(second), strict=True)
    )


def readable(color: str, background: str, minimum: float = 4.5) -> str:
    if contrast(color, background) >= minimum:
        return color
    target = max(("#000000", "#ffffff"), key=lambda value: contrast(value, background))
    for step in range(1, 101):
        candidate = blend(color, target, step / 100)
        if contrast(candidate, background) >= minimum:
            return candidate
    return target


def environment_is_light(value: str | None) -> bool:
    """COLORFGBG is only a light/dark hint; its palette indexes aren't RGB."""
    if not value:
        return False
    try:
        index = int(value.split(";")[-1])
    except ValueError:
        return False
    if index in (7, 15):
        return True
    if 16 <= index < 232:
        levels = (0, 95, 135, 175, 215, 255)
        cube = index - 16
        color = "#" + "".join(
            f"{levels[channel]:02x}"
            for channel in (cube // 36, cube // 6 % 6, cube % 6)
        )
        return luminance(color) >= 0.179
    return 244 <= index <= 255


def parse_terminal_color(value: str) -> str | None:
    match = re.fullmatch(
        r"rgb:([0-9a-f]{1,4})/([0-9a-f]{1,4})/([0-9a-f]{1,4})", value, re.IGNORECASE
    )
    if not match:
        return None
    return "#" + "".join(
        f"{round(int(part, 16) * 255 / (16 ** len(part) - 1)):02x}"
        for part in match.groups()
    )


@dataclass(frozen=True)
class Palette:
    background: str
    foreground: str
    muted: str
    disabled: str
    accent: str
    accent_text: str
    surface: str
    hover: str
    active: str
    active_foreground: str
    active_accent: str
    hover_foreground: str
    surface_foreground: str
    surface_accent: str
    control_border: str
    button_border: str
    frame_border: str
    error: str
    success: str
    overlay: str
    dark: bool
    native: bool = False

    def css_variables(self) -> dict[str, str]:
        return {
            f"router-{item.name.replace('_', '-')}": getattr(self, item.name)
            for item in fields(self)
            if item.name not in ("dark", "native")
        }

    def rich(self, role: str) -> str:
        value = getattr(self, role)
        return value.removeprefix("ansi_") if value.startswith("ansi_") else value

    def animation_accent(self, shimmer: float) -> str:
        return (
            self.rich("accent")
            if self.native
            else blend(self.accent, self.foreground, shimmer * 0.12)
        )

    def animation_shade(self, shimmer: float, logo: bool) -> str:
        if self.native:
            return self.rich("muted")
        amount = (0.24 if logo else 0.4) + shimmer * (0.18 if logo else 0.3)
        return blend(self.background, self.foreground, amount)


def make_palette(
    mode: str,
    *,
    background: str | None = None,
    foreground: str | None = None,
    light_hint: bool = False,
) -> Palette:
    dark = (
        mode == "dark"
        or mode not in ("light", "dark")
        and (luminance(background) < 0.179 if background else not light_hint)
    )
    if mode == "terminal":
        accent = "ansi_yellow" if dark else "ansi_blue"
        return Palette(
            background="ansi_default",
            foreground="ansi_default",
            muted="ansi_bright_black",
            disabled="ansi_bright_black",
            accent=accent,
            accent_text="ansi_black" if dark else "ansi_bright_white",
            surface="ansi_default",
            hover="ansi_default",
            active="ansi_default",
            active_foreground="ansi_default",
            active_accent=accent,
            hover_foreground="ansi_default",
            surface_foreground="ansi_default",
            surface_accent=accent,
            control_border="ansi_bright_black",
            button_border="ansi_bright_black",
            frame_border="ansi_default",
            error="ansi_red",
            success="ansi_green",
            overlay="ansi_default",
            dark=dark,
            native=True,
        )
    bg = (
        background
        if mode == "auto" and background
        else "#252525"
        if dark
        else "#f7f8f2"
    )
    fg = readable(foreground or ("#ededed" if dark else "#242822"), bg)
    accent = readable("#e4f222" if dark else "#596500", bg)
    accent_text = max(("#000000", "#ffffff"), key=lambda value: contrast(value, accent))
    surface = blend(bg, fg, 0.055)
    hover = blend(bg, fg, 0.08)
    active = blend(bg, fg, 0.14)
    return Palette(
        background=bg,
        foreground=fg,
        muted=readable("#a5a5a5" if dark else "#5e665b", bg),
        disabled=blend(bg, fg, 0.45),
        accent=accent,
        accent_text=accent_text,
        surface=surface,
        hover=hover,
        active=active,
        active_foreground=readable(fg, active),
        active_accent=readable(accent, active),
        hover_foreground=readable(fg, hover),
        surface_foreground=readable(fg, surface),
        surface_accent=readable(accent, surface),
        control_border=readable("#888888" if dark else "#758070", bg, 3),
        button_border=readable("#555555" if dark else "#87907f", bg, 2),
        frame_border=fg,
        error=readable("#ff9393" if dark else "#b3261e", bg),
        success=readable("#74ef47" if dark else "#286a1c", bg),
        overlay=f"{bg} 75%",
        dark=dark,
    )


DARK = make_palette("dark")
