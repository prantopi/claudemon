"""Task P2: paths, liveness, system (dark-mode probe), settings, worker. Headless, no real ~/.claude."""
from __future__ import annotations

import support  # noqa: F401

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from claudemon_lib import liveness, paths, settings, system, worker
from claudemon_lib.model import Snapshot, TimeRange
from claudemon_lib.settings import Settings
from claudemon_lib.theme import ThemeChoice


# ------------------------------------------------------------------------------------ paths


class PathsTests(unittest.TestCase):
    def test_claude_home_is_home_dot_claude(self):
        with mock.patch.object(Path, "home", return_value=Path("/h/user")):
            self.assertEqual(paths.claude_home(), Path("/h/user/.claude"))

    def test_linux_uses_xdg_config_home(self):
        with mock.patch.object(sys, "platform", "linux"), mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": "/xdg/conf"}
        ):
            self.assertEqual(paths.settings_path(), Path("/xdg/conf/claudemon/settings.json"))

    def test_linux_without_xdg_uses_dot_config(self):
        env = {k: v for k, v in os.environ.items() if k != "XDG_CONFIG_HOME"}
        with mock.patch.object(sys, "platform", "linux"), mock.patch.dict(
            os.environ, env, clear=True
        ), mock.patch.object(Path, "home", return_value=Path("/h/user")):
            self.assertEqual(paths.settings_path(), Path("/h/user/.config/claudemon/settings.json"))

    def test_linux_empty_or_relative_xdg_ignored(self):
        for value in ("", "relative/conf"):
            with mock.patch.object(sys, "platform", "linux"), mock.patch.dict(
                os.environ, {"XDG_CONFIG_HOME": value}
            ), mock.patch.object(Path, "home", return_value=Path("/h/user")):
                self.assertEqual(
                    paths.settings_path(), Path("/h/user/.config/claudemon/settings.json"), value
                )

    def test_windows_uses_appdata(self):
        appdata = r"C:\Users\me\AppData\Roaming"
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(
            os.environ, {"APPDATA": appdata}
        ):
            self.assertEqual(paths.settings_path(), Path(appdata) / "claudemon" / "settings.json")

    def test_windows_without_appdata_falls_back(self):
        env = {k: v for k, v in os.environ.items() if k != "APPDATA"}
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(
            os.environ, env, clear=True
        ), mock.patch.object(Path, "home", return_value=Path("/h/user")):
            self.assertEqual(
                paths.settings_path(),
                Path("/h/user") / "AppData" / "Roaming" / "claudemon" / "settings.json",
            )

    def test_macos_dev_run_uses_posix_path(self):
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": "/xdg"}
        ):
            self.assertEqual(paths.settings_path(), Path("/xdg/claudemon/settings.json"))


# ------------------------------------------------------------------------------------ liveness


def _unused_pid() -> int:
    """A pid that is almost certainly not running (probe downwards from a large value)."""
    for pid in range(4_000_000, 3_990_000, -1):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except (PermissionError, OSError, OverflowError):
            continue
    raise unittest.SkipTest("no unused pid found")


@unittest.skipIf(sys.platform == "win32", "POSIX liveness path")
class LivenessPosixTests(unittest.TestCase):
    def test_own_pid_alive(self):
        self.assertTrue(liveness.pid_alive(os.getpid()))

    def test_unused_pid_dead(self):
        self.assertFalse(liveness.pid_alive(_unused_pid()))

    def test_invalid_pids_are_false_without_os_call(self):
        with mock.patch.object(liveness.os, "kill") as kill:
            for pid in (0, -1, -12345, 2**40, True, None, "12", 1.5):
                self.assertFalse(liveness.pid_alive(pid), repr(pid))  # type: ignore[arg-type]
            kill.assert_not_called()  # kill(0|-n, 0) would address process groups

    def test_permission_error_means_alive(self):
        with mock.patch.object(liveness.os, "kill", side_effect=PermissionError):
            self.assertTrue(liveness.pid_alive(1234))

    def test_other_oserror_means_dead(self):
        with mock.patch.object(liveness.os, "kill", side_effect=OSError(22, "EINVAL")):
            self.assertFalse(liveness.pid_alive(1234))


