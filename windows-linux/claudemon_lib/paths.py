"""Platform paths: Claude Code home directory and the claudemon settings file. architecture.md D11."""
from __future__ import annotations

import ntpath
import os
import posixpath
import sys
from pathlib import Path

IS_WINDOWS: bool = sys.platform == "win32"
IS_LINUX: bool = sys.platform.startswith("linux")

APP_DIR_NAME = "claudemon"
SETTINGS_FILE_NAME = "settings.json"


def claude_home() -> Path:
    """Path.home() / ".claude" (i.e. %USERPROFILE%\\.claude on Windows)."""
    return Path.home() / ".claude"


def settings_path() -> Path:
    """The claudemon settings.json path. See architecture.md D11.

    Windows: %APPDATA%\\claudemon\\settings.json, falling back to ~\\AppData\\Roaming\\claudemon.
    Elsewhere: $XDG_CONFIG_HOME/claudemon/settings.json, else ~/.config/claudemon/settings.json.
    Relative env values are ignored (XDG spec: such values are invalid).
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        base = Path(appdata) if appdata and ntpath.isabs(appdata) else Path.home() / "AppData" / "Roaming"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", "")
        base = Path(xdg) if xdg and posixpath.isabs(xdg) else Path.home() / ".config"
    return base / APP_DIR_NAME / SETTINGS_FILE_NAME
