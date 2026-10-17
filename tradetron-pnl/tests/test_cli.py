"""End-to-end CLI runs against fixtures, entirely offline, in temporary directories.

The flag shapes mirror ``.github/workflows/pnl.yml`` and ``tests.yml``.
"""

from __future__ import annotations

import copy
import csv
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tradetron_pnl import cli
from tradetron_pnl.store import DataStore
from tradetron_pnl.trading_calendar import load_holidays

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "deployments.json"
BASE_FIXTURE = json.loads(FIXTURE.read_text(encoding="utf-8"))

CLEAN_ENV = {k: v for k, v in os.environ.items()
             if not k.startswith(("TTPNL_", "TRADETRON_", "TELEGRAM_"))}


def run(*args, env=None) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env or CLEAN_ENV, clear=True), redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli.main(list(args))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            print(exc, file=err)
    return code, out.getvalue(), err.getvalue()


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.reports = self.root / "reports"
        self.fixture = self.root / "fixture.json"
        self.fixture.write_text(json.dumps(BASE_FIXTURE), encoding="utf-8")
        self.config = ROOT / "config"

    def set_pnls(self, pnls: dict, statuses: dict = None):
        payload = copy.deepcopy(BASE_FIXTURE)
        for dep in payload["deployments"]["deployments"]:
            if dep["deployment_id"] in pnls:
                dep["pnl"] = pnls[dep["deployment_id"]]
            if statuses and dep["deployment_id"] in statuses:
                dep["status"] = statuses[dep["deployment_id"]]
        self.fixture.write_text(json.dumps(payload), encoding="utf-8")

    def run_at(self, when: str, *extra, env=None):
        return run("run", "--source", "fixture", "--fixture", str(self.fixture), "--now", when,
                   "--data-dir", str(self.data), "--reports-dir", str(self.reports),
                   "--config-dir", str(self.config), "--env-file", str(self.root / "nope.env"),
                   "--dry-run", *extra, env=env)


