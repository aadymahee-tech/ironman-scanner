"""Markdown summary (for the Actions step summary) and a self-contained HTML dashboard.

The dashboard is inline CSS + an inline SVG equity curve. No JavaScript, no CDN,
nothing external: it opens from disk, from a workflow artifact, or from GitHub's
file view (raw). It is *not* meant for GitHub Pages — the data is your P&L.
"""

from __future__ import annotations

import html
from datetime import date
from typing import List, Optional, Sequence, Tuple

from .analytics import DeploymentStats, PeriodStats, equity_curve
from .messages import RunView, fmt_money, pct
from .store import LedgerRow

DailyTotal = Tuple[date, float]


def _m(value: Optional[float], currency: str, sign: bool = True) -> str:
    return fmt_money(value, currency, sign=sign)


def _pf(stats: PeriodStats) -> str:
    return "—" if stats.profit_factor is None else f"{stats.profit_factor:.2f}"


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #
def render_markdown(view: RunView, recent: Sequence[DailyTotal], fy_deployments: Sequence[DeploymentStats]) -> str:
    cur = view.currency
    lines: List[str] = []
    lines.append(f"# Tradetron P&L — {view.now:%d %b %Y, %H:%M IST}")
    lines.append("")
    lines.append(f"**Today {_m(view.today_total, cur)}** · MTD {_m(view.mtd.net, cur)} · "
                 f"FYTD {_m(view.fytd.net, cur)} ({view.fy}) · {len(view.active)} active · "
                 f"{len(view.errors)} in error · source `{view.source}` · run `{view.kind}`")
    lines.append("")

    lines.append("## Deployments")
    lines.append("")
    lines.append("| Deployment | Id | Type | Broker | Status | Day P&L | API P&L | Baseline | Open |")
    lines.append("| --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: |")
    for v in sorted(view.deployments, key=lambda d: (-d.day_pnl, d.label)):
        dep = v.deployment
        flag = "" if v.counted else " _(not counted)_"
        lines.append(
            f"| {v.label}{flag} | {dep.deployment_id} | {dep.deployment_type or '—'} | {dep.broker or '—'} | "
            f"{dep.status or '—'} | {_m(v.day_pnl, v.currency)} | {_m(dep.pnl, v.currency)} | "
            f"{v.baseline_source or '—'} | {dep.open_positions if dep.open_positions is not None else '—'} |"
        )
    lines.append("")

    if recent:
        lines.append("## Recent trading days")
        lines.append("")
        lines.append("| Day | P&L | Cumulative (FY) |")
        lines.append("| --- | ---: | ---: |")
        curve = dict(equity_curve(list(view.fytd.daily)))
        for day, value in list(recent)[-20:]:
            lines.append(f"| {day:%a %d %b %Y} | {_m(value, cur)} | {_m(curve.get(day), cur)} |")
        lines.append("")

    if view.months:
        lines.append(f"## Months ({view.fy})")
        lines.append("")
        lines.append("| Month | Days | Win rate | Net |")
        lines.append("| --- | ---: | ---: | ---: |")
        for m in view.months:
            lines.append(f"| {m.month} | {m.days} | {pct(m.win_rate)} | {_m(m.net, cur)} |")
        lines.append("")

    if fy_deployments:
        lines.append(f"## Deployments ({view.fy})")
        lines.append("")
        lines.append("| Deployment | Segment | Days | Win rate | Best | Worst | Net |")
        lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
        for d in fy_deployments:
            lines.append(f"| {d.label} | {d.segment} | {d.days} | {pct(d.win_rate)} | "
                         f"{_m(d.best, cur)} | {_m(d.worst, cur)} | {_m(d.net, cur)} |")
        lines.append("")

    s = view.fytd
    lines.append(f"## Statistics ({view.fy})")
    lines.append("")
    lines.append(f"- Trading days: {s.trading_days} · win {s.win_days} / loss {s.loss_days} / flat {s.flat_days}"
                 f" · win rate {pct(s.win_rate)}")
    lines.append(f"- Gross profit {_m(s.gross_profit, cur)} · gross loss {_m(s.gross_loss, cur)} · profit factor {_pf(s)}")
    lines.append(f"- Average day {_m(s.avg_day, cur)} · average win {_m(s.avg_win, cur)} · average loss {_m(s.avg_loss, cur)}")
    if s.best_day and s.worst_day:
        lines.append(f"- Best day {s.best_day[0]:%d %b} {_m(s.best_day[1], cur)} · worst day {s.worst_day[0]:%d %b} {_m(s.worst_day[1], cur)}")
    dd_span = ""
    if s.drawdown_from and s.drawdown_to:
        dd_span = f" ({s.drawdown_from:%d %b} → {s.drawdown_to:%d %b})"
    lines.append(f"- Max drawdown {_m(s.max_drawdown, cur, sign=False)}{dd_span} · estimated turnover {_m(s.turnover_estimate, cur, sign=False)}")
    lines.append("")
    for note in view.notes:
        lines.append(f"> {note}")
    if view.notes:
        lines.append("")
    lines.append(f"<sub>Generated {view.now:%d %b %Y %H:%M IST} · tradetron-pnl</sub>")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# HTML dashboard
