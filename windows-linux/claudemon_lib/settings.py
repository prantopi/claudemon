"""Settings dataclass and JSON load/save. See components.md (file schema), architecture.md D11.

Security-sensitive (architecture.md security-sensitive area #4): the file is user-editable, so
every key is validated by type and value and nothing in it is trusted. Writes are atomic
(temp file in the same folder + os.replace) and happen only inside the settings file's folder.

Persisted: range, theme, compact, reduce_motion, window position (right edge + top).
Always on Top is deliberately NOT persisted (claude/luna/decisions.md, 2026-09-24).
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import paths
from .model import TimeRange
from .theme import ThemeChoice

SCHEMA_VERSION = 1
MAX_FILE_BYTES = 64 * 1024  # a valid file is ~150 bytes; anything larger is ignored
MAX_COORD = 1_000_000  # sanity bound for window coordinates (multi-monitor can be negative)


@dataclass
class Settings:
    range: TimeRange = TimeRange.HOUR
    theme: ThemeChoice = ThemeChoice.TERMINAL
    compact: bool = False
    reduce_motion: bool = False
    window_right: Optional[int] = None
    window_top: Optional[int] = None


def _coord(v: Any) -> Optional[int]:
    # bool is a subclass of int; reject it. Floats are rejected too (the app only writes ints).
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    if abs(v) > MAX_COORD:
        return None
    return v


def _from_dict(data: Any) -> Settings:
    s = Settings()
    if not isinstance(data, dict):
        return s

    r = data.get("range")
    if isinstance(r, str):
        tr = TimeRange.from_label(r)
        if tr is not None:
            s.range = tr

    t = data.get("theme")
    if isinstance(t, str):
        tc = ThemeChoice.from_key(t)
        if tc is not None:
            s.theme = tc

    c = data.get("compact")
    if isinstance(c, bool):
        s.compact = c

    rm = data.get("reduce_motion")
    if isinstance(rm, bool):
        s.reduce_motion = rm

    w = data.get("window")
    if isinstance(w, dict):
        right = _coord(w.get("right"))
        top = _coord(w.get("top"))
        if right is not None and top is not None:  # the anchor is only useful as a pair
            s.window_right = right
            s.window_top = top
    return s


def _to_dict(s: Settings) -> dict:
    window = None
    right = _coord(s.window_right)
    top = _coord(s.window_top)
    if right is not None and top is not None:
        window = {"right": right, "top": top}
    rng = s.range if isinstance(s.range, TimeRange) else TimeRange.HOUR
    theme = s.theme if isinstance(s.theme, ThemeChoice) else ThemeChoice.TERMINAL
    return {
        "version": SCHEMA_VERSION,
        "range": rng.label,
        "theme": theme.key,
        "compact": bool(s.compact),
        "reduce_motion": bool(s.reduce_motion),
        "window": window,
    }


def load(path: Optional[Path] = None) -> Settings:
    """Load settings from path (default paths.settings_path()).

    Never raises; each key is validated independently and falls back to its default; unknown
    keys are ignored; a corrupt file yields all defaults.
    """
    try:
        p = Path(path) if path is not None else paths.settings_path()
        with open(p, "rb") as f:
            raw = f.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            return Settings()
        data = json.loads(raw.decode("utf-8"))
    except Exception:  # missing file, permissions, bad UTF-8, bad JSON, deep nesting...
        return Settings()
    try:
        return _from_dict(data)
    except Exception:
        return Settings()


def save(s: Settings, path: Optional[Path] = None) -> bool:
    """Atomically write s to path (default paths.settings_path()) as JSON. True on success.

    Never raises.
    """
    tmp_name: Optional[str] = None
    try:
        p = Path(path) if path is not None else paths.settings_path()
        text = json.dumps(_to_dict(s), indent=2, sort_keys=False) + "\n"
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=str(p.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_name, str(p))
        tmp_name = None
        return True
    except Exception:
        return False
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