class TestRunBasics(CliCase):
    def test_market_closed_is_a_noop(self):
        code, out, _ = self.run_at("2026-10-17T12:00")                     # Saturday
        self.assertEqual(code, 0)
        self.assertIn("market closed (weekend)", out)
        self.assertFalse(self.data.exists())
        code, out, _ = self.run_at("2026-10-20T12:00")                     # Dussehra
        self.assertIn("market closed (Dussehra)", out)

    def test_force_runs_on_a_holiday(self):
        code, out, _ = self.run_at("2026-10-20T12:00", "--force", "--quiet")
        self.assertEqual(code, 0)
        self.assertTrue((self.data / "snapshots" / "2026-10.csv").exists())

    def test_preopen_is_silent_by_default_and_stores_a_baseline(self):
        code, out, err = self.run_at("2026-10-16T09:00")
        self.assertEqual(code, 0, err)
        self.assertNotIn("--- telegram [preopen]", out)
        self.assertIn("--- telegram [error]", out)                          # 1003 is in Error-Execution
        snaps = DataStore(self.data).read_snapshots()
        self.assertEqual(len(snaps), 4)
        self.assertTrue(all(s.phase == "pre_open" and s.run_kind == "preopen" for s in snaps))
        self.assertTrue((self.data / "positions" / "2026-10-16.jsonl").exists())
        self.assertTrue((self.data / "deployments_seen.csv").exists())

    def test_preopen_message_can_be_enabled(self):
        env = dict(CLEAN_ENV, TTPNL_PREOPEN_MESSAGE="1")
        code, out, _ = self.run_at("2026-10-16T09:00", env=env)
        self.assertIn("--- telegram [preopen]", out)
        self.assertIn("🌅 <b>Pre-open</b>", out)

    def test_hourly_update_shows_day_pnl_against_preopen_baseline(self):
        self.run_at("2026-10-16T09:00", "--quiet")
        self.set_pnls({1001: 5250.5, 1002: -900, 1004: 1500})
        code, out, err = self.run_at("2026-10-16T11:00")
        self.assertEqual(code, 0, err)
        self.assertIn("--- telegram [hourly]", out)
        self.assertIn("Today <b>+₹1,300</b> ▲", out)                        # +1000 + 300, paper ignored
        self.assertIn("paper (1): +₹510 — not counted", out)
        ledger = DataStore(self.data).read_ledger()
        row = {r.deployment_id: r for r in ledger}
        self.assertEqual((row[1001].day_pnl, row[1001].baseline_source), (1000.0, "preopen"))
        self.assertEqual(row[1002].day_pnl, 300.0)
        self.assertTrue(row[1004].paper)

    def test_today_semantics_uses_api_value_directly(self):
        env = dict(CLEAN_ENV, TTPNL_PNL_SEMANTICS="today")
        self.run_at("2026-10-16T09:00", "--quiet", env=env)
        code, out, _ = self.run_at("2026-10-16T11:00", env=env)
        self.assertIn("Today <b>+₹3,051</b>", out)                           # 4250.5 - 1200 + 0

    def test_no_positions_and_no_reports_flags(self):
        code, _, _ = self.run_at("2026-10-16T11:00", "--no-positions", "--no-reports", "--quiet")
        self.assertEqual(code, 0)
        self.assertFalse((self.data / "positions").exists())
        self.assertFalse(self.reports.exists())

    def test_reports_are_written_and_self_contained(self):
        code, _, _ = self.run_at("2026-10-16T11:00", "--quiet")
        self.assertEqual(code, 0)
        md = (self.reports / "latest.md").read_text(encoding="utf-8")
        self.assertIn("# Tradetron P&L — 16 Oct 2026, 11:00 IST", md)
        self.assertIn("| Nifty Weekly Straddle | 1001 |", md)
        html = (self.reports / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("<!doctype html>", html)
        self.assertNotRegex(html, r'(src|href)="https?://')
        self.assertIn('name="robots" content="noindex"', html)
        summary = json.loads((self.reports / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["fy"], "FY2026-27")
        self.assertEqual(summary["errors"], [1003])
        self.assertTrue((self.reports / "tax" / "FY2026-27.md").exists())
        self.assertTrue((self.reports / "tax" / "FY2026-27.csv").exists())

    def test_invalid_setting_is_a_config_error(self):
        env = dict(CLEAN_ENV, TTPNL_PNL_SEMANTICS="nope")
        code, _, err = self.run_at("2026-10-16T11:00", env=env)
        self.assertEqual(code, cli.EXIT_CONFIG)
        self.assertIn("TTPNL_PNL_SEMANTICS", err)

    def test_fixture_source_requires_a_file(self):
        code, _, err = run("run", "--source", "fixture", "--now", "2026-10-16T11:00",
                           "--data-dir", str(self.data), "--reports-dir", str(self.reports),
                           "--config-dir", str(self.config), "--dry-run")
        self.assertNotEqual(code, 0)
        self.assertIn("--fixture", err)

    def test_missing_fixture_file_is_reported_as_fetch_failure(self):
        self.fixture.unlink()
        code, out, err = self.run_at("2026-10-16T11:00")
        self.assertEqual(code, cli.EXIT_FETCH_FAILED)
        self.assertIn("--- telegram [fetch_failure]", out)
        self.assertIn("FAILED", out)
        state = DataStore(self.data).load_state()
        self.assertIn("last_error", state)

    def test_api_source_without_key_fails_cleanly(self):
        code, out, err = run("run", "--now", "2026-10-16T11:00", "--data-dir", str(self.data),
                             "--reports-dir", str(self.reports), "--config-dir", str(self.config),
                             "--env-file", str(self.root / "nope.env"), "--dry-run")
        self.assertEqual(code, cli.EXIT_FETCH_FAILED)
        self.assertIn("TRADETRON_API_KEY is not set", out + err)


class TestKinds(CliCase):
    def test_resolve_kind(self):
        from datetime import date
        day = date(2026, 10, 16)
        self.assertEqual(cli.resolve_kind("auto", "pre_open", {}, day), "preopen")
        self.assertEqual(cli.resolve_kind("auto", "open", {}, day), "hourly")
        self.assertEqual(cli.resolve_kind("auto", "post_close", {}, day), "eod")
        self.assertEqual(cli.resolve_kind("auto", "post_close", {"last_eod_day": "2026-10-16"}, day), "silent")
        self.assertEqual(cli.resolve_kind("auto", "closed", {}, day), "silent")
        self.assertEqual(cli.resolve_kind("hourly", "post_close", {}, day), "hourly")

    def test_eod_on_month_end_friday_sends_weekly_and_monthly(self):
        self.run_at("2026-10-30T09:00", "--quiet")
        self.set_pnls({1001: 6000, 1002: -1500})
        code, out, err = self.run_at("2026-10-30T15:40")
        self.assertEqual(code, 0, err)
        self.assertIn("--- telegram [eod]", out)
        self.assertIn("--- telegram [weekly]", out)
        self.assertIn("--- telegram [monthly]", out)
        self.assertNotIn("--- telegram [quarter]", out)
        self.assertIn("Day <b>+₹1,450</b>", out)

    def test_quarter_digest_on_last_trading_day_of_quarter(self):
        self.run_at("2026-12-31T09:00", "--quiet")
        code, out, _ = self.run_at("2026-12-31T15:40")
        self.assertIn("--- telegram [quarter]", out)
        self.assertIn("Q3 FY2026-27 closed", out)

    def test_explicit_kind_overrides_clock(self):
        code, out, _ = self.run_at("2026-10-16T11:00", "--kind", "eod")
        self.assertIn("--- telegram [eod]", out)
        code, out, _ = self.run_at("2026-10-16T11:00", "--kind", "silent", "--quiet")
        self.assertNotIn("--- telegram [hourly]", out)
        self.assertNotIn("--- telegram [eod]", out)
        self.assertIn("--- telegram [error]", out)                        # alerts are never suppressed


class TestTelegramDelivery(CliCase):
    def test_sends_through_bot_and_records_state(self):
        env = dict(CLEAN_ENV, TELEGRAM_BOT_TOKEN="1:x", TELEGRAM_CHAT_ID="7")
        sent = []

        class FakeBot:
            def __init__(self, *a, **k):
                pass

            def send(self, text, **k):
                sent.append(text)
                return [len(sent)]

        with mock.patch.object(cli, "TelegramBot", FakeBot):
            code, out, err = run("run", "--source", "fixture", "--fixture", str(self.fixture),
                                 "--now", "2026-10-16T15:40", "--data-dir", str(self.data),
                                 "--reports-dir", str(self.reports), "--config-dir", str(self.config),
                                 "--env-file", str(self.root / "nope.env"), "--quiet", env=env)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(sent), 3)                          # error alert + EOD + weekly (16 Oct is a Friday)
        self.assertTrue(sent[0].startswith("🚨"))
        self.assertTrue(sent[1].startswith("🧾 <b>EOD"))
        self.assertTrue(sent[2].startswith("📅"))
        state = DataStore(self.data).load_state()
        self.assertEqual(state["last_eod_day"], "2026-10-16")
        self.assertEqual(state["error_alerted"]["2026-10-16"], [1003])

        # a second post-close run the same day is silent and does not repeat the alert
        with mock.patch.object(cli, "TelegramBot", FakeBot):
            code, out, _ = run("run", "--source", "fixture", "--fixture", str(self.fixture),
                               "--now", "2026-10-16T16:30", "--data-dir", str(self.data),
                               "--reports-dir", str(self.reports), "--config-dir", str(self.config),
                               "--env-file", str(self.root / "nope.env"), "--quiet", env=env)
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 3)
        self.assertIn("silent", out)
        self.assertEqual(state["last_weekly_day"], "2026-10-16")

    def test_silent_hourly_flag(self):
        env = dict(CLEAN_ENV, TELEGRAM_BOT_TOKEN="1:x", TELEGRAM_CHAT_ID="7", TTPNL_SILENT_HOURLY="1")
        sent = []

        class FakeBot:
            def __init__(self, *a, **k):
                pass

            def send(self, text, **k):
                sent.append((text[:1], k.get("silent")))
                return [1]

        with mock.patch.object(cli, "TelegramBot", FakeBot):
            code, _, _ = run("run", "--source", "fixture", "--fixture", str(self.fixture),
                             "--now", "2026-10-16T11:00", "--data-dir", str(self.data),
                             "--reports-dir", str(self.reports), "--config-dir", str(self.config),
                             "--env-file", str(self.root / "nope.env"), "--quiet", env=env)
        self.assertEqual(code, 0)
        self.assertEqual(sent, [("🚨", False), ("📊", True)])

    def test_telegram_failure_keeps_data_and_returns_distinct_code(self):
        env = dict(CLEAN_ENV, TELEGRAM_BOT_TOKEN="1:x", TELEGRAM_CHAT_ID="7")
        from tradetron_pnl.telegram import TelegramError

        class BrokenBot:
            def __init__(self, *a, **k):
                pass

            def send(self, text, **k):
                raise TelegramError("chat not found", status=400)

        with mock.patch.object(cli, "TelegramBot", BrokenBot):
            code, out, err = run("run", "--source", "fixture", "--fixture", str(self.fixture),
                                 "--now", "2026-10-16T11:00", "--data-dir", str(self.data),
                                 "--reports-dir", str(self.reports), "--config-dir", str(self.config),
                                 "--env-file", str(self.root / "nope.env"), "--quiet", env=env)
        self.assertEqual(code, cli.EXIT_TELEGRAM_FAILED)
        self.assertIn("::warning title=Telegram::", err)
        self.assertTrue((self.data / "ledger.csv").exists())
        self.assertNotIn("last_eod_day", DataStore(self.data).load_state())

    def test_unconfigured_telegram_prints_instead(self):
        code, out, err = run("run", "--source", "fixture", "--fixture", str(self.fixture),
                             "--now", "2026-10-16T11:00", "--data-dir", str(self.data),
                             "--reports-dir", str(self.reports), "--config-dir", str(self.config),
                             "--env-file", str(self.root / "nope.env"), "--quiet")
        self.assertEqual(code, 0)
        self.assertIn("Telegram is not configured", err)
        self.assertIn("--- telegram [hourly]", out)


