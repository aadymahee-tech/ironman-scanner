"""Engine tests: indicator parity, na semantics, and the scoring paths."""

from __future__ import annotations

import math
import unittest
from datetime import timedelta

from ironman.engine import (
    ACTIONABLE_THRESHOLD,
    Bar,
    BarEval,
    _ema,
    _ge,
    _gt,
    _le,
    _lt,
    _sma,
    _stdev,
    _sum,
    _Series,
    scan_symbol,
)

from tests import synth

BAND = {"min_price": 1000.0, "max_price": 10000.0}


def scan(bars, lookback: int = 2, **band):
    kwargs = {**BAND, **band}
    return scan_symbol("NSE:TEST", "Test Co", "IT", bars, lookback=lookback, **kwargs)


class TestPinePrimitives(unittest.TestCase):
    """The ta.* functions have to match Pine's exact conventions."""

    def test_ema_seeds_with_first_source_value(self):
        # alpha = 2/(3+1) = 0.5 -> 10, 15, 22.5 (NOT seeded with an SMA)
        self.assertEqual(_ema([10.0, 20.0, 30.0], 3), [10.0, 15.0, 22.5])

    def test_sma_and_sum_are_na_until_length_bars(self):
        self.assertEqual(_sma([1.0, 2.0, 3.0], 2), [None, 1.5, 2.5])
        self.assertEqual(_sum([1.0, 2.0, 3.0], 2), [None, 3.0, 5.0])
        # a single warm-up None must never be treated as a zero contribution
        self.assertEqual(_sum([1.0, 2.0], 3), [None, None])

    def test_stdev_is_population_not_sample(self):
        # Pine's ta.stdev defaults to biased=true -> divide by N.
        got = _stdev([1.0, 2.0, 3.0, 4.0], 4)[3]
        self.assertAlmostEqual(got, math.sqrt(1.25), places=12)
        self.assertNotAlmostEqual(got, math.sqrt(5 / 3), places=6)   # sample stdev

    def test_comparisons_with_na_are_false(self):
        # Pine: `na > x` is na, and an na condition behaves as false.
        self.assertFalse(_gt(None, 5.0))
        self.assertFalse(_lt(None, 5.0))
        self.assertFalse(_le(1.0, None))
        self.assertFalse(_ge(None, None))


class TestSeries(unittest.TestCase):
    def test_box_window_excludes_the_current_bar(self):
        bars = synth.box_breakout_long()
        s = _Series(bars)
        expected = max(b.high for b in bars[-21:-1])
        self.assertEqual(s.box_high[-1], expected)
        self.assertLess(s.box_height_pct[-1], 1.50)          # a valid box
        self.assertEqual(s.box_high[0], None)                # not enough history

    def test_climax_bar_is_flagged_tier3_and_not_tier2(self):
        s = _Series(synth.climactic_bar())
        i = len(s.bars) - 1
        self.assertGreaterEqual(s.z_range[i], 3.5)
        self.assertTrue(s.is_tier3_climax[i])
        self.assertFalse(s.is_tier2_power[i])

    def test_climax_exclusion_is_not_universal(self):
        """A tier-3 bar can still score through `ema_rejection_bull`.

        The climax guard sits on the candle-pattern flags (is_strong_bull and
        friends), but `ema_rejection_bull` is in the valid-bar list without it,
        so a blow-off bar sitting on the EMA still scores. That is upstream
        v16.1 behaviour and is reproduced deliberately — changing it here would
        make the scanner disagree with the indicator on the chart.
        """
        result = scan(synth.climactic_bar())
        self.assertEqual(result.latest.score, 5)
        self.assertEqual(result.latest.setup, "LONG CONT")

    def test_daily_tracker_holds_the_previous_calendar_day(self):
        bars = synth.box_breakout_long()
        s = _Series(bars)

        # first bar of a new calendar day: daily_* == the previous day's range
        for i in range(1, len(bars)):
            if bars[i].day != bars[i - 1].day:
                prev_day = [b for b in bars if b.day == bars[i - 1].day]
                self.assertEqual(s.daily_h[i], max(b.high for b in prev_day))
                self.assertEqual(s.daily_l[i], min(b.low for b in prev_day))
                break
        else:
            self.fail("series never crosses a calendar day")

        self.assertIsNone(s.daily_h[0])                      # nothing before bar 0

    def test_er_20_flags_chop(self):
        chop = _Series(synth.chop_in_band())
        self.assertTrue(chop.is_statistical_chop[-1])
        self.assertLess(chop.er_20[-1], 0.25)

    def test_tight_boxes_are_chop(self):
        # A 20-bar box <1.5% wide has almost no net displacement, so the
        # efficiency ratio reads it as chop. That is why the box-breakout rule
        # short-circuits the trend requirement instead of relying on it.
        box = _Series(synth.box_breakout_long())
        self.assertTrue(box.is_statistical_chop[-1])
        self.assertEqual(scan(synth.box_breakout_long()).latest.context, "CHOP")

    def test_sustained_move_is_not_chop(self):
        sweep = _Series(synth.sweep_long_reversal())
        self.assertFalse(sweep.is_statistical_chop[-1])
        self.assertGreater(sweep.er_20[-1], 0.25)


