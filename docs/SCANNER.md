# The headless scanner (`ironman_scanner.py`)

## Why it exists

The Pine indicator only runs while you have a TradingView chart open with the
script attached. `.github/workflows/main.yml` was written to close that gap: run
the same watchlist through the same rules on a schedule, and commit the result
back to `results/` so there is a history even when no chart is open.

That workflow invokes `python ironman_scanner.py`. Until now **no such file
existed in the repository** — the workflow, the CSV output paths, the step
summary and the auto-commit were all in place, but the program they call was
never written. Every scheduled run would have died at the scan step.

This document covers what the scanner does, how it relates to the indicator, and
where it deliberately differs.

## Layout

| Path | Role |
| --- | --- |
| `ironman_scanner.py` | CLI entry point — this is what the workflow runs. |
| `ironman/engine.py` | The scoring engine, a bar-by-bar port of `f_ironman_eval()`. |
| `ironman/datafeed.py` | Bar providers: live Yahoo chart data, or a local fixture. |
| `ironman/report.py` | Markdown summary + per-symbol CSV rows. |
| `ironman/watchlist.py` | Loads the watchlist through `tools/build_indicator.py`. |
| `tests/` | Offline test suite and fixtures (no network needed). |

The scanner imports the watchlist loader from `tools/build_indicator.py` rather
than re-parsing the CSV, so the scan and the generated indicator can never
disagree about which symbols are enabled or about the 40-slot Pine ceiling.

## CLI

```
python3 ironman_scanner.py \
  --interval 15m --lookback 2 \
  --low 1000 --high 10000 \
  --workers 6 \
  --csv results/history/2026-10-13.csv \
  --md  results/latest.md
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--interval` | `15m` | Candle size: `1m`, `5m`, `15m`, `30m`, `1h`, `1d`. |
| `--lookback` | `2` | Closed candles in the trigger window (see below). |
| `--low` / `--high` | `1000` / `10000` | Price band, in ₹. |
| `--workers` | `6` | Concurrent fetches. |
| `--csv` | — | Appends one row per symbol; a header is written only if the file is new. |
| `--md` | — | Markdown summary path, or `-` for stdout. |
| `--watchlist` | `data/fno_watchlist_price_band.csv` | Watchlist CSV. |
| `--source` | `yahoo` | `yahoo` for live data, `fixture` for offline runs. |
| `--fixture` | — | Required with `--source fixture`. |
| `--history-range` | per interval | Override the provider's lookback window, e.g. `30d`. |
| `--timeout` | `20` | Per-request timeout, seconds. |
| `--quiet` / `--verbose` | — | Less / more logging. |

Exit codes: **0** normally, **1** when nothing could be fetched at all (a broken
feed or symbol mapping) or when the arguments are invalid.

## The trigger window (`--lookback`)

This is the one idea the scanner adds, because the Pine alert has no equivalent.

In Pine, the alert fires on the bar being evaluated — the last bar, possibly
still forming. The workflow's variable is described as *"Confirmed candles in
trigger window"*, so the scanner treats it as: **look at the last N closed bars;
a symbol triggers if any of them scores ≥ 4 while in band.** It then reports the
freshest such bar and how far back it is:

- `bars_ago = 0` — the trigger is the most recent closed bar: fresh.
- `bars_ago = 1` — the trigger was one closed bar ago: still in the window, but
  the setup is no longer printing.

Only closed bars are ever scored; the still-forming candle is dropped by the data
feed, because a forming bar's score can change before it closes. On the daily
timeframe a bar is treated as closed after **15:30 IST**.

## Running it offline

`--source fixture` reads bars from a JSON file, so the whole pipeline can be run
and tested without network access:

```bash
python3 ironman_scanner.py \
  --source fixture --fixture tests/fixtures/engine.json \
  --watchlist tests/fixtures/watchlist.csv \
  --md - --quiet
```

