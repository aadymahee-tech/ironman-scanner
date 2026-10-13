# ironman-scanner

**IronMan** — a Pine Script v5 TradingView screener for NSE F&O stocks. It watches a
curated watchlist in the background, scores every name against a continuation /
reversal / box-breakout engine, and renders a live sorted table on your chart.

Current build: **v16.2 — ₹1,000 to ₹10,000 price band**.

```
┌────────────┬─────────┬───────┬────────┬──────────────┐
│ IRONMAN    │ ₹1000   │ SIG 3 │ 34/40  │ NSE F&O      │
├────────────┼─────────┼───────┼────────┼──────────────┤
│ Symbol     │ LTP ₹   │ Setup │ Rating │ Context      │
├────────────┼─────────┼───────┼────────┼──────────────┤
│ TATAELXSI  │ 6120.50 │ LONG CONT    │ 5/5   │ BULLISH      │
│ DIVISLAB   │ 5980.00 │ BOX BREAKOUT │ 5/5   │ BULLISH      │
│ BAJAJ-AUTO │ 9120.75 │ SHORT CONT   │ 4/5   │ BEARISH      │
│ ...        │         │              │       │              │
│ HDFCBANK   │ 985.20  │ OUT OF RANGE │ -     │ OUT OF BAND  │
└────────────┴─────────┴───────┴────────┴──────────────┘
```

---

## Repository layout

| Path | What it is |
| --- | --- |
| `indicators/ironman_v16_2_price_band_screener.pine` | **Use this one.** Paste into TradingView Pine Editor. |
| `indicators/archive/ironman_v16_1_original.pine` | Your original v16.1, kept verbatim for diffing. |
| `tools/indicator_template.pine` | Pine template with `@@__TOKEN__@@` placeholders. |
| `tools/build_indicator.py` | Generates the `.pine` file from the CSV. |
| `data/fno_watchlist_price_band.csv` | **The 40-name TradingView watchlist.** This is the file you edit for the indicator. |
| `data/fno_universe.csv` | **The full F&O universe** (210+ names) — what the scanner and dashboard use. |
| `tools/sync_universe.py` | Keeps the universe in step with NSE's published F&O list. |
| `ironman_scanner.py` | **Headless scanner** — the same engine, run from the CLI or on a schedule. |
| `ironman/` | Python port of the engine, the data feed and the report renderer. |
| `tests/` | Offline test suite: no network, no third-party packages. |
| `docs/LOGIC.md` | What every condition in the engine actually means. |
| `docs/PRICE_BAND.md` | How the ₹1,000–10,000 filter works and how to change it. |
| `docs/SCANNER.md` | The headless scanner: usage, parity with Pine, and its limits. |
| `.github/workflows/main.yml` | Scheduled scan; commits `results/` back to the repo. |
| `.github/workflows/tests.yml` | CI: test suite + generated-file drift check. |

---

## Quick start

1. Open TradingView → **Pine Editor** → new blank indicator.
2. Copy the entire contents of `indicators/ironman_v16_2_price_band_screener.pine`.
3. Paste, **Save**, **Add to chart**.
4. In the indicator settings, set the price band (defaults `1000` → `10000`).

The indicator is `overlay=true` and only draws the table — it does not put anything
on your candles, so you can drop it on any chart and any timeframe. It evaluates each
watchlist symbol on **your chart's timeframe** (`timeframe.period`).

---

## What changed in v16.2

You asked for the watchlist to be restricted to stocks trading between
**₹1,000 and ₹10,000**. That is done two ways, because Pine cannot do it with one:

### 1. The watchlist itself was re-curated (static)

The old list was the generic "Top 30 F&O" — heavy on sub-₹1,000 names
(SBIN, ITC, TATASTEEL, ONGC, NTPC, POWERGRID, COALINDIA, TATAMOTORS, TATACHEM,
JSWSTEEL) plus names far above ₹10,000 (MARUTI, ULTRACEMCO, NIFTY).

Those are out. The list is now hand-picked F&O names that sit inside the band,
across six sectors — **39 of the 40 Pine slots are in use**:

| Sector | Count | Names |
| --- | --- | --- |
| Banking & Finance | 8 | HDFCBANK, ICICIBANK, AXISBANK, KOTAKBANK, BAJFINANCE, BAJAJFINSV, SBILIFE, MUTHOOTFIN |
| IT | 9 | TCS, INFY, HCLTECH, TECHM, LTM, PERSISTENT, MPHASIS, TATAELXSI, COFORGE |
| Pharma & Healthcare | 6 | SUNPHARMA, DIVISLAB, DRREDDY, CIPLA, LUPIN, APOLLOHOSP |
| Auto & Ancillaries | 4 | M&M, BAJAJ-AUTO, TVSMOTOR, EICHERMOT |
| FMCG & Consumer | 5 | HINDUNILVR, NESTLEIND, BRITANNIA, TITAN, ASIANPAINT |
| Diversified & Infra | 7 | RELIANCE, LT, ADANIENT, ADANIPORTS, BHARTIARTL, HAL, SIEMENS |

