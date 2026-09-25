"""Tests for claudemon_lib.animation.Animator. See components.md P3 row, ui-spec.md §5,
claude/luna/decisions.md "Reduce Motion turns off the border glow too" (numbers still snap and
new agents still appear without fade when Reduce Motion is on).
"""
from __future__ import annotations

import unittest

import support  # noqa: F401

from claudemon_lib.animation import Animator
from claudemon_lib.model import Snapshot, Span, Usage


def _snap(loaded: bool = True, today: int = 0, per_minute: int = 0, spans=None) -> Snapshot:
    return Snapshot(
        loaded=loaded,
        today=Usage(input=today, output=0, cache_write=0, cache_read=0),
        per_minute=per_minute,
        agents_today=list(spans or []),
    )


def _span(id_: str, running: bool = True) -> Span:
    return Span(id=id_, type="agent", task="t", family="opus", start=0.0, end=0.0, tokens=0, running=running)


class TestApplyAndNum(unittest.TestCase):
    def test_first_apply_seeds_shown_to_target(self) -> None:
        anim = Animator()
        anim.apply(_snap(today=500), now=0.0, reduce_motion=False)
        self.assertEqual(anim.num("today", -1), 500)

    def test_num_uses_fallback_for_unknown_key(self) -> None:
        anim = Animator()
        anim.apply(_snap(), now=0.0, reduce_motion=False)
        self.assertEqual(anim.num("nope", 77), 77)


class TestEasingConvergence(unittest.TestCase):
    def test_step_moves_toward_target_and_converges(self) -> None:
        anim = Animator()
        anim.apply(_snap(today=0), now=0.0, reduce_motion=False)
        self.assertEqual(anim.num("today", -1), 0)

        anim.apply(_snap(today=1000), now=1.0, reduce_motion=False)
        # Target jumped but shown hasn't moved yet.
        self.assertEqual(anim.num("today", -1), 0)
        self.assertTrue(anim.needs_animation(1.0, reduce_motion=False))

        now = 1.0
        moving = True
        steps = 0
        while moving and steps < 1000:
            now += 1.0 / 30
            moving = anim.step(now)
            steps += 1
        self.assertLess(steps, 1000, "easing should converge well within 1000 frames")
        self.assertEqual(anim.num("today", -1), 1000)
        self.assertFalse(anim.needs_animation(now, reduce_motion=False))

    def test_small_diff_snaps_immediately(self) -> None:
        anim = Animator()
        anim.apply(_snap(today=1000), now=0.0, reduce_motion=False)
        anim.apply(_snap(today=1001), now=1.0, reduce_motion=False)
        # diff (1) <= max(1, 1001*0.002) so it should snap on the very next step.
        anim.step(1.0)
        self.assertEqual(anim.num("today", -1), 1001)


class TestReduceMotionSnapping(unittest.TestCase):
    def test_reduce_motion_snaps_shown_on_apply(self) -> None:
        anim = Animator()
        anim.apply(_snap(today=0), now=0.0, reduce_motion=False)
        anim.apply(_snap(today=5000), now=1.0, reduce_motion=True)
        self.assertEqual(anim.num("today", -1), 5000)
        self.assertFalse(anim.needs_animation(1.0, reduce_motion=True))

    def test_settle_snaps_and_completes_fades(self) -> None:
        anim = Animator()
        # Seed with no agents first so the next one gets a real (non-instant) fade-in.
        anim.apply(_snap(today=0, spans=[]), now=0.0, reduce_motion=False)
        span = _span("a1")
        anim.apply(_snap(today=9000, spans=[span]), now=0.1, reduce_motion=False)
        self.assertLess(anim.fade("a1", 0.1), 1.0)

        anim.settle()
        self.assertEqual(anim.num("today", -1), 9000)
        self.assertEqual(anim.fade("a1", 0.1), 1.0)
        self.assertFalse(anim.needs_animation(0.1, reduce_motion=False))


class TestFadeIn(unittest.TestCase):
    def test_agents_present_on_first_loaded_apply_have_no_fade(self) -> None:
        anim = Animator()
        span = _span("existing")
        anim.apply(_snap(loaded=True, spans=[span]), now=100.0, reduce_motion=False)
        self.assertEqual(anim.fade("existing", 100.0), 1.0)

    def test_new_agent_after_seeding_fades_in_over_time(self) -> None:
        anim = Animator()
        anim.apply(_snap(loaded=True, spans=[]), now=100.0, reduce_motion=False)

        new_span = _span("new1")
        anim.apply(_snap(loaded=True, spans=[new_span]), now=100.0, reduce_motion=False)

        self.assertAlmostEqual(anim.fade("new1", 100.0), 0.0)
        self.assertAlmostEqual(anim.fade("new1", 100.3), 0.5, places=6)
        self.assertEqual(anim.fade("new1", 100.6), 1.0)
        self.assertEqual(anim.fade("new1", 200.0), 1.0)

    def test_new_agent_under_reduce_motion_has_no_fade(self) -> None:
        anim = Animator()
        anim.apply(_snap(loaded=True, spans=[]), now=100.0, reduce_motion=True)
        new_span = _span("new1")
        anim.apply(_snap(loaded=True, spans=[new_span]), now=100.0, reduce_motion=True)
        self.assertEqual(anim.fade("new1", 100.0), 1.0)

    def test_unknown_span_id_fades_fully_in(self) -> None:
        anim = Animator()
        self.assertEqual(anim.fade("nope", 0.0), 1.0)


class TestNeedsAnimation(unittest.TestCase):
    def test_reduce_motion_never_needs_animation(self) -> None:
        anim = Animator()
        anim.apply(_snap(today=0), now=0.0, reduce_motion=False)
        anim.apply(_snap(today=99999), now=1.0, reduce_motion=False)
        self.assertFalse(anim.needs_animation(1.0, reduce_motion=True))

    def test_fade_keeps_animation_running(self) -> None:
        anim = Animator()
        # Seed with no agents first so the next one is treated as newly arrived, not
        # already-present-at-launch (which would skip the fade entirely).
        anim.apply(_snap(loaded=True, spans=[]), now=100.0, reduce_motion=False)
        span = _span("a1")
        anim.apply(_snap(loaded=True, spans=[span]), now=100.0, reduce_motion=False)
        self.assertTrue(anim.needs_animation(100.1, reduce_motion=False))
        self.assertFalse(anim.needs_animation(101.0, reduce_motion=False))


if __name__ == "__main__":
    unittest.main()
