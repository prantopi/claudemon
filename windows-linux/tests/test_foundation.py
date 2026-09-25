"""Foundation tests: model.family/project_name/TimeRange, theme.resolve/hex table,
layout.pack_lanes/chart_index/x_for. See components.md S0 row.
"""
from __future__ import annotations

import unittest

import support  # noqa: F401

from claudemon_lib import layout, model, theme
from claudemon_lib.model import Span, TimeRange


class TestFamily(unittest.TestCase):
    def test_known_families(self) -> None:
        self.assertEqual(model.family("claude-opus-4"), "opus")
        self.assertEqual(model.family("claude-3-5-sonnet-20241022"), "sonnet")
        self.assertEqual(model.family("claude-haiku-3"), "haiku")
        self.assertEqual(model.family("fable-1"), "fable")

    def test_unknown_family(self) -> None:
        self.assertEqual(model.family("gpt-4"), "other")


class TestProjectName(unittest.TestCase):
    def test_plain_path(self) -> None:
        self.assertEqual(model.project_name("/home/u/code/app"), "app")

    def test_worktree_posix(self) -> None:
        self.assertEqual(model.project_name("/home/u/code/app/.claude/worktrees/feat-x"), "app")

    def test_worktree_windows(self) -> None:
        self.assertEqual(model.project_name(r"C:\Users\u\app\.claude\worktrees\w1"), "app")

    def test_trailing_slash(self) -> None:
        self.assertEqual(model.project_name(r"C:\Users\u\app\\"), "app")

    def test_root(self) -> None:
        self.assertEqual(model.project_name("/"), "/")

    def test_empty(self) -> None:
        self.assertEqual(model.project_name(""), "")


class TestTimeRange(unittest.TestCase):
    def test_labels(self) -> None:
        self.assertEqual(TimeRange.HOUR.label, "1h")
        self.assertEqual(TimeRange.FIVE_HOURS.label, "5h")
        self.assertEqual(TimeRange.DAY.label, "24h")
        self.assertEqual(TimeRange.WEEK.label, "7d")

    def test_seconds(self) -> None:
        self.assertEqual(TimeRange.HOUR.seconds, 3600.0)
        self.assertEqual(TimeRange.FIVE_HOURS.seconds, 18000.0)
        self.assertEqual(TimeRange.DAY.seconds, 86400.0)
        self.assertEqual(TimeRange.WEEK.seconds, 604800.0)

    def test_buckets(self) -> None:
        self.assertEqual(TimeRange.HOUR.buckets, 60)
        self.assertEqual(TimeRange.FIVE_HOURS.buckets, 60)
        self.assertEqual(TimeRange.DAY.buckets, 96)
        self.assertEqual(TimeRange.WEEK.buckets, 0)

    def test_axis(self) -> None:
        self.assertEqual(TimeRange.HOUR.axis, ("60m", "30m", "now"))
        self.assertEqual(TimeRange.FIVE_HOURS.axis, ("5h", "2.5h", "now"))
        self.assertEqual(TimeRange.DAY.axis, ("24h", "12h", "now"))
        self.assertEqual(TimeRange.WEEK.axis, ())

    def test_from_label(self) -> None:
        self.assertEqual(TimeRange.from_label("1h"), TimeRange.HOUR)
        self.assertEqual(TimeRange.from_label("7d"), TimeRange.WEEK)
        self.assertIsNone(TimeRange.from_label("bogus"))


class TestThemeHexTable(unittest.TestCase):
    def test_terminal(self) -> None:
        self.assertEqual(theme.to_hex(theme.TERMINAL.bg[:3]), "#090b0f")
        self.assertEqual(theme.to_hex(theme.TERMINAL.border[:3]), "#d97857")
        self.assertEqual(theme.to_hex(theme.TERMINAL.fg[:3]), "#8cff9e")
        self.assertEqual(theme.to_hex(theme.TERMINAL.agents[:3]), "#ff9e45")
        self.assertEqual(theme.to_hex(theme.TERMINAL.tooltip_bg[:3]), "#1a1f26")

    def test_claude(self) -> None:
        self.assertEqual(theme.to_hex(theme.CLAUDE.bg[:3]), "#1a1816")
        self.assertEqual(theme.to_hex(theme.CLAUDE.fg[:3]), "#d97857")
        self.assertEqual(theme.to_hex(theme.CLAUDE.tooltip_bg[:3]), "#292624")

    def test_light(self) -> None:
        self.assertEqual(theme.to_hex(theme.LIGHT.bg[:3]), "#f8f9f7")
        self.assertEqual(theme.to_hex(theme.LIGHT.fg[:3]), "#1f8c4f")
        self.assertEqual(theme.to_hex(theme.LIGHT.tooltip_bg[:3]), "#ffffff")

    def test_green_dot(self) -> None:
        self.assertEqual(theme.to_hex(theme.GREEN_DOT[:3]), "#59cc73")


