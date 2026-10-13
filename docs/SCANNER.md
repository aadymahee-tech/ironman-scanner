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

## The full F&O universe

The Pine indicator carries 40 symbols because **Pine Script v5 caps a script at
40 `request.security()` calls**. That is a platform limit — no amount of
generator work gets around it, and a "full F&O list" indicator is therefore
impossible on TradingView.

The scanner is a Python program, so it has no such ceiling. It runs over the
whole single-stock F&O segment:

```bash
python3 ironman_scanner.py --universe --interval 15m --lookback 2 \
  --low 1000 --high 10000 --workers 8 \
  --csv results/history/$(date -u +%F).csv \
  --md results/latest.md \
  --html results/dashboard.html
```

| Flag | Effect |
| --- | --- |
| *(none)* / `--watchlist FILE` | The 40-name price-band list in `data/fno_watchlist_price_band.csv`. |
| `--universe` | Every symbol in `data/fno_universe.csv` — the full segment. |

`data/fno_universe.csv` holds `symbol,name,sector,enabled` for each underlying.
It is seeded from NSE's own `fo_mktlots.csv` and kept current by
`tools/sync_universe.py`:

```bash
python3 tools/sync_universe.py                 # diff against live NSE, report only
python3 tools/sync_universe.py --write         # apply additions and removals
python3 tools/sync_universe.py --audit         # also check the 40-name watchlist
python3 tools/sync_universe.py --from FILE     # use a local copy of the NSE file
python3 tools/sync_universe.py --check         # exit 1 if out of date (for CI)
```

Sync rules, so re-running it is never destructive:

- **Adds** symbols NSE has added. Name comes from the curated metadata in the
  tool when there is one, otherwise from NSE's own underlying text; sector
  defaults to `Unknown`.
- **Removes** symbols NSE has dropped.
- **Never rewrites** the name, sector or `enabled` flag of a symbol already in
  the file, so hand-curated values survive every re-sync.

NSE blocks some datacentre ranges, which is what `--from` is for. The scheduled
workflow treats a refresh as best effort: the scan runs with the committed
universe whether or not NSE answered.

### Keeping the hand-curated 40 in step

`--audit` cross-checks the enabled names in the 40-name price-band watchlist
against the current F&O segment and reports any that are no longer tradeable:

```
watchlist audit : 1 enabled name(s) NOT in the F&O segment: LTTS
```

That check is what caught two dead rows in this repo — see the CHANGELOG:
`NSE:LTTS` (out of F&O since 2025-03-28) and `NSE:LTIM` (renamed to `LTM`).

## The dashboard

`--html PATH` writes a single self-contained page: inline CSS and a little
vanilla JS, no CDN, no fonts, no frameworks. It works from a `file://` path, as
a workflow artifact, or committed to `results/`.

It contains the scan header (interval, lookback, band, signal count, coverage),
stat cards, a signals table sorted by score and freshness, and the full universe
as a sortable/filterable table — filter by text, signals only, in-band only, or
sector, and click any header to sort. Rows are coloured like the indicator's
table: green for long setups, red for short, amber for out-of-range names.

The scan payload is also embedded as JSON in a `<script type="application/json">`
tag, so the page can be re-rendered or audited without re-running a scan:

```bash
python3 - <<'PY'
import json, re, html
page = open("results/dashboard.html", encoding="utf-8").read()
data = json.loads(html.unescape(re.search(r'id="scan-data">(.*?)</script>', page, re.S).group(1)))
print(data["counts"], data["symbols"][0])
PY
```

Sector labels are a convenience grouping for the interface, not an official NSE
classification. Prices in the page come from whatever feed the scan used.

### Publishing it as a live URL

GitHub serves a committed `.html` file as source text, so the scheduled workflow
also publishes the dashboard to GitHub Pages. Enable it once under
**Settings → Pages → Source: GitHub Actions**; after that the dashboard is live at

```
https://aadymahee-tech.github.io/ironman-scanner/
```

and is republished on every scan. The publish step checks whether Pages is
enabled before doing anything: if it is not, the scan run stays green and emits a
warning telling you to switch it on, so a missing setting can never be mistaken
for a broken scan.

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