`tests/fixtures/engine.json` is generated from the synthetic series in
`tests/synth.py`; regenerate it with `python3 tests/make_fixtures.py` after
changing those builders.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

93 tests, no network, no third-party packages: engine rules and Pine
conventions (`ta.ema` seeding, `ta.stdev` population bias, `na` semantics), the
provider parsers against canned payloads, the report renderers, the watchlist
guards, and an end-to-end CLI run using the same flags the workflow passes.

## The data source

Live bars come from the public Yahoo Finance chart endpoint
(`query1.finance.yahoo.com`, falling back to `query2`), converted as
`NSE:HDFCBANK` → `HDFCBANK.NS` and `BSE:...` → `.BO`.

Caveats worth knowing:

- It is an **unofficial endpoint**. A shape change would be caught as fetch
  failures (the run exits non-zero when *every* symbol fails) rather than as a
  silent "0 signals".
- Intraday history is limited: 1-minute bars only go back ~7 days, and
  5m/15m/30m/1h ~60 days. The defaults respect those windows.
- Yahoo prices are not the exchange's official feed. Treat the LTP as
  indicative — the on-chart indicator uses TradingView's data.

## Parity with the indicator

The engine is a deliberate line-by-line port of `f_ironman_eval()`. Where Pine
has a convention that is easy to get wrong, the Python matches Pine exactly and
there is a test for it:

| Pine construct | Ported as |
| --- | --- |
| `ta.ema(x, n)` | Recursive from the first value (not SMA-seeded). |
| `ta.stdev(x, n)` | **Population** stdev (Pine's `biased=true` default). |
| `ta.sma` / `math.sum` / `ta.highest` | `na` until `n` bars exist, then computed over the correct window. |
| `x[1]`, `high[1]`, `ta.highest(high[1], 20)` | Offset lookbacks — note the box window *excludes* the current bar. |
| `na` in a comparison or condition | `None` here, and every comparison helper returns `False`. |
| `var` prior-day high/low via `time("D")` | Rolled over on the calendar date change. |
| Score tie-breaks in `active_name` | Same branch **order** — ties are not reordered. |

### Deliberate deviations

1. **Dead Pine variables are not ported.** `rsi` (template line 46) and
   `neckline_res` / `neckline_sup` (lines 138-143) are assigned in the indicator
   and never read by any rule, the table, or the alert. They are omitted here
   instead of being carried as phantom computation.
2. **The trigger window** described above — a scanner concept, not a Pine one.
3. **Only closed bars** are evaluated; the indicator may score a forming bar.

Everything else — candle anatomy, range expansion and the tier-2/tier-3 split,
Kaufman efficiency-ratio chop, EMA trend and entanglement, volume energy, the
pattern set, EMA proximity, box breakout, the 2A sweep, all four scores and the
≥ 4 threshold — is the same arithmetic in the same order.

### An upstream quirk, reproduced rather than fixed

The tier-3 climax guard ("blow-off bars are excluded from every setup") sits on
the candle-pattern flags — `is_strong_bull`, `is_marubozu_bull` and friends all
carry `and not is_tier3_climax`. But `ema_rejection_bull` / `ema_rejection_bear`
are in the valid-bar list **without** that guard, so a climactic bar sitting on
the EMA can still score 5. This is v16.1 behaviour; the scanner reproduces it
(`tests/test_engine.py::test_climax_exclusion_is_not_universal`) so that the two
implementations agree. Fixing it belongs in the Pine source, where it would
change the indicator too.

## What is *not* verified

The port is covered by unit tests per rule and by the end-to-end fixture run, and
every threshold was read off the Pine source. It has **not** been validated
against TradingView itself: there is no Pine interpreter in CI, and the numbers
have not been diffed name-by-name against a live chart. Before trusting a signal
from this scanner, spot-check that name against the indicator on TradingView.

The scanner is a scanning tool, not investment advice — the same disclaimer as
the indicator applies, and a signal that has not been confirmed on a chart should
be treated as a prompt to look, not a reason to trade.
