"""Daily P&L derivation: baselines, semantics, resets, rebuilds and imports."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from tradetron_pnl import ledger
from tradetron_pnl.api import Deployment
from tradetron_pnl.store import DataStore, Snapshot
from tradetron_pnl.trading_calendar import IST, load_holidays

ROOT = Path(__file__).resolve().parents[1]
HOLIDAYS = load_holidays(ROOT / "config" / "holidays_nse.csv")


def snap(dep_id: int, when: str, pnl: float, *, deployed_on="2026-09-01", status="Active",
         paper=False, pnl_known=True) -> Snapshot:
    now = datetime.fromisoformat(when).replace(tzinfo=IST)
    clock = now.time()
    phase = "pre_open" if clock < datetime.min.time().replace(hour=9, minute=15) else (
        "open" if clock < datetime.min.time().replace(hour=15, minute=30) else "post_close")
    dep = Deployment(deployment_id=dep_id, name=f"Dep {dep_id}", deployment_type="LIVE OFFLINE" if paper else "LIVE AUTO",
                     status=status, broker="Z", pnl=pnl, pnl_known=pnl_known, currency="INR", deployed_on=deployed_on)
    return Snapshot.from_deployment(dep, now=now, phase=phase, run_kind="test", source="api")


class TestDeriveDay(unittest.TestCase):
    day = date(2026, 10, 16)

    def test_preopen_baseline_is_preferred(self):
        rows = ledger.derive_day(self.day, [
            snap(1, "2026-10-16T09:00", 1000), snap(1, "2026-10-16T12:00", 1400), snap(1, "2026-10-16T15:40", 1250.5),
        ], prev_close={1: 999.0})
        row = rows[0]
        self.assertEqual((row.baseline_source, row.baseline_pnl, row.close_pnl, row.day_pnl), ("preopen", 1000.0, 1250.5, 250.5))
        self.assertEqual(row.snapshots, 3)
        self.assertEqual(row.first_ts_ist.hour, 9)
        self.assertEqual(row.last_ts_ist.hour, 15)
        self.assertIn("reset or redeploy", row.notes)      # 999 != 1000 is flagged

    def test_preopen_matching_previous_close_has_no_note(self):
        rows = ledger.derive_day(self.day, [snap(1, "2026-10-16T09:00", 1000), snap(1, "2026-10-16T15:40", 1100)],
                                 prev_close={1: 1000.0})
        self.assertEqual(rows[0].notes, "")

    def test_prev_close_when_preopen_run_was_missed(self):
        rows = ledger.derive_day(self.day, [snap(1, "2026-10-16T11:00", 1400), snap(1, "2026-10-16T15:40", 1300)],
                                 prev_close={1: 1000.0})
        self.assertEqual((rows[0].baseline_source, rows[0].day_pnl), ("prev_close", 300.0))

    def test_new_deployment_starts_from_zero(self):
        rows = ledger.derive_day(self.day, [snap(1, "2026-10-16T11:00", 400, deployed_on="2026-10-16")], prev_close={})
        self.assertEqual((rows[0].baseline_source, rows[0].day_pnl), ("new_deployment", 400.0))

    def test_partial_when_nothing_better_exists(self):
        rows = ledger.derive_day(self.day, [snap(1, "2026-10-16T11:00", 400), snap(1, "2026-10-16T15:40", 700)], prev_close={})
        self.assertEqual((rows[0].baseline_source, rows[0].day_pnl), ("partial", 300.0))
        self.assertIn("understated", rows[0].notes)

    def test_today_semantics_uses_close_directly(self):
        rows = ledger.derive_day(self.day, [snap(1, "2026-10-16T09:00", 5000), snap(1, "2026-10-16T15:40", 700)],
                                 prev_close={1: 5000.0}, semantics="today")
        self.assertEqual((rows[0].baseline_source, rows[0].baseline_pnl, rows[0].day_pnl), ("today", 0.0, 700.0))

    def test_unknown_semantics_rejected(self):
        with self.assertRaises(ValueError):
            ledger.derive_day(self.day, [], {}, semantics="weekly")

    def test_other_days_snapshots_are_ignored_and_rows_sorted(self):
        rows = ledger.derive_day(self.day, [
            snap(2, "2026-10-16T09:00", 0), snap(1, "2026-10-16T09:00", 0),
            snap(1, "2026-10-15T15:40", 999), snap(1, "2026-10-16T15:40", 10), snap(2, "2026-10-16T15:40", -5),
        ], prev_close={})
        self.assertEqual([(r.deployment_id, r.day_pnl) for r in rows], [(1, 10.0), (2, -5.0)])

    def test_paper_flag_and_status_carry_through(self):
        rows = ledger.derive_day(self.day, [snap(7, "2026-10-16T09:00", 0, paper=True, status="Live-Entered")], prev_close={})
        self.assertTrue(rows[0].paper)
        self.assertEqual(rows[0].status_eod, "Live-Entered")
        self.assertEqual(rows[0].deployment_type, "LIVE OFFLINE")

    def test_missing_pnl_is_noted(self):
        rows = ledger.derive_day(self.day, [snap(1, "2026-10-16T09:00", 0, pnl_known=False)], prev_close={})
        self.assertIn("pnl missing", rows[0].notes)


class TestPreviousClose(unittest.TestCase):
    def test_looks_back_across_holidays_and_missed_days(self):
        rows = ledger.derive_day(date(2026, 10, 16), [snap(1, "2026-10-16T09:00", 0), snap(1, "2026-10-16T15:40", 50)], {})
        rows += ledger.derive_day(date(2026, 10, 19), [snap(2, "2026-10-19T09:00", 0), snap(2, "2026-10-19T15:40", 70)], {})
        # 20 Oct is Dussehra; 21 Oct looks back to 19 Oct (dep 2) and 16 Oct (dep 1)
        prev = ledger.previous_close_map(rows, date(2026, 10, 21), HOLIDAYS)
        self.assertEqual(prev, {2: 70.0, 1: 50.0})

    def test_gives_up_after_max_gap(self):
        rows = ledger.derive_day(date(2026, 9, 1), [snap(1, "2026-09-01T09:00", 0), snap(1, "2026-09-01T15:40", 50)], {})
        self.assertEqual(ledger.previous_close_map(rows, date(2026, 10, 16), HOLIDAYS, max_gap_days=5), {})

    def test_rows_on_or_after_day_are_ignored(self):
        rows = ledger.derive_day(date(2026, 10, 16), [snap(1, "2026-10-16T15:40", 50)], {})
        self.assertEqual(ledger.previous_close_map(rows, date(2026, 10, 16), HOLIDAYS), {})


class TestStoreIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DataStore(Path(self.tmp.name))

    def test_update_is_idempotent_and_uses_previous_day(self):
        self.store.append_snapshots([snap(1, "2026-10-15T09:00", 100), snap(1, "2026-10-15T15:40", 400)])
        ledger.update_ledger_for_day(self.store, date(2026, 10, 15), HOLIDAYS)
        self.store.append_snapshots([snap(1, "2026-10-16T11:00", 450)])
        ledger.update_ledger_for_day(self.store, date(2026, 10, 16), HOLIDAYS)
        self.store.append_snapshots([snap(1, "2026-10-16T15:40", 250)])
        rows = ledger.update_ledger_for_day(self.store, date(2026, 10, 16), HOLIDAYS)
        self.assertEqual((rows[0].baseline_source, rows[0].day_pnl), ("prev_close", -150.0))
        self.assertEqual(len(self.store.read_ledger()), 2)
        self.assertEqual(ledger.update_ledger_for_day(self.store, date(2026, 10, 19), HOLIDAYS), [])

    def test_rebuild_recomputes_and_keeps_imported(self):
        self.store.append_snapshots([snap(1, "2026-10-15T09:00", 100), snap(1, "2026-10-15T15:40", 400)])
        self.store.append_snapshots([snap(1, "2026-10-16T11:00", 450), snap(1, "2026-10-16T15:40", 250)])
        imported = ledger.import_rows([{"date": "2026-10-01", "pnl": "12.5", "name": "Old strat"}])
        self.store.upsert_ledger(imported)
        rows = ledger.rebuild_ledger(self.store, HOLIDAYS, semantics="cumulative")
        self.assertEqual([(r.trading_day.day, r.day_pnl, r.source) for r in rows],
                         [(1, 12.5, "import"), (15, 300.0, "api"), (16, -150.0, "api")])
        rows = ledger.rebuild_ledger(self.store, HOLIDAYS, semantics="today", keep_imported=False)
        self.assertEqual([(r.trading_day.day, r.day_pnl) for r in rows], [(15, 400.0), (16, 250.0)])

    def test_import_rows_are_deterministic_and_negative_ids(self):
        a = ledger.import_rows([{"date": "2026-10-01", "pnl": "1,000", "name": "Old strat"}])[0]
        b = ledger.import_rows([{"date": "2026-10-02", "pnl": "-5", "name": "Old strat"}])[0]
        self.assertEqual(a.deployment_id, b.deployment_id)
        self.assertLess(a.deployment_id, 0)
        self.assertEqual(a.day_pnl, 1000.0)
        self.assertEqual(a.source, "import")
        c = ledger.import_rows([{"date": "2026-10-01", "pnl": "7", "deployment_id": "1001", "currency": "usd"}])[0]
        self.assertEqual((c.deployment_id, c.currency, c.name), (1001, "USD", "imported"))


if __name__ == "__main__":
    unittest.main()
