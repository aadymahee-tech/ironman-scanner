"""Financial-year tax workings."""

from __future__ import annotations

import csv
import io
import unittest
from datetime import date

from tradetron_pnl import tax
from tradetron_pnl.config import DeploymentTag
from tradetron_pnl.store import LedgerRow


def row(day: str, dep_id: int, pnl: float, *, currency="INR", paper=False, name=None) -> LedgerRow:
    return LedgerRow(trading_day=date.fromisoformat(day), deployment_id=dep_id, name=name or f"Dep {dep_id}",
                     deployment_type="LIVE OFFLINE" if paper else "LIVE AUTO", broker="Z", currency=currency,
                     paper=paper, day_pnl=pnl, close_pnl=None, baseline_pnl=None, baseline_source="t",
                     snapshots=1, first_ts_ist=None, last_ts_ist=None, status_eod="Active")


LEDGER = [
    row("2026-03-31", 1, 999),                     # previous FY, must be ignored
    row("2026-05-05", 1, 1000), row("2026-05-05", 2, -200),   # Q1
    row("2026-06-20", 1, -400),                    # Q1 (after the 15 Jun instalment)
    row("2026-08-10", 1, 2500), row("2026-08-10", 3, 300),    # Q2 (3 = intraday equity)
    row("2026-10-16", 1, -1500), row("2026-10-16", 4, 50, paper=True), row("2026-10-16", 5, 80, currency="USD"),
    row("2026-10-16", 6, 700),                     # 6 excluded by config
    row("2027-02-02", 2, 600),                     # Q4
    row("2027-04-01", 1, 123),                     # next FY, must be ignored
]
TAGS = {
    1: DeploymentTag(1, alias="Straddle", segment="fno"),
    3: DeploymentTag(3, segment="intraday_equity"),
    6: DeploymentTag(6, include=False),
}


class TestBuildTaxReport(unittest.TestCase):
    def setUp(self):
        self.report = tax.build_tax_report(LEDGER, "FY2026-27", TAGS, as_of=date(2026, 10, 16))

    def test_fy_window_and_totals(self):
        o = self.report.overall
        # counted: 1000-200-400 + 2500+300 - 1500 + 600 = 2300
        self.assertEqual(o.net, 2300.0)
        self.assertEqual(o.trading_days, 5)
        self.assertEqual(o.gross_profit, 800 + 2800 + 600)
        self.assertEqual(o.gross_loss, -400 - 1500)

    def test_quarters_and_cumulative(self):
        q = {x.quarter: x for x in self.report.quarters}
        self.assertEqual(q[1].stats.net, 400.0)
        self.assertEqual(q[2].stats.net, 2800.0)
        self.assertEqual(q[3].stats.net, -1500.0)
        self.assertEqual(q[4].stats.net, 600.0)
        self.assertEqual([x.cumulative for x in self.report.quarters], [400.0, 3200.0, 1700.0, 2300.0])
        self.assertEqual(q[4].start, date(2027, 1, 1))

    def test_heads_split_by_segment(self):
        heads = {h.segment: h for h in self.report.heads}
        self.assertEqual(set(heads), {"fno", "intraday_equity"})
        fno = heads["fno"]
        self.assertEqual(fno.head, "Non-speculative business income")
        self.assertEqual(fno.net, 1000 - 200 - 400 + 2500 - 1500 + 600)
        self.assertEqual(fno.turnover_estimate, 1000 + 200 + 400 + 2500 + 1500 + 600)
        self.assertEqual(fno.deployments, ["Dep 2", "Straddle"])
        spec = heads["intraday_equity"]
        self.assertEqual(spec.head, "Speculative business income")
        self.assertEqual(spec.net, 300.0)

    def test_exclusions(self):
        self.assertEqual(self.report.paper_excluded, 1)
        self.assertEqual(self.report.excluded_ids, [6])
        self.assertEqual(self.report.other_currencies, {"USD": 80.0})

    def test_advance_tax_lines(self):
        lines = {a.due.ordinal: a for a in self.report.advance_tax}
        self.assertEqual(lines["1st"].fytd_net, 800.0)          # before 20 Jun loss
        self.assertEqual(lines["2nd"].fytd_net, 3200.0)
        self.assertEqual(lines["3rd"].fytd_net, 1700.0)         # capped at as_of (16 Oct)
        self.assertEqual(lines["3rd"].status, "upcoming")
        self.assertEqual(lines["1st"].status, "past")
        self.assertIsNone(lines["3rd"].indicative_cumulative_tax)

    def test_indicative_rate(self):
        report = tax.build_tax_report(LEDGER, "FY2026-27", TAGS, as_of=date(2026, 10, 16), rate=0.30)
        second = next(a for a in report.advance_tax if a.due.ordinal == "2nd")
        self.assertEqual(second.indicative_cumulative_tax, round(3200 * 0.30 * 0.45, 2))
        self.assertTrue(report.notes[0].startswith("Indicative tax"))

    def test_empty_year(self):
        report = tax.build_tax_report(LEDGER, "FY2030-31", TAGS)
        self.assertEqual(report.overall.trading_days, 0)
        self.assertEqual(report.heads, [])
        md = tax.render_tax_markdown(report)
        self.assertIn("No ledger rows", md)


class TestRenderers(unittest.TestCase):
    def test_markdown_sections(self):
        report = tax.build_tax_report(LEDGER, "FY2026-27", TAGS, as_of=date(2026, 10, 16), rate=0.3)
        md = tax.render_tax_markdown(report)
        for heading in ("# Tax workings — FY2026-27", "## Quarters", "## Heads of income", "## Months",
                        "## Deployments", "## Advance tax calendar", "## Exclusions", "## Notes"):
            self.assertIn(heading, md)
        self.assertIn("| Q3 | 01 Oct – 31 Dec 2026 |", md)
        self.assertIn("Speculative business income", md)
        self.assertIn("Indicative cumulative tax", md)
        self.assertIn("USD P&L not included", md)
        self.assertIn("+₹2,300", md)

    def test_csv_rows_mark_included_and_running_total(self):
        text = tax.tax_csv(LEDGER, "FY2026-27", TAGS)
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(len(rows), 10)                      # FY rows only
        self.assertEqual(rows[0]["trading_day"], "2026-05-05")
        self.assertEqual(rows[0]["deployment"], "Straddle")
        self.assertEqual(rows[0]["head"], "Non-speculative business income")
        by_dep = {(r["trading_day"], r["deployment_id"]): r for r in rows}
        self.assertEqual(by_dep[("2026-10-16", "4")]["included"], "0")     # paper
        self.assertEqual(by_dep[("2026-10-16", "6")]["included"], "0")     # include=0
        self.assertEqual(by_dep[("2026-10-16", "5")]["included"], "0")     # USD
        self.assertEqual(by_dep[("2026-10-16", "5")]["cumulative_fy"], "")
        self.assertEqual(by_dep[("2027-02-02", "2")]["cumulative_fy"], "2300.00")
        self.assertEqual(by_dep[("2027-02-02", "2")]["quarter"], "Q4")


if __name__ == "__main__":
    unittest.main()
