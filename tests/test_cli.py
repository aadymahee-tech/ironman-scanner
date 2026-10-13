"""End-to-end CLI tests, run entirely offline against the committed fixture.

The flag shape here mirrors what ``.github/workflows/main.yml`` invokes, so a
change that breaks the workflow breaks these tests too.
"""

from __future__ import annotations

import csv
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import ironman_scanner

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ENGINE_FIXTURE = FIXTURES / "engine.json"
WATCHLIST = FIXTURES / "watchlist.csv"


def run(*args) -> tuple:
    """Run the CLI, returning (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = ironman_scanner.main(list(args))
        except SystemExit as exc:                       # argparse / config errors
            code = exc.code if isinstance(exc.code, int) else 1
            print(exc, file=err)
    return code, out.getvalue(), err.getvalue()


BASE = ("--source", "fixture", "--fixture", str(ENGINE_FIXTURE), "--watchlist", str(WATCHLIST))


class TestFixtureRun(unittest.TestCase):
    def test_workflow_flag_shape_runs_clean(self):
        code, out, err = run(*BASE, "--interval", "15m", "--lookback", "2",
                             "--low", "1000", "--high", "10000", "--workers", "4",
                             "--md", "-", "--quiet")
        self.assertEqual(code, 0)
        self.assertIn("## Signals", out)
        self.assertIn("BOX BREAKOUT", out)
        self.assertIn("LONG 2A SWEEP", out)
        self.assertIn("2 signal(s)", out)

    def test_out_of_band_symbol_never_signals(self):
        _, out, _ = run(*BASE, "--md", "-", "--quiet")
        self.assertIn("**Out of band (1):** NSE:HDFCBANK", out)

    def test_csv_history_is_written_with_every_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history" / "2026-10-13.csv"
            code, _, _ = run(*BASE, "--csv", str(path), "--quiet")
            self.assertEqual(code, 0)
            with path.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 4)
            self.assertEqual(
                {r["symbol"] for r in rows},
                {"NSE:TATAELXSI", "NSE:HAL", "NSE:INFY", "NSE:HDFCBANK"},
            )
            self.assertEqual({r["status"] for r in rows}, {"ok"})

    def test_markdown_file_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.md"
            code, _, _ = run(*BASE, "--md", str(path), "--quiet")
            self.assertEqual(code, 0)
            self.assertIn("# IronMan F&O scan", path.read_text(encoding="utf-8"))

    def test_narrow_band_suppresses_signals_but_not_the_scan(self):
        code, out, _ = run(*BASE, "--low", "1500", "--high", "2000", "--md", "-", "--quiet")
        self.assertEqual(code, 0)
        self.assertIn("**0 signal(s)**", out)
        self.assertIn("No watchlist symbol scored", out)

    def test_summary_is_always_printed(self):
        _, out, _ = run(*BASE, "--md", "-", "--quiet")
        self.assertIn("scanned      : 4/4 symbols", out)
        self.assertIn("interval     : 15m  lookback 2", out)


class TestDashboardFlag(unittest.TestCase):
    def test_html_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard.html"
            code, _, _ = run(*BASE, "--html", str(path), "--quiet")
            self.assertEqual(code, 0)
            html = path.read_text(encoding="utf-8")
            self.assertTrue(html.lstrip().startswith("<!doctype html>"))
            self.assertIn("BOX BREAKOUT", html)

    def test_dashboard_can_be_written_alongside_csv_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "h.csv"
            md_path = Path(tmp) / "latest.md"
            html_path = Path(tmp) / "dashboard.html"
            code, _, _ = run(*BASE, "--csv", str(csv_path), "--md", str(md_path),
                             "--html", str(html_path), "--quiet")
            self.assertEqual(code, 0)
            for path in (csv_path, md_path, html_path):
                self.assertTrue(path.exists(), path)


class TestUniverseFlag(unittest.TestCase):
    def test_scans_the_full_universe_file_not_the_watchlist(self):
        # The small fixture only contains 4 of the 210 universe symbols; the rest
        # fail to load, which is exactly what a partial feed looks like.
        code, out, _ = run("--universe", "--source", "fixture", "--fixture", str(ENGINE_FIXTURE),
                           "--md", "-", "--quiet")
        self.assertEqual(code, 0)                      # some symbols did resolve
        self.assertIn("/210 symbols scanned", out)
        self.assertIn("fetch failure(s)", out)

    def test_universe_dashboard_lists_every_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.html"
            run("--universe", "--source", "fixture", "--fixture", str(ENGINE_FIXTURE),
                "--html", str(path), "--quiet")
            body = path.read_text(encoding="utf-8").split('<table id="universe">')[1]
            body = body.split("</table>")[0]
            self.assertEqual(body.count("data-symbol="), 210)

    def test_explicit_watchlist_still_wins(self):
        code, out, _ = run(*BASE, "--md", "-", "--quiet")
        self.assertEqual(code, 0)
        self.assertIn("/4 symbols scanned", out)


class TestFailureHandling(unittest.TestCase):
    def test_symbol_missing_from_the_feed_is_reported_not_fatal(self):
        # Only NSE:INFY is in this fixture; the other three fail to load.
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "wl.csv"
            csv_path.write_text(
                "symbol,name,sector,approx_price,enabled\n"
                "NSE:INFY,Infosys,IT,1500,1\n"
                "NSE:NOPE,Missing,IT,1500,1\n",
                encoding="utf-8",
            )
            code, out, err = run("--source", "fixture", "--fixture", str(ENGINE_FIXTURE),
                                 "--watchlist", str(csv_path), "--md", "-")
        self.assertEqual(code, 0)                     # one symbol still scanned
        self.assertIn("fetch failure(s)", out)
        self.assertIn("## Skipped", out)
        self.assertIn("NSE:NOPE", err)

    def test_every_fetch_failing_is_an_error_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "wl.csv"
            csv_path.write_text(
                "symbol,name,sector,approx_price,enabled\nNSE:NOPE,Missing,IT,1500,1\n",
                encoding="utf-8",
            )
            code, _, err = run("--source", "fixture", "--fixture", str(ENGINE_FIXTURE),
                               "--watchlist", str(csv_path), "--quiet")
        self.assertEqual(code, 1)
        self.assertIn("no symbol could be scanned", err)

    def test_insufficient_history_is_counted_as_scanned(self):
        # A feed that returns too few bars is 'ok' but unscored, not a failure.
        with tempfile.TemporaryDirectory() as tmp:
            short = Path(tmp) / "short.json"
            short.write_text(
                '{"symbols": {"NSE:INFY": [[1700000000, 10, 11, 9, 10.5, 100]]}}', encoding="utf-8"
            )
            csv_path = Path(tmp) / "wl.csv"
            csv_path.write_text(
                "symbol,name,sector,approx_price,enabled\nNSE:INFY,Infosys,IT,1500,1\n",
                encoding="utf-8",
            )
            code, out, _ = run("--source", "fixture", "--fixture", str(short),
                               "--watchlist", str(csv_path), "--md", "-", "--quiet")
        self.assertEqual(code, 0)
        self.assertIn("insufficient_data", out)


class TestArgumentValidation(unittest.TestCase):
    def test_low_must_be_below_high(self):
        code, _, err = run(*BASE, "--low", "5000", "--high", "1000", "--quiet")
        self.assertEqual(code, 1)
        self.assertIn("must be below", err)

    def test_lookback_must_be_positive(self):
        code, _, err = run(*BASE, "--lookback", "0", "--quiet")
        self.assertEqual(code, 1)
        self.assertIn("--lookback must be >= 1", err)

    def test_fixture_source_requires_a_fixture_path(self):
        code, _, err = run("--source", "fixture", "--watchlist", str(WATCHLIST), "--quiet")
        self.assertEqual(code, 1)
        self.assertIn("requires --fixture", err)

    def test_unknown_interval_is_rejected_even_offline(self):
        # Must be validated up front, not lazily by the network provider —
        # otherwise a fixture run would silently accept a nonsense interval.
        code, _, err = run(*BASE, "--interval", "9h", "--quiet")
        self.assertEqual(code, 1)
        self.assertIn("--interval must be one of", err)

    def test_argparse_rejects_an_unknown_source(self):
        # argparse writes its usage to stderr before exiting; keep it out of the
        # test output.
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                ironman_scanner.parse_args(["--source", "carrier-pigeon"])
        self.assertEqual(ctx.exception.code, 2)

    def test_parse_args_matches_the_workflow_invocation(self):
        args = ironman_scanner.parse_args([
            "--interval", "15m", "--lookback", "2", "--low", "1000", "--high", "10000",
            "--workers", "6", "--csv", "results/history/2026-10-13.csv", "--md", "results/latest.md",
        ])
        self.assertEqual(args.interval, "15m")
        self.assertEqual(args.lookback, 2)
        self.assertEqual(args.workers, 6)
        self.assertEqual(str(args.csv), "results/history/2026-10-13.csv")
        self.assertEqual(args.md, "results/latest.md")


if __name__ == "__main__":
    unittest.main()
