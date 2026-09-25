"""Palettes, theme choice and color resolution. See ui-spec.md §1, architecture.md D6.

claudemon.swift 295-361.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Optional, Tuple

RGBA = Tuple[float, float, float, float]


@dataclass(frozen=True)
class Palette:
    bg: RGBA
    border: RGBA
    fg: RGBA
    agents: RGBA
    dim: RGBA
    label: RGBA
    text: RGBA
    warn: RGBA
    hot: RGBA
    cyan: RGBA
    purple: RGBA
    tooltip_bg: RGBA


# ui-spec.md §1, exact floats copied from claudemon.swift 295-361.
TERMINAL = Palette(
    bg=(0.035, 0.045, 0.06, 0.985),
    border=(0.85, 0.47, 0.34, 0.55),
    fg=(0.55, 1.00, 0.62, 1.0),
    agents=(1.00, 0.62, 0.27, 1.0),
    dim=(0.33, 0.40, 0.45, 1.0),
    label=(0.93, 0.56, 0.40, 1.0),
    text=(0.86, 0.90, 0.93, 1.0),
    warn=(1.00, 0.78, 0.25, 1.0),
    hot=(1.00, 0.33, 0.33, 1.0),
    cyan=(0.35, 0.85, 0.95, 1.0),
    purple=(0.72, 0.58, 1.00, 1.0),
    tooltip_bg=(0.10, 0.12, 0.15, 1.0),
)

CLAUDE = Palette(
    bg=(0.10, 0.094, 0.086, 0.985),
    border=(0.85, 0.47, 0.34, 0.70),
    fg=(0.85, 0.47, 0.34, 1.0),
    agents=(0.91, 0.76, 0.54, 1.0),
    dim=(0.46, 0.43, 0.40, 1.0),
    label=(0.80, 0.72, 0.64, 1.0),
    text=(0.93, 0.90, 0.86, 1.0),
    warn=(0.95, 0.76, 0.30, 1.0),
    hot=(0.95, 0.36, 0.33, 1.0),
    cyan=(0.45, 0.78, 0.85, 1.0),
    purple=(0.70, 0.60, 0.95, 1.0),
    tooltip_bg=(0.16, 0.15, 0.14, 1.0),
)

LIGHT = Palette(
    bg=(0.972, 0.976, 0.968, 0.985),
    border=(0.78, 0.45, 0.33, 0.55),
    fg=(0.12, 0.55, 0.31, 1.0),
    agents=(0.85, 0.47, 0.02, 1.0),
    dim=(0.47, 0.52, 0.50, 1.0),
    label=(0.75, 0.34, 0.18, 1.0),
    text=(0.11, 0.14, 0.13, 1.0),
    warn=(0.72, 0.47, 0.02, 1.0),
    hot=(0.77, 0.19, 0.19, 1.0),
    cyan=(0.07, 0.53, 0.66, 1.0),
    purple=(0.44, 0.28, 0.91, 1.0),
    tooltip_bg=(1.00, 1.00, 1.00, 1.0),
)

# Third title dot, all themes; not part of Palette because it never changes with theme.
GREEN_DOT: RGBA = (0.35, 0.80, 0.45, 1.0)

ROLES = (
    "bg", "border", "fg", "agents", "dim", "label", "text",
    "warn", "hot", "cyan", "purple", "tooltip_bg", "green_dot",
)


class ThemeChoice(IntEnum):
    TERMINAL = 0
    CLAUDE = 1
    SYSTEM = 2

    @property
    def title(self) -> str:
        return {
            ThemeChoice.TERMINAL: "Terminal",
            ThemeChoice.CLAUDE: "Claude",
            ThemeChoice.SYSTEM: "Match System",
        }[self]

    @property
    def key(self) -> str:
        return {
            ThemeChoice.TERMINAL: "terminal",
            ThemeChoice.CLAUDE: "claude",
            ThemeChoice.SYSTEM: "system",
        }[self]

    @classmethod
    def from_key(cls, s: str) -> Optional["ThemeChoice"]:
        for t in cls:
            if t.key == s:
                return t
        return None


def palette_for(choice: ThemeChoice, system_dark: bool) -> Palette:
    """Terminal -> TERMINAL; Claude -> CLAUDE; Match System -> TERMINAL if OS dark else LIGHT."""
    if choice == ThemeChoice.TERMINAL:
        return TERMINAL
    if choice == ThemeChoice.CLAUDE:
        return CLAUDE
    return TERMINAL if system_dark else LIGHT


def model_role(family: str) -> str:
    """opus -> purple, sonnet -> fg, haiku -> cyan, fable -> warn, else dim."""
    if family == "opus":
        return "purple"
    if family == "sonnet":
        return "fg"
    if family == "haiku":
        return "cyan"
    if family == "fable":
        return "warn"
    return "dim"


def to_hex(rgb: Tuple[float, float, float]) -> str:
    """"#rrggbb" for an opaque rgb triple, each channel int(c*255 + 0.5) clamped to [0, 255]."""
    def channel(v: float) -> int:
        return max(0, min(255, int(v * 255 + 0.5)))

    r, g, b = rgb
    return "#{:02x}{:02x}{:02x}".format(channel(r), channel(g), channel(b))


def resolve(p: Palette, role: str, alpha: Optional[float] = None, mult: float = 1.0) -> str:
    """Blend the role's RGBA over p.bg (opaque) and return "#rrggbb".

    ui-spec.md §1 "Blending": rgb_out = a*rgb + (1-a)*bg_rgb. `alpha=` replaces the palette
    alpha (Swift withAlphaComponent); `mult=` multiplies it (faded rows/bars).
    """
    rgba = GREEN_DOT if role == "green_dot" else getattr(p, role)
    r, g, b, a = rgba
    if alpha is not None:
        a = alpha
    a = a * mult
    bg_r, bg_g, bg_b, _ = p.bg
    out = (
        a * r + (1 - a) * bg_r,
        a * g + (1 - a) * bg_g,
        a * b + (1 - a) * bg_b,
    )
    return to_hex(out)
