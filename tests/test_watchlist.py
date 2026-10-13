"""Watchlist tests — including the Pine 40-slot ceiling, from the scanner's side."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ironman.watchlist import ROOT, load_watchlist

REAL_CSV = ROOT / "data" / "fno_watchlist_price_band.csv"
HEADER = "symbol,name,sector,approx_price,enabled\n"


class TestRealWatchlist(unittest.TestCase):
    def setUp(self):
        self.rows = load_watchlist()

    def test_only_enabled_rows_are_returned(self):
        self.assertEqual(len(self.rows), 40)
        self.assertTrue(all(r["enabled"].strip() == "1" for r in self.rows))

    def test_symbols_are_unique_and_well_formed(self):
        symbols = [r["symbol"] for r in self.rows]
        self.assertEqual(len(symbols), len(set(symbols)))
        for symbol in symbols:
            self.assertRegex(symbol, r"^[A-Z0-9_.\-]+:[A-Z0-9_.\-]+$")

    def test_rows_carry_the_columns_the_report_needs(self):
        for row in self.rows:
            self.assertTrue(row["name"].strip())
            self.assertTrue(row["sector"].strip())

    def test_known_names_are_present(self):
        symbols = {r["symbol"] for r in self.rows}
        self.assertIn("NSE:HDFCBANK", symbols)
        self.assertIn("NSE:TATAELXSI", symbols)


class TestValidationIsSharedWithTheGenerator(unittest.TestCase):
    """The scanner reuses the generator's loader, so its guards must still bite."""

    def _csv(self, body: str) -> str:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        tmp.write(HEADER + body)
        tmp.close()
        return tmp.name

    def test_more_than_forty_enabled_rows_is_rejected(self):
        body = "".join(f"NSE:SYM{i},Name {i},IT,1000,1\n" for i in range(41))
        with self.assertRaises(SystemExit) as ctx:
            load_watchlist(self._csv(body))
        self.assertIn("at most 40", str(ctx.exception))

    def test_malformed_symbol_is_rejected(self):
        with self.assertRaises(SystemExit):
            load_watchlist(self._csv("HDFCBANK,HDFC Bank,Banking,1000,1\n"))

    def test_duplicate_symbol_is_rejected(self):
        body = "NSE:INFY,Infosys,IT,1500,1\nNSE:INFY,Infosys again,IT,1500,1\n"
        with self.assertRaises(SystemExit) as ctx:
            load_watchlist(self._csv(body))
        self.assertIn("duplicate", str(ctx.exception))

    def test_disabled_rows_are_ignored(self):
        rows = load_watchlist(self._csv("NSE:INFY,Infosys,IT,1500,1\nNSE:SBIN,SBI,Banking,800,0\n"))
        self.assertEqual([r["symbol"] for r in rows], ["NSE:INFY"])


class TestPathHandling(unittest.TestCase):
    def test_missing_file_exits_with_a_message(self):
        with self.assertRaises(SystemExit) as ctx:
            load_watchlist("/nonexistent/watchlist.csv")
        self.assertIn("not found", str(ctx.exception))

    def test_explicit_path_is_used(self):
        fixture = Path(__file__).resolve().parent / "fixtures" / "watchlist.csv"
        rows = load_watchlist(str(fixture))
        self.assertEqual(len(rows), 4)
        self.assertNotIn("NSE:SBIN", [r["symbol"] for r in rows])


if __name__ == "__main__":
    unittest.main()
