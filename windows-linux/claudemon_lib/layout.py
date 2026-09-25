"""Rects, scaled metrics and pure geometry helpers shared by charts.py and view.py.

May rely on model.py only; rounds inline with math.floor(x + 0.5) (patterns.md #10), no import
of formatting.py. claudemon.swift 348-351, 445-450, 606-642.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .model import Snapshot, Span, TimeRange

Rect = Tuple[float, float, float, float]


def contains(r: Rect, x: float, y: float) -> bool:
    """True if (x, y) is in r, inclusive of x0/y0, exclusive of x1/y1."""
    x0, y0, x1, y1 = r
    return x0 <= x < x1 and y0 <= y < y1


def inset(r: Rect, dx: float, dy: float) -> Rect:
    """Shrink r by dx on each side horizontally and dy vertically; negative values grow it."""
    x0, y0, x1, y1 = r
    return (x0 + dx, y0 + dy, x1 - dx, y1 - dy)


@dataclass(frozen=True)
class Metrics:
    """ui-spec.md §2 metrics, already scaled to ints for the current DPI."""

    scale: float
    pad_w: int
    pad_h: int
    title_h: int
    gap: int
    header_h: int
    sub_h: int
    plot_h: int
    axis_h: int
    lane_h: int
    lane_gap: int
    max_lanes: int
    cell_h: int
    cell_gap: int
    min_width: int
    corner_r: int
    heat_label_w: int
    seg_extra_w: int
    seg_h: int
    close_hit_w: int
    dot_x: int

    @classmethod
    def for_scale(cls, scale: float) -> "Metrics":
        def px(v: float) -> int:
            return int(math.floor(v * scale + 0.5))

        return cls(
            scale=scale,
            pad_w=px(14),
            pad_h=px(10),
            title_h=px(24),
            gap=px(12),
            header_h=px(20),
            sub_h=px(14),
            plot_h=px(56),
            axis_h=px(14),
            lane_h=px(7),
            lane_gap=px(3),
            max_lanes=6,
            cell_h=px(10),
            cell_gap=px(2),
            min_width=px(400),
            corner_r=px(9),
            heat_label_w=px(30),
            seg_extra_w=px(14),
            seg_h=px(16),
            close_hit_w=px(22),
            dot_x=px(10),
        )

    def px(self, v: float) -> int:
        return int(math.floor(v * self.scale + 0.5))


def pack_lanes(spans: List[Span], max_lanes: int = 6) -> List[Tuple[Span, int]]:
    """data-flow.md §7 / claudemon.swift 606-620. spans must already be sorted by start."""
    ends: List[float] = []
    out: List[Tuple[Span, int]] = []
    for s in spans:
        free = next((k for k, e in enumerate(ends) if e < s.start), None)
        if free is not None:
            ends[free] = s.end
            out.append((s, free))
        elif len(ends) < max_lanes:
            ends.append(s.end)
            out.append((s, len(ends) - 1))
        else:
            k = min(range(len(ends)), key=lambda k: ends[k])
            ends[k] = max(ends[k], s.end)
            out.append((s, k))
    return out


def lane_count(packed: List[Tuple[Span, int]]) -> int:
    """Max lane + 1, or 0 if packed is empty."""
    if not packed:
        return 0
    return max(lane for _, lane in packed) + 1


def chart_block_height(m: Metrics, range: TimeRange) -> int:
    """ui-spec.md §3: gap + header_h + (heatmap body if WEEK else line-chart body)."""
    if range == TimeRange.WEEK:
        body = 7 * (m.cell_h + m.cell_gap) + m.axis_h
    else:
        body = m.sub_h + m.plot_h + m.axis_h
    return m.gap + m.header_h + body


def timeline_height(m: Metrics, n_lanes: int) -> int:
    """ui-spec.md §3: gap + header_h + n_lanes*(lane_h + lane_gap)."""
    return m.gap + m.header_h + n_lanes * (m.lane_h + m.lane_gap)


def chart_index(
    mouse: Optional[Tuple[float, float]],
    chart_rect: Optional[Rect],
    n: int,
    grow_x: float = 6,
    grow_y: float = 8,
) -> Optional[int]:
    """Bucket index under the mouse, or None. components.md layout.py; the view passes m.px(6), m.px(8)."""
    if mouse is None or chart_rect is None or n <= 1:
        return None
    mx, my = mouse
    grown = inset(chart_rect, -grow_x, -grow_y)
    if not contains(grown, mx, my):
        return None
    x0, _, x1, _ = chart_rect
    width = x1 - x0
    if width == 0:
        return 0
    i = math.floor((mx - x0) / width * (n - 1) + 0.5)
    return max(0, min(n - 1, int(i)))


def x_for(t: float, now: float, range: TimeRange, x0: float, x1: float) -> float:
    """ui-spec.md §6 Timeline: x_for(t) clamped to [x0, x1] over the last range.seconds."""
    width = x1 - x0
    frac = (t - (now - range.seconds)) / range.seconds
    frac = max(0.0, min(1.0, frac))
    return x0 + width * frac


def show_timeline(snap: Snapshot, range: TimeRange) -> bool:
    """range != WEEK and snap.spans is non-empty."""
    return range != TimeRange.WEEK and bool(snap.spans)


def has_agent_series(snap: Snapshot) -> bool:
    return any(v > 0 for v in snap.agent_series)


def running_agents(snap: Snapshot) -> int:
    """Count of running spans in snap.agents_today."""
    return sum(1 for a in snap.agents_today if a.running)
