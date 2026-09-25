"""App: window chrome, timers, menu, keys, entry point. [tk]

architecture.md D3/D5, data-flow.md §1, ui-spec.md §3/§9. Reference: claudemon.swift ~1008-1140
(AppDelegate: applicationDidFinishLaunching, refresh/tick/animateIfNeeded, setRange/toggleCompact/
chooseTheme/togglePin, resize/keepOnScreen). The Python port splits Swift's single UI-thread
`refresh()` into a worker thread (architecture.md D1/D2) whose snapshots arrive over a queue.Queue.

Note for the "Reading Claude Code logs..." state (components.md, ui-spec.md §5): this is already
handled by `rows.build_rows()` (`if not snap.loaded: return [Line([Seg("Reading Claude Code
logs...", "dim")])]`), which both `MonitorView.draw()` and `MonitorView.fitting_size()` call. No
change to view.py was needed or made for this task.
"""
from __future__ import annotations

import queue
import time
import tkinter as tk
from typing import Optional, Tuple

from . import settings as settings_mod
from . import shape, system
from .drawing import KEY_COLOR, MAC_TRANSPARENT
from .fonts import Fonts
from .layout import Metrics
from .model import TimeRange
from .paths import IS_LINUX, IS_WINDOWS
from .settings import Settings
from .store import Store
from .theme import ThemeChoice
from .view import MonitorView
from .worker import Worker

# Timers (architecture.md D3, patterns.md #6). No other `after()` loops are used.
POLL_MS = 100
TICK_MS = 500
ANIM_MS = 33
RELIFT_MS = 2000

# ui-spec.md §3: default bottom-right start margin from the work area edge.
_START_MARGIN = 16

# ui-spec.md §9: ranges bound to keys "1".."4", in TimeRange order.
_RANGE_KEYS = (TimeRange.HOUR, TimeRange.FIVE_HOURS, TimeRange.DAY, TimeRange.WEEK)


def windowing_system(root: tk.Misc) -> str:
    """Tk's windowing system: "win32", "aqua" (macOS) or "x11". Input and window chrome differ by
    windowing system, not by sys.platform (a Tk built for X11 can run on macOS, for example)."""
    try:
        return str(root.tk.call("tk", "windowingsystem"))
    except tk.TclError:
        return "x11"


def context_menu_events(ws: str) -> Tuple[str, ...]:
    """Events that open the context menu (ui-spec.md §9 "right-click"). aqua Tk reports the right
    button as Button-2 (X11/Windows: Button-3), and a Control-click is the Mac right-click too."""
    if ws == "aqua":
        return ("<Button-2>", "<Control-Button-1>")
    return ("<Button-3>",)


