"""DPI awareness, OS light/dark mode, monitor work area. See architecture.md D4/D10.

Security-sensitive (architecture.md security-sensitive area #2):
- subprocess: argument lists only, shell=False, timeout=1, stdin=DEVNULL, output captured, and
  FileNotFoundError / TimeoutExpired / OSError caught. No subprocess is ever run on Windows.
- winreg: read-only access (KEY_READ). ctypes calls declare argtypes/restype and are guarded by try.
ctypes/winreg/subprocess are imported lazily inside the functions that need them.
"""
from __future__ import annotations

import os
from typing import Callable, Mapping, Optional, Tuple

from . import paths

_GSETTINGS_TIMEOUT = 1.0
_GSETTINGS_SCHEMA = "org.gnome.desktop.interface"


def enable_dpi_awareness() -> None:
    """Windows only; call before tk.Tk() is created. No-op elsewhere. Never raises. architecture.md D4.

    System-aware (PROCESS_SYSTEM_DPI_AWARE = 1) via shcore.SetProcessDpiAwareness, falling back
    to user32.SetProcessDPIAware on Windows before 8.1. Per-monitor-v2 is deliberately not used.
    """
    if not paths.IS_WINDOWS:
        return
    try:
        import ctypes

        try:
            shcore = ctypes.WinDLL("shcore")
            fn = shcore.SetProcessDpiAwareness
            fn.argtypes = [ctypes.c_int]
            fn.restype = ctypes.c_long  # HRESULT
            if fn(1) == 0:  # S_OK
                return
            # E_ACCESSDENIED means awareness was already set (manifest or earlier call): fine.
        except Exception:
            pass
        user32 = ctypes.WinDLL("user32")
        legacy = user32.SetProcessDPIAware
        legacy.argtypes = []
        legacy.restype = ctypes.c_int
        legacy()
    except Exception:
        return


# ---------------------------------------------------------------------------- dark mode (D10)


def _windows_is_dark() -> bool:
    """AppsUseLightTheme: 0 = dark, 1 = light; missing key/value = light."""
    try:
        import winreg  # type: ignore[import-not-found]

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            0,
            winreg.KEY_READ,
        ) as key:
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return int(value) == 0
    except Exception:
        return False


def _gsettings_get(key: str) -> Optional[str]:
    """`gsettings get org.gnome.desktop.interface <key>` stdout stripped, or None on any failure."""
    try:
        import subprocess

        proc = subprocess.run(
            ["gsettings", "get", _GSETTINGS_SCHEMA, key],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_GSETTINGS_TIMEOUT,
            check=False,
        )
    except (FileNotFoundError, OSError, ValueError):
        return None
    except Exception:  # subprocess.TimeoutExpired and anything unexpected
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        text = proc.stdout.decode("utf-8", "replace")
    except Exception:
        return None
    return text.strip()[:512]


def _linux_is_dark(
    environ: Optional[Mapping[str, str]] = None,
    gsettings_get: Optional[Callable[[str], Optional[str]]] = None,
) -> bool:
    """architecture.md D10, Linux branch: first decisive answer wins; nothing readable -> dark."""
    env = environ if environ is not None else os.environ
    if gsettings_get is None:
        gsettings_get = _gsettings_get
    gtk_theme_env = (env.get("GTK_THEME") or "").lower()
    if ":dark" in gtk_theme_env or "-dark" in gtk_theme_env:
        return True

    scheme = gsettings_get("color-scheme")
    if scheme is not None:
        s = scheme.strip("'\"")
        if s == "prefer-dark":
            return True
        if s == "prefer-light":
            return False

    theme = gsettings_get("gtk-theme")
    if theme is not None:
        return "dark" in theme.lower()
    return True


def system_is_dark() -> bool:
    """Detect the OS light/dark theme. May block up to ~2 s on Linux: worker thread only.

    architecture.md D10. Never raises; undetectable -> dark (Windows: missing key -> light).
    """
    try:
        if paths.IS_WINDOWS:
            return _windows_is_dark()
        if paths.IS_LINUX:
            return _linux_is_dark()
    except Exception:
        return True
    return True  # other OS (macOS dev runs)


# ---------------------------------------------------------------------------- work area


def _windows_work_area(x: int, y: int) -> Optional[Tuple[int, int, int, int]]:
    import ctypes
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    user32 = ctypes.WinDLL("user32")
    monitor_from_point = user32.MonitorFromPoint
    monitor_from_point.argtypes = [wintypes.POINT, wintypes.DWORD]
    monitor_from_point.restype = wintypes.HANDLE  # HMONITOR
    get_monitor_info = user32.GetMonitorInfoW
    get_monitor_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
    get_monitor_info.restype = wintypes.BOOL

    hmon = monitor_from_point(wintypes.POINT(int(x), int(y)), 2)  # MONITOR_DEFAULTTONEAREST
    if not hmon:
        return None
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not get_monitor_info(hmon, ctypes.byref(info)):
        return None
    r = info.rcWork
    if r.right <= r.left or r.bottom <= r.top:
        return None
    return (int(r.left), int(r.top), int(r.right), int(r.bottom))


def work_area(x: int, y: int, screen_w: int, screen_h: int) -> Tuple[int, int, int, int]:
    """(left, top, right, bottom) of the work area of the monitor nearest (x, y).

    Windows: rcWork of MonitorFromPoint((x, y), MONITOR_DEFAULTTONEAREST=2) via GetMonitorInfoW.
    Any failure or non-Windows: (0, 0, screen_w, screen_h). Fast enough for the UI thread.
    """
    fallback = (0, 0, int(screen_w), int(screen_h))
    if not paths.IS_WINDOWS:
        return fallback
    try:
        area = _windows_work_area(x, y)
    except Exception:
        return fallback
    return area if area is not None else fallback
