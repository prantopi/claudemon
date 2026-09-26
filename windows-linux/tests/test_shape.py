"""Task U1: X11 rounded-corner region math and fallbacks (shape.py), the circular round_rect outline
(drawing.arc_rect_points), per-windowing-system input bindings (app.context_menu_events), and a Tk
smoke test of the real App window run in a subprocess (skipped where Tk cannot start)."""
from __future__ import annotations

import support  # noqa: F401

import math
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from claudemon_lib import shape
from support import ROOT


def _covered(rects, w, h):
    grid = [[0] * w for _ in range(h)]
    for x, y, rw, rh in rects:
        for yy in range(y, y + rh):
            for xx in range(x, x + rw):
                grid[yy][xx] += 1
    return grid


class RoundedRectRectsTests(unittest.TestCase):
    def test_empty_size_gives_no_rects(self):
        self.assertEqual(shape.rounded_rect_rects(0, 10, 9), [])
        self.assertEqual(shape.rounded_rect_rects(10, 0, 9), [])
        self.assertEqual(shape.rounded_rect_rects(-5, 10, 9), [])

    def test_zero_radius_is_the_full_rect(self):
        self.assertEqual(shape.rounded_rect_rects(435, 408, 0), [(0, 0, 435, 408)])

    def test_known_insets_for_radius_9(self):
        rects = shape.rounded_rect_rects(435, 408, 9)
        # Row 0: circle inset at the pixel centre = 9 - sqrt(81 - 8.5^2) = 6.04 -> 6.
        self.assertEqual(rects[0], (6, 0, 435 - 12, 1))
        # Insets per row from the circle formula: 6, 4, 2, 1, 1, then 0 (inset < 1 px) from row 5.
        self.assertEqual([x for x, _, _, _ in rects[:4]], [6, 4, 2, 1])
        self.assertEqual(rects[3], (1, 3, 433, 2))
        self.assertIn((0, 5, 435, 408 - 10), rects)
        # Bottom mirrors top.
        self.assertEqual(rects[-1], (6, 407, 435 - 12, 1))

    def test_every_row_covered_exactly_once_and_symmetric(self):
        for w, h, r in ((435, 408, 9), (405, 24, 9), (40, 10, 9), (3, 3, 9), (100, 50, 18)):
            with self.subTest(size=(w, h, r)):
                rects = shape.rounded_rect_rects(w, h, r)
                ys = sorted(y for _, y, _, _ in rects)
                self.assertEqual(ys, [y for _, y, _, _ in rects], "YX-banded: sorted by y")
                grid = _covered(rects, w, h)
                for row in range(h):
                    self.assertLessEqual(max(grid[row]), 1)
                    filled = [x for x in range(w) if grid[row][x]]
                    self.assertTrue(filled, "every row keeps some pixels")
                    left, right = filled[0], w - 1 - filled[-1]
                    self.assertEqual(left, right, "left/right symmetric")
                    self.assertEqual(len(filled), filled[-1] - filled[0] + 1, "one span per row")
                    mirror = [x for x in range(w) if grid[h - 1 - row][x]]
                    self.assertEqual(filled, mirror, "top/bottom symmetric")

    def test_area_close_to_a_true_rounded_rect(self):
        w, h, r = 435, 408, 9
        area = sum(rw * rh for _, _, rw, rh in shape.rounded_rect_rects(w, h, r))
        exact = w * h - (4 - math.pi) * r * r
        # Rounding down keeps edge pixels, so the region is never smaller than the true shape.
        self.assertGreaterEqual(area, exact)
        self.assertLess(area - exact, 4 * r)

    def test_radius_is_clamped_to_half_the_smaller_side(self):
        # Height 10 -> r clamps to (10 - 1) / 2 = 4.5, same as round_rect's clamp.
        self.assertEqual(shape.rounded_rect_rects(100, 10, 9), shape.rounded_rect_rects(100, 10, 4.5))

    def test_adjacent_equal_rows_are_merged(self):
        rects = shape.rounded_rect_rects(435, 408, 9)
        for a, b in zip(rects, rects[1:]):
            self.assertNotEqual(a[0], b[0], "neighbouring bands differ in inset")


