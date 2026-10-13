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
| `data/fno_watchlist_price_band.csv` | **The watchlist.** This is the file you edit. |
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

Those are out. The list is now 40 hand-picked F&O names that sit inside the band,
across six sectors:

| Sector | Count | Names |
| --- | --- | --- |
| Banking & Finance | 8 | HDFCBANK, ICICIBANK, AXISBANK, KOTAKBANK, BAJFINANCE, BAJAJFINSV, SBILIFE, MUTHOOTFIN |
| IT | 10 | TCS, INFY, HCLTECH, TECHM, LTIM, PERSISTENT, MPHASIS, LTTS, TATAELXSI, COFORGE |
| Pharma & Healthcare | 6 | SUNPHARMA, DIVISLAB, DRREDDY, CIPLA, LUPIN, APOLLOHOSP |
| Auto & Ancillaries | 4 | M_M, BAJAJ-AUTO, TVSMOTOR, EICHERMOT |
| FMCG & Consumer | 5 | HINDUNILVR, NESTLEIND, BRITANNIA, TITAN, ASIANPAINT |
| Diversified & Infra | 7 | RELIANCE, LT, ADANIENT, ADANIPORTS, BHARTIARTL, HAL, SIEMENS |

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
capped at exactly 40 (and the generator hard-fails above that). The CSV ships 40
enabled rows plus ~40 disabled "Reserve" rows — SBIN, JSWSTEEL, TATACHEM (near the
₹1,000 floor, they'll re-qualify if they run), MARUTI, ULTRACEMCO, NIFTY (above the
₹10,000 ceiling), and other in-band names like DMART, TRENT, BSE, CDSL, POLYCAB,
INDIGO, TORNTPHARM, ABB, CUMMINSIND, MCX.

To swap a name in you must swap one out — set `enabled=1` on the new row and
`enabled=0` on the row you're dropping. `approx_price` in the CSV is just a
reference note for you; it has **no runtime effect**, the live gate uses actual
market prices.

---

## Headless scan (GitHub Actions)

The indicator needs a chart open in your browser. `ironman_scanner.py` runs the
same watchlist through the same engine from the command line, so a scheduled job
can scan whether or not anyone is looking at a chart. See
[`docs/SCANNER.md`](docs/SCANNER.md) for the full detail.

```bash
python3 ironman_scanner.py \
  --interval 15m --lookback 2 \
  --low 1000 --high 10000 \
  --csv results/history/$(date -u +%F).csv \
  --md  results/latest.md
```

`.github/workflows/main.yml` runs exactly that every 15 minutes from 08:45 to
15:45 IST on weekdays, plus a closing scan at 15:35 IST, writes the summary to
the Actions step summary, and commits `results/` back to the repo. Adjust it
without touching the workflow by setting repository variables (Settings →
Secrets and variables → Actions → Variables):

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCAN_INTERVAL` | `15m` | Candle size. |
| `SCAN_LOOKBACK` | `2` | Closed candles in the trigger window. |
| `SCAN_BAND_LOW` | `1000` | Price-band floor. |
| `SCAN_BAND_HIGH` | `10000` | Price-band ceiling. |

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

93 tests covering the engine rules, the Pine conventions they depend on
(`ta.ema` seeding, `ta.stdev` population bias, `na` semantics), the provider
parsers, the report renderers, the watchlist guards, and an end-to-end CLI run.
CI ([`tests.yml`](.github/workflows/tests.yml)) runs them on every push and PR,
and also fails if `indicators/` is out of sync with the watchlist CSV.

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
