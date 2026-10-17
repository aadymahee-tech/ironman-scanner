# tradetron-pnl

Fetches the P&L of your own Tradetron deployments through the **Tradetron
Developer API** (read-only API key), keeps an append-only history in this
repository for analysis and Indian tax workings, and pushes **hourly updates
during market hours plus an end-of-day summary** to Telegram — all from a
scheduled GitHub Actions workflow. No server, no database, no third-party
packages: Python standard library only.

```
 GitHub Actions (cron, IST market hours)
      │
      ▼
 ttpnl.py run ──► Tradetron Developer API  (GET /me/deployments, /me/deployments/{id}/positions)
      │
      ├─► data/snapshots/YYYY-MM.csv   every observation, never rewritten
      ├─► data/ledger.csv              one row per trading day × deployment (day P&L, close P&L)
      ├─► data/positions/YYYY-MM-DD.jsonl, data/deployments_seen.csv, data/state.json
      ├─► reports/latest.md, dashboard.html, latest.json, tax/FY2026-27.{md,csv}
      └─► Telegram: 🌅 pre-open (optional) · 📊 hourly · 🧾 EOD · 📅 weekly · 🗓 monthly · 📆 quarter · 🚨 alerts
```

> **Private data.** The repository stores your trading results. Keep it
> **private** and do not enable GitHub Pages for it.

## What you get

| Telegram | When (IST, NSE trading days) | Contents |
|---|---|---|
| 🚨 Deployment error | any run, once per deployment per day | deployments whose status starts with `Error` |
| 📊 Hourly | 10:00 … 15:00 | today's P&L, change since last update, per-deployment lines, open positions, MTD / FYTD |
| 🧾 End of day | 15:40 | day P&L, per-deployment table, week / month / FY-to-date, streak, drawdown, profit factor, advance-tax reminder from 3 weeks before each due date (hourly: last week) |
| 📅 Weekly digest | last trading day of the week, with EOD | daily bars for the week, best / worst day, win rate |
| 🗓 Monthly digest | last trading day of the month | calendar of daily P&L, top / bottom deployments |
| 📆 Quarter close | last trading day of Jun / Sep / Dec / Mar | FY quarter totals and the matching advance-tax instalment |
| ⛔ Fetch failure | once per day when Tradetron cannot be read | error code, status, hint |

Everything is also written to `reports/`:

* `reports/latest.md` — today's table, month-to-date, FY-to-date, per-deployment statistics (also appended to the Actions job summary).
* `reports/dashboard.html` — a single self-contained page (inline CSS + SVG equity curve, no external requests).
* `reports/latest.json` — machine-readable summary.
* `reports/tax/FY2026-27.md` / `.csv` — financial-year workings by tax head and quarter, with the advance-tax schedule. See [docs/TAX.md](docs/TAX.md).

## Setup (about 15 minutes)

### 1. Tradetron Developer API key

In your Tradetron account open **Developer API** and create a key with
**read** permissions for deployments, positions/P&L and (optionally)
notifications. Copy it once; it is shown once. Details and the exact requests
this tool makes are in [docs/API.md](docs/API.md).

This is **not** the older "Tradetron API" (`api.tradetron.tech/api?auth-token=…`)
used to *send signals* to strategies — that one cannot read P&L.

### 2. Telegram bot

1. Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
2. Start a chat with your bot (or add it to a group / channel) and get the chat id.
   [docs/TELEGRAM.md](docs/TELEGRAM.md) shows the two-line way to do that.

### 3. Repository secrets and variables

`Settings → Secrets and variables → Actions`:

| Secret | Value |
|---|---|
| `TRADETRON_API_KEY` | the Developer API key |
| `TELEGRAM_BOT_TOKEN` | from BotFather |
| `TELEGRAM_CHAT_ID` | your chat / group / channel id |

Optional **Variables** (not secrets):

| Variable | Default | Meaning |
|---|---|---|
| `TTPNL_PNL_SEMANTICS` | `cumulative` | What the API's `pnl` means for your account: `cumulative` (since deployment; the tool derives today's P&L from the pre-open baseline) or `today` (already intraday). See *Verifying the P&L semantics* below. |
| `TTPNL_DEFAULT_SEGMENT` | `fno` | Tax head for deployments not listed in `config/deployments.csv`. |
| `TTPNL_FETCH_POSITIONS` | `1` | Also call the positions endpoint for each live deployment (open-position counts and a daily positions log). |
| `TTPNL_MAX_POSITIONS` | `25` | Upper bound on positions calls per run (rate-limit guard). |
| `TTPNL_PREOPEN_MESSAGE` | `0` | Send a short message at the 09:00 baseline run. |
| `TTPNL_SILENT_HOURLY` | `0` | Deliver hourly updates without a notification sound (EOD, digests and alerts stay loud). |
| `TTPNL_CURRENCY` | `INR` | Reporting currency. Deployments in other currencies are listed separately and never summed in. |
| `TTPNL_TAX_RATE` | unset | e.g. `0.30` — adds an indicative tax figure to the tax report. |
| `TRADETRON_API_BASE_URL` | `https://tradetron.tech/api/v1` | Only if your Developer API page shows something else. |