> Two corrections since v16.2: `NSE:LTTS` left the F&O segment on 2025-03-28 and
> is now disabled, and `NSE:LTIM` was renamed by NSE to `NSE:LTM`. That leaves
> one slot open — `tools/sync_universe.py --audit` reports this class of drift.
> See **[Full F&O universe + dashboard](#full-fo-universe--dashboard)** for the
> full 210-name universe, which is what the scanner and dashboard cover.

### 2. A runtime price-band gate was added (dynamic)

Prices move. A stock at ₹1,050 today can be ₹940 after a bad month, and the whole
point of a band is that it should react. So the screener now checks the live price of
every symbol on every bar:

```pine
// inside f_ironman_eval(), i.e. in the context of the requested symbol
in_band = (close >= min_price) and (close <= max_price)
```

`in_band` gates three things:

- **Out-of-band rows** are handled per your *Out-of-band handling* setting
  (hidden / blanked / kept-and-marked).
- **Ratings** on out-of-band names are blanked in the default mode.
- **The alert** now requires `score >= 4 AND in_band` — so an out-of-band name can
  never wake you up.

New inputs (group **Price Band Filter (₹)**):

| Input | Default | Meaning |
| --- | --- | --- |
| `Min Price (₹)` | `1000` | Below this = out of band. |
| `Max Price (₹)` | `10000` | Above this = out of band. |
| `Out-of-band handling` | `Hide out-of-range rows` | `Hide` / `Blank row (no signal)` / `Keep score, mark row`. |
| `Show Price column` | `true` | Adds a live LTP column. |

The trading logic from v16.1 is **otherwise completely untouched**. See
[`docs/LOGIC.md`](docs/LOGIC.md) for a plain-English walkthrough of it.

---

## Changing the watchlist

Do **not** hand-edit the generated `.pine`. Edit the CSV and regenerate:

```bash
# 1. flip enabled=1 / enabled=0 on rows in data/fno_watchlist_price_band.csv
# 2. regenerate
python3 tools/build_indicator.py

# validate without writing anything
python3 tools/build_indicator.py --check
```

Then copy-paste the regenerated file into TradingView again.

### ⚠️ The 40-symbol ceiling

Pine Script v5 allows **40 `request.security()` calls per script**. The watchlist is
capped at 40 (the generator hard-fails above that) and currently uses 39. The CSV
ships those plus ~40 disabled "Reserve" rows — SBIN, JSWSTEEL, TATACHEM (near the
₹1,000 floor, they'll re-qualify if they run), MARUTI, ULTRACEMCO, NIFTY (above the
₹10,000 ceiling), and other in-band names like DMART, TRENT, BSE, CDSL, POLYCAB,
INDIGO, TORNTPHARM, ABB, CUMMINSIND, MCX, KPITTECH.

**This ceiling applies to the indicator only.** The scanner and dashboard run the
full F&O universe with no limit — see
[Full F&O universe + dashboard](#full-fo-universe--dashboard).

> Looking for the open slot? As of the last check, `KPITTECH` (your existing
> reserve row) had fallen to ~₹507, below the ₹1,000 floor, so it would be hidden
> by the band gate. `NAUKRI` (~₹1,211) is in F&O and inside the band if you want
> an IT-sector replacement for LTTS.

To swap a name in you must swap one out — set `enabled=1` on the new row and
`enabled=0` on the row you're dropping. `approx_price` in the CSV is just a
reference note for you; it has **no runtime effect**, the live gate uses actual
market prices.

---

## Full F&O universe + dashboard

The indicator can only carry **40 symbols** — Pine Script v5 caps a script at 40
`request.security()` calls, so a "full F&O list" *indicator* is not possible on
TradingView. The scanner is a Python program, so it has no such limit and runs
over the whole single-stock F&O segment (`data/fno_universe.csv`, 210 names).
The 40-name list stays exactly as it is, for the chart.

```bash
python3 ironman_scanner.py --universe \
  --interval 15m --lookback 2 \
  --low 1000 --high 10000 --workers 8 \
  --csv results/history/$(date -u +%F).csv \
  --md  results/latest.md \
  --html results/dashboard.html
```

`--html` writes a **self-contained dashboard** — inline CSS and vanilla JS, no
CDN, no frameworks — that opens straight from disk or from a workflow artifact:
a header with the scan settings, stat cards, a signals table, and the full
universe as a sortable, filterable table (text search, signals only, in-band
only, sector). Styling matches the indicator's table.

Keeping the universe current — NSE adds, drops and renames F&O names quarterly:

```bash
python3 tools/sync_universe.py --audit          # report drift (and check the 40-name list)
python3 tools/sync_universe.py --audit --write  # apply additions and removals
```

Sync never rewrites the `name`, `sector` or `enabled` value of a name already in
the file, so your curation survives. It also **audits the 40-name watchlist**,
which is how two dead rows were found in this repo: `NSE:LTTS` (out of F&O since
2025-03-28, now disabled) and `NSE:LTIM` (renamed by NSE to `LTM`).

### Headless scan (GitHub Actions)

`.github/workflows/main.yml` runs the scan above every 15 minutes from 08:45 to
15:45 IST on weekdays, plus a closing scan at 15:35 IST, writes the summary to
the Actions step summary, publishes `results/dashboard.html` (committed *and*
uploaded as an artifact), and commits `results/` back to the repo. Adjust it
without touching the workflow by setting repository variables (Settings →
Secrets and variables → Actions → Variables):

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCAN_INTERVAL` | `15m` | Candle size. |
| `SCAN_LOOKBACK` | `2` | Closed candles in the trigger window. |
| `SCAN_BAND_LOW` | `1000` | Price-band floor. |
| `SCAN_BAND_HIGH` | `10000` | Price-band ceiling. |
| `SCAN_WORKERS` | `8` | Concurrent fetches. |
| `SCAN_UNIVERSE` | `universe` | `universe` or `watchlist`. |

A symbol triggers when any bar in the window scores **≥ 4/5 while in band** — the
same conjunction the alert requires. The report shows how fresh each trigger is:
`latest closed bar` means it has just confirmed, `n bar(s) ago` means it is
stale but still inside the window.

Run it offline (no network) against the committed fixtures:

```bash
python3 ironman_scanner.py --source fixture --fixture tests/fixtures/engine.json \
  --watchlist tests/fixtures/watchlist.csv --md - --quiet
```

It uses only the standard library, takes its bars from the public Yahoo Finance
chart endpoint, and **never scores the still-forming candle** — a bar has to
close before it can trigger.

> The scanner has not been diffed against TradingView itself. Spot-check a
> signal on a chart before acting on it.

### Tests

```bash
python3 -m unittest discover -s tests -t .
```

153 tests covering the engine rules, the Pine conventions they depend on
(`ta.ema` seeding, `ta.stdev` population bias, `na` semantics), the provider
parsers, the universe sync and audit, the dashboard, the report renderers, the
watchlist guards, and end-to-end CLI runs. CI
([`tests.yml`](.github/workflows/tests.yml)) runs them on every push and PR,
fails if `indicators/` drifts from the watchlist CSV, and smoke-tests the
dashboard for self-containment. A separate
[`feed-check.yml`](.github/workflows/feed-check.yml) probes the live provider
across all 210 symbols on every PR (report-only) and uploads a live dashboard
artifact.

---

## Alerts

The script exposes one alert condition:

> **IronMan Watchlist Alert (₹1k-10k)** — *"IronMan Signal Triggered on Watchlist!"*

It fires when **any** in-band watchlist symbol scores **≥ 4/5** on a closed or
forming bar. It is a watchlist-level alert, so it won't tell you *which* stock
fired — check the table (top row is always the highest score).

---

## Reading the table

| Column | Meaning |
| --- | --- |
| **Symbol** | Watchlist name, `NSE:` prefix stripped. Greyed out = outside the price band. |
| **LTP ₹** | Live last traded price of that symbol. Amber = out of band. |
| **Setup** | `BOX BREAKOUT`, `BOX BREAKDOWN`, `LONG/SHORT CONT`, `LONG/SHORT 2A SWEEP`, or `None`. |
| **Rating** | `0/5` … `5/5`. Only **≥ 4** counts as an actionable signal. |
| **Context** | Regime: `BULLISH`, `BEARISH`, `EXT BULL`, `EXT BEAR`, `CHOP`, `SIDEWAYS`, or `OUT OF BAND`. |

Rows are always sorted **by rating, descending**, so live signals float to the top.

---

## Disclaimer

This is a scanning/visualisation tool, not investment advice. Signals are derived
from price action statistics on historical bars and will produce false positives,
especially around events and in `CHOP` regimes. Backtest and forward-test before
risking capital. Verify the `approx_price` column and F&O membership against the
current NSE F&O list before trading — both change over time.
