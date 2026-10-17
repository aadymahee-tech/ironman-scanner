"""Telegram message rendering (HTML parse mode) and money formatting.

The view model (``RunView``) is assembled by the CLI from the API payload, the
ledger and the analytics; every renderer here is a pure function of it, so the
exact text of each update is unit-testable without a network.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence

from .analytics import DeploymentStats, MonthStats, PeriodStats
from .api import Deployment
from .config import DeploymentTag
from .telegram import escape
from .trading_calendar import AdvanceTaxDue, iso_week_label, quarter_label

CURRENCY_SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def group_indian(number: int) -> str:
    """12345678 -> '1,23,45,678'."""
    text = str(abs(int(number)))
    if len(text) <= 3:
        return text
    head, tail = text[:-3], text[-3:]
    parts: List[str] = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def fmt_money(value: Optional[float], currency: str = "INR", *, sign: bool = False,
              decimals: int = 0, html: bool = False) -> str:
    if value is None:
        return "—"
    negative = value < 0
    magnitude = abs(value)
    if decimals == 0:
        magnitude = math.floor(magnitude + 0.5)          # half-up, not banker's rounding
        if magnitude == 0:
            negative = False                              # never print "-₹0"
    if currency == "INR":
        whole = int(magnitude)
        body = group_indian(whole)
        if decimals:
            body += f"{magnitude - whole:.{decimals}f}"[1:]
    else:
        body = f"{magnitude:,.{decimals}f}"
    symbol = CURRENCY_SYMBOLS.get(currency)
    text = f"{symbol}{body}" if symbol else f"{body} {currency}"
    if negative:
        text = "-" + text
    elif sign and value > 0:
        text = "+" + text
    return escape(text) if html else text


def arrow(value: float) -> str:
    if value > 0:
        return "▲"
    if value < 0:
        return "▼"
    return "•"


def pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def short(text: str, width: int = 22) -> str:
    text = text.strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def fmt_stamp(moment: datetime) -> str:
    return moment.strftime("%H:%M IST")


def fmt_day(day: date) -> str:
    return day.strftime("%a %d %b %Y")


# --------------------------------------------------------------------------- #
# View model
# --------------------------------------------------------------------------- #
@dataclass
class DeploymentView:
    deployment: Deployment
    tag: DeploymentTag
    day_pnl: float
    baseline_source: str = ""
    counted: bool = True
    hour_delta: Optional[float] = None
    positions: Optional[dict] = None

    @property
    def label(self) -> str:
        return self.tag.alias or self.deployment.label

    @property
    def currency(self) -> str:
        return self.deployment.currency


@dataclass
class RunView:
    now: datetime
    day: date
    phase: str
    kind: str
    fy: str
    currency: str
    source: str
    deployments: List[DeploymentView] = field(default_factory=list)
    today_total: float = 0.0
    hour_delta: Optional[float] = None
    paper_total: Optional[float] = None
    other_currency_totals: Dict[str, float] = field(default_factory=dict)
    open_positions: int = 0
    week: PeriodStats = field(default_factory=PeriodStats)
    mtd: PeriodStats = field(default_factory=PeriodStats)
    quarter: PeriodStats = field(default_factory=PeriodStats)
    fytd: PeriodStats = field(default_factory=PeriodStats)
    month_deployments: List[DeploymentStats] = field(default_factory=list)
    months: List[MonthStats] = field(default_factory=list)
    next_due: Optional[AdvanceTaxDue] = None
    new_errors: List[DeploymentView] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def counted(self) -> List[DeploymentView]:
        return [d for d in self.deployments if d.counted]

    @property
    def errors(self) -> List[DeploymentView]:
        return [d for d in self.deployments if d.deployment.in_error]

    @property
    def active(self) -> List[DeploymentView]:
        return [d for d in self.deployments if not d.deployment.finished and not d.deployment.paper]

    @property
    def paper(self) -> List[DeploymentView]:
        return [d for d in self.deployments if d.deployment.paper]


# --------------------------------------------------------------------------- #
# Building blocks
# --------------------------------------------------------------------------- #
def _status_tag(dep: Deployment) -> str:
    status = dep.status or "—"
    if dep.in_error:
        return f"⚠️ {escape(status)}"
    return escape(status)


def _deployment_lines(views: Sequence[DeploymentView], *, with_status: bool,
                      limit: int = 15) -> List[str]:
    lines: List[str] = []
    ordered = sorted(views, key=lambda v: (-v.day_pnl, v.label))
    for view in ordered[:limit]:
        money = fmt_money(view.day_pnl, view.currency, sign=True, html=True)
        line = f"• {escape(short(view.label))}  <b>{money}</b>"
        if view.hour_delta is not None and view.hour_delta != 0:
            line += f" ({fmt_money(view.hour_delta, view.currency, sign=True, html=True)}/h)"
        if with_status:
            line += f"  <i>{_status_tag(view.deployment)}</i>"
        if view.baseline_source == "partial":
            line += " ˣ"
        lines.append(line)
    if len(ordered) > limit:
        lines.append(f"… and {len(ordered) - limit} more")
    return lines


def _period_line(label: str, stats: PeriodStats, currency: str) -> str:
    if stats.trading_days == 0:
        return f"{label} —"
    text = f"{label} <b>{fmt_money(stats.net, currency, sign=True, html=True)}</b>"
    text += f" ({stats.trading_days}d, {pct(stats.win_rate)} win)"
    return text


def _header(view: RunView, icon: str, title: str) -> str:
    return f"{icon} <b>{escape(title)}</b> · {fmt_stamp(view.now)}"


def _footer_periods(view: RunView) -> List[str]:
    cur = view.currency
    return [
        " · ".join([
            _period_line("MTD", view.mtd, cur),
            _period_line("FYTD", view.fytd, cur),
        ]),
    ]


def _advance_tax_line(view: RunView, *, window_days: int = 21) -> Optional[str]:
    due = view.next_due
    if due is None:
        return None
    days = due.days_until(view.day)
    if days < 0 or days > window_days:
        return None
    when = "today" if days == 0 else f"in {days} day{'s' if days != 1 else ''}"
    return (f"🧾 Advance tax {due.ordinal} instalment ({due.cumulative_pct}% cumulative) due "
            f"{due.due:%d %b} — {when}. FYTD {fmt_money(view.fytd.net, view.currency, sign=True, html=True)}")


def _exclusions(view: RunView) -> List[str]:
    lines: List[str] = []
    if view.paper and view.paper_total is not None:
        lines.append(f"<i>paper ({len(view.paper)}): {fmt_money(view.paper_total, view.currency, sign=True, html=True)} — not counted</i>")
    for code, total in view.other_currency_totals.items():
        lines.append(f"<i>{escape(code)}: {fmt_money(total, code, sign=True, html=True)} — reported separately</i>")
    return lines


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
def render_hourly(view: RunView) -> str:
    cur = view.currency
    lines = [_header(view, "📊", "Tradetron")]
    headline = f"Today <b>{fmt_money(view.today_total, cur, sign=True, html=True)}</b> {arrow(view.today_total)}"
    if view.hour_delta is not None:
        headline += f"  ({fmt_money(view.hour_delta, cur, sign=True, html=True)} since last update)"
    lines.append(headline)
    counts = f"{len(view.active)} active"
    if view.errors:
        counts += f" · <b>{len(view.errors)} in error ⚠️</b>"
    if view.open_positions:
        counts += f" · {view.open_positions} open position{'s' if view.open_positions != 1 else ''}"
    lines.append(counts)
    if view.counted:
        lines.append("")
        lines.extend(_deployment_lines(view.counted, with_status=True))
    lines.extend(_exclusions(view))
    lines.append("")
    lines.extend(_footer_periods(view))
    tax = _advance_tax_line(view, window_days=7)
    if tax:
        lines.append(tax)
    if any(d.baseline_source == "partial" for d in view.counted):
        lines.append("<i>ˣ first snapshot was mid-session; day figure is partial</i>")
    return "\n".join(lines)


def render_eod(view: RunView) -> str:
    cur = view.currency
    lines = [f"🧾 <b>EOD · {escape(fmt_day(view.day))}</b>"]
    lines.append(f"Day <b>{fmt_money(view.today_total, cur, sign=True, html=True)}</b> {arrow(view.today_total)}")
    streak = view.fytd.current_streak
    if streak and view.today_total != 0:
        lines.append(f"Streak: {'+' if streak > 0 else ''}{streak} day{'s' if abs(streak) != 1 else ''} "
                     f"{'green' if streak > 0 else 'red'}")
    if view.errors:
        lines.append(f"<b>{len(view.errors)} deployment(s) ended in error ⚠️</b>")
    if view.counted:
        lines.append("")
        lines.extend(_deployment_lines(view.counted, with_status=False))
    lines.extend(_exclusions(view))
    lines.append("")
    lines.append(_period_line("Week", view.week, cur))
    lines.extend(_footer_periods(view))
    if view.fytd.trading_days:
        dd = fmt_money(view.fytd.max_drawdown, cur, html=True)
        lines.append(f"FY max drawdown {dd} · profit factor {view.fytd.profit_factor if view.fytd.profit_factor is not None else '—'}")
    tax = _advance_tax_line(view)
    if tax:
        lines.append(tax)
    for note in view.notes:
        lines.append(f"<i>{escape(note)}</i>")
    return "\n".join(lines)


def render_weekly(view: RunView) -> str:
    cur = view.currency
    w = view.week
    lines = [f"📅 <b>Week {escape(iso_week_label(view.day))}</b>"]
    if w.trading_days == 0:
        lines.append("No trading days recorded this week.")
        return "\n".join(lines)
    lines.append(f"Net <b>{fmt_money(w.net, cur, sign=True, html=True)}</b> · {w.win_days}W / {w.loss_days}L"
                 + (f" / {w.flat_days} flat" if w.flat_days else ""))
    if w.best_day and w.worst_day:
        lines.append(f"Best {w.best_day[0]:%a} {fmt_money(w.best_day[1], cur, sign=True, html=True)} · "
                     f"worst {w.worst_day[0]:%a} {fmt_money(w.worst_day[1], cur, sign=True, html=True)}")
    lines.append("")
    for day, value in w.daily:
        lines.append(f"{day:%a %d}  {fmt_money(value, cur, sign=True, html=True)}")
    return "\n".join(lines)


def render_monthly(view: RunView) -> str:
    cur = view.currency
    m = view.mtd
    lines = [f"🗓 <b>{view.day:%B %Y}</b>"]
    if m.trading_days == 0:
        lines.append("No trading days recorded this month.")
        return "\n".join(lines)
    lines.append(f"Net <b>{fmt_money(m.net, cur, sign=True, html=True)}</b> · {m.trading_days} days · "
                 f"{pct(m.win_rate)} win · PF {m.profit_factor if m.profit_factor is not None else '—'}")
    lines.append(f"Avg day {fmt_money(m.avg_day, cur, sign=True, html=True)} · "
                 f"max DD {fmt_money(m.max_drawdown, cur, html=True)}")
    if view.month_deployments:
        lines.append("")
        lines.append("<b>By deployment</b>")
        for d in view.month_deployments[:10]:
            lines.append(f"• {escape(short(d.label))}  <b>{fmt_money(d.net, cur, sign=True, html=True)}</b>"
                         f"  ({d.days}d, {pct(d.win_rate)})")
    lines.append("")
    lines.append(_period_line(escape(quarter_label(view.day)), view.quarter, cur))
    lines.append(_period_line(f"FYTD ({escape(view.fy)})", view.fytd, cur))
    return "\n".join(lines)


def render_quarter(view: RunView) -> str:
    cur = view.currency
    q = view.quarter
    lines = [f"📘 <b>{escape(quarter_label(view.day))} closed</b>"]
    lines.append(f"Quarter net <b>{fmt_money(q.net, cur, sign=True, html=True)}</b> · {q.trading_days} days · "
                 f"gross +{fmt_money(q.gross_profit, cur, html=True)} / {fmt_money(q.gross_loss, cur, html=True)}")
    lines.append(f"FYTD <b>{fmt_money(view.fytd.net, cur, sign=True, html=True)}</b> · "
                 f"est. turnover {fmt_money(view.fytd.turnover_estimate, cur, html=True)}")
    if view.next_due:
        lines.append(f"Next advance-tax instalment: {view.next_due.ordinal} ({view.next_due.cumulative_pct}%) "
                     f"due {view.next_due.due:%d %b %Y}")
    lines.append("<i>Tax workings: reports/tax/</i>")
    return "\n".join(lines)


def render_preopen(view: RunView) -> str:
    cur = view.currency
    lines = [_header(view, "🌅", "Pre-open")]
    lines.append(f"{len(view.active)} active deployment{'s' if len(view.active) != 1 else ''}"
                 + (f" · <b>{len(view.errors)} in error ⚠️</b>" if view.errors else ""))
    lines.extend(_footer_periods(view))
    return "\n".join(lines)


def render_error_alert(view: RunView) -> str:
    lines = [f"🚨 <b>Deployment error</b> · {fmt_stamp(view.now)}"]
    for d in view.new_errors:
        lines.append(f"• {escape(short(d.label, 30))} — <b>{escape(d.deployment.status)}</b>"
                     f" ({escape(d.deployment.broker or 'broker n/a')})")
    lines.append("<i>Check the Deployed page on Tradetron.</i>")
    return "\n".join(lines)


def render_fetch_failure(error: str, now: datetime) -> str:
    return (f"⚠️ <b>Tradetron fetch failed</b> · {fmt_stamp(now)}\n"
            f"<code>{escape(error)}</code>\n"
            "<i>No snapshot was stored for this run.</i>")


__all__ = [
    "DeploymentView", "RunView", "arrow", "fmt_day", "fmt_money", "fmt_stamp", "group_indian",
    "pct", "render_eod", "render_error_alert", "render_fetch_failure", "render_hourly",
    "render_monthly", "render_preopen", "render_quarter", "render_weekly", "short",
]
