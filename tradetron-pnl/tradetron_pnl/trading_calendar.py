"""IST clock, NSE session phases, trading-day arithmetic and Indian financial-year helpers.

NSE has no daylight saving, so a fixed +05:30 offset is exact and avoids a tzdata
dependency on minimal CI images.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
UTC = timezone.utc

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

PRE_OPEN = "pre_open"      # trading day, before 09:15 IST
OPEN = "open"              # 09:15 – 15:30 IST
POST_CLOSE = "post_close"  # trading day, after 15:30 IST
CLOSED = "closed"          # weekend or exchange holiday

Holidays = Dict[date, str]


# --------------------------------------------------------------------------- #
# Clock
# --------------------------------------------------------------------------- #
def now_ist() -> datetime:
    return datetime.now(tz=IST)


def to_ist(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def parse_when(text: str) -> datetime:
    """Parse an ISO-8601 timestamp; a naive value is taken as IST."""
    cleaned = text.strip().replace("Z", "+00:00")
    moment = datetime.fromisoformat(cleaned)
    return to_ist(moment)


# --------------------------------------------------------------------------- #
# Holidays and sessions
# --------------------------------------------------------------------------- #
def load_holidays(path: Path) -> Holidays:
    """Read ``date,name`` rows. Blank lines and ``#`` comments are ignored.

    A missing file is not an error: the tool then treats every weekday as a
    trading day and ``holiday_coverage`` reports the gap.
    """
    holidays: Holidays = {}
    path = Path(path)
    if not path.exists():
        return holidays
    with path.open(newline="", encoding="utf-8") as fh:
        for raw in csv.reader(fh):
            if not raw or not raw[0].strip() or raw[0].lstrip().startswith("#"):
                continue
            if raw[0].strip().lower() == "date":
                continue
            day = date.fromisoformat(raw[0].strip())
            holidays[day] = raw[1].strip() if len(raw) > 1 else ""
    return holidays


def holiday_coverage(holidays: Holidays, year: int) -> bool:
    """True when the holiday table has at least one entry for ``year``."""
    return any(d.year == year for d in holidays)


def is_trading_day(day: date, holidays: Holidays) -> bool:
    return day.weekday() < 5 and day not in holidays


def session_phase(now: datetime, holidays: Holidays) -> str:
    now = to_ist(now)
    if not is_trading_day(now.date(), holidays):
        return CLOSED
    clock = now.time().replace(tzinfo=None)
    if clock < MARKET_OPEN:
        return PRE_OPEN
    if clock < MARKET_CLOSE:
        return OPEN
    return POST_CLOSE


def previous_trading_day(day: date, holidays: Holidays, max_lookback: int = 30) -> date:
    probe = day
    for _ in range(max_lookback):
        probe -= timedelta(days=1)
        if is_trading_day(probe, holidays):
            return probe
    raise ValueError(f"no trading day in the {max_lookback} days before {day}")


def next_trading_day(day: date, holidays: Holidays, max_lookahead: int = 30) -> date:
    probe = day
    for _ in range(max_lookahead):
        probe += timedelta(days=1)
        if is_trading_day(probe, holidays):
            return probe
    raise ValueError(f"no trading day in the {max_lookahead} days after {day}")


def is_last_trading_day_of_week(day: date, holidays: Holidays) -> bool:
    nxt = next_trading_day(day, holidays)
    return nxt.isocalendar()[:2] != day.isocalendar()[:2]


def is_last_trading_day_of_month(day: date, holidays: Holidays) -> bool:
    nxt = next_trading_day(day, holidays)
    return (nxt.year, nxt.month) != (day.year, day.month)


def is_last_trading_day_of_quarter(day: date, holidays: Holidays) -> bool:
    """Quarters are Indian financial-year quarters (Apr-Jun, Jul-Sep, Oct-Dec, Jan-Mar)."""
    nxt = next_trading_day(day, holidays)
    return (fy_label(nxt), fy_quarter(nxt)) != (fy_label(day), fy_quarter(day))


def iso_week_label(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"W{week:02d} {year}"


def week_bounds(day: date) -> Tuple[date, date]:
    """Monday..Friday of the calendar week containing ``day``."""
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=4)


def month_bounds(day: date) -> Tuple[date, date]:
    first = day.replace(day=1)
    if first.month == 12:
        nxt = first.replace(year=first.year + 1, month=1)
    else:
        nxt = first.replace(month=first.month + 1)
    return first, nxt - timedelta(days=1)


# --------------------------------------------------------------------------- #
# Indian financial year
# --------------------------------------------------------------------------- #
def fy_start_year(day: date) -> int:
    return day.year if day.month >= 4 else day.year - 1


def fy_label(day: date) -> str:
    start = fy_start_year(day)
    return f"FY{start}-{(start + 1) % 100:02d}"


def fy_bounds(label: str) -> Tuple[date, date]:
    """``FY2026-27`` -> (2026-04-01, 2027-03-31)."""
    text = label.strip().upper()
    if not text.startswith("FY"):
        raise ValueError(f"bad financial year label {label!r}; expected like FY2026-27")
    start = int(text[2:6])
    return date(start, 4, 1), date(start + 1, 3, 31)


def fy_quarter(day: date) -> int:
    """1 = Apr-Jun, 2 = Jul-Sep, 3 = Oct-Dec, 4 = Jan-Mar."""
    return ((day.month - 4) % 12) // 3 + 1


def quarter_bounds(label: str, quarter: int) -> Tuple[date, date]:
    if quarter not in (1, 2, 3, 4):
        raise ValueError("quarter must be 1..4")
    start_year = fy_bounds(label)[0].year
    month = 4 + 3 * (quarter - 1)
    year = start_year
    if month > 12:
        month -= 12
        year += 1
    first = date(year, month, 1)
    end_month = month + 2
    end_year = year
    if end_month > 12:
        end_month -= 12
        end_year += 1
    last = month_bounds(date(end_year, end_month, 1))[1]
    return first, last


def quarter_label(day: date) -> str:
    return f"Q{fy_quarter(day)} {fy_label(day)}"


@dataclass(frozen=True)
class AdvanceTaxDue:
    due: date
    cumulative_pct: int
    ordinal: str

    def days_until(self, day: date) -> int:
        return (self.due - day).days


def advance_tax_schedule(label: str) -> List[AdvanceTaxDue]:
    """Section 211 instalments for an individual (not under presumptive 44AD)."""
    start = fy_bounds(label)[0].year
    return [
        AdvanceTaxDue(date(start, 6, 15), 15, "1st"),
        AdvanceTaxDue(date(start, 9, 15), 45, "2nd"),
        AdvanceTaxDue(date(start, 12, 15), 75, "3rd"),
        AdvanceTaxDue(date(start + 1, 3, 15), 100, "4th"),
    ]


def next_advance_tax_due(day: date) -> Optional[AdvanceTaxDue]:
    for due in advance_tax_schedule(fy_label(day)):
        if due.due >= day:
            return due
    return None
