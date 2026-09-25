"""timeparse.parse_timestamp: ISO 8601 variants (architecture D8). Task P1."""
from __future__ import annotations

import calendar
import unittest

import support  # noqa: F401

from claudemon_lib.timeparse import parse_timestamp

BASE = float(calendar.timegm((2026, 9, 24, 10, 30, 15, 0, 0, 0)))


class TestParseTimestamp(unittest.TestCase):
    def test_zulu_with_millis(self) -> None:
        self.assertAlmostEqual(parse_timestamp("2026-09-24T10:30:15.123Z"), BASE + 0.123, places=6)

    def test_lowercase_z_and_space_separator(self) -> None:
        self.assertEqual(parse_timestamp("2026-09-24 10:30:15z"), BASE)

    def test_no_fraction(self) -> None:
        self.assertEqual(parse_timestamp("2026-09-24T10:30:15Z"), BASE)

    def test_fraction_any_length(self) -> None:
        cases = {"5": 0.5, "12": 0.12, "123": 0.123, "123456": 0.123456, "123456789": 0.123456789,
                 "000000000001": 1e-12, "9" * 40: 1.0}
        for frac, want in cases.items():
            with self.subTest(frac=frac):
                self.assertAlmostEqual(parse_timestamp("2026-09-24T10:30:15." + frac + "Z"), BASE + want, places=6)

    def test_comma_fraction(self) -> None:
        self.assertAlmostEqual(parse_timestamp("2026-09-24T10:30:15,25Z"), BASE + 0.25, places=6)

    def test_offsets(self) -> None:
        self.assertEqual(parse_timestamp("2026-09-24T12:30:15+02:00"), BASE)
        self.assertEqual(parse_timestamp("2026-09-24T12:30:15+0200"), BASE)
        self.assertEqual(parse_timestamp("2026-09-24T05:00:15-05:30"), BASE)
        self.assertEqual(parse_timestamp("2026-09-24T05:00:15-0530"), BASE)
        self.assertEqual(parse_timestamp("2026-09-24T10:30:15+00:00"), BASE)
        self.assertAlmostEqual(parse_timestamp("2026-09-24T11:30:15.5+01:00"), BASE + 0.5, places=6)

    def test_offset_crosses_midnight(self) -> None:
        self.assertEqual(parse_timestamp("2026-09-25T00:30:15+14:00"), BASE)

    def test_timezone_required(self) -> None:
        self.assertIsNone(parse_timestamp("2026-09-24T10:30:15"))
        self.assertIsNone(parse_timestamp("2026-09-24T10:30:15.123"))

    def test_invalid_values(self) -> None:
        for s in (
            "2026-02-30T10:00:00Z",  # no Feb 30
            "2026-13-01T10:00:00Z",
            "2026-09-24T24:00:00Z",
            "2026-09-24T10:60:00Z",
            "2026-09-24T10:00:60Z",  # leap seconds rejected
            "0000-01-01T00:00:00Z",
            "2026-09-24T10:00:00+24:00",
            "2026-09-24T10:00:00+05:60",
        ):
            with self.subTest(s=s):
                self.assertIsNone(parse_timestamp(s))

    def test_malformed(self) -> None:
        for s in (
            "", "garbage", "2026-09-24", "2026-9-24T10:00:00Z", "2026-09-24T10:00Z",
            "2026-09-24T10:00:00.Z", "2026-09-24T10:00:00Z\n", " 2026-09-24T10:00:00Z",
            "2026-09-24T10:00:00+2", "2026-09-24T10:00:00 UTC",
            "٢٠٢٦-09-24T10:00:00Z",  # Arabic-Indic digits are not \d here
        ):
            with self.subTest(s=s):
                self.assertIsNone(parse_timestamp(s))

    def test_non_string_never_raises(self) -> None:
        for v in (None, 123, 1.5, b"2026-09-24T10:30:15Z", [], {}):
            with self.subTest(v=v):
                self.assertIsNone(parse_timestamp(v))  # type: ignore[arg-type]

    def test_leap_day(self) -> None:
        want = float(calendar.timegm((2028, 2, 29, 0, 0, 0, 0, 0, 0)))
        self.assertEqual(parse_timestamp("2028-02-29T00:00:00Z"), want)
        self.assertIsNone(parse_timestamp("2026-02-29T00:00:00Z"))


if __name__ == "__main__":
    unittest.main()
