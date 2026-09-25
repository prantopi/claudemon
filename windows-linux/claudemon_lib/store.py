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
import os
import stat
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from .model import (
    ModelTotal,
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
from .liveness import live_session_count
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
        if obj.get("type") != "assistant" or not isinstance(msg, dict):
            return
        u = msg.get("usage")
        model = msg.get("model")
        ts = obj.get("timestamp")
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
        s.live_sessions = live_session_count(self.sessions_dir, self._pid_alive)  # data-flow §6

        mains = [t for t in self.transcripts.values() if not t.is_agent]
        busy = sum(1 for t in mains if t.mtime is not None and now - t.mtime < 20)
        s.busy_sessions = min(s.live_sessions, busy)
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
        return s
