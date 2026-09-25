"""ISO 8601 transcript timestamp parsing. See architecture.md D8.

Not datetime.fromisoformat: Python 3.9/3.10 reject "Z" and fractions that are not 3 or 6 digits.
Swift equivalent: ISO8601DateFormatter [.withInternetDateTime, .withFractionalSeconds]
(claudemon.swift 89-93); the fraction is optional here (a deliberate leniency, D8).
"""
from __future__ import annotations

import calendar
import datetime
import re
from typing import Optional

# re.ASCII so \d never matches non-ASCII digits; fullmatch so "$" can't accept a trailing newline.
_ISO = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d+))?(Z|z|[+-]\d{2}:?\d{2})",
    re.ASCII,
)


def parse_timestamp(s: str) -> Optional[float]:
    """Parse "YYYY-MM-DDTHH:MM:SS[.frac][Z|±HH:MM|±HHMM]" (timezone required) to epoch seconds.

    Regex: ^(\\d{4})-(\\d{2})-(\\d{2})[T ](\\d{2}):(\\d{2}):(\\d{2})(?:[.,](\\d+))?(Z|z|[+-]\\d{2}:?\\d{2})$
    Validated via datetime(...) (catching ValueError). Never raises; returns None on any bad input.
    architecture.md D8.
    """
    if not isinstance(s, str):
        return None
    m = _ISO.fullmatch(s)
    if m is None:
        return None
    y, mo, d, h, mi, sec = (int(g) for g in m.groups()[:6])
    frac, tz = m.group(7), m.group(8)
    try:
        datetime.datetime(y, mo, d, h, mi, sec)  # validates ranges (no Feb 30, no hour 24, no leap second)
    except ValueError:
        return None
    offset = 0
    if tz not in ("Z", "z"):
        digits = tz[1:].replace(":", "")
        oh, om = int(digits[:2]), int(digits[2:])
        if oh > 23 or om > 59:
            return None
        offset = (oh * 3600 + om * 60) * (1 if tz[0] == "+" else -1)
    epoch = float(calendar.timegm((y, mo, d, h, mi, sec, 0, 0, 0)) - offset)
    if frac:
        # float("0." + frac) is int(frac) / 10**len(frac), correctly rounded, with no digit-count limit.
        epoch += float("0." + frac)
    return epoch
