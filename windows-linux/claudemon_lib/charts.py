"""Chart header, line chart, heatmap and agent timeline drawing. See ui-spec.md §6. [tk]

Each function draws into ctx.canvas and returns hit regions; no state. claudemon.swift 690-890.
"""
from __future__ import annotations

import math
from typing import Callable, List, Optional, Tuple

from . import formatting, theme
from .drawing import DrawContext, oval, round_rect
from .drawing import text as draw_text
from .layout import Rect, has_agent_series, inset, pack_lanes, x_for
from .model import Snapshot, Span, TimeRange


def draw_chart_header(ctx: DrawContext, top: float, range: TimeRange) -> List[Tuple[Rect, TimeRange]]:
    """Draw the chart title and the 1h/5h/24h/7d segmented control; return clickable (rect, range).

    claudemon.swift 706-732.
    """
    m = ctx.m
    left = m.pad_w
    right = ctx.width - m.pad_w
    title = "Activity, last 7 days" if range == TimeRange.WEEK else "Tokens per minute"
    draw_text(ctx, left, top + m.px(2), title, "text", ctx.fonts.font)

    labels = [r.label for r in TimeRange]
    widths = [ctx.fonts.small_bold.measure(lbl) + m.seg_extra_w for lbl in labels]
    total = sum(widths)
    box: Rect = (right - total, top + m.px(1), right, top + m.px(1) + m.seg_h)
    round_rect(ctx.canvas, box[0], box[1], box[2], box[3], m.px(5), fill=ctx.color("dim", alpha=0.12))

    regions: List[Tuple[Rect, TimeRange]] = []
    x = box[0]
    for r, w in zip(TimeRange, widths):
        seg: Rect = (x, box[1], x + w, box[3])
        if r == range:
            inner = inset(seg, m.px(1), m.px(1))
            round_rect(ctx.canvas, inner[0], inner[1], inner[2], inner[3], m.px(4), fill=ctx.color("label", alpha=0.28))
        cx = (seg[0] + seg[2]) / 2
        cy = (seg[1] + seg[3]) / 2
        role = "text" if r == range else "dim"
        ctx.canvas.create_text(cx, cy, text=r.label, anchor="center", font=ctx.fonts.small_bold, fill=ctx.color(role))
        regions.append((seg, r))
        x += w
    return regions


def draw_line_chart(ctx: DrawContext, top: float, snap: Snapshot, hover: Optional[int]) -> Rect:
    """Draw the tokens/min line chart (legend, grid, gradient area, hover dot); return chart_rect.

    claudemon.swift 734-825.
    """
    m = ctx.m
    left = m.pad_w
    width = ctx.width - 2 * m.pad_w
    main = snap.main
    agents = snap.agent_series
    peak = max([1.0] + list(main) + list(agents))
    has_agents = has_agent_series(snap)

    if has_agents:
        x = left
        for name, role in (("Main session", "fg"), ("Agents", "agents")):
            d = m.px(7)
            cx = x + d / 2
            cy = top + m.px(4) + d / 2
            oval(ctx, cx, cy, d / 2, ctx.color(role))
            w = draw_text(ctx, x + m.px(10), top, name, "dim", ctx.fonts.small)
            x += m.px(10) + w + m.px(12)

    s1 = "max "
    s2 = formatting.tokens(int(peak)) + "/min"
    w1 = ctx.fonts.small.measure(s1)
    w2 = ctx.fonts.small.measure(s2)
    sx = left + width - (w1 + w2)
    draw_text(ctx, sx, top, s1, "dim", ctx.fonts.small)
    draw_text(ctx, sx + w1, top, s2, "text", ctx.fonts.small)

    r: Rect = (left, top + m.sub_h, left + width, top + m.sub_h + m.plot_h)
    n = len(main)
    if n <= 1:
        return r

    inset4 = m.px(4)
    for f in (0.0, 0.5, 1.0):
        y = math.floor(r[1] + inset4 + (m.plot_h - inset4) * f + 0.5)
        alpha = 0.45 if f == 1.0 else 0.25
        kwargs = {"fill": ctx.color("dim", alpha=alpha), "width": m.px(1)}
        if f < 1.0:
            kwargs["dash"] = (m.px(2), m.px(3))
        ctx.canvas.create_line(r[0], y, r[2], y, **kwargs)

    def point(data: List[float], i: int) -> Tuple[float, float]:
        x = r[0] + width * i / (n - 1)
        y = r[3] - (m.plot_h - inset4) * (data[i] / peak)
        return (x, y)

    # Agents first, so the main session's line stays on top where they overlap.
    if has_agents:
        pts: List[float] = []
        for i in range(n):
            pts.extend(point(agents, i))
        ctx.canvas.create_line(*pts, fill=ctx.color("agents"), width=m.px(2), capstyle="round", joinstyle="round")

    main_pts = [point(main, i) for i in range(n)]
    bands = 8
    band_h = m.plot_h / bands
    for k in range(bands):
        a = r[1] + k * band_h
        b = r[1] + (k + 1) * band_h
        poly: List[float] = []
        for (x, y) in main_pts:
            poly.extend((x, min(max(y, a), b)))
        poly.extend((r[2], b))
        poly.extend((r[0], b))
        mid = (a + b) / 2
        alpha = 0.28 * (1 - (mid - r[1]) / m.plot_h)
        ctx.canvas.create_polygon(*poly, fill=ctx.color("fg", alpha=alpha), outline="")

    main_line: List[float] = []
    for (x, y) in main_pts:
        main_line.extend((x, y))
    ctx.canvas.create_line(*main_line, fill=ctx.color("fg"), width=m.px(2), capstyle="round", joinstyle="round")

    axis = snap.range.axis
    axis_y = r[3] + m.px(3)
    if len(axis) == 3:
        draw_text(ctx, r[0], axis_y, axis[0], "dim", ctx.fonts.small)
        w = ctx.fonts.small.measure(axis[1])
        draw_text(ctx, (r[0] + r[2]) / 2 - w / 2, axis_y, axis[1], "dim", ctx.fonts.small)
        w2 = ctx.fonts.small.measure(axis[2])
        draw_text(ctx, r[2] - w2, axis_y, axis[2], "dim", ctx.fonts.small)

    def dot(p: Tuple[float, float], role: str) -> None:
        oval(ctx, p[0], p[1], m.px(6), ctx.color("bg"))
        oval(ctx, p[0], p[1], m.px(4), ctx.color(role))

    if hover is not None and 0 <= hover < n:
        p = point(main, hover)
        cx = math.floor(p[0] + 0.5)
        ctx.canvas.create_line(cx, r[1], cx, r[3], fill=ctx.color("text", alpha=0.35), width=m.px(1))
        dot(p, "fg")
        if has_agents:
            dot(point(agents, hover), "agents")
    else:
        dot(point(main, n - 1), "fg")

    return r


