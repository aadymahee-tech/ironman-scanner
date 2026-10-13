#!/usr/bin/env python3
"""
Generate the IronMan F&O screener Pine Script from the curated watchlist CSV.

Why a generator?
----------------
Pine Script v5 has three hard limits that make hand-editing this indicator painful:

  1. Max 40 `request.security()` calls per script -> the watchlist is capped at 40.
  2. `array.from()` has a maximum arity -> 40-element arrays must be chunked + `array.concat`.
  3. Symbols cannot be enumerated at runtime -> the list must be hard-coded, and
     hand-writing 40 inputs x 5 places (input, fetch, unpack, array, alert) is ~250
     lines of pure boilerplate that drifts out of sync the moment you swap a stock.

So: edit `data/fno_watchlist_price_band.csv`, run this, commit the generated .pine.

Usage
-----
    python3 tools/build_indicator.py                  # write indicators/ironman_v16_2_price_band_screener.pine
    python3 tools/build_indicator.py --check          # validate config only, write nothing
    python3 tools/build_indicator.py --data other.csv --out /tmp/out.pine
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "data" / "fno_watchlist_price_band.csv"
DEFAULT_TEMPLATE = ROOT / "tools" / "indicator_template.pine"
DEFAULT_OUT = ROOT / "indicators" / "ironman_v16_2_price_band_screener.pine"

MAX_SECURITY_CALLS = 40   # Pine Script v5 hard limit
ARRAY_FROM_CHUNK = 10     # keep every array.from() call well under its max arity

SYMBOL_RE = re.compile(r"^[A-Z0-9_.\-]+:[A-Z0-9_.\-]+$")


# --------------------------------------------------------------------------- #
# Watchlist
# --------------------------------------------------------------------------- #
def load_watchlist(path: Path) -> list[dict]:
    if not path.exists():
        sys.exit(f"ERROR: watchlist not found: {path}")

    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    if not rows:
        sys.exit(f"ERROR: {path} is empty")

    required = {"symbol", "name", "sector", "approx_price", "enabled"}
    missing = required - set(rows[0].keys())
    if missing:
        sys.exit(f"ERROR: {path} missing columns: {sorted(missing)}")

    enabled = [r for r in rows if r["enabled"].strip() == "1"]

    # --- validation -------------------------------------------------------- #
    seen: set[str] = set()
    for r in enabled:
        sym = r["symbol"].strip()
        if not SYMBOL_RE.match(sym):
            sys.exit(f"ERROR: bad symbol {sym!r} - expected EXCHANGE:TICKER (e.g. NSE:RELIANCE)")
        if sym in seen:
            sys.exit(f"ERROR: duplicate symbol {sym}")
        seen.add(sym)

    if len(enabled) > MAX_SECURITY_CALLS:
        sys.exit(
            f"ERROR: {len(enabled)} symbols enabled but Pine allows at most "
            f"{MAX_SECURITY_CALLS} request.security() calls. Set enabled=0 on "
            f"{len(enabled) - MAX_SECURITY_CALLS} row(s)."
        )

    # Sort for a stable, reviewable diff: sector order preserved from CSV, A-Z inside.
    order = {s: i for i, s in enumerate(dict.fromkeys(r["sector"].strip() for r in rows))}
    enabled.sort(key=lambda r: (order[r["sector"].strip()], r["symbol"].strip()))
    return enabled


# --------------------------------------------------------------------------- #
# Code emission
# --------------------------------------------------------------------------- #
def chunked(items: list[str], prefix: str, indent: str = "    ", size: int = ARRAY_FROM_CHUNK) -> str:
    """Build `array.from(a1..a10)` (+ `array.concat` for any further chunks).

    Every emitted line is indented, because these statements live inside the
    `if barstate.islast` block of the generated script.
    """
    chunks = [items[i : i + size] for i in range(0, len(items), size)]
    if not chunks:
        return f"{indent}{prefix} = array.new_string()"

    first = f"array.from({', '.join(chunks[0])})"
    lines = [f"{prefix} = {first}"]
    for ch in chunks[1:]:
        lines.append(f"{prefix} := array.concat({prefix}, array.from({', '.join(ch)}))")
    return "\n".join(indent + line for line in lines)


def build_symbol_inputs(symbols: list[dict]) -> str:
    return "\n".join(
        f's{i:02d} = input.symbol("{r["symbol"]}", "Stock {i:02d}  -  {r["name"]}", group=grp_list)'
        for i, r in enumerate(symbols, start=1)
    )


def build_fetch_calls(symbols: list[dict]) -> str:
    return "\n".join(
        f"[act{i:02d}, sc{i:02d}, tr{i:02d}, ir{i:02d}, px{i:02d}] = get_data(s{i:02d})"
        for i in range(1, len(symbols) + 1)
    )


def build_alert_expr(symbols: list[dict]) -> str:
    terms = [f"(sc{i:02d} >= 4 and ir{i:02d})" for i in range(1, len(symbols) + 1)]
    lines, per = [], 4
    for i in range(0, len(terms), per):
        lines.append(" or ".join(terms[i : i + per]))
    return " or\n     ".join(lines)


def render(symbols: list[dict], template: str) -> str:
    n = len(symbols)
    idx = [f"{i:02d}" for i in range(1, n + 1)]

    replacements = {
        "@@__COUNT__@@": str(n),
        "@@__SYMBOL_INPUTS__@@": build_symbol_inputs(symbols),
        "@@__FETCH_CALLS__@@": build_fetch_calls(symbols),
        "@@__ALERT_EXPR__@@": build_alert_expr(symbols),
        "@@__SYMS_ARRAY__@@": chunked([f"s{i}" for i in idx], "syms"),
        "@@__SCRS_ARRAY__@@": chunked([f"sc{i}" for i in idx], "scrs"),
        "@@__ACTS_ARRAY__@@": chunked([f"act{i}" for i in idx], "acts"),
        "@@__TRNS_ARRAY__@@": chunked([f"tr{i}" for i in idx], "trns"),
        "@@__PRCS_ARRAY__@@": chunked([f"px{i}" for i in idx], "prcs"),
        "@@__INRG_ARRAY__@@": chunked([f"ir{i}" for i in idx], "inrg"),
    }

    out = template
    for token, value in replacements.items():
        out = out.replace(token, value)

    leftover = re.findall(r"@@__[A-Z_]+__@@", out)
    if leftover:
        sys.exit(f"ERROR: unsubstituted template tokens: {sorted(set(leftover))}")
    return out


def sanity_check(pine: str, n: int) -> None:
    """Cheap lint so obvious breakage never reaches a commit."""
    problems = []
    if pine.count("request.security(") != 1:
        problems.append("expected exactly one request.security definition (get_data)")
    if pine.count("get_data(s") != n + 1:  # +1 for the definition line
        problems.append(f"expected {n + 1} get_data occurrences, found {pine.count('get_data(s')}")
    if pine.count("input.symbol(") != n:
        problems.append(f"expected {n} input.symbol calls, found {pine.count('input.symbol(')}")
    if not pine.startswith("//@version=5"):
        problems.append("generated file must start with //@version=5")
    if "//@version=5" in pine[12:]:
        problems.append("//@version=5 must appear only on line 1")
    for token in ("table.new", "array.sort_indices", "alertcondition"):
        if token not in pine:
            problems.append(f"missing expected construct: {token}")
    if problems:
        sys.exit("ERROR: generated script failed sanity check:\n  - " + "\n  - ".join(problems))


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA, help="watchlist CSV")
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE, help="Pine template")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output .pine file")
    ap.add_argument("--check", action="store_true", help="validate only, do not write")
    args = ap.parse_args()

    symbols = load_watchlist(args.data)
    template = args.template.read_text(encoding="utf-8")
    pine = render(symbols, template)
    sanity_check(pine, len(symbols))

    print(f"watchlist      : {args.data.relative_to(ROOT)}")
    print(f"enabled symbols: {len(symbols)} / {MAX_SECURITY_CALLS} request.security slots")
    print(f"generated lines: {len(pine.splitlines())}")

    if args.check:
        print("check: OK (no files written)")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(pine, encoding="utf-8")
    print(f"written        : {args.out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
