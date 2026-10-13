"""Dashboard tests: rendering, escaping, embedded payload, and self-containment."""

from __future__ import annotations

import json
import re
import unittest
from datetime import datetime, timedelta, timezone

from ironman.dashboard import dashboard_payload, render_dashboard
from ironman.engine import SymbolScan, scan_symbol
from ironman.report import ScanReport

from tests import synth

BAND = {"min_price": 1000.0, "max_price": 10000.0}
START = datetime(2026, 10, 13, 3, 45, tzinfo=timezone.utc)


def _scan(symbol, bars, *, name="Test Co", sector="IT", lookback=2):
    return scan_symbol(symbol, name, sector, bars, lookback=lookback, **BAND)


def make_report(**overrides) -> ScanReport:
    results = [
        _scan("NSE:TATAELXSI", synth.box_breakout_long(), name="Tata Elxsi", sector="IT"),
        _scan("NSE:HAL", synth.sweep_long_reversal(), name="Hindustan Aeronautics",
              sector="Capital Goods & Defence"),
        _scan("NSE:INFY", synth.chop_in_band(), name="Infosys", sector="IT"),
        _scan("NSE:HDFCBANK", synth.quiet_out_of_band_low(), name="HDFC Bank", sector="Banks"),
    ]
    fields = dict(
        results=results,
        interval="15m",
        lookback=2,
        min_price=1000.0,
        max_price=10000.0,
        started_at=START,
        finished_at=START + timedelta(seconds=7),
        source="fixture",
    )
    fields.update(overrides)
    return ScanReport(**fields)


def payload_of(html_text: str) -> dict:
    match = re.search(
        r'<script type="application/json" id="scan-data">(.*?)</script>', html_text, re.S
    )
    assert match, "scan-data payload missing"
    import html as html_module

    return json.loads(html_module.unescape(match.group(1)))


class TestStructure(unittest.TestCase):
    def setUp(self):
        self.html = render_dashboard(make_report())

    def test_is_a_complete_document(self):
        self.assertTrue(self.html.lstrip().startswith("<!doctype html>"))
        self.assertIn("</html>", self.html)

    def test_is_self_contained(self):
        # No CDN, no fonts, no remote scripts: it has to work from a file:// path
        # and inside a sandboxed artifact viewer.
        self.assertNotIn("<link", self.html)
        self.assertNotIn('src="http', self.html)
        self.assertNotIn('href="http', self.html)
        self.assertNotIn("@import", self.html)

    def test_has_no_placeholder_left_behind(self):
        for marker in ("@@__", "{report.", "None</td>"):
            self.assertNotIn(marker, self.html)

    def test_header_shows_the_scan_configuration(self):
        self.assertIn("15m bars", self.html)
        self.assertIn("lookback 2", self.html)
        self.assertIn("₹1,000 – ₹10,000", self.html)
        self.assertIn("SIG 2", self.html)
        self.assertIn("4/4 scanned", self.html)

    def test_stat_cards(self):
        self.assertIn(">2</b><span>signals ≥4/5", self.html)
        self.assertIn(">7.0s</b><span>scan time", self.html)

    def test_disclaimer_is_present(self):
        self.assertIn("not investment advice", self.html)
        self.assertIn("has not been diffed against TradingView", self.html)


class TestSignalsTable(unittest.TestCase):
    def test_signals_are_listed(self):
        html = render_dashboard(make_report())
        table = html.split('<table id="signals">')[1].split("</table>")[0]
        self.assertIn("TATAELXSI", table)
        self.assertIn("BOX BREAKOUT", table)
        self.assertIn("HAL", table)
        self.assertIn("LONG 2A SWEEP", table)

    def test_no_signals_shows_an_explanation(self):
        html = render_dashboard(
            make_report(results=[_scan("NSE:INFY", synth.chop_in_band())])
        )
        self.assertIn("No watchlist symbol scored ≥4/5", html)
        self.assertIn("SIG 0", html)

    def test_empty_state_is_not_an_empty_table(self):
        html = render_dashboard(make_report(results=[_scan("NSE:INFY", synth.chop_in_band())]))
        self.assertNotIn('<table id="signals">', html)


