"""Tests for claudemon_statusline.py.

Run with: python3 -m unittest discover -s statusline

Each test invokes the script as a real subprocess (as Claude Code would),
pointing CLAUDEMON_LIMITS_FILE at a throwaway temp file so no test ever
touches a real ~/.claude/claudemon/limits.json.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent / "claudemon_statusline.py"


def run_script(payload_bytes, args=None, limits_file=None, timeout=10):
    env = dict(os.environ)
    if limits_file is not None:
        env["CLAUDEMON_LIMITS_FILE"] = str(limits_file)
    cmd = [sys.executable, str(SCRIPT_PATH)] + list(args or [])
    return subprocess.run(
        cmd,
        input=payload_bytes,
        capture_output=True,
        timeout=timeout,
        env=env,
    )


class ClaudemonStatuslineTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.limits_file = Path(self._tmpdir.name) / "limits.json"

    # -- limits.json writing -------------------------------------------------

    def test_valid_input_writes_expected_json(self):
        payload = {
            "model": {"display_name": "Opus"},
            "rate_limits": {
                "five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
                "seven_day": {"used_percentage": 41.2, "resets_at": 1738857600},
            },
        }
        before = 0
        import time

        before = time.time()
        result = run_script(json.dumps(payload).encode(), limits_file=self.limits_file)
        after = time.time()

        self.assertEqual(result.returncode, 0)
        self.assertTrue(self.limits_file.exists())
        written = json.loads(self.limits_file.read_text())

        self.assertEqual(written["version"], 1)
        self.assertTrue(before - 1 <= written["updated_at"] <= after + 1)
        self.assertEqual(
            written["five_hour"], {"used_percentage": 23.5, "resets_at": 1738425600}
        )
        self.assertEqual(
            written["seven_day"], {"used_percentage": 41.2, "resets_at": 1738857600}
        )
        self.assertEqual(result.stdout.decode().strip(), "Opus · 5h 23% · 7d 41%")

    def test_missing_rate_limits_writes_nothing(self):
        payload = {"model": {"display_name": "Sonnet"}}
        result = run_script(json.dumps(payload).encode(), limits_file=self.limits_file)

        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.limits_file.exists())
        self.assertEqual(result.stdout.decode().strip(), "Sonnet")

    def test_empty_rate_limits_object_writes_nothing(self):
        payload = {"model": {"display_name": "Sonnet"}, "rate_limits": {}}
        result = run_script(json.dumps(payload).encode(), limits_file=self.limits_file)

        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.limits_file.exists())

    def test_partial_windows_only_five_hour_present(self):
        payload = {
            "model": {"display_name": "Opus"},
            "rate_limits": {
                "five_hour": {"used_percentage": 10, "resets_at": 1738425600},
            },
        }
        result = run_script(json.dumps(payload).encode(), limits_file=self.limits_file)

        self.assertEqual(result.returncode, 0)
        written = json.loads(self.limits_file.read_text())
        self.assertIn("five_hour", written)
        self.assertNotIn("seven_day", written)
        self.assertEqual(result.stdout.decode().strip(), "Opus · 5h 10%")

    def test_partial_windows_only_seven_day_present(self):
        payload = {
            "model": {"display_name": "Opus"},
            "rate_limits": {
                "seven_day": {"used_percentage": 99, "resets_at": 1738425600},
            },
        }
        result = run_script(json.dumps(payload).encode(), limits_file=self.limits_file)

        self.assertEqual(result.returncode, 0)
        written = json.loads(self.limits_file.read_text())
        self.assertNotIn("five_hour", written)
        self.assertIn("seven_day", written)
        self.assertEqual(result.stdout.decode().strip(), "Opus · 7d 99%")

    def test_invalid_values_are_skipped(self):
        payload = {
            "model": {"display_name": "Opus"},
            "rate_limits": {
                # percentage out of range
                "five_hour": {"used_percentage": 1500, "resets_at": 1738425600},
                # bool is not a valid number even though bool is an int subclass
                "seven_day": {"used_percentage": True, "resets_at": 1738425600},
            },
        }
        result = run_script(json.dumps(payload).encode(), limits_file=self.limits_file)

        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.limits_file.exists())
        self.assertEqual(result.stdout.decode().strip(), "Opus")

    def test_invalid_resets_at_skips_window(self):
        payload = {
            "rate_limits": {
                "five_hour": {"used_percentage": 50, "resets_at": 0},
                "seven_day": {"used_percentage": 50, "resets_at": -5},
            },
        }
        result = run_script(json.dumps(payload).encode(), limits_file=self.limits_file)

        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.limits_file.exists())

    def test_non_numeric_percentage_skips_window(self):
        payload = {
            "rate_limits": {
                "five_hour": {"used_percentage": "23.5", "resets_at": 1738425600},
            },
        }
        result = run_script(json.dumps(payload).encode(), limits_file=self.limits_file)

        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.limits_file.exists())

    def test_malformed_json_does_not_crash(self):
        result = run_script(b"{not valid json!!", limits_file=self.limits_file)

        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.limits_file.exists())
        self.assertEqual(result.stdout.decode().strip(), "Claude")

    def test_empty_stdin_does_not_crash(self):
        result = run_script(b"", limits_file=self.limits_file)

        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.limits_file.exists())
        self.assertEqual(result.stdout.decode().strip(), "Claude")

    # -- chained command -------------------------------------------------

    def test_chained_command_output_is_printed(self):
        payload = {"model": {"display_name": "Opus"}}
        child = [
            sys.executable,
            "-c",
            "import sys; data = sys.stdin.read(); print('CHAINED:' + data.strip())",
        ]
        result = run_script(
            json.dumps(payload).encode(), args=child, limits_file=self.limits_file
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.decode().strip(), "CHAINED:" + json.dumps(payload)
        )

    def test_chained_command_receives_full_stdin_and_writes_limits(self):
        payload = {
            "model": {"display_name": "Opus"},
            "rate_limits": {
                "five_hour": {"used_percentage": 12, "resets_at": 1738425600},
            },
        }
        child = [sys.executable, "-c", "print('ok')"]
        result = run_script(
            json.dumps(payload).encode(), args=child, limits_file=self.limits_file
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.decode().strip(), "ok")
        self.assertTrue(self.limits_file.exists())

    def test_chained_command_failure_falls_back_to_default_line(self):
        payload = {"model": {"display_name": "Opus"}}
        child = [sys.executable, "-c", "import sys; sys.exit(1)"]
        result = run_script(
            json.dumps(payload).encode(), args=child, limits_file=self.limits_file
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.decode().strip(), "Opus")

    def test_chained_command_missing_binary_falls_back_to_default_line(self):
        payload = {"model": {"display_name": "Opus"}}
        child = ["this-binary-does-not-exist-claudemon-test"]
        result = run_script(
            json.dumps(payload).encode(), args=child, limits_file=self.limits_file
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.decode().strip(), "Opus")

    # -- default line text -------------------------------------------------

    def test_default_line_model_only(self):
        payload = {"model": {"display_name": "Haiku"}}
        result = run_script(json.dumps(payload).encode(), limits_file=self.limits_file)
        self.assertEqual(result.stdout.decode().strip(), "Haiku")

    def test_default_line_missing_model_falls_back_to_claude(self):
        payload = {"rate_limits": {}}
        result = run_script(json.dumps(payload).encode(), limits_file=self.limits_file)
        self.assertEqual(result.stdout.decode().strip(), "Claude")

    def test_default_line_both_windows(self):
        payload = {
            "model": {"display_name": "Sonnet"},
            "rate_limits": {
                "five_hour": {"used_percentage": 5, "resets_at": 1738425600},
                "seven_day": {"used_percentage": 88, "resets_at": 1738857600},
            },
        }
        result = run_script(json.dumps(payload).encode(), limits_file=self.limits_file)
        self.assertEqual(
            result.stdout.decode().strip(), "Sonnet · 5h 5% · 7d 88%"
        )

    # -- stdin size cap -------------------------------------------------

    def test_oversized_stdin_is_capped_without_crashing(self):
        huge_payload = b'{"model": {"display_name": "Opus"}, "padding": "' + (
            b"x" * (2 * 1024 * 1024)
        ) + b'"}'
        result = run_script(huge_payload, limits_file=self.limits_file)

        self.assertEqual(result.returncode, 0)
        # Truncated JSON is invalid, so it should fall back gracefully.
        self.assertFalse(self.limits_file.exists())


if __name__ == "__main__":
    unittest.main()
