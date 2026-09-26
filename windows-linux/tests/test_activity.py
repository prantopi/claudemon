"""Activity release: current action per transcript, the "Now" block and official limits.

See the shared activity spec §1-§3.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import List, Optional

import support  # noqa: F401
from support import iso, make_home, write_lines

from claudemon_lib import formatting as fmt
from claudemon_lib.animation import Animator
from claudemon_lib.model import Limits, LimitWindow, NowAgent, NowSession, Snapshot, Span, TimeRange
from claudemon_lib.paths import limits_path
from claudemon_lib.rows import build_rows, limits_line, now_lines
from claudemon_lib.store import (
    FINISHED,
    LIMITS_CAP,
    THINKING,
    WAITING,
    Store,
    read_limits,
    tool_action,
)
from claudemon_lib.formatting import clip_text, sanitize

NOW = time.time()


def tool(name: str, **inp) -> dict:
    return {"type": "tool_use", "id": "t", "name": name, "input": inp}


def assistant(t: float, content: list, stop: Optional[str] = None, model: str = "claude-opus-4") -> str:
    obj = {
        "type": "assistant",
        "timestamp": iso(t),
        "message": {
            "id": "m%f" % t,
            "model": model,
            "content": content,
            "stop_reason": stop,
            "usage": {"input_tokens": 1, "output_tokens": 2},
        },
    }
    return json.dumps(obj) + "\n"


def user(t: float, content="hi") -> str:
    return json.dumps({"type": "user", "timestamp": iso(t), "message": {"role": "user", "content": content}}) + "\n"


def text(s: str = "done") -> dict:
    return {"type": "text", "text": s}


def text_of(line) -> str:
    return "".join(seg.text for seg in line.segs)


class ToolMappingTest(unittest.TestCase):
    def test_every_tool_in_the_table(self) -> None:
        cases = [
            (tool("Edit", file_path="/a/b/store.py"), "Editing store.py"),
            (tool("MultiEdit", file_path="C:\\x\\y\\rows.py"), "Editing rows.py"),
            (tool("NotebookEdit", notebook_path="/n/book.ipynb"), "Editing book.ipynb"),
            (tool("Write", file_path="/a/new.txt"), "Writing new.txt"),
            (tool("Read", file_path="/a/model.py"), "Reading model.py"),
            (tool("Bash", command="ls -la", description="List files"), "Running List files"),
            (tool("Bash", command="git status\ngit log"), "Running git status"),
            (tool("Grep", pattern="def foo"), "Searching for def foo"),
            (tool("Glob", pattern="**/*.py"), "Finding **/*.py"),
            (tool("Agent", description="Review code", subagent_type="iris"), "Starting agent: Review code"),
            (tool("Task", subagent_type="Explore"), "Starting agent: Explore"),
            (tool("WebFetch", url="https://docs.python.org/3/library"), "Reading docs.python.org"),
            (tool("WebSearch", query="tkinter canvas"), "Searching the web: tkinter canvas"),
            (tool("TodoWrite", todos=[]), "Updating the plan"),
            (tool("Skill", skill="run"), "Using skill run"),
            (tool("mcp__github__create_issue"), "Using github: create_issue"),
            (tool("SomethingNew", x=1), "Using SomethingNew"),
        ]
        for block, want in cases:
            with self.subTest(want=want):
                self.assertEqual(tool_action(block), want)

    def test_missing_fields_fall_back_to_using_name(self) -> None:
        for name in ("Edit", "MultiEdit", "NotebookEdit", "Write", "Read", "Bash", "Grep", "Glob",
                     "Agent", "Task", "WebFetch", "WebSearch", "Skill"):
            with self.subTest(name=name):
                self.assertEqual(tool_action({"type": "tool_use", "name": name}), "Using " + name)
                self.assertEqual(tool_action(tool(name, file_path=5, command="  ", url="nope")), "Using " + name)
        self.assertEqual(tool_action(tool("mcp__srv")), "Using mcp__srv")
        self.assertEqual(tool_action(tool("Read", file_path="/a/b/")), "Reading b")  # trailing separator
        self.assertEqual(tool_action(tool("Read", file_path="C:\\a\\c\\")), "Reading c")
        # F9: an empty or missing name
        self.assertEqual(tool_action({"type": "tool_use"}), "Using a tool")
        self.assertEqual(tool_action(tool("")), "Using a tool")
        self.assertEqual(tool_action(tool(" \n ")), "Using a tool")

    def test_sanitising_and_clipping(self) -> None:
        self.assertEqual(sanitize("a\nb\t\x00c\u202e  d\r\n"), "a b c d")
        # D/F11: format characters (bidi, zero-width), line and paragraph separators
        self.assertEqual(sanitize("x\u200by\u2028z\u2029w\ufeff "), "x y z w")
        self.assertEqual(clip_text("abc", 3), "abc")
        self.assertEqual(clip_text("abcd", 3), "ab…")
        long = "x" * 100
        got = tool_action(tool("Grep", pattern=long))
        self.assertEqual(got, "Searching for " + "x" * 39 + "…")  # value clipped to 40
        got = tool_action(tool("mcp__" + "s" * 60 + "__" + "t" * 60))
        # F6: the full action is stored (each value capped at 40); rows clip it
        self.assertEqual(got, "Using " + "s" * 39 + "…: " + "t" * 39 + "…")
        self.assertEqual(tool_action(tool("Bash", description="evil\n\x1b[31mred   text")), "Running evil [31mred text")


class ActionRulesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = make_home(Path(self._tmp.name))
        self.proj = self.home / "projects" / "-Users-me-app"
        self.alive = {100}
        self.store = Store(claude_home=self.home, pid_alive=lambda p: p in self.alive)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def main(self, sid: str = "S1") -> Path:
        return self.proj / (sid + ".jsonl")

    def agent(self, name: str, sid: str = "S1", atype: str = "Explore") -> Path:
        p = self.proj / sid / "subagents" / (name + ".jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        (p.parent / (name + ".meta.json")).write_text(json.dumps({"agentType": atype, "description": "look"}))
        return p

    def refresh(self) -> None:
        self.store.refresh(now=NOW, force_discover=True)

    def t(self, path: Path):
        return self.store.transcripts[str(path)]

    def test_waiting_thinking_tool_rules(self) -> None:
        p = self.main()
        write_lines(p, [user(NOW - 60)])
        self.refresh()
        self.assertEqual(self.t(p).action, THINKING)  # the first prompt counts too
        write_lines(p, [assistant(NOW - 50, [text()], "end_turn")])
        self.refresh()
        self.assertEqual(self.t(p).action, WAITING)
        write_lines(p, [user(NOW - 40)])
        self.refresh()
        self.assertEqual(self.t(p).action, THINKING)
        self.assertAlmostEqual(self.t(p).action_time, NOW - 40, delta=0.01)
        write_lines(p, [assistant(NOW - 30, [text("let me look"), tool("Read", file_path="/x/a.py"),
                                             tool("Bash", command="make")], "tool_use")])
        self.refresh()
        self.assertEqual(self.t(p).action, "Running make")  # last tool_use block wins
        write_lines(p, [user(NOW - 20, [{"type": "tool_result", "content": "ok"}])])
        self.refresh()
        self.assertEqual(self.t(p).action, "Running make")  # tool result keeps the tool action
        self.assertAlmostEqual(self.t(p).action_time, NOW - 30, delta=0.01)
        write_lines(p, [assistant(NOW - 10, [text()], None)])
        self.refresh()
        self.assertEqual(self.t(p).action, THINKING)  # mid-turn text
        self.assertAlmostEqual(self.t(p).action_time, NOW - 10, delta=0.01)
        write_lines(p, [assistant(NOW - 5, [text()], "end_turn")])
        self.refresh()
        self.assertEqual(self.t(p).action, WAITING)
        write_lines(p, [assistant(NOW - 4, [tool("Grep", pattern="x")], "tool_use"),
                        user(NOW - 3, [{"type": "tool_result", "content": "a"}, {"type": "tool_result", "content": "b"}])])
        self.refresh()
        self.assertEqual(self.t(p).action, "Searching for x")  # several tool results keep it too
        self.assertAlmostEqual(self.t(p).action_time, NOW - 4, delta=0.01)
        write_lines(p, [user(NOW - 2, [{"type": "tool_result", "content": "a"}, text("and this")])])
        self.refresh()
        self.assertEqual(self.t(p).action, THINKING)  # not only tool results: a prompt

    def test_thinking_only_assistant_line(self) -> None:
        p = self.main()
        write_lines(p, [assistant(NOW - 30, [tool("Read", file_path="/a.py")], "tool_use"),
                        assistant(NOW - 20, [{"type": "thinking", "thinking": "hm"}], None)])
        self.refresh()
        self.assertEqual(self.t(p).action, THINKING)
        self.assertAlmostEqual(self.t(p).action_time, NOW - 20, delta=0.01)
        write_lines(p, [assistant(NOW - 10, [{"type": "thinking", "thinking": "hm"}], "end_turn")])
        self.refresh()
        self.assertEqual(self.t(p).action, THINKING)  # nothing but thinking, even at end_turn

    def test_meta_and_command_lines_are_skipped(self) -> None:
        p = self.main()
        write_lines(p, [assistant(NOW - 30, [tool("Read", file_path="/a.py")], "tool_use")])
        meta = json.dumps({"type": "user", "isMeta": True, "timestamp": iso(NOW - 20),
                           "message": {"role": "user", "content": "Caveat: local commands"}}) + "\n"
        write_lines(p, [meta, user(NOW - 19, "<local-command-stdout>ok</local-command-stdout>"),
                        user(NOW - 18, "<command-name>/model</command-name>")])
        self.refresh()
        self.assertEqual(self.t(p).action, "Reading a.py")
        self.assertAlmostEqual(self.t(p).action_time, NOW - 30, delta=0.01)
        meta_a = json.dumps({"type": "assistant", "isMeta": True, "timestamp": iso(NOW - 10),
                             "message": {"content": [text()], "stop_reason": "end_turn"}}) + "\n"
        write_lines(p, [meta_a])
        self.refresh()
        self.assertEqual(self.t(p).action, "Reading a.py")
        write_lines(p, [user(NOW - 5, " <command-name>x")])  # only a prefix counts
        self.refresh()
        self.assertEqual(self.t(p).action, THINKING)

    def test_interrupt(self) -> None:
        p = self.main()
        write_lines(p, [assistant(NOW - 30, [tool("Bash", command="make")], "tool_use"),
                        user(NOW - 20, [text("[Request interrupted by user for tool use]")])])
        self.refresh()
        self.assertEqual(self.t(p).action, WAITING)
        self.assertAlmostEqual(self.t(p).action_time, NOW - 20, delta=0.01)
        write_lines(p, [user(NOW - 10), user(NOW - 9, "[Request interrupted by user]")])
        self.refresh()
        self.assertEqual(self.t(p).action, WAITING)
        write_lines(p, [assistant(NOW - 8, [tool("Bash", command="make")], "tool_use"),
                        user(NOW - 7, [{"type": "tool_result", "content": "[Request interrupted by user for tool use]"}])])
        self.refresh()
        self.assertEqual(self.t(p).action, WAITING)  # inside tool_result content, before the keep rule
        self.assertAlmostEqual(self.t(p).action_time, NOW - 7, delta=0.01)
        write_lines(p, [assistant(NOW - 6, [tool("Bash", command="make")], "tool_use"),
                        user(NOW - 5, [{"type": "tool_result", "content": [text("[Request interrupted by user]")]}])])
        self.refresh()
        self.assertEqual(self.t(p).action, WAITING)
        a = self.agent("agent-i")
        write_lines(a, [user(NOW - 10), user(NOW - 9, [text("[Request interrupted by user]")])])
        self.refresh()
        self.assertEqual(self.t(a).action, FINISHED)

    def test_skipped_lines_still_count_tokens(self) -> None:
        p = self.main()
        meta = json.dumps({"type": "assistant", "isMeta": True, "timestamp": iso(NOW - 10),
                           "message": {"id": "meta1", "model": "claude-opus-4", "content": [text()],
                                       "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 4}}}) + "\n"
        write_lines(p, [meta])
        self.refresh()
        self.assertIsNone(self.t(p).action)
        self.assertIn("meta1", self.store.replies)

    def test_line_without_timestamp_still_updates_action(self) -> None:
        p = self.main()
        write_lines(p, [user(NOW - 30)])
        no_ts = json.dumps({"type": "assistant", "message": {"content": [tool("Edit", file_path="/a/b.py")]}}) + "\n"
        bad_ts = json.dumps({"type": "user", "timestamp": "yesterday", "message": {"content": "go"}}) + "\n"
        write_lines(p, [no_ts])
        self.refresh()
        self.assertEqual(self.t(p).action, "Editing b.py")
        self.assertIsNone(self.t(p).action_time)
        write_lines(p, [bad_ts])
        self.refresh()
        self.assertEqual(self.t(p).action, THINKING)
        self.assertIsNone(self.t(p).action_time)

    def test_waiting_needs_text_only_content(self) -> None:
        p = self.main()
        write_lines(p, [user(NOW - 20), assistant(NOW - 10, [{"type": "thinking", "thinking": "hm"}, text()], "end_turn")])
        self.refresh()
        self.assertEqual(self.t(p).action, WAITING)  # thinking blocks are ignored
        write_lines(p, [assistant(NOW - 9, [], "end_turn")])
        self.refresh()
        self.assertEqual(self.t(p).action, THINKING)  # empty content is not text
        write_lines(p, [assistant(NOW - 8, [text(), {"type": "image"}], "end_turn")])
        self.refresh()
        self.assertEqual(self.t(p).action, THINKING)  # not only text
        write_lines(p, [assistant(NOW - 8, [text(), text("more")], "end_turn")])
        self.refresh()
        self.assertEqual(self.t(p).action, WAITING)

    def test_subagent_finishes(self) -> None:
        p = self.agent("agent-a")
        write_lines(p, [user(NOW - 20), assistant(NOW - 10, [text()], "end_turn")])
        self.refresh()
        self.assertEqual(self.t(p).action, FINISHED)

    def test_subagent_links_to_parent_session(self) -> None:
        write_lines(self.main("S1"), [user(NOW - 5)])
        a = self.agent("agent-a", "S1")
        write_lines(a, [user(NOW - 5)])
        self.refresh()
        self.assertEqual(self.t(self.main("S1")).session_id, "S1")
        self.assertEqual(self.t(a).session_id, "S1")


    def test_synthetic_text_is_done(self) -> None:  # G1
        p = self.main()
        write_lines(p, [user(NOW - 30), assistant(NOW - 20, [text("Session limit reached")], "stop_sequence",
                                                   model="<synthetic>")])
        self.refresh()
        self.assertEqual(self.t(p).action, WAITING)
        a = self.agent("agent-a")
        write_lines(a, [user(NOW - 30), assistant(NOW - 20, [{"type": "thinking"}, text("API Error")], None,
                                                   model="<synthetic>")])
        self.refresh()
        self.assertEqual(self.t(a).action, FINISHED)
        write_lines(p, [assistant(NOW - 10, [text("mid-turn")], "stop_sequence")])  # not synthetic
        self.refresh()
        self.assertEqual(self.t(p).action, THINKING)

    def test_compact_summary_skipped(self) -> None:  # G2
        p = self.main()
        write_lines(p, [assistant(NOW - 30, [text()], "end_turn")])
        summary = json.dumps({"type": "user", "isCompactSummary": True, "timestamp": iso(NOW - 20),
                              "message": {"role": "user", "content": [text("This session is being continued")]}})
        write_lines(p, [summary + "\n"])
        self.refresh()
        self.assertEqual(self.t(p).action, WAITING)
        self.assertAlmostEqual(self.t(p).action_time, NOW - 30, delta=0.01)

    def test_user_line_without_message_ignored(self) -> None:  # G3
        p = self.main()
        write_lines(p, [assistant(NOW - 30, [text()], "end_turn"),
                        json.dumps({"type": "user", "timestamp": iso(NOW - 20)}) + "\n",
                        json.dumps({"type": "user", "timestamp": iso(NOW - 15), "message": "hi"}) + "\n"])
        self.refresh()
        self.assertEqual(self.t(p).action, WAITING)
        self.assertAlmostEqual(self.t(p).action_time, NOW - 30, delta=0.01)

    def test_mixed_content_list_finds_tool_use(self) -> None:  # G4
        p = self.main()
        write_lines(p, [assistant(NOW - 20, ["junk", 3, None, tool("Read", file_path="/x/a.py"), ["x"]], "tool_use")])
        self.refresh()
        self.assertEqual(self.t(p).action, "Reading a.py")
        write_lines(p, [assistant(NOW - 10, [7, text()], "end_turn")])
        self.refresh()
        self.assertEqual(self.t(p).action, WAITING)

def _write_session(home: Path, pid: int, sid: str, status: Optional[str] = None, cwd: str = "/Users/me/app",
                   name: str = "") -> None:
    obj = {"pid": pid, "sessionId": sid, "cwd": cwd}
    if status is not None:
        obj["status"] = status
    if name:
        obj["name"] = name
    (home / "sessions" / ("%d.json" % pid)).write_text(json.dumps(obj))


class NowBlockStoreTest(ActionRulesTest):
    def test_sessions_and_running_agents(self) -> None:
        self.alive = {100, 101}
        _write_session(self.home, 100, "S1", "busy", "/Users/me/app/.claude/worktrees/w1", "my-session")
        _write_session(self.home, 101, "S2", "idle", "/Users/me/other")
        _write_session(self.home, 102, "S3", "busy")  # pid not alive
        write_lines(self.main("S1"), [user(NOW - 100), assistant(NOW - 90, [tool("Edit", file_path="/a/x.py")])],
                    mtime=NOW - 5)
        write_lines(self.main("S2"), [user(NOW - 100), assistant(NOW - 30, [text()], "end_turn")], mtime=NOW - 30)
        a = self.agent("agent-a", "S1", "Explore")
        write_lines(a, [user(NOW - 60), assistant(NOW - 50, [tool("Grep", pattern="foo")])], mtime=NOW - 5)
        done = self.agent("agent-b", "S1", "Plan")
        write_lines(done, [user(NOW - 60), assistant(NOW - 50, [text()], "end_turn")], mtime=NOW - 5)
        self.refresh()
        s = self.store.snapshot(TimeRange.HOUR, now=NOW)
        self.assertEqual([n.session_id for n in s.now_sessions], ["S1", "S2"])
        s1, s2 = s.now_sessions
        self.assertTrue(s1.busy)
        self.assertFalse(s2.busy)
        self.assertEqual(s1.project, "app")  # worktree rule
        self.assertEqual(s1.name, "my-session")
        self.assertEqual(s1.action, "Editing x.py")
        self.assertEqual(s2.action, WAITING)
        self.assertEqual([ag.span.type for ag in s1.agents], ["Explore"])  # finished agent-b left out
        self.assertEqual(s1.agents[0].action, "Searching for foo")
        self.assertEqual(s2.agents, ())
        self.assertEqual(s.more_sessions, 0)

    def test_ordering_and_cap(self) -> None:
        self.alive = {1, 2, 3, 4, 5}
        for pid, sid, status, t in ((1, "A", "idle", 50), (2, "B", "busy", 40), (3, "C", "idle", 10),
                                    (4, "D", "busy", 5), (5, "E", "idle", 1)):
            _write_session(self.home, pid, sid, status)
            write_lines(self.main(sid), [assistant(NOW - t, [text()], "end_turn")])
        self.refresh()
        s = self.store.snapshot(TimeRange.HOUR, now=NOW)
        self.assertEqual([n.session_id for n in s.now_sessions], ["D", "B", "E"])  # busy first, then newest
        self.assertEqual(s.more_sessions, 2)

    def test_no_live_session_no_block(self) -> None:
        self.alive = set()
        _write_session(self.home, 100, "S1", "busy")
        s = self.store.snapshot(TimeRange.HOUR, now=NOW)
        self.assertEqual(s.now_sessions, ())
        rows = build_rows(s, Animator(), NOW, True)
        self.assertFalse(any(text_of(r).startswith("  ● ") for r in rows))

    def test_status_absent_uses_recent_writes(self) -> None:
        _write_session(self.home, 100, "S1", None)
        write_lines(self.main("S1"), [user(NOW - 5)], mtime=NOW - 5)
        self.refresh()
        s = self.store.snapshot(TimeRange.HOUR, now=NOW)
        self.assertTrue(s.now_sessions[0].busy)
        self.assertEqual(s.busy_sessions, 1)

    def test_busy_count_matches_the_dots(self) -> None:
        # C: status "busy" wins over an old file; "idle" wins over a fresh one; no status -> 20 s.
        self.alive = {1, 2, 3, 4, 5}
        for pid, sid, status, mtime in ((1, "A", "busy", NOW - 600), (2, "B", "idle", NOW - 1),
                                        (3, "C", None, NOW - 5), (4, "D", "", NOW - 30), (5, "E", "busy", NOW - 1)):
            _write_session(self.home, pid, sid, status)
            write_lines(self.main(sid), [user(NOW - 700)], mtime=mtime)
        self.refresh()
        s = self.store.snapshot(TimeRange.HOUR, now=NOW)
        self.assertEqual(s.live_sessions, 5)
        self.assertEqual(s.busy_sessions, 3)  # A, C, E (counted over all, not just the 3 shown)
        self.assertEqual([n.busy for n in s.now_sessions], [True, True, True])
        self.assertEqual(s.more_sessions, 2)

    def test_live_count_and_now_block_share_one_listing(self) -> None:
        # F12: a pid that dies between two listings cannot make the count and the block disagree.
        calls = {"n": 0}

        def flaky(pid: int) -> bool:
            calls["n"] += 1
            return calls["n"] == 1

        store = Store(claude_home=self.home, pid_alive=flaky)
        _write_session(self.home, 100, "S1", "busy")
        s = store.snapshot(TimeRange.HOUR, now=NOW)
        self.assertEqual(calls["n"], 1)
        self.assertEqual((s.live_sessions, len(s.now_sessions), s.busy_sessions), (1, 1, 1))

    def test_project_name_sanitised(self) -> None:
        _write_session(self.home, 100, "S1", "busy", "/Users/me/ev\u202eil\u200bname")
        s = self.store.snapshot(TimeRange.HOUR, now=NOW)
        self.assertEqual(s.now_sessions[0].project, "ev il name")

    def test_running_agents_newest_first(self) -> None:
        _write_session(self.home, 100, "S1", "busy")
        write_lines(self.main("S1"), [user(NOW - 5)], mtime=NOW - 5)
        for i in range(5):
            a = self.agent("agent-%d" % i, "S1")
            write_lines(a, [assistant(NOW - 100 + i * 10, [tool("Read", file_path="/f%d" % i)], "tool_use")],
                        mtime=NOW - 5)
        self.refresh()
        s = self.store.snapshot(TimeRange.HOUR, now=NOW)
        ses = s.now_sessions[0]
        self.assertEqual([ag.action for ag in ses.agents], ["Reading f4", "Reading f3", "Reading f2"])
        self.assertEqual(ses.more_agents, 2)


def _span(i: int, start: float) -> Span:
    return Span(id="a%d" % i, type="Explore", task="t", family="opus", start=start, end=NOW, tokens=10, running=True)


    def test_tooltip_fields_sanitised(self) -> None:  # G5
        self.alive = {100, 101}
        _write_session(self.home, 100, "S1", "bu\u202esy\n", "/Users/me/a\u200bpp\x07/" + "d" * 300, "my\nses\u202esion")
        _write_session(self.home, 101, "S2", "idle", "/Users/me/\u200b")
        self.refresh()
        s = self.store.snapshot(TimeRange.HOUR, now=NOW)
        s1 = [n for n in s.now_sessions if n.session_id == "S1"][0]
        s2 = [n for n in s.now_sessions if n.session_id == "S2"][0]
        self.assertEqual(s1.name, "my ses sion")
        self.assertEqual(s1.status, "bu sy")
        self.assertEqual(len(s1.cwd), 200)
        self.assertTrue(s1.cwd.startswith("/Users/me/a pp /") and s1.cwd.endswith("…"))
        self.assertEqual(len(s1.project), 200)
        self.assertEqual(s2.project, "?")  # empty after sanitising
        line = now_lines(Snapshot(loaded=True, now_sessions=(s2,)), Animator(), NOW, True)[0]
        self.assertTrue(text_of(line).startswith("  ● ? "))
        self.assertEqual(line.tip[0], "?")

class NowBlockRowsTest(unittest.TestCase):
    def ses(self, sid: str, agents: int = 0, more: int = 0, busy: bool = True) -> NowSession:
        ags = tuple(NowAgent(_span(i, NOW - 65), "Reading a.py", NOW - 3) for i in range(agents))
        return NowSession(sid, "app", "n", "/Users/me/app", "busy" if busy else "idle", busy,
                          "Editing x.py", NOW - 12, ags, more)

    def test_rows_after_sessions_before_tokens(self) -> None:
        snap = Snapshot(loaded=True, live_sessions=1, now_sessions=(self.ses("S1", agents=2),))
        rows = build_rows(snap, Animator(), NOW, True)
        texts = [text_of(r) for r in rows]
        self.assertTrue(texts[0].startswith("Sessions"))
        self.assertEqual(texts[1], "  ● app · Editing x.py · 12s")
        self.assertEqual(texts[2], "    ├─ ◐ Explore: Reading a.py · 1m5s")
        self.assertEqual(texts[3], "    └─ ◐ Explore: Reading a.py · 1m5s")
        self.assertTrue(texts[4].startswith("Tokens"))
        self.assertEqual(rows[1].segs[1].role, "warn")
        # F5: project in the text colour, action and elapsed dim, busy only colours the dot
        self.assertEqual([seg.role for seg in rows[1].segs[2:]], ["text", "dim", "dim"])
        tip = rows[1].tip
        self.assertIn("app", tip)
        self.assertIn("Session: n", tip)
        self.assertIn("/Users/me/app", tip)
        self.assertIn("Status: busy", tip)
        self.assertIn("Editing x.py", tip)
        self.assertIn("since " + fmt.clock(NOW - 12), tip)
        self.assertEqual(rows[2].tip[:2], ["Explore", "t"])
        self.assertEqual(rows[2].tip[-1], "Now: Reading a.py")  # F3
        # G6: "<type>" normal, ": <action> · " dim, run time warn
        self.assertEqual([(seg.text, seg.role) for seg in rows[2].segs[2:]],
                         [("Explore", "text"), (": Reading a.py · ", "dim"), ("1m5s", "warn")])

    def test_caps_and_more_lines(self) -> None:
        snap = Snapshot(loaded=True, now_sessions=(self.ses("S1", agents=3, more=2), self.ses("S2", busy=False)),
                        more_sessions=4)
        texts = [text_of(r) for r in now_lines(snap, Animator(), NOW, False)]
        self.assertEqual(len(texts), 7)
        self.assertTrue(all(t.startswith("    ├─ ◓ ") for t in texts[1:4]))
        self.assertEqual(texts[4], "    └─ +2 more")
        self.assertEqual(texts[6], "  +4 more sessions")
        idle = now_lines(snap, Animator(), NOW, False)[5]
        self.assertEqual(idle.segs[1].role, "dim")
        self.assertEqual([seg.role for seg in idle.segs[2:]], ["text", "dim", "dim"])

    def test_session_without_action(self) -> None:
        ses = NowSession("S", "app", "", "", "", False, None, None)
        self.assertEqual(text_of(now_lines(Snapshot(loaded=True, now_sessions=(ses,)), Animator(), NOW, True)[0]),
                         "  ● app")
        ses = NowSession("S", "app", "", "", "", False, "Reading a.py", None)  # F8: no time, no elapsed
        self.assertEqual(text_of(now_lines(Snapshot(loaded=True, now_sessions=(ses,)), Animator(), NOW, True)[0]),
                         "  ● app · Reading a.py")

    def test_agent_without_action(self) -> None:
        ag = NowAgent(_span(0, NOW - 65), None, None)
        ses = NowSession("S", "app", "", "", "", True, None, None, (ag,))
        line = now_lines(Snapshot(loaded=True, now_sessions=(ses,)), Animator(), NOW, True)[1]
        self.assertEqual(text_of(line), "    └─ ◐ Explore · 1m5s")  # F2: no ":" without an action
        self.assertFalse(any(t.startswith("Now:") for t in line.tip))
        self.assertEqual([(seg.text, seg.role) for seg in line.segs[2:]],
                         [("Explore", "text"), (" · ", "dim"), ("1m5s", "warn")])

    def test_clipping_at_row_time(self) -> None:
        long = "Using " + "s" * 39 + "…: " + "t" * 39 + "…"
        ag = NowAgent(_span(0, NOW - 65), long, NOW - 1)
        ses = NowSession("S", "a-very-long-project-name", "", "", "", True, long, None, (ag,))
        lines = now_lines(Snapshot(loaded=True, now_sessions=(ses,)), Animator(), NOW, True)
        self.assertEqual(text_of(lines[0]), "  ● a-very-long-pro… · " + clip_text(long, 48))
        self.assertEqual(len(clip_text(long, 48)), 48)
        self.assertEqual(text_of(lines[1]), "    └─ ◐ Explore: " + clip_text(long, 40) + " · 1m5s")
        self.assertIn(long, lines[0].tip)  # tooltips carry the full action
        self.assertEqual(lines[1].tip[-1], "Now: " + long)

    def test_agent_lines_fade_in_like_the_agents_row(self) -> None:
        span = _span(7, NOW - 65)
        anim = Animator()
        anim.first_seen[span.id] = NOW - anim.FADE_SECONDS / 2
        ses = NowSession("S", "app", "", "", "", True, None, None, (NowAgent(span, "x", NOW),))
        snap = Snapshot(loaded=True, now_sessions=(ses,), agents_today=[span])
        rows = build_rows(snap, anim, NOW, True)
        sub = [r for r in rows if text_of(r).startswith("    └─ ")][0]
        agent_row = [r for r in rows if text_of(r).startswith("  ◐ Explore")][0]
        self.assertAlmostEqual(sub.alpha, 0.5, places=3)
        self.assertEqual(sub.alpha, agent_row.alpha)


class LimitsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = make_home(Path(self._tmp.name))
        self.path = limits_path(self.home)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, obj) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(obj if isinstance(obj, str) else json.dumps(obj))

    def test_path(self) -> None:
        self.assertEqual(self.path, self.home / "claudemon" / "limits.json")

    def test_valid(self) -> None:
        self.write({"version": 1, "updated_at": NOW - 7,
                    "five_hour": {"used_percentage": 23.5, "resets_at": int(NOW + 3600)},
                    "seven_day": {"used_percentage": 41, "resets_at": int(NOW + 86400)}})
        lim = read_limits(self.path, NOW)
        self.assertEqual(lim.five_hour.used_percentage, 23.5)
        self.assertEqual(lim.seven_day.resets_at, int(NOW + 86400))
        # also reaches the snapshot through the store
        s = Store(claude_home=self.home, pid_alive=lambda p: False).snapshot(TimeRange.HOUR, now=NOW)
        self.assertEqual(s.limits, lim)

    def test_expired_window_dropped(self) -> None:
        self.write({"version": 1, "updated_at": NOW,
                    "five_hour": {"used_percentage": 23, "resets_at": NOW - 1},
                    "seven_day": {"used_percentage": 41, "resets_at": NOW + 100}})
        lim = read_limits(self.path, NOW)
        self.assertIsNone(lim.five_hour)
        self.assertIsNotNone(lim.seven_day)
        self.write({"version": 1, "updated_at": NOW, "five_hour": {"used_percentage": 23, "resets_at": NOW}})
        self.assertIsNone(read_limits(self.path, NOW))

    def test_missing(self) -> None:
        self.assertIsNone(read_limits(self.path, NOW))

    def test_malformed(self) -> None:
        bad = [
            "not json", "[1, 2]", {"version": 2, "updated_at": NOW, "five_hour": {"used_percentage": 1, "resets_at": NOW + 9}},
            {"updated_at": NOW, "five_hour": {"used_percentage": 1, "resets_at": NOW + 9}},
            {"version": 1, "five_hour": {"used_percentage": 1, "resets_at": NOW + 9}},
            {"version": 1, "updated_at": "x", "five_hour": {"used_percentage": 1, "resets_at": NOW + 9}},
            {"version": 1, "updated_at": NOW, "five_hour": {"used_percentage": "5", "resets_at": NOW + 9}},
            {"version": 1, "updated_at": NOW, "five_hour": {"used_percentage": True, "resets_at": NOW + 9}},
            {"version": 1, "updated_at": NOW, "five_hour": {"used_percentage": 101, "resets_at": NOW + 9}},
            {"version": 1, "updated_at": NOW, "five_hour": {"used_percentage": -1, "resets_at": NOW + 9}},
            {"version": 1, "updated_at": NOW, "five_hour": {"used_percentage": 5}},
            # one malformed window spoils the whole file, even with a valid other window
            {"version": 1, "updated_at": NOW, "five_hour": {"used_percentage": 5, "resets_at": NOW + 9},
             "seven_day": {"used_percentage": "41", "resets_at": NOW + 9}},
            {"version": 1, "updated_at": NOW, "five_hour": [5, NOW + 9]},
            {"version": 1, "updated_at": NOW},
        ]
        for obj in bad:
            with self.subTest(obj=obj):
                self.write(obj)
                self.assertIsNone(read_limits(self.path, NOW))
        self.write('{"version": 1, "updated_at": NaN, "five_hour": {"used_percentage": 1, "resets_at": %d}}' % (NOW + 9))
        self.assertIsNone(read_limits(self.path, NOW))

    def test_oversized(self) -> None:
        obj = {"version": 1, "updated_at": NOW, "five_hour": {"used_percentage": 1, "resets_at": NOW + 9}}
        self.write(json.dumps(obj) + " " * LIMITS_CAP)
        self.assertIsNone(read_limits(self.path, NOW))

    def test_not_a_regular_file(self) -> None:
        self.path.mkdir(parents=True)
        self.assertIsNone(read_limits(self.path, NOW))

    def test_row(self) -> None:
        five = LimitWindow(22.5, NOW + 3600)
        week = LimitWindow(90.0, NOW + 3 * 86400)
        snap = Snapshot(loaded=True, limits=Limits(NOW - 12, five, week))
        rows = build_rows(snap, Animator(), NOW, True)
        texts = [text_of(r) for r in rows]
        i = texts.index(text_of(limits_line(snap.limits, NOW)))
        self.assertTrue(texts[i - 1].strip().startswith("in "))  # right after the Tokens rows
        want = "Limits    5h 23% · resets {} · week 90% · resets {} {}".format(
            fmt.hm(five.resets_at), time.strftime("%a", time.localtime(week.resets_at)), fmt.hm(week.resets_at))
        self.assertEqual(texts[i], want)
        line = rows[i]
        roles = {seg.text: seg.role for seg in line.segs}
        self.assertEqual(roles["23%"], "text")
        self.assertEqual(roles["90%"], "hot")
        self.assertEqual(line.tip[0], "Official Claude usage from Claude Code's status line")
        self.assertEqual(line.tip[1], "Updated 12s ago")
        self.assertTrue(line.tip[2].startswith("5-hour: 22.5% used, resets "))  # F13
        self.assertTrue(line.tip[3].startswith("7-day: 90% used, resets "))

    def test_row_single_window_and_warn(self) -> None:
        line = limits_line(Limits(NOW, None, LimitWindow(70.0, NOW + 60)), NOW)
        self.assertTrue(text_of(line).startswith("Limits    week 70% · resets "))
        self.assertEqual([s.role for s in line.segs if s.text == "70%"], ["warn"])
        line = limits_line(Limits(NOW, LimitWindow(69.4, NOW + 60), None), NOW)
        self.assertEqual([s.role for s in line.segs if s.text == "69%"], ["text"])
        line = limits_line(Limits(NOW, LimitWindow(69.5, NOW + 60), None), NOW)  # rounded percent decides
        self.assertEqual([s.role for s in line.segs if s.text == "70%"], ["warn"])
        line = limits_line(Limits(NOW, LimitWindow(89.5, NOW + 60), None), NOW)
        self.assertEqual([s.role for s in line.segs if s.text == "90%"], ["hot"])

    def test_reset_bounds(self) -> None:
        # E/F7: a window counts only when now < resets_at <= now + 8 days; others still count.
        day = 86400
        self.write({"version": 1, "updated_at": NOW, "seven_day": {"used_percentage": 5, "resets_at": 1e300}})
        self.assertIsNone(read_limits(self.path, NOW))
        self.write({"version": 1, "updated_at": NOW,
                    "five_hour": {"used_percentage": 5, "resets_at": NOW + 8 * day + 1},
                    "seven_day": {"used_percentage": 6, "resets_at": NOW + 8 * day}})
        lim = read_limits(self.path, NOW)
        self.assertIsNone(lim.five_hour)
        self.assertEqual(lim.seven_day.used_percentage, 6)

    def test_null_window_is_absent(self) -> None:
        self.write({"version": 1, "updated_at": NOW, "five_hour": None,
                    "seven_day": {"used_percentage": 6, "resets_at": NOW + 60}})
        lim = read_limits(self.path, NOW)
        self.assertIsNone(lim.five_hour)
        self.assertIsNotNone(lim.seven_day)

    def test_updated_at_bounds(self) -> None:
        win = {"used_percentage": 6, "resets_at": NOW + 60}
        for updated, ok in ((NOW + 60, True), (NOW + 61, False), (0, False), (-5, False), (1e300, False), (1, True)):
            with self.subTest(updated=updated):
                self.write({"version": 1, "updated_at": updated, "five_hour": win})
                self.assertEqual(read_limits(self.path, NOW) is not None, ok)

    def test_far_future_reset_formats_safely(self) -> None:
        line = limits_line(Limits(NOW, None, LimitWindow(5, 1e300)), NOW)
        self.assertIn("resets ?", text_of(line))
        self.assertTrue(line.tip[2].endswith("resets ?"))

    def test_tooltip_reset_format(self) -> None:
        t = time.mktime((2026, 10, 1, 2, 52, 0, 0, 0, -1))
        line = limits_line(Limits(t - 100, LimitWindow(12.25, t), None), t - 100)
        self.assertEqual(line.tip[2], "5-hour: 12.25% used, resets Thu 1 Oct 02:52:00")

    def test_no_limits_no_row_and_estimate_kept(self) -> None:
        snap = Snapshot(loaded=True, window=100, window_reset=NOW + 60)
        texts = [text_of(r) for r in build_rows(snap, Animator(), NOW, True)]
        self.assertFalse(any(t.startswith("Limits") for t in texts))
        self.assertTrue(any("resets ~" in t for t in texts))


if __name__ == "__main__":
    unittest.main()
