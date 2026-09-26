"""The whole data engine: discovery, incremental read, snapshot math. Used only on the worker thread.

data-flow.md §2-§6, claudemon.swift 82-285 (`Store`).

Security-sensitive (architecture.md security-sensitive area #3): every input file is untrusted and
user-writable. Directories are walked without following symlinks, hidden entries are skipped, only
regular files are opened, lines are parsed with json.loads only, every per-line error is caught, and a
single line without a newline can never grow the partial buffer past PARTIAL_CAP.
"""
from __future__ import annotations

import datetime
import json
import math
import os
import re
import stat
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .formatting import clip_text, sanitize

from .model import (
    Limits,
    LimitWindow,
    ModelTotal,
    NowAgent,
    NowSession,
    ProjectTotal,
    Reply,
    Snapshot,
    Span,
    TimeRange,
    Transcript,
    Usage,
    family,
    project_name,
)
from .timeparse import parse_timestamp

_FIVE_HOURS = 5 * 3600.0
_META_CAP = 1024 * 1024  # a subagent .meta.json larger than this is ignored
_SEVEN = range(7)  # snapshot()'s `range` parameter (contract name) shadows the builtin there


def _int(u: Dict[str, Any], key: str) -> int:
    """u[key] if it's a real int (not a bool), else 0. Swift `as? Int ?? 0`."""
    v = u.get(key)
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    return 0


def _local_midnight(d: datetime.date) -> float:
    """Epoch seconds of local midnight on d (architecture D9: like Swift Calendar.startOfDay)."""
    return datetime.datetime(d.year, d.month, d.day).timestamp()


def _is_regular(path: str) -> Optional[os.stat_result]:
    """stat(path) if it is a regular file (never a FIFO/device that could block or be endless)."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return st if stat.S_ISREG(st.st_mode) else None


# ---- current action (activity spec §1) -----------------------------------------------------------

VALUE_CAP = 40  # every untrusted value inserted into an action; rows.py clips the whole action
THINKING = "Thinking…"
WAITING = "Waiting for you"
FINISHED = "Finished"
INTERRUPT = "[Request interrupted by user"
_SESSION_JSON_CAP = 64 * 1024
LIMITS_CAP = 64 * 1024
RESET_HORIZON = 8 * 86400  # a limit window never resets further ahead than this
UPDATED_SLACK = 60.0  # updated_at may be at most this far in the future (clock skew)
BUSY_SECONDS = 20.0  # the busy fallback when a session's status is missing
NOW_SESSIONS_MAX = 3
NOW_AGENTS_MAX = 3
TIP_CAP = 200  # session tooltip fields (G5)


def _value(v: Any) -> Optional[str]:
    """A sanitised, clipped string field of a tool input, or None when missing/empty."""
    if not isinstance(v, str):
        return None
    s = clip_text(sanitize(v), VALUE_CAP)
    return s or None


def _base(v: Any) -> Optional[str]:
    if not isinstance(v, str):
        return None
    return _value(re.split(r"[\\/]", v.rstrip("/\\"))[-1])


def _host(v: Any) -> Optional[str]:
    if not isinstance(v, str):
        return None
    try:
        return _value(urllib.parse.urlsplit(v.strip()).hostname)
    except ValueError:
        return None


def tool_action(block: Dict[str, Any]) -> str:
    """Action text for one tool_use block (activity spec §1 table). Each inserted value is capped
    at VALUE_CAP; the whole action is stored in full and clipped only when rows are built (F6)."""
    raw = block.get("name")
    name = raw if isinstance(raw, str) else ""
    inp = block.get("input")
    if not isinstance(inp, dict):
        inp = {}
    text: Optional[str] = None
    if name in ("Edit", "MultiEdit"):
        v = _base(inp.get("file_path"))
        text = v and "Editing " + v
    elif name == "NotebookEdit":
        v = _base(inp.get("notebook_path"))
        text = v and "Editing " + v
    elif name == "Write":
        v = _base(inp.get("file_path"))
        text = v and "Writing " + v
    elif name == "Read":
        v = _base(inp.get("file_path"))
        text = v and "Reading " + v
    elif name == "Bash":
        v = _value(inp.get("description"))
        if v is None:
            cmd = inp.get("command")
            if isinstance(cmd, str) and cmd.strip():
                v = _value(cmd.strip().splitlines()[0])
        text = v and "Running " + v
    elif name == "Grep":
        v = _value(inp.get("pattern"))
        text = v and "Searching for " + v
    elif name == "Glob":
        v = _value(inp.get("pattern"))
        text = v and "Finding " + v
    elif name in ("Agent", "Task"):
        v = _value(inp.get("description")) or _value(inp.get("subagent_type"))
        text = v and "Starting agent: " + v
    elif name == "WebFetch":
        v = _host(inp.get("url"))
        text = v and "Reading " + v
    elif name == "WebSearch":
        v = _value(inp.get("query"))
        text = v and "Searching the web: " + v
    elif name == "TodoWrite":
        text = "Updating the plan"
    elif name == "Skill":
        v = _value(inp.get("skill"))
        text = v and "Using skill " + v
    elif name.startswith("mcp__"):
        server, _, tool = name[5:].partition("__")
        s, t = _value(server), _value(tool)
        text = s and t and "Using {}: {}".format(s, t)
    if not text:
        v = _value(name)
        text = "Using " + v if v else "Using a tool"
    return text


def _only_tool_results(content: Any) -> bool:
    return (
        isinstance(content, list)
        and len(content) > 0
        and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    )


def _interrupted(content: Any, depth: int = 0) -> bool:
    """True when a user line contains Claude Code's interrupt marker: in string content, in a text
    block, or inside a tool_result's content (same places as the macOS app)."""
    if isinstance(content, str):
        return INTERRUPT in content
    if not isinstance(content, list) or depth > 1:
        return False
    for b in content:
        if not isinstance(b, dict):
            continue
        kind = b.get("type")
        if kind == "text" and isinstance(b.get("text"), str) and INTERRUPT in b["text"]:
            return True
        if kind == "tool_result" and _interrupted(b.get("content"), depth + 1):
            return True
    return False


