"""Store.snapshot math (data-flow §5, claudemon.swift 178-285). Task P1."""
from __future__ import annotations

import datetime
import json
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional

import support  # noqa: F401
from support import assistant_line, local_ts, make_home, write_lines

from claudemon_lib.model import ModelTotal, ProjectTotal, Snapshot, TimeRange
from claudemon_lib.store import Store

NOW = local_ts(2026, 9, 24, 12)
H = 3600.0


def usage(i: int = 0, cw: int = 0, cr: int = 0, o: int = 0) -> dict:
    return {"input_tokens": i, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr,
            "output_tokens": o}


def tok(n: int) -> dict:
    return usage(o=n)


class SnapCase(unittest.TestCase):
    now = NOW

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = make_home(Path(self._tmp.name))
        self.proj = self.home / "projects" / "-home-u-app"
        self.n = 0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def reply(self, rel: str, t: float, n: int = 100, model: str = "claude-opus-4",
              cwd: Optional[str] = None, stop: Optional[str] = "end_turn",
              mtime: Optional[float] = None, u: Optional[dict] = None) -> str:
        self.n += 1
        path = self.proj / rel
        write_lines(path, [assistant_line("m%d" % self.n, t, model=model, usage=u or tok(n),
                                          stop_reason=stop, cwd=cwd)],
                    mtime=self.now - 3600 if mtime is None else mtime)
        return str(path)

    def snap(self, r: TimeRange = TimeRange.HOUR, now: Optional[float] = None,
             live: List[int] = (), sids: Optional[Dict[int, str]] = None) -> Snapshot:
        for pid in live:
            sid = (sids or {}).get(pid)
            (self.home / "sessions" / ("%d.json" % pid)).write_text(json.dumps({"sessionId": sid}) if sid else "{}")
        now = self.now if now is None else now
        store = Store(claude_home=self.home, pid_alive=lambda pid: pid in live)
        store.refresh(now=now, force_discover=True)
        return store.snapshot(r, now=now)


class TestTotals(SnapCase):
    def test_token_totals_today(self) -> None:
        self.reply("s.jsonl", NOW - 2 * H, u=usage(1, 20, 300, 4000))
        self.reply("s.jsonl", NOW - 1 * H, u=usage(5, 0, 0, 5))
        self.reply("s.jsonl", NOW - 13 * H, u=usage(0, 0, 0, 999))  # yesterday (NOW is local noon)
        s = self.snap()
        self.assertEqual((s.today.input, s.today.cache_write, s.today.cache_read, s.today.output),
                         (6, 20, 300, 4005))
        self.assertEqual((s.today.total, s.today_replies), (4331, 2))

    def test_per_model_totals(self) -> None:
        self.reply("s.jsonl", NOW - 60, 300, model="claude-opus-4-1")
        self.reply("s.jsonl", NOW - 50, 200, model="claude-opus-4")
        self.reply("s.jsonl", NOW - 40, 200, model="claude-sonnet-4-5")
        self.reply("s.jsonl", NOW - 30, 300, model="claude-sonnet-4-5")
        self.reply("s.jsonl", NOW - 20, 500, model="claude-haiku-4-5")
        self.reply("s.jsonl", NOW - 10, 7, model="gpt-x")
        self.reply("s.jsonl", NOW - 5, 999, model="<synthetic>")
        s = self.snap()
        self.assertEqual(s.models, [ModelTotal("haiku", 500, 1), ModelTotal("opus", 500, 2),
                                    ModelTotal("sonnet", 500, 2), ModelTotal("other", 7, 1)])

    def test_per_project_totals_with_worktree_rule(self) -> None:
        self.reply("a.jsonl", NOW - 60, 100, cwd="/home/u/code/app")
        self.reply("b.jsonl", NOW - 60, 50, cwd="/home/u/code/app/.claude/worktrees/feat-x")
        self.reply("c.jsonl", NOW - 60, 20, cwd=r"C:\Users\u\app\.claude\worktrees\w1")
        self.reply("d.jsonl", NOW - 60, 300, cwd="/home/u/code/web")
        self.reply("e.jsonl", NOW - 60, 170)  # no cwd
        self.reply("f.jsonl", NOW - 60, 170, cwd="/srv/api")
        s = self.snap()
        self.assertEqual(s.projects, [ProjectTotal("web", 300), ProjectTotal("app", 170),
                                      ProjectTotal("?", 170), ProjectTotal("api", 170)][:1]
                         + sorted([ProjectTotal("app", 170), ProjectTotal("?", 170), ProjectTotal("api", 170)],
                                  key=lambda p: p.name))


