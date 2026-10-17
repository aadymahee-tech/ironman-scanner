"""Turn the snapshot time series into one P&L figure per deployment per trading day.

The Developer API reports a single ``pnl`` number per deployment. Whether that is
*today's* P&L or the P&L *since deployment* is not documented, so the semantics
are a setting (``TTPNL_PNL_SEMANTICS``):

``cumulative`` (default)
    ``day_pnl = close_pnl - baseline``. The baseline is chosen in this order:

    1. ``preopen``        — the first snapshot of the day, when it was taken before
                            09:15 IST (the 09:00 scheduled run exists for this);
    2. ``prev_close``     — the previous trading day's close for the same deployment;
    3. ``new_deployment`` — 0, when the deployment was deployed today;
    4. ``partial``        — the first snapshot of the day, mid-session, so the day's
                            P&L is understated; flagged in ``baseline_source``.

``today``
    ``day_pnl = close_pnl``; the baseline is 0.

Both the baseline and its provenance are written to the ledger so a figure can
always be audited back to raw snapshots. ``docs/API.md`` explains how to check
which semantics your account uses.
"""

from __future__ import annotations

import zlib
from collections import defaultdict
from datetime import date
from typing import Dict, Iterable, List, Mapping, Sequence

from .store import DataStore, LedgerRow, Snapshot
from .trading_calendar import PRE_OPEN, Holidays, previous_trading_day

RESET_TOLERANCE = 0.01


def group_by_deployment(snapshots: Iterable[Snapshot]) -> Dict[int, List[Snapshot]]:
    grouped: Dict[int, List[Snapshot]] = defaultdict(list)
    for snap in snapshots:
        grouped[snap.deployment_id].append(snap)
    for rows in grouped.values():
        rows.sort(key=lambda s: s.ts_utc)
    return dict(grouped)


def derive_day(
    day: date,
    snapshots: Sequence[Snapshot],
    prev_close: Mapping[int, float],
    semantics: str = "cumulative",
) -> List[LedgerRow]:
    """Build ledger rows for ``day`` from that day's snapshots."""
    if semantics not in ("cumulative", "today"):
        raise ValueError(f"unknown pnl semantics {semantics!r}")

    rows: List[LedgerRow] = []
    for dep_id, series in sorted(group_by_deployment(s for s in snapshots if s.trading_day == day).items()):
        first, last = series[0], series[-1]
        notes: List[str] = []
        if not last.pnl_known:
            notes.append("pnl missing in last snapshot")

        if semantics == "today":
            baseline, source = 0.0, "today"
        elif first.phase == PRE_OPEN:
            baseline, source = first.pnl, "preopen"
            previous = prev_close.get(dep_id)
            if previous is not None and abs(previous - first.pnl) > RESET_TOLERANCE:
                notes.append(f"pre-open {first.pnl:.2f} != previous close {previous:.2f} (reset or redeploy)")
        elif dep_id in prev_close:
            baseline, source = prev_close[dep_id], "prev_close"
        elif first.deployed_on[:10] == day.isoformat():
            baseline, source = 0.0, "new_deployment"
        else:
            baseline, source = first.pnl, "partial"
            notes.append("first snapshot was mid-session; day P&L may be understated")

        rows.append(LedgerRow(
            trading_day=day,
            deployment_id=dep_id,
            name=last.name or first.name,
            deployment_type=last.deployment_type,
            broker=last.broker,
            currency=last.currency,
            paper=last.paper,
            day_pnl=round(last.pnl - baseline, 2),
            close_pnl=round(last.pnl, 2),
            baseline_pnl=round(baseline, 2),
            baseline_source=source,
            snapshots=len(series),
            first_ts_ist=first.ts_ist,
            last_ts_ist=last.ts_ist,
            status_eod=last.status,
            source="api" if last.source != "fixture" else "fixture",
            notes="; ".join(notes),
        ))
    return rows


