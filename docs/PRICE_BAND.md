# The ₹1,000 – ₹10,000 price band (v16.2)

## Why it needs two mechanisms

Pine Script has no way to enumerate an exchange's symbol list at runtime. There is
no `security_list()`, no `get_all_fno_symbols()`. The only way to look at another
symbol is `request.security("NSE:XYZ", ...)` with the ticker **hard-coded at compile
time**, and you get at most 40 of those per script.

So "give me every F&O stock between ₹1,000 and ₹10,000" can't be answered
dynamically. v16.2 answers it in two layers instead:

| Layer | What it does | What it catches |
| --- | --- | --- |
| **Static curation** | The 40 watchlist slots are filled with F&O names that sit inside the band. | Makes the default list right *today*. |
| **Runtime gate** | Every bar, each symbol's live price is checked against `[min_price, max_price]`. | Keeps it right *tomorrow*, after prices drift. |

Without the second layer the list would silently rot — a ₹1,100 stock that falls to
₹900 would keep firing signals even though it's outside your mandate.

## The gate

```pine
// at the end of f_ironman_eval(), which runs inside request.security()
in_band = (close >= min_price) and (close <= max_price)

[active_name, max_score, trend_txt, in_band, close]
```

`f_ironman_eval()` is passed to `request.security()`, so it executes **in the context
of the requested symbol**. Inside it, `close` is the LTP of e.g. `NSE:TATAELXSI`, not
of whatever chart you dropped the indicator on. That is what makes the gate work.

`min_price` / `max_price` are plain `input.float` values, so they are readable from
inside the security context without any extra plumbing.

## What `in_band` gates

1. **Alert** — always. `any_signal_triggered` requires `(scNN >= 4 and irNN)`.
   An out-of-band name can never fire the alert, in any mode.
2. **Table row** — per the *Out-of-band handling* input.
3. **Signal count** in the title bar (`SIG n`) — out-of-band hits are not counted.

## The three out-of-band modes

Set via **Out-of-band handling** in the indicator settings.

| Mode | Row visible? | Setup cell | Rating cell | Context cell | Sorted to |
| --- | --- | --- | --- | --- | --- |
| `Hide out-of-range rows` (default) | No | — | — | — | — |
| `Blank row (no signal)` | Yes | `OUT OF RANGE` | `-` | `OUT OF BAND` | Bottom (score forced to 0) |
| `Keep score, mark row` | Yes | real setup | real rating | `OUT OF BAND` | Wherever its rating puts it |

- **Hide** is the "strict screener" mode — the table is exactly your tradable universe.
  The table is rebuilt whenever the visible row count changes, so you never get a
  ragged empty grid when names drop in or out.
- **Blank row** keeps the name on screen so you can watch it approach the band from
  below — useful for names like SBIN or JSWSTEEL that hover near ₹1,000.
- **Keep score** is for diagnostics: you see what the engine *would* have said, with
  the orange `OUT OF BAND` tag warning you it doesn't count.

## Changing the band

Just change the two inputs — no regeneration needed, they are runtime inputs:

- `Min Price (₹)` → e.g. `500` if you want to widen downward.
- `Max Price (₹)` → e.g. `5000` to only scan the middle of the band.

The title bar shows the active band (`₹1000 - ₹10000`) and `shown/total`, so you can
always see what filter is live.

If you want a *very* different band (say ₹100–₹500), the curated 40 will mostly fall
outside it and you'll be looking at an empty table. In that case re-curate the CSV:

```bash
# set enabled=0 on the expensive names, enabled=1 on cheap ones
$EDITOR data/fno_watchlist_price_band.csv
python3 tools/build_indicator.py
```

## Known limitations

- **40-symbol ceiling.** Pine allows 40 `request.security()` calls. The watchlist uses
  39 of them (one slot is open — `NSE:LTTS` left the F&O segment in March 2025).
  You cannot add a 41st without removing one.
  The ceiling is a **Pine** limit: the headless scanner and dashboard run the full
  210-name universe from `data/fno_universe.csv`. See [`SCANNER.md`](SCANNER.md).
- **The band is checked on `close`, not on intrabar extremes.** A stock that spends
  the day at ₹1,050 and closes at ₹995 is out of band on that bar. This is deliberate —
  it avoids flip-flopping — but if you want intrabar behaviour, swap `close` for
  `hl2` or `(high + low + close) / 3` in the `in_band` line.
- **F&O membership changes.** NSE adds and removes stocks from the F&O segment
  regularly. A name can drop out of F&O while still sitting in the band. Re-check the
  list when NSE publishes a new F&O circular.
- **Corporate actions.** Splits and bonuses move prices across the band overnight
  (e.g. a 1:5 split takes ₹5,000 to ₹1,000). The runtime gate reacts immediately, but
  the `approx_price` column in the CSV won't — update it when you notice.
