"""Tests for claudemon_lib.rows. See components.md P3 row, ui-spec.md §5/§7."""
from __future__ import annotations

import datetime
import unittest

import support  # noqa: F401

from claudemon_lib.animation import Animator
from claudemon_lib.model import ModelTotal, ProjectTotal, Snapshot, Span, Usage
from claudemon_lib.rows import (
    SPARKS,
    build_rows,
    chart_tooltip,
    compact_summary,
    heat_tooltip,
    span_tooltip,
)


def _seg_text(line) -> str:
    return "".join(seg.text for seg in line.segs)


def _running_span(id_: str, task: str, start: float, tokens: int = 500) -> Span:
    return Span(id=id_, type="agent", task=task, family="claude-opus-4", start=start, end=0.0, tokens=tokens, running=True)


def _done_span(id_: str, task: str, start: float, end: float, tokens: int = 300) -> Span:
    return Span(id=id_, type="review", task=task, family="claude-3-5-sonnet", start=start, end=end, tokens=tokens, running=False)


class TestNotLoaded(unittest.TestCase):
    def test_single_dim_row_no_tip(self) -> None:
        rows = build_rows(Snapshot(loaded=False), Animator(), now=0.0, spin_phase=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].segs[0].text, "Reading Claude Code logs…")
        self.assertEqual(rows[0].segs[0].role, "dim")
        self.assertEqual(rows[0].tip, [])


class TestEmptySnapshot(unittest.TestCase):
    def test_six_rows_only_when_nothing_to_show(self) -> None:
        snap = Snapshot(loaded=True)
        rows = build_rows(snap, Animator(), now=1000.0, spin_phase=True)
        # Sessions, Tokens, in/out, Speed, Models, Agents; no agent rows, no Projects row.
        self.assertEqual(len(rows), 6)
        self.assertIn("no replies today", _seg_text(rows[4]))

    def test_sessions_row_no_busy_no_ctx(self) -> None:
        snap = Snapshot(loaded=True, live_sessions=0, busy_sessions=0, context=0)
        rows = build_rows(snap, Animator(), now=1000.0, spin_phase=True)
        text = _seg_text(rows[0])
        self.assertNotIn("busy", text)
        self.assertNotIn("ctx", text)
        self.assertEqual(rows[0].tip[-1], "No conversation yet")

    def test_tokens_row_no_window_reset(self) -> None:
        snap = Snapshot(loaded=True)
        rows = build_rows(snap, Animator(), now=1000.0, spin_phase=True)
        self.assertNotIn("resets", _seg_text(rows[1]))
        self.assertEqual(rows[1].tip[-1], "No active 5h window")


