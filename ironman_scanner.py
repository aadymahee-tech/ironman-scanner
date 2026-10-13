#!/usr/bin/env python3
"""IronMan F&O scanner — headless port of the TradingView indicator.

This is the program ``.github/workflows/main.yml`` runs on a schedule. It scores
every enabled symbol in ``data/fno_watchlist_price_band.csv`` with the v16.1/v16.2
engine (see ``docs/LOGIC.md``), applies the ₹1,000–10,000 price-band gate (see
``docs/PRICE_BAND.md``), and writes a markdown summary plus a CSV history row per
symbol so the run can be audited later.

Only the standard library is used, so it runs on a bare CI Python.

Usage
-----
    python3 ironman_scanner.py --interval 15m --lookback 2 --md results/latest.md
    python3 ironman_scanner.py --csv results/history/$(date -u +%F).csv --md results/latest.md
    python3 ironman_scanner.py --source fixture --fixture tests/fixtures/engine.json --md -

Offline smoke test (no network, no watchlist fetch):

    python3 ironman_scanner.py --source fixture --fixture tests/fixtures/engine.json --md -
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ironman.datafeed import INTERVALS, fetch_yahoo, load_fixture  # noqa: E402
from ironman.engine import Bar, SymbolScan, scan_symbol  # noqa: E402
from ironman.report import (  # noqa: E402
    ScanReport,
    append_csv,
    console_summary,
    csv_rows,
    render_markdown,
    write_markdown,
)
from ironman.watchlist import load_watchlist  # noqa: E402


@dataclass
class _Fetched:
    symbol: str
    bars: List[Bar]
    error: Optional[str] = None


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Scan the IronMan F&O watchlist and write results.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--interval", default="15m",
                    help="candle size: 1m, 5m, 15m, 30m, 1h, 1d (default: 15m)")
    ap.add_argument("--lookback", type=int, default=2,
                    help="closed candles in the trigger window (default: 2)")
    ap.add_argument("--low", type=float, default=1000.0,
                    help="price-band floor in ₹ (default: 1000)")
    ap.add_argument("--high", type=float, default=10000.0,
                    help="price-band ceiling in ₹ (default: 10000)")
    ap.add_argument("--workers", type=int, default=6,
                    help="concurrent fetches (default: 6)")
    ap.add_argument("--csv", type=Path, default=None,
                    help="append per-symbol rows to this CSV (created if missing)")
    ap.add_argument("--md", default=None,
                    help="write the markdown summary here; '-' for stdout")
    ap.add_argument("--watchlist", default=None,
                    help="watchlist CSV (default: data/fno_watchlist_price_band.csv)")
    ap.add_argument("--source", choices=("yahoo", "fixture"), default="yahoo",
                    help="where bars come from (default: yahoo)")
    ap.add_argument("--fixture", default=None,
                    help="fixture JSON for --source fixture")
    ap.add_argument("--history-range", default=None,
                    help="override the provider's lookback window, e.g. 30d")
    ap.add_argument("--timeout", type=float, default=20.0,
                    help="per-request timeout in seconds (default: 20)")
    ap.add_argument("--quiet", action="store_true",
                    help="only print the final summary")
    ap.add_argument("--verbose", action="store_true",
                    help="print a line per symbol")
    return ap.parse_args(argv)


def fetch_all(symbols: List[dict], args: argparse.Namespace) -> List[_Fetched]:
    """Fetch every symbol's bars, concurrently, never raising on a single failure."""
    if args.source == "fixture":
        if not args.fixture:
            sys.exit("ERROR: --source fixture requires --fixture <file.json>")
        out: List[_Fetched] = []
        for row in symbols:
            symbol = row["symbol"].strip()
            try:
                out.append(_Fetched(symbol, load_fixture(args.fixture, symbol)))
            except Exception as exc:                                   # noqa: BLE001
                out.append(_Fetched(symbol, [], f"{type(exc).__name__}: {exc}"))
    else:
        def one(row: dict) -> _Fetched:
            symbol = row["symbol"].strip()
            try:
                bars = fetch_yahoo(
                    symbol, args.interval, timeout=args.timeout, range_=args.history_range
                )
                return _Fetched(symbol, bars)
            except Exception as exc:                                   # noqa: BLE001
                return _Fetched(symbol, [], str(exc))

        out = []
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {pool.submit(one, row): row["symbol"].strip() for row in symbols}
            for future in as_completed(futures):
                out.append(future.result())

    # Log per-symbol outcomes the same way for both sources, so a failure is
    # always visible in the Actions log rather than only in the summary.
    for fetched in sorted(out, key=lambda f: f.symbol):
        if fetched.error:
            if not args.quiet:
                print(f"  ! {fetched.symbol}: {fetched.error}", file=sys.stderr)
        elif args.verbose and not args.quiet:
            print(f"  · {fetched.symbol}: {len(fetched.bars)} bars", file=sys.stderr)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    if args.low >= args.high:
        sys.exit(f"ERROR: --low ({args.low:g}) must be below --high ({args.high:g})")
    if args.lookback < 1:
        sys.exit("ERROR: --lookback must be >= 1")
    if args.interval not in INTERVALS:
        sys.exit(f"ERROR: --interval must be one of {', '.join(sorted(INTERVALS))}")

    started = datetime.now(timezone.utc)

    watchlist = load_watchlist(args.watchlist)
    if not args.quiet:
        print(f"watchlist    : {len(watchlist)} enabled symbol(s)", file=sys.stderr)

    fetched = {f.symbol: f for f in fetch_all(watchlist, args)}

    results: List[SymbolScan] = []
    failures: List[tuple] = []
    for row in watchlist:
        symbol = row["symbol"].strip()
        data = fetched.get(symbol)
        if data is None or data.error is not None:
            error = data.error if data else "no data returned"
            failures.append((symbol, error))
            results.append(
                SymbolScan(
                    symbol=symbol,
                    name=row.get("name", ""),
                    sector=row.get("sector", ""),
                    status="fetch_error",
                    bars=0,
                    latest=None,
                    hit=None,
                    bars_ago=None,
                    error=error,
                )
            )
            continue
        results.append(
            scan_symbol(
                symbol=symbol,
                name=row.get("name", ""),
                sector=row.get("sector", ""),
                bars=data.bars,
                min_price=args.low,
                max_price=args.high,
                lookback=args.lookback,
            )
        )

    finished = datetime.now(timezone.utc)
    report = ScanReport(
        results=results,
        interval=args.interval,
        lookback=args.lookback,
        min_price=args.low,
        max_price=args.high,
        started_at=started,
        finished_at=finished,
        failures=failures,
        source=args.source,
    )

    markdown = render_markdown(report)

    if args.csv:
        append_csv(Path(args.csv), csv_rows(report))
        if not args.quiet:
            print(f"wrote        : {Path(args.csv)}", file=sys.stderr)

    if args.md == "-":
        print(markdown)
    elif args.md:
        write_markdown(Path(args.md), markdown)
        if not args.quiet:
            print(f"wrote        : {Path(args.md)}", file=sys.stderr)

    print(console_summary(report))

    # Exit non-zero only when nothing could be fetched at all — that means the
    # feed or the symbol mapping is broken. Too little history for a name is a
    # legitimate outcome (a recent listing), not a run failure.
    fetched_ok = [r for r in results if r.status != "fetch_error"]
    evaluated = [r for r in results if r.status == "ok"]
    if not fetched_ok:
        print(
            "ERROR: no symbol could be scanned - every fetch failed. "
            "Check network access and the symbol mapping.",
            file=sys.stderr,
        )
        return 1
    if not evaluated:
        print(
            "WARNING: bars were fetched but no symbol had enough history to score "
            "(need >= 101 bars).",
            file=sys.stderr,
        )
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
