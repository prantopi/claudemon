"""Low-level canvas drawing helpers shared by charts.py and view.py.

claudemon.swift 646-680. [tk]
"""
from __future__ import annotations

import math
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass
from typing import List, Optional

from .fonts import Fonts
from .layout import Metrics
from .rows import Seg
from .theme import Palette, resolve

# Windows -transparentcolor key; the canvas background when transparent_corners is on.
KEY_COLOR = "#010203"
# aqua (macOS) Tk's fully transparent colour, used with `wm attributes -transparent 1`.
MAC_TRANSPARENT = "systemTransparent"


@dataclass
class DrawContext:
    canvas: tk.Canvas
    palette: Palette
    fonts: Fonts
    m: Metrics
    width: int
    height: int
    now: float
    spin_phase: bool
    reduce_motion: bool

    def color(self, role: str, alpha: Optional[float] = None, mult: float = 1.0) -> str:
        return resolve(self.palette, role, alpha, mult)


def round_rect(
    canvas: tk.Canvas,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    r: float,
    *,
    fill: str = "",
    outline: str = "",
    width: float = 1,
    true_radius: bool = False,
) -> None:
    """Smooth rounded-rect polygon; r is clamped to half the smaller side; r <= 0 draws a plain rect.

    Tk's smooth polygons are parabolic splines through the midpoints of the segments, so by default
    each corner is a parabola from r/2 before the corner to r/2 after it: a visibly smaller corner
    than r. true_radius=True instead draws each corner as a circular arc of radius r (a
    _ARC_STEPS-segment polyline, which Tk antialiases), like Swift's NSBezierPath(roundedRect:).
    """
    w = x1 - x0
    h = y1 - y0
    r = max(0.0, min(r, min(w, h) / 2))
    if r <= 0:
        canvas.create_rectangle(x0, y0, x1, y1, fill=fill, outline=outline, width=width)
        return
    if true_radius:
        canvas.create_polygon(arc_rect_points(x0, y0, x1, y1, r), fill=fill, outline=outline, width=width)
        return
    points = [
        x0 + r, y0,
        x1 - r, y0,
        x1, y0,
        x1, y0 + r,
        x1, y1 - r,
        x1, y1,
        x1 - r, y1,
        x0 + r, y1,
        x0, y1,
        x0, y1 - r,
        x0, y0 + r,
        x0, y0,
    ]
    canvas.create_polygon(points, fill=fill, outline=outline, width=width, smooth=True)


_ARC_STEPS = 8  # segments per quarter circle; under 0.2 px from a true circle for r <= 12


def arc_rect_points(x0: float, y0: float, x1: float, y1: float, r: float) -> List[float]:
    """Flat [x, y, ...] outline of a rounded rect with circular radius-r corners, clockwise from
    the top-left corner's top end. r must already be clamped to half the smaller side."""
    corners = (
        (x1 - r, y0 + r, -90.0),  # top-right: centre, start angle (degrees, y down)
        (x1 - r, y1 - r, 0.0),  # bottom-right
        (x0 + r, y1 - r, 90.0),  # bottom-left
        (x0 + r, y0 + r, 180.0),  # top-left
    )
    points: List[float] = []
    for cx, cy, start in corners:
        for i in range(_ARC_STEPS + 1):
            a = math.radians(start + 90.0 * i / _ARC_STEPS)
            points.extend((cx + r * math.cos(a), cy + r * math.sin(a)))
    return points


def text(
    ctx: DrawContext,
    x: float,
    y: float,
    s: str,
    role: str,
    font: tkfont.Font,
    *,
    anchor: str = "nw",
    alpha: Optional[float] = None,
) -> int:
    """Draw s and return its measured width."""
    ctx.canvas.create_text(x, y, text=s, anchor=anchor, font=font, fill=ctx.color(role, alpha=alpha))
    return font.measure(s)


def measure_segments(fonts: Fonts, segs: List[Seg]) -> int:
    total = 0
    for seg in segs:
        total += fonts.get(seg.bold).measure(seg.text)
    return total


def draw_segments(ctx: DrawContext, x: float, y: float, segs: List[Seg], alpha: float = 1.0) -> int:
    """Draw consecutive create_text items left to right; return total width advanced."""
    cur_x = x
    for seg in segs:
        font = ctx.fonts.get(seg.bold)
        ctx.canvas.create_text(cur_x, y, text=seg.text, anchor="nw", font=font, fill=ctx.color(seg.role, mult=alpha))
        cur_x += font.measure(seg.text)
    return int(cur_x - x)


def oval(ctx: DrawContext, cx: float, cy: float, radius: float, fill: str) -> None:
    ctx.canvas.create_oval(cx - radius, cy - radius, cx + radius, cy + radius, fill=fill, outline="")
