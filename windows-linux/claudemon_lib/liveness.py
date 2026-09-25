"""Process liveness checks without a hard psutil dependency. See architecture.md D7.

Security-sensitive (architecture.md security-sensitive area #1):
- os.kill is NEVER called on Windows (any signal value terminates the target there).
- pids <= 0 are rejected before any OS call (POSIX kill(0|-n, 0) addresses process groups).
- ctypes argtypes/restype are always declared (HANDLE is pointer-sized) and handles always closed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, Optional

from . import paths

_MAX_PID = 0xFFFFFFFF  # Windows DWORD; far above any POSIX pid_max as well

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5

_win_api = None  # cached (kernel32, OpenProcess, GetExitCodeProcess, CloseHandle, GetLastError)


def _on_windows() -> bool:
    # Checked two ways on purpose: a wrong answer here would mean calling os.kill on Windows.
    return paths.IS_WINDOWS or sys.platform == "win32" or os.name == "nt"


def _normalize_pid(pid: object) -> Optional[int]:
    # Only real ints (bool is an int subclass; reject it, and strings/floats as well).
    if isinstance(pid, bool) or not isinstance(pid, int):
        return None
    if pid <= 0 or pid > _MAX_PID:
        return None
    return int(pid)


def _pid_alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # exists, owned by another user
    except (ProcessLookupError, OverflowError, ValueError):
        return False
    except OSError:
        return False
    return True


def _load_win_api():
    global _win_api
    if _win_api is None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        _win_api = (ctypes, wintypes, open_process, get_exit_code, close_handle)
    return _win_api


def _pid_alive_windows_ctypes(pid: int) -> bool:
    ctypes, wintypes, open_process, get_exit_code, close_handle = _load_win_api()
    handle = open_process(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Access denied means the process exists (e.g. elevated / another user's).
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD(0)
        if not get_exit_code(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        close_handle(handle)


def _pid_alive_windows(pid: int) -> bool:
    try:
        import psutil  # type: ignore[import-not-found]
    except Exception:
        psutil = None
    if psutil is not None:
        try:
            return bool(psutil.pid_exists(pid))
        except Exception:
            pass  # fall through to ctypes
    try:
        return _pid_alive_windows_ctypes(pid)
    except Exception:
        return False


def pid_alive(pid: int) -> bool:
    """True if pid is a live process; False for pid <= 0. Never raises. architecture.md D7."""
    p = _normalize_pid(pid)
    if p is None:
        return False
    try:
        if _on_windows():
            return _pid_alive_windows(p)
        return _pid_alive_posix(p)
    except Exception:
        return False


def live_session_count(sessions_dir: Path, pid_alive: Callable[[int], bool] = pid_alive) -> int:
    """Count of *.json files in sessions_dir whose stem is a pid for which pid_alive is True.

    data-flow.md §6 (claudemon.swift 172-176). Missing/unreadable directory -> 0. Never raises.
    """
    try:
        names = os.listdir(sessions_dir)
    except (OSError, TypeError, ValueError):
        return 0
    count = 0
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            pid = int(name[:-5])
        except ValueError:
            continue
        if pid <= 0:
            continue
        try:
            if pid_alive(pid):
                count += 1
        except Exception:
            continue
    return count
