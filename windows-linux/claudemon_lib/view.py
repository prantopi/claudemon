"""MonitorView: layout, full draw, hit testing and tooltips for the monitor window. [tk]

ui-spec.md §3-§7, patterns.md #7. claudemon.swift 622-700 (size + draw), 894-943 (hover, tooltip).
All text and numbers come from rows.py / charts.py; this module only places them on the canvas.
"""
from __future__ import annotations

import time
import tkinter as tk
from typing import List, Optional, Tuple

from . import charts, layout, rows
from .animation import Animator
from .drawing import DrawContext, draw_segments, measure_segments, round_rect
from .drawing import text as draw_text
from .fonts import Fonts
from .formatting import clock
from .layout import Metrics, Rect, contains
from .model import Snapshot, Span, TimeRange
from .theme import Palette, ThemeChoice, palette_for


class MonitorView:
    """Owns canvas drawing and hit-testing for one monitor window."""

    # Public state; App reads and writes these directly (components.md).
    snap: Snapshot
    range: TimeRange
    compact: bool
    spin_phase: bool
    reduce_motion: bool
    theme: ThemeChoice
    system_dark: bool
    mouse: Optional[Tuple[float, float]]
    anim: Animator

    def __init__(
        self,
        canvas: tk.Canvas,
        fonts: Fonts,
        metrics: Metrics,
        transparent_corners: bool,
        corner_bg: Optional[str] = None,
        true_radius: bool = False,
    ) -> None:
        """transparent_corners draws the card with corner_r rounded corners (ui-spec.md §4).
        corner_bg is then the canvas background left showing at the corners: drawing.KEY_COLOR on
        Windows, drawing.MAC_TRANSPARENT on aqua, or None to use the card's own bg colour (X11,
        where shape.py cuts the corners off the window instead). true_radius is passed to
        drawing.round_rect for the card, tint, glow and border (macOS/X11; Windows keeps the
        default so its corners are unchanged)."""
        self.canvas = canvas
        self.fonts = fonts
        self.m = metrics
        self.transparent_corners = transparent_corners
        self.corner_bg = corner_bg
        self.true_radius = true_radius

        self.snap = Snapshot()
        self.range = TimeRange.HOUR
        self.compact = False
        self.spin_phase = False
        self.reduce_motion = False
        self.theme = ThemeChoice.TERMINAL
        self.system_dark = True
        self.mouse = None
        self.anim = Animator()

        # Hit regions rebuilt by every draw (patterns.md #7).
        self._row_regions: List[Tuple[Rect, List[str]]] = []
        self._seg_regions: List[Tuple[Rect, TimeRange]] = []
        self._chart_rect: Optional[Rect] = None
        self._span_regions: List[Tuple[Rect, Span]] = []
        self._heat_regions: List[Tuple[Rect, int, int]] = []
        self._canvas_bg: Optional[str] = None

    # ------------------------------------------------------------------ state

    @property
    def palette(self) -> Palette:
        """theme.palette_for(self.theme, self.system_dark)."""
        return palette_for(self.theme, self.system_dark)

    def apply(self, snap: Snapshot, now: float) -> None:
        """Store snap and drive self.anim.apply(snap, now, self.reduce_motion)."""
        self.snap = snap
        self.anim.apply(snap, now, self.reduce_motion)

    def running_agents(self) -> int:
        """Count of running agents in the current snapshot."""
        return layout.running_agents(self.snap)

    # ----------------------------------------------------------------- layout

    @property
    def _row_h(self) -> int:
        # ui-spec.md §2: row_h = font linespace + px(3).
        return self.fonts.font.metrics("linespace") + self.m.px(3)

    def _lane_count(self) -> int:
        return layout.lane_count(layout.pack_lanes(self.snap.spans, self.m.max_lanes))

    def fitting_size(self) -> Tuple[int, int]:
        """(width, height) for the current mode (compact or full). ui-spec.md §3."""
        m = self.m
        dots_w = measure_segments(self.fonts, rows.TITLE_DOTS)
        if self.compact:
            # claudemon.swift 634-635
            summary = rows.compact_summary(self.snap, self.anim, self.spin_phase)
            w = m.dot_x + dots_w + m.px(12) + measure_segments(self.fonts, summary) + m.px(12)
            return (int(w), m.title_h)
        # claudemon.swift 636-641
        lines = rows.build_rows(self.snap, self.anim, time.time(), self.spin_phase)
        width = max([0] + [measure_segments(self.fonts, ln.segs) for ln in lines])
        height = m.title_h + m.pad_h + len(lines) * self._row_h + layout.chart_block_height(m, self.range) + m.pad_h
        if layout.show_timeline(self.snap, self.range):
            height += layout.timeline_height(m, self._lane_count())
        return (int(max(width + 2 * m.pad_w + m.px(8), m.min_width)), int(height))

    # ------------------------------------------------------------------- draw

    def draw(self, width: int, height: int) -> None:
        """canvas.delete("all") then a full redraw, rebuilding every hit region. patterns.md #7."""
        canvas = self.canvas
        canvas.delete("all")
        now = time.time()
        pal = self.palette
        m = self.m
        ctx = DrawContext(
            canvas=canvas, palette=pal, fonts=self.fonts, m=m, width=width, height=height,
            now=now, spin_phase=self.spin_phase, reduce_motion=self.reduce_motion,
        )

        bg = self.corner_bg if (self.transparent_corners and self.corner_bg) else ctx.color("bg")
        if bg != self._canvas_bg:
            canvas.configure(bg=bg)
            self._canvas_bg = bg

        self._row_regions = []
        self._seg_regions = []
        self._chart_rect = None
        self._span_regions = []
        self._heat_regions = []

        r = m.corner_r if self.transparent_corners else 0
        W, H = width, height

        # Card and title tint. claudemon.swift 646-653
        # Tk fills straddle the coordinates the same way outlines do (see _draw_border below),
        # so use the same W-1/H-1 rect as the border: otherwise a thin bg-coloured sliver can
        # show outside the border at the rounded corners.
        cx1, cy1 = W - 1, H - 1
        tr = self.true_radius
        round_rect(canvas, 0, 0, cx1, cy1, r, fill=ctx.color("bg"), true_radius=tr)
        tint = ctx.color("border", alpha=0.07)
        if r > 0:
            if H < m.title_h + 2 * r:
                # Compact (or any window short enough that title_h ~= H): a separate
                # title-height tint rect would have straight sides reaching y = title_h and
                # rounded corners sitting below the canvas, covering the card's own rounded
                # bottom corners with tint colour. Swift instead clips the tint to the card
                # shape (claudemon.swift 650-653); draw it with the card's own rounded rect
                # so it follows the same corners.
                round_rect(canvas, 0, 0, cx1, cy1, r, fill=tint, true_radius=tr)
            else:
                round_rect(canvas, 0, 0, cx1, m.title_h + r, r, fill=tint, true_radius=tr)
                canvas.create_rectangle(0, m.title_h, W, m.title_h + r, fill=ctx.color("bg"), outline="")
        else:
            canvas.create_rectangle(0, 0, W, m.title_h, fill=tint, outline="")

        # Title dots, vertically centered. claudemon.swift 666
        line_h = self.fonts.font.metrics("linespace")
        ty = (m.title_h - line_h) / 2
        dots_w = draw_segments(ctx, m.dot_x, ty, rows.TITLE_DOTS)
        after_dots = m.dot_x + dots_w + m.px(12)

        if self.compact:
            # claudemon.swift 667-671: summary only, no separator, clock, rows or tooltip.
            draw_segments(ctx, after_dots, ty, rows.compact_summary(self.snap, self.anim, self.spin_phase))
            self._draw_border(ctx)
            return

        # Separator, name, clock. claudemon.swift 672-677
        canvas.create_rectangle(0, m.title_h, W, m.title_h + max(1, m.px(1)),
                                fill=ctx.color("border", alpha=0.3), outline="")
        draw_text(ctx, after_dots, ty, "claudemon", "dim", self.fonts.font)
        draw_text(ctx, W - m.px(12), ty, clock(now), "dim", self.fonts.font, anchor="ne")

        # Rows. claudemon.swift 679-685
        row_h = self._row_h
        y = m.title_h + m.pad_h
        for line in rows.build_rows(self.snap, self.anim, now, self.spin_phase):
            draw_segments(ctx, m.pad_w, y, line.segs, line.alpha)
            if line.tip:
                self._row_regions.append(((0, y - 1, W, y - 1 + row_h), line.tip))
            y += row_h

        # Charts. claudemon.swift 687-699
        y += m.gap
        self._seg_regions = charts.draw_chart_header(ctx, y, self.range)
        y += m.header_h
        if self.range == TimeRange.WEEK:
            self._heat_regions = charts.draw_heatmap(ctx, y, self.snap)
        else:
            # The chart rect is fixed geometry, so the hover index can be computed before drawing.
            expected: Rect = (m.pad_w, y + m.sub_h, W - m.pad_w, y + m.sub_h + m.plot_h)
            hover = layout.chart_index(self.mouse, expected, len(self.snap.main), m.px(6), m.px(8))
            self._chart_rect = charts.draw_line_chart(ctx, y, self.snap, hover)
            y += m.sub_h + m.plot_h + m.axis_h
            if layout.show_timeline(self.snap, self.range):
                self._span_regions = charts.draw_timeline(
                    ctx, y + m.gap, self.snap, self.range, self._chart_rect,
                    lambda span_id: self.anim.fade(span_id, now),
                )

        # Border last so content never covers it (ui-spec.md §4), then the tooltip on top.
        self._draw_border(ctx)
        self._draw_tooltip(ctx)

    def _draw_border(self, ctx: DrawContext) -> None:
        """claudemon.swift 655-664, with the Reduce Motion override from claude/luna/decisions.md
        (2026-09-24): under Reduce Motion no glow of any kind is drawn, so the border looks exactly
        as it does with no agents running."""
        m = self.m
        canvas = ctx.canvas
        r = m.corner_r if self.transparent_corners else 0
        # Tk outlines straddle the coordinates, so the last visible pixel is W-1 / H-1.
        x1, y1 = ctx.width - 1, ctx.height - 1
        glowing = self.running_agents() > 0 and not self.reduce_motion
        if glowing:
            g = m.px(2)
            gr = m.px(8) if r > 0 else 0
            round_rect(canvas, g, g, x1 - g, y1 - g, gr,
                       outline=ctx.color("agents", alpha=0.35 if self.spin_phase else 0.15), width=m.px(3),
                       true_radius=self.true_radius)
            outline = ctx.color("agents", alpha=0.9)
        else:
            outline = ctx.color("border")
        round_rect(canvas, 0, 0, x1, y1, r, outline=outline, width=max(1, m.px(1)), true_radius=self.true_radius)

    # ---------------------------------------------------------------- tooltip

    def _tooltip_lines(self, now: float) -> Optional[List[str]]:
        """claudemon.swift 902-925: chart index, then last span, then first heat cell, then first row."""
        mouse = self.mouse
        if mouse is None:
            return None
        mx, my = mouse
        m = self.m
        if self.range != TimeRange.WEEK:
            i = layout.chart_index(mouse, self._chart_rect, len(self.snap.main), m.px(6), m.px(8))
            if i is not None:
                return rows.chart_tooltip(self.snap, i, now)
        for rect, span in reversed(self._span_regions):
            if contains(rect, mx, my):
                return rows.span_tooltip(span, now)
        for rect, d, h in self._heat_regions:
            if contains(rect, mx, my):
                if d < len(self.snap.heat_days):
                    return rows.heat_tooltip(self.snap, d, h)
                break
        for rect, tip in self._row_regions:
            if contains(rect, mx, my):
                return tip
        return None

    def _draw_tooltip(self, ctx: DrawContext) -> None:
        """claudemon.swift 927-943. ui-spec.md §7."""
        if self.mouse is None:
            return
        lines = self._tooltip_lines(ctx.now)
        if not lines:
            return
        m = self.m
        mx, my = self.mouse
        f_first, f_rest = self.fonts.small_bold, self.fonts.small
        lh = f_rest.metrics("linespace")
        text_w = max((f_first if i == 0 else f_rest).measure(s) for i, s in enumerate(lines))
        text_h = lh * len(lines)
        bw, bh = text_w + m.px(14), text_h + m.px(8)
        x, y = mx + m.px(12), my + m.px(14)
        if x + bw > ctx.width - m.px(4):
            x = max(m.px(4), mx - m.px(12) - bw)
        if y + bh > ctx.height - m.px(4):
            y = max(m.title_h + m.px(2), my - m.px(10) - bh)
        round_rect(ctx.canvas, x, y, x + bw, y + bh, m.px(5),
                   fill=ctx.color("tooltip_bg"), outline=ctx.color("border", alpha=0.6), width=max(1, m.px(1)))
        ty = y + m.px(4)
        for i, s in enumerate(lines):
            draw_text(ctx, x + m.px(7), ty, s, "text" if i == 0 else "dim", f_first if i == 0 else f_rest)
            ty += lh

    # -------------------------------------------------------------------- hit

    def hit(self, x: float, y: float) -> Tuple[str, Optional[TimeRange]]:
        """("close", None) | ("range", r) | ("title", None) | ("body", None), from the last draw's regions.

        ui-spec.md §9 / claudemon.swift 964-980.
        """
        m = self.m
        if y < m.title_h and x < m.close_hit_w:
            return ("close", None)
        if not self.compact:
            for rect, r in self._seg_regions:
                if contains(rect, x, y):
                    return ("range", r)
        if y < m.title_h:
            return ("title", None)
        return ("body", None)
