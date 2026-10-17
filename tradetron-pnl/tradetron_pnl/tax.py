"""Indian financial-year tax workings from the daily ledger.

What this produces
------------------
* net P&L per FY quarter (Apr-Jun, Jul-Sep, Oct-Dec, Jan-Mar) and per month;
* a split by **head of income**, driven by the segment tag of each deployment
  (``config/deployments.csv``): F&O / currency / commodity derivatives are
  non-speculative business income, intraday cash equity is speculative business
  income, delivery equity is capital gains, crypto is VDA income;
* gross profit, gross loss and an **estimated turnover** (absolute sum of
  results — the ICAI guidance-note method applied at day × deployment grain,
  which is a floor for the trade-level figure the auditor will compute);
* the advance-tax calendar with the FY-to-date P&L at each due date, and an
  optional indicative liability at a flat effective rate;
* a day × deployment CSV for a chartered accountant.

What it does not do
-------------------
It does not compute your tax. Tradetron's P&L is *before* brokerage, exchange
charges, STT and GST, and a deployment-level figure cannot be split into
per-trade legs. Use the broker's tax P&L statement for filing and treat this as
the running management view. See ``docs/TAX.md``.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Iterable, List, Mapping, Optional

from .analytics import PeriodStats, by_month, daily_totals, period_stats
from .config import DeploymentTag, tag_for
from .store import LedgerRow
from .trading_calendar import (
    AdvanceTaxDue, advance_tax_schedule, fy_bounds, fy_label, fy_quarter, quarter_bounds,
)

HEADS: Dict[str, tuple] = {
    "fno": ("Non-speculative business income",
            "Sec 43(5)(d): equity/index F&O on a recognised exchange is not speculative"),
    "currency": ("Non-speculative business income", "Sec 43(5)(d): currency derivatives"),
    "commodity": ("Non-speculative business income", "Sec 43(5)(e): commodity derivatives on a recognised association"),
    "intraday_equity": ("Speculative business income",
                        "Sec 43(5): cash-segment trades squared off without delivery"),
    "delivery_equity": ("Capital gains",
                        "STCG/LTCG by holding period — needs trade-level dates from the broker"),
    "crypto": ("Income from virtual digital assets",
               "Sec 115BBH: 30% flat, no expense deduction, no loss set-off or carry-forward"),
    "other": ("Unclassified", "tag this deployment in config/deployments.csv"),
}


@dataclass
class HeadTotals:
    segment: str
    head: str
    basis: str
    net: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    turnover_estimate: float = 0.0
    days: int = 0
    deployments: List[str] = field(default_factory=list)


@dataclass
class QuarterTotals:
    quarter: int
    start: date
    end: date
    stats: PeriodStats
    cumulative: float


@dataclass
class AdvanceTaxLine:
    due: AdvanceTaxDue
    fytd_net: float
    indicative_cumulative_tax: Optional[float]
    status: str                      # "paid-by" | "upcoming" | "today"


@dataclass
class TaxReport:
    fy: str
    start: date
    end: date
    as_of: date
    currency: str
    overall: PeriodStats
    quarters: List[QuarterTotals]
    months: list
    heads: List[HeadTotals]
    deployments: list
    advance_tax: List[AdvanceTaxLine]
    paper_excluded: int
    excluded_ids: List[int]
    other_currencies: Dict[str, float]
    rate: Optional[float]
    notes: List[str]


def build_tax_report(
    ledger: Iterable[LedgerRow],
    fy: str,
    tags: Mapping[int, DeploymentTag],
    *,
    as_of: Optional[date] = None,
    currency: str = "INR",
    default_segment: str = "fno",
    rate: Optional[float] = None,
) -> TaxReport:
    start, end = fy_bounds(fy)
    as_of = as_of or end
    rows = [r for r in ledger if start <= r.trading_day <= end]

    paper_ids = {r.deployment_id for r in rows if r.paper}
    excluded_ids = sorted({
        r.deployment_id for r in rows
        if not tag_for(r.deployment_id, tags, default_segment).include
    })
    other_currencies: Dict[str, float] = defaultdict(float)
    counted: List[LedgerRow] = []
    for r in rows:
        if r.paper or r.deployment_id in excluded_ids:
            continue
        if r.currency != currency:
            other_currencies[r.currency] += r.day_pnl
            continue
        counted.append(r)

    daily = daily_totals(counted)
    overall = period_stats(daily, start, end)

    quarters: List[QuarterTotals] = []
    running = 0.0
    for q in (1, 2, 3, 4):
        q_start, q_end = quarter_bounds(fy, q)
        stats = period_stats(daily, q_start, q_end)
        running += stats.net
        quarters.append(QuarterTotals(q, q_start, q_end, stats, round(running, 2)))

    heads: Dict[str, HeadTotals] = {}
    per_dep_day: Dict[tuple, float] = defaultdict(float)
    dep_names: Dict[int, str] = {}
    for r in counted:
        per_dep_day[(r.deployment_id, r.trading_day)] += r.day_pnl
        dep_names[r.deployment_id] = tag_for(r.deployment_id, tags, default_segment).alias or r.name
    for (dep_id, _day), value in per_dep_day.items():
        segment = tag_for(dep_id, tags, default_segment).segment
        head_name, basis = HEADS.get(segment, HEADS["other"])
        bucket = heads.setdefault(segment, HeadTotals(segment, head_name, basis))
        bucket.net += value
        bucket.days += 1
        bucket.turnover_estimate += abs(value)
        if value > 0:
            bucket.gross_profit += value
        else:
            bucket.gross_loss += value
        label = dep_names.get(dep_id) or f"Deployment {dep_id}"
        if label not in bucket.deployments:
            bucket.deployments.append(label)
    for bucket in heads.values():
        bucket.net = round(bucket.net, 2)
        bucket.gross_profit = round(bucket.gross_profit, 2)
        bucket.gross_loss = round(bucket.gross_loss, 2)
        bucket.turnover_estimate = round(bucket.turnover_estimate, 2)
        bucket.deployments.sort()

    from .analytics import by_deployment  # local import keeps module graph simple
    deployments = by_deployment(counted, tags, default_segment)

    advance: List[AdvanceTaxLine] = []
    for due in advance_tax_schedule(fy):
        fytd = round(sum(v for d, v in daily if d <= min(due.due, as_of)), 2)
        indicative = None
        if rate is not None and fytd > 0:
            indicative = round(fytd * rate * due.cumulative_pct / 100.0, 2)
        if due.due < as_of:
            status = "past"
        elif due.due == as_of:
            status = "today"
        else:
            status = "upcoming"
        advance.append(AdvanceTaxLine(due, fytd, indicative, status))

    notes = [
        "Figures are Tradetron deployment P&L: before brokerage, exchange/SEBI charges, STT, stamp duty and GST.",
        "Turnover is estimated as the absolute sum of day-wise results per deployment (ICAI guidance-note method at "
        "day grain). The trade-level turnover your auditor computes from contract notes will be equal or higher.",
        "Paper / Live-Offline deployments are excluded automatically; deployments with include=0 are excluded.",
        "Non-speculative business loss can be carried forward 8 years and set off against any head except salary; "
        "speculative loss 4 years, only against speculative income — both require filing by the due date.",
        "Advance tax: individuals pay 15% / 45% / 75% / 100% of the year's estimated liability by 15 Jun / 15 Sep / "
        "15 Dec / 15 Mar (Sec 211); interest under 234B/234C applies to shortfalls. Presumptive (44AD) filers pay 100% by 15 Mar.",
        "Verify everything against the broker's tax P&L statement with a chartered accountant before filing.",
    ]
    if rate is not None:
        notes.insert(0, f"Indicative tax uses a flat effective rate of {rate:.0%} on positive FY-to-date P&L only; "
                        "it ignores slabs, other income, deductions, cess and losses brought forward.")

    return TaxReport(
        fy=fy, start=start, end=end, as_of=as_of, currency=currency,
        overall=overall, quarters=quarters, months=by_month(daily),
        heads=sorted(heads.values(), key=lambda h: h.segment),
        deployments=deployments, advance_tax=advance,
        paper_excluded=len(paper_ids), excluded_ids=excluded_ids,
        other_currencies={k: round(v, 2) for k, v in sorted(other_currencies.items())},
        rate=rate, notes=notes,
    )


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #
def _money(value: Optional[float], currency: str) -> str:
    from .messages import fmt_money
    return "—" if value is None else fmt_money(value, currency, sign=True, decimals=0, html=False)


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def render_tax_markdown(report: TaxReport) -> str:
    cur = report.currency
    o = report.overall
    lines: List[str] = []
    lines.append(f"# Tax workings — {report.fy} (as of {report.as_of:%d %b %Y})")
    lines.append("")
    lines.append(f"**Net P&L {_money(o.net, cur)}** · gross profit {_money(o.gross_profit, cur)} · "
                 f"gross loss {_money(o.gross_loss, cur)} · {o.trading_days} trading days · "
                 f"win rate {_pct(o.win_rate)} · est. turnover {_money(o.turnover_estimate, cur)}")
    lines.append("")

    lines.append("## Quarters")
    lines.append("")
    lines.append("| Quarter | Period | Days | Net P&L | Gross profit | Gross loss | Cumulative |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for q in report.quarters:
        s = q.stats
        lines.append(f"| Q{q.quarter} | {q.start:%d %b} – {q.end:%d %b %Y} | {s.trading_days} | "
                     f"{_money(s.net, cur)} | {_money(s.gross_profit, cur)} | {_money(s.gross_loss, cur)} | "
                     f"{_money(q.cumulative, cur)} |")
    lines.append("")

    lines.append("## Heads of income")
    lines.append("")
    if report.heads:
        lines.append("| Segment | Head | Net P&L | Gross profit | Gross loss | Est. turnover | Deployments |")
        lines.append("| --- | --- | ---: | ---: | ---: | ---: | --- |")
        for h in report.heads:
            lines.append(f"| `{h.segment}` | {h.head} | {_money(h.net, cur)} | {_money(h.gross_profit, cur)} | "
                         f"{_money(h.gross_loss, cur)} | {_money(h.turnover_estimate, cur)} | "
                         f"{', '.join(h.deployments)} |")
        lines.append("")
        for h in report.heads:
            lines.append(f"- **{h.segment}** — {h.basis}.")
    else:
        lines.append("_No ledger rows in this financial year yet._")
    lines.append("")

    lines.append("## Months")
    lines.append("")
    lines.append("| Month | Days | Win rate | Net P&L |")
    lines.append("| --- | ---: | ---: | ---: |")
    for m in report.months:
        lines.append(f"| {m.month} | {m.days} | {_pct(m.win_rate)} | {_money(m.net, cur)} |")
    lines.append("")

    lines.append("## Deployments")
    lines.append("")
    lines.append("| Deployment | Segment | Days | Win rate | Best day | Worst day | Net P&L |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for d in report.deployments:
        lines.append(f"| {d.label} (`{d.deployment_id}`) | {d.segment} | {d.days} | {_pct(d.win_rate)} | "
                     f"{_money(d.best, cur)} | {_money(d.worst, cur)} | {_money(d.net, cur)} |")
    lines.append("")

    lines.append("## Advance tax calendar")
    lines.append("")
    header = "| Instalment | Due | Cumulative % | FY-to-date P&L at due date |"
    sep = "| --- | --- | ---: | ---: |"
    if report.rate is not None:
        header += " Indicative cumulative tax |"
        sep += " ---: |"
    lines.append(header)
    lines.append(sep)
    for a in report.advance_tax:
        marker = {"past": "", "today": " ← today", "upcoming": " (upcoming)"}[a.status]
        row = f"| {a.due.ordinal} | {a.due.due:%d %b %Y}{marker} | {a.due.cumulative_pct}% | {_money(a.fytd_net, cur)} |"
        if report.rate is not None:
            row += f" {_money(a.indicative_cumulative_tax, cur)} |"
        lines.append(row)
    lines.append("")

    if report.paper_excluded or report.excluded_ids or report.other_currencies:
        lines.append("## Exclusions")
        lines.append("")
        if report.paper_excluded:
            lines.append(f"- {report.paper_excluded} paper / Live-Offline deployment(s) excluded.")
        if report.excluded_ids:
            lines.append(f"- Excluded by config (`include=0`): {', '.join(map(str, report.excluded_ids))}.")
        for code, value in report.other_currencies.items():
            lines.append(f"- {code} P&L not included in the {cur} totals: {value:,.2f} {code}.")
        lines.append("")

    lines.append("## Notes")
    lines.append("")
    for note in report.notes:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


TAX_CSV_COLUMNS = [
    "trading_day", "fy", "quarter", "deployment_id", "deployment", "segment", "head",
    "currency", "paper", "included", "day_pnl", "cumulative_fy",
]


def tax_csv(ledger: Iterable[LedgerRow], fy: str, tags: Mapping[int, DeploymentTag], *,
            currency: str = "INR", default_segment: str = "fno") -> str:
    """Day × deployment rows for the FY, with a running FY total of the counted rows."""
    start, end = fy_bounds(fy)
    rows = sorted((r for r in ledger if start <= r.trading_day <= end),
                  key=lambda r: (r.trading_day, r.deployment_id))
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=TAX_CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    running = 0.0
    for r in rows:
        tag = tag_for(r.deployment_id, tags, default_segment)
        included = tag.include and not r.paper and r.currency == currency
        if included:
            running += r.day_pnl
        writer.writerow({
            "trading_day": r.trading_day.isoformat(),
            "fy": fy_label(r.trading_day),
            "quarter": f"Q{fy_quarter(r.trading_day)}",
            "deployment_id": r.deployment_id,
            "deployment": tag.alias or r.name,
            "segment": tag.segment,
            "head": HEADS.get(tag.segment, HEADS["other"])[0],
            "currency": r.currency,
            "paper": int(r.paper),
            "included": int(included),
            "day_pnl": f"{r.day_pnl:.2f}",
            "cumulative_fy": f"{running:.2f}" if included else "",
        })
    return buf.getvalue()


__all__ = ["HEADS", "TaxReport", "build_tax_report", "render_tax_markdown", "tax_csv"]
