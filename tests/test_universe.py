"""Universe tests: NSE file parsing, merge/curation rules, and the committed CSV."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from ironman.watchlist import ROOT, load_universe, load_watchlist

import importlib.util

SYNC_PATH = ROOT / "tools" / "sync_universe.py"
_spec = importlib.util.spec_from_file_location("ironman_sync_universe", SYNC_PATH)
sync = importlib.util.module_from_spec(_spec)          # type: ignore[arg-type]
_spec.loader.exec_module(sync)                         # type: ignore[union-attr]

SAMPLE = Path(__file__).resolve().parent / "fixtures" / "fo_mktlots_sample.csv"
UNIVERSE_CSV = ROOT / "data" / "fno_universe.csv"


class TestParseMktlots(unittest.TestCase):
    def setUp(self):
        self.pairs = sync.parse_mktlots(SAMPLE.read_text(encoding="utf-8"))
        self.symbols = [s for s, _ in self.pairs]

    def test_only_the_stock_section_is_returned(self):
        # Indices cannot be scanned as scrips, so they must not leak in.
        for index in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "NIFTYFPI"):
            with self.subTest(index=index):
                self.assertNotIn(index, self.symbols)

    def test_the_section_header_is_not_a_symbol(self):
        self.assertNotIn("Symbol", self.symbols)
        self.assertNotIn("UNDERLYING", self.symbols)

    def test_stocks_are_parsed_with_their_underlying(self):
        self.assertEqual(
            self.pairs,
            [
                ("360ONE", "360 ONE WAM LIMITED"),
                ("M&M", "MAHINDRA & MAHINDRA LTD"),
                ("GVT&D", "GE VERNOVA T&D INDIA LTD"),
                ("RELIANCE", "RELIANCE INDUSTRIES LTD"),
                ("CROMPTON", "CROMPT GREA CON ELEC LTD"),
                ("DIVISLAB", "DIVI S LABORATORIES LTD"),
            ],
        )

    def test_ampersand_symbols_survive(self):
        # M&M and GVT&D are real NSE tickers; a naive regex drops them.
        self.assertIn("M&M", self.symbols)
        self.assertIn("GVT&D", self.symbols)

    def test_blank_trailing_row_is_ignored(self):
        self.assertEqual(len(self.pairs), 6)

    def test_missing_section_header_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            sync.parse_mktlots("UNDERLYING,SYMBOL\nNIFTY 50,NIFTY\n")
        self.assertIn("Individual Securities", str(ctx.exception))

    def test_empty_stock_section_is_rejected(self):
        header = "Derivatives on Individual Securities,Symbol,SEP-26\n"
        with self.assertRaises(ValueError):
            sync.parse_mktlots(header)


class TestMetadata(unittest.TestCase):
    def test_curated_symbols_use_the_curated_name(self):
        self.assertEqual(sync.display_name("RELIANCE", "RELIANCE INDUSTRIES LTD"), "Reliance Industries")
        self.assertEqual(sync.display_name("M&M", "MAHINDRA & MAHINDRA LTD"), "Mahindra & Mahindra")

    def test_unknown_symbol_falls_back_to_nse_text(self):
        name = sync.display_name("NEWCO", "SOME NEW COMPANY LTD")
        self.assertEqual(name, "Some New Company")

    def test_unknown_symbol_gets_the_unknown_sector(self):
        self.assertEqual(sync.sector_for("NEWCO"), "Unknown")
        self.assertEqual(sync.sector_for("RELIANCE"), "Diversified")


class TestMerge(unittest.TestCase):
    def setUp(self):
        self.nse = sync.parse_mktlots(SAMPLE.read_text(encoding="utf-8"))

    def test_creates_prefixed_rows_from_scratch(self):
        rows, added, removed = sync.merge([], self.nse)
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]["symbol"], "NSE:360ONE")
        self.assertIn("NSE:M&M", [r["symbol"] for r in rows])
        self.assertEqual(removed, [])
        self.assertIn("360ONE", added)

    def test_curated_values_are_preserved(self):
        existing = [
            {"symbol": "NSE:RELIANCE", "name": "My Own Name", "sector": "My Sector", "enabled": "0"}
        ]
        rows, added, removed = sync.merge(existing, self.nse)
        reloaded = {r["symbol"]: r for r in rows}["NSE:RELIANCE"]
        self.assertEqual(reloaded["name"], "My Own Name")
        self.assertEqual(reloaded["sector"], "My Sector")
        self.assertEqual(reloaded["enabled"], "0")          # a disabled name stays disabled
        self.assertNotIn("RELIANCE", added)

    def test_dropped_symbols_are_reported_and_removed(self):
        existing = [{"symbol": "NSE:DELISTED", "name": "Gone", "sector": "IT", "enabled": "1"}]
        rows, added, removed = sync.merge(existing, self.nse)
        self.assertEqual(removed, ["NSE:DELISTED"])
        self.assertNotIn("NSE:DELISTED", [r["symbol"] for r in rows])

    def test_output_follows_nse_order_for_stable_diffs(self):
        rows, _, _ = sync.merge([], self.nse)
        self.assertEqual([r["symbol"] for r in rows][:3], ["NSE:360ONE", "NSE:M&M", "NSE:GVT&D"])

    def test_new_symbols_get_nse_name_and_unknown_sector(self):
        nse = [("BRANDNEW", "BRAND NEW INDUSTRIES LTD")]
        rows, added, _ = sync.merge([], nse)
        self.assertEqual(rows[0]["name"], "Brand New Industries")
        self.assertEqual(rows[0]["sector"], "Unknown")
        self.assertEqual(added, ["BRANDNEW"])

    def test_round_trip_through_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "universe.csv"
            rows, _, _ = sync.merge([], self.nse)
            sync.write_universe(path, rows)
            again = sync.read_universe(path)
            self.assertEqual([r["symbol"] for r in again], [r["symbol"] for r in rows])


class TestCommittedUniverse(unittest.TestCase):
    def setUp(self):
        with UNIVERSE_CSV.open(newline="", encoding="utf-8") as fh:
            self.raw = list(csv.DictReader(fh))
        self.rows = load_universe()

    def test_has_the_expected_shape(self):
        self.assertEqual(list(self.raw[0].keys()), ["symbol", "name", "sector", "enabled"])
        self.assertGreaterEqual(len(self.rows), 200)

    def test_symbols_are_unique_valid_and_prefixed(self):
        symbols = [r["symbol"] for r in self.rows]
        self.assertEqual(len(symbols), len(set(symbols)))
        for symbol in symbols:
            self.assertTrue(symbol.startswith("NSE:"), symbol)
            self.assertRegex(symbol, r"^NSE:[A-Z0-9_.\-&]+$")

    def test_no_indices_leaked_in(self):
        symbols = {r["symbol"] for r in self.rows}
        for index in ("NSE:NIFTY", "NSE:BANKNIFTY", "NSE:FINNIFTY", "NSE:MIDCPNIFTY"):
            self.assertNotIn(index, symbols)

    def test_every_row_is_curated(self):
        # The seeded file is fully classified; a future sync may add 'Unknown'
        # rows, which is why this is asserted here rather than in the loader.
        for row in self.rows:
            with self.subTest(symbol=row["symbol"]):
                self.assertTrue(row["name"].strip())
                self.assertTrue(row["sector"].strip())
                self.assertNotEqual(row["sector"], "Unknown")

    def test_ampersand_names_are_present(self):
        symbols = {r["symbol"] for r in self.rows}
        self.assertIn("NSE:M&M", symbols)
        self.assertIn("NSE:GVT&D", symbols)

    def test_all_rows_are_enabled_by_default(self):
        self.assertTrue(all(r["enabled"].strip() == "1" for r in self.rows))

    def test_universe_is_not_capped_at_forty(self):
        self.assertGreater(len(load_universe()), 40)

    def test_watchlist_loader_still_enforces_the_pine_ceiling(self):
        # The 40-symbol limit is a Pine constraint; the universe file must fail
        # under the watchlist loader, proving the two paths differ on purpose.
        with self.assertRaises(SystemExit) as ctx:
            load_watchlist(str(UNIVERSE_CSV))
        self.assertIn("at most 40", str(ctx.exception))

    def test_coexists_with_the_price_band_watchlist(self):
        watchlist = load_watchlist()
        universe = load_universe()
        self.assertLessEqual(len(watchlist), 40)
        # Every *enabled* watchlist name must also be a current F&O underlying.
        # This is the invariant that catches a hand-curated list going stale
        # (it is how NSE:LTTS and the old NSE:LTIM were found).
        missing = {r["symbol"] for r in watchlist} - {r["symbol"] for r in universe}
        self.assertEqual(missing, set(), f"watchlist names absent from the universe: {missing}")

    def test_audit_reports_a_stale_watchlist_name(self):
        nse = {"RELIANCE", "TCS"}                     # LTTS absent, as in reality
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wl.csv"
            path.write_text(
                "symbol,name,sector,approx_price,enabled\n"
                "NSE:RELIANCE,Reliance,Diversified,1400,1\n"
                "NSE:LTTS,L&T Technology Services,IT,4200,1\n"
                "NSE:SBIN,State Bank of India,Banks,800,0\n",
                encoding="utf-8",
            )
            stale = sync.audit_watchlist(nse, path)
        # disabled rows are not reported: only what would actually be scanned
        self.assertEqual(stale, ["LTTS"])

    def test_audit_is_clean_for_the_committed_watchlist(self):
        nse = {r["symbol"].split(":")[-1] for r in self.rows}
        self.assertEqual(sync.audit_watchlist(nse, ROOT / "data" / "fno_watchlist_price_band.csv"), [])


if __name__ == "__main__":
    unittest.main()