def _update_action(t: Transcript, kind: Any, msg: Dict[str, Any], ts: Any, skip: bool = False) -> None:
    """Advance t's current action for one assistant/user line (activity spec §1, amendments A and G).

    A line without a valid timestamp still updates the action; its time is then unknown (F8)."""
    if skip:
        return  # isMeta or isCompactSummary (G2)
    content = msg.get("content")
    if isinstance(content, str) and content.startswith(("<local-command", "<command-")):
        return  # slash-command noise, compact summaries
    when = parse_timestamp(ts) if isinstance(ts, str) else None
    done = FINISHED if t.is_agent else WAITING
    if kind == "assistant":
        # G4: skip non-object items and evaluate the rest
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        tools = [b for b in blocks if b.get("type") == "tool_use"]
        if tools:
            t.action = tool_action(tools[-1])
        else:
            rest = [b for b in blocks if b.get("type") != "thinking"]
            text_only = len(rest) > 0 and all(b.get("type") == "text" for b in rest)
            # G1: a synthetic text reply (session limit, API error) ends the turn whatever its stop_reason
            if text_only and (msg.get("stop_reason") == "end_turn" or msg.get("model") == "<synthetic>"):
                t.action = done
            else:
                t.action = THINKING  # thinking only, or text in the middle of a turn
    elif _interrupted(content):  # checked before the tool_result-only rule
        t.action = done
    elif _only_tool_results(content):
        return  # the tool just returned: keep the action and its time
    else:
        t.action = THINKING
    t.action_time = when


# ---- live sessions and official limits (activity spec §2, §3) -------------------------------------


def _read_json_capped(path: str, cap: int) -> Optional[Dict[str, Any]]:
    """A regular file's JSON object if it is at most cap bytes and parses; else None. Never raises."""
    if _is_regular(path) is None:
        return None
    try:
        with open(path, "rb") as f:
            data = f.read(cap + 1)
    except OSError:
        return None
    if len(data) > cap:
        return None
    try:
        obj = json.loads(data)
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def live_sessions(sessions_dir: Path, pid_alive: Callable[[int], bool]) -> List[Dict[str, str]]:
    """{session_id, name, cwd, status} (strings, "" when absent/invalid) for each <pid>.json whose pid
    is alive. Same file and pid rules as liveness.live_session_count. Never raises."""
    try:
        names = sorted(os.listdir(sessions_dir))
    except (OSError, TypeError, ValueError):
        return []
    out = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            pid = int(name[:-5])
        except ValueError:
            continue
        if pid <= 0:
            continue
        try:
            if not pid_alive(pid):
                continue
        except Exception:
            continue
        obj = _read_json_capped(os.path.join(str(sessions_dir), name), _SESSION_JSON_CAP) or {}
        info = {}
        for key, src in (("session_id", "sessionId"), ("name", "name"), ("cwd", "cwd"), ("status", "status")):
            v = obj.get(src)
            info[key] = v if isinstance(v, str) else ""
        out.append(info)
    return out