def _day_abbr(d) -> str:
    """Tiny local helper: "%a" weekday abbreviation (formatting.day_name is not in charts.py's
    documented dependency list per components.md, which only grants formatting.tokens)."""
    return d.strftime("%a")


def draw_heatmap(ctx: DrawContext, top: float, snap: Snapshot) -> List[Tuple[Rect, int, int]]:
    """Draw the 7x24 activity heatmap; return (rect, day, hour) hit regions.

    claudemon.swift 864-890.
    """
    m = ctx.m
    x0 = m.pad_w + m.heat_label_w
    width = ctx.width - m.pad_w - x0
    cell_w = (width - 23 * m.cell_gap) / 24
    all_values = [v for row in snap.heat for v in row]
    maxv = max([1] + all_values)

    regions: List[Tuple[Rect, int, int]] = []
    for d, day in enumerate(snap.heat):
        y = top + d * (m.cell_h + m.cell_gap)
        if d < len(snap.heat_days):
            is_today = d == len(snap.heat) - 1
            label = "Today" if is_today else _day_abbr(snap.heat_days[d])
            draw_text(ctx, m.pad_w, y - m.px(1), label, "text" if is_today else "dim", ctx.fonts.small)
        for h, v in enumerate(day):
            cx0 = x0 + h * (cell_w + m.cell_gap)
            cell: Rect = (cx0, y, cx0 + cell_w, y + m.cell_h)
            if v == 0:
                fill = ctx.color("dim", alpha=0.14)
            else:
                fill = ctx.color("fg", alpha=0.22 + 0.78 * (v / maxv))
            round_rect(ctx.canvas, cell[0], cell[1], cell[2], cell[3], m.px(2), fill=fill)
            regions.append((inset(cell, -m.cell_gap / 2, -m.cell_gap / 2), d, h))

    axis_y = top + 7 * (m.cell_h + m.cell_gap) + m.px(1)
    for h in (0, 6, 12, 18):
        hx = x0 + h * (cell_w + m.cell_gap)
        draw_text(ctx, hx, axis_y, f"{h}:00", "dim", ctx.fonts.small)
    return regions


def draw_timeline(
    ctx: DrawContext,
    top: float,
    snap: Snapshot,
    range: TimeRange,
    chart_rect: Rect,
    fade: Callable[[str], float],
) -> List[Tuple[Rect, Span]]:
    """Draw the packed agent timeline bars and family legend; return (rect, span) hit regions.

    claudemon.swift 833-862.
    """
    m = ctx.m
    left = m.pad_w
    right = ctx.width - m.pad_w
    draw_text(ctx, left, top + m.px(2), "Agents", "text", ctx.fonts.font)

    families = sorted(set(s.family for s in snap.spans))
    x = right
    for fam in reversed(families):
        w = ctx.fonts.small.measure(fam)
        x -= w
        draw_text(ctx, x, top + m.px(3), fam, "dim", ctx.fonts.small)
        x -= m.px(10)
        d = m.px(7)
        oval(ctx, x + d / 2, top + m.px(7) + d / 2, d / 2, ctx.color(theme.model_role(fam)))
        x -= m.px(12)

    x0_line, x1_line = chart_rect[0], chart_rect[2]
    lane_top = top + m.header_h
    regions: List[Tuple[Rect, Span]] = []
    for span, lane in pack_lanes(snap.spans, m.max_lanes):
        bx0 = x_for(span.start, ctx.now, range, x0_line, x1_line)
        bx1 = max(bx0 + m.px(3), x_for(span.end, ctx.now, range, x0_line, x1_line))
        by0 = lane_top + lane * (m.lane_h + m.lane_gap)
        by1 = by0 + m.lane_h
        base = (1.0 if ctx.spin_phase else 0.7) if (span.running and not ctx.reduce_motion) else 0.9
        alpha = base * fade(span.id)
        round_rect(ctx.canvas, bx0, by0, bx1, by1, m.px(3), fill=ctx.color(theme.model_role(span.family), alpha=alpha))
        regions.append((inset((bx0, by0, bx1, by1), -m.px(2), -m.px(2)), span))
    return regions
