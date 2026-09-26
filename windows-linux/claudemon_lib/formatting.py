"""Number, duration and time formatting. See ui-spec.md §8.

claudemon.swift 389-409, 440-443.
"""
from __future__ import annotations

import datetime
import math
import unicodedata


def tokens(n: int) -> str:
    """">=1_000_000 -> "%.1fM"; >=10_000 -> "%.0fk"; >=1000 -> "%.1fk"; else str(n)."""
    d = float(n)
    if n >= 1_000_000:
        return "%.1fM" % (d / 1_000_000)
    if n >= 10_000:
        return "%.0fk" % (d / 1000)
    if n >= 1000:
        return "%.1fk" % (d / 1000)
    return str(n)


def exact(n: int) -> str:
    """f"{n:,}"."""
    return "{:,}".format(n)


def clip(s: str, n: int) -> str:
    """s.ljust(n) if len(s) <= n else s[:n-1] + "…"."""
    return s.ljust(n) if len(s) <= n else s[: n - 1] + "…"


def clip_text(s: str, n: int) -> str:
    """s if it fits in n characters, else its first n-1 characters + "…" (no padding)."""
    return s if len(s) <= n else s[: n - 1] + "…"


def sanitize(s: str) -> str:
    """Untrusted text for one line: newlines and control/format characters become spaces, runs of
    whitespace collapse to one space, and the ends are trimmed."""
    out = []
    for ch in s:
        if ch in "\n\r\t" or unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp"):
            out.append(" ")
        else:
            out.append(ch)
    return " ".join("".join(out).split())


def duration(seconds: float) -> str:
    """s = int(max(0, seconds)); >=3600 -> "{h}h{m}m"; >=60 -> "{m}m{s}s"; else "{s}s"."""
    s = int(max(0, seconds))
    if s >= 3600:
        return "{}h{}m".format(s // 3600, s % 3600 // 60)
    if s >= 60:
        return "{}m{}s".format(s // 60, s % 60)
    return "{}s".format(s)


def hm(t: float) -> str:
    """Local time "%H:%M"."""
    return datetime.datetime.fromtimestamp(t).strftime("%H:%M")


def clock(t: float) -> str:
    """Local time "%H:%M:%S"."""
    return datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S")


def day_name(d: datetime.date) -> str:
    """"%a"."""
    return d.strftime("%a")


def day_long(d: datetime.date) -> str:
    """f"{d:%a} {d.day} {d:%b}"."""
    return "{} {} {}".format(d.strftime("%a"), d.day, d.strftime("%b"))


def round_half_up(x: float) -> int:
    """math.floor(x + 0.5) (Python round() is banker's rounding; patterns.md #10)."""
    return int(math.floor(x + 0.5))