class LivenessWindowsGuardTests(unittest.TestCase):
    def test_never_calls_os_kill_on_windows(self):
        fake_psutil = mock.Mock()
        fake_psutil.pid_exists.return_value = True
        with mock.patch.object(paths, "IS_WINDOWS", True), mock.patch.object(
            liveness.os, "kill", side_effect=AssertionError("os.kill on Windows")
        ) as kill, mock.patch.dict(sys.modules, {"psutil": fake_psutil}):
            self.assertTrue(liveness.pid_alive(4321))
            kill.assert_not_called()
        fake_psutil.pid_exists.assert_called_once_with(4321)

    def test_windows_without_psutil_falls_back_to_ctypes(self):
        with mock.patch.object(paths, "IS_WINDOWS", True), mock.patch.object(
            liveness.os, "kill", side_effect=AssertionError("os.kill on Windows")
        ), mock.patch.dict(sys.modules, {"psutil": None}), mock.patch.object(
            liveness, "_pid_alive_windows_ctypes", return_value=False
        ) as ct:
            self.assertFalse(liveness.pid_alive(4321))
            ct.assert_called_once_with(4321)

    def test_windows_ctypes_failure_is_false(self):
        with mock.patch.object(paths, "IS_WINDOWS", True), mock.patch.dict(
            sys.modules, {"psutil": None}
        ), mock.patch.object(liveness, "_pid_alive_windows_ctypes", side_effect=OSError):
            self.assertFalse(liveness.pid_alive(4321))


class LiveSessionCountTests(unittest.TestCase):
    def test_counts_live_pid_files_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for name in ("10.json", "11.json", "12.json", "0.json", "-3.json", "abc.json",
                         "13.txt", ".json", "14.json.bak"):
                (d / name).write_text("{}")
            seen = []

            def alive(pid: int) -> bool:
                seen.append(pid)
                return pid in (10, 12)

            self.assertEqual(liveness.live_session_count(d, alive), 2)
            self.assertEqual(sorted(seen), [10, 11, 12])

    def test_missing_dir_is_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(liveness.live_session_count(Path(tmp) / "nope", lambda p: True), 0)

    def test_raising_predicate_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "5.json").write_text("{}")
            (d / "6.json").write_text("{}")

            def alive(pid: int) -> bool:
                if pid == 5:
                    raise RuntimeError
                return True

            self.assertEqual(liveness.live_session_count(d, alive), 1)

    @unittest.skipIf(sys.platform == "win32", "POSIX liveness path")
    def test_default_predicate_with_own_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "{}.json".format(os.getpid())).write_text("{}")
            (d / "{}.json".format(_unused_pid())).write_text("{}")
            self.assertEqual(liveness.live_session_count(d), 1)


# ------------------------------------------------------------------------------------ system


