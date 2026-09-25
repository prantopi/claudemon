"""Eases displayed numbers toward Snapshot targets and fades in new rows/spans.

claudemon.swift 456-500; data-flow.md §7. "Seen before" is stored as float("-inf")
(Swift .distantPast).
"""
from __future__ import annotations

import math
from typing import Dict

from .model import Snapshot


class Animator:
    FADE_SECONDS = 0.6
    KEYS = ("today", "window", "speed", "peak", "ctx")

    def __init__(self) -> None:
        self.shown: Dict[str, float] = {}
        self.targets: Dict[str, float] = {}
        self.first_seen: Dict[str, float] = {}
        self.seeded: bool = False

    def apply(self, snap: Snapshot, now: float, reduce_motion: bool) -> None:
        """Set new targets from snap; record first-seen times for fade-in. claudemon.swift 456-468."""
        if snap.loaded and not self.seeded:
            # Agents that already existed at launch appear without a fade.
            for a in snap.agents_today:
                self.first_seen[a.id] = float("-inf")
            self.seeded = True
        for a in snap.agents_today:
            if a.id not in self.first_seen:
                self.first_seen[a.id] = float("-inf") if reduce_motion else now

        self.targets = {
            "today": float(snap.today.total),
            "window": float(snap.window),
            "speed": float(snap.per_minute),
            "peak": float(snap.peak_per_minute),
            "ctx": float(snap.context),
        }
        for k, v in self.targets.items():
            if k not in self.shown or reduce_motion:
                self.shown[k] = v

    def step(self, now: float) -> bool:
        """Advance one 30 fps frame of easing. Returns True while numbers move or a fade runs.

        claudemon.swift 471-486.
        """
        moving = False
        for k, target in self.targets.items():
            cur = self.shown.get(k, target)
            diff = target - cur
            if abs(diff) <= max(1, abs(target) * 0.002):
                self.shown[k] = target
            else:
                self.shown[k] = cur + diff * 0.25
                moving = True
        if any((now - seen) < self.FADE_SECONDS for seen in self.first_seen.values()):
            moving = True
        return moving

    def needs_animation(self, now: float, reduce_motion: bool) -> bool:
        """Whether the 33 ms animation timer should keep running. claudemon.swift 488-493."""
        if reduce_motion:
            return False
        for k, target in self.targets.items():
            shown = self.shown.get(k, target)
            if abs(target - shown) > max(1, abs(target) * 0.002):
                return True
        return any((now - seen) < self.FADE_SECONDS for seen in self.first_seen.values())

    def settle(self) -> None:
        """Snap shown values to targets and complete all fades (Reduce Motion turned on)."""
        self.shown = dict(self.targets)
        for k in self.first_seen:
            self.first_seen[k] = float("-inf")

    def num(self, key: str, fallback: int) -> int:
        """floor(shown[key] + 0.5), or fallback if key is unknown."""
        if key not in self.shown:
            return fallback
        return int(math.floor(self.shown[key] + 0.5))

    # Absolute tolerance for the "has the fade finished?" check below. `now` and
    # `seen` are independent float epoch seconds, so `now - seen` for a caller that
    # waited exactly FADE_SECONDS can land a hair under FADE_SECONDS (e.g. computing
    # 100.6 - 100.0 gives 0.5999999999999943, not 0.6); a plain `>=` comparison, or
    # `min(1.0, elapsed / FADE_SECONDS)` after dividing, would then report the fade
    # as not-quite-done (0.9999999999999906) forever. This epsilon is far smaller
    # than one animation frame (1/30s) so it never shortens a fade perceptibly.
    _FADE_EPS = 1e-9

    def fade(self, span_id: str, now: float) -> float:
        """Fade-in alpha in [0, 1] for span_id; 1 if unknown."""
        seen = self.first_seen.get(span_id)
        if seen is None:
            return 1.0
        elapsed = now - seen
        if elapsed >= self.FADE_SECONDS - self._FADE_EPS:
            return 1.0
        if elapsed <= 0:
            return 0.0
        return elapsed / self.FADE_SECONDS