class TestOtherCommands(CliCase):
    def common(self):
        return ("--data-dir", str(self.data), "--reports-dir", str(self.reports),
                "--config-dir", str(self.config), "--env-file", str(self.root / "nope.env"))

    def test_probe_with_fixture_and_save(self):
        target = self.root / "captured.json"
        code, out, _ = run("probe", "--source", "fixture", "--fixture", str(self.fixture),
                           "--positions", "1", "--notifications", "--save-fixture", str(target), *self.common())
        self.assertEqual(code, 0)
        self.assertIn("4 deployment record(s) from fixture", out)
        self.assertIn('"deployment_id": 1001', out)
        self.assertIn("TTPNL_PNL_SEMANTICS", out)
        saved = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["deployments"]), 4)
        self.assertIn("1001", saved["positions"])
        # the saved file is itself a valid fixture
        code, _, _ = run("run", "--source", "fixture", "--fixture", str(target), "--now", "2026-10-16T11:00",
                         "--dry-run", "--quiet", *self.common())
        self.assertEqual(code, 0)

    def test_report_rebuilds_from_stored_data(self):
        self.run_at("2026-10-16T09:00", "--quiet", "--no-reports")
        self.set_pnls({1001: 5250.5})
        self.run_at("2026-10-16T15:40", "--quiet", "--no-reports")
        self.assertFalse(self.reports.exists())
        code, out, _ = run("report", "--now", "2026-10-18T10:00", *self.common())
        self.assertEqual(code, 0)
        self.assertIn("reports rebuilt as of 2026-10-16", out)
        md = (self.reports / "latest.md").read_text(encoding="utf-8")
        self.assertIn("16 Oct 2026, 15:30 IST", md)
        self.assertIn("+₹1,000", md)

    def test_report_without_data(self):
        code, out, _ = run("report", *self.common())
        self.assertEqual(code, cli.EXIT_CONFIG)
        self.assertIn("no snapshots", out)

    def test_tax_command(self):
        self.run_at("2026-10-16T09:00", "--quiet")
        self.set_pnls({1001: 5250.5})
        self.run_at("2026-10-16T15:40", "--quiet")
        code, out, _ = run("tax", "--fy", "FY2026-27", "--as-of", "2026-10-16", "--rate", "0.3", "--stdout", *self.common())
        self.assertEqual(code, 0)
        self.assertIn("# Tax workings — FY2026-27", out)
        self.assertIn("Indicative cumulative tax", out)
        code, out, _ = run("tax", "--fy", "fy2026-27", *self.common())
        self.assertEqual(code, 0)
        self.assertIn("FY2026-27 net +₹1,000", out)
        self.assertTrue((self.reports / "tax" / "FY2026-27.csv").exists())

    def test_import_then_rebuild_keeps_history(self):
        path = self.root / "old.csv"
        path.write_text("Date,Strategy,Net P&L\n2026-09-01,Old Straddle,\"1,500\"\n2026-09-02,Old Straddle,-250\n\n",
                        encoding="utf-8")
        code, out, _ = run("import", str(path), "--date-col", "Date", "--pnl-col", "Net P&L",
                           "--name-col", "Strategy", *self.common())
        self.assertEqual(code, 0, out)
        self.assertIn("imported 2 row(s)", out)
        self.run_at("2026-10-16T09:00", "--quiet")
        self.set_pnls({1001: 4000.5})
        self.run_at("2026-10-16T15:40", "--quiet")
        code, out, _ = run("rebuild", *self.common())
        self.assertEqual(code, 0)
        self.assertIn("2026-09-01 → 2026-10-16", out)
        with (self.data / "ledger.csv").open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual([r["source"] for r in rows][:2], ["import", "import"])
        self.assertEqual(len(rows), 6)
        code, out, _ = run("import", str(path), "--date-col", "Wrong", *self.common())
        self.assertEqual(code, 0)                        # no usable rows -> nothing imported
        self.assertIn("imported 0 row(s)", out)

    def test_import_missing_file(self):
        code, out, _ = run("import", str(self.root / "missing.csv"), *self.common())
        self.assertEqual(code, cli.EXIT_CONFIG)

    def test_send_requires_configuration(self):
        code, out, _ = run("send", *self.common())
        self.assertEqual(code, cli.EXIT_CONFIG)
        self.assertIn("TELEGRAM_BOT_TOKEN", out)

    def test_version_flag(self):
        code, out, err = run("--version")
        self.assertEqual(code, 0)
        self.assertIn("tradetron-pnl", out + err)