class App:
    """Owns the window, the Worker, timers, redraw scheduling and input handling."""

    def __init__(self, root: tk.Tk, settings: Settings, store: Optional[Store] = None) -> None:
        self.root = root
        self.settings = settings
        self.ws = windowing_system(root)

        # architecture.md D5: withdrawn during setup, shown once placed, to avoid a flash at (0,0).
        root.withdraw()
        # self.win is the monitor window: root itself on Windows/X11, a Toplevel on aqua (below).
        self.win: tk.Misc = self._make_aqua_window(root) if self.ws == "aqua" else root
        win = self.win
        if win is root:
            root.overrideredirect(True)
        win.attributes("-topmost", True)
        self._topmost = True
        transparent_corners = False
        corner_bg: Optional[str] = None
        if IS_WINDOWS:
            # architecture.md D5: Windows transparent corners via the canvas's key color.
            root.attributes("-transparentcolor", KEY_COLOR)
            transparent_corners, corner_bg = True, KEY_COLOR
        elif self.ws == "aqua":
            # A see-through NSWindow; the canvas bg is aqua's transparent colour so the card's
            # rounded corners show the desktop (claudemon.swift 1033-1034: isOpaque = false,
            # backgroundColor = .clear). Older Tk without -transparent keeps square corners.
            try:
                win.attributes("-transparent", True)
                win.configure(bg=MAC_TRANSPARENT)
                transparent_corners, corner_bg = True, MAC_TRANSPARENT
            except tk.TclError:
                pass
        elif self.ws == "x11":
            # shape.py cuts the corners off the X window once it is mapped; if that fails,
            # _apply_shape() turns rounding back off so the border is drawn square.
            transparent_corners = True
        win.protocol("WM_DELETE_WINDOW", self.quit)
        if self.ws == "aqua":
            # Cmd-Q / Dock > Quit go through quit() too, so settings are saved.
            root.createcommand("::tk::mac::Quit", self.quit)

        # architecture.md D4: scale, then Fonts/Metrics built from it.
        scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.fonts: Fonts = Fonts.create(root, scale)
        self.metrics: Metrics = Metrics.for_scale(scale)

        self.canvas = tk.Canvas(win, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        self.view = MonitorView(
            self.canvas, self.fonts, self.metrics, transparent_corners, corner_bg,
            true_radius=not IS_WINDOWS,
        )
        self.view.range = settings.range
        self.view.compact = settings.compact
        self.view.reduce_motion = settings.reduce_motion
        self.view.theme = settings.theme
        # view.system_dark starts True (MonitorView default) until the worker's first probe,
        # matching architecture.md D10.

        self._store = store if store is not None else Store()
        self._out: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        self.worker = Worker(self._store, self._out, self.view.range)

        # after() ids, tracked so quit() can cancel every one of them (patterns.md #6).
        self._poll_after_id: Optional[str] = None
        self._tick_after_id: Optional[str] = None
        self._anim_after_id: Optional[str] = None
        self._relift_after_id: Optional[str] = None
        self._redraw_after_id: Optional[str] = None
        self._quitting = False

        # True until consumed: forces an immediate redraw for the first loaded snapshot and the
        # first snapshot after a range change (architecture.md D3).
        self._force_next_redraw = True

        # Drag state (ui-spec.md §9).
        self._drag_offset: Optional[Tuple[int, int]] = None
        self._moved_since_press = False

        # Current window size, tracked explicitly rather than re-read from winfo_*, since we are
        # the only thing that ever resizes this window (ui-spec.md §3).
        self._width = 0
        self._height = 0
        self._shaped_size: Optional[Tuple[int, int]] = None  # X11: size the shape was built for

        self._build_menu()
        self._bind_events()

        if self.view.theme == ThemeChoice.SYSTEM:
            self.worker.watch_system_theme(True)
        self.worker.start()

        self._initial_placement()
        # Render once before the window is ever shown, so deiconify() doesn't reveal a blank frame.
        self.view.draw(self._width, self._height)
        win.deiconify()
        win.focus_force()
        if self.ws == "x11":
            self._apply_shape()

        self._schedule_poll()
        self._schedule_tick()
        if IS_LINUX:
            self._schedule_relift()

    @staticmethod
    def _make_aqua_window(root: tk.Tk) -> tk.Toplevel:
        """aqua: a title-less window that can still become the key window.

        `wm overrideredirect` is not usable on aqua: Tk 8.6 marks override-redirect windows
        kWindowNoActivatesAttribute, so they never become key (canBecomeKeyWindow returns NO) and
        `focus -force` returns early for them. Keys then never arrive, and <Motion> never does
        either, because aqua Tk routes mouse-moved events to the key window. Swift's borderless
        PanelWindow overrides canBecomeKey for the same reason (claudemon.swift 1011-1013).

        MacWindowStyle "plain" gives a borderless NSWindow without that attribute, but the style is
        only read when the NSWindow is created, and root's already exists by the time tk.Tk()
        returns; hence a fresh Toplevel, styled before it is ever mapped, with root kept withdrawn.
        """
        win = tk.Toplevel(root)
        win.withdraw()
        try:
            root.tk.call("::tk::unsupported::MacWindowStyle", "style", win._w, "plain", "none")
        except tk.TclError:
            win.overrideredirect(True)  # no MacWindowStyle: borderless at least
        return win

    # -------------------------------------------------------------------- entry points

    def run(self) -> None:
        """Enter the Tk mainloop."""
        self.root.mainloop()

    def quit(self) -> None:
        """Stop the worker, save settings, destroy the window."""
        if self._quitting:
            return
        self._quitting = True
        for after_id in (
            self._poll_after_id,
            self._tick_after_id,
            self._anim_after_id,
            self._relift_after_id,
            self._redraw_after_id,
        ):
            if after_id is not None:
                try:
                    self.root.after_cancel(after_id)
                except Exception:
                    pass
        self.worker.stop()
        settings_mod.save(self.settings)
        try:
            self.root.destroy()
        except Exception:
            pass

    # -------------------------------------------------------------------- menu/key actions

    def set_range(self, r: TimeRange) -> None:
        if r == self.view.range:
            return
        self.view.range = r
        self.settings.range = r
        settings_mod.save(self.settings)
        self._force_next_redraw = True  # architecture.md D3: first snapshot after a range change
        self.request_redraw()
        self.worker.request(r)

    def toggle_compact(self) -> None:
        self.view.compact = not self.view.compact
        self.settings.compact = self.view.compact
        settings_mod.save(self.settings)
        self._resize(force=True)
        self.request_redraw()

    def set_theme(self, t: ThemeChoice) -> None:
        if t == self.view.theme:
            return
        self.view.theme = t
        self.settings.theme = t
        settings_mod.save(self.settings)
        # architecture.md D10: probe only while Match System is selected.
        self.worker.watch_system_theme(t == ThemeChoice.SYSTEM)
        self.request_redraw()

    def toggle_reduce_motion(self) -> None:
        self.view.reduce_motion = not self.view.reduce_motion
        self.settings.reduce_motion = self.view.reduce_motion
        settings_mod.save(self.settings)
        if self.view.reduce_motion:
            # claude/luna/decisions.md 2026-09-24: numbers/fades/glow all settle immediately.
            self.view.anim.settle()
            self._cancel_animation_timer()
        else:
            self._animate_if_needed()
        self.request_redraw()

    def toggle_topmost(self) -> None:
        # architecture.md D11: Always on Top is never persisted.
        self._topmost = not self._topmost
        try:
            self.win.attributes("-topmost", self._topmost)
        except tk.TclError:
            pass
        self.request_redraw()

    # -------------------------------------------------------------------- redraw scheduling

    def request_redraw(self) -> None:
        """Coalesce redraw requests into a single pending after_idle call (patterns.md #6)."""
        if self._redraw_after_id is not None:
            return
        self._redraw_after_id = self.root.after_idle(self._do_redraw)

    def _do_redraw(self) -> None:
        self._redraw_after_id = None
        self.view.draw(self._width, self._height)

    # -------------------------------------------------------------------- timers

    def _schedule_poll(self) -> None:
        self._poll_after_id = self.root.after(POLL_MS, self._poll_queue)

    def _poll_queue(self) -> None:
        """data-flow.md §1: drain the queue; discard snapshots for a stale range (patterns.md #5)."""
        self._poll_after_id = None
        while True:
            try:
                kind, payload = self._out.get_nowait()
            except queue.Empty:
                break
            if kind == "snapshot":
                snap = payload
                if snap.range != self.view.range:
                    continue
                now = time.time()
                self.view.apply(snap, now)
                self._resize()
                self._animate_if_needed()
                if self._force_next_redraw:
                    self._force_next_redraw = False
                    self.request_redraw()
            elif kind == "dark":
                self.view.system_dark = bool(payload)
                if self.view.theme == ThemeChoice.SYSTEM:
                    self.request_redraw()
        if self.ws == "aqua":
            self._poll_pointer()
        self._schedule_poll()

    def _poll_pointer(self) -> None:
        """aqua: hover while the app is inactive. Swift tracks the mouse with an `.activeAlways`
        tracking area (claudemon.swift 945-960); aqua Tk only delivers <Motion>/<Leave> to the key
        window of the active app, so the pointer is also sampled here every POLL_MS."""
        if self._drag_offset is not None:
            return
        try:
            px, py = self.win.winfo_pointerxy()
            x = px - self.win.winfo_rootx()
            y = py - self.win.winfo_rooty()
        except tk.TclError:
            return
        inside = 0 <= x < self._width and 0 <= y < self._height
        mouse = (x, y) if inside else None
        if mouse != self.view.mouse:
            self.view.mouse = mouse
            self.request_redraw()

    def _schedule_tick(self) -> None:
        self._tick_after_id = self.root.after(TICK_MS, self._on_tick)

    def _on_tick(self) -> None:
        """data-flow.md §1: spinner phase + clock, 2 redraws/s when idle."""
        self.view.spin_phase = not self.view.spin_phase
        self.request_redraw()
        self._schedule_tick()

    def _animate_if_needed(self) -> None:
        if self._anim_after_id is not None:
            return
        if not self.view.anim.needs_animation(time.time(), self.view.reduce_motion):
            return
        self._anim_after_id = self.root.after(ANIM_MS, self._animation_tick)

    def _animation_tick(self) -> None:
        moving = self.view.anim.step(time.time())
        self.request_redraw()
        if moving:
            self._anim_after_id = self.root.after(ANIM_MS, self._animation_tick)
        else:
            self._anim_after_id = None

    def _cancel_animation_timer(self) -> None:
        if self._anim_after_id is not None:
            try:
                self.root.after_cancel(self._anim_after_id)
            except Exception:
                pass
            self._anim_after_id = None

    def _schedule_relift(self) -> None:
        self._relift_after_id = self.root.after(RELIFT_MS, self._on_relift)

    def _on_relift(self) -> None:
        """architecture.md D5: some Linux WMs ignore -topmost, so re-lift periodically."""
        if self._topmost:
            try:
                self.win.lift()
            except tk.TclError:
                pass
        self._schedule_relift()

    # -------------------------------------------------------------------- window geometry

    def _initial_placement(self) -> None:
        """ui-spec.md §3: saved anchor (right edge + top), else bottom-right of the work area."""
        w, h = self.view.fitting_size()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        if self.settings.window_right is not None and self.settings.window_top is not None:
            x = self.settings.window_right - w
            y = self.settings.window_top
        else:
            left, top, right, bottom = system.work_area(0, 0, sw, sh)
            margin = self.metrics.px(_START_MARGIN)
            x = right - w - margin
            y = bottom - h - margin
        self._width, self._height = w, h
        self.win.geometry("{}x{}+{}+{}".format(w, h, int(x), int(y)))
        self._keep_on_screen()

    def _resize(self, force: bool = False) -> None:
        """ui-spec.md §3: keep the right edge and top fixed; width only grows unless forced."""
        new_w, new_h = self.view.fitting_size()
        cur_w, cur_h = self._width, self._height
        width = new_w if force else max(new_w, cur_w)
        if not force and abs(new_h - cur_h) <= 1 and width == cur_w:
            return
        x = self.win.winfo_x()
        y = self.win.winfo_y()
        new_x = x + cur_w - width
        self._width, self._height = width, new_h
        self.win.geometry("{}x{}+{}+{}".format(width, new_h, int(new_x), int(y)))
        self._keep_on_screen()
        if self.ws == "x11":
            self._apply_shape()

    def _apply_shape(self) -> None:
        """X11: clip the window to the rounded card (ui-spec.md §4) with shape.py, rebuilt whenever
        the size changes (resize, compact toggle). Any failure turns rounding off for good, so the
        card and border are drawn square instead. The region only depends on the size, so it can be
        applied before the new geometry has taken effect."""
        if not self.view.transparent_corners:
            return
        size = (self._width, self._height)
        if size == self._shaped_size:
            return
        ok = False
        try:
            self.win.update_idletasks()  # make sure the X wrapper window exists
            window = int(self.win.wm_frame(), 16)
            ok = shape.apply_rounded(self.win.winfo_screen(), window, size[0], size[1], self.metrics.corner_r)
        except (tk.TclError, ValueError):
            ok = False
        if ok:
            self._shaped_size = size
        else:
            self.view.transparent_corners = False
            self.request_redraw()

    def _keep_on_screen(self) -> None:
        """ui-spec.md §3: clamp into the work area of the monitor nearest the window."""
        x = self.win.winfo_x()
        y = self.win.winfo_y()
        w, h = self._width, self._height
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        left, top, right, bottom = system.work_area(x, y, sw, sh)
        new_x = min(max(x, left), right - w)
        new_y = min(max(y, top), bottom - h)
        if new_x != x or new_y != y:
            self.win.geometry("+{}+{}".format(int(new_x), int(new_y)))

    # -------------------------------------------------------------------- menu

    def _build_menu(self) -> None:
        """ui-spec.md §9: right-click context menu. Variables are resynced before each popup."""
        self._menu = tk.Menu(self.root, tearoff=0)
        self._topmost_var = tk.BooleanVar(self.root, value=self._topmost)
        self._compact_var = tk.BooleanVar(self.root, value=self.settings.compact)
        self._reduce_motion_var = tk.BooleanVar(self.root, value=self.settings.reduce_motion)
        self._theme_var = tk.IntVar(self.root, value=int(self.settings.theme))

        self._menu.add_checkbutton(
            label="Always on Top", variable=self._topmost_var, command=self.toggle_topmost
        )
        self._menu.add_checkbutton(
            label="Compact Mode",
            variable=self._compact_var,
            accelerator="C",
            command=self.toggle_compact,
        )
        theme_menu = tk.Menu(self._menu, tearoff=0)
        for t in ThemeChoice:
            theme_menu.add_radiobutton(
                label=t.title,
                variable=self._theme_var,
                value=int(t),
                command=lambda t=t: self.set_theme(t),
            )
        self._menu.add_cascade(label="Theme", menu=theme_menu)
        self._menu.add_checkbutton(
            label="Reduce Motion",
            variable=self._reduce_motion_var,
            command=self.toggle_reduce_motion,
        )
        self._menu.add_separator()
        self._menu.add_command(label="Quit claudemon", command=self.quit)

    def _show_context_menu(self, event: "tk.Event") -> None:
        self._topmost_var.set(self._topmost)
        self._compact_var.set(self.view.compact)
        self._reduce_motion_var.set(self.view.reduce_motion)
        self._theme_var.set(int(self.view.theme))
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self._menu.grab_release()
            except Exception:
                pass

    # -------------------------------------------------------------------- input (ui-spec.md §9)

    def _bind_events(self) -> None:
        c = self.canvas
        c.bind("<Button-1>", self._on_button1)
        c.bind("<Double-Button-1>", self._on_double_button1)
        c.bind("<B1-Motion>", self._on_b1_motion)
        c.bind("<ButtonRelease-1>", self._on_button1_release)
        for sequence in context_menu_events(self.ws):
            c.bind(sequence, self._show_context_menu)
        c.bind("<Motion>", self._on_motion)
        c.bind("<Leave>", self._on_leave)

        # Bound on the monitor window, which is in the canvas's bindtags, so keys reach these
        # handlers whichever of the two has the focus.
        win = self.win
        win.bind("<Key-q>", lambda e: self.quit())
        win.bind("<Escape>", lambda e: self.quit())
        win.bind("<Key-c>", lambda e: self.toggle_compact())
        for i, r in enumerate(_RANGE_KEYS, start=1):
            win.bind("<Key-{}>".format(i), lambda e, r=r: self.set_range(r))

    def _on_button1(self, event: "tk.Event") -> None:
        # architecture.md D5: override-redirect windows need this on X11; on aqua it activates
        # the app and makes the window key, so the keys below reach it.
        self.win.focus_force()
        kind, r = self.view.hit(event.x, event.y)
        if kind == "close":
            self.quit()
            return
        if kind == "range" and r is not None:
            self.set_range(r)
            return
        # "title" or "body": start a drag (ui-spec.md §9).
        self._drag_offset = (event.x_root - self.win.winfo_x(), event.y_root - self.win.winfo_y())
        self._moved_since_press = False

    def _on_double_button1(self, event: "tk.Event") -> None:
        if event.y < self.metrics.title_h:
            self.toggle_compact()

    def _on_b1_motion(self, event: "tk.Event") -> None:
        if self._drag_offset is None:
            return
        dx, dy = self._drag_offset
        new_x = event.x_root - dx
        new_y = event.y_root - dy
        self.win.geometry("+{}+{}".format(int(new_x), int(new_y)))
        self._moved_since_press = True

    def _on_button1_release(self, event: "tk.Event") -> None:
        if self._moved_since_press:
            self._keep_on_screen()
            self.settings.window_right = self.win.winfo_x() + self._width
            self.settings.window_top = self.win.winfo_y()
            settings_mod.save(self.settings)
        self._drag_offset = None
        self._moved_since_press = False

    def _on_motion(self, event: "tk.Event") -> None:
        self.view.mouse = (event.x, event.y)
        self.request_redraw()

    def _on_leave(self, event: "tk.Event") -> None:
        self.view.mouse = None
        self.request_redraw()


def main() -> None:
    """Settings.load(), tk.Tk(), App(...).run()."""
    settings = settings_mod.load()
    root = tk.Tk()
    app = App(root, settings)
    app.run()