# --------------------------------------------------------------------------- #
CSS = """
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--fg:#e6e8ee;--mute:#8b93a7;--up:#2ecc71;--down:#ff5c5c;--accent:#f5b84b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:24px 16px 48px}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 10px;color:var(--mute);text-transform:uppercase;letter-spacing:.06em}
.sub{color:var(--mute);margin-bottom:18px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}.card .k{color:var(--mute);font-size:12px}.card .v{font-size:20px;font-weight:600;margin-top:4px}
.up{color:var(--up)}.down{color:var(--down)}.flat{color:var(--mute)}table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}th{color:var(--mute);font-weight:500;font-size:12px}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:0}.err{color:var(--down);font-weight:600}.mute{color:var(--mute)}svg{width:100%;height:auto;display:block;background:var(--card);border:1px solid var(--line);border-radius:10px}
.note{color:var(--mute);font-size:12px;margin-top:18px}
"""


def _cls(value: float) -> str:
    return "up" if value > 0 else "down" if value < 0 else "flat"


def _svg_curve(curve: Sequence[DailyTotal], currency: str, width: int = 1000, height: int = 260) -> str:
    if len(curve) < 2:
        return ('<svg viewBox="0 0 1000 120" xmlns="http://www.w3.org/2000/svg">'
                '<text x="500" y="64" fill="#8b93a7" text-anchor="middle" font-size="14">'
                'Equity curve appears after two trading days</text></svg>')
    pad_l, pad_r, pad_t, pad_b = 70, 16, 16, 28
    values = [v for _, v in curve]
    lo, hi = min(0.0, min(values)), max(0.0, max(values))
    if hi == lo:
        hi = lo + 1.0
    span_x = width - pad_l - pad_r
    span_y = height - pad_t - pad_b

    def x_at(i: int) -> float:
        return pad_l + span_x * i / (len(curve) - 1)

    def y_at(v: float) -> float:
        return pad_t + span_y * (hi - v) / (hi - lo)

    points = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, (_, v) in enumerate(curve))
    zero_y = y_at(0.0)
    area = f"M{x_at(0):.1f},{zero_y:.1f} L{points.replace(' ', ' L')} L{x_at(len(curve) - 1):.1f},{zero_y:.1f} Z"
    colour = "#2ecc71" if values[-1] >= 0 else "#ff5c5c"
    labels = []
    for frac in (0.0, 0.5, 1.0):
        v = lo + (hi - lo) * frac
        labels.append(f'<text x="{pad_l - 8}" y="{y_at(v) + 4:.1f}" fill="#8b93a7" font-size="11" '
                      f'text-anchor="end">{html.escape(fmt_money(v, currency))}</text>')
    first, last = curve[0][0], curve[-1][0]
    return (
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Equity curve">'
        f'<line x1="{pad_l}" y1="{zero_y:.1f}" x2="{width - pad_r}" y2="{zero_y:.1f}" stroke="#3a4150" stroke-dasharray="4 4"/>'
        f'<path d="{area}" fill="{colour}" fill-opacity="0.12"/>'
        f'<polyline points="{points}" fill="none" stroke="{colour}" stroke-width="2"/>'
        + "".join(labels) +
        f'<text x="{pad_l}" y="{height - 8}" fill="#8b93a7" font-size="11">{first:%d %b %Y}</text>'
        f'<text x="{width - pad_r}" y="{height - 8}" fill="#8b93a7" font-size="11" text-anchor="end">{last:%d %b %Y}</text>'
        '</svg>'
    )


