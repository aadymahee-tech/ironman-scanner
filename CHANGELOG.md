# Changelog

All notable changes to the IronMan screener.

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
