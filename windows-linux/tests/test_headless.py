"""Every pure module must be importable without ever pulling in tkinter (patterns.md #1). Run in a
subprocess: once a test process imports tkinter for any other reason, sys.modules would stay
polluted for every later test in this process, so the check must happen in a fresh interpreter.
"""
from __future__ import annotations

import subprocess
import sys
import unittest

import support  # noqa: F401
from support import ROOT

PURE_MODULES = (
    "model",
    "timeparse",
    "store",
    "paths",
    "liveness",
    "system",
    "settings",
    "worker",
    "theme",
    "formatting",
    "layout",
    "animation",
    "rows",
    "shape",
)


class TestHeadless(unittest.TestCase):
    def test_pure_modules_never_import_tkinter(self) -> None:
        imports = "\n".join(f"import claudemon_lib.{name}" for name in PURE_MODULES)
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            f"{imports}\n"
            "assert 'tkinter' not in sys.modules, sorted(k for k in sys.modules if 'tkinter' in k)\n"
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)

    def test_each_pure_module_individually_avoids_tkinter(self) -> None:
        # Belt and suspenders: importing them one at a time (instead of all of them together) rules
        # out a module that only avoids tkinter because an earlier import already pulled it in
        # (impossible here, since none of them import tkinter, but this pins that invariant per
        # module rather than for the group as a whole).
        for name in PURE_MODULES:
            script = (
                "import sys\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                f"import claudemon_lib.{name}\n"
                "assert 'tkinter' not in sys.modules\n"
            )
            with self.subTest(module=name):
                result = subprocess.run(
                    [sys.executable, "-c", script],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