class TestWindow(SnapCase):
    def test_window_restarts_after_five_hours_from_start(self) -> None:
        for age, n in ((6 * H, 1), (4 * H, 10), (3 * H, 100), (0.5 * H, 1000), (0.1 * H, 10000)):
            self.reply("s.jsonl", NOW - age, n)
        s = self.snap()
        # 6h ago starts a window (expires 1h ago); the 4h/3h replies fall inside it; 30m ago starts a new one.
        self.assertEqual(s.window_start, NOW - 0.5 * H)
        self.assertEqual(s.window_reset, NOW + 4.5 * H)
        self.assertEqual(s.window, 11000)

    def test_window_is_not_gap_based(self) -> None:
        # No 5 h gap anywhere, yet the replies span 7 h. Swift 219-228: the window that starts 7h ago
        # ends 2h ago, so the first reply at or after that boundary (1.5h ago) starts a new window.
        # A gap-based rule would keep the 7h-ago start, which has expired, and report no window.
        for age in (7 * H, 5 * H, 3 * H, 1.5 * H, 0.1 * H):
            self.reply("s.jsonl", NOW - age, 10)
        s = self.snap()
        self.assertEqual(s.window_start, NOW - 1.5 * H)
        self.assertEqual(s.window_reset, NOW + 3.5 * H)
        self.assertEqual(s.window, 20)

    def test_window_does_not_restart_before_five_hours(self) -> None:
        for age in (4.5 * H, 3 * H, 1.5 * H, 0.1 * H):  # boundary is 4.5h ago + 5h = 0.5h from now
            self.reply("s.jsonl", NOW - age, 10)
        s = self.snap()
        self.assertEqual((s.window_start, s.window_reset, s.window), (NOW - 4.5 * H, NOW + 0.5 * H, 40))

    def test_window_active(self) -> None:
        self.reply("s.jsonl", NOW - 4 * H, 7)
        self.reply("s.jsonl", NOW - 1 * H, 3)
        s = self.snap()
        self.assertEqual((s.window_start, s.window_reset, s.window), (NOW - 4 * H, NOW + H, 10))

    def test_window_expired(self) -> None:
        self.reply("s.jsonl", NOW - 5 * H, 7)  # exactly 5 h old: now < ws + 5h is false
        s = self.snap()
        self.assertEqual((s.window_start, s.window_reset, s.window), (None, None, 0))

    def test_no_replies(self) -> None:
        s = self.snap()
        self.assertEqual((s.window_start, s.window), (None, 0))