class DarkModeTests(unittest.TestCase):
    def _gs(self, answers):
        calls = []

        def get(key):
            calls.append(key)
            return answers.get(key)

        return get, calls

    def test_gtk_theme_env_dark_wins_without_subprocess(self):
        get, calls = self._gs({})
        for value in ("Adwaita:dark", "Arc-Dark", "adwaita-dark"):
            self.assertTrue(system._linux_is_dark({"GTK_THEME": value}, get), value)
        self.assertEqual(calls, [])

    def test_color_scheme(self):
        get, _ = self._gs({"color-scheme": "'prefer-dark'", "gtk-theme": "'Adwaita'"})
        self.assertTrue(system._linux_is_dark({}, get))
        get, calls = self._gs({"color-scheme": "'prefer-light'", "gtk-theme": "'Adwaita-dark'"})
        self.assertFalse(system._linux_is_dark({}, get))
        self.assertEqual(calls, ["color-scheme"])

    def test_default_scheme_falls_to_gtk_theme(self):
        get, _ = self._gs({"color-scheme": "'default'", "gtk-theme": "'Yaru-DARK'"})
        self.assertTrue(system._linux_is_dark({}, get))
        get, _ = self._gs({"color-scheme": "'default'", "gtk-theme": "'Adwaita'"})
        self.assertFalse(system._linux_is_dark({}, get))

    def test_nothing_readable_is_dark(self):
        get, calls = self._gs({})
        self.assertTrue(system._linux_is_dark({}, get))
        self.assertEqual(calls, ["color-scheme", "gtk-theme"])

    def test_gsettings_missing_binary_is_none(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(system._gsettings_get("color-scheme"))

    def test_gsettings_timeout_is_none(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gsettings", 1)):
            self.assertIsNone(system._gsettings_get("color-scheme"))

    def test_gsettings_nonzero_exit_is_none(self):
        done = subprocess.CompletedProcess([], 1, stdout=b"", stderr=None)
        with mock.patch("subprocess.run", return_value=done):
            self.assertIsNone(system._gsettings_get("gtk-theme"))

    def test_gsettings_call_is_safe(self):
        done = subprocess.CompletedProcess([], 0, stdout=b"'prefer-dark'\n", stderr=None)
        with mock.patch("subprocess.run", return_value=done) as run:
            self.assertEqual(system._gsettings_get("color-scheme"), "'prefer-dark'")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"])
        self.assertIs(kwargs.get("shell"), False)
        self.assertEqual(kwargs.get("timeout"), 1.0)
        self.assertIs(kwargs.get("stdin"), subprocess.DEVNULL)

    def test_linux_system_probe_all_failing_is_dark(self):
        env = {k: v for k, v in os.environ.items() if k != "GTK_THEME"}
        with mock.patch.object(paths, "IS_WINDOWS", False), mock.patch.object(
            paths, "IS_LINUX", True
        ), mock.patch.dict(os.environ, env, clear=True), mock.patch(
            "subprocess.run", side_effect=OSError
        ):
            self.assertTrue(system.system_is_dark())

    def test_other_os_is_dark(self):
        with mock.patch.object(paths, "IS_WINDOWS", False), mock.patch.object(
            paths, "IS_LINUX", False
        ), mock.patch("subprocess.run", side_effect=AssertionError("no subprocess")):
            self.assertTrue(system.system_is_dark())

    def test_windows_registry_unavailable_is_light(self):
        # No winreg on this host (or a missing key): D10 says missing key = light.
        with mock.patch.object(paths, "IS_WINDOWS", True), mock.patch.dict(
            sys.modules, {"winreg": None}
        ), mock.patch("subprocess.run", side_effect=AssertionError("no subprocess on Windows")):
            self.assertFalse(system.system_is_dark())


class SystemMiscTests(unittest.TestCase):
    def test_work_area_non_windows_is_screen(self):
        with mock.patch.object(paths, "IS_WINDOWS", False):
            self.assertEqual(system.work_area(50, 60, 1920, 1080), (0, 0, 1920, 1080))

    def test_work_area_windows_failure_falls_back(self):
        with mock.patch.object(paths, "IS_WINDOWS", True), mock.patch.object(
            system, "_windows_work_area", side_effect=OSError
        ):
            self.assertEqual(system.work_area(0, 0, 800, 600), (0, 0, 800, 600))

    def test_dpi_awareness_is_noop_and_safe(self):
        with mock.patch.object(paths, "IS_WINDOWS", False):
            self.assertIsNone(system.enable_dpi_awareness())
        with mock.patch.object(paths, "IS_WINDOWS", True):  # no WinDLL on this host: swallowed
            self.assertIsNone(system.enable_dpi_awareness())


# ------------------------------------------------------------------------------------ settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "claudemon"
        self.path = self.dir / "settings.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, content):
        self.dir.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            self.path.write_bytes(content)
        else:
            self.path.write_text(content, encoding="utf-8")

    def test_round_trip(self):
        s = Settings(TimeRange.WEEK, ThemeChoice.SYSTEM, True, True, 1904, -20)
        self.assertTrue(settings.save(s, self.path))
        self.assertEqual(settings.load(self.path), s)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            data,
            {"version": 1, "range": "7d", "theme": "system", "compact": True,
             "reduce_motion": True, "window": {"right": 1904, "top": -20}},
        )

    def test_round_trip_every_enum_value(self):
        for r in TimeRange:
            for t in ThemeChoice:
                s = Settings(range=r, theme=t)
                self.assertTrue(settings.save(s, self.path))
                self.assertEqual(settings.load(self.path), s)

    def test_no_always_on_top_persisted(self):
        settings.save(Settings(), self.path)
        text = self.path.read_text(encoding="utf-8").lower()
        self.assertNotIn("top_most", text)
        self.assertNotIn("topmost", text)
        self.assertNotIn("always", text)
        self.assertEqual(
            set(json.loads(text)), {"version", "range", "theme", "compact", "reduce_motion", "window"}
        )

    def test_window_none_written_as_null(self):
        settings.save(Settings(), self.path)
        self.assertIsNone(json.loads(self.path.read_text(encoding="utf-8"))["window"])

    def test_missing_file_is_defaults(self):
        self.assertEqual(settings.load(self.path), Settings())

    def test_corrupt_files_are_defaults(self):
        for content in ("", "{", "not json", "[1, 2]", "null", "42", '"str"',
                        b"\xff\xfe\x00garbage", "[" * 100000):
            self._write(content)
            self.assertEqual(settings.load(self.path), Settings(), repr(content)[:40])

    def test_oversized_file_is_defaults(self):
        self._write(json.dumps({"range": "7d", "pad": "x" * (settings.MAX_FILE_BYTES + 10)}))
        self.assertEqual(settings.load(self.path), Settings())

    def test_partial_file_keeps_valid_keys(self):
        self._write(json.dumps({"range": "24h", "compact": True}))
        self.assertEqual(settings.load(self.path), Settings(range=TimeRange.DAY, compact=True))

    def test_wrong_types_fall_back_per_key(self):
        self._write(json.dumps({
            "range": "2h", "theme": 1, "compact": "yes", "reduce_motion": 1,
            "window": {"right": 100, "top": "5"}, "extra": {"ignored": True},
        }))
        self.assertEqual(settings.load(self.path), Settings())
        self._write(json.dumps({
            "range": ["1h"], "theme": "claude", "compact": 0, "reduce_motion": True,
            "window": [1, 2],
        }))
        self.assertEqual(
            settings.load(self.path), Settings(theme=ThemeChoice.CLAUDE, reduce_motion=True)
        )

    def test_window_validation(self):
        bad = [{"right": True, "top": 5}, {"right": 1.5, "top": 5}, {"right": 10},
               {"right": 10 ** 12, "top": 0}, {"right": None, "top": None}, "10,20", None]
        for w in bad:
            self._write(json.dumps({"window": w}))
            s = settings.load(self.path)
            self.assertIsNone(s.window_right, repr(w))
            self.assertIsNone(s.window_top, repr(w))
        self._write(json.dumps({"window": {"right": -300, "top": 0}}))
        s = settings.load(self.path)
        self.assertEqual((s.window_right, s.window_top), (-300, 0))

    def test_save_creates_folder_and_leaves_no_temp_files(self):
        self.assertFalse(self.dir.exists())
        self.assertTrue(settings.save(Settings(compact=True), self.path))
        self.assertEqual([p.name for p in self.dir.iterdir()], ["settings.json"])

    def test_atomic_save_keeps_old_file_on_failure(self):
        old = Settings(range=TimeRange.FIVE_HOURS)
        self.assertTrue(settings.save(old, self.path))
        with mock.patch.object(settings.os, "replace", side_effect=OSError("disk full")):
            self.assertFalse(settings.save(Settings(range=TimeRange.WEEK), self.path))
        self.assertEqual(settings.load(self.path), old)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["settings.json"])

    def test_atomic_save_uses_replace_in_same_folder(self):
        real_replace = os.replace
        seen = []

        def spy(src, dst):
            seen.append((Path(src).parent, Path(dst)))
            return real_replace(src, dst)

        with mock.patch.object(settings.os, "replace", side_effect=spy):
            self.assertTrue(settings.save(Settings(), self.path))
        self.assertEqual(seen, [(self.dir, self.path)])

    def test_save_unwritable_location_returns_false(self):
        blocker = Path(self._tmp.name) / "file"
        blocker.write_text("x")
        self.assertFalse(settings.save(Settings(), blocker / "sub" / "settings.json"))

    def test_default_path_used(self):
        with mock.patch.object(paths, "settings_path", return_value=self.path):
            self.assertTrue(settings.save(Settings(theme=ThemeChoice.CLAUDE)))
            self.assertEqual(settings.load().theme, ThemeChoice.CLAUDE)


