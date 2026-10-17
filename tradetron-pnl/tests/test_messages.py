"""Money formatting and the text of every Telegram update."""

from __future__ import annotations

import unittest
from datetime import date, datetime

from tradetron_pnl import messages as m
from tradetron_pnl.analytics import DeploymentStats, PeriodStats, period_stats
from tradetron_pnl.api import Deployment
from tradetron_pnl.config import DeploymentTag
from tradetron_pnl.trading_calendar import IST, AdvanceTaxDue


def dep(dep_id, name, pnl, status="Active", paper=False, currency="INR", open_positions=0) -> Deployment:
    return Deployment(deployment_id=dep_id, name=name, deployment_type="LIVE OFFLINE" if paper else "LIVE AUTO",
                      status=status, broker="Zerodha", pnl=pnl, currency=currency, open_positions=open_positions)


def dview(d: Deployment, day_pnl, *, counted=True, alias="", hour_delta=None, baseline="preopen") -> m.DeploymentView:
    return m.DeploymentView(deployment=d, tag=DeploymentTag(d.deployment_id, alias=alias), day_pnl=day_pnl,
                            baseline_source=baseline, counted=counted, hour_delta=hour_delta)


def make_view(**overrides) -> m.RunView:
    now = datetime(2026, 10, 30, 15, 40, tzinfo=IST)
    daily = [(date(2026, 10, 26), 2400.0), (date(2026, 10, 27), -500.0), (date(2026, 10, 28), 0.0),
             (date(2026, 10, 29), 3849.5), (date(2026, 10, 30), -2500.0)]
    views = [
        dview(dep(1001, "Nifty <W> & Straddle", -2500, "Live-Entered", open_positions=4), -2500.0, hour_delta=-2500.0),
        dview(dep(1002, "BankNifty Iron Condor", -1000), 0.0, alias="BN Condor"),
        dview(dep(1003, "Crude Momentum", 0, "Error-Execution"), 0.0),
        dview(dep(1004, "Paper Test", 1300, "Live-Entered", paper=True), 10.0, counted=False),
    ]
    base = dict(
        now=now, day=now.date(), phase="post_close", kind="eod", fy="FY2026-27", currency="INR", source="api",
        deployments=views, today_total=-2500.0, hour_delta=-2500.0, paper_total=10.0, open_positions=4,
        week=period_stats(daily, date(2026, 10, 26), date(2026, 10, 30)),
        mtd=period_stats(daily, date(2026, 10, 1), date(2026, 10, 31)),
        quarter=period_stats(daily, date(2026, 10, 1), date(2026, 12, 31)),
        fytd=period_stats(daily, date(2026, 4, 1), date(2027, 3, 31)),
        month_deployments=[DeploymentStats(1001, "Nifty <W> & Straddle", 5, 3049.5, 2, 2, 3449.5, -2500.0, "Live-Entered", now.date())],
        next_due=AdvanceTaxDue(date(2026, 12, 15), 75, "3rd"),
    )
    base.update(overrides)
    return m.RunView(**base)


class TestFormatting(unittest.TestCase):
    def test_indian_grouping(self):
        self.assertEqual(m.group_indian(0), "0")
        self.assertEqual(m.group_indian(999), "999")
        self.assertEqual(m.group_indian(1000), "1,000")
        self.assertEqual(m.group_indian(123456), "1,23,456")
        self.assertEqual(m.group_indian(12345678), "1,23,45,678")

    def test_fmt_money(self):
        self.assertEqual(m.fmt_money(4250.5), "₹4,251")
        self.assertEqual(m.fmt_money(4250.5, sign=True), "+₹4,251")
        self.assertEqual(m.fmt_money(-123456.78), "-₹1,23,457")
        self.assertEqual(m.fmt_money(-123456.78, decimals=2), "-₹1,23,456.78")
        self.assertEqual(m.fmt_money(0, sign=True), "₹0")
        self.assertEqual(m.fmt_money(None), "—")
        self.assertEqual(m.fmt_money(1234.5, "USD", sign=True, decimals=2), "+$1,234.50")
        self.assertEqual(m.fmt_money(5, "AED"), "5 AED")

    def test_misc_helpers(self):
        self.assertEqual(m.arrow(1), "▲")
        self.assertEqual(m.arrow(-1), "▼")
        self.assertEqual(m.arrow(0), "•")
        self.assertEqual(m.pct(0.5), "50%")
        self.assertEqual(m.pct(None), "—")
        self.assertEqual(m.short("a" * 30, 10), "a" * 9 + "…")


