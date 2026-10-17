"""Period statistics over the daily ledger: totals, win rate, drawdown, streaks.

All functions take plain ledger rows. Filtering (reporting currency, paper
deployments, excluded ids) is the caller's job — see ``select_rows`` — so the
same maths serves the Telegram messages, the dashboard and the tax report.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .config import DeploymentTag, tag_for
from .store import LedgerRow

DailyTotal = Tuple[date, float]


def select_rows(rows: Iterable[LedgerRow], tags: Mapping[int, DeploymentTag], *,
                currency: str = "INR", default_segment: str = "fno",
                include_paper: bool = False, start: Optional[date] = None,
                end: Optional[date] = None) -> List[LedgerRow]:
    """Rows that count towards the headline numbers."""
    out: List[LedgerRow] = []
    for row in rows:
        if row.currency != currency:
            continue
        if row.paper and not include_paper:
            continue
        if not tag_for(row.deployment_id, tags, default_segment).include:
            continue
        if start and row.trading_day < start:
            continue
        if end and row.trading_day > end:
            continue
        out.append(row)
    return out


def daily_totals(rows: Iterable[LedgerRow]) -> List[DailyTotal]:
    totals: Dict[date, float] = defaultdict(float)
    for row in rows:
        totals[row.trading_day] += row.day_pnl
    return sorted((day, round(value, 2)) for day, value in totals.items())


def equity_curve(daily: Sequence[DailyTotal]) -> List[DailyTotal]:
    running = 0.0
    curve: List[DailyTotal] = []
    for day, value in daily:
        running += value
        curve.append((day, round(running, 2)))
    return curve


def max_drawdown(curve: Sequence[DailyTotal]) -> Tuple[float, Optional[date], Optional[date]]:
    """Largest peak-to-trough fall of the cumulative curve (starting from 0)."""
    peak = 0.0
    peak_day: Optional[date] = None
    worst = 0.0
    worst_peak: Optional[date] = None
    worst_trough: Optional[date] = None
    for day, value in curve:
        if value > peak:
            peak, peak_day = value, day
        dd = value - peak
        if dd < worst:
            worst, worst_peak, worst_trough = dd, peak_day, day
    return round(worst, 2), worst_peak, worst_trough


@dataclass
class PeriodStats:
    start: Optional[date] = None
    end: Optional[date] = None
    trading_days: int = 0
    net: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    win_days: int = 0
    loss_days: int = 0
    flat_days: int = 0
    best_day: Optional[DailyTotal] = None
    worst_day: Optional[DailyTotal] = None
    max_drawdown: float = 0.0
    drawdown_from: Optional[date] = None
    drawdown_to: Optional[date] = None
    current_streak: int = 0          # +n winning days in a row, -n losing days
    daily: List[DailyTotal] = field(default_factory=list)

    @property
    def win_rate(self) -> Optional[float]:
        decided = self.win_days + self.loss_days
        return None if decided == 0 else self.win_days / decided

    @property
    def avg_win(self) -> Optional[float]:
        return None if self.win_days == 0 else round(self.gross_profit / self.win_days, 2)

    @property
    def avg_loss(self) -> Optional[float]:
        return None if self.loss_days == 0 else round(self.gross_loss / self.loss_days, 2)

    @property
    def profit_factor(self) -> Optional[float]:
        if self.gross_loss == 0:
            return None
        return round(self.gross_profit / abs(self.gross_loss), 2)

    @property
    def avg_day(self) -> Optional[float]:
        return None if self.trading_days == 0 else round(self.net / self.trading_days, 2)

    @property
    def turnover_estimate(self) -> float:
        """Absolute sum of daily results — the day-grain floor of ICAI turnover."""
        return round(self.gross_profit + abs(self.gross_loss), 2)


def period_stats(daily: Sequence[DailyTotal], start: Optional[date] = None,
                 end: Optional[date] = None) -> PeriodStats:
    window = [(d, v) for d, v in daily if (start is None or d >= start) and (end is None or d <= end)]
    stats = PeriodStats(start=start, end=end, daily=window)
    if not window:
        return stats
    stats.trading_days = len(window)
    for day, value in window:
        stats.net += value
        if value > 0:
            stats.win_days += 1
            stats.gross_profit += value
        elif value < 0:
            stats.loss_days += 1
            stats.gross_loss += value
        else:
            stats.flat_days += 1
    stats.net = round(stats.net, 2)
    stats.gross_profit = round(stats.gross_profit, 2)
    stats.gross_loss = round(stats.gross_loss, 2)
    stats.best_day = max(window, key=lambda x: x[1])
    stats.worst_day = min(window, key=lambda x: x[1])
    stats.max_drawdown, stats.drawdown_from, stats.drawdown_to = max_drawdown(equity_curve(window))
    stats.current_streak = _streak(window)
    return stats


def _streak(window: Sequence[DailyTotal]) -> int:
    streak = 0
    for _, value in reversed(window):
        if value == 0:
            continue
        sign = 1 if value > 0 else -1
        if streak == 0:
            streak = sign
        elif (streak > 0) == (sign > 0):
            streak += sign
        else:
            break
    return streak


@dataclass
class DeploymentStats:
    deployment_id: int
    name: str
    days: int
    net: float
    win_days: int
    loss_days: int
    best: float
    worst: float
    last_status: str
    last_day: Optional[date]
    segment: str = ""
    alias: str = ""

    @property
    def win_rate(self) -> Optional[float]:
        decided = self.win_days + self.loss_days
        return None if decided == 0 else self.win_days / decided

    @property
    def label(self) -> str:
        return self.alias or self.name or f"Deployment {self.deployment_id}"


def by_deployment(rows: Iterable[LedgerRow], tags: Mapping[int, DeploymentTag] = {},
                  default_segment: str = "fno") -> List[DeploymentStats]:
    grouped: Dict[int, List[LedgerRow]] = defaultdict(list)
    for row in rows:
        grouped[row.deployment_id].append(row)
    out: List[DeploymentStats] = []
    for dep_id, items in grouped.items():
        items.sort(key=lambda r: r.trading_day)
        values = [r.day_pnl for r in items]
        tag = tag_for(dep_id, tags, default_segment)
        out.append(DeploymentStats(
            deployment_id=dep_id,
            name=items[-1].name,
            days=len(items),
            net=round(sum(values), 2),
            win_days=sum(1 for v in values if v > 0),
            loss_days=sum(1 for v in values if v < 0),
            best=round(max(values), 2),
            worst=round(min(values), 2),
            last_status=items[-1].status_eod,
            last_day=items[-1].trading_day,
            segment=tag.segment,
            alias=tag.alias,
        ))
    out.sort(key=lambda d: (-d.net, d.deployment_id))
    return out


@dataclass
class MonthStats:
    month: str            # YYYY-MM
    days: int
    net: float
    win_days: int
    loss_days: int

    @property
    def win_rate(self) -> Optional[float]:
        decided = self.win_days + self.loss_days
        return None if decided == 0 else self.win_days / decided


def by_month(daily: Sequence[DailyTotal]) -> List[MonthStats]:
    grouped: Dict[str, List[float]] = defaultdict(list)
    for day, value in daily:
        grouped[f"{day:%Y-%m}"].append(value)
    return [
        MonthStats(month=m, days=len(v), net=round(sum(v), 2),
                   win_days=sum(1 for x in v if x > 0), loss_days=sum(1 for x in v if x < 0))
        for m, v in sorted(grouped.items())
    ]


__all__ = [
    "DeploymentStats", "MonthStats", "PeriodStats", "by_deployment", "by_month",
    "daily_totals", "equity_curve", "max_drawdown", "period_stats", "select_rows",
]
