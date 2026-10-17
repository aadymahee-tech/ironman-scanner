"""Append-only, git-friendly storage: CSV snapshots, JSONL positions, a daily ledger.

Layout under ``data/``::

    snapshots/YYYY-MM.csv      one row per deployment per run (the raw time series)
    positions/YYYY-MM-DD.jsonl one line per deployment per run (raw position records)
    ledger.csv                 one row per deployment per trading day (derived P&L)
    deployments_seen.csv       registry of every deployment id ever observed
    state.json                 what was sent / done, for idempotency

Everything is plain text so that ``git diff`` on the data is meaningful and a
spreadsheet or pandas can open it directly.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .api import Deployment
from .trading_calendar import IST, UTC

SNAPSHOT_COLUMNS = [
    "ts_utc", "ts_ist", "trading_day", "phase", "run_kind",
    "deployment_id", "name", "deployment_type", "status", "broker",
    "multiplier", "capital", "pnl", "pnl_known", "open_positions", "total_positions",
    "currency", "paper", "deployed_on", "source",
]

LEDGER_COLUMNS = [
    "trading_day", "deployment_id", "name", "deployment_type", "broker", "currency", "paper",
    "day_pnl", "close_pnl", "baseline_pnl", "baseline_source", "snapshots",
    "first_ts_ist", "last_ts_ist", "status_eod", "source", "notes",
]

REGISTRY_COLUMNS = [
    "deployment_id", "name", "deployment_type", "broker", "currency",
    "first_seen", "last_seen", "last_status", "deployed_on",
]


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _opt_float(text: Optional[str]) -> Optional[float]:
    if text is None or str(text).strip() == "":
        return None
    return float(text)


def _opt_int(text: Optional[str]) -> Optional[int]:
    if text is None or str(text).strip() == "":
        return None
    return int(float(text))


def _bool(text: Optional[str]) -> bool:
    return str(text or "").strip().lower() in ("1", "true", "yes")


# --------------------------------------------------------------------------- #
# Row types
# --------------------------------------------------------------------------- #
@dataclass
class Snapshot:
    ts_utc: datetime
    ts_ist: datetime
    trading_day: date
    phase: str
    run_kind: str
    deployment_id: int
    name: str
    deployment_type: str
    status: str
    broker: str
    multiplier: Optional[float]
    capital: Optional[float]
    pnl: float
    pnl_known: bool
    open_positions: Optional[int]
    total_positions: Optional[int]
    currency: str
    paper: bool
    deployed_on: str
    source: str

    @classmethod
    def from_deployment(cls, dep: Deployment, *, now: datetime, phase: str,
                        run_kind: str, source: str) -> "Snapshot":
        now_ist = now.astimezone(IST)
        return cls(
            ts_utc=now.astimezone(UTC).replace(microsecond=0),
            ts_ist=now_ist.replace(microsecond=0),
            trading_day=now_ist.date(),
            phase=phase,
            run_kind=run_kind,
            deployment_id=dep.deployment_id,
            name=dep.name,
            deployment_type=dep.deployment_type,
            status=dep.status,
            broker=dep.broker,
            multiplier=dep.multiplier,
            capital=dep.capital,
            pnl=round(dep.pnl, 2),
            pnl_known=dep.pnl_known,
            open_positions=dep.open_positions,
            total_positions=dep.total_positions,
            currency=dep.currency,
            paper=dep.paper,
            deployed_on=dep.deployed_on,
            source=source,
        )

    def to_row(self) -> Dict[str, str]:
        return {k: _fmt(v) for k, v in asdict(self).items()}

    @classmethod
    def from_row(cls, row: Dict[str, str]) -> "Snapshot":
        return cls(
            ts_utc=datetime.fromisoformat(row["ts_utc"]),
            ts_ist=datetime.fromisoformat(row["ts_ist"]),
            trading_day=date.fromisoformat(row["trading_day"]),
            phase=row.get("phase", ""),
            run_kind=row.get("run_kind", ""),
            deployment_id=int(row["deployment_id"]),
            name=row.get("name", ""),
            deployment_type=row.get("deployment_type", ""),
            status=row.get("status", ""),
            broker=row.get("broker", ""),
            multiplier=_opt_float(row.get("multiplier")),
            capital=_opt_float(row.get("capital")),
            pnl=float(row.get("pnl") or 0.0),
            pnl_known=_bool(row.get("pnl_known", "1")),
            open_positions=_opt_int(row.get("open_positions")),
            total_positions=_opt_int(row.get("total_positions")),
            currency=row.get("currency") or "INR",
            paper=_bool(row.get("paper")),
            deployed_on=row.get("deployed_on", ""),
            source=row.get("source", ""),
        )


@dataclass
class LedgerRow:
    trading_day: date
    deployment_id: int
    name: str
    deployment_type: str
    broker: str
    currency: str
    paper: bool
    day_pnl: float
    close_pnl: Optional[float]
    baseline_pnl: Optional[float]
    baseline_source: str
    snapshots: int
    first_ts_ist: Optional[datetime]
    last_ts_ist: Optional[datetime]
    status_eod: str
    source: str = "api"
    notes: str = ""

    @property
    def key(self):
        return (self.trading_day, self.deployment_id)

    def to_row(self) -> Dict[str, str]:
        return {k: _fmt(v) for k, v in asdict(self).items()}

    @classmethod
    def from_row(cls, row: Dict[str, str]) -> "LedgerRow":
        return cls(
            trading_day=date.fromisoformat(row["trading_day"]),
            deployment_id=int(row["deployment_id"]),
            name=row.get("name", ""),
            deployment_type=row.get("deployment_type", ""),
            broker=row.get("broker", ""),
            currency=row.get("currency") or "INR",
            paper=_bool(row.get("paper")),
            day_pnl=float(row.get("day_pnl") or 0.0),
            close_pnl=_opt_float(row.get("close_pnl")),
            baseline_pnl=_opt_float(row.get("baseline_pnl")),
            baseline_source=row.get("baseline_source", ""),
            snapshots=int(float(row.get("snapshots") or 0)),
            first_ts_ist=datetime.fromisoformat(row["first_ts_ist"]) if row.get("first_ts_ist") else None,
            last_ts_ist=datetime.fromisoformat(row["last_ts_ist"]) if row.get("last_ts_ist") else None,
            status_eod=row.get("status_eod", ""),
            source=row.get("source") or "api",
            notes=row.get("notes", ""),
        )


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
class DataStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.snapshots_dir = self.root / "snapshots"
        self.positions_dir = self.root / "positions"
        self.ledger_path = self.root / "ledger.csv"
        self.registry_path = self.root / "deployments_seen.csv"
        self.state_path = self.root / "state.json"

    # -- snapshots ---------------------------------------------------------- #
    def snapshot_file(self, day: date) -> Path:
        return self.snapshots_dir / f"{day:%Y-%m}.csv"

    def append_snapshots(self, snapshots: Sequence[Snapshot]) -> Path:
        if not snapshots:
            raise ValueError("nothing to append")
        path = self.snapshot_file(snapshots[0].trading_day)
        _append_csv(path, SNAPSHOT_COLUMNS, [s.to_row() for s in snapshots])
        return path

    def read_snapshots(self, start: Optional[date] = None, end: Optional[date] = None) -> List[Snapshot]:
        out: List[Snapshot] = []
        if not self.snapshots_dir.exists():
            return out
        for path in sorted(self.snapshots_dir.glob("*.csv")):
            month = path.stem                     # YYYY-MM
            if start and month < f"{start:%Y-%m}":
                continue
            if end and month > f"{end:%Y-%m}":
                continue
            for row in _read_csv(path):
                snap = Snapshot.from_row(row)
                if start and snap.trading_day < start:
                    continue
                if end and snap.trading_day > end:
                    continue
                out.append(snap)
        out.sort(key=lambda s: (s.ts_utc, s.deployment_id))
        return out

    def snapshots_for_day(self, day: date) -> List[Snapshot]:
        return self.read_snapshots(day, day)

    def snapshot_days(self) -> List[date]:
        return sorted({s.trading_day for s in self.read_snapshots()})

    # -- positions ---------------------------------------------------------- #
    def positions_file(self, day: date) -> Path:
        return self.positions_dir / f"{day.isoformat()}.jsonl"

    def append_positions(self, day: date, records: Iterable[dict]) -> Optional[Path]:
        records = list(records)
        if not records:
            return None
        path = self.positions_file(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        return path

    def read_positions(self, day: date) -> List[dict]:
        path = self.positions_file(day)
        if not path.exists():
            return []
        out = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    # -- ledger ------------------------------------------------------------- #
    def read_ledger(self) -> List[LedgerRow]:
        rows = [LedgerRow.from_row(r) for r in _read_csv(self.ledger_path)]
        rows.sort(key=lambda r: (r.trading_day, r.deployment_id))
        return rows

    def write_ledger(self, rows: Sequence[LedgerRow]) -> Path:
        ordered = sorted(rows, key=lambda r: (r.trading_day, r.deployment_id))
        _write_csv(self.ledger_path, LEDGER_COLUMNS, [r.to_row() for r in ordered])
        return self.ledger_path

    def upsert_ledger(self, new_rows: Sequence[LedgerRow]) -> List[LedgerRow]:
        """Replace rows with the same (day, deployment) key; keep everything else."""
        table = {r.key: r for r in self.read_ledger()}
        for row in new_rows:
            table[row.key] = row
        merged = list(table.values())
        self.write_ledger(merged)
        return sorted(merged, key=lambda r: (r.trading_day, r.deployment_id))

    # -- registry ----------------------------------------------------------- #
    def read_registry(self) -> Dict[int, Dict[str, str]]:
        return {int(r["deployment_id"]): r for r in _read_csv(self.registry_path)}

    def update_registry(self, deployments: Sequence[Deployment], now: datetime) -> List[int]:
        """Record first/last sighting of each deployment. Returns ids seen for the first time."""
        registry = self.read_registry()
        stamp = now.astimezone(IST).isoformat(timespec="seconds")
        new_ids: List[int] = []
        for dep in deployments:
            entry = registry.get(dep.deployment_id)
            if entry is None:
                new_ids.append(dep.deployment_id)
                entry = {"deployment_id": str(dep.deployment_id), "first_seen": stamp}
                registry[dep.deployment_id] = entry
            entry.update({
                "name": dep.name,
                "deployment_type": dep.deployment_type,
                "broker": dep.broker,
                "currency": dep.currency,
                "last_seen": stamp,
                "last_status": dep.status,
                "deployed_on": dep.deployed_on,
            })
        ordered = [registry[k] for k in sorted(registry)]
        _write_csv(self.registry_path, REGISTRY_COLUMNS, ordered)
        return new_ids

    # -- state -------------------------------------------------------------- #
    def load_state(self) -> dict:
        if not self.state_path.exists():
            return {}
        with self.state_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def save_state(self, state: dict) -> Path:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.state_path.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True, default=str)
            fh.write("\n")
        return self.state_path


# --------------------------------------------------------------------------- #
# CSV helpers
# --------------------------------------------------------------------------- #
def _read_csv(path: Path) -> List[Dict[str, str]]:
    path = Path(path)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def _write_csv(path: Path, columns: Sequence[str], rows: Iterable[Dict[str, str]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})


def _append_csv(path: Path, columns: Sequence[str], rows: Iterable[Dict[str, str]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore", lineterminator="\n")
        if new_file:
            writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})


__all__ = [
    "DataStore", "LedgerRow", "Snapshot",
    "SNAPSHOT_COLUMNS", "LEDGER_COLUMNS", "REGISTRY_COLUMNS",
]

# keep dataclass field order == column order (guards against drift in edits)
assert [f.name for f in fields(Snapshot)] == SNAPSHOT_COLUMNS
assert [f.name for f in fields(LedgerRow)] == LEDGER_COLUMNS
