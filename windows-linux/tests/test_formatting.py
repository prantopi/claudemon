"""Tests for claudemon_lib.formatting. See components.md P3 row, ui-spec.md §8."""
from __future__ import annotations

import datetime
import unittest

import support  # noqa: F401

from claudemon_lib import formatting as fmt


class TestTokens(unittest.TestCase):
    def test_small_number(self) -> None:
        self.assertEqual(fmt.tokens(0), "0")
        self.assertEqual(fmt.tokens(42), "42")
        self.assertEqual(fmt.tokens(999), "999")

    def test_thousands_one_decimal(self) -> None:
        self.assertEqual(fmt.tokens(1000), "1.0k")
        self.assertEqual(fmt.tokens(1234), "1.2k")
        self.assertEqual(fmt.tokens(9999), "10.0k")

    def test_ten_thousands_no_decimal(self) -> None:
        self.assertEqual(fmt.tokens(10000), "10k")
        self.assertEqual(fmt.tokens(123456), "123k")
        self.assertEqual(fmt.tokens(999999), "1000k")

    def test_millions(self) -> None:
        self.assertEqual(fmt.tokens(1_000_000), "1.0M")
        self.assertEqual(fmt.tokens(2_500_000), "2.5M")


class TestExact(unittest.TestCase):
    def test_grouping(self) -> None:
        self.assertEqual(fmt.exact(0), "0")
        self.assertEqual(fmt.exact(999), "999")
        self.assertEqual(fmt.exact(1000), "1,000")
        self.assertEqual(fmt.exact(1234567), "1,234,567")


class TestClip(unittest.TestCase):
    def test_short_string_is_padded(self) -> None:
        self.assertEqual(fmt.clip("abc", 10), "abc".ljust(10))
        self.assertEqual(len(fmt.clip("abc", 10)), 10)

    def test_exact_length_no_ellipsis(self) -> None:
        self.assertEqual(fmt.clip("1234567890", 10), "1234567890")

    def test_long_string_gets_ellipsis(self) -> None:
        self.assertEqual(fmt.clip("hello world", 5), "hell…")
        self.assertEqual(len(fmt.clip("hello world", 5)), 5)

    def test_empty_string(self) -> None:
        self.assertEqual(fmt.clip("", 4), "    ")


class TestDuration(unittest.TestCase):
    def test_seconds(self) -> None:
        self.assertEqual(fmt.duration(0), "0s")
        self.assertEqual(fmt.duration(45), "45s")

    def test_minutes(self) -> None:
        self.assertEqual(fmt.duration(60), "1m0s")
        self.assertEqual(fmt.duration(125), "2m5s")

    def test_hours(self) -> None:
        self.assertEqual(fmt.duration(3600), "1h0m")
        self.assertEqual(fmt.duration(3725), "1h2m")

    def test_negative_clamped_to_zero(self) -> None:
        self.assertEqual(fmt.duration(-10), "0s")


class TestTimeFormats(unittest.TestCase):
    def test_hm(self) -> None:
        t = datetime.datetime(2026, 9, 24, 8, 5, 30).timestamp()
        self.assertEqual(fmt.hm(t), "08:05")

    def test_clock(self) -> None:
        t = datetime.datetime(2026, 9, 24, 23, 59, 9).timestamp()
        self.assertEqual(fmt.clock(t), "23:59:09")

    def test_day_name(self) -> None:
        d = datetime.date(2026, 9, 24)  # a Thursday
        self.assertEqual(fmt.day_name(d), d.strftime("%a"))

    def test_day_long(self) -> None:
        d = datetime.date(2026, 9, 24)
        self.assertEqual(fmt.day_long(d), f"{d.strftime('%a')} 24 {d.strftime('%b')}")

    def test_midnight_edge(self) -> None:
        t = datetime.datetime(2026, 9, 24, 0, 0, 0).timestamp()
        self.assertEqual(fmt.hm(t), "00:00")
        self.assertEqual(fmt.clock(t), "00:00:00")


class TestRoundHalfUp(unittest.TestCase):
    def test_rounds_half_up_not_banker(self) -> None:
        self.assertEqual(fmt.round_half_up(2.5), 3)
        self.assertEqual(fmt.round_half_up(1.5), 2)
        self.assertEqual(fmt.round_half_up(0.5), 1)

    def test_rounds_down_below_half(self) -> None:
        self.assertEqual(fmt.round_half_up(2.4), 2)

    def test_negative(self) -> None:
        self.assertEqual(fmt.round_half_up(-0.5), 0)


if __name__ == "__main__":
    unittest.main()
