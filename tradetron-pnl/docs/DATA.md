# Data model

Everything lives in plain CSV / JSON under `data/` and is committed back to the
repository by the workflow after every run. Files are append-only or
idempotently rewritten, Unix newlines, UTF-8, ISO-8601 timestamps with
offsets, so they diff cleanly in Git and open directly in pandas, DuckDB,
Excel or Google Sheets.

```
data/
  snapshots/YYYY-MM.csv       every observation of every deployment (append-only, one file per month)
  ledger.csv                  one row per trading day × deployment (rewritten, sorted)
  positions/YYYY-MM-DD.jsonl  raw positions payload per deployment per run (append-only)
  deployments_seen.csv        registry of every deployment id ever seen
  state.json                  small bookkeeping for the bot (what was sent when)
reports/
  latest.md  dashboard.html  latest.json  tax/FY2026-27.md  tax/FY2026-27.csv
```

## `data/snapshots/YYYY-MM.csv` — raw observations

One row per deployment per run. Never edited after the fact; `rebuild`
recomputes the ledger from these.

| column | meaning |
|---|---|
| `ts_utc`, `ts_ist` | observation time |
| `trading_day` | IST calendar date of the run |
| `phase` | `pre_open` (before 09:15), `open`, `post_close` (after 15:30), `closed` (weekend/holiday with `--force`) |
| `run_kind` | `preopen`, `hourly`, `eod`, `silent` |
| `deployment_id`, `name`, `deployment_type`, `status`, `broker` | as returned by the API |
| `multiplier`, `capital` | as returned (2 dp) |
| `pnl` | the API's P&L figure as-is (2 dp); meaning depends on `TTPNL_PNL_SEMANTICS` |
| `pnl_known` | `0` when the record carried no P&L field (value stored as 0) |
| `open_positions`, `total_positions` | from the deployment record, or from the positions call when available |
| `currency` | `INR`, `USD`, … |
| `paper` | `1` for `LIVE OFFLINE` / paper deployments |
| `deployed_on` | deployment date as reported |
| `source` | `api` or `fixture` |

## `data/ledger.csv` — the analysis table

Derived from the snapshots once per run (`update_ledger_for_day`) and fully
recomputable with `python ttpnl.py rebuild`. This is the table analytics and
tax workings read.

| column | meaning |
|---|---|
| `trading_day`, `deployment_id`, `name`, `deployment_type`, `broker`, `currency`, `paper` | identity |
| `day_pnl` | P&L attributed to the day (see below) |
| `close_pnl` | last observed `pnl` of the day |
| `baseline_pnl` | the value `day_pnl` was measured against |
| `baseline_source` | `preopen` · `prev_close` · `new_deployment` · `partial` · `today` · `import` |
| `snapshots` | number of observations that day |
| `first_ts_ist`, `last_ts_ist` | observation window |
| `status_eod` | last status seen |
| `source` | `api` · `fixture` · `import` |
| `notes` | free text, e.g. `pre-open 0.00 != previous close 4250.50 (reset or redeploy)`, `first snapshot was mid-session; day P&L may be understated` |

### How `day_pnl` is derived (`TTPNL_PNL_SEMANTICS=cumulative`)

```
baseline = pre-open snapshot of the day            (baseline_source = preopen)
         | else previous trading day's close_pnl    (prev_close; looks back ≤ 10 calendar days across holidays)
         | else 0 when deployed_on == today         (new_deployment)
         | else first observation of the day        (partial — the figure is a lower bound, marked ˣ in Telegram)
day_pnl  = close_pnl − baseline
```

If the pre-open value differs from the previous close (counter reset after a
redeploy, overnight MTM move) a note is written; the day is still measured
from the pre-open value so the jump never lands in `day_pnl`. A redeploy
*during* the session does reset the counter mid-day — check `notes` and the
snapshots if a day looks wrong, and fix the row by hand or re-import it. With
`TTPNL_PNL_SEMANTICS=today`, `day_pnl = close_pnl` and `baseline_source = today`.

Only `pnl_known = 1` snapshots feed the ledger. Paper rows are kept (`paper = 1`)
but excluded from every total unless a report is asked to include them.

### Quick analysis

```python
import pandas as pd
led = pd.read_csv("data/ledger.csv", parse_dates=["trading_day"])
live = led[led.paper == 0]
daily = live.groupby("trading_day").day_pnl.sum()
print(daily.cumsum().tail())
print(live.pivot_table(index="trading_day", columns="name", values="day_pnl", aggfunc="sum").tail())
```

```sql
-- duckdb
select strftime(trading_day, '%Y-%m') as month, sum(day_pnl) as pnl, count(distinct trading_day) as days
from 'data/ledger.csv' where paper = 0 group by 1 order by 1;
```

Intraday curves come from the snapshots:

```python
snap = pd.read_csv("data/snapshots/2026-10.csv", parse_dates=["ts_ist"])
snap[snap.deployment_id == 1001].set_index("ts_ist").pnl.plot()
```

## `data/positions/YYYY-MM-DD.jsonl`

One JSON object per deployment per run with the **raw** positions payload plus
a small `summary` (`count`, `net_quantity`, `pnl`, `symbols`) and timestamps.
`positions_hidden: true` means the account hides positions — unknown, not flat.
Kept raw on purpose because the record schema is the least verified part of
the API.

## `data/deployments_seen.csv`

Registry of every deployment id ever observed: `first_seen`, `last_seen`,
`last_status`, `deployed_on`, broker, currency. Useful for tagging new
deployments in `config/deployments.csv` and for noticing silently stopped ones.

## `data/state.json`

```json
{
  "last_eod_day": "2026-10-16", "last_weekly_day": "2026-10-16",
  "last_monthly_day": "2026-09-30", "last_quarter_day": "2026-09-30",
  "error_alerted": {"2026-10-16": [1003]},
  "fetch_failure_alert_day": "2026-10-12",
  "last_run_ist": "2026-10-16T15:40:00+05:30", "last_kind": "eod", "last_source": "api",
  "last_error": {"when": "...", "code": "AUTH", "message": "..."}
}
```

Dry runs do not update the delivery keys (so a dry run never suppresses a real
message later). Delete the file to re-send today's EOD.

## Importing history

Day-wise P&L you already have (Tradetron report exports, your own sheet) can be
loaded once:

```bash
python ttpnl.py import history.csv --date-col Date --pnl-col "Net P&L" --name-col Strategy [--id-col ID] [--currency INR]
```

Rows become `source = import` ledger rows; dates accept `YYYY-MM-DD`,
`DD-MM-YYYY`, `DD/MM/YYYY`, `DD-Mon-YYYY`, and amounts may contain `₹`, commas
and parentheses. Without an id column a stable negative id is derived from the
name so re-imports update rather than duplicate. `rebuild` keeps imported rows
unless `--drop-imported` is given.

## Retention and size

About 20 bytes per field × ~20 fields × deployments × 7 runs a day: a handful
of deployments produce well under 1 MB per year of snapshots. Monthly files keep
each commit diff small. Nothing is ever deleted automatically.

## Privacy

`data/` is your trading record. Keep the repository private. The API key and
Telegram token never touch the data files (`redact()` scrubs the key from
error messages too), but strategy names, brokers, capital and P&L are all
there in clear text.