def previous_close_map(ledger: Iterable[LedgerRow], day: date, holidays: Holidays,
                       max_gap_days: int = 10) -> Dict[int, float]:
    """Close P&L per deployment from the most recent ledger day before ``day``.

    Looks back through the ledger (not only the immediately preceding trading
    day) so a missed run does not lose the baseline, but gives up after
    ``max_gap_days`` so a stale value from weeks ago is not treated as current.
    """
    by_day: Dict[date, Dict[int, float]] = defaultdict(dict)
    for row in ledger:
        if row.trading_day < day and row.close_pnl is not None:
            by_day[row.trading_day][row.deployment_id] = row.close_pnl
    result: Dict[int, float] = {}
    probe = day
    for _ in range(max_gap_days):
        try:
            probe = previous_trading_day(probe, holidays)
        except ValueError:
            break
        for dep_id, close in by_day.get(probe, {}).items():
            result.setdefault(dep_id, close)
    return result


def update_ledger_for_day(store: DataStore, day: date, holidays: Holidays,
                          semantics: str = "cumulative") -> List[LedgerRow]:
    """Recompute ``day`` from snapshots and merge into the ledger. Idempotent."""
    snapshots = store.snapshots_for_day(day)
    if not snapshots:
        return []
    prev_close = previous_close_map(store.read_ledger(), day, holidays)
    rows = derive_day(day, snapshots, prev_close, semantics)
    store.upsert_ledger(rows)
    return rows


def rebuild_ledger(store: DataStore, holidays: Holidays, semantics: str = "cumulative",
                   keep_imported: bool = True) -> List[LedgerRow]:
    """Recompute every day that has snapshots, oldest first.

    Imported rows (``source == 'import'``) for days *without* snapshots survive;
    where both exist, snapshots win.
    """
    existing = store.read_ledger()
    table: Dict[tuple, LedgerRow] = {}
    if keep_imported:
        table = {r.key: r for r in existing if r.source == "import"}
    all_snaps = store.read_snapshots()
    days = sorted({s.trading_day for s in all_snaps})
    for day in days:
        prev_close = previous_close_map(table.values(), day, holidays)
        for row in derive_day(day, [s for s in all_snaps if s.trading_day == day], prev_close, semantics):
            table[row.key] = row
    rows = sorted(table.values(), key=lambda r: (r.trading_day, r.deployment_id))
    store.write_ledger(rows)
    return rows


def import_rows(records: Iterable[Mapping[str, str]], *, default_currency: str = "INR") -> List[LedgerRow]:
    """Build ledger rows from an external day-wise P&L export.

    Each record needs ``date`` and ``pnl``; ``deployment_id`` and ``name`` are
    optional (a synthetic negative id is derived from the name when absent so
    imported history never collides with real Tradetron ids).
    """
    rows: List[LedgerRow] = []
    for rec in records:
        day = date.fromisoformat(str(rec.get("date") or rec.get("trading_day")).strip()[:10])
        pnl = float(str(rec.get("pnl") or rec.get("day_pnl") or "0").replace(",", ""))
        name = str(rec.get("name") or rec.get("strategy") or "imported").strip()
        raw_id = str(rec.get("deployment_id") or "").strip()
        dep_id = int(raw_id) if raw_id else -(zlib.crc32(name.encode('utf-8')) % 10_000_000 + 1)
        rows.append(LedgerRow(
            trading_day=day, deployment_id=dep_id, name=name,
            deployment_type=str(rec.get("deployment_type") or "").strip(),
            broker=str(rec.get("broker") or "").strip(),
            currency=str(rec.get("currency") or default_currency).strip().upper() or default_currency,
            paper=False, day_pnl=round(pnl, 2), close_pnl=None, baseline_pnl=None,
            baseline_source="import", snapshots=0, first_ts_ist=None, last_ts_ist=None,
            status_eod="", source="import", notes=str(rec.get("notes") or "").strip(),
        ))
    return rows


__all__ = [
    "derive_day", "group_by_deployment", "import_rows", "previous_close_map",
    "rebuild_ledger", "update_ledger_for_day",
]
