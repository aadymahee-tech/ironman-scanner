"""Data-feed tests: symbol mapping, Yahoo parsing, forming-bar rejection, fixtures."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ironman.datafeed import (
    IST,
    fetch_yahoo,
    interval_seconds,
    is_forming,
    load_fixture,
    parse_yahoo_payload,
    to_yahoo_ticker,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "engine.json"


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def payload(timestamps, *, closes, error=None):
    n = len(timestamps)
    series = closes if isinstance(closes, list) else [closes] * n
    return {
        "chart": {
            "error": error,
            "result": [
                {
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": [c - 1 for c in series],
                                "high": [c + 2 for c in series],
                                "low": [c - 2 for c in series],
                                "close": series,
                                "volume": [1000] * n,
                            }
                        ]
                    },
                }
            ],
        }
    }


class TestSymbolMapping(unittest.TestCase):
    def test_exchange_prefixes(self):
        self.assertEqual(to_yahoo_ticker("NSE:HDFCBANK"), "HDFCBANK.NS")
        self.assertEqual(to_yahoo_ticker("BSE:RELIANCE"), "RELIANCE.BO")
        self.assertEqual(to_yahoo_ticker("NFO:NIFTY24OCT"), "NIFTY24OCT.NS")

    def test_bare_ticker_passes_through(self):
        self.assertEqual(to_yahoo_ticker("TCS.NS"), "TCS.NS")

    def test_unknown_exchange_is_rejected(self):
        with self.assertRaises(ValueError):
            to_yahoo_ticker("NASDAQ:AAPL")


class TestIntervals(unittest.TestCase):
    def test_seconds_per_bar(self):
        self.assertEqual(interval_seconds("1m"), 60)
        self.assertEqual(interval_seconds("15m"), 900)
        self.assertEqual(interval_seconds("1h"), 3600)
        self.assertEqual(interval_seconds("1d"), 86400)

    def test_unknown_interval_is_rejected(self):
        with self.assertRaises(ValueError):
            interval_seconds("3h")


class TestFormingBar(unittest.TestCase):
    def test_intraday_bar_is_forming_until_its_period_ends(self):
        now = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)
        started_5_min_ago = now - timedelta(minutes=5)
        started_20_min_ago = now - timedelta(minutes=20)
        self.assertTrue(is_forming(started_5_min_ago, "15m", now))
        self.assertFalse(is_forming(started_20_min_ago, "15m", now))

    def test_daily_bar_closes_at_1530_ist(self):
        # A daily bar stamped 09:15 IST is still forming at 13:30 IST...
        bar_ts = datetime(2026, 10, 13, 3, 45, tzinfo=timezone.utc)
        self.assertTrue(is_forming(bar_ts, "1d", datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)))
        # ...and closed after 15:30 IST.
        self.assertFalse(is_forming(bar_ts, "1d", datetime(2026, 10, 13, 11, 0, tzinfo=timezone.utc)))

    def test_ist_offset_is_fixed(self):
        self.assertEqual(IST.utcoffset(None), timedelta(hours=5, minutes=30))


class TestYahooParsing(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.old = int((self.now - timedelta(days=2)).timestamp())
        self.future = int((self.now + timedelta(hours=3)).timestamp())

    def test_parses_bars_and_drops_the_forming_one(self):
        data = payload([self.old, self.old + 900, self.future], closes=[100.0, 101.0, 999.0])
        bars = parse_yahoo_payload(data, "15m")
        self.assertEqual(len(bars), 2)
        self.assertEqual([b.close for b in bars], [100.0, 101.0])
        self.assertLess(bars[0].ts, bars[1].ts)

    def test_skips_null_rows(self):
        data = payload([self.old, self.old + 900, self.old + 1800], closes=[100.0, 101.0, 102.0])
        quote = data["chart"]["result"][0]["indicators"]["quote"][0]
        for field in ("open", "high", "low", "close", "volume"):
            quote[field][1] = None
        bars = parse_yahoo_payload(data, "15m")
        self.assertEqual([round(b.close) for b in bars], [100, 102])

    def test_provider_error_is_raised(self):
        data = payload([], closes=[], error={"code": "Not Found", "description": "No data found"})
        with self.assertRaises(ValueError) as ctx:
            parse_yahoo_payload(data, "15m")
        self.assertIn("No data found", str(ctx.exception))

    def test_empty_result_is_raised(self):
        with self.assertRaises(ValueError):
            parse_yahoo_payload({"chart": {"result": [], "error": None}}, "15m")

    def test_missing_volume_defaults_to_zero(self):
        data = payload([self.old], closes=[100.0])
        del data["chart"]["result"][0]["indicators"]["quote"][0]["volume"]
        self.assertEqual(parse_yahoo_payload(data, "15m")[0].volume, 0.0)


class TestFetch(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.old = int((self.now - timedelta(days=2)).timestamp())

    def test_uses_the_yahoo_code_and_default_range(self):
        with mock.patch(
            "ironman.datafeed.urllib.request.urlopen",
            return_value=_FakeResponse(payload([self.old], closes=[1500.0])),
        ) as urlopen:
            bars = fetch_yahoo("NSE:INFY", "1h")
        url = urlopen.call_args[0][0].full_url
        self.assertIn("INFY.NS", url)
        self.assertIn("interval=60m", url)      # 1h is 60m to Yahoo
        self.assertIn("range=60d", url)
        self.assertEqual(len(bars), 1)

    def test_range_override_is_used(self):
        with mock.patch(
            "ironman.datafeed.urllib.request.urlopen",
            return_value=_FakeResponse(payload([self.old], closes=[1500.0])),
        ) as urlopen:
            fetch_yahoo("NSE:INFY", "15m", range_="30d")
        self.assertIn("range=30d", urlopen.call_args[0][0].full_url)

    def test_retries_alternate_between_mirror_hosts(self):
        with mock.patch(
            "ironman.datafeed.urllib.request.urlopen",
            side_effect=[OSError("rate limited"), _FakeResponse(payload([self.old], closes=[7.0]))],
        ) as urlopen, mock.patch("ironman.datafeed.time.sleep"):
            bars = fetch_yahoo("NSE:INFY", "15m", retries=1)
        hosts = [call[0][0].full_url.split("/")[2] for call in urlopen.call_args_list]
        self.assertEqual(hosts, ["query1.finance.yahoo.com", "query2.finance.yahoo.com"])
        self.assertEqual(len(bars), 1)

    def test_failure_after_retries_raises_runtime_error(self):
        with mock.patch(
            "ironman.datafeed.urllib.request.urlopen", side_effect=OSError("boom")
        ) as urlopen, mock.patch("ironman.datafeed.time.sleep") as sleep:
            with self.assertRaises(RuntimeError) as ctx:
                fetch_yahoo("NSE:INFY", "15m", retries=2)
        self.assertEqual(urlopen.call_count, 3)          # initial + 2 retries
        self.assertEqual(sleep.call_count, 2)
        self.assertIn("NSE:INFY", str(ctx.exception))

    def test_unknown_interval_is_rejected_before_any_request(self):
        with mock.patch("ironman.datafeed.urllib.request.urlopen") as urlopen:
            with self.assertRaises(ValueError):
                fetch_yahoo("NSE:INFY", "9h")
        urlopen.assert_not_called()


class TestFixtureLoader(unittest.TestCase):
    def _write(self, obj) -> str:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(obj, tmp)
        tmp.close()
        return tmp.name

    def test_nested_symbols_shape(self):
        path = self._write({"symbols": {"NSE:X": [[1700000000, 1, 2, 0.5, 1.5, 100]]}})
        bars = load_fixture(path, "NSE:X")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].close, 1.5)

    def test_flat_shape_and_iso_timestamps(self):
        path = self._write({"NSE:Y": [["2026-01-05T03:45:00Z", 10, 11, 9, 10.5, 500]]})
        bars = load_fixture(path, "NSE:Y")
        self.assertEqual(bars[0].open, 10.0)
        self.assertEqual(bars[0].ts.year, 2026)

    def test_bars_are_sorted_ascending(self):
        path = self._write(
            {"symbols": {"NSE:Z": [[1700000900, 1, 2, 0.5, 1.5, 1], [1700000000, 1, 2, 0.5, 1.5, 1]]}}
        )
        bars = load_fixture(path, "NSE:Z")
        self.assertLess(bars[0].ts, bars[1].ts)

    def test_missing_symbol_is_a_key_error(self):
        path = self._write({"symbols": {"NSE:X": [[1700000000, 1, 2, 0.5, 1.5, 100]]}})
        with self.assertRaises(KeyError):
            load_fixture(path, "NSE:ABSENT")

    def test_short_row_is_rejected(self):
        path = self._write({"symbols": {"NSE:X": [[1700000000, 1, 2]]}})
        with self.assertRaises(ValueError):
            load_fixture(path, "NSE:X")

    def test_volume_may_be_omitted(self):
        path = self._write({"symbols": {"NSE:X": [[1700000000, 1, 2, 0.5, 1.5]]}})
        self.assertEqual(load_fixture(path, "NSE:X")[0].volume, 0.0)

    def test_committed_fixture_has_every_documented_symbol(self):
        for symbol in ("NSE:TATAELXSI", "NSE:HAL", "NSE:INFY", "NSE:HDFCBANK"):
            with self.subTest(symbol=symbol):
                self.assertGreaterEqual(len(load_fixture(str(FIXTURE), symbol)), 101)


if __name__ == "__main__":
    unittest.main()
