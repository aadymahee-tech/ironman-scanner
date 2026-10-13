"""Report tests: markdown summary, CSV rows, and append behaviour."""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ironman.engine import SymbolScan, scan_symbol
from ironman.report import (
    CSV_COLUMNS,
    ScanReport,
    append_csv,
    console_summary,
    csv_rows,
    render_markdown,
)

from tests import synth

BAND = {"min_price": 1000.0, "max_price": 10000.0}
START = datetime(2026, 10, 13, 3, 45, tzinfo=timezone.utc)


def _scan(symbol, name, sector, bars, lookback=2):
    return scan_symbol(symbol, name, sector, bars, lookback=lookback, **BAND)


def make_report(*, include_signals: bool = True) -> ScanReport:
    results = []
    if include_signals:
        results.append(_scan("NSE:TATAELXSI", "Tata Elxsi", "IT", synth.box_breakout_long()))
        results.append(
            _scan("NSE:HAL", "Hindustan Aeronautics", "Diversified & Infra", synth.sweep_long_reversal())
        )
    results.append(_scan("NSE:INFY", "Infosys", "IT", synth.chop_in_band()))
    results.append(_scan("NSE:HDFCBANK", "HDFC Bank", "Banking & Finance", synth.quiet_out_of_band_low()))
    return ScanReport(
        results=results,
        interval="15m",
        lookback=2,
        min_price=1000.0,
        max_price=10000.0,
        started_at=START,
        finished_at=START + timedelta(seconds=4),
        source="fixture",
    )


class TestMarkdown(unittest.TestCase):
    def test_header_carries_the_scan_configuration(self):
        text = render_markdown(make_report())
        self.assertIn("# IronMan F&O scan", text)
        self.assertIn("lookback **2**", text)
        self.assertIn("₹1,000 – ₹10,000", text)
        self.assertIn("source `fixture`", text)

    def test_signals_are_listed_with_setup_and_rating(self):
        text = render_markdown(make_report())
        self.assertIn("## Signals", text)
        self.assertIn("BOX BREAKOUT", text)
        self.assertIn("LONG 2A SWEEP", text)
        self.assertIn("5/5", text)
        self.assertIn("latest closed bar", text)
        self.assertIn("**2 signal(s)**", text)

    def test_out_of_band_symbols_are_separated_not_hidden(self):
        text = render_markdown(make_report())
        self.assertIn("**Out of band (1):** NSE:HDFCBANK (500.50)", text)
        self.assertIn("| **out** |", text)
        self.assertIn("OUT OF RANGE", text)

    def test_no_signal_message(self):
        text = render_markdown(make_report(include_signals=False))
        self.assertIn("No watchlist symbol scored ≥4/5", text)
        self.assertIn("**0 signal(s)**", text)

    def test_stale_trigger_reports_age(self):
        report = make_report()
        report.results = list(report.results) + [
            _scan("NSE:STALE", "Stale Co", "IT", synth.box_breakout_then_quiet())
        ]
        self.assertIn("1 bar(s) ago", render_markdown(report))

    def test_failed_symbols_are_reported(self):
        report = make_report()
        report.results = list(report.results) + [
            SymbolScan(
                symbol="NSE:BROKEN", name="Broken", sector="IT", status="fetch_error",
                bars=0, latest=None, hit=None, bars_ago=None, error="HTTP 404",
            )
        ]
        report.failures = [("NSE:BROKEN", "HTTP 404")]
        text = render_markdown(report)
        self.assertIn("## Skipped", text)
        self.assertIn("HTTP 404", text)
        self.assertIn("1 fetch failure(s)", text)

    def test_insufficient_history_is_reported(self):
        report = make_report()
        report.results = list(report.results) + [
            _scan("NSE:NEW", "New Listing", "IT", synth.too_short())
        ]
        self.assertIn("insufficient_data", render_markdown(report))

    def test_elapsed_time_is_shown(self):
        self.assertIn("Scan took 4.0s", render_markdown(make_report()))


class TestCsv(unittest.TestCase):
    def test_row_shape_matches_the_declared_columns(self):
        rows = csv_rows(make_report())
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertEqual(list(row.keys()), CSV_COLUMNS)

    def test_signal_columns_are_populated_only_for_hits(self):
        rows = {r["symbol"]: r for r in csv_rows(make_report())}
        hit = rows["NSE:TATAELXSI"]
        self.assertEqual(hit["signal_score"], 5)
        self.assertEqual(hit["signal_setup"], "BOX BREAKOUT")
        self.assertEqual(hit["signal_bars_ago"], 0)
        self.assertTrue(hit["signal_bar_utc"].endswith("Z"))

        quiet = rows["NSE:INFY"]
        self.assertEqual(quiet["signal_score"], "")
        self.assertEqual(quiet["signal_setup"], "")
        self.assertEqual(quiet["status"], "ok")

    def test_band_flag_is_written_as_0_or_1(self):
        rows = {r["symbol"]: r for r in csv_rows(make_report())}
        self.assertEqual(rows["NSE:HDFCBANK"]["in_band"], "0")
        self.assertEqual(rows["NSE:INFY"]["in_band"], "1")

    def test_gate_flags_are_recorded(self):
        rows = {r["symbol"]: r for r in csv_rows(make_report())}
        self.assertEqual(rows["NSE:TATAELXSI"]["box_breakout_long"], 1)
        self.assertEqual(rows["NSE:HAL"]["sweep_long"], 1)
        self.assertEqual(rows["NSE:INFY"]["chop"], 1)

    def test_fetch_error_row_keeps_the_error_text(self):
        report = make_report()
        report.results = [
            SymbolScan(
                symbol="NSE:BROKEN", name="Broken", sector="IT", status="fetch_error",
                bars=0, latest=None, hit=None, bars_ago=None, error="HTTP 404",
            )
        ]
        row = csv_rows(report)[0]
        self.assertEqual(row["status"], "fetch_error")
        self.assertEqual(row["error"], "HTTP 404")
        self.assertEqual(row["ltp"], "")

    def test_append_writes_one_header_and_accumulates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history" / "2026-10-13.csv"
            append_csv(path, csv_rows(make_report()))
            append_csv(path, csv_rows(make_report()))

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0].count("scan_ts_utc"), 1)
            self.assertEqual(sum(1 for line in lines if line.startswith("scan_ts_utc")), 1)
            with path.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 8)
            self.assertEqual(rows[0]["interval"], "15m")


class TestConsole(unittest.TestCase):
    def test_summary_lists_each_signal(self):
        text = console_summary(make_report())
        self.assertIn("signals      : 2", text)
        self.assertIn("NSE:TATAELXSI", text)
        self.assertIn("BOX BREAKOUT", text)
        self.assertIn("(latest)", text)

    def test_summary_counts_failures(self):
        report = make_report()
        report.failures = [("NSE:X", "boom")]
        self.assertIn("1 failure(s)", console_summary(report))


if __name__ == "__main__":
    unittest.main()
