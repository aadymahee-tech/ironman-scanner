"""IronMan scoring engine — Python port of the Pine Script v16.2 evaluator.

Source of truth for the trading logic is ``tools/indicator_template.pine``;
``docs/LOGIC.md`` describes what each condition means. The ``f_ironman_eval()``
Pine function is reproduced here bar-by-bar so the headless scanner
(``ironman_scanner.py``) reports the same setups the on-chart indicator draws.

Parity notes / deliberate deviations
------------------------------------
* **na semantics.** Pine propagates ``na`` through comparisons, and a ternary or
  ``if`` whose condition is ``na`` behaves as ``false``. Unavailable values are
  therefore ``None`` here and every comparison helper below returns ``False``
  when an operand is missing. This matters during warm-up (``ta.sma(tr, 20)``
  is ``na`` for the first 19 bars, which makes ``er_20`` 0.0 and therefore
  flags the early bars as ``CHOP`` — exactly what Pine does).
* **``ta.ema`` seeding.** Pine seeds ``ta.ema`` with the first source value
  rather than an SMA, so that is what :func:`_ema` does.
* **``ta.stdev`` bias.** Pine's default is ``biased=true``, i.e. a *population*
  standard deviation (divide by N). :func:`_stdev` matches that; using the
  sample stdev would shift every ``z_range`` / ``z_dist`` threshold.
* **Dead upstream variables are not ported.** ``rsi`` (template line 46) and
  ``neckline_res`` / ``neckline_sup`` (lines 138-143) are computed in Pine and
  never read by any rule, the table, or the alert. They are omitted here rather
  than carried as phantom computation. Nothing else differs.
* **``request.security`` context.** In Pine the function runs inside the
  requested symbol's context, so ``close`` there is that symbol's LTP. Here each
  symbol is simply evaluated from its own bars, which is the same thing.

The band gate added in v16.2 (``in_band = close >= min_price and close <=
max_price``) is applied per bar, as in Pine.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence

ACTIONABLE_THRESHOLD = 4

# Pine's ta.* lookbacks used by the engine.
EMA_FAST = 20
EMA_SLOW = 100
RANGE_WINDOW = 20
CHOP_WINDOW = 20
ENTANGLE_WINDOW = 40
BOX_WINDOW = 20
VOLUME_WINDOW = 20


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Bar:
    """One OHLCV candle. ``ts`` is the bar's open time, in UTC."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def day(self) -> date:
        return self.ts.date()


@dataclass(frozen=True)
class BarEval:
    """Everything ``f_ironman_eval()`` returns for a single bar."""

    setup: str
    score: int
    context: str
    in_band: bool
    close: float
    close_ts: datetime
    # Factual gates, surfaced in the scan CSV so a row can be audited without
    # re-running the engine. Booleans, never interpretations.
    chop: bool
    strict_bull: bool
    strict_bear: bool
    box_breakout_long: bool
    box_breakout_short: bool
    sweep_long: bool
    sweep_short: bool
    high_vol: bool

    @property
    def actionable(self) -> bool:
        return self.score >= ACTIONABLE_THRESHOLD


@dataclass(frozen=True)
class SymbolScan:
    """The result of scanning one watchlist symbol over the lookback window."""

    symbol: str
    name: str
    sector: str
    status: str                    # ok | insufficient_data | fetch_error
    bars: int
    latest: Optional[BarEval]      # evaluation of the most recent closed bar
    hit: Optional[BarEval]         # most recent bar scoring >= 4 while in band
    bars_ago: Optional[int]        # bars between `hit` and the latest closed bar
    error: Optional[str] = None

    @property
    def has_signal(self) -> bool:
        return self.hit is not None


# --------------------------------------------------------------------------- #
# Pine na-safe comparison helpers
# --------------------------------------------------------------------------- #
# Pine: `na > x` is na, and an na condition behaves as false. So a comparison
# with a missing operand is False, never an exception.
def _gt(a: Optional[float], b: Optional[float]) -> bool:
    return a is not None and b is not None and a > b


def _lt(a: Optional[float], b: Optional[float]) -> bool:
    return a is not None and b is not None and a < b


def _ge(a: Optional[float], b: Optional[float]) -> bool:
    return a is not None and b is not None and a >= b


def _le(a: Optional[float], b: Optional[float]) -> bool:
    return a is not None and b is not None and a <= b