class TestTypicalSnapshot(unittest.TestCase):
    def _snap(self) -> Snapshot:
        return Snapshot(
            loaded=True,
            live_sessions=3,
            busy_sessions=1,
            context=1500,
            context_project="my-project",
            today=Usage(input=1000, cache_write=200, cache_read=300, output=500),
            today_replies=7,
            window=2000,
            window_start=100.0,
            window_reset=5000.0,
            per_minute=250,
            peak_per_minute=900,
            models=[
                ModelTotal(name="opus", tokens=8000, replies=4),
                ModelTotal(name="sonnet", tokens=1900, replies=3),
                ModelTotal(name="haiku", tokens=50, replies=1),
            ],
            agents_today=[
                _running_span("a1", "refactor auth", start=940.0),
                _done_span("a2", "write tests", start=800.0, end=900.0),
            ],
            projects=[
                ProjectTotal(name="alpha", tokens=5000),
                ProjectTotal(name="beta", tokens=3000),
                ProjectTotal(name="gamma", tokens=1000),
            ],
        )

    def test_row_count_and_order(self) -> None:
        rows = build_rows(self._snap(), Animator(), now=1000.0, spin_phase=True)
        # Sessions, Tokens, in/out, Speed, Models, Agents, 2 agent rows, Projects.
        self.assertEqual(len(rows), 9)

    def test_sessions_row_text(self) -> None:
        rows = build_rows(self._snap(), Animator(), now=1000.0, spin_phase=True)
        text = _seg_text(rows[0])
        self.assertTrue(text.startswith("Sessions".ljust(10)))
        self.assertIn("3", text)
        self.assertIn("1 busy", text)
        self.assertIn("ctx", text)
        self.assertIn("my-project", text)
        self.assertIn("Latest conversation: 1,500 tokens of context (my-project)", rows[0].tip)

    def test_tokens_row_text(self) -> None:
        rows = build_rows(self._snap(), Animator(), now=1000.0, spin_phase=True)
        text = _seg_text(rows[1])
        self.assertIn("today", text)
        self.assertIn("2.0k", text)  # tokens(2000) total
        self.assertIn("resets ~", text)

    def test_inout_row_text(self) -> None:
        rows = build_rows(self._snap(), Animator(), now=1000.0, spin_phase=True)
        text = _seg_text(rows[2])
        self.assertTrue(text.startswith(" " * 10))
        self.assertIn("in ", text)
        self.assertIn("out ", text)
        self.assertIn("cache ", text)

    def test_speed_row_uses_fg_when_positive(self) -> None:
        rows = build_rows(self._snap(), Animator(), now=1000.0, spin_phase=True)
        speed_seg = rows[3].segs[1]
        self.assertEqual(speed_seg.role, "fg")
        self.assertTrue(speed_seg.bold)

    def test_speed_row_uses_dim_when_zero(self) -> None:
        snap = self._snap()
        snap.per_minute = 0
        rows = build_rows(snap, Animator(), now=1000.0, spin_phase=True)
        speed_seg = rows[3].segs[1]
        self.assertEqual(speed_seg.role, "dim")

    def test_models_row_percentages(self) -> None:
        rows = build_rows(self._snap(), Animator(), now=1000.0, spin_phase=True)
        text = _seg_text(rows[4])
        self.assertIn("opus", text)
        self.assertIn("sonnet", text)
        self.assertIn("haiku", text)
        # haiku: 50 * 100 = 5000 < total (9950) -> "<1%" (ui-spec.md §5, claudemon.swift 558)
        self.assertIn("<1%", text)
        self.assertTrue(any("<1" in line for line in rows[4].tip))

    def test_agents_summary_row(self) -> None:
        rows = build_rows(self._snap(), Animator(), now=1000.0, spin_phase=True)
        text = _seg_text(rows[5])
        self.assertIn("1", text)  # 1 running
        self.assertIn("running", text)
        self.assertIn("2", text)  # 2 today
        running_seg = rows[5].segs[1]
        self.assertEqual(running_seg.role, "warn")

    def test_agent_row_running_spinner_and_role(self) -> None:
        rows_on = build_rows(self._snap(), Animator(), now=1000.0, spin_phase=True)
        rows_off = build_rows(self._snap(), Animator(), now=1000.0, spin_phase=False)
        running_row_on = rows_on[6]
        running_row_off = rows_off[6]
        self.assertEqual(running_row_on.segs[1].text, "◐ ")
        self.assertEqual(running_row_off.segs[1].text, "◓ ")
        self.assertEqual(running_row_on.segs[1].role, "warn")
        self.assertIn("Running for", running_row_on.tip[-1])

    def test_agent_row_done_checkmark_and_ran_prefix(self) -> None:
        rows = build_rows(self._snap(), Animator(), now=1000.0, spin_phase=True)
        done_row = rows[7]
        self.assertEqual(done_row.segs[1].text, "✓ ")
        self.assertEqual(done_row.segs[1].role, "fg")
        self.assertTrue(done_row.tip[-1].startswith("Ran "))

    def test_agent_row_tip_differs_from_span_tooltip_ran_prefix(self) -> None:
        span = _done_span("a2", "write tests", start=800.0, end=900.0)
        rows = build_rows(self._snap(), Animator(), now=1000.0, spin_phase=True)
        row_tip = rows[7].tip
        hover_tip = span_tooltip(span, now=1000.0)
        self.assertNotEqual(row_tip[-1], hover_tip[-1])
        self.assertTrue(row_tip[-1].startswith("Ran "))
        self.assertFalse(hover_tip[-1].startswith("Ran "))

    def test_agent_rows_limited_to_four(self) -> None:
        snap = self._snap()
        snap.agents_today = [
            _running_span(f"r{i}", f"task {i}", start=900.0 + i) for i in range(6)
        ]
        rows = build_rows(snap, Animator(), now=1000.0, spin_phase=True)
        # Sessions, Tokens, in/out, Speed, Models, Agents, 4 agent rows, Projects.
        self.assertEqual(len(rows), 6 + 4 + 1)

    def test_projects_row_top_two_and_tip_top_six(self) -> None:
        rows = build_rows(self._snap(), Animator(), now=1000.0, spin_phase=True)
        proj_row = rows[-1]
        text = _seg_text(proj_row)
        self.assertIn("alpha", text)
        self.assertIn("beta", text)
        self.assertNotIn("gamma", text)
        self.assertEqual(len(proj_row.tip), 1 + 3)  # header + 3 projects (fewer than 6)

    def test_no_projects_row_when_empty(self) -> None:
        snap = self._snap()
        snap.projects = []
        rows = build_rows(snap, Animator(), now=1000.0, spin_phase=True)
        self.assertEqual(len(rows), 8)  # one fewer than the full 9

    def test_build_rows_uses_animated_number_not_raw_snapshot(self) -> None:
        snap = self._snap()
        anim = Animator()
        anim.apply(snap, now=0.0, reduce_motion=False)  # seeds shown = target (today total = 2000)
        snap2 = self._snap()
        snap2.today = Usage(input=100000, cache_write=0, cache_read=0, output=0)  # huge new target
        anim.apply(snap2, now=1.0, reduce_motion=False)  # shown stays behind target
        rows = build_rows(snap2, anim, now=1.0, spin_phase=True)
        text = _seg_text(rows[1])
        # Raw target is 100000 ("100k"); eased shown value should still read the old total.
        self.assertNotIn("100k", text)


