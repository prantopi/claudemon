"""Rounded window corners on X11 through the X Shape extension. ui-spec.md §4 (corner_r).

claudemon.swift clips the window to a rounded rect (radius 9 pt); Windows gets the same look from a
-transparentcolor key and macOS from `wm attributes -transparent`. Plain X11 has neither, so the
Linux build cuts the corners off the toplevel's X window with XShapeCombineRectangles instead.

`rounded_rect_rects()` is pure (no tk, no ctypes) and unit-tested. `apply_rounded()` lazy-loads
libX11 + libXext and falls back silently (returns False) when anything is missing or fails: no
libXext, no X server (Wayland without XWayland), a bad window id, any X error.

Security-sensitive (same rules as liveness.py, architecture.md security-sensitive area #1):
- ctypes argtypes/restype are always declared (Display* and XID are pointer/long sized).
- Every Display we open is closed in a `finally`.
- Xlib's default error handler calls exit(), so a private handler is installed only around our own
  requests and the previous one (Tk's) is restored before returning.
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple

Rect = Tuple[int, int, int, int]  # x, y, width, height

# X.h / shape.h constants.
_SHAPE_BOUNDING = 0
_SHAPE_SET = 0
_YX_BANDED = 3

# Library names tried in order (after ctypes.util.find_library). Module-level so tests can point
# them at another Xlib build.
X11_NAMES: Tuple[str, ...] = ("libX11.so.6", "libX11.so")
XEXT_NAMES: Tuple[str, ...] = ("libXext.so.6", "libXext.so")

_api = None  # cached _XApi, or False once loading failed


def _corner_inset(row: int, r: float) -> int:
    """Pixels cut from one side of scanline `row` (0 = the outermost row) of a radius-r corner.

    drawing.round_rect(..., true_radius=True) draws circular corners, so this is the circle's
    inset measured at the pixel centre, rounded down so pixels the edge passes through are kept
    (they show the card's own background, never a hole).
    """
    dy = r - (row + 0.5)
    if r <= 0 or dy <= 0:
        return 0
    return int(math.floor(r - math.sqrt(max(0.0, r * r - dy * dy))))


def rounded_rect_rects(width: int, height: int, r: float) -> List[Rect]:
    """YX-banded rectangles covering a width x height rounded rect whose corners match
    drawing.round_rect(canvas, 0, 0, width - 1, height - 1, r, true_radius=True).

    r is clamped like round_rect (to half the smaller side); r <= 0 gives the full rect; an empty
    size gives []. Consecutive rows with the same inset are merged into one band.
    """
    w, h = int(width), int(height)
    if w <= 0 or h <= 0:
        return []
    r = max(0.0, min(float(r), min(w - 1, h - 1) / 2.0))
    insets = [0] * h
    for row in range(h):
        edge = min(row, h - 1 - row)  # distance from the nearer of the top/bottom edges
        insets[row] = min(_corner_inset(edge, r), (w - 1) // 2)
    rects: List[Rect] = []
    start = 0
    for row in range(1, h + 1):
        if row == h or insets[row] != insets[start]:
            dx = insets[start]
            rects.append((dx, start, w - 2 * dx, row - start))
            start = row
    return rects


# ------------------------------------------------------------------------------ ctypes / Xlib


class _XApi:
    def __init__(self, ctypes, x11, xext) -> None:
        self.ctypes = ctypes

        class XRectangle(ctypes.Structure):
            _fields_ = [
                ("x", ctypes.c_short),
                ("y", ctypes.c_short),
                ("width", ctypes.c_ushort),
                ("height", ctypes.c_ushort),
            ]

        self.XRectangle = XRectangle
        display_p = ctypes.c_void_p

        self.open_display = x11.XOpenDisplay
        self.open_display.argtypes = [ctypes.c_char_p]
        self.open_display.restype = display_p
        self.close_display = x11.XCloseDisplay
        self.close_display.argtypes = [display_p]
        self.close_display.restype = ctypes.c_int
        self.sync = x11.XSync
        self.sync.argtypes = [display_p, ctypes.c_int]
        self.sync.restype = ctypes.c_int
        self.set_error_handler = x11.XSetErrorHandler
        self.set_error_handler.argtypes = [ctypes.c_void_p]
        self.set_error_handler.restype = ctypes.c_void_p

        self.query_extension = xext.XShapeQueryExtension
        self.query_extension.argtypes = [display_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        self.query_extension.restype = ctypes.c_int
        self.combine_rectangles = xext.XShapeCombineRectangles
        self.combine_rectangles.argtypes = [
            display_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(XRectangle), ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ]
        self.combine_rectangles.restype = None

        # int (*XErrorHandler)(Display *, XErrorEvent *). Kept on self so it is never collected.
        self.errors = 0

        def _on_error(_display, _event):
            self.errors += 1
            return 0

        self._handler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(_on_error)
        self.handler_ptr = ctypes.cast(self._handler, ctypes.c_void_p)


def _load_lib(short: str, names: Tuple[str, ...]):
    import ctypes
    import ctypes.util

    candidates = []
    found = ctypes.util.find_library(short)
    if found:
        candidates.append(found)
    candidates.extend(names)
    for name in candidates:
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    return None


def _load_api() -> Optional[_XApi]:
    global _api
    if _api is None:
        try:
            import ctypes

            x11 = _load_lib("X11", X11_NAMES)
            xext = _load_lib("Xext", XEXT_NAMES) if x11 is not None else None
            _api = _XApi(ctypes, x11, xext) if xext is not None else False
        except Exception:
            _api = False
    return _api or None


def apply_rounded(display_name: Optional[str], window: int, width: int, height: int, r: float) -> bool:
    """Set the bounding shape of X window `window` to a width x height rounded rect.

    display_name is Tk's `winfo screen` (e.g. ":0.0"); None uses $DISPLAY. Returns True only if the
    shape was applied without an X error. Never raises.
    """
    try:
        if not isinstance(window, int) or window <= 0:
            return False
        rects = rounded_rect_rects(width, height, r)
        if not rects or width > 0x7FFF or height > 0x7FFF:
            return False
        api = _load_api()
        if api is None:
            return False
        ctypes = api.ctypes
        name = display_name.encode("utf-8") if display_name else None
        dpy = api.open_display(name)
        if not dpy:
            return False
        try:
            ev, er = ctypes.c_int(0), ctypes.c_int(0)
            if not api.query_extension(dpy, ctypes.byref(ev), ctypes.byref(er)):
                return False
            arr = (api.XRectangle * len(rects))(*[api.XRectangle(x, y, w, h) for x, y, w, h in rects])
            api.errors = 0
            previous = api.set_error_handler(api.handler_ptr)
            try:
                api.combine_rectangles(dpy, window, _SHAPE_BOUNDING, 0, 0, arr, len(rects), _SHAPE_SET, _YX_BANDED)
                api.sync(dpy, 0)  # flush and collect any error while our handler is installed
            finally:
                api.set_error_handler(previous)
            return api.errors == 0
        finally:
            api.close_display(dpy)
    except Exception:
        return False