# --------------------------------------------------------------------------- #
# Indicators (Pine ta.* equivalents)
# --------------------------------------------------------------------------- #
def _ema(values: Sequence[float], length: int) -> List[float]:
    """Pine ``ta.ema``: recursive from the first value, alpha = 2/(length+1)."""
    alpha = 2.0 / (length + 1)
    out: List[float] = []
    prev: Optional[float] = None
    for v in values:
        prev = v if prev is None else alpha * v + (1.0 - alpha) * prev
        out.append(prev)
    return out


def _sma(values: Sequence[float], length: int) -> List[Optional[float]]:
    """Pine ``ta.sma``: na until ``length`` bars are available."""
    out: List[Optional[float]] = []
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= length:
            total -= values[i - length]
        out.append(total / length if i >= length - 1 else None)
    return out


def _sum(values: Sequence[float], length: int) -> List[Optional[float]]:
    """Pine ``math.sum``: na until ``length`` bars are available."""
    out: List[Optional[float]] = []
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= length:
            total -= values[i - length]
        out.append(total if i >= length - 1 else None)
    return out


def _stdev(values: Sequence[float], length: int) -> List[Optional[float]]:
    """Pine ``ta.stdev`` (biased=true -> population standard deviation)."""
    out: List[Optional[float]] = []
    for i in range(len(values)):
        if i < length - 1:
            out.append(None)
            continue
        window = values[i - length + 1 : i + 1]
        mean = sum(window) / length
        var = sum((x - mean) ** 2 for x in window) / length
        out.append(math.sqrt(var))
    return out


def _highest(values: Sequence[Optional[float]], length: int, end: int) -> Optional[float]:
    """``ta.highest(values[end-length+1 .. end])``; None when not enough bars."""
    if end - length + 1 < 0:
        return None
    window = values[end - length + 1 : end + 1]
    if any(v is None for v in window):
        return None
    return max(window)  # type: ignore[arg-type]


def _lowest(values: Sequence[Optional[float]], length: int, end: int) -> Optional[float]:
    """``ta.lowest(values[end-length+1 .. end])``; None when not enough bars."""
    if end - length + 1 < 0:
        return None
    window = values[end - length + 1 : end + 1]
    if any(v is None for v in window):
        return None
    return min(window)  # type: ignore[arg-type]


def _crossing(a: Sequence[Optional[float]], b: Sequence[Optional[float]], i: int) -> bool:
    """Pine ``ta.cross(a, b)`` — true on a cross in either direction."""
    if i < 1 or a[i] is None or b[i] is None or a[i - 1] is None or b[i - 1] is None:
        return False
    cross_up = a[i] > b[i] and a[i - 1] <= b[i - 1]  # type: ignore[operator]
    cross_dn = a[i] < b[i] and a[i - 1] >= b[i - 1]  # type: ignore[operator]
    return bool(cross_up or cross_dn)