class TestCompactSummary(unittest.TestCase):
    def test_not_loaded(self) -> None:
        segs = compact_summary(Snapshot(loaded=False), Animator(), spin_phase=True)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].text, "loading…")
        self.assertEqual(segs[0].role, "dim")

    def test_sparkline_and_live_count(self) -> None:
        snap = Snapshot(loaded=True, per_minute=120, live_sessions=4, last_hour=[0, 2, 8, 4] * 5)
        segs = compact_summary(snap, Animator(), spin_phase=True)
        text = "".join(s.text for s in segs)
        self.assertIn("/min", text)
        self.assertIn("4 live", text)
        self.assertIn("·", text)  # at least one zero-value tick rendered as a dot
        self.assertTrue(any(c in SPARKS for c in text))

    def test_no_running_agents_no_suffix(self) -> None:
        snap = Snapshot(loaded=True, agents_today=[])
        segs = compact_summary(snap, Animator(), spin_phase=True)
        text = "".join(s.text for s in segs)
        self.assertNotIn("agent", text)

    def test_singular_agent_suffix(self) -> None:
        snap = Snapshot(loaded=True, agents_today=[_running_span("r1", "t", start=0.0)])
        segs = compact_summary(snap, Animator(), spin_phase=True)
        text = "".join(s.text for s in segs)
        self.assertIn("1 agent", text)
        self.assertNotIn("1 agents", text)

    def test_plural_agent_suffix_and_spinner(self) -> None:
        snap = Snapshot(
            loaded=True,
            agents_today=[_running_span("r1", "t", start=0.0), _running_span("r2", "t", start=0.0)],
        )
        segs_on = compact_summary(snap, Animator(), spin_phase=True)
        segs_off = compact_summary(snap, Animator(), spin_phase=False)
        text_on = "".join(s.text for s in segs_on)
        text_off = "".join(s.text for s in segs_off)
        self.assertIn("2 agents", text_on)
        self.assertIn("◐", text_on)
        self.assertIn("◓", text_off)


class TestChartTooltip(unittest.TestCase):
    def test_minute_bucket_shows_single_time(self) -> None:
        snap = Snapshot(main=[10.0, 20.0, 30.0], agent_series=[], bucket_seconds=60.0)
        lines = chart_tooltip(snap, i=2, now=1000.0)
        self.assertEqual(len(lines), 2)
        self.assertIn("Main session: 30/min", lines[1])

    def test_larger_bucket_shows_range(self) -> None:
        snap = Snapshot(main=[10.0, 20.0], agent_series=[], bucket_seconds=900.0)
        lines = chart_tooltip(snap, i=0, now=1000.0)
        self.assertIn("–", lines[0])

    def test_includes_agent_series_when_present(self) -> None:
        snap = Snapshot(main=[10.0, 20.0], agent_series=[5.0, 15.0], bucket_seconds=60.0)
        lines = chart_tooltip(snap, i=1, now=1000.0)
        self.assertEqual(len(lines), 3)
        self.assertIn("Agents: 15/min", lines[2])

    def test_omits_agent_series_when_all_zero(self) -> None:
        snap = Snapshot(main=[10.0, 20.0], agent_series=[0.0, 0.0], bucket_seconds=60.0)
        lines = chart_tooltip(snap, i=1, now=1000.0)
        self.assertEqual(len(lines), 2)


class TestSpanTooltip(unittest.TestCase):
    def test_running_span(self) -> None:
        span = _running_span("a1", "do stuff", start=940.0)
        lines = span_tooltip(span, now=1000.0)
        self.assertEqual(lines[0], "agent")
        self.assertEqual(lines[1], "do stuff")
        self.assertIn("claude-opus-4", lines[2])
        self.assertTrue(lines[3].startswith("Running for"))

    def test_finished_span_no_ran_prefix(self) -> None:
        span = _done_span("a2", "", start=800.0, end=900.0)
        lines = span_tooltip(span, now=1000.0)
        self.assertEqual(lines[1], "(no description)")
        self.assertFalse(lines[3].startswith("Ran "))
        self.assertIn("–", lines[3])


class TestHeatTooltip(unittest.TestCase):
    def test_formats_day_and_hour_range(self) -> None:
        d = datetime.date(2026, 9, 24)
        snap = Snapshot(heat_days=[d], heat=[[0] * 24])
        snap.heat[0][23] = 42
        lines = heat_tooltip(snap, d=0, h=23)
        self.assertIn("23:00–00:00", lines[0])
        self.assertIn("42 tokens", lines[1])


if __name__ == "__main__":
    unittest.main()