def _number(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


class _Malformed(Exception):
    pass


def _limit_window(v: Any) -> Optional[LimitWindow]:
    """None when the window is absent; raises _Malformed when it is present but invalid."""
    if v is None:
        return None
    if not isinstance(v, dict):
        raise _Malformed
    pct = _number(v.get("used_percentage"))
    resets = _number(v.get("resets_at"))
    if pct is None or resets is None or not 0 <= pct <= 100:
        raise _Malformed
    return LimitWindow(pct, resets)


def read_limits(path: Path, now: float) -> Optional[Limits]:
    """The official limits file (activity spec §3, amendments E and F7), or None when missing,
    oversized or malformed (one window with wrong types spoils the file), when updated_at is not in
    (0, now + 60], or when no window counts. A window counts only while now < resets_at <= now + 8
    days; one outside that range is dropped while the other still counts. Never raises."""
    obj = _read_json_capped(str(path), LIMITS_CAP)
    if obj is None:
        return None
    version = obj.get("version")
    updated = _number(obj.get("updated_at"))
    if isinstance(version, bool) or version != 1 or updated is None or not 0 < updated <= now + UPDATED_SLACK:
        return None
    try:
        windows = [_limit_window(obj.get(k)) for k in ("five_hour", "seven_day")]
    except _Malformed:
        return None
    five, seven = [w if w is not None and now < w.resets_at <= now + RESET_HORIZON else None for w in windows]
    if five is None and seven is None:
        return None
    return Limits(updated, five, seven)


class Store:
    RETENTION: float = 7 * 86400
    DISCOVERY_INTERVAL: float = 3.0
    PARTIAL_CAP: int = 64 * 1024 * 1024
    READ_CHUNK: int = 8 * 1024 * 1024  # bytes read per step; bounds memory on a huge first read

    # Instance attributes set by __init__ (documented here; see components.md):
    projects_dir: Path
    sessions_dir: Path
    transcripts: Dict[str, Transcript]  # read-only for tests
    replies: Dict[str, Reply]  # read-only for tests

    def __init__(
        self,
        claude_home: Optional[Path] = None,
        pid_alive: Optional[Callable[[int], bool]] = None,
    ) -> None:
        """claude_home defaults to paths.claude_home(); pid_alive defaults to liveness.pid_alive."""
        if claude_home is None:
            from .paths import claude_home as default_home

            claude_home = default_home()
        if pid_alive is None:
            from .liveness import pid_alive as default_pid_alive

            pid_alive = default_pid_alive
        self.projects_dir = Path(claude_home) / "projects"
        self.sessions_dir = Path(claude_home) / "sessions"
        from .paths import limits_path

        self.limits_path = limits_path(Path(claude_home))
        self._pid_alive = pid_alive
        self.transcripts = {}
        self.replies = {}  # keyed by message id: streamed replies repeat the same id
        self._last_discovery = float("-inf")
        # Paths whose over-long partial line was dropped: skip bytes up to the next newline.
        self._skipping: Set[str] = set()

    # ---- refresh: discovery + incremental read -------------------------------------------------

    def refresh(self, now: Optional[float] = None, force_discover: bool = False) -> None:
        """Discover new transcripts and incrementally read changed ones. data-flow.md §2-§3.

        Inputs: files under claude_home. Never raises on I/O or bad data.
        """
        # claudemon.swift 95-107
        now = time.time() if now is None else now
        cutoff = now - self.RETENTION
        if force_discover or now - self._last_discovery > self.DISCOVERY_INTERVAL:
            self._discover(cutoff)
            self._last_discovery = now
        for path, t in self.transcripts.items():
            st = _is_regular(path)
            if st is None:
                continue
            key = (st.st_mtime_ns, st.st_size)
            if key == t.stat_key:  # Swift compares mtime only; adding size is harmless
                continue
            t.stat_key = key
            t.mtime = st.st_mtime
            if not self._read(path, t, cutoff):
                t.stat_key = None  # I/O error: retry on the next poll
        self.replies = {k: r for k, r in self.replies.items() if r.time >= cutoff}

    def _discover(self, cutoff: float) -> None:
        # claudemon.swift 109-130
        if not self.projects_dir.is_dir():
            return
        for dirpath, dirnames, filenames in os.walk(str(self.projects_dir), followlinks=False):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]  # Swift .skipsHiddenFiles
            for name in filenames:
                if name.startswith(".") or not name.endswith(".jsonl"):
                    continue
                path = os.path.join(dirpath, name)
                if path in self.transcripts:
                    continue
                st = _is_regular(path)
                if st is None or st.st_mtime < cutoff:
                    continue
                is_agent = os.path.basename(dirpath) == "subagents"
                t = Transcript(is_agent=is_agent)
                # activity spec §2: <project-dir>/<sessionId>.jsonl and
                # <project-dir>/<sessionId>/subagents/agent-*.jsonl share the same session id.
                t.session_id = os.path.basename(os.path.dirname(dirpath)) if is_agent else name[: -len(".jsonl")]
                if is_agent:
                    # Swift replaces the extension: agent-x.jsonl -> agent-x.meta.json
                    meta = self._read_meta(path[: -len(".jsonl")] + ".meta.json")
                    if meta is not None:
                        v = meta.get("agentType")
                        t.agent_type = v if isinstance(v, str) else "agent"
                        v = meta.get("description")
                        t.agent_task = v if isinstance(v, str) else ""
                self.transcripts[path] = t

    @staticmethod
    def _read_meta(path: str) -> Optional[Dict[str, Any]]:
        if _is_regular(path) is None:
            return None
        try:
            with open(path, "rb") as f:
                data = f.read(_META_CAP + 1)
        except OSError:
            return None
        if len(data) > _META_CAP:
            return None
        try:
            obj = json.loads(data)
        except (ValueError, RecursionError):
            return None
        return obj if isinstance(obj, dict) else None

    def _read(self, path: str, t: Transcript, cutoff: float) -> bool:
        """Read new bytes of path from t.offset. False on an I/O error (offset kept consistent)."""
        # claudemon.swift 132-163, read in bounded chunks
        try:
            with open(path, "rb") as f:
                size = f.seek(0, 2)
                if size < t.offset:  # file was rewritten/truncated
                    t.offset = 0
                    t.partial = b""
                    self._skipping.discard(path)
                f.seek(t.offset)
                while True:
                    chunk = f.read(self.READ_CHUNK)
                    if not chunk:
                        break
                    t.offset += len(chunk)
                    self._feed(path, t, chunk, cutoff)
        except OSError:
            return False
        return True

    def _feed(self, path: str, t: Transcript, chunk: bytes, cutoff: float) -> None:
        """Append chunk to the partial line, parse every complete line, keep the rest (capped)."""
        if path in self._skipping:
            nl = chunk.find(b"\n")
            if nl < 0:
                return
            chunk = chunk[nl + 1 :]
            self._skipping.discard(path)
        data = t.partial + chunk
        nl = data.rfind(b"\n")
        tail = data if nl < 0 else data[nl + 1 :]
        if len(tail) > self.PARTIAL_CAP:  # architecture security #3: drop, skip to next newline
            t.partial = b""
            self._skipping.add(path)
        else:
            t.partial = tail
        if nl < 0:
            return
        for line in data[:nl].split(b"\n"):
            if line.strip():
                self._parse_line(path, t, line, cutoff)

    def _parse_line(self, path: str, t: Transcript, line: bytes, cutoff: float) -> None:
        # claudemon.swift 146-162
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
            return
        if not isinstance(obj, dict):
            return
        cwd = obj.get("cwd")
        if t.project == "?" and isinstance(cwd, str):
            t.project = project_name(cwd)  # where the session started
        msg = obj.get("message")
        kind = obj.get("type")
        ts = obj.get("timestamp")
        if kind in ("user", "assistant") and isinstance(msg, dict):  # G3: no message, no change
            _update_action(t, kind, msg, ts, obj.get("isMeta") is True or obj.get("isCompactSummary") is True)
        if kind != "assistant" or not isinstance(msg, dict):
            return
        u = msg.get("usage")
        model = msg.get("model")
        if not isinstance(u, dict) or not isinstance(model, str) or model == "<synthetic>":
            return
        if not isinstance(ts, str):
            return
        when = parse_timestamp(ts)
        if when is None:
            return
        usage = Usage(
            _int(u, "input_tokens"),
            _int(u, "cache_creation_input_tokens"),
            _int(u, "cache_read_input_tokens"),
            _int(u, "output_tokens"),
        )
        stop = msg.get("stop_reason")
        t.last_stop = stop if isinstance(stop, str) else None
        t.last_context = usage.input + usage.cache_read + usage.cache_write
        t.last_reply = when
        if when < cutoff:
            return
        rid = msg.get("id")
        if not isinstance(rid, str):
            rid = obj.get("uuid")
            if not isinstance(rid, str):
                rid = uuid.uuid4().hex
        self.replies[rid] = Reply(when, model, usage, path)  # same id again: overwritten, counted once

    # ---- snapshot -------------------------------------------------------------------------------

    def snapshot(self, range: TimeRange, now: Optional[float] = None) -> Snapshot:
        """Compute a Snapshot for range from the current transcripts/replies. data-flow.md §5.

        Never raises on I/O or bad data.
        """
        # claudemon.swift 178-285
        now = time.time() if now is None else now
        s = Snapshot(loaded=True, range=range, now=now)
        # One listing of ~/.claude/sessions feeds the live count, the busy count and the Now block
        # (amendment C, F12), so they never disagree.
        infos = live_sessions(self.sessions_dir, self._pid_alive)
        s.live_sessions = len(infos)  # data-flow §6

        mains = [t for t in self.transcripts.values() if not t.is_agent]
        cands = [t for t in mains if t.last_reply is not None]
        if cands:
            cur = max(cands, key=lambda t: t.mtime if t.mtime is not None else float("-inf"))
            s.context = cur.last_context
            s.context_project = cur.project

        today = datetime.date.fromtimestamp(now)
        start_of_day = _local_midnight(today)
        replies = sorted(self.replies.values(), key=lambda r: r.time)

        # claudemon.swift 196-205: per-transcript totals for the agent rows and timeline.
        per_file: Dict[str, List[Any]] = {}  # path -> [first, last, tokens, model]
        for r in replies:
            f = per_file.get(r.file)
            if f is None:
                per_file[r.file] = [r.time, r.time, r.usage.total, r.model]
            else:
                f[1] = r.time
                f[2] += r.usage.total
                f[3] = r.model

        # claudemon.swift 207-217: today, per model, per project.
        by_model: Dict[str, List[int]] = {}
        by_project: Dict[str, int] = {}
        for r in replies:
            if r.time < start_of_day:
                continue
            total = r.usage.total
            s.today.add(r.usage)
            s.today_replies += 1
            m = by_model.setdefault(family(r.model), [0, 0])
            m[0] += total
            m[1] += 1
            t = self.transcripts.get(r.file)
            proj = t.project if t is not None else "?"
            by_project[proj] = by_project.get(proj, 0) + total
        s.models = sorted(
            (ModelTotal(k, v[0], v[1]) for k, v in by_model.items()), key=lambda x: (-x.tokens, x.name)
        )
        s.projects = sorted(
            (ProjectTotal(k, v) for k, v in by_project.items()), key=lambda x: (-x.tokens, x.name)
        )

        # claudemon.swift 219-228: 5h window starts at the first reply at/after previous start + 5h.
        ws: Optional[float] = None
        for r in replies:
            if ws is None or r.time >= ws + _FIVE_HOURS:
                ws = r.time
        if ws is not None and now < ws + _FIVE_HOURS:
            s.window_start = ws
            s.window_reset = ws + _FIVE_HOURS
            s.window = sum(r.usage.total for r in replies if r.time >= ws)

        # claudemon.swift 230-241: last hour per minute, speed, peak.
        minutes = [0] * 60
        per = 0
        for r in reversed(replies):
            age = now - r.time
            if age >= 3600:
                break
            if age < 0:
                continue
            minutes[59 - int(age // 60)] += r.usage.total
            if age < 300:
                per += r.usage.total
        s.per_minute = per // 5
        s.last_hour = minutes
        s.peak_per_minute = max(minutes)

        # claudemon.swift 243-258: chart buckets, tokens per minute.
        if range != TimeRange.WEEK:
            n = range.buckets
            bucket = range.seconds / n
            main = [0.0] * n
            agents = [0.0] * n
            for r in reversed(replies):
                age = now - r.time
                if age >= range.seconds:
                    break
                if age < 0:
                    continue
                i = n - 1 - min(n - 1, int(age // bucket))
                t = self.transcripts.get(r.file)
                (agents if t is not None and t.is_agent else main)[i] += r.usage.total
            s.bucket_seconds = bucket
            s.main = [v / (bucket / 60) for v in main]
            s.agent_series = [v / (bucket / 60) for v in agents]

        # claudemon.swift 260-269: heatmap, tokens per local hour over the last 7 days.
        first_date = today - datetime.timedelta(days=6)
        first_day = _local_midnight(first_date)
        s.heat_days = [first_date + datetime.timedelta(days=k) for k in _SEVEN]
        heat = [[0] * 24 for _ in _SEVEN]
        for r in replies:
            if r.time < first_day:
                continue
            try:
                dt = datetime.datetime.fromtimestamp(r.time)
            except (OverflowError, OSError, ValueError):  # absurd future timestamps
                continue
            d = (dt.date() - first_date).days
            if 0 <= d < 7:
                heat[d][dt.hour] += r.usage.total
        s.heat = heat

        # claudemon.swift 271-283: agent spans.
        spans: List[Span] = []
        for path, t in self.transcripts.items():
            f = per_file.get(path)
            if not t.is_agent or f is None:
                continue
            running = t.mtime is not None and now - t.mtime < 90 and t.last_stop != "end_turn"
            spans.append(
                Span(
                    id=path,
                    type=t.agent_type,
                    task=t.agent_task,
                    family=family(f[3]),
                    start=f[0],
                    end=now if running else f[1],
                    tokens=f[2],
                    running=running,
                )
            )
        s.agents_today = sorted(
            (x for x in spans if x.end >= start_of_day), key=lambda x: (x.running, x.start), reverse=True
        )
        if range != TimeRange.WEEK:
            frm = now - range.seconds
            s.spans = sorted((x for x in spans if x.end >= frm), key=lambda x: x.start)

        s.now_sessions, s.more_sessions, s.busy_sessions = self._now_block(infos, spans, now)
        s.limits = read_limits(self.limits_path, now)
        return s

    def _now_block(
        self, infos: List[Dict[str, str]], spans: List[Span], now: float
    ) -> Tuple[Tuple[NowSession, ...], int, int]:
        """Live sessions with their running subagents (activity spec §2): busy first, then latest
        action; at most NOW_SESSIONS_MAX, plus the count left out and the busy count over every
        live session (the same rule as the Now dots, amendment C)."""
        if not infos:
            return (), 0, 0
        mains: Dict[str, Transcript] = {}
        for t in self.transcripts.values():
            if t.is_agent or not t.session_id:
                continue
            cur = mains.get(t.session_id)
            if cur is None or (t.mtime or 0.0) > (cur.mtime or 0.0):
                mains[t.session_id] = t
        running: Dict[str, List[Span]] = {}
        for x in spans:
            at = self.transcripts.get(x.id)
            if x.running and at is not None and at.session_id:
                running.setdefault(at.session_id, []).append(x)
        out: List[NowSession] = []
        for info in infos:
            sid = info["session_id"]
            t = mains.get(sid) if sid else None
            if info["status"]:
                busy = info["status"] == "busy"
            else:
                busy = t is not None and t.mtime is not None and now - t.mtime < BUSY_SECONDS
            cwd = clip_text(sanitize(info["cwd"]), TIP_CAP)
            project = project_name(info["cwd"]) if info["cwd"] else (t.project if t is not None else "?")
            agents = sorted(running.get(sid, []) if sid else [], key=lambda x: x.start, reverse=True)
            now_agents = []
            for x in agents[:NOW_AGENTS_MAX]:
                at = self.transcripts[x.id]
                now_agents.append(NowAgent(x, at.action, at.action_time))
            out.append(
                NowSession(
                    session_id=sid,
                    project=clip_text(sanitize(project), TIP_CAP) or "?",  # G5
                    name=clip_text(sanitize(info["name"]), TIP_CAP),
                    cwd=cwd,
                    status=clip_text(sanitize(info["status"]), TIP_CAP),
                    busy=busy,
                    action=t.action if t is not None else None,
                    action_time=t.action_time if t is not None else None,
                    agents=tuple(now_agents),
                    more_agents=max(0, len(agents) - NOW_AGENTS_MAX),
                )
            )
        out.sort(key=lambda n: (not n.busy, -(n.action_time if n.action_time is not None else float("-inf"))))
        busy_count = sum(1 for n in out if n.busy)
        return tuple(out[:NOW_SESSIONS_MAX]), max(0, len(out) - NOW_SESSIONS_MAX), busy_count
