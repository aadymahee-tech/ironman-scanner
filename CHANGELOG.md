# Changelog

All notable changes to the IronMan screener.

## Unreleased — headless scanner + CI

### Fixed
- **`.github/workflows/main.yml` called a program that did not exist.**
  The scheduled scan ran `python ironman_scanner.py`, but no such file was ever
  committed — only `tools/build_indicator.py` existed. The workflow, the
  `results/` output paths, the step summary and the auto-commit step were all in
  place; the scanner they invoke was missing, so every scheduled run died at the
  scan step. `ironman_scanner.py` is now implemented.

### Added
- `ironman_scanner.py` — headless scanner CLI, invoked by the workflow with the
  flags it already passed (`--interval --lookback --low --high --workers --csv
  --md`). Standard library only, so no `pip install` step is needed.
- `ironman/` — the engine (`engine.py`), bar providers (`datafeed.py`), report
  rendering (`report.py`) and watchlist loading (`watchlist.py`). The engine is a
  bar-by-bar port of `f_ironman_eval()`; `docs/SCANNER.md` documents the parity
  rules and the deliberate deviations.
- `tests/` — 93 offline tests (no network, no third-party packages) plus a
  synthetic series module and a committed fixture.
- `.github/workflows/tests.yml` — CI running the test suite, a `--check` pass on
  the generator, a generated-file drift check on `indicators/`, and an offline
  scanner smoke test.
- `docs/SCANNER.md` — usage, parity table, trigger-window semantics, and an
  explicit list of what is *not* verified.
- Price-band gating in the scan: an out-of-band name can be scored but can never
  trigger, matching `score >= 4 AND in_band` in the alert.

### Notes
- The scanner scores **closed bars only**; the still-forming candle is dropped by
  the feed. The Pine indicator may score a forming bar.
- `--lookback` ("confirmed candles in trigger window") is a scanner concept with
  no Pine equivalent: a symbol triggers if any of the last N closed bars scored
  ≥ 4 while in band, and the report says how fresh that trigger is.
- Two upstream quirks are reproduced rather than fixed, so the scanner and the
  indicator keep agreeing: a tier-3 climax bar can still score through the
  `ema_rejection_bull` / `ema_rejection_bear` paths, and a tight box reads as
  `CHOP` (which is why the box-breakout rule short-circuits the trend test).
- Dead Pine variables (`rsi`, `neckline_res`, `neckline_sup`) are computed in the
  indicator and never read; they are not ported.
- The port is unit-tested per rule but has **not** been diffed against
  TradingView. It lives on the public Yahoo chart endpoint, not TradingView data.

### Not changed
- The indicator itself. `python3 tools/build_indicator.py` regenerates
  `indicators/ironman_v16_2_price_band_screener.pine` with no diff.

## v16.2 — Price band ₹1,000 to ₹10,000

### Added
- **Runtime price-band gate.** `f_ironman_eval()` now returns `in_band`, computed as
  `close >= min_price and close <= max_price` inside the requested symbol's context.
- New input group **Price Band Filter (₹)**: `Min Price` (default `1000`),
  `Max Price` (default `10000`), `Out-of-band handling`, `Show Price column`.
- Out-of-band handling modes: `Hide out-of-range rows`, `Blank row (no signal)`,
  `Keep score, mark row`.
- **LTP column** in the table, so you can see where each name sits inside the band.
- Title row showing the active band (`₹1000 - ₹10000`), live signal count (`SIG n`),
  and `shown/total`.
- Table is rebuilt dynamically when the visible row count changes (rows no longer
  leave an empty grid behind when names are hidden).
- `data/fno_watchlist_price_band.csv` — the watchlist as data, with 40 enabled and
  ~40 disabled reserve rows.
- `tools/build_indicator.py` + `tools/indicator_template.pine` — generate the Pine
  script from the CSV instead of hand-editing 250 lines of boilerplate.
- Generator lint: fails on duplicate/malformed symbols, >40 enabled rows,
  unsubstituted template tokens, and structural mismatches in the output.
- `docs/LOGIC.md`, `docs/PRICE_BAND.md`.

### Changed
- Watchlist expanded 30 → **40** slots and re-curated to the ₹1,000–10,000 band.
  Removed: SBIN, ITC, TATAMOTORS, TATASTEEL, ULTRACEMCO, NTPC, POWERGRID,
  COALINDIA, ONGC, JSWSTEEL, TATACHEM, NIFTY (outside the band).
  Added: HCLTECH, TECHM, PERSISTENT, MPHASIS, LTTS, TATAELXSI, COFORGE, BAJAJFINSV,
  SBILIFE, MUTHOOTFIN, DIVISLAB, DRREDDY, CIPLA, LUPIN, APOLLOHOSP, NESTLEIND,
  BRITANNIA, ASIANPAINT, BAJAJ-AUTO, TVSMOTOR, EICHERMOT, HAL, SIEMENS.
- Alert condition now requires `score >= 4` **AND** `in_band`. Previously the alert
  fired for any name scoring ≥ 4 regardless of price.
- Alert renamed to *"IronMan Watchlist Alert (₹1k-10k)"*.
- Table widened from 4 to 5 columns and gained a title row.
- Array construction switched from single 30-arg `array.from()` calls to chunked
  `array.from()` + `array.concat()`, to stay under `array.from()`'s max arity at 40
  symbols.
- Table variable renamed `sc` → `tbl` (the old name shadowed the `scNN` score
  variables' prefix and was confusing).

### Not changed
- All candle, range-expansion, chop, trend, volume, box-breakout and 2A-sweep logic
  from v16.1 is byte-for-byte identical. See `indicators/archive/ironman_v16_1_original.pine`.
- The ≥ 4/5 actionable threshold.

### Known limitations
- 40 `request.security()` calls is a hard Pine limit; the watchlist uses all 40.
- The band is evaluated on `close`, not intrabar extremes.
- `approx_price` in the CSV is a reference note only — it has no runtime effect.
