"""Period statistics, drawdown, streaks and the row selector."""

from __future__ import annotations

import unittest
from datetime import date

from tradetron_pnl import analytics
from tradetron_pnl.config import DeploymentTag
from tradetron_pnl.store import LedgerRow


def row(day: str, dep_id: int, pnl: float, *, currency="INR", paper=False, name=None) -> LedgerRow:
    return LedgerRow(trading_day=date.fromisoformat(day), deployment_id=dep_id, name=name or f"Dep {dep_id}",
                     deployment_type="LIVE AUTO", broker="Z", currency=currency, paper=paper, day_pnl=pnl,
                     close_pnl=None, baseline_pnl=None, baseline_source="t", snapshots=1, first_ts_ist=None,
                     last_ts_ist=None, status_eod="Active")


ROWS = [
    row("2026-10-26", 1, 3100), row("2026-10-26", 2, -700),
    row("2026-10-27", 1, -1000), row("2026-10-27", 2, 500),
    row("2026-10-28", 1, 0),
    row("2026-10-29", 1, 3449.5), row("2026-10-29", 2, 400),
    row("2026-10-30", 1, -2500),
    row("2026-11-02", 1, 800),
]


class TestDailyAndPeriod(unittest.TestCase):
    def test_daily_totals_sum_per_day(self):
        daily = analytics.daily_totals(ROWS)
        self.assertEqual(daily[0], (date(2026, 10, 26), 2400.0))
        self.assertEqual(daily[1], (date(2026, 10, 27), -500.0))
        self.assertEqual(len(daily), 6)

    def test_period_stats_numbers(self):
        s = analytics.period_stats(analytics.daily_totals(ROWS), date(2026, 10, 26), date(2026, 10, 30))
        self.assertEqual(s.trading_days, 5)
        self.assertEqual(s.net, 3249.5)
        self.assertEqual((s.win_days, s.loss_days, s.flat_days), (2, 2, 1))
        self.assertEqual(s.gross_profit, 6249.5)
        self.assertEqual(s.gross_loss, -3000.0)
        self.assertAlmostEqual(s.win_rate, 0.5)
        self.assertEqual(s.best_day, (date(2026, 10, 29), 3849.5))
        self.assertEqual(s.worst_day, (date(2026, 10, 30), -2500.0))
        self.assertEqual(s.profit_factor, 2.08)
        self.assertEqual(s.avg_win, 3124.75)
        self.assertEqual(s.avg_loss, -1500.0)
        self.assertEqual(s.avg_day, 649.9)
        self.assertEqual(s.turnover_estimate, 9249.5)
        self.assertEqual(s.current_streak, -1)

    def test_drawdown_is_peak_to_trough(self):
        curve = analytics.equity_curve(analytics.daily_totals(ROWS))
        self.assertEqual(curve[-1], (date(2026, 11, 2), 4049.5))
        dd, start, end = analytics.max_drawdown(curve)
        self.assertEqual(dd, -2500.0)
        self.assertEqual((start, end), (date(2026, 10, 29), date(2026, 10, 30)))
        self.assertEqual(analytics.max_drawdown([]), (0.0, None, None))
        # a curve that never rises above zero draws down from the zero start
        self.assertEqual(analytics.max_drawdown([(date(2026, 1, 1), -10.0), (date(2026, 1, 2), -30.0)])[0], -30.0)

    def test_empty_period(self):
        s = analytics.period_stats([], date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(s.trading_days, 0)
        self.assertIsNone(s.win_rate)
        self.assertIsNone(s.profit_factor)
        self.assertIsNone(s.avg_day)
        self.assertIsNone(s.best_day)

    def test_streaks_skip_flat_days(self):
        self.assertEqual(analytics._streak([(date(2026, 1, 1), 1), (date(2026, 1, 2), 2), (date(2026, 1, 3), 0)]), 2)
        self.assertEqual(analytics._streak([(date(2026, 1, 1), 1), (date(2026, 1, 2), -2), (date(2026, 1, 3), -1)]), -2)
        self.assertEqual(analytics._streak([(date(2026, 1, 1), 0)]), 0)

    def test_profit_factor_without_losses_is_none(self):
        s = analytics.period_stats([(date(2026, 1, 1), 5.0)])
        self.assertIsNone(s.profit_factor)
        self.assertEqual(s.current_streak, 1)


class TestGroupings(unittest.TestCase):
    def test_by_deployment_sorted_by_net(self):
        stats = analytics.by_deployment(ROWS, {1: DeploymentTag(1, alias="Straddle", segment="fno")})
        self.assertEqual([d.deployment_id for d in stats], [1, 2])
        top = stats[0]
        self.assertEqual(top.label, "Straddle")
        self.assertEqual(top.net, 3849.5)
        self.assertEqual((top.days, top.win_days, top.loss_days), (6, 3, 2))
        self.assertEqual((top.best, top.worst), (3449.5, -2500.0))
        self.assertEqual(top.last_day, date(2026, 11, 2))
        self.assertEqual(stats[1].label, "Dep 2")
        self.assertAlmostEqual(stats[1].win_rate, 2 / 3)

    def test_by_month(self):
        months = analytics.by_month(analytics.daily_totals(ROWS))
        self.assertEqual([(m.month, m.days, m.net) for m in months], [("2026-10", 5, 3249.5), ("2026-11", 1, 800.0)])
        self.assertAlmostEqual(months[0].win_rate, 0.5)


class TestSelectRows(unittest.TestCase):
    def test_filters_currency_paper_include_and_dates(self):
        rows = ROWS + [row("2026-10-26", 3, 999, currency="USD"), row("2026-10-26", 4, 555, paper=True),
                       row("2026-10-26", 5, 111)]
        tags = {5: DeploymentTag(5, include=False)}
        selected = analytics.select_rows(rows, tags)
        ids = {r.deployment_id for r in selected}
        self.assertEqual(ids, {1, 2})
        self.assertEqual({r.deployment_id for r in analytics.select_rows(rows, tags, include_paper=True)}, {1, 2, 4})
        self.assertEqual({r.deployment_id for r in analytics.select_rows(rows, tags, currency="USD")}, {3})
        windowed = analytics.select_rows(rows, tags, start=date(2026, 10, 29), end=date(2026, 10, 30))
        self.assertEqual(sorted(r.trading_day.day for r in windowed), [29, 29, 30])


if __name__ == "__main__":
    unittest.main()