def render_dashboard(view: RunView, fy_deployments: Sequence[DeploymentStats],
                     recent: Sequence[DailyTotal]) -> str:
    cur = view.currency
    e = html.escape
    s = view.fytd

    def card(label: str, value: str, klass: str = "") -> str:
        return f'<div class="card"><div class="k">{e(label)}</div><div class="v {klass}">{e(value)}</div></div>'

    cards = [
        card("Today", _m(view.today_total, cur), _cls(view.today_total)),
        card("Week", _m(view.week.net, cur), _cls(view.week.net)),
        card("Month to date", _m(view.mtd.net, cur), _cls(view.mtd.net)),
        card(f"FY to date ({view.fy})", _m(s.net, cur), _cls(s.net)),
        card("Win rate (FY)", pct(s.win_rate)),
        card("Profit factor (FY)", _pf(s)),
        card("Max drawdown (FY)", _m(s.max_drawdown, cur, sign=False), "down" if s.max_drawdown < 0 else "flat"),
        card("Active / error", f"{len(view.active)} / {len(view.errors)}", "down" if view.errors else ""),
    ]

    dep_rows = []
    for v in sorted(view.deployments, key=lambda d: (-d.day_pnl, d.label)):
        dep = v.deployment
        status_cls = "err" if dep.in_error else ("mute" if dep.finished else "")
        counted = "" if v.counted else ' <span class="mute">(not counted)</span>'
        dep_rows.append(
            f"<tr><td>{e(v.label)}{counted}</td><td class='n'>{dep.deployment_id}</td><td>{e(dep.deployment_type or '—')}</td>"
            f"<td>{e(dep.broker or '—')}</td><td class='{status_cls}'>{e(dep.status or '—')}</td>"
            f"<td class='n {_cls(v.day_pnl)}'>{e(_m(v.day_pnl, v.currency))}</td>"
            f"<td class='n'>{e(_m(dep.pnl, v.currency))}</td><td class='mute'>{e(v.baseline_source or '—')}</td>"
            f"<td class='n'>{dep.open_positions if dep.open_positions is not None else '—'}</td></tr>"
        )

    curve = equity_curve(list(s.daily))
    curve_map = dict(curve)
    day_rows = [
        f"<tr><td>{d:%a %d %b %Y}</td><td class='n {_cls(v)}'>{e(_m(v, cur))}</td>"
        f"<td class='n'>{e(_m(curve_map.get(d), cur))}</td></tr>"
        for d, v in reversed(list(recent)[-30:])
    ]
    month_rows = [
        f"<tr><td>{e(m.month)}</td><td class='n'>{m.days}</td><td class='n'>{e(pct(m.win_rate))}</td>"
        f"<td class='n {_cls(m.net)}'>{e(_m(m.net, cur))}</td></tr>"
        for m in view.months
    ]
    fy_rows = [
        f"<tr><td>{e(d.label)}</td><td>{e(d.segment)}</td><td class='n'>{d.days}</td><td class='n'>{e(pct(d.win_rate))}</td>"
        f"<td class='n up'>{e(_m(d.best, cur))}</td><td class='n down'>{e(_m(d.worst, cur))}</td>"
        f"<td class='n {_cls(d.net)}'>{e(_m(d.net, cur))}</td></tr>"
        for d in fy_deployments
    ]

    def table(head: Sequence[str], rows: Sequence[str], numeric: Sequence[int] = ()) -> str:
        ths = "".join(f"<th class='{'n' if i in numeric else ''}'>{e(h)}</th>" for i, h in enumerate(head))
        body = "".join(rows) if rows else f"<tr><td colspan='{len(head)}' class='mute'>nothing yet</td></tr>"
        return f"<table><thead><tr>{ths}</tr></thead><tbody>{body}</tbody></table>"

    notes = "".join(f"<div class='note'>{e(n)}</div>" for n in view.notes)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Tradetron P&amp;L · {e(view.fy)}</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>Tradetron P&amp;L</h1>
<div class="sub">{view.now:%A %d %B %Y, %H:%M IST} · {e(view.fy)} · reporting currency {e(cur)} · source {e(view.source)}</div>
<div class="cards">{''.join(cards)}</div>
<h2>Equity curve · {e(view.fy)}</h2>
{_svg_curve(curve, cur)}
<h2>Deployments · today</h2>
{table(["Deployment", "Id", "Type", "Broker", "Status", "Day P&L", "API P&L", "Baseline", "Open"], dep_rows, (1, 5, 6, 8))}
<h2>Recent trading days</h2>
{table(["Day", "P&L", "Cumulative (FY)"], day_rows, (1, 2))}
<h2>Months · {e(view.fy)}</h2>
{table(["Month", "Days", "Win rate", "Net"], month_rows, (1, 2, 3))}
<h2>Deployments · {e(view.fy)}</h2>
{table(["Deployment", "Segment", "Days", "Win rate", "Best", "Worst", "Net"], fy_rows, (2, 3, 4, 5, 6))}
{notes}
<div class="note">Tradetron deployment P&amp;L before brokerage and charges. Generated by tradetron-pnl; keep this repository private.</div>
</div></body></html>
"""


def recent_daily(rows: Sequence[LedgerRow]) -> List[DailyTotal]:
    """Convenience for callers that already filtered rows: day totals, ascending."""
    from .analytics import daily_totals
    return daily_totals(rows)


__all__ = ["render_dashboard", "render_markdown", "recent_daily"]
