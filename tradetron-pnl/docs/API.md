# Tradetron Developer API — what this tool calls and assumes

There is no public reference for the Tradetron **Developer API** at the time
of writing. What follows was pieced together from the account UI (Tradetron →
Developer API, key with granular read permissions) and from an open-source
integration that was exercised against a live account in October 2026. Treat it
as *observed*, not *guaranteed*; every assumption below has a configuration
escape hatch and the `probe` command exists to show you the raw truth for your
account.

## Not to be confused with the "Tradetron API"

The long-standing "Tradetron API" you may have seen in the help centre
(`GET https://api.tradetron.tech/api?auth-token=<uuid>&key=<k>&value=<v>`) is
the *signal ingest* endpoint used to feed conditions into a strategy. It cannot
read deployments or P&L and is not used here.

## Requests made

| Call | Purpose | When |
|---|---|---|
| `GET {base}/me/deployments[?page=N]` | all deployments of the account, paginated | every run |
| `GET {base}/me/deployments/{id}/positions` | open legs for one deployment | every run, live non-finished deployments, at most `TTPNL_MAX_POSITIONS` (default 25), paced 0.25 s apart |
| `GET {base}/me/notifications` | platform notifications | `probe --notifications` only |

* `base` = `TRADETRON_API_BASE_URL`, default `https://tradetron.tech/api/v1`.
* Headers: `Authorization: Bearer <key>`, `Accept: application/json`,
  `User-Agent: tradetron-pnl/<version>`.
* Paths are overridable: `TRADETRON_DEPLOYMENTS_PATH`,
  `TRADETRON_POSITIONS_PATH` (must contain `{id}`),
  `TRADETRON_NOTIFICATIONS_PATH`.

Per run that is 1 + (number of live deployments) requests, at most 7 runs a
day. Tradetron documents a per-key rate limit without stating the number; the
client honours `Retry-After` on 429 and otherwise backs off 0.8 s then 1.6 s
(two retries, three attempts). Set `TTPNL_FETCH_POSITIONS=0` to reduce it to
one request per run.

## Response shape

Envelope:

```json
{ "success": true, "data": { ... } }
```

`success: false` or a missing `data` member is treated as an error, except that
a bare object carrying `deployments` / `positions` is accepted as-is.

### `/me/deployments`

```json
{
  "page": 1, "last_page": 1, "total": 2,
  "deployments": [
    {
      "deployment_id": 1001,
      "name": "Nifty Weekly Straddle",
      "deployment_type": "LIVE AUTO",
      "status": "Live-Entered",
      "broker": "Zerodha",
      "multiplier": 1,
      "capital": 300000,
      "pnl": 4250.5,
      "open_positions": 4,
      "total_positions": 6,
      "currency_code": "INR",
      "deployed_on": "2026-10-01"
    }
  ]
}
```

Pagination follows `last_page` (also accepts `meta.last_page` / `total_pages`);
more than 20 pages is treated as a loop and refused.

Field names are matched through alias lists (`tradetron_pnl/api.py`,
`FIELD_ALIASES`), so `id`, `strategy_name`, `total_pnl`, `currency`, … also
work. Add to the lists if your payload differs.

Derived flags:

| Flag | Rule |
|---|---|
| paper | `deployment_type` contains `OFFLINE`, `PAPER` or `SIMULAT` (e.g. `LIVE OFFLINE`) — stored, shown as "not counted", excluded from totals and tax |
| in error | `status` starts with `Error` (e.g. `Error-Execution`) — triggers one 🚨 alert per deployment per day |
| finished | `status` starts with `Exited` / `Paused` / `Blocked` / `Stopped` / `Completed` — kept in the ledger for the day, no positions call |
| `pnl_known` | `0` when the record has no P&L field at all (value stored as 0 and flagged) |

### `/me/deployments/{id}/positions`

```json
{
  "deployment_id": 1001,
  "as_of": "2026-10-16T05:30:12Z",
  "positions_hidden": false,
  "positions": [
    { "instrument_symbol": "NIFTY26O2125800CE", "net_quantity": -150, "pnl": 2100.25, "ltp": 41.2, "average_price": 55.2 }
  ]
}
```

`positions_hidden: true` means the account hides positions for that deployment
— this is *unknown*, not zero, and is stored as such. The individual position
record schema is the least verified part; raw JSON is kept in
`data/positions/YYYY-MM-DD.jsonl` and only `symbol`, `quantity`, `pnl` are
extracted through `POSITION_ALIASES`.

### Errors

| HTTP | Handling |
|---|---|
| 401 / 403 | `AUTH` — not retried; message never contains the key; check the key's read permissions |
| 404 | `NOT_FOUND` — usually a wrong base URL / path; the error says so |
| 429 | `RATE_LIMITED` — waits `Retry-After` (else the normal backoff) then retries |
| 5xx, network, timeout | retried with exponential backoff, then `SERVER_ERROR` / `NETWORK_ERROR` |
| non-JSON body | `INVALID_JSON` |

A fetch failure ends the run with exit code 1, records `last_error` in
`data/state.json`, and sends one ⛔ message per day.

## What `pnl` means — `TTPNL_PNL_SEMANTICS`

Tradetron's Deployed page shows two numbers per deployment: "Expiry/MTM"
(cumulative since deployment) and "Today". The API exposes one `pnl`. Choose:

* `cumulative` (default): today's P&L = latest `pnl` − baseline, where the
  baseline is, in order of preference, the 09:00 pre-open snapshot of the same
  day (`preopen`), the previous trading day's close (`prev_close`, looking back
  up to 10 calendar days across holidays), zero for a deployment whose
  `deployed_on` is today (`new_deployment`), or else the first observation of
  the day (`partial` — a lower bound). When the pre-open value differs from
  the previous close (counter reset after a redeploy, or an overnight MTM
  move) a note is written to the ledger; the day is still measured from the
  pre-open value, so the jump never lands in `day_pnl`.
* `today`: `pnl` is already the intraday figure and is used directly; the
  close value is carried as `close_pnl`.

Run `python ttpnl.py probe` and compare `pnl` with the Deployed page to decide.
The ledger records `baseline_source` per row so a wrong choice can be
corrected later with `python ttpnl.py rebuild` after changing the setting.

## Capturing your real payload as a fixture

```bash
python ttpnl.py probe --positions 3 --notifications --save-fixture tests/fixtures/mine.json
```

The saved file has the layout the `FixtureClient` reads
(`{"deployments": {...}, "positions": {"<id>": {...}}, "notifications": [...]}`)
and can be used with `run --source fixture --fixture tests/fixtures/mine.json`
for offline experiments. The API key is never written to it; still, review the
file before committing it, since it contains your strategy names and P&L.
