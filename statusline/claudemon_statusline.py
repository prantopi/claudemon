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
     passes its output through unchanged (as raw bytes, so emoji/box-drawing
     output survives regardless of the terminal's locale encoding); otherwise
     it prints a short default line.

Stdlib only, Python 3.9+, no network access. main() is split into
independent parse / extract / write / output stages, each wrapped in its
own try, so a failure in one stage (e.g. malformed numbers) can never
prevent a later stage -- in particular the chained command -- from running.
The script never raises and always exits 0 so it can never break a user's
status line.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

MAX_STDIN_BYTES = 1024 * 1024  # 1 MB
COMMAND_TIMEOUT_SECONDS = 5
DEFAULT_MODEL_NAME = "Claude"
RATE_LIMIT_WINDOWS = ("five_hour", "seven_day")
MAX_USED_PERCENTAGE = 100
MAX_RESETS_AT = 1e11  # exclusive; anything at or beyond this isn't a sane epoch seconds value
MAX_MODEL_NAME_LENGTH = 64

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_MULTI_SPACE_RE = re.compile(r" +")


def limits_file_path() -> Path:
    """Where limits.json is written.

    CLAUDEMON_TEST_LIMITS_FILE is a test-only override: it lets the test
    suite point writes at a throwaway temp file so no test run ever touches
    a real ~/.claude/claudemon/limits.json. It is not meant to be set by
    end users and is intentionally left out of the README.
    """
    override = os.environ.get("CLAUDEMON_TEST_LIMITS_FILE")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "claudemon" / "limits.json"


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        # math.isfinite raises OverflowError on ints too large to convert
        # to a float (e.g. 10**400). Such a value is never valid input.
        return False


def _is_valid_percentage(value: Any) -> bool:
    return _is_number(value) and 0 <= value <= MAX_USED_PERCENTAGE


def _is_valid_resets_at(value: Any) -> bool:
    return _is_number(value) and 0 < value < MAX_RESETS_AT


def _read_stdin_capped(max_bytes: int) -> Tuple[bytes, bool]:
    """Read at most max_bytes from stdin. Returns (data, was_truncated)."""
    try:
        raw = sys.stdin.buffer.read(max_bytes + 1)
    except Exception:
        return b"", False
    if raw is None:
        return b"", False
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    return raw, truncated


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


def _sanitize_display_name(name: str) -> str:
    """Strip control characters/newlines, collapse spaces, cap the length."""
    cleaned = _CONTROL_CHARS_RE.sub(" ", name)
    cleaned = _MULTI_SPACE_RE.sub(" ", cleaned).strip()
    return cleaned[:MAX_MODEL_NAME_LENGTH]


def _extract_model_name(data: Dict[str, Any]) -> str:
    model = data.get("model")
    if isinstance(model, dict):
        name = model.get("display_name")
        if isinstance(name, str):
            sanitized = _sanitize_display_name(name)
            if sanitized:
                return sanitized
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


def run_chained_command(args, stdin_bytes: bytes) -> Optional[bytes]:
    """Run the user's existing status-line command, passing through stdin.

    Returns its raw stdout bytes on success, or None on any failure (missing
    binary, non-zero exit, timeout, ...) so the caller can fall back. The
    bytes are returned as-is (never decoded/re-encoded) so the chained
    command's own encoding choices -- including non-UTF-8 output on
    platforms where stdout defaults to a different code page -- survive
    unchanged.
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
    return result.stdout


def _write_bytes_and_flush(data: bytes) -> None:
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _redirect_stdout_to_devnull() -> None:
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except Exception:
        pass


def main(argv) -> int:
    # Stage: parse. Read stdin (capped) and parse it as JSON. A failure here
    # (or truncation) just leaves `data` empty -- it must not stop later
    # stages, in particular the chained command, from running.
    raw = b""
    truncated = False
    data: Dict[str, Any] = {}
    try:
        raw, truncated = _read_stdin_capped(MAX_STDIN_BYTES)
        if not truncated:
            data = _safe_parse_json(raw)
    except Exception:
        raw, truncated, data = b"", False, {}

    # Stage: extract. Pull the model name and any valid rate-limit windows
    # out of the parsed data. Oversized ints, out-of-range numbers, etc.
    # must never propagate past this stage.
    model_name = DEFAULT_MODEL_NAME
    windows: Dict[str, Dict[str, float]] = {}
    try:
        model_name = _extract_model_name(data)
        windows = extract_valid_windows(data.get("rate_limits"))
    except Exception:
        model_name, windows = DEFAULT_MODEL_NAME, {}

    # Stage: write. Best-effort persistence of the windows to limits.json.
    # A filesystem problem here must not block the chained command or the
    # default line below.
    try:
        if windows:
            write_limits_file(limits_file_path(), windows, time.time())
    except Exception:
        pass

    # Stage: output. Run the chained command (if any) and pass its output
    # through untouched; otherwise (or on failure) print the default line.
    # If stdin was truncated by the size cap, don't forward the truncated
    # bytes to the chained command -- fall straight through to the default
    # line instead.
    chained_args = list(argv[1:])
    if chained_args and not truncated:
        output: Optional[bytes] = None
        try:
            output = run_chained_command(chained_args, raw)
        except Exception:
            output = None
        if output is not None:
            try:
                to_write = output if output.endswith(b"\n") else output + b"\n"
                _write_bytes_and_flush(to_write)
                return 0
            except BrokenPipeError:
                _redirect_stdout_to_devnull()
                return 0
            except Exception:
                pass  # Fall through to the default line below.

    try:
        default_line = build_default_line(model_name, windows)
        _write_bytes_and_flush(default_line.encode("utf-8") + b"\n")
    except BrokenPipeError:
        _redirect_stdout_to_devnull()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise
    except BaseException:
        try:
            sys.stdout.buffer.write(DEFAULT_MODEL_NAME.encode("utf-8") + b"\n")
            sys.stdout.buffer.flush()
        except Exception:
            pass
        sys.exit(0)
