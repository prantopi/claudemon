"""Store discovery and incremental reading (data-flow §2, §3, §6). Task P1."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from support import assistant_line, local_ts, make_home, write_lines

from claudemon_lib.model import TimeRange
from claudemon_lib.store import Store

NOW = local_ts(2026, 9, 24, 12)


def usage(i: int = 0, cw: int = 0, cr: int = 0, o: int = 0) -> dict:
    return {"input_tokens": i, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr,
            "output_tokens": o}


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = make_home(self.tmp)
        self.proj = self.home / "projects" / "-home-u-app"
        self.store = Store(claude_home=self.home, pid_alive=lambda pid: pid == 4242)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def refresh(self, now: float = NOW) -> None:
        self.store.refresh(now=now, force_discover=True)

    def write(self, path: Path, lines, mtime: float = NOW - 5) -> str:
        write_lines(path, lines, mtime=mtime)
        return str(path)


class TestMissingAndBadInput(StoreCase):
    def test_missing_claude_dir(self) -> None:
        store = Store(claude_home=self.tmp / "nope", pid_alive=lambda pid: True)
        store.refresh(now=NOW, force_discover=True)
        s = store.snapshot(TimeRange.HOUR, now=NOW)
        self.assertTrue(s.loaded)
        self.assertEqual((s.live_sessions, s.today.total, s.today_replies, s.window), (0, 0, 0, 0))
        self.assertEqual(s.main, [0.0] * 60)
        self.assertEqual(s.heat, [[0] * 24 for _ in range(7)])
        self.assertEqual((s.models, s.projects, s.spans, s.agents_today), ([], [], [], []))
        self.assertIsNone(s.window_start)

    def test_invalid_json_lines_skipped(self) -> None:
        self.write(self.proj / "s.jsonl", [
            "{not json\n",
            assistant_line("a", NOW - 60),
            "[1, 2, 3]\n",
            "\"just a string\"\n",
            "\n",
            "   \n",
            "\xff\xfe garbage \u0000\n",
            "[" * 100000 + "\n",  # deep nesting: RecursionError must be caught
            json.dumps({"type": "assistant", "message": "nope"}) + "\n",
            json.dumps({"type": "assistant", "timestamp": "2026-09-24T10:00:00Z",
                        "message": {"model": "claude-opus-4", "usage": [1, 2]}}) + "\n",
            assistant_line("b", NOW - 30),
        ])
        with open(self.proj / "s.jsonl", "ab") as f:
            f.write(b"\xc3\x28 invalid utf8\n")
        os.utime(self.proj / "s.jsonl", (NOW - 5, NOW - 5))
        self.refresh()
        self.assertEqual(sorted(self.store.replies), ["a", "b"])

    def test_skips_non_assistant_synthetic_bad_timestamp(self) -> None:
        good = json.loads(assistant_line("x", NOW - 60))
        user = dict(good, type="user")
        synth = json.loads(assistant_line("syn", NOW - 60, model="<synthetic>"))
        bad_ts = json.loads(assistant_line("badts", NOW - 60))
        bad_ts["timestamp"] = "yesterday"
        no_tz = json.loads(assistant_line("notz", NOW - 60))
        no_tz["timestamp"] = no_tz["timestamp"][:-1]
        no_model = json.loads(assistant_line("nomodel", NOW - 60))
        del no_model["message"]["model"]
        self.write(self.proj / "s.jsonl", [json.dumps(o) + "\n" for o in (user, synth, bad_ts, no_tz, no_model)])
        self.refresh()
        self.assertEqual(self.store.replies, {})

    def test_non_int_usage_values_count_as_zero(self) -> None:
        u = {"input_tokens": "10", "cache_creation_input_tokens": True, "cache_read_input_tokens": 1.5,
             "output_tokens": 7}
        self.write(self.proj / "s.jsonl", [assistant_line("a", NOW - 60, usage=u)])
        self.refresh()
        self.assertEqual(self.store.replies["a"].usage.total, 7)


class TestCountOnce(StoreCase):
    def test_streamed_reply_counted_once(self) -> None:
        self.write(self.proj / "s.jsonl", [
            assistant_line("msg_1", NOW - 60, usage=usage(i=5, o=1), stop_reason=None),
            assistant_line("msg_1", NOW - 60, usage=usage(i=5, o=50), stop_reason=None),
            assistant_line("msg_1", NOW - 60, usage=usage(i=5, o=100)),
        ])
        self.refresh()
        self.assertEqual(list(self.store.replies), ["msg_1"])
        self.assertEqual(self.store.replies["msg_1"].usage.total, 105)  # last line wins
        s = self.store.snapshot(TimeRange.HOUR, now=NOW)
        self.assertEqual((s.today_replies, s.today.total), (1, 105))

    def test_same_id_in_two_files_counted_once(self) -> None:
        self.write(self.proj / "a.jsonl", [assistant_line("dup", NOW - 60)])
        self.write(self.proj / "b.jsonl", [assistant_line("dup", NOW - 60)])
        self.refresh()
        self.assertEqual(self.store.snapshot(TimeRange.HOUR, now=NOW).today_replies, 1)

    def test_id_fallbacks(self) -> None:
        a = json.loads(assistant_line("m", NOW - 60))
        del a["message"]["id"]
        a["uuid"] = "uuid-1"
        b = json.loads(assistant_line("m", NOW - 50))
        del b["message"]["id"]
        del b["uuid"]
        c = dict(b)
        self.write(self.proj / "s.jsonl", [json.dumps(o) + "\n" for o in (a, b, c)])
        self.refresh()
        self.assertIn("uuid-1", self.store.replies)
        self.assertEqual(len(self.store.replies), 3)  # no id at all: each line is its own reply

    def test_rereading_unchanged_file_does_not_double_count(self) -> None:
        self.write(self.proj / "s.jsonl", [assistant_line("a", NOW - 60)])
        self.refresh()
        self.refresh(NOW + 1)
        self.assertEqual(self.store.snapshot(TimeRange.HOUR, now=NOW + 1).today_replies, 1)


class TestIncrementalRead(StoreCase):
    def test_appended_lines_read_from_offset(self) -> None:
        p = self.write(self.proj / "s.jsonl", [assistant_line("a", NOW - 60)])
        self.refresh()
        t = self.store.transcripts[p]
        first = t.offset
        self.assertEqual(first, os.path.getsize(p))
        self.write(self.proj / "s.jsonl", [assistant_line("b", NOW - 30)], mtime=NOW - 1)
        self.refresh()
        self.assertEqual(sorted(self.store.replies), ["a", "b"])
        self.assertEqual(t.offset, os.path.getsize(p))
        self.assertGreater(t.offset, first)

    def test_half_written_last_line_completed_later(self) -> None:
        obj = json.loads(assistant_line("late", NOW - 60, cwd="/home/u/caf\u00e9"))
        raw = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        cut = raw.index("\u00e9".encode("utf-8")) + 1  # split inside a multi-byte UTF-8 char
        path = self.proj / "s.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.write(assistant_line("early", NOW - 90).encode("utf-8") + raw[:cut])
        os.utime(path, (NOW - 5, NOW - 5))
        self.refresh()
        t = self.store.transcripts[str(path)]
        self.assertEqual(list(self.store.replies), ["early"])
        self.assertEqual(t.partial, raw[:cut])
        with open(path, "ab") as f:
            f.write(raw[cut:])
        os.utime(path, (NOW - 1, NOW - 1))
        self.refresh()
        self.assertEqual(sorted(self.store.replies), ["early", "late"])
        self.assertEqual(t.partial, b"")
        self.assertEqual(t.project, "café")  # multi-byte char survived the split

    def test_only_complete_lines_parsed(self) -> None:
        path = self.proj / "s.jsonl"
        line = assistant_line("a", NOW - 60)
        write_lines(path, [line.rstrip("\n")], mtime=NOW - 5)  # valid JSON, but no newline yet
        self.refresh()
        self.assertEqual(self.store.replies, {})
        write_lines(path, ["\n"], mtime=NOW - 1)
        self.refresh()
        self.assertEqual(list(self.store.replies), ["a"])

    def test_truncated_file_restarts_from_zero(self) -> None:
        path = self.proj / "s.jsonl"
        p = self.write(path, [assistant_line("a", NOW - 60), assistant_line("b", NOW - 50)])
        self.refresh()
        path.write_text(assistant_line("c", NOW - 40), encoding="utf-8")
        os.utime(path, (NOW - 1, NOW - 1))
        self.refresh()
        self.assertIn("c", self.store.replies)
        self.assertEqual(self.store.transcripts[p].offset, os.path.getsize(p))

    def test_chunked_reads_match_whole_reads(self) -> None:
        lines = [assistant_line("m%d" % k, NOW - 600 + k) for k in range(50)]
        self.write(self.proj / "s.jsonl", lines)
        self.store.READ_CHUNK = 37  # force many tiny chunks
        self.refresh()
        self.assertEqual(len(self.store.replies), 50)

    def test_partial_line_cap(self) -> None:
        path = self.proj / "s.jsonl"
        self.store.PARTIAL_CAP = 1000
        self.store.READ_CHUNK = 300
        write_lines(path, ['{"junk": "' + "x" * 5000], mtime=NOW - 5)  # no newline, over the cap
        self.refresh()
        t = self.store.transcripts[str(path)]
        self.assertLessEqual(len(t.partial), 1000)
        # The rest of the huge line arrives, then a good line: only the good line counts.
        write_lines(path, ["y" * 700 + '"}\n', assistant_line("good", NOW - 30)], mtime=NOW - 1)
        self.refresh()
        self.assertEqual(list(self.store.replies), ["good"])
        self.assertEqual(t.partial, b"")

    @unittest.skipIf(sys.platform == "win32" or getattr(os, "geteuid", lambda: 0)() == 0,
                     "needs POSIX permissions and a non-root user")
    def test_oserror_on_read_retries_next_poll(self) -> None:
        p = self.write(self.proj / "s.jsonl", [assistant_line("a", NOW - 60)])
        os.chmod(p, 0)
        try:
            self.refresh()  # open() fails: no crash, nothing read, retried later
            t = self.store.transcripts[p]
            self.assertEqual((t.offset, t.stat_key, self.store.replies), (0, None, {}))
        finally:
            os.chmod(p, 0o644)
        self.refresh()  # same mtime/size as before, but the failed read must be retried
        self.assertEqual(list(self.store.replies), ["a"])


class TestDiscovery(StoreCase):
    def test_only_recent_files(self) -> None:
        self.write(self.proj / "old.jsonl", [assistant_line("old", NOW - 60)], mtime=NOW - 8 * 86400)
        self.write(self.proj / "new.jsonl", [assistant_line("new", NOW - 60)])
        self.refresh()
        self.assertEqual([os.path.basename(p) for p in self.store.transcripts], ["new.jsonl"])

    def test_replies_older_than_retention_dropped_but_state_updated(self) -> None:
        p = self.write(self.proj / "s.jsonl", [assistant_line("ancient", NOW - 8 * 86400)])
        self.refresh()
        self.assertEqual(self.store.replies, {})
        self.assertEqual(self.store.transcripts[p].last_reply, NOW - 8 * 86400)

    def test_hidden_and_non_jsonl_skipped(self) -> None:
        self.write(self.proj / ".hidden.jsonl", [assistant_line("h1", NOW - 60)])
        self.write(self.home / "projects" / ".git" / "x.jsonl", [assistant_line("h2", NOW - 60)])
        self.write(self.proj / "notes.json", [assistant_line("h3", NOW - 60)])
        self.write(self.proj / "deep" / "er" / "s.jsonl", [assistant_line("deep", NOW - 60)])
        self.refresh()
        self.assertEqual(list(self.store.replies), ["deep"])

    def test_subagents_and_meta(self) -> None:
        sub = self.proj / "sess1" / "subagents"
        a = self.write(sub / "agent-a.jsonl", [assistant_line("a", NOW - 60)])
        (sub / "agent-a.meta.json").write_text(json.dumps({"agentType": "Explore", "description": "Find it"}))
        b = self.write(sub / "agent-b.jsonl", [assistant_line("b", NOW - 60)])  # no meta
        c = self.write(sub / "agent-c.jsonl", [assistant_line("c", NOW - 60)])
        (sub / "agent-c.meta.json").write_text("{broken")
        d = self.write(sub / "agent-d.jsonl", [assistant_line("d", NOW - 60)])
        (sub / "agent-d.meta.json").write_text(json.dumps({"agentType": 5, "description": None}))
        m = self.write(self.proj / "sess1.jsonl", [assistant_line("m", NOW - 60)])
        self.refresh()
        ts = self.store.transcripts
        self.assertTrue(ts[a].is_agent)
        self.assertFalse(ts[m].is_agent)
        self.assertEqual((ts[a].agent_type, ts[a].agent_task), ("Explore", "Find it"))
        for p in (b, c, d):
            self.assertEqual((ts[p].agent_type, ts[p].agent_task), ("agent", ""))

    def test_discovery_interval(self) -> None:
        self.write(self.proj / "a.jsonl", [assistant_line("a", NOW - 60)])
        self.store.refresh(now=NOW)
        self.write(self.proj / "b.jsonl", [assistant_line("b", NOW - 60)])
        self.store.refresh(now=NOW + 2)
        self.assertEqual(list(self.store.replies), ["a"])
        self.store.refresh(now=NOW + 3.5)
        self.assertEqual(sorted(self.store.replies), ["a", "b"])

    def test_project_from_first_cwd_and_worktree_rule(self) -> None:
        p1 = self.write(self.proj / "s1.jsonl", [
            json.dumps({"type": "user", "cwd": "/home/u/code/app/.claude/worktrees/feat-x"}) + "\n",
            assistant_line("a", NOW - 60, cwd="/home/u/other"),
        ])
        p2 = self.write(self.proj / "s2.jsonl", [assistant_line("b", NOW - 60, cwd=r"C:\Users\u\app\.claude\worktrees\w1")])
        p3 = self.write(self.proj / "s3.jsonl", [assistant_line("c", NOW - 60)])
        self.refresh()
        self.assertEqual(self.store.transcripts[p1].project, "app")
        self.assertEqual(self.store.transcripts[p2].project, "app")
        self.assertEqual(self.store.transcripts[p3].project, "?")

    def test_last_stop_and_context(self) -> None:
        p = self.write(self.proj / "s.jsonl", [
            assistant_line("a", NOW - 60, usage=usage(1, 2, 3, 4), stop_reason="end_turn"),
            assistant_line("b", NOW - 30, usage=usage(10, 20, 30, 40), stop_reason="tool_use"),
        ])
        self.refresh()
        t = self.store.transcripts[p]
        self.assertEqual((t.last_stop, t.last_context, t.last_reply), ("tool_use", 60, NOW - 30))

    @unittest.skipIf(sys.platform == "win32", "POSIX-only: symlinks and FIFOs")
    def test_symlinks_not_followed_and_fifo_skipped(self) -> None:
        outside = self.tmp / "outside"
        self.write(outside / "s.jsonl", [assistant_line("outside", NOW - 60)])
        self.proj.mkdir(parents=True, exist_ok=True)
        os.symlink(str(outside), str(self.proj / "linked"))
        os.mkfifo(str(self.proj / "pipe.jsonl"))
        self.refresh()  # must not block on the FIFO
        self.assertEqual(self.store.replies, {})
        self.assertEqual(self.store.transcripts, {})


class TestLiveSessions(StoreCase):
    def test_live_sessions_counted_by_pid(self) -> None:
        sessions = self.home / "sessions"
        for name in ("4242.json", "99.json", "abc.json", "0.json", "-1.json", "4242.txt"):
            (sessions / name).write_text("{}")
        s = self.store.snapshot(TimeRange.HOUR, now=NOW)
        self.assertEqual(s.live_sessions, 1)

    def test_pid_probe_errors_count_as_dead(self) -> None:
        def boom(pid: int) -> bool:
            raise RuntimeError("probe failed")

        store = Store(claude_home=self.home, pid_alive=boom)
        (self.home / "sessions" / "4242.json").write_text("{}")
        self.assertEqual(store.snapshot(TimeRange.HOUR, now=NOW).live_sessions, 0)

    def test_missing_sessions_dir(self) -> None:
        (self.home / "sessions").rmdir()
        self.assertEqual(self.store.snapshot(TimeRange.HOUR, now=NOW).live_sessions, 0)


if __name__ == "__main__":
    unittest.main()
