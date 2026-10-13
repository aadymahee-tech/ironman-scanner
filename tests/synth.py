"""Deterministic synthetic bar series used by the tests and the fixtures.

Each builder is tuned to land on one specific engine path, so a test failure
points at a rule rather than at vague market data. The series are pure
functions of their arguments: no randomness, no clock, no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Sequence, Tuple

from ironman.engine import Bar

# 09:15 IST == 03:45 UTC, the NSE open.
#
# NB: these series are continuous — there are no overnight gaps, so a calendar
# day is simply every 96th bar. That is deliberate: the engine's prior-day
# tracker keys on the calendar date (Pine's `ta.change(time("D"))`), so a
# synthetic series only has to be internally consistent to exercise it.
SESSION_START = datetime(2026, 1, 5, 3, 45, tzinfo=timezone.utc)
BAR_MINUTES = 15

Row = Tuple[float, float, float, float, float]


def build(rows: Sequence[Row], minutes: int = BAR_MINUTES) -> List[Bar]:
    return [
        Bar(
            ts=SESSION_START + timedelta(minutes=minutes * i),
            open=o,
            high=h,
            low=l,
            close=c,
            volume=v,
        )
        for i, (o, h, l, c, v) in enumerate(rows)
    ]


def _ramp(n: int, start: float, end: float, volume: float = 1000.0) -> List[Row]:
    rows: List[Row] = []
    for i in range(n):
        o = start + (end - start) * i / n
        c = start + (end - start) * (i + 1) / n
        rows.append((o, max(o, c) + 0.5, min(o, c) - 0.5, c, volume))
    return rows


def box_breakout_long(scale: float = 1.0) -> List[Bar]:
    """Uptrend -> tight 20-bar box -> energetic breakout closing above it.

    Scores 5/5 "BOX BREAKOUT" on the final bar. ``scale`` multiplies every
    price, which is how the out-of-band variant is built.
    """
    rows = _ramp(130, 850.0, 1000.0)
    # 20-bar box oscillating inside 996.5-1000.0 (~0.35% wide -> a valid box)
    for k in range(20):
        lo = 996.5 + (0.2 if k % 3 == 0 else 0.0)
        hi = 999.5 - (0.2 if k % 4 == 0 else 0.0)
        o, c = (lo, hi) if k % 2 == 0 else (hi, lo)
        rows.append((o, hi + 0.4, lo - 0.4, c, 1000.0))
    # breakout: opens inside the box, closes above box_high on 3x volume
    rows.append((999.0, 1001.6, 998.9, 1001.4, 3000.0))
    return build([tuple(v * scale for v in row[:4]) + (row[4],) for row in rows])


def box_breakout_then_quiet() -> List[Bar]:
    """The breakout bar, then one quiet bar — a stale (not fresh) trigger."""
    bars = box_breakout_long()
    last = bars[-1]
    bars.append(
        Bar(
            ts=last.ts + timedelta(minutes=BAR_MINUTES),
            open=1001.2,
            high=1001.6,
            low=1000.8,
            close=1001.0,
            volume=1000.0,
        )
    )
    return bars


def quiet_in_band() -> List[Bar]:
    """Dead flat, in band, nothing to report."""
    return build([(1000.0, 1001.0, 999.0, 1000.5, 1000.0) for _ in range(151)])


def quiet_out_of_band_low() -> List[Bar]:
    """Same flat series, priced below a 1,000 floor."""
    return build([(500.0, 501.0, 499.0, 500.5, 1000.0) for _ in range(151)])


def chop_in_band() -> List[Bar]:
    """Two-bar oscillation -> Kaufman ER ~0 -> CHOP regime."""
    rows = []
    for i in range(151):
        o = 1005.0 + (5 if i % 2 == 0 else -5)
        c = 1005.0 + (-5 if i % 2 == 0 else 5)
        rows.append((o, max(o, c) + 1.0, min(o, c) - 1.0, c, 1000.0))
    return build(rows)


def sweep_long_reversal() -> List[Bar]:
    """Four quiet sessions, a sharp plunge, then a reclaim bar.

    The plunge puts price >2 population-sd below EMA100 (``EXT BEAR``), the red
    bar sweeps the prior session's low, and the reclaim closes above the prior
    bar's high on high volume: a 2A sweep worth 5/5.
    """
    rows: List[Row] = []
    for i in range(120):                                   # 4 quiet sessions
        c = 1500.0 + 20.0 * (i / 119.0)
        o = 1500.0 + 20.0 * (max(i - 1, 0) / 119.0)
        rows.append((o, max(o, c) + 2.0, min(o, c) - 2.0, c, 1000.0))

    price = 1520.0
    for i in range(6):                                     # the plunge
        o, c = price, price - 45.0
        rows.append((o, o + 1.0, c - 1.0, c, 1500.0 + 100.0 * i))
        price = c

    o, c = price, price - 25.0                             # red sweep bar
    rows.append((o, o + 1.0, c - 2.0, c, 2000.0))
    price = c

    o, c = price, price + 95.0                             # reclaim bar
    rows.append((o, c + 2.0, o - 2.0, c, 6000.0))
    return build(rows)


def climactic_bar() -> List[Bar]:
    """A single blow-off bar far outside its own 20-bar range distribution."""
    rows = _ramp(150, 1000.0, 1010.0)
    rows.append((1010.0, 1100.0, 1005.0, 1090.0, 9000.0))
    return build(rows)


def too_short() -> List[Bar]:
    return build(_ramp(60, 1000.0, 1100.0))