# --------------------------------------------------------------------------- #
# Pre-computed per-bar series
# --------------------------------------------------------------------------- #
class _Series:
    """Per-bar values the engine reads, computed once per symbol.

    Everything Pine evaluates with a lookback (``close[1]``, ``ema20[3]``,
    ``ta.cum`` ...) is materialised here as a list indexed by bar.
    """

    def __init__(self, bars: Sequence[Bar]) -> None:
        if not bars:
            raise ValueError("no bars")
        self.bars = list(bars)
        n = len(self.bars)

        self.open = [b.open for b in self.bars]
        self.high = [b.high for b in self.bars]
        self.low = [b.low for b in self.bars]
        self.close = [b.close for b in self.bars]
        self.volume = [b.volume for b in self.bars]

        self.total_range = [b.high - b.low for b in self.bars]
        self.body = [abs(b.close - b.open) for b in self.bars]
        self.wick_up = [b.high - max(b.close, b.open) for b in self.bars]
        self.wick_down = [min(b.close, b.open) - b.low for b in self.bars]
        self.body_ratio = [
            (self.body[i] / self.total_range[i]) if self.total_range[i] > 0 else 0.0
            for i in range(n)
        ]

        # --- range expansion ------------------------------------------------ #
        mean_range = _sma(self.total_range, RANGE_WINDOW)
        sd_range = _stdev(self.total_range, RANGE_WINDOW)
        self.mean_range = mean_range
        # `stdev_range > 0 ? ... : 0.0` -> 0.0 while stdev is still na.
        self.z_range = [
            ((self.total_range[i] - mean_range[i]) / sd_range[i])
            if (_gt(sd_range[i], 0.0) and mean_range[i] is not None)
            else 0.0
            for i in range(n)
        ]
        self.is_tier3_climax = [z >= 3.5 for z in self.z_range]
        self.is_tier2_power = [z >= 1.5 and not c for z, c in zip(self.z_range, self.is_tier3_climax)]

        # --- chop (Kaufman efficiency ratio) -------------------------------- #
        self.sum_ranges_20 = _sum(self.total_range, CHOP_WINDOW)
        self.er_20 = [
            (abs(self.close[i] - self.close[i - CHOP_WINDOW]) / self.sum_ranges_20[i])
            if (i >= CHOP_WINDOW and _gt(self.sum_ranges_20[i], 0.0))
            else 0.0
            for i in range(n)
        ]
        self.is_statistical_chop = [er < 0.25 for er in self.er_20]

        # --- trend structure ------------------------------------------------- #
        self.ema20 = _ema(self.close, EMA_FAST)
        self.ema100 = _ema(self.close, EMA_SLOW)
        sd_price = _stdev(self.close, EMA_SLOW)
        self.stdev_price_100 = sd_price
        self.z_dist = [
            ((self.close[i] - self.ema100[i]) / sd_price[i]) if _gt(sd_price[i], 0.0) else 0.0
            for i in range(n)
        ]
        self.is_extended_uptrend = [z >= 2.0 for z in self.z_dist]
        self.is_extended_downtrend = [z <= -2.0 for z in self.z_dist]

        self.ema20_slope_up = [_gt(self.ema20[i], self.ema20[i - 3]) if i >= 3 else False for i in range(n)]
        self.ema20_slope_dn = [_lt(self.ema20[i], self.ema20[i - 3]) if i >= 3 else False for i in range(n)]

        # ta.cum(cross) - ta.cum(cross[40]) == crosses within the last 40 bars.
        cross = [_crossing(self.ema20, self.ema100, i) for i in range(n)]
        self.ema_cross = cross
        self.crosses_in_40 = [
            sum(1 for j in range(max(0, i - ENTANGLE_WINDOW + 1), i + 1) if cross[j])
            for i in range(n)
        ]
        self.is_entangled = [c > 1 for c in self.crosses_in_40]

        self.strict_bull_trend = [
            self.close[i] > self.ema100[i]
            and self.ema20[i] > self.ema100[i]
            and self.ema20_slope_up[i]
            and not self.is_entangled[i]
            and not self.is_statistical_chop[i]
            for i in range(n)
        ]
        self.strict_bear_trend = [
            self.close[i] < self.ema100[i]
            and self.ema20[i] < self.ema100[i]
            and self.ema20_slope_dn[i]
            and not self.is_entangled[i]
            and not self.is_statistical_chop[i]
            for i in range(n)
        ]

        # --- volume ---------------------------------------------------------- #
        vol_ma = _sma(self.volume, VOLUME_WINDOW)
        self.vol_ma = vol_ma
        self.high_vol = [_gt(self.volume[i], vol_ma[i]) for i in range(n)]

        bar_energy = [self.total_range[i] * self.volume[i] for i in range(n)]
        self.bar_energy = bar_energy
        self.re_ratio = [
            (bar_energy[i] / bar_energy[i - 1]) if (i >= 1 and _gt(bar_energy[i - 1], 0.0)) else 0.0
            for i in range(n)
        ]

        # --- EMA proximity ---------------------------------------------------- #
        self.near_long_ema = [self._near_ema(i, "long") for i in range(n)]
        self.near_short_ema = [self._near_ema(i, "short") for i in range(n)]
        self.ema_rejection_bull = [self.near_long_ema[i] and self.close[i] > self.open[i] for i in range(n)]
        self.ema_rejection_bear = [self.near_short_ema[i] and self.close[i] < self.open[i] for i in range(n)]

        # --- intraday prior-day high/low tracker ------------------------------ #
        # Pine keeps these in `var` and only rolls them over when time("D")
        # changes, i.e. `daily_h/l` hold the PREVIOUS completed session.
        self.daily_h: List[Optional[float]] = [None] * n
        self.daily_l: List[Optional[float]] = [None] * n
        dh: Optional[float] = None
        dl: Optional[float] = None
        cd_h: Optional[float] = None
        cd_l: Optional[float] = None
        for i, bar in enumerate(self.bars):
            new_session = i == 0 or bar.day != self.bars[i - 1].day
            if new_session:
                dh, dl = cd_h, cd_l
                cd_h, cd_l = bar.high, bar.low
            else:
                cd_h = bar.high if cd_h is None else max(cd_h, bar.high)
                cd_l = bar.low if cd_l is None else min(cd_l, bar.low)
            self.daily_h[i] = dh
            self.daily_l[i] = dl

        # --- box -------------------------------------------------------------- #
        # ta.highest(high[1], 20) -> the 20 bars BEFORE the current one.
        self.box_high = [_highest(self.high, BOX_WINDOW, i - 1) if i >= 1 else None for i in range(n)]
        self.box_low = [_lowest(self.low, BOX_WINDOW, i - 1) if i >= 1 else None for i in range(n)]
        self.box_height_pct = [
            ((self.box_high[i] - self.box_low[i]) / self.close[i] * 100)
            if (self.box_high[i] is not None and self.box_low[i] is not None and self.close[i])
            else None
            for i in range(n)
        ]
        self.is_valid_box = [_le(self.box_height_pct[i], 1.50) for i in range(n)]

        # ta.highest(high[1], 2) / ta.lowest(low[1], 2).
        self.highest_high_2 = [_highest(self.high, 2, i - 1) if i >= 1 else None for i in range(n)]
        self.lowest_low_2 = [_lowest(self.low, 2, i - 1) if i >= 1 else None for i in range(n)]

    # -- helpers ------------------------------------------------------------ #
    def _near_ema(self, i: int, side: str) -> bool:
        if side == "long":
            dist20 = abs(self.low[i] - self.ema20[i]) / self.ema20[i] * 100
            dist100 = abs(self.low[i] - self.ema100[i]) / self.ema100[i] * 100
            return (
                (self.low[i] <= self.ema20[i] and self.high[i] >= self.ema20[i])
                or dist20 <= 0.05
                or dist100 <= 0.05
            )
        dist20 = abs(self.high[i] - self.ema20[i]) / self.ema20[i] * 100
        dist100 = abs(self.high[i] - self.ema100[i]) / self.ema100[i] * 100
        return (
            (self.high[i] >= self.ema20[i] and self.low[i] <= self.ema20[i])
            or dist20 <= 0.05
            or dist100 <= 0.05
        )

    def prev(self, attr: str, i: int) -> Optional[float]:
        """Pine ``x[1]`` — the previous bar's value, None if unavailable."""
        return getattr(self, attr)[i - 1] if i >= 1 else None