class TestHourly(unittest.TestCase):
    def test_hourly_contents_and_escaping(self):
        view = make_view(kind="hourly", phase="open", now=datetime(2026, 10, 30, 12, 0, tzinfo=IST))
        text = m.render_hourly(view)
        self.assertIn("📊 <b>Tradetron</b> · 12:00 IST", text)
        self.assertIn("Today <b>-₹2,500</b> ▼", text)
        self.assertIn("(-₹2,500 since last update)", text)
        self.assertIn("<b>1 in error ⚠️</b>", text)
        self.assertIn("4 open positions", text)
        self.assertIn("Nifty &lt;W&gt; &amp; Straddle", text)      # escaped for HTML mode
        self.assertNotIn("<W>", text)
        self.assertIn("BN Condor", text)                                # alias wins
        self.assertIn("⚠️ Error-Execution", text)
        self.assertIn("paper (1): +₹10 — not counted", text)
        self.assertNotIn("Paper Test  <b>", text)                        # paper not in the counted list
        self.assertIn("MTD <b>+₹3,250</b> (5d, 50% win)", text)
        self.assertNotIn("Advance tax", text)                            # 46 days away: outside the hourly window

    def test_partial_baseline_marker(self):
        view = make_view(kind="hourly")
        view.deployments[1].baseline_source = "partial"
        text = m.render_hourly(view)
        self.assertIn("BN Condor  <b>₹0</b>  <i>Active</i> ˣ", text)
        self.assertIn("day figure is partial", text)


class TestEodAndDigests(unittest.TestCase):
    def test_eod(self):
        text = m.render_eod(make_view())
        self.assertIn("🧾 <b>EOD · Fri 30 Oct 2026</b>", text)
        self.assertIn("Day <b>-₹2,500</b> ▼", text)
        self.assertIn("Streak: -1 day red", text)
        self.assertIn("1 deployment(s) ended in error", text)
        self.assertIn("Week <b>+₹3,250</b> (5d, 50% win)", text)
        self.assertIn("FY max drawdown -₹2,500 · profit factor 2.08", text)
        self.assertNotIn("<i>Active</i>", text)                          # no status column at EOD

    def test_eod_hides_streak_on_flat_day(self):
        text = m.render_eod(make_view(today_total=0.0))
        self.assertNotIn("Streak", text)

    def test_advance_tax_reminder_inside_window(self):
        view = make_view(now=datetime(2026, 12, 10, 15, 40, tzinfo=IST), day=date(2026, 12, 10))
        text = m.render_eod(view)
        self.assertIn("Advance tax 3rd instalment (75% cumulative) due 15 Dec — in 5 days", text)
        view = make_view(now=datetime(2026, 12, 15, 15, 40, tzinfo=IST), day=date(2026, 12, 15))
        self.assertIn("due 15 Dec — today", m.render_eod(view))

    def test_weekly(self):
        text = m.render_weekly(make_view())
        self.assertIn("📅 <b>Week W44 2026</b>", text)
        self.assertIn("Net <b>+₹3,250</b> · 2W / 2L / 1 flat", text)
        self.assertIn("Best Thu +₹3,850 · worst Fri -₹2,500", text)
        self.assertIn("Mon 26  +₹2,400", text)
        self.assertIn("Fri 30  -₹2,500", text)

    def test_monthly(self):
        text = m.render_monthly(make_view())
        self.assertIn("🗓 <b>October 2026</b>", text)
        self.assertIn("Net <b>+₹3,250</b> · 5 days · 50% win · PF 2.08", text)
        self.assertIn("Avg day +₹650 · max DD -₹2,500", text)
        self.assertIn("Nifty &lt;W&gt; &amp; Straddle  <b>+₹3,050</b>  (5d, 50%)", text)
        self.assertIn("Q3 FY2026-27 <b>+₹3,250</b>", text)
        self.assertIn("FYTD (FY2026-27)", text)

    def test_quarter(self):
        text = m.render_quarter(make_view(day=date(2026, 12, 31)))
        self.assertIn("📘 <b>Q3 FY2026-27 closed</b>", text)
        self.assertIn("est. turnover ₹9,250", text)
        self.assertIn("3rd (75%) due 15 Dec 2026", text)

    def test_empty_periods(self):
        view = make_view(week=PeriodStats(), mtd=PeriodStats())
        self.assertIn("No trading days recorded this week", m.render_weekly(view))
        self.assertIn("No trading days recorded this month", m.render_monthly(view))

    def test_preopen_error_alert_and_fetch_failure(self):
        view = make_view(kind="preopen", phase="pre_open")
        view.new_errors = [view.deployments[2]]
        self.assertIn("🌅 <b>Pre-open</b>", m.render_preopen(view))
        self.assertIn("3 active deployments · <b>1 in error ⚠️</b>", m.render_preopen(view))
        alert = m.render_error_alert(view)
        self.assertIn("🚨 <b>Deployment error</b>", alert)
        self.assertIn("Crude Momentum — <b>Error-Execution</b> (Zerodha)", alert)
        failure = m.render_fetch_failure("HTTP 401 <denied>", view.now)
        self.assertIn("<code>HTTP 401 &lt;denied&gt;</code>", failure)

    def test_other_currency_line(self):
        view = make_view(other_currency_totals={"USD": 12.5})
        self.assertIn("USD: +$13 — reported separately", m.render_hourly(view))


if __name__ == "__main__":
    unittest.main()
