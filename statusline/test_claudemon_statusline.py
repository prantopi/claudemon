"""Tests for claudemon_statusline.py.

Run with: python3 -m unittest discover -s statusline

Each test invokes the script as a real subprocess (as Claude Code would),
pointing CLAUDEMON_TEST_LIMITS_FILE (a test-only override, see the comment
on limits_file_path() in claudemon_statusline.py) at a throwaway temp file
so no test ever touches a real ~/.claude/claudemon/limits.json.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent / "claudemon_statusline.py"


def run_script(payload_bytes, args=None, limits_file=None, timeout=10, extra_env=None):
    env = dict(os.environ)
    if limits_file is not None:
        env["CLAUDEMON_TEST_LIMITS_FILE"] = str(limits_file)
    if extra_env:
        env.update(extra_env)
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

    def test_truncated_stdin_does_not_reach_chained_command(self):
        huge_payload = b'{"model": {"display_name": "Opus"}, "padding": "' + (
            b"x" * (2 * 1024 * 1024)
        ) + b'"}'
        child = [sys.executable, "-c", "print('should-not-run')"]
        result = run_script(huge_payload, args=child, limits_file=self.limits_file)

        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.limits_file.exists())
        # The chained command must not see truncated/garbage stdin: fall
        # back to the default line instead of invoking it.
        self.assertEqual(result.stdout.decode().strip(), "Claude")

    # -- overflow handling -------------------------------------------------

    def test_huge_int_percentage_still_runs_chained_command(self):
        payload = {
            "model": {"display_name": "Opus"},
            "rate_limits": {
                # math.isfinite() raises OverflowError on ints this large;
                # that must be swallowed, not crash extraction and skip the
                # chained command.
                "five_hour": {"used_percentage": 10 ** 400, "resets_at": 1738425600},
            },
        }
        child = [sys.executable, "-c", "print('ok-chain')"]
        result = run_script(
            json.dumps(payload).encode(), args=child, limits_file=self.limits_file
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.decode().strip(), "ok-chain")
        self.assertFalse(self.limits_file.exists())

    # -- bounds -------------------------------------------------------------

    def test_percentage_bounds_are_inclusive(self):
        payload = {
            "rate_limits": {
                "five_hour": {"used_percentage": 0, "resets_at": 1738425600},
                "seven_day": {"used_percentage": 100, "resets_at": 1738425600},
            }
        }
        result = run_script(json.dumps(payload).encode(), limits_file=self.limits_file)

        self.assertEqual(result.returncode, 0)
        written = json.loads(self.limits_file.read_text())
        self.assertIn("five_hour", written)
        self.assertIn("seven_day", written)

    def test_percentage_out_of_bounds_is_rejected(self):
        payload = {
            "rate_limits": {
                "five_hour": {"used_percentage": -0.1, "resets_at": 1738425600},
                "seven_day": {"used_percentage": 100.1, "resets_at": 1738425600},
            }
        }
        result = run_script(json.dumps(payload).encode(), limits_file=self.limits_file)

        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.limits_file.exists())

    def test_resets_at_bounds(self):
        payload = {
            "rate_limits": {
                "five_hour": {"used_percentage": 50, "resets_at": 1e11 - 1},
            }
        }
        result = run_script(json.dumps(payload).encode(), limits_file=self.limits_file)

        self.assertEqual(result.returncode, 0)
        written = json.loads(self.limits_file.read_text())
        self.assertIn("five_hour", written)

    def test_resets_at_out_of_bounds_is_rejected(self):
        payload = {
            "rate_limits": {
                "five_hour": {"used_percentage": 50, "resets_at": 1e11},
                "seven_day": {"used_percentage": 50, "resets_at": 0},
            }
        }
        result = run_script(json.dumps(payload).encode(), limits_file=self.limits_file)

        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.limits_file.exists())

    # -- display_name sanitising ---------------------------------------------

    def test_display_name_control_chars_and_newlines_become_spaces(self):
        payload = {"model": {"display_name": "Op\x00us\nPro  Max\t!"}}
        result = run_script(json.dumps(payload).encode(), limits_file=self.limits_file)

        self.assertEqual(result.stdout.decode().strip(), "Op us Pro Max !")

    def test_display_name_capped_at_64_chars(self):
        payload = {"model": {"display_name": "A" * 100}}
        result = run_script(json.dumps(payload).encode(), limits_file=self.limits_file)

        self.assertEqual(result.stdout.decode().strip(), "A" * 64)

    # -- Windows / non-UTF-8 stdout encoding --------------------------------

    def test_chained_output_passes_through_as_raw_bytes_under_cp1252(self):
        # On Windows, when stdout is a pipe, Python falls back to the
        # locale code page (cp1252) unless told otherwise. The bridge must
        # pass the chained command's stdout through as raw bytes rather
        # than decoding/re-encoding it, so non-cp1252-representable output
        # (emoji, box-drawing) survives untouched.
        payload = {"model": {"display_name": "Opus"}}
        child = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write('\U0001F680 main │ x'.encode('utf-8'))",
        ]
        result = run_script(
            json.dumps(payload).encode(),
            args=child,
            limits_file=self.limits_file,
            extra_env={"PYTHONIOENCODING": "cp1252"},
        )

        expected = "\U0001F680 main │ x".encode("utf-8") + b"\n"
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, expected)

    def test_default_line_dot_separator_is_utf8_under_cp1252(self):
        # The default line's "·" separator must be written as UTF-8 bytes
        # even when the process's locale encoding is cp1252 (where "·"
        # would otherwise become the single byte 0xB7).
        payload = {
            "model": {"display_name": "Sonnet"},
            "rate_limits": {
                "five_hour": {"used_percentage": 5, "resets_at": 1738425600},
            },
        }
        result = run_script(
            json.dumps(payload).encode(),
            limits_file=self.limits_file,
            extra_env={"PYTHONIOENCODING": "cp1252"},
        )

        expected = "Sonnet · 5h 5%\n".encode("utf-8")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, expected)


if __name__ == "__main__":
    unittest.main()