# --------------------------------------------------------------------------- #
# The evaluator
# --------------------------------------------------------------------------- #
def _eval_bar(s: _Series, i: int, min_price: float, max_price: float) -> BarEval:
    """Port of ``f_ironman_eval()`` for bar ``i`` of ``s``."""
    total_range = s.total_range[i]
    body_ratio = s.body_ratio[i]
    is_tier2_power = s.is_tier2_power[i]
    is_tier3_climax = s.is_tier3_climax[i]

    close_near_high = s.close[i] >= (s.high[i] - total_range * 0.20)
    close_near_low = s.close[i] <= (s.low[i] + total_range * 0.20)

    req_body_ratio = 0.60 if is_tier2_power else 0.80

    is_strong_bull = (
        s.close[i] > s.open[i]
        and (body_ratio >= req_body_ratio or is_tier2_power)
        and close_near_high
        and not is_tier3_climax
    )
    is_strong_bear = (
        s.close[i] < s.open[i]
        and (body_ratio >= req_body_ratio or is_tier2_power)
        and close_near_low
        and not is_tier3_climax
    )

    is_open_eq_low = (
        abs(s.open[i] - s.low[i]) <= (total_range * 0.04)
        and s.close[i] > s.open[i]
        and not is_tier3_climax
    )
    is_open_eq_high = (
        abs(s.open[i] - s.high[i]) <= (total_range * 0.04)
        and s.close[i] < s.open[i]
        and not is_tier3_climax
    )

    is_bear_rejection_drive = (
        s.wick_up[i] >= total_range * 0.25
        and close_near_low
        and s.wick_down[i] <= total_range * 0.10
        and s.close[i] < s.open[i]
        and not is_tier3_climax
    )
    is_bull_rejection_drive = (
        s.wick_down[i] >= total_range * 0.25
        and close_near_high
        and s.wick_up[i] <= total_range * 0.10
        and s.close[i] > s.open[i]
        and not is_tier3_climax
    )

    is_marubozu_bear = (
        is_strong_bear and s.wick_up[i] <= total_range * 0.05 and s.wick_down[i] <= total_range * 0.05
    )
    is_marubozu_bull = (
        is_strong_bull and s.wick_up[i] <= total_range * 0.05 and s.wick_down[i] <= total_range * 0.05
    )
    is_shooting_star = (
        s.wick_up[i] >= s.body[i] * 1.8 and s.wick_down[i] <= s.body[i] * 0.3 and s.close[i] < s.open[i]
    )
    is_hammer = (
        s.wick_down[i] >= s.body[i] * 1.8 and s.wick_up[i] <= s.body[i] * 0.3 and s.close[i] > s.open[i]
    )

    high_vol = s.high_vol[i]

    box_breakout_long = (
        s.is_valid_box[i]
        and _gt(s.close[i], s.box_high[i])
        and s.close[i] > s.open[i]
        and (is_tier2_power or is_strong_bull or is_open_eq_low)
        and high_vol
    )
    box_breakout_short = (
        s.is_valid_box[i]
        and _lt(s.close[i], s.box_low[i])
        and s.close[i] < s.open[i]
        and (is_tier2_power or is_strong_bear or is_open_eq_high)
        and high_vol
    )

    # --- 2A sweep ---------------------------------------------------------- #
    # Pine: (low[1] <= daily_l) — false while daily_l is still na.
    pdl_swept_red = _le(s.prev("low", i), s.daily_l[i]) and _lt(s.prev("close", i), s.prev("open", i))
    has_power_bull = (s.re_ratio[i] >= 1.25 or s.z_range[i] >= 1.0 or is_open_eq_low) and high_vol
    nullify_bull_2a = (
        s.is_extended_downtrend[i]
        and pdl_swept_red
        and has_power_bull
        and _gt(s.close[i], s.prev("high", i))
        and s.close[i] > s.open[i]
    )

    pdh_swept_green = _ge(s.prev("high", i), s.daily_h[i]) and _gt(s.prev("close", i), s.prev("open", i))
    has_power_bear = (s.re_ratio[i] >= 1.25 or s.z_range[i] >= 1.0 or is_open_eq_high) and high_vol
    nullify_bear_2a = (
        s.is_extended_uptrend[i]
        and pdh_swept_green
        and has_power_bear
        and _lt(s.close[i], s.prev("low", i))
        and s.close[i] < s.open[i]
    )

    # --- continuous scores ------------------------------------------------- #
    # Pine tests `near_long_ema or near_long_ema[1]`; the [1] lookup is na on
    # the very first bar, which is falsy, so no special-casing is needed there.
    near_long_prev = s.near_long_ema[i - 1] if i >= 1 else False
    near_short_prev = s.near_short_ema[i - 1] if i >= 1 else False

    score_cont_long = 0
    if box_breakout_long:
        score_cont_long = 5
    elif s.strict_bull_trend[i] and (s.near_long_ema[i] or near_long_prev):
        valid_bar = (
            is_tier2_power
            or is_strong_bull
            or is_open_eq_low
            or is_marubozu_bull
            or is_hammer
            or s.ema_rejection_bull[i]
            or is_bull_rejection_drive
        )
        if valid_bar:
            score_cont_long += 2
            if high_vol:
                score_cont_long += 1
            if _gt(s.close[i], s.highest_high_2[i]):
                score_cont_long += 1
            if body_ratio >= 0.65 or (s.low[i] <= s.ema100[i] and s.high[i] >= s.ema100[i]):
                score_cont_long += 1

    score_cont_short = 0
    if box_breakout_short:
        score_cont_short = 5
    elif s.strict_bear_trend[i] and (s.near_short_ema[i] or near_short_prev):
        valid_bar = (
            is_tier2_power
            or is_strong_bear
            or is_open_eq_high
            or is_marubozu_bear
            or is_shooting_star
            or s.ema_rejection_bear[i]
            or is_bear_rejection_drive
        )
        if valid_bar:
            score_cont_short += 2
            if high_vol:
                score_cont_short += 1
            if _lt(s.close[i], s.lowest_low_2[i]):
                score_cont_short += 1
            if body_ratio >= 0.65 or (s.high[i] >= s.ema100[i] and s.low[i] <= s.ema100[i]):
                score_cont_short += 1

    score_rev_long = 5 if nullify_bull_2a else 0
    score_rev_short = 5 if nullify_bear_2a else 0

    max_score = max(score_cont_short, score_cont_long, score_rev_short, score_rev_long)

    # Pine's branch ORDER matters when two scores tie — do not reorder.
    setup = "None"
    if max_score == score_cont_short and score_cont_short >= 4:
        setup = "BOX BREAKDOWN" if box_breakout_short else "SHORT CONT"
    elif max_score == score_cont_long and score_cont_long >= 4:
        setup = "BOX BREAKOUT" if box_breakout_long else "LONG CONT"
    elif max_score == score_rev_short and score_rev_short >= 4:
        setup = "SHORT 2A SWEEP"
    elif max_score == score_rev_long and score_rev_long >= 4:
        setup = "LONG 2A SWEEP"

    context = (
        "CHOP"
        if (s.is_statistical_chop[i] or s.is_entangled[i])
        else "EXT BULL"
        if s.is_extended_uptrend[i]
        else "EXT BEAR"
        if s.is_extended_downtrend[i]
        else "BULLISH"
        if s.strict_bull_trend[i]
        else "BEARISH"
        if s.strict_bear_trend[i]
        else "SIDEWAYS"
    )

    # ---- v16.2 price-band gate -------------------------------------------- #
    in_band = (s.close[i] >= min_price) and (s.close[i] <= max_price)

    return BarEval(
        setup=setup,
        score=max_score,
        context=context,
        in_band=in_band,
        close=s.close[i],
        close_ts=s.bars[i].ts,
        chop=s.is_statistical_chop[i] or s.is_entangled[i],
        strict_bull=s.strict_bull_trend[i],
        strict_bear=s.strict_bear_trend[i],
        box_breakout_long=box_breakout_long,
        box_breakout_short=box_breakout_short,
        sweep_long=nullify_bull_2a,
        sweep_short=nullify_bear_2a,
        high_vol=high_vol,
    )