class TestThemeResolve(unittest.TestCase):
    def _blend(self, palette: theme.Palette, role: str, alpha=None, mult: float = 1.0) -> str:
        rgba = theme.GREEN_DOT if role == "green_dot" else getattr(palette, role)
        r, g, b, a = rgba
        if alpha is not None:
            a = alpha
        a *= mult
        bg_r, bg_g, bg_b, _ = palette.bg
        return theme.to_hex((a * r + (1 - a) * bg_r, a * g + (1 - a) * bg_g, a * b + (1 - a) * bg_b))

    def test_bg_ignores_alpha(self) -> None:
        self.assertEqual(theme.resolve(theme.TERMINAL, "bg"), theme.to_hex(theme.TERMINAL.bg[:3]))

    def test_border_blends_over_bg(self) -> None:
        self.assertEqual(theme.resolve(theme.TERMINAL, "border"), self._blend(theme.TERMINAL, "border"))

    def test_alpha_replaces(self) -> None:
        full = theme.resolve(theme.TERMINAL, "border", alpha=1.0)
        self.assertEqual(full, theme.to_hex(theme.TERMINAL.border[:3]))

    def test_mult_multiplies(self) -> None:
        half = theme.resolve(theme.TERMINAL, "fg", mult=0.5)
        self.assertEqual(half, self._blend(theme.TERMINAL, "fg", mult=0.5))

    def test_green_dot_role(self) -> None:
        self.assertEqual(
            theme.resolve(theme.TERMINAL, "green_dot", alpha=1.0),
            theme.to_hex(theme.GREEN_DOT[:3]),
        )

    def test_model_role(self) -> None:
        self.assertEqual(theme.model_role("opus"), "purple")
        self.assertEqual(theme.model_role("sonnet"), "fg")
        self.assertEqual(theme.model_role("haiku"), "cyan")
        self.assertEqual(theme.model_role("fable"), "warn")
        self.assertEqual(theme.model_role("other"), "dim")

    def test_palette_for(self) -> None:
        self.assertIs(theme.palette_for(theme.ThemeChoice.TERMINAL, True), theme.TERMINAL)
        self.assertIs(theme.palette_for(theme.ThemeChoice.CLAUDE, False), theme.CLAUDE)
        self.assertIs(theme.palette_for(theme.ThemeChoice.SYSTEM, True), theme.TERMINAL)
        self.assertIs(theme.palette_for(theme.ThemeChoice.SYSTEM, False), theme.LIGHT)

    def test_theme_choice(self) -> None:
        self.assertEqual(theme.ThemeChoice.TERMINAL.key, "terminal")
        self.assertEqual(theme.ThemeChoice.CLAUDE.title, "Claude")
        self.assertEqual(theme.ThemeChoice.SYSTEM.title, "Match System")
        self.assertEqual(theme.ThemeChoice.from_key("claude"), theme.ThemeChoice.CLAUDE)
        self.assertIsNone(theme.ThemeChoice.from_key("bogus"))


def _span(id_: str, start: float, end: float) -> Span:
    return Span(id=id_, type="agent", task="", family="opus", start=start, end=end, tokens=0, running=False)


class TestPackLanes(unittest.TestCase):
    def test_non_overlapping_share_lane(self) -> None:
        # pack_lanes frees a lane only once its end is strictly before the next span's start.
        packed = layout.pack_lanes([_span("a", 0, 10), _span("b", 11, 20)])
        self.assertEqual([lane for _, lane in packed], [0, 0])

    def test_overlapping_get_new_lane(self) -> None:
        packed = layout.pack_lanes([_span("a", 0, 10), _span("b", 5, 15)])
        self.assertEqual([lane for _, lane in packed], [0, 1])

    def test_max_lanes_reuses_earliest_free_lane(self) -> None:
        spans = [_span(str(i), i, i + 100) for i in range(8)]
        packed = layout.pack_lanes(spans, max_lanes=2)
        self.assertEqual(max(lane for _, lane in packed), 1)

    def test_lane_count(self) -> None:
        packed = layout.pack_lanes([_span("a", 0, 10), _span("b", 5, 15)])
        self.assertEqual(layout.lane_count(packed), 2)
        self.assertEqual(layout.lane_count([]), 0)


class TestChartIndex(unittest.TestCase):
    def test_none_without_mouse(self) -> None:
        self.assertIsNone(layout.chart_index(None, (0, 0, 100, 50), 10))

    def test_none_without_chart_rect(self) -> None:
        self.assertIsNone(layout.chart_index((10, 10), None, 10))

    def test_none_for_single_point(self) -> None:
        self.assertIsNone(layout.chart_index((50, 25), (0, 0, 100, 50), 1))

    def test_none_when_outside_grown_rect(self) -> None:
        self.assertIsNone(layout.chart_index((-100, 25), (0, 0, 100, 50), 11, grow_x=0, grow_y=0))

    def test_middle_index(self) -> None:
        self.assertEqual(layout.chart_index((50, 25), (0, 0, 100, 50), 11), 5)

    def test_first_index(self) -> None:
        self.assertEqual(layout.chart_index((0, 25), (0, 0, 100, 50), 11), 0)

    def test_last_index(self) -> None:
        self.assertEqual(layout.chart_index((100, 25), (0, 0, 100, 50), 11), 10)


class TestXFor(unittest.TestCase):
    def test_start_of_range(self) -> None:
        now = 1000.0
        x = layout.x_for(now - TimeRange.HOUR.seconds, now, TimeRange.HOUR, 0.0, 100.0)
        self.assertAlmostEqual(x, 0.0)

    def test_end_of_range(self) -> None:
        now = 1000.0
        x = layout.x_for(now, now, TimeRange.HOUR, 0.0, 100.0)
        self.assertAlmostEqual(x, 100.0)

    def test_midpoint(self) -> None:
        now = 1000.0
        t = now - TimeRange.HOUR.seconds / 2
        x = layout.x_for(t, now, TimeRange.HOUR, 0.0, 100.0)
        self.assertAlmostEqual(x, 50.0)

    def test_clamped_before_range(self) -> None:
        now = 1000.0
        x = layout.x_for(now - 10 * TimeRange.HOUR.seconds, now, TimeRange.HOUR, 0.0, 100.0)
        self.assertAlmostEqual(x, 0.0)

    def test_clamped_after_range(self) -> None:
        now = 1000.0
        x = layout.x_for(now + 100, now, TimeRange.HOUR, 0.0, 100.0)
        self.assertAlmostEqual(x, 100.0)


if __name__ == "__main__":
    unittest.main()