class ApplyRoundedFallbackTests(unittest.TestCase):
    def setUp(self):
        self._saved = shape._api
        shape._api = None

    def tearDown(self):
        shape._api = self._saved

    def test_bad_arguments_return_false_without_loading_libraries(self):
        with mock.patch.object(shape, "_load_api", side_effect=AssertionError("must not load")):
            self.assertFalse(shape.apply_rounded(None, 0, 100, 100, 9))
            self.assertFalse(shape.apply_rounded(None, -1, 100, 100, 9))
            self.assertFalse(shape.apply_rounded(None, "0x1", 100, 100, 9))  # type: ignore[arg-type]
            self.assertFalse(shape.apply_rounded(None, 0x400001, 0, 100, 9))
            self.assertFalse(shape.apply_rounded(None, 0x400001, 40000, 100, 9))

    def test_missing_libraries_fall_back_silently(self):
        with mock.patch("ctypes.util.find_library", return_value=None), mock.patch.object(
            shape, "X11_NAMES", ("libclaudemon-no-such-x11.so",)
        ), mock.patch.object(shape, "XEXT_NAMES", ("libclaudemon-no-such-xext.so",)):
            self.assertFalse(shape.apply_rounded(":0", 0x400001, 100, 100, 9))
            self.assertIs(shape._api, False, "the failure is cached")
            self.assertFalse(shape.apply_rounded(":0", 0x400001, 100, 100, 9))

    def test_no_display_returns_false_and_closes_nothing(self):
        api = mock.Mock()
        api.open_display.return_value = None
        with mock.patch.object(shape, "_load_api", return_value=api):
            self.assertFalse(shape.apply_rounded(":57", 0x400001, 100, 100, 9))
        api.close_display.assert_not_called()
        api.combine_rectangles.assert_not_called()

    def test_x_error_returns_false_restores_handler_and_closes_display(self):
        import ctypes

        api = mock.Mock()
        api.ctypes = ctypes
        api.XRectangle = mock.Mock(side_effect=lambda *a: a)
        api.open_display.return_value = 1234
        api.query_extension.return_value = 1
        api.set_error_handler.side_effect = ["tk-handler", "ours"]

        def combine(*args):
            api.errors = 1  # what the installed handler does on an X error

        api.combine_rectangles.side_effect = combine
        with mock.patch.object(shape, "_load_api", return_value=api), mock.patch.object(
            ctypes, "byref", side_effect=lambda v: v
        ):
            # XRectangle * n needs a real ctypes type; use one of the right shape.
            class R(ctypes.Structure):
                _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short),
                            ("width", ctypes.c_ushort), ("height", ctypes.c_ushort)]

            api.XRectangle = R
            self.assertFalse(shape.apply_rounded(":0", 0x400001, 100, 100, 9))
        self.assertEqual(api.set_error_handler.call_args_list[-1], mock.call("tk-handler"))
        api.close_display.assert_called_once_with(1234)


def _tkinter_importable() -> bool:
    try:
        import tkinter  # noqa: F401
    except Exception:
        return False
    return True


@unittest.skipUnless(_tkinter_importable(), "tkinter not available")
class ArcRectPointsTests(unittest.TestCase):
    def test_corners_are_circular_arcs_of_radius_r(self):
        from claudemon_lib.drawing import _ARC_STEPS, arc_rect_points

        x0, y0, x1, y1, r = 0, 0, 434, 407, 9
        pts = arc_rect_points(x0, y0, x1, y1, r)
        xy = list(zip(pts[0::2], pts[1::2]))
        self.assertEqual(len(xy), 4 * (_ARC_STEPS + 1))
        centres = [(x1 - r, y0 + r), (x1 - r, y1 - r), (x0 + r, y1 - r), (x0 + r, y0 + r)]
        for k, (cx, cy) in enumerate(centres):
            for px, py in xy[k * (_ARC_STEPS + 1):(k + 1) * (_ARC_STEPS + 1)]:
                self.assertAlmostEqual(math.hypot(px - cx, py - cy), r, places=6)
        for px, py in xy:
            self.assertTrue(x0 - 1e-9 <= px <= x1 + 1e-9 and y0 - 1e-9 <= py <= y1 + 1e-9)
        self.assertAlmostEqual(xy[0][0], x1 - r)
        self.assertAlmostEqual(xy[0][1], y0)


