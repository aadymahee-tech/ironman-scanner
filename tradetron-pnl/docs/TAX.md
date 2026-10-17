# Tax workings (India)

`python ttpnl.py tax [--fy FY2026-27] [--as-of DATE] [--rate 0.30] [--stdout]`
writes `reports/tax/FY2026-27.md` and `.csv` from `data/ledger.csv`. The
workflow refreshes them on every run. **This is bookkeeping support, not tax
advice** — the rules below are summarised as commonly applied to individual
retail traders; verify with a chartered accountant before filing.

## Financial year and quarters

| | Period |
|---|---|
| FY2026-27 | 1 Apr 2026 – 31 Mar 2027 (assessment year 2027-28) |
| Q1 / Q2 / Q3 / Q4 | Apr–Jun / Jul–Sep / Oct–Dec / Jan–Mar |

The report shows net P&L, gross profit, gross loss, trading days and
cumulative FY P&L per quarter, plus month and per-deployment tables.

## Heads of income by segment

The Developer API does not say what a deployment trades, so the head is taken
from `segment` in `config/deployments.csv` (default `TTPNL_DEFAULT_SEGMENT`,
`fno`).

| `segment` | Head | Basis | Loss treatment |
|---|---|---|---|
| `fno` | Non-speculative business income (PGBP) | Sec 43(5)(d): eligible equity/index derivative transactions on a recognised stock exchange are not speculative | set off against any head except salary; carry forward 8 years (return filed by the due date) |
| `currency` | Non-speculative business income | Sec 43(5)(d), currency derivatives | as above |
| `commodity` | Non-speculative business income | Sec 43(5)(e), commodity derivatives on a recognised association | as above |
| `intraday_equity` | **Speculative** business income | Sec 43(5): cash-segment trades settled without delivery | set off only against speculative income; carry forward 4 years |
| `delivery_equity` | Capital gains | needs holding periods (STCG/LTCG) that a P&L feed cannot give | **flagged** — use broker's capital-gains statement |
| `crypto` | Virtual digital assets, Sec 115BBH | 30% flat, no expense deduction, no set-off or carry-forward of losses, 1% TDS under 194S | **flagged** |
| `other` | — | reported separately | — |

Paper / `LIVE OFFLINE` deployments are excluded automatically; `include=0`
excludes a deployment from everything.

## Turnover (for the audit question)

For derivatives the ICAI *Guidance Note on Tax Audit* treats **turnover as the
sum of absolute values of favourable and unfavourable differences** (plus
option premium received, on the sale side, depending on the edition of the
guidance and how the broker reports it). The report computes
`est. turnover = Σ |day_pnl|` per deployment per trading day — the same idea at
**day grain**, which is a **lower bound**: several trades inside a day net
against each other before this tool sees them. Your broker's tax P&L report
(trade-level) is the number to use with your CA.

Why it matters: Sec 44AB audit thresholds (₹10 crore when ≥95% of receipts
and payments are digital, otherwise ₹1 crore), the Sec 44AD presumptive
option, and the "declared profit below 6%/8% with income above the basic
exemption" audit trap all key off turnover.

## Advance tax

Individuals with a tax liability above ₹10,000 pay advance tax (Sec 211):

| Instalment | Due | Cumulative share of the year's liability |
|---|---|---|
| 1st | 15 June | 15% |
| 2nd | 15 September | 45% |
| 3rd | 15 December | 75% |
| 4th | 15 March | 100% |

Shortfalls attract interest under Sec 234B/234C. Presumptive (44AD) filers may
pay 100% by 15 March. The report lists FY-to-date P&L at each due date, the
EOD Telegram message reminds you from three weeks before a due date, and with
`--rate` / `TTPNL_TAX_RATE` an *indicative* cumulative figure is shown
(`fytd × rate × instalment%` on positive FYTD only — it ignores slabs, other
income, deductions and losses brought forward).

## What the numbers do and do not include

* Tradetron's deployment P&L is **before brokerage, exchange transaction
  charges, SEBI fees, STT/CTT, stamp duty and GST**. All of these are
  deductible business expenses for the business heads; your broker's ledger
  has them.
* Other deductible expenses (data/platform subscriptions, Tradetron fees,
  internet, advisory) are not tracked here.
* Hourly sampling can miss intraday round trips; day-level P&L is unaffected
  if the deployment's cumulative counter is correct, but turnover is a lower
  bound.
* Deployments in currencies other than the reporting currency are listed
  separately and never converted or summed.

## Output files

* `reports/tax/FY2026-27.md` — human-readable workings with notes.
* `reports/tax/FY2026-27.csv` — `trading_day, fy, quarter, deployment_id,
  deployment, segment, head, currency, paper, included, day_pnl,
  cumulative_fy` — one row per ledger row, ready for a pivot table or to hand
  over to your CA.
