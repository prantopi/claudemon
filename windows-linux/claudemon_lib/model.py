"""Shared data types and the two tiny rules (family, project_name). No I/O.

claudemon.swift 287-290 (family), 167-170 (project_name).
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import List, Optional, Tuple


@dataclass
class Usage:
    input: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0

    @property
    def total(self) -> int:
        return self.input + self.cache_write + self.cache_read + self.output

    def add(self, other: "Usage") -> None:
        """Add other's four fields into self in place."""
        self.input += other.input
        self.cache_write += other.cache_write
        self.cache_read += other.cache_read
        self.output += other.output


@dataclass(frozen=True)
class Reply:
    time: float
    model: str
    usage: Usage
    file: str


@dataclass
class Transcript:
    is_agent: bool
    offset: int = 0
    partial: bytes = b""
    mtime: Optional[float] = None
    stat_key: Optional[Tuple[int, int]] = None
    project: str = "?"
    last_stop: Optional[str] = None
    last_context: int = 0
    last_reply: Optional[float] = None
    agent_type: str = "agent"
    agent_task: str = ""


class TimeRange(IntEnum):
    HOUR = 0
    FIVE_HOURS = 1
    DAY = 2
    WEEK = 3

    @property
    def label(self) -> str:
        return {
            TimeRange.HOUR: "1h",
            TimeRange.FIVE_HOURS: "5h",
            TimeRange.DAY: "24h",
            TimeRange.WEEK: "7d",
        }[self]

    @property
    def seconds(self) -> float:
        return {
            TimeRange.HOUR: 3600.0,
            TimeRange.FIVE_HOURS: 18000.0,
            TimeRange.DAY: 86400.0,
            TimeRange.WEEK: 604800.0,
        }[self]

    @property
    def buckets(self) -> int:
        return {
            TimeRange.HOUR: 60,
            TimeRange.FIVE_HOURS: 60,
            TimeRange.DAY: 96,
            TimeRange.WEEK: 0,
        }[self]

    @property
    def axis(self) -> Tuple[str, ...]:
        return {
            TimeRange.HOUR: ("60m", "30m", "now"),
            TimeRange.FIVE_HOURS: ("5h", "2.5h", "now"),
            TimeRange.DAY: ("24h", "12h", "now"),
            TimeRange.WEEK: (),
        }[self]

    @classmethod
    def from_label(cls, s: str) -> Optional["TimeRange"]:
        for r in cls:
            if r.label == s:
                return r
        return None


@dataclass(frozen=True)
class Span:
    id: str
    type: str
    task: str
    family: str
    start: float
    end: float
    tokens: int
    running: bool


@dataclass(frozen=True)
class ModelTotal:
    name: str
    tokens: int
    replies: int


@dataclass(frozen=True)
class ProjectTotal:
    name: str
    tokens: int


@dataclass
class Snapshot:
    loaded: bool = False
    live_sessions: int = 0
    busy_sessions: int = 0
    context: int = 0
    context_project: str = ""
    today: Usage = field(default_factory=Usage)
    today_replies: int = 0
    window: int = 0
    window_start: Optional[float] = None
    window_reset: Optional[float] = None
    per_minute: int = 0
    peak_per_minute: int = 0
    last_hour: List[int] = field(default_factory=list)
    range: TimeRange = TimeRange.HOUR
    main: List[float] = field(default_factory=list)
    agent_series: List[float] = field(default_factory=list)
    bucket_seconds: float = 60.0
    spans: List[Span] = field(default_factory=list)
    heat: List[List[int]] = field(default_factory=list)
    heat_days: List[datetime.date] = field(default_factory=list)
    models: List[ModelTotal] = field(default_factory=list)
    agents_today: List[Span] = field(default_factory=list)
    projects: List[ProjectTotal] = field(default_factory=list)
    now: float = 0.0


def family(model: str) -> str:
    """claudemon.swift 287-290: first of opus/sonnet/haiku/fable found in model, else "other"."""
    for f in ("opus", "sonnet", "haiku", "fable"):
        if f in model:
            return f
    return "other"


def project_name(cwd: str) -> str:
    """claudemon.swift 167-170: last path component of cwd, stripped of any .claude/worktrees suffix."""
    root = cwd.split("/.claude/worktrees/")[0].split("\\.claude\\worktrees\\")[0]
    trimmed = root.rstrip("/\\")
    name = re.split(r"[\\/]", trimmed)[-1] if trimmed else ""
    return name or root