### 4. Verify, then let the schedule run

Actions → **Tradetron P&L** → *Run workflow* with `dry_run = true`. The job
fetches your deployments, writes `data/` and `reports/`, and prints the
Telegram messages in the log instead of sending them. Run once more with
`dry_run = false` to receive the first message. From then on the cron
schedule in [`.github/workflows/pnl.yml`](.github/workflows/pnl.yml) takes over:

```
30 3-9 * * 1-5   → 09:00 (silent baseline), 10:00 … 15:00 IST
10 10 * * 1-5    → 15:40 IST end of day (+ weekly / monthly / quarterly digests when due)
```

Weekends and NSE holidays (`config/holidays_nse.csv`) are skipped
automatically. **Add next year's holidays** when NSE publishes them — the tool
warns in the log when the file has no entries for the current year.

### Verifying the P&L semantics (do this once)

Tradetron's Deployed page shows both an "Expiry/MTM" figure (cumulative since
deployment) and a "Today" figure. Which one the Developer API's `pnl`
corresponds to is not documented, so the tool is configurable:

```bash
python ttpnl.py probe            # prints the raw deployments payload (key redacted)
```

Compare the `pnl` values with the Deployed page. If they match the cumulative /
MTM figure, keep the default. If they match "Today", set the repository
variable `TTPNL_PNL_SEMANTICS=today`. With the default, today's P&L is the
difference between the latest value and the 09:00 pre-open baseline (or the
previous day's close for deployments first seen later in the day); a P&L
reset after a redeploy is detected and noted in the ledger.

Also note that Tradetron P&L is **before brokerage, exchange charges and
taxes**. Your broker's statements remain the authoritative source for returns.

## Tagging deployments (`config/deployments.csv`)

Alias, tax segment and inclusion are per deployment id:

```csv
deployment_id,alias,segment,include,notes
1001,Nifty Straddle,fno,1,
1002,BN Condor,fno,1,
2005,Intraday Cash,intraday_equity,1,speculative head
3001,,fno,0,friend's strategy - exclude from totals
```

Segments: `fno`, `currency`, `commodity` (non-speculative business income),
`intraday_equity` (speculative business income), `delivery_equity` (capital
gains — flagged, needs holding periods), `crypto` (VDA, flagged), `other`.
Paper deployments (`LIVE OFFLINE`) are stored but excluded from totals and tax
automatically.

## Local use

```bash
cp .env.example .env            # fill in the key and Telegram credentials; .env is git-ignored
python ttpnl.py probe --positions 2 --save-fixture tests/fixtures/mine.json
python ttpnl.py run --dry-run --verbose
python ttpnl.py run --source fixture --fixture tests/fixtures/deployments.json --now 2026-10-16T12:00 --dry-run
python ttpnl.py report                        # rebuild reports/ from data/
python ttpnl.py tax --fy FY2026-27 --rate 0.30 --stdout
python ttpnl.py import old_pnl.csv --date-col Date --pnl-col "Net P&L" --name-col Strategy
python ttpnl.py rebuild                       # recompute ledger.csv from snapshots (keeps imported rows)
python ttpnl.py send --text "hello from tradetron-pnl"
python -m unittest discover -s tests -t .     # 150+ offline tests
```

Exit codes: `0` ok · `1` Tradetron fetch failed · `2` configuration / usage ·
`3` data saved but Telegram delivery failed. The workflow commits `data/` and
`reports/` on every exit code.

## Layout

```
ttpnl.py                      CLI entry point (also: python -m tradetron_pnl)
tradetron_pnl/
  api.py                      Developer API client, retries, pagination, normalisation, fixture client
  trading_calendar.py         IST clock, NSE sessions, holidays, FY / quarter / advance-tax dates
  config.py                   environment settings, deployment tags
  store.py                    CSV / JSONL persistence (snapshots, ledger, positions, registry, state)
  ledger.py                   day P&L derivation from snapshots, rebuild, CSV import
  analytics.py                period statistics, drawdown, streaks, per-deployment and monthly tables
  tax.py                      FY / quarter workings by tax head, advance-tax schedule, markdown + CSV
  telegram.py                 Bot API sendMessage with chunking, rate-limit and HTML fallbacks
  messages.py                 all Telegram message templates
  reports.py                  markdown report and self-contained HTML dashboard
  cli.py                      run / probe / report / tax / send / import / rebuild
config/holidays_nse.csv       NSE trading holidays (extend yearly)
config/deployments.csv        alias / tax segment / include per deployment
data/                         committed by the workflow — see docs/DATA.md
reports/                      committed by the workflow
tests/                        unittest suite + tests/fixtures/deployments.json
docs/API.md DATA.md TAX.md TELEGRAM.md
```

## Caveats

* The Developer API is documented here from observed behaviour, not an official
  reference; field names are matched through alias lists and the endpoint paths
  and base URL are configurable (`docs/API.md`). `probe --save-fixture` captures
  your real payload shape for the offline tests.
* Hourly sampling is a lower bound on intraday activity; the tax report's
  turnover estimate is per day × deployment and is **not** a substitute for the
  trade-level turnover your CA computes from broker contract notes.
* Nothing here is tax advice. Rules change; verify with a professional.
