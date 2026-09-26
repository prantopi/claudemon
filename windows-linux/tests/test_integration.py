"""End-to-end integration tests: a fake ~/.claude tree read by Store, snapshotted for every
TimeRange, and fed through rows.py's row/tooltip builders. See components.md Q1 row,
data-flow.md §2-§6, patterns.md #3/#11.
"""
from __future__ import annotations

import datetime
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from support import assistant_line, local_ts, make_home, write_lines

from claudemon_lib import formatting as fmt
from claudemon_lib.animation import Animator
from claudemon_lib.model import ModelTotal, ProjectTotal, TimeRange
from claudemon_lib.rows import build_rows, chart_tooltip, compact_summary, heat_tooltip, span_tooltip
from claudemon_lib.store import Store

NOW = local_ts(2026, 9, 24, 12)  # local noon: avoids midnight edges (patterns.md #11)
H = 3600.0


def usage(i: int = 0, cw: int = 0, cr: int = 0, o: int = 0) -> dict:
    return {"input_tokens": i, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr,
            "output_tokens": o}


def tok(n: int) -> dict:
    return usage(o=n)


class IntegrationCase(unittest.TestCase):
    """One fake ~/.claude tree covering: several projects (one reached via a worktree cwd), a
    subagent transcript with its .meta.json (one running agent), mixed models, replies spread
    across several days and hours, a duplicate message id, a half-written last line, an invalid
    JSON line, and a live session pid (via an injected pid_alive fake)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = make_home(Path(self._tmp.name))
        self._build_fixture()
        self.pid_alive = lambda pid: pid == 4242
        self.store = Store(claude_home=self.home, pid_alive=self.pid_alive)
        self.store.refresh(now=NOW, force_discover=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _build_fixture(self) -> None:
        projects = self.home / "projects"

        # ---- project "app": a main session with several replies, several days apart ----
        self.session1_path = projects / "-home-u-app" / "session1.jsonl"
        self.A2_t = NOW - 2 * H
        self.dup1a_t = NOW - 3 * H
        self.dup1b_t = NOW - 3 * H + 1  # same message id as dup1a: overwrites, counted once
        self.A4_t = NOW - 30 * H  # yesterday
        self.A0_t = NOW - 10 * H  # today, before the 5h window start
        self.A1_t = NOW - 600  # 10 min ago

        a2_line = assistant_line("a2", self.A2_t, model="claude-sonnet-4-5", usage=tok(200),
                                  cwd="/home/u/code/app")
        dup1a_line = assistant_line("dup1", self.dup1a_t, model="claude-sonnet-4-5", usage=tok(50),
                                     cwd="/home/u/code/app")
        dup1b_line = assistant_line("dup1", self.dup1b_t, model="claude-sonnet-4-5", usage=tok(999),
                                     cwd="/home/u/code/app")
        a4_line = assistant_line("a4", self.A4_t, model="claude-haiku-4-5", usage=tok(300),
                                  cwd="/home/u/code/app")
        a0_line = assistant_line("a0", self.A0_t, model="claude-opus-4", usage=tok(60),
                                  cwd="/home/u/code/app")
        a1_line = assistant_line("a1", self.A1_t, model="claude-opus-4",
                                  usage=usage(i=100, cw=20, cr=300, o=80), cwd="/home/u/code/app")

        # A half-written last line: the stream was cut mid-object, no trailing newline. It must
        # stay fully buffered (never parsed) until the rest arrives. Built by chopping a valid
        # line's closing "}}\n" off a real assistant_line so the completed text is unambiguous.
        self.half1_t = NOW - 30
        full_half_line = assistant_line("half1", self.half1_t, model="claude-opus-4",
                                         usage=usage(i=5, o=15), cwd="/home/u/code/app")
        assert full_half_line.endswith("\n")
        self.half1_partial = full_half_line[:-3]  # no trailing newline: stays fully buffered
        self.half1_completion = full_half_line[-3:]  # reassembles the exact original line
        assert not self.half1_partial.endswith("\n")
        assert self.half1_partial + self.half1_completion == full_half_line

        write_lines(self.session1_path,
                    [a2_line, dup1a_line, dup1b_line, a4_line, a0_line, a1_line, self.half1_partial],
                    mtime=NOW - 5)  # recent mtime: this session is "busy"

        # ---- an older reply in the same project: fills the oldest heatmap day ----
        self.older_path = projects / "-home-u-app" / "older.jsonl"
        self.older_t = local_ts(2026, 9, 18, 9, 0, 0)  # exactly the oldest displayed heat day
        older_line = assistant_line("older1", self.older_t, model="claude-opus-4", usage=tok(400),
                                     cwd="/home/u/code/app")
        write_lines(self.older_path, [older_line], mtime=self.older_t)

        # ---- same project "app", reached through a worktree cwd ----
        self.wt_path = projects / "-home-u-app-worktree" / "wtsession.jsonl"
        self.W1_t = NOW - 45 * 60
        w1_line = assistant_line("w1", self.W1_t, model="claude-opus-4-1", usage=tok(150),
                                  cwd="/home/u/code/app/.claude/worktrees/feat-x")
        write_lines(self.wt_path, [w1_line], mtime=self.W1_t)

        # ---- project "web": mixed model families, an invalid JSON line, a synthetic reply ----
        self.web_path = projects / "-home-u-web" / "web1.jsonl"
        self.B1_t = NOW - 20 * 60
        self.B3_t = NOW - 12 * 60
        b1_line = assistant_line("b1", self.B1_t, model="gpt-4-turbo", usage=tok(70),
                                  cwd="/home/u/code/web")
        invalid_line = "{this is not valid json,,,}\n"
        synthetic_line = assistant_line("synth1", NOW - 15 * 60, model="<synthetic>", usage=tok(9999),
                                         cwd="/home/u/code/web")
        b3_line = assistant_line("b3", self.B3_t, model="claude-haiku-4-5", usage=tok(130),
                                  cwd="/home/u/code/web")
        write_lines(self.web_path, [b1_line, invalid_line, synthetic_line, b3_line],
                    mtime=NOW - 20 * 60)

        # ---- one running subagent, with its .meta.json ----
        self.agent_path = projects / "-home-u-app" / "subagents" / "agent-explore.jsonl"
        self.AG1_t = NOW - 4 * 60
        self.AG2_t = NOW - 60
        ag1_line = assistant_line("ag1", self.AG1_t, model="claude-sonnet-4", usage=tok(120),
                                   stop_reason="tool_use", cwd="/home/u/code/app")
        ag2_line = assistant_line("ag2", self.AG2_t, model="claude-sonnet-4", usage=tok(80),
                                   stop_reason="tool_use", cwd="/home/u/code/app")
        write_lines(self.agent_path, [ag1_line, ag2_line], mtime=NOW - 10)  # < 90s: running
        meta_path = Path(str(self.agent_path)[: -len(".jsonl")] + ".meta.json")
        meta_path.write_text('{"agentType": "Explore", "description": "scan the codebase"}')

        # ---- live session pids, via an injected pid_alive fake (4242 is the only live one) ----
        sessions = self.home / "sessions"
        (sessions / "4242.json").write_text('{"sessionId": "session1"}')  # no status: 20 s rule
        (sessions / "7777.json").write_text("{}")  # not alive per the fake
        (sessions / "-3.json").write_text("{}")  # pid <= 0: never even asked
        (sessions / "notapid.json").write_text("{}")  # not an int: skipped
        (sessions / "ignore.txt").write_text("nope")  # wrong suffix: skipped

    # ---- snapshot math, one TimeRange at a time --------------------------------------------

    def test_hour_range(self) -> None:
        s = self.store.snapshot(TimeRange.HOUR, now=NOW)

        # Today: a1(500) + a2(200) + dup1(999, final value only) + w1(150) + a0(60)
        #      + b1(70) + b3(130) + ag1(120) + ag2(80) = 2309, 9 replies.
        self.assertEqual((s.today.input, s.today.cache_write, s.today.cache_read, s.today.output),
                          (100, 20, 300, 1889))
        self.assertEqual((s.today.total, s.today_replies), (2309, 9))

        # 5h window restarts at dup1(final): the reply chain a4 -> a0 -> dup1 each restarts it,
        # then nothing after dup1 is >= 5h later, so it stays open.
        self.assertEqual(s.window_start, self.dup1b_t)
        self.assertEqual(s.window_reset, self.dup1b_t + 5 * H)
        self.assertEqual(s.window, 2249)  # dup1+a2+w1+b1+b3+a1+ag1+ag2 (a0 is before the window)

        self.assertEqual(s.models, [
            ModelTotal("sonnet", 1399, 4),  # a2 + dup1 + ag1 + ag2
            ModelTotal("opus", 710, 3),  # a1 + w1 + a0
            ModelTotal("haiku", 130, 1),  # b3
            ModelTotal("other", 70, 1),  # b1
        ])
        self.assertEqual(s.projects, [ProjectTotal("app", 2109), ProjectTotal("web", 200)])

        self.assertEqual(s.live_sessions, 1)
        self.assertEqual(s.busy_sessions, 1)  # only session1.jsonl was touched in the last 20s
        self.assertEqual((s.context, s.context_project), (420, "app"))  # a1's usage: 100+300+20

        self.assertEqual(len(s.last_hour), 60)
        self.assertEqual(s.last_hour[14], 150)  # w1, 45 min ago
        self.assertEqual(s.last_hour[39], 70)  # b1, 20 min ago
        self.assertEqual(s.last_hour[47], 130)  # b3, 12 min ago
        self.assertEqual(s.last_hour[49], 500)  # a1, 10 min ago
        self.assertEqual(s.last_hour[55], 120)  # ag1, 4 min ago
        self.assertEqual(s.last_hour[58], 80)  # ag2, 1 min ago
        self.assertEqual(sum(s.last_hour), 1050)
        self.assertEqual(s.per_minute, 40)  # (ag2 80 + ag1 120) // 5
        self.assertEqual(s.peak_per_minute, 500)

        self.assertEqual(s.bucket_seconds, 60.0)
        self.assertEqual((len(s.main), len(s.agent_series)), (60, 60))
        self.assertEqual((s.main[14], s.main[39], s.main[47], s.main[49]), (150.0, 70.0, 130.0, 500.0))
        self.assertEqual(s.main[55], 0.0)  # agent tokens never leak into the main series
        self.assertEqual(sum(s.main), 850.0)
        self.assertEqual((s.agent_series[55], s.agent_series[58]), (120.0, 80.0))
        self.assertEqual(sum(s.agent_series), 200.0)

        self.assertEqual(len(s.agents_today), 1)
        a = s.agents_today[0]
        self.assertEqual(a.id, str(self.agent_path))
        self.assertTrue(a.running)
        self.assertEqual((a.type, a.task, a.family), ("Explore", "scan the codebase", "sonnet"))
        self.assertEqual((a.start, a.end, a.tokens), (self.AG1_t, NOW, 200))
        self.assertEqual(s.spans, [a])

    def test_five_hours_range(self) -> None:
        s = self.store.snapshot(TimeRange.FIVE_HOURS, now=NOW)
        self.assertEqual(s.bucket_seconds, 300.0)
        self.assertEqual((len(s.main), len(s.agent_series)), (60, 60))
        self.assertEqual(s.main[50], 30.0)  # w1: 150/5
        self.assertEqual(s.main[55], 14.0)  # b1: 70/5
        self.assertEqual(s.main[57], 126.0)  # b3 + a1: (130+500)/5
        self.assertEqual(s.main[35], 40.0)  # a2: 200/5
        self.assertAlmostEqual(s.main[24], 199.8)  # dup1: 999/5
        self.assertEqual(s.agent_series[59], 40.0)  # ag1 + ag2: (120+80)/5
        self.assertAlmostEqual(sum(s.main), 2049 / 5)
        self.assertEqual(sum(s.agent_series), 40.0)
        # a0 (10h ago) is outside the 5h range, unlike a2 and dup1 (2h/3h ago).
        self.assertEqual(len(s.spans), 1)

    def test_day_range(self) -> None:
        s = self.store.snapshot(TimeRange.DAY, now=NOW)
        self.assertEqual(s.bucket_seconds, 900.0)
        self.assertEqual((len(s.main), len(s.agent_series)), (96, 96))
        self.assertEqual(s.main[55], 4.0)  # a0: 60/15
        self.assertEqual(s.main[92], 10.0)  # w1: 150/15
        self.assertEqual(s.main[95], 42.0)  # b3 + a1: (130+500)/15
        self.assertAlmostEqual(s.main[87], 200 / 15)  # a2
        self.assertAlmostEqual(s.main[84], 999 / 15)  # dup1
        self.assertAlmostEqual(s.agent_series[95], 200 / 15)  # ag1 + ag2
        self.assertAlmostEqual(sum(s.main), 2109 / 15)
        self.assertAlmostEqual(sum(s.agent_series), 200 / 15)
        # a4 (30h ago) and older1 (~6 days ago) are outside the 24h range.
        self.assertEqual(len(s.spans), 1)

    def test_week_range(self) -> None:
        s = self.store.snapshot(TimeRange.WEEK, now=NOW)
        self.assertEqual((s.main, s.agent_series, s.spans), ([], [], []))
        self.assertEqual(s.bucket_seconds, 60.0)
        self.assertEqual(len(s.agents_today), 1)  # agents_today is computed for every range
        self.assertEqual(s.range, TimeRange.WEEK)

    def test_heat_grid_same_for_every_range(self) -> None:
        first_date = datetime.date(2026, 9, 18)
        expected_days = [first_date + datetime.timedelta(days=k) for k in range(7)]
        heats = []
        for r in TimeRange:
            s = self.store.snapshot(r, now=NOW)
            self.assertEqual(s.heat_days, expected_days)
            heats.append(s.heat)
        self.assertTrue(all(h == heats[0] for h in heats))

        heat = heats[0]
        self.assertEqual(len(heat), 7)
        self.assertTrue(all(len(row) == 24 for row in heat))
        self.assertEqual(heat[0][9], 400)  # older1, day 0 (the oldest shown day)
        self.assertEqual(heat[5][6], 300)  # a4, yesterday
        self.assertEqual(heat[6][2], 60)  # a0, today at 02:00
        self.assertEqual(heat[6][9], 999)  # dup1
        self.assertEqual(heat[6][10], 200)  # a2
        self.assertEqual(heat[6][11], 1050)  # w1 + b1 + b3 + a1 + ag1 + ag2, all in the last hour
        self.assertEqual(sum(map(sum, heat)), 3009)

    def test_duplicate_message_id_counted_once(self) -> None:
        self.assertIn("dup1", self.store.replies)
        self.assertEqual(self.store.replies["dup1"].usage.total, 999)
        self.assertEqual(self.store.replies["dup1"].time, self.dup1b_t)
        self.assertEqual(len(self.store.replies), 11)  # not 12: dup1a was overwritten, not added

    # ---- feed a snapshot through rows.py: must not raise, and must contain the right text ----

    def test_build_rows(self) -> None:
        s = self.store.snapshot(TimeRange.HOUR, now=NOW)
        anim = Animator()
        anim.apply(s, now=NOW, reduce_motion=True)  # shown snaps straight to target
        rows = build_rows(s, anim, now=NOW, spin_phase=True)
        # Sessions, 1 "Now" line (pid 4242 is live), Tokens, in/out, Speed, Models, Agents, 1 agent row, Projects
        self.assertEqual(len(rows), 9)
        now_line = rows.pop(1)
        self.assertTrue("".join(seg.text for seg in now_line.segs).startswith("  ● "))

        sessions_text = "".join(seg.text for seg in rows[0].segs)
        self.assertIn("1 busy", sessions_text)
        self.assertIn("app", sessions_text)
        self.assertIn(fmt.tokens(420), sessions_text)
        self.assertIn(f"Latest conversation: {fmt.exact(420)} tokens of context (app)", rows[0].tip)

        tokens_text = "".join(seg.text for seg in rows[1].segs)
        self.assertIn(fmt.tokens(2309), tokens_text)  # "2.3k"
        self.assertIn("resets ~", tokens_text)

        inout_text = "".join(seg.text for seg in rows[2].segs)
        self.assertIn("in ", inout_text)
        self.assertIn("out ", inout_text)
        self.assertIn("cache ", inout_text)

        speed_text = "".join(seg.text for seg in rows[3].segs)
        self.assertIn(fmt.tokens(40), speed_text)
        self.assertIn(fmt.tokens(500), speed_text)

        models_text = "".join(seg.text for seg in rows[4].segs)
        self.assertIn("sonnet", models_text)
        self.assertIn("opus", models_text)
        self.assertIn("haiku", models_text)
        self.assertTrue(any("other:" in line for line in rows[4].tip))

        agents_text = "".join(seg.text for seg in rows[5].segs)
        self.assertIn("running", agents_text)
        self.assertIn("today", agents_text)
        self.assertEqual(rows[5].segs[1].role, "warn")  # 1 running

        agent_row_text = "".join(seg.text for seg in rows[6].segs)
        self.assertIn("Explore", agent_row_text)
        self.assertIn("scan the codebase", agent_row_text)
        self.assertIn("4m0s", agent_row_text)
        self.assertTrue(rows[6].tip[-1].startswith("Running for"))

        proj_text = "".join(seg.text for seg in rows[-1].segs)
        self.assertIn("app", proj_text)
        self.assertIn("web", proj_text)
        self.assertEqual(len(rows[-1].tip), 1 + 2)  # header + 2 projects

    def test_compact_summary(self) -> None:
        s = self.store.snapshot(TimeRange.HOUR, now=NOW)
        anim = Animator()
        anim.apply(s, now=NOW, reduce_motion=True)
        segs = compact_summary(s, anim, spin_phase=True)
        text = "".join(seg.text for seg in segs)
        self.assertIn(fmt.tokens(40), text)
        self.assertIn("/min", text)
        self.assertIn("1 live", text)
        self.assertIn("1 agent", text)
        self.assertIn("◐", text)

    def test_chart_tooltip(self) -> None:
        s = self.store.snapshot(TimeRange.HOUR, now=NOW)
        # Bucket 49 is a1's minute (10 min ago): a main-series bucket with no agent tokens.
        lines = chart_tooltip(s, i=49, now=NOW)
        self.assertEqual(lines[0], fmt.hm(self.A1_t))
        self.assertEqual(lines[1], f"Main session: {fmt.tokens(500)}/min")
        self.assertEqual(lines[2], "Agents: 0/min")
        # Bucket 55 is ag1's minute: an agent-series bucket with no main tokens.
        lines2 = chart_tooltip(s, i=55, now=NOW)
        self.assertEqual(lines2[1], "Main session: 0/min")
        self.assertEqual(lines2[2], f"Agents: {fmt.tokens(120)}/min")

    def test_span_tooltip(self) -> None:
        s = self.store.snapshot(TimeRange.HOUR, now=NOW)
        lines = span_tooltip(s.agents_today[0], now=NOW)
        self.assertEqual(lines[0], "Explore")
        self.assertEqual(lines[1], "scan the codebase")
        self.assertIn("sonnet", lines[2])
        self.assertIn(fmt.exact(200), lines[2])
        self.assertTrue(lines[3].startswith("Running for"))
        self.assertIn(fmt.duration(240), lines[3])

    def test_heat_tooltip(self) -> None:
        s = self.store.snapshot(TimeRange.HOUR, now=NOW)
        lines = heat_tooltip(s, d=6, h=11)
        self.assertEqual(lines[0], f"{fmt.day_long(s.heat_days[6])}, 11:00–12:00")
        self.assertEqual(lines[1], f"{fmt.exact(1050)} tokens")

    # ---- append new data, refresh again: only the new data is added -------------------------

    def test_incremental_refresh_adds_only_new_data(self) -> None:
        before = self.store.snapshot(TimeRange.HOUR, now=NOW)
        before_reply_count = len(self.store.replies)

        NOW2 = NOW + 60
        a5_t = NOW2 - 10
        a5_line = assistant_line("a5", a5_t, model="claude-opus-4", usage=tok(60),
                                  cwd="/home/u/code/app")
        # Complete the half-written line from setUp, then append one brand-new reply, in one write.
        write_lines(self.session1_path, [self.half1_completion, a5_line], mtime=NOW2 - 5)

        self.store.refresh(now=NOW2, force_discover=True)

        self.assertEqual(len(self.store.replies), before_reply_count + 2)
        self.assertEqual(self.store.replies["half1"].usage.total, 20)
        self.assertEqual(self.store.replies["half1"].time, self.half1_t)
        self.assertEqual(self.store.replies["a5"].usage.total, 60)
        # Old data is untouched: dup1's overwrite still holds, nothing was recounted or lost.
        self.assertEqual(self.store.replies["dup1"].usage.total, 999)

        after = self.store.snapshot(TimeRange.HOUR, now=NOW2)
        self.assertEqual(after.today_replies, before.today_replies + 2)
        self.assertEqual(after.today.total, before.today.total + 20 + 60)


if __name__ == "__main__":
    unittest.main()