@unittest.skipUnless(_tkinter_importable(), "tkinter not available")
class ContextMenuEventsTests(unittest.TestCase):
    def test_right_click_sequences_follow_the_windowing_system(self):
        from claudemon_lib.app import context_menu_events

        self.assertEqual(context_menu_events("aqua"), ("<Button-2>", "<Control-Button-1>"))
        self.assertEqual(context_menu_events("x11"), ("<Button-3>",))
        self.assertEqual(context_menu_events("win32"), ("<Button-3>",))


_SMOKE = textwrap.dedent(
    """
    import sys, tempfile, tkinter as tk
    from pathlib import Path
    sys.path.insert(0, {root!r})
    from claudemon_lib import settings as settings_mod
    from claudemon_lib.app import App, context_menu_events
    from claudemon_lib.store import Store

    root = tk.Tk()
    ws = root.tk.call("tk", "windowingsystem")
    home = Path(tempfile.mkdtemp())
    (home / "projects").mkdir()
    (home / "sessions").mkdir()
    app = App(root, settings_mod.Settings(), Store(claude_home=home))
    root.update()
    c = app.canvas
    for seq in context_menu_events(ws):
        assert c.bind(seq), ("context menu not bound", seq)
    for seq in ("<Key-q>", "<Key-Escape>", "<Key-c>", "<Key-1>", "<Key-4>"):
        assert app.win.bind(seq), ("key not bound", seq)
    if ws == "aqua":
        assert app.win is not root and root.state() == "withdrawn"
        style = root.tk.call("::tk::unsupported::MacWindowStyle", "style", app.win._w)
        assert str(style[0]) == "plain", style
        assert not root.tk.call("wm", "overrideredirect", app.win._w)
        assert app.view.transparent_corners and app.view.true_radius
    # Wait for the worker's first snapshot: it changes the full-mode rows ("Reading Claude Code
    # logs..." -> loaded), so a height recorded before it arrives would race the toggles below.
    import time
    deadline = time.monotonic() + 20
    while not app.view.snap.loaded:
        assert time.monotonic() < deadline, "no snapshot from the worker"
        root.update()
        time.sleep(0.02)
    root.update()
    app._resize(force=True)
    root.update_idletasks()
    full_h = app._height
    assert full_h == app.view.fitting_size()[1], (full_h, app.view.fitting_size())
    # Double-click on the title toggles compact (ui-spec.md §9), and back.
    def double_click(x, y):
        for _ in range(2):
            c.event_generate("<ButtonPress-1>", x=x, y=y)
            c.event_generate("<ButtonRelease-1>", x=x, y=y)
        root.update()

    double_click(200, 5)
    assert app.view.compact and app._height == app.metrics.title_h, (app.view.compact, app._height)
    double_click(200, 5)
    root.update_idletasks()
    assert not app.view.compact, "compact did not toggle back"
    assert app._height == app.view.fitting_size()[1] == full_h, (app._height, app.view.fitting_size(), full_h)
    # Motion sets the hover point; Leave clears it.
    c.event_generate("<Motion>", x=50, y=60)
    assert app.view.mouse == (50, 60), app.view.mouse
    c.event_generate("<Leave>")
    assert app.view.mouse is None
    app.set_theme(app.view.theme.__class__.CLAUDE)
    root.update()
    app.quit()
    print("SMOKE-OK", ws)
    """
)


class AppSmokeTests(unittest.TestCase):
    """Runs the real App in a child interpreter: some Pythons abort inside tk.Tk() (no usable Tk),
    which would take the whole test run down if done in-process."""

    def test_app_window_and_bindings(self):
        if not _tkinter_importable():
            self.skipTest("tkinter not available")
        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            self.skipTest("no X display")
        with tempfile.TemporaryDirectory() as cfg:
            env = dict(os.environ, XDG_CONFIG_HOME=cfg, APPDATA=cfg)
            try:
                proc = subprocess.run(
                    [sys.executable, "-c", _SMOKE.format(root=str(ROOT))],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60, env=env,
                )
            except subprocess.TimeoutExpired:
                self.fail("Tk smoke test timed out")
        if proc.returncode < 0 or "TclError" in proc.stderr and "display" in proc.stderr:
            self.skipTest("Tk cannot start here (exit {}): {}".format(proc.returncode, proc.stderr[-200:]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SMOKE-OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