# ------------------------------------------------------------------------------------ worker


class FakeStore:
    """Implements only the Store contract used by Worker: refresh() and snapshot(range)."""

    def __init__(self, fail_first: int = 0):
        self.refreshes = 0
        self.ranges = []
        self.fail_first = fail_first
        self.thread_names = set()

    def refresh(self, now=None, force_discover=False):
        self.thread_names.add(threading.current_thread().name)
        self.refreshes += 1
        if self.refreshes <= self.fail_first:
            raise RuntimeError("boom")

    def snapshot(self, range, now=None):
        self.ranges.append(range)
        return Snapshot(range=range, loaded=True)


def _get(q: "queue.Queue", kind: str, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("no {!r} message within {}s".format(kind, timeout))
        k, v = q.get(timeout=remaining)
        if k == kind:
            return v


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.q: "queue.Queue" = queue.Queue()
        self.workers = []

    def tearDown(self):
        for w in self.workers:
            w.stop()

    def _worker(self, store, **kw):
        kw.setdefault("interval", 30.0)  # long: tests rely on wake-ups, not on the timer
        kw.setdefault("dark_probe", lambda: True)
        w = worker.Worker(store, self.q, TimeRange.HOUR, **kw)
        self.workers.append(w)
        return w

    def test_delivers_snapshot_on_named_daemon_thread(self):
        store = FakeStore()
        w = self._worker(store)
        w.start()
        snap = _get(self.q, "snapshot")
        self.assertIsInstance(snap, Snapshot)
        self.assertEqual(snap.range, TimeRange.HOUR)
        self.assertEqual(store.thread_names, {"claudemon-worker"})
        self.assertTrue(w._thread.daemon)

    def test_request_changes_range_and_wakes_now(self):
        store = FakeStore()
        w = self._worker(store)
        w.start()
        _get(self.q, "snapshot")
        t0 = time.monotonic()
        w.request(TimeRange.WEEK)
        snap = _get(self.q, "snapshot")
        self.assertEqual(snap.range, TimeRange.WEEK)
        self.assertLess(time.monotonic() - t0, 5.0)  # far below the 30 s interval

    def test_interval_loop_repeats(self):
        store = FakeStore()
        w = self._worker(store, interval=0.01)
        w.start()
        for _ in range(3):
            _get(self.q, "snapshot")
        self.assertGreaterEqual(store.refreshes, 3)

    def test_stop_ends_thread_quickly(self):
        w = self._worker(FakeStore())
        w.start()
        _get(self.q, "snapshot")
        t0 = time.monotonic()
        w.stop()
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertFalse(w._thread.is_alive())

    def test_stop_before_start_and_twice_is_safe(self):
        w = self._worker(FakeStore())
        w.stop()
        w.stop()

    def test_exceptions_are_swallowed_and_loop_continues(self):
        store = FakeStore(fail_first=2)
        w = self._worker(store, interval=0.01)
        with mock.patch.object(worker.traceback, "print_exc"):
            w.start()
            snap = _get(self.q, "snapshot")
        self.assertEqual(snap.range, TimeRange.HOUR)
        self.assertGreaterEqual(store.refreshes, 3)
        self.assertTrue(w._thread.is_alive())

    def test_no_dark_probe_unless_watching(self):
        probe = mock.Mock(return_value=True)
        w = self._worker(FakeStore(), interval=0.01, dark_probe=probe)
        w.start()
        for _ in range(3):
            _get(self.q, "snapshot")
        probe.assert_not_called()

    def test_watch_probes_immediately_then_waits_for_interval(self):
        probe = mock.Mock(return_value=False)
        w = self._worker(FakeStore(), dark_interval=3600.0, dark_probe=probe)
        w.start()
        _get(self.q, "snapshot")
        w.watch_system_theme(True)
        self.assertIs(_get(self.q, "dark"), False)
        w.request(TimeRange.DAY)
        _get(self.q, "snapshot")
        self.assertEqual(probe.call_count, 1)

    def test_probe_repeats_after_dark_interval(self):
        answers = iter([True, False, True, False, True, False])
        w = self._worker(FakeStore(), interval=0.01, dark_interval=0.0,
                         dark_probe=lambda: next(answers, True))
        w.watch_system_theme(True)
        w.start()
        self.assertIs(_get(self.q, "dark"), True)
        self.assertIs(_get(self.q, "dark"), False)

    def test_probe_error_is_swallowed(self):
        w = self._worker(FakeStore(), dark_probe=mock.Mock(side_effect=RuntimeError))
        w.watch_system_theme(True)
        w.start()
        self.assertIsInstance(_get(self.q, "snapshot"), Snapshot)

    def test_dark_message_precedes_snapshot_in_same_iteration(self):
        w = self._worker(FakeStore(), dark_probe=lambda: True)
        w.watch_system_theme(True)
        w.start()
        first = self.q.get(timeout=3.0)
        self.assertEqual(first, ("dark", True))
        self.assertEqual(self.q.get(timeout=3.0)[0], "snapshot")

    def test_default_dark_probe_is_system(self):
        w = worker.Worker(FakeStore(), self.q, TimeRange.HOUR)
        self.assertIs(w._dark_probe, system.system_is_dark)


if __name__ == "__main__":
    unittest.main()