class TestUniverseTable(unittest.TestCase):
    def setUp(self):
        self.html = render_dashboard(make_report())
        self.body = self.html.split('<table id="universe">')[1].split("</table>")[0]

    def test_every_scanned_symbol_has_a_row(self):
        self.assertEqual(self.body.count("data-symbol="), 4)

    def test_rows_carry_filter_and_sort_keys(self):
        for attribute in ('data-search=', 'data-score=', 'data-in=', 'data-sig=',
                          'data-sector=', 'data-price=', 'data-age='):
            with self.subTest(attribute=attribute):
                self.assertIn(attribute, self.body)

    def test_out_of_band_row_is_marked(self):
        row = [line for line in self.body.splitlines() if "HDFCBANK" in line][0]
        self.assertIn('data-in="0"', row)
        self.assertIn("OUT OF RANGE", row)
        self.assertIn("OUT OF BAND", row)
        self.assertIn('class="oor', row)

    def test_in_band_row_is_marked(self):
        row = [line for line in self.body.splitlines() if "INFY" in line][0]
        self.assertIn('data-in="1"', row)

    def test_short_setups_are_flagged_for_colouring(self):
        html = render_dashboard(make_report(results=[_scan("NSE:X", synth.chop_in_band())]))
        self.assertIn("data-sector=", html)

    def test_sector_options_are_offered(self):
        self.assertIn('<option value="IT">IT</option>', self.html)
        self.assertIn('<option value="Banks">Banks</option>', self.html)
        self.assertIn('<option value="Capital Goods &amp; Defence">', self.html)


class TestEscaping(unittest.TestCase):
    def test_hostile_symbol_and_name_are_escaped(self):
        hostile = _scan(
            "NSE:<script>alert(1)</script>",
            synth.chop_in_band(),
            name='"><img src=x onerror=alert(2)>',
            sector="<b>sector</b>",
        )
        html = render_dashboard(make_report(results=[hostile]))
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<img src=x", html)
        self.assertNotIn("<b>sector</b>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_error_text_is_escaped(self):
        broken = SymbolScan(
            symbol="NSE:BROKEN", name="<i>x</i>", sector="IT", status="fetch_error",
            bars=0, latest=None, hit=None, bars_ago=None, error="<script>bad()</script>",
        )
        html = render_dashboard(make_report(results=[broken]))
        self.assertNotIn("<script>bad()</script>", html)
        self.assertIn("&lt;script&gt;bad()", html)


class TestFailures(unittest.TestCase):
    def test_failed_symbols_are_listed(self):
        broken = SymbolScan(
            symbol="NSE:BROKEN", name="Broken", sector="IT", status="fetch_error",
            bars=0, latest=None, hit=None, bars_ago=None, error="HTTP 404",
        )
        report = make_report(results=[_scan("NSE:INFY", synth.chop_in_band()), broken])
        report.failures = [("NSE:BROKEN", "HTTP 404")]
        html = render_dashboard(report)
        self.assertIn("Skipped", html)
        self.assertIn("HTTP 404", html)
        self.assertIn('class="stat warn"', html)


class TestPayload(unittest.TestCase):
    def setUp(self):
        self.report = make_report()
        self.payload = dashboard_payload(self.report)

    def test_counts(self):
        counts = self.payload["counts"]
        self.assertEqual(counts["universe"], 4)
        self.assertEqual(counts["scanned"], 4)
        self.assertEqual(counts["signals"], 2)
        self.assertEqual(counts["out_of_band"], 1)
        self.assertEqual(counts["failures"], 0)

    def test_band_and_settings_are_recorded(self):
        self.assertEqual(self.payload["band"], {"low": 1000.0, "high": 10000.0})
        self.assertEqual(self.payload["interval"], "15m")
        self.assertEqual(self.payload["threshold"], 4)
        self.assertTrue(self.payload["generated_at"].endswith("Z"))

    def test_symbol_records(self):
        by_symbol = {s["symbol"]: s for s in self.payload["symbols"]}
        tata = by_symbol["NSE:TATAELXSI"]
        self.assertEqual(tata["score"], 5)
        self.assertEqual(tata["setup"], "BOX BREAKOUT")
        self.assertTrue(tata["in_band"])
        self.assertEqual(tata["signal_score"], 5)
        self.assertEqual(tata["bars_ago"], 0)

        hdfc = by_symbol["NSE:HDFCBANK"]
        self.assertFalse(hdfc["in_band"])
        self.assertIsNone(hdfc["signal_score"])

    def test_embedded_payload_matches_the_payload_function(self):
        self.assertEqual(payload_of(render_dashboard(self.report)), self.payload)

    def test_payload_is_valid_json_even_with_hostile_names(self):
        hostile = _scan("NSE:ZZ", synth.chop_in_band(), name='</script><script>x()</script>')
        html = render_dashboard(make_report(results=[hostile]))
        self.assertEqual(len(payload_of(html)["symbols"]), 1)


class TestLookbackInteraction(unittest.TestCase):
    def test_signal_age_appears_on_the_dashboard(self):
        report = make_report(results=[_scan("NSE:STALE", synth.box_breakout_then_quiet())])
        html = render_dashboard(report)
        self.assertIn("+1 bars", html)
        self.assertEqual(payload_of(html)["symbols"][0]["bars_ago"], 1)


if __name__ == "__main__":
    unittest.main()
