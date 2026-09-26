#!/usr/bin/env python3
"""Claudemon status-line bridge.

Claude Code's status line feeds a script JSON on stdin, which for claude.ai
Pro and Max subscribers can include ``rate_limits`` (official 5-hour and
7-day usage windows). This script:

  1. Reads that JSON from stdin (capped, never trusted).
  2. If a valid ``five_hour`` or ``seven_day`` window is present, writes it
     atomically to ``~/.claude/claudemon/limits.json`` so the Claudemon apps
     (macOS/Swift and Windows/Linux/Python) can show it without any network
     access or invented numbers.
  3. Prints something for the status line itself: if this script was given
     a command as arguments, it runs that command with the same stdin and
     prints its output (so an existing status line keeps working); otherwise
     it prints a short default line.

Stdlib only, Python 3.9+, no network access. Never raises and always exits 0
so it can never break a user's status line.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

MAX_STDIN_BYTES = 1024 * 1024  # 1 MB
COMMAND_TIMEOUT_SECONDS = 5
DEFAULT_MODEL_NAME = "Claude"
RATE_LIMIT_WINDOWS = ("five_hour", "seven_day")


def limits_file_path() -> Path:
    """Where limits.json is written, honouring the test/override env var."""
    override = os.environ.get("CLAUDEMON_LIMITS_FILE")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "claudemon" / "limits.json"


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _is_valid_percentage(value: Any) -> bool:
    return _is_number(value) and 0 <= value <= 1000


def _is_valid_resets_at(value: Any) -> bool:
    return _is_number(value) and value > 0


def _read_stdin_capped(max_bytes: int) -> bytes:
    try:
        raw = sys.stdin.buffer.read(max_bytes + 1)
    except Exception:
        return b""
    if raw is None:
        return b""
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
    return raw


def _safe_parse_json(raw_bytes: bytes) -> Dict[str, Any]:
    try:
        text = raw_bytes.decode("utf-8")
    except Exception:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_model_name(data: Dict[str, Any]) -> str:
    model = data.get("model")
    if isinstance(model, dict):
        name = model.get("display_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return DEFAULT_MODEL_NAME


def extract_valid_windows(rate_limits: Any) -> Dict[str, Dict[str, float]]:
    """Return only the windows with numeric, in-range percentage/resets_at."""
    windows: Dict[str, Dict[str, float]] = {}
    if not isinstance(rate_limits, dict):
        return windows
    for name in RATE_LIMIT_WINDOWS:
        window = rate_limits.get(name)
        if not isinstance(window, dict):
            continue
        used_percentage = window.get("used_percentage")
        resets_at = window.get("resets_at")
        if _is_valid_percentage(used_percentage) and _is_valid_resets_at(resets_at):
            windows[name] = {
                "used_percentage": float(used_percentage),
                "resets_at": int(resets_at),
            }
    return windows


def write_limits_file(path: Path, windows: Dict[str, Dict[str, float]], updated_at: float) -> None:
    """Atomically write limits.json: temp file in the same dir + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {"version": 1, "updated_at": updated_at}
    payload.update(windows)

    fd, tmp_name = tempfile.mkstemp(
        prefix=".claudemon-limits-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp_name, str(path))
    except BaseException:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise


def build_default_line(model_name: str, windows: Dict[str, Dict[str, float]]) -> str:
    parts = []
    if "five_hour" in windows:
        parts.append("5h {}%".format(int(windows["five_hour"]["used_percentage"])))
    if "seven_day" in windows:
        parts.append("7d {}%".format(int(windows["seven_day"]["used_percentage"])))
    if not parts:
        return model_name
    return "{} · {}".format(model_name, " · ".join(parts))


def run_chained_command(args, stdin_bytes: bytes) -> Optional[str]:
    """Run the user's existing status-line command, passing through stdin.

    Returns its stdout on success, or None on any failure (missing binary,
    non-zero exit, timeout, decode error, ...) so the caller can fall back.
    """
    try:
        result = subprocess.run(
            args,
            input=stdin_bytes,
            capture_output=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    try:
        return result.stdout.decode("utf-8", errors="replace")
    except Exception:
        return None


def main(argv) -> int:
    raw = _read_stdin_capped(MAX_STDIN_BYTES)
    data = _safe_parse_json(raw)
    model_name = _extract_model_name(data)
    windows = extract_valid_windows(data.get("rate_limits"))

    if windows:
        try:
            write_limits_file(limits_file_path(), windows, time.time())
        except Exception:
            pass  # Never let a filesystem problem break the status line.

    default_line = build_default_line(model_name, windows)

    chained_args = list(argv[1:])
    if chained_args:
        output = run_chained_command(chained_args, raw)
        if output is not None:
            sys.stdout.write(output if output.endswith("\n") else output + "\n")
            return 0

    print(default_line)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:
        try:
            print(DEFAULT_MODEL_NAME)
        except Exception:
            pass
        sys.exit(0)
