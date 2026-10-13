#!/usr/bin/env python3
"""Regenerate ``tests/fixtures/engine.json`` from the synthetic series.

The fixture is committed so the CLI can be exercised offline with
``--source fixture``. Regenerate it after changing anything in ``synth.py``:

    python3 tests/make_fixtures.py

Bar rows are ``[epoch_seconds, open, high, low, close, volume]``, which is the
shape ``ironman.datafeed.load_fixture`` accepts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests import synth  # noqa: E402

OUT = Path(__file__).resolve().parent / "fixtures" / "engine.json"

SERIES = {
    "NSE:TATAELXSI": synth.box_breakout_long,          # 5/5 BOX BREAKOUT, in band
    "NSE:HAL": synth.sweep_long_reversal,              # 5/5 LONG 2A SWEEP, in band
    "NSE:INFY": synth.chop_in_band,                    # CHOP, in band, no signal
    "NSE:HDFCBANK": synth.quiet_out_of_band_low,       # out of band (below floor)
}


def main() -> int:
    symbols = {}
    for symbol, maker in SERIES.items():
        bars = maker()
        symbols[symbol] = {
            "bars": [
                [int(b.ts.timestamp()), b.open, b.high, b.low, b.close, b.volume] for b in bars
            ]
        }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps({"symbols": symbols}, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    counts = ", ".join(f"{s}={len(v['bars'])}" for s, v in symbols.items())
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size} bytes)")
    print(f"  {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
