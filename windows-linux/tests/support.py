"""sys.path setup and fixture builders shared by every test_*.py module.

Every test module starts with `import support  # noqa: F401` before importing claudemon_lib.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def iso(t: float) -> str:
    """UTC ISO 8601 timestamp "YYYY-MM-DDTHH:MM:SS.mmmZ" for epoch seconds t."""
    dt = datetime.datetime.utcfromtimestamp(t)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}Z".format(dt.microsecond // 1000)


def assistant_line(
    msg_id: str,
    t: float,
    model: str = "claude-opus-4",
    usage: Optional[dict] = None,
    stop_reason: Optional[str] = "end_turn",
    cwd: Optional[str] = None,
) -> str:
    """One JSON transcript line (with a trailing "\\n") for an assistant reply."""
    if usage is None:
        usage = {
            "input_tokens": 10,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 20,
        }
    obj = {
        "type": "assistant",
        "timestamp": iso(t),
        "uuid": msg_id,
        "message": {
            "id": msg_id,
            "model": model,
            "usage": usage,
            "stop_reason": stop_reason,
        },
    }
    if cwd is not None:
        obj["cwd"] = cwd
    return json.dumps(obj) + "\n"


def write_lines(path: Path, lines: List[str], mtime: Optional[float] = None) -> None:
    """Append lines to path, creating parent directories as needed; optionally set st_mtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def make_home(tmp: Path) -> Path:
    """Create tmp/.claude/projects and tmp/.claude/sessions; return tmp/.claude."""
    home = tmp / ".claude"
    (home / "projects").mkdir(parents=True, exist_ok=True)
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    return home


def local_ts(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> float:
    """Epoch seconds for a naive local datetime."""
    return time.mktime(datetime.datetime(y, mo, d, h, mi, s).timetuple())
