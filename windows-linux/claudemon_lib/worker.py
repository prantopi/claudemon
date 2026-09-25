"""Background daemon thread driving Store.refresh()/snapshot(). data-flow.md §1, architecture.md D1/D2.

Only this thread touches the Store; the UI thread only reads the queue. The Store is used
duck-typed (refresh(), snapshot(range)) so tests can pass a fake.
"""
from __future__ import annotations

import queue
import threading
import time
import traceback
from typing import Callable, Optional, Tuple

from .model import TimeRange
from .store import Store


class Worker:
    """Drives a Store on a daemon thread and posts ("snapshot", Snapshot) / ("dark", bool) to out.

    Loop order: probe dark (if watching and due) -> store.refresh() -> store.snapshot(range) ->
    put -> wake.wait(interval). Exceptions inside an iteration are swallowed. data-flow.md §1.
    """

    THREAD_NAME = "claudemon-worker"
    STOP_JOIN_TIMEOUT = 1.0

    def __init__(
        self,
        store: Store,
        out: "queue.Queue[Tuple[str, object]]",
        range: TimeRange,
        *,
        interval: float = 1.0,
        dark_probe: Optional[Callable[[], bool]] = None,
        dark_interval: float = 10.0,
    ) -> None:
        """dark_probe defaults to system.system_is_dark."""
        if dark_probe is None:
            from .system import system_is_dark

            dark_probe = system_is_dark
        self._store = store
        self._out = out
        self._range = range
        self._interval = max(0.0, float(interval))
        self._dark_probe = dark_probe
        self._dark_interval = max(0.0, float(dark_interval))
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._watching = False
        self._probe_now = False
        self._next_probe = 0.0  # time.monotonic() deadline
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ public, any thread

    def start(self) -> None:
        """Start the daemon thread, named "claudemon-worker"."""
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name=self.THREAD_NAME, daemon=True)
            thread = self._thread
        thread.start()

    def stop(self) -> None:
        """Set the stop flag and wake the loop; does not join longer than 1 s."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(self.STOP_JOIN_TIMEOUT)

    def request(self, range: TimeRange) -> None:
        """Thread-safe: change the requested range and wake the loop now."""
        with self._lock:
            self._range = range
        self._wake.set()

    def watch_system_theme(self, on: bool) -> None:
        """Thread-safe: enable/disable dark-mode probing; probes on the next loop when turned on."""
        with self._lock:
            was = self._watching
            self._watching = bool(on)
            if on and not was:
                self._probe_now = True
        if on and not was:
            self._wake.set()  # architecture D10: probe once immediately when selected

    # ------------------------------------------------------------------ worker thread

    def _run(self) -> None:
        while not self._stop.is_set():
            # Clear before working: a request() arriving mid-iteration re-sets it, so the
            # following wait returns at once instead of losing the wake-up.
            self._wake.clear()
            try:
                self._iteration()
            except Exception:
                try:
                    traceback.print_exc()
                except Exception:
                    pass
            if self._stop.is_set():
                break
            self._wake.wait(self._interval)

    def _iteration(self) -> None:
        self._maybe_probe()
        if self._stop.is_set():
            return
        with self._lock:
            rng = self._range
        self._store.refresh()
        snap = self._store.snapshot(rng)
        if not self._stop.is_set():
            self._out.put(("snapshot", snap))

    def _maybe_probe(self) -> None:
        now = time.monotonic()
        with self._lock:
            due = self._watching and (self._probe_now or now >= self._next_probe)
            if due:
                self._probe_now = False
                self._next_probe = now + self._dark_interval
        if not due:
            return
        try:
            dark = bool(self._dark_probe())
        except Exception:
            return  # keep the UI's last value; retried after dark_interval
        if not self._stop.is_set():
            self._out.put(("dark", dark))
