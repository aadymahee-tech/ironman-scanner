"""CSV / JSONL storage round-trips."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from tradetron_pnl.api import Deployment
from tradetron_pnl.store import LEDGER_COLUMNS, SNAPSHOT_COLUMNS, DataStore, LedgerRow, Snapshot
from tradetron_pnl.trading_calendar import IST


def dep(dep_id=1001, pnl=100.0, **kw) -> Deployment:
    base = dict(deployment_id=dep_id, name=f"Dep {dep_id}", deployment_type="LIVE AUTO", status="Active",
                broker="Zerodha", multiplier=1.0, capital=1e5, pnl=pnl, open_positions=1,
                total_positions=2, currency="INR", deployed_on="2026-10-01")
    base.update(kw)
    return Deployment(**base)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=IST)


def ledger_row(day: date, dep_id: int, pnl: float, source="api", close=None) -> LedgerRow:
    return LedgerRow(trading_day=day, deployment_id=dep_id, name=f"Dep {dep_id}", deployment_type="LIVE AUTO",
                     broker="Z", currency="INR", paper=False, day_pnl=pnl, close_pnl=close,
                     baseline_pnl=None, baseline_source="test", snapshots=1, first_ts_ist=None,
                     last_ts_ist=None, status_eod="Active", source=source)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DataStore(Path(self.tmp.name) / "data")


class TestSnapshots(StoreCase):
    def test_round_trip_and_monthly_files(self):
        s1 = Snapshot.from_deployment(dep(), now=at("2026-10-16T09:00"), phase="pre_open", run_kind="preopen", source="api")
        s2 = Snapshot.from_deployment(dep(pnl=250.25), now=at("2026-11-02T12:00"), phase="open", run_kind="hourly", source="api")
        self.store.append_snapshots([s1])
        self.store.append_snapshots([s2])
        self.assertTrue((self.store.snapshots_dir / "2026-10.csv").exists())
        self.assertTrue((self.store.snapshots_dir / "2026-11.csv").exists())
        rows = self.store.read_snapshots()
        self.assertEqual(len(rows), 2)
        back = rows[0]
        self.assertEqual(back.ts_utc.isoformat(), "2026-10-16T03:30:00+00:00")
        self.assertEqual(back.ts_ist.isoformat(), "2026-10-16T09:00:00+05:30")
        self.assertEqual(back.trading_day, date(2026, 10, 16))
        self.assertEqual(back.pnl, 100.0)
        self.assertTrue(back.pnl_known)
        self.assertFalse(back.paper)
        self.assertEqual(back.open_positions, 1)
        self.assertEqual(rows[1].pnl, 250.25)

    def test_range_filters(self):
        for day in ("2026-10-15", "2026-10-16", "2026-11-02"):
            self.store.append_snapshots([Snapshot.from_deployment(
                dep(), now=at(f"{day}T12:00"), phase="open", run_kind="hourly", source="api")])
        self.assertEqual(len(self.store.snapshots_for_day(date(2026, 10, 16))), 1)
        self.assertEqual(len(self.store.read_snapshots(start=date(2026, 10, 16))), 2)
        self.assertEqual(len(self.store.read_snapshots(end=date(2026, 10, 15))), 1)
        self.assertEqual(self.store.snapshot_days(), [date(2026, 10, 15), date(2026, 10, 16), date(2026, 11, 2)])

    def test_header_written_once_and_unix_newlines(self):
        snap = Snapshot.from_deployment(dep(), now=at("2026-10-16T09:00"), phase="pre_open", run_kind="preopen", source="api")
        self.store.append_snapshots([snap])
        self.store.append_snapshots([snap])
        raw = self.store.snapshot_file(date(2026, 10, 16)).read_bytes()
        self.assertEqual(raw.count(b"ts_utc,"), 1)
        self.assertNotIn(b"\r\n", raw)
        self.assertEqual(raw.decode().splitlines()[0], ",".join(SNAPSHOT_COLUMNS))

    def test_empty_append_is_an_error(self):
        with self.assertRaises(ValueError):
            self.store.append_snapshots([])

    def test_none_fields_survive_round_trip(self):
        snap = Snapshot.from_deployment(dep(multiplier=None, capital=None, open_positions=None, total_positions=None,
                                            pnl_known=False), now=at("2026-10-16T09:00"), phase="pre_open",
                                        run_kind="preopen", source="api")
        self.store.append_snapshots([snap])
        back = self.store.read_snapshots()[0]
        self.assertIsNone(back.multiplier)
        self.assertIsNone(back.open_positions)
        self.assertFalse(back.pnl_known)


class TestLedger(StoreCase):
    def test_upsert_replaces_same_key_and_sorts(self):
        d = date(2026, 10, 16)
        self.store.upsert_ledger([ledger_row(d, 2, 5.0), ledger_row(d, 1, 1.0)])
        self.store.upsert_ledger([ledger_row(d, 1, 9.0), ledger_row(date(2026, 10, 15), 1, 3.0)])
        rows = self.store.read_ledger()
        self.assertEqual([(r.trading_day.day, r.deployment_id, r.day_pnl) for r in rows],
                         [(15, 1, 3.0), (16, 1, 9.0), (16, 2, 5.0)])
        header = self.store.ledger_path.read_text().splitlines()[0]
        self.assertEqual(header, ",".join(LEDGER_COLUMNS))

    def test_empty_ledger_reads_as_empty(self):
        self.assertEqual(self.store.read_ledger(), [])

    def test_optional_fields_round_trip(self):
        row = ledger_row(date(2026, 10, 16), 1, -12.5, close=88.0)
        row.first_ts_ist = at("2026-10-16T09:00")
        row.notes = "a; b"
        self.store.write_ledger([row])
        back = self.store.read_ledger()[0]
        self.assertEqual(back.close_pnl, 88.0)
        self.assertIsNone(back.baseline_pnl)
        self.assertEqual(back.first_ts_ist, at("2026-10-16T09:00"))
        self.assertIsNone(back.last_ts_ist)
        self.assertEqual(back.notes, "a; b")


class TestPositionsRegistryState(StoreCase):
    def test_positions_jsonl(self):
        day = date(2026, 10, 16)
        self.assertIsNone(self.store.append_positions(day, []))
        self.store.append_positions(day, [{"deployment_id": 1, "positions": [{"x": 1}]}])
        self.store.append_positions(day, [{"deployment_id": 2, "positions": []}])
        rows = self.store.read_positions(day)
        self.assertEqual([r["deployment_id"] for r in rows], [1, 2])
        self.assertEqual(self.store.read_positions(date(2026, 1, 1)), [])

    def test_registry_tracks_first_and_last_seen(self):
        new = self.store.update_registry([dep(1), dep(2)], at("2026-10-15T09:00"))
        self.assertEqual(new, [1, 2])
        new = self.store.update_registry([dep(2, status="Exited"), dep(3)], at("2026-10-16T09:00"))
        self.assertEqual(new, [3])
        reg = self.store.read_registry()
        self.assertEqual(set(reg), {1, 2, 3})
        self.assertEqual(reg[2]["first_seen"], "2026-10-15T09:00:00+05:30")
        self.assertEqual(reg[2]["last_seen"], "2026-10-16T09:00:00+05:30")
        self.assertEqual(reg[2]["last_status"], "Exited")
        self.assertEqual(reg[1]["last_seen"], "2026-10-15T09:00:00+05:30")

    def test_state_round_trip(self):
        self.assertEqual(self.store.load_state(), {})
        self.store.save_state({"last_eod_day": "2026-10-16", "error_alerted": {"2026-10-16": [1003]}})
        self.assertEqual(self.store.load_state()["error_alerted"], {"2026-10-16": [1003]})


if __name__ == "__main__":
    unittest.main()