def scan_symbol(
    symbol: str,
    name: str,
    sector: str,
    bars: Sequence[Bar],
    *,
    min_price: float,
    max_price: float,
    lookback: int,
) -> SymbolScan:
    """Evaluate ``lookback`` closed bars and report the freshest actionable one.

    A symbol is a *hit* when any bar in the window scores >= 4 (the actionable
    threshold) **while in band** — the same conjunction the Pine alert requires.
    ``bars_ago`` says how far back that bar is: 0 is the most recent closed bar,
    i.e. a trigger that has just confirmed.
    """
    minimum_bars = max(EMA_SLOW, RANGE_WINDOW, BOX_WINDOW + 1, CHOP_WINDOW + 1) + 1
    if len(bars) < minimum_bars:
        return SymbolScan(
            symbol=symbol,
            name=name,
            sector=sector,
            status="insufficient_data",
            bars=len(bars),
            latest=None,
            hit=None,
            bars_ago=None,
            error=f"need >= {minimum_bars} bars, got {len(bars)}",
        )

    s = _Series(bars)
    n = len(bars)
    window = min(max(lookback, 1), n)
    first = n - window

    evals: Dict[int, BarEval] = {
        i: _eval_bar(s, i, min_price, max_price) for i in range(first, n)
    }

    latest = evals[n - 1]

    hit: Optional[BarEval] = None
    bars_ago: Optional[int] = None
    for i in range(n - 1, first - 1, -1):          # freshest first
        ev = evals[i]
        if ev.actionable and ev.in_band:
            hit, bars_ago = ev, n - 1 - i
            break

    return SymbolScan(
        symbol=symbol,
        name=name,
        sector=sector,
        status="ok",
        bars=n,
        latest=latest,
        hit=hit,
        bars_ago=bars_ago,
    )
