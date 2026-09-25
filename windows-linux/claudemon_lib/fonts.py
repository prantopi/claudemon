"""Font family fallback and the four tk fonts the UI uses. See ui-spec.md §2.

claudemon.swift 348-351. [tk]
"""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass

FAMILIES = (
    "Cascadia Mono",
    "Consolas",
    "JetBrains Mono",
    "DejaVu Sans Mono",
    "Ubuntu Mono",
    "Liberation Mono",
)


def pick_family(root: tk.Misc) -> str:
    """First family in FAMILIES present in tkinter.font.families(root), else TkFixedFont's family."""
    available = set(tkfont.families(root))
    for fam in FAMILIES:
        if fam in available:
            return fam
    return tkfont.Font(root=root, name="TkFixedFont", exists=True).actual("family")


def _px_size(v: float, scale: float) -> int:
    """Negative (pixel) Tk font size for a Swift point size v at the given DPI scale."""
    return -int(v * scale + 0.5)


@dataclass
class Fonts:
    font: tkfont.Font
    bold: tkfont.Font
    small: tkfont.Font
    small_bold: tkfont.Font

    @classmethod
    def create(cls, root: tk.Misc, scale: float) -> "Fonts":
        family = pick_family(root)
        size = _px_size(11, scale)
        small_size = _px_size(9.5, scale)
        return cls(
            font=tkfont.Font(root=root, family=family, size=size, weight="normal"),
            bold=tkfont.Font(root=root, family=family, size=size, weight="bold"),
            small=tkfont.Font(root=root, family=family, size=small_size, weight="normal"),
            small_bold=tkfont.Font(root=root, family=family, size=small_size, weight="bold"),
        )

    def get(self, bold: bool, small: bool = False) -> tkfont.Font:
        if small:
            return self.small_bold if bold else self.small
        return self.bold if bold else self.font