class TestDeploymentTags(CliCase):
    def test_alias_segment_and_exclusion_flow_through(self):
        cfg = self.root / "config"
        cfg.mkdir()
        (cfg / "holidays_nse.csv").write_text((ROOT / "config" / "holidays_nse.csv").read_text(encoding="utf-8"))
        (cfg / "deployments.csv").write_text(
            "deployment_id,alias,segment,include,notes\n1001,Straddle,fno,1,\n1002,,intraday_equity,0,skip\n",
            encoding="utf-8")
        self.config = cfg
        self.run_at("2026-10-16T09:00", "--quiet")
        self.set_pnls({1001: 5250.5, 1002: -900})
        code, out, _ = self.run_at("2026-10-16T15:40")
        self.assertEqual(code, 0)
        self.assertIn("Straddle  <b>+₹1,000</b>", out)
        self.assertIn("Day <b>+₹1,000</b>", out)                           # 1002 excluded
        self.assertNotIn("BankNifty Iron Condor  <b>", out)
        md = (self.reports / "tax" / "FY2026-27.md").read_text(encoding="utf-8")
        self.assertIn("Excluded by config (`include=0`): 1002", md)
        self.assertIn("| Straddle (`1001`) | fno |", md)


class TestHolidayCoverageNote(CliCase):
    def test_warns_when_year_is_missing_from_holiday_file(self):
        cfg = self.root / "config"
        cfg.mkdir()
        (cfg / "holidays_nse.csv").write_text("date,name\n2026-01-26,Republic Day\n", encoding="utf-8")
        self.config = cfg
        code, out, _ = self.run_at("2027-04-05T11:00")
        self.assertEqual(code, 0)
        self.assertIn("no entries for 2027", out)


if __name__ == "__main__":
    unittest.main()