class TestScoringPaths(unittest.TestCase):
    def test_box_breakout_scores_five(self):
        result = scan(synth.box_breakout_long())
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.latest.score, 5)
        self.assertEqual(result.latest.setup, "BOX BREAKOUT")
        self.assertTrue(result.latest.box_breakout_long)
        self.assertTrue(result.has_signal)
        self.assertEqual(result.bars_ago, 0)                 # fresh, not stale

    def test_stale_trigger_reports_bars_ago(self):
        result = scan(synth.box_breakout_then_quiet())
        self.assertTrue(result.has_signal)
        self.assertEqual(result.bars_ago, 1)
        self.assertEqual(result.hit.setup, "BOX BREAKOUT")   # the hit, not the last bar
        self.assertEqual(result.latest.setup, "None")        # last bar is quiet

    def test_lookback_window_controls_what_counts(self):
        bars = synth.box_breakout_then_quiet()
        self.assertFalse(scan(bars, lookback=1).has_signal)  # breakout is outside a 1-bar window
        self.assertTrue(scan(bars, lookback=2).has_signal)

    def test_price_band_gate_blocks_the_signal_not_the_score(self):
        # 20x the breakout series -> ~20,000, above the ceiling.
        result = scan(synth.box_breakout_long(scale=20.0))
        self.assertEqual(result.latest.score, 5)             # engine still scores it
        self.assertFalse(result.latest.in_band)
        self.assertFalse(result.has_signal)                  # but it can never fire
        self.assertIsNone(result.hit)

    def test_floor_of_the_band_is_inclusive(self):
        bars = synth.quiet_in_band()
        self.assertTrue(scan(bars, min_price=1000.0).latest.in_band)
        self.assertFalse(scan(bars, min_price=1001.0).latest.in_band)

    def test_two_a_sweep_long(self):
        result = scan(synth.sweep_long_reversal())
        self.assertEqual(result.latest.score, 5)
        self.assertEqual(result.latest.setup, "LONG 2A SWEEP")
        self.assertTrue(result.latest.sweep_long)
        self.assertEqual(result.latest.context, "EXT BEAR")

    def test_chop_regime_is_reported(self):
        result = scan(synth.chop_in_band())
        self.assertEqual(result.latest.context, "CHOP")
        self.assertEqual(result.latest.score, 0)
        self.assertFalse(result.has_signal)

    def test_quiet_series_has_no_signal(self):
        result = scan(synth.quiet_in_band())
        self.assertEqual(result.latest.score, 0)
        self.assertEqual(result.latest.setup, "None")
        self.assertFalse(result.has_signal)
        self.assertIsNone(result.bars_ago)

    def test_insufficient_history_is_reported_not_scored(self):
        result = scan(synth.too_short())
        self.assertEqual(result.status, "insufficient_data")
        self.assertIn("need >= 101 bars", result.error)
        self.assertIsNone(result.latest)
        self.assertFalse(result.has_signal)

    def test_lookback_larger_than_history_is_clamped(self):
        result = scan(synth.box_breakout_long(), lookback=10_000)
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.has_signal)

    def test_empty_bars_is_insufficient_not_a_crash(self):
        result = scan([])
        self.assertEqual(result.status, "insufficient_data")


class TestThreshold(unittest.TestCase):
    def test_actionable_threshold_matches_the_indicator(self):
        self.assertEqual(ACTIONABLE_THRESHOLD, 4)

    def test_three_of_five_is_not_actionable(self):
        base = dict(
            setup="None", score=3, context="SIDEWAYS", in_band=True, close=100.0,
            close_ts=synth.SESSION_START, chop=False, strict_bull=False,
            strict_bear=False, box_breakout_long=False, box_breakout_short=False,
            sweep_long=False, sweep_short=False, high_vol=False,
        )
        self.assertFalse(BarEval(**base).actionable)
        self.assertTrue(BarEval(**{**base, "score": 4}).actionable)


class TestBarValidation(unittest.TestCase):
    def test_bar_day_uses_the_bar_timestamp(self):
        bar = Bar(ts=synth.SESSION_START, open=1, high=2, low=0.5, close=1.5, volume=10)
        self.assertEqual(bar.day, synth.SESSION_START.date())

    def test_single_bar_series_is_safe(self):
        # Every lookback is unavailable, so nothing may raise or be scored.
        s = _Series([Bar(ts=synth.SESSION_START, open=1, high=2, low=0.5, close=1.5, volume=10)])
        self.assertIsNone(s.box_high[0])
        self.assertIsNone(s.sum_ranges_20[0])
        self.assertEqual(s.daily_h[0], None)

    def test_session_timestamps_step_by_interval(self):
        bars = synth.box_breakout_long()
        for a, b in zip(bars, bars[1:]):
            self.assertEqual(b.ts - a.ts, timedelta(minutes=synth.BAR_MINUTES))


if __name__ == "__main__":
    unittest.main()