class TestLastHour(SnapCase):
    def test_minutes_speed_peak(self) -> None:
        self.reply("s.jsonl", NOW - 30, 100)
        self.reply("s.jsonl", NOW - 45, 50)
        self.reply("s.jsonl", NOW - 90, 200)
        self.reply("s.jsonl", NOW - 299, 3)
        self.reply("s.jsonl", NOW - 300, 1000)  # not in the 5-minute speed window
        self.reply("s.jsonl", NOW - 3599, 7)
        self.reply("s.jsonl", NOW - 3600, 5000)  # out of the hour
        self.reply("s.jsonl", NOW + 30, 9000)  # future: ignored
        s = self.snap()
        self.assertEqual(len(s.last_hour), 60)
        self.assertEqual(s.last_hour[59], 150)
        self.assertEqual(s.last_hour[58], 200)
        self.assertEqual(s.last_hour[55], 3)
        self.assertEqual(s.last_hour[54], 1000)
        self.assertEqual(s.last_hour[0], 7)
        self.assertEqual(sum(s.last_hour), 1360)
        self.assertEqual(s.per_minute, (150 + 200 + 3) // 5)
        self.assertEqual(s.peak_per_minute, 1000)


class TestBuckets(SnapCase):
    def setUp(self) -> None:
        super().setUp()
        self.agent = "sess/subagents/agent-a.jsonl"

    def test_hour(self) -> None:
        self.reply("s.jsonl", NOW - 30, 120)
        self.reply("s.jsonl", NOW - 150, 60)
        self.reply(self.agent, NOW - 30, 40)
        self.reply("s.jsonl", NOW - 3599, 11)
        self.reply("s.jsonl", NOW - 3600, 999)
        s = self.snap(TimeRange.HOUR)
        self.assertEqual((s.bucket_seconds, len(s.main), len(s.agent_series)), (60.0, 60, 60))
        self.assertEqual((s.main[59], s.main[57], s.main[0]), (120.0, 60.0, 11.0))
        self.assertEqual(s.agent_series[59], 40.0)
        self.assertEqual(sum(s.main), 191.0)
        self.assertEqual(sum(s.agent_series), 40.0)

    def test_five_hours(self) -> None:
        self.reply("s.jsonl", NOW - 10, 500)
        self.reply("s.jsonl", NOW - 301, 1000)
        self.reply(self.agent, NOW - 4 * H, 50)
        s = self.snap(TimeRange.FIVE_HOURS)
        self.assertEqual((s.bucket_seconds, len(s.main)), (300.0, 60))
        self.assertEqual(s.main[59], 100.0)  # per minute: 500 / 5
        self.assertEqual(s.main[58], 200.0)
        self.assertEqual(s.agent_series[60 - 1 - 48], 10.0)

    def test_day(self) -> None:
        self.reply("s.jsonl", NOW - 1000, 1500)
        self.reply("s.jsonl", NOW - 23.9 * H, 30)
        self.reply("s.jsonl", NOW - 25 * H, 999)
        s = self.snap(TimeRange.DAY)
        self.assertEqual((s.bucket_seconds, len(s.main), len(s.agent_series)), (900.0, 96, 96))
        self.assertEqual(s.main[94], 100.0)  # 1500 / 15
        self.assertEqual(s.main[0], 2.0)
        self.assertEqual(sum(s.main), 102.0)

    def test_week_has_no_chart_or_spans(self) -> None:
        self.reply("s.jsonl", NOW - 60, 100)
        self.reply(self.agent, NOW - 60, 100, stop="tool_use", mtime=NOW - 5)
        s = self.snap(TimeRange.WEEK)
        self.assertEqual((s.main, s.agent_series, s.bucket_seconds, s.spans), ([], [], 60.0, []))
        self.assertEqual(len(s.agents_today), 1)
        self.assertEqual(s.range, TimeRange.WEEK)


class TestHeatmap(SnapCase):
    def test_cells_and_days(self) -> None:
        self.reply("s.jsonl", local_ts(2026, 9, 24, 10, 15), 5)
        self.reply("s.jsonl", local_ts(2026, 9, 24, 10, 45), 6)
        self.reply("s.jsonl", local_ts(2026, 9, 18, 23, 59), 7)   # oldest shown day
        self.reply("s.jsonl", local_ts(2026, 9, 17, 23, 59), 999)  # one day too old
        self.reply("s.jsonl", local_ts(2026, 9, 21, 0, 0), 8)
        s = self.snap(TimeRange.HOUR)
        self.assertEqual(s.heat_days, [datetime.date(2026, 9, 18) + datetime.timedelta(days=k) for k in range(7)])
        self.assertEqual(len(s.heat), 7)
        self.assertTrue(all(len(row) == 24 for row in s.heat))
        self.assertEqual(s.heat[6][10], 11)
        self.assertEqual(s.heat[0][23], 7)
        self.assertEqual(s.heat[3][0], 8)
        self.assertEqual(sum(map(sum, s.heat)), 26)

    def test_same_for_every_range(self) -> None:
        self.reply("s.jsonl", NOW - 30 * H, 5)
        heats = [self.snap(r).heat for r in TimeRange]
        self.assertTrue(all(h == heats[0] for h in heats))


class TestMidnight(SnapCase):
    now = local_ts(2026, 9, 24, 0, 30)

    def test_today_starts_at_local_midnight(self) -> None:
        self.reply("s.jsonl", local_ts(2026, 9, 23, 23, 50), 40)
        self.reply("s.jsonl", local_ts(2026, 9, 24, 0, 10), 2)
        s = self.snap()
        self.assertEqual((s.today.total, s.today_replies), (2, 1))
        self.assertEqual(s.heat[5][23], 40)
        self.assertEqual(s.heat[6][0], 2)
        self.assertEqual(sum(s.main), 42.0)  # the chart ignores the calendar


class TestAgentsAndSessions(SnapCase):
    def test_spans_running_and_order(self) -> None:
        run = self.reply("s1/subagents/agent-run.jsonl", NOW - 600, 10, stop="tool_use", mtime=NOW - 30)
        self.reply("s1/subagents/agent-run.jsonl", NOW - 60, 5, model="claude-sonnet-4", stop="tool_use", mtime=NOW - 30)
        (self.proj / "s1" / "subagents" / "agent-run.meta.json").write_text(
            '{"agentType": "Explore", "description": "Look around"}')
        done = self.reply("s1/subagents/agent-done.jsonl", NOW - 1800, 20, mtime=NOW - 30)
        self.reply("s1/subagents/agent-done.jsonl", NOW - 1200, 20, mtime=NOW - 30)
        stale = self.reply("s1/subagents/agent-stale.jsonl", NOW - 2 * H, 1, stop="tool_use", mtime=NOW - 91)
        old = self.reply("s1/subagents/agent-old.jsonl", NOW - 20 * H, 1, mtime=NOW - 20 * H)
        self.reply("s1.jsonl", NOW - 60, 1000)
        s = self.snap(TimeRange.HOUR)
        spans = {x.id: x for x in s.agents_today}
        self.assertEqual(set(spans), {run, done, stale})  # old one ended before today's midnight
        r = spans[run]
        self.assertTrue(r.running)
        self.assertEqual((r.type, r.task, r.family, r.start, r.end, r.tokens),
                         ("Explore", "Look around", "sonnet", NOW - 600, NOW, 15))
        d = spans[done]
        self.assertFalse(d.running)
        self.assertEqual((d.type, d.task, d.start, d.end, d.tokens), ("agent", "", NOW - 1800, NOW - 1200, 40))
        self.assertFalse(spans[stale].running)  # tool_use but file quiet for > 90 s
        self.assertEqual([x.id for x in s.agents_today], [run, done, stale])
        self.assertEqual([x.id for x in s.spans], [done, run])  # in range, by start
        self.assertEqual(len(self.snap(TimeRange.DAY).spans), 4)
        self.assertNotIn(old, [x.id for x in s.spans])

    def test_busy_sessions_and_context(self) -> None:
        self.reply("a.jsonl", NOW - 600, u=usage(1, 2, 3, 4), cwd="/p/alpha", mtime=NOW - 300)
        self.reply("b.jsonl", NOW - 30, u=usage(100, 20, 3000, 5), cwd="/p/beta", mtime=NOW - 10)
        self.reply("c.jsonl", NOW - 30, cwd="/p/gamma", mtime=NOW - 5)
        (self.proj / "c.jsonl").write_text('{"type": "user", "cwd": "/p/gamma"}\n')  # no reply at all
        import os
        os.utime(self.proj / "c.jsonl", (NOW - 5, NOW - 5))
        self.reply("x/subagents/agent-z.jsonl", NOW - 5, mtime=NOW - 1)  # agents never count as busy
        sids = {11: "a", 12: "b", 13: "c"}
        s = self.snap(live=[11, 12, 13], sids=sids)
        self.assertEqual(s.live_sessions, 3)
        # No status in the sessions json: the 20 s fallback on each live session's own transcript.
        self.assertEqual(s.busy_sessions, 2)  # b and c changed in the last 20 s
        self.assertEqual((s.context, s.context_project), (3120, "beta"))
        self.assertEqual(self.snap(live=[11], sids=sids).busy_sessions, 0)  # only live sessions count
        self.assertEqual(self.snap(live=[12, 14], sids=sids).busy_sessions, 1)  # 14: no sessionId

    def test_snapshot_metadata(self) -> None:
        s = self.snap(TimeRange.FIVE_HOURS)
        self.assertTrue(s.loaded)
        self.assertEqual((s.range, s.now), (TimeRange.FIVE_HOURS, NOW))


if __name__ == "__main__":
    unittest.main()
