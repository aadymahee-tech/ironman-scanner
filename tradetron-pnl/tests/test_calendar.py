"""Trading calendar: sessions, holidays, trading-day arithmetic, financial-year helpers."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from tradetron_pnl import trading_calendar as tc

ROOT = Path(__file__).resolve().parents[1]
HOLIDAYS = tc.load_holidays(ROOT / "config" / "holidays_nse.csv")


class TestHolidayFile(unittest.TestCase):
    def test_committed_file_parses_and_covers_2026(self):
        self.assertTrue(tc.holiday_coverage(HOLIDAYS, 2026))
        self.assertEqual(HOLIDAYS[date(2026, 10, 20)], "Dussehra")
        self.assertEqual(len([d for d in HOLIDAYS if d.year == 2026]), 16)

    def test_every_committed_holiday_is_a_weekday(self):
        for day in HOLIDAYS:
            self.assertLess(day.weekday(), 5, day)

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(tc.load_holidays(Path("/nonexistent/holidays.csv")), {})
        self.assertFalse(tc.holiday_coverage({}, 2026))

    def test_comments_blank_lines_and_header_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.csv"
            path.write_text("# comment\n\ndate,name\n2026-01-26,Republic Day\n2026-03-03\n", encoding="utf-8")
            table = tc.load_holidays(path)
        self.assertEqual(table, {date(2026, 1, 26): "Republic Day", date(2026, 3, 3): ""})


class TestSessionPhase(unittest.TestCase):
    def phase(self, text: str) -> str:
        return tc.session_phase(tc.parse_when(text), HOLIDAYS)

    def test_phases_through_a_trading_day(self):
        self.assertEqual(self.phase("2026-10-16T09:00"), tc.PRE_OPEN)
        self.assertEqual(self.phase("2026-10-16T09:14:59"), tc.PRE_OPEN)
        self.assertEqual(self.phase("2026-10-16T09:15"), tc.OPEN)
        self.assertEqual(self.phase("2026-10-16T15:29:59"), tc.OPEN)
        self.assertEqual(self.phase("2026-10-16T15:30"), tc.POST_CLOSE)
        self.assertEqual(self.phase("2026-10-16T23:00"), tc.POST_CLOSE)

    def test_weekend_and_holiday_are_closed(self):
        self.assertEqual(self.phase("2026-10-17T12:00"), tc.CLOSED)   # Saturday
        self.assertEqual(self.phase("2026-10-18T12:00"), tc.CLOSED)   # Sunday
        self.assertEqual(self.phase("2026-10-20T12:00"), tc.CLOSED)   # Dussehra

    def test_utc_input_is_converted_to_ist(self):
        moment = datetime(2026, 10, 16, 3, 30, tzinfo=timezone.utc)   # 09:00 IST
        self.assertEqual(tc.session_phase(moment, HOLIDAYS), tc.PRE_OPEN)
        moment = datetime(2026, 10, 16, 3, 45, tzinfo=timezone.utc)   # 09:15 IST
        self.assertEqual(tc.session_phase(moment, HOLIDAYS), tc.OPEN)

    def test_parse_when_accepts_naive_as_ist_and_z_suffix(self):
        naive = tc.parse_when("2026-10-16T12:00")
        self.assertEqual(naive.utcoffset().total_seconds(), 5.5 * 3600)
        zulu = tc.parse_when("2026-10-16T06:30:00Z")
        self.assertEqual(zulu.hour, 12)


class TestTradingDayArithmetic(unittest.TestCase):
    def test_previous_and_next_skip_weekends_and_holidays(self):
        self.assertEqual(tc.previous_trading_day(date(2026, 10, 19), HOLIDAYS), date(2026, 10, 16))
        self.assertEqual(tc.next_trading_day(date(2026, 10, 19), HOLIDAYS), date(2026, 10, 21))  # 20th is Dussehra
        self.assertEqual(tc.next_trading_day(date(2026, 10, 16), HOLIDAYS), date(2026, 10, 19))

    def test_last_trading_day_of_week(self):
        self.assertTrue(tc.is_last_trading_day_of_week(date(2026, 10, 16), HOLIDAYS))    # Friday
        self.assertFalse(tc.is_last_trading_day_of_week(date(2026, 10, 15), HOLIDAYS))
        # Thursday 24 Dec 2026: Friday 25th is Christmas, so Thursday closes the week
        self.assertTrue(tc.is_last_trading_day_of_week(date(2026, 12, 24), HOLIDAYS))

    def test_last_trading_day_of_month_and_quarter(self):
        self.assertTrue(tc.is_last_trading_day_of_month(date(2026, 10, 30), HOLIDAYS))
        self.assertFalse(tc.is_last_trading_day_of_month(date(2026, 10, 29), HOLIDAYS))
        self.assertTrue(tc.is_last_trading_day_of_quarter(date(2026, 12, 31), HOLIDAYS))
        self.assertFalse(tc.is_last_trading_day_of_quarter(date(2026, 10, 30), HOLIDAYS))
        # 31 Mar 2026 is Mahavir Jayanti, so 30 Mar closes FY2025-26
        self.assertTrue(tc.is_last_trading_day_of_quarter(date(2026, 3, 30), HOLIDAYS))

    def test_week_and_month_bounds(self):
        self.assertEqual(tc.week_bounds(date(2026, 10, 14)), (date(2026, 10, 12), date(2026, 10, 16)))
        self.assertEqual(tc.month_bounds(date(2026, 2, 10)), (date(2026, 2, 1), date(2026, 2, 28)))
        self.assertEqual(tc.month_bounds(date(2026, 12, 10)), (date(2026, 12, 1), date(2026, 12, 31)))

    def test_iso_week_label(self):
        self.assertEqual(tc.iso_week_label(date(2026, 10, 30)), "W44 2026")


class TestFinancialYear(unittest.TestCase):
    def test_labels_and_bounds(self):
        self.assertEqual(tc.fy_label(date(2026, 10, 16)), "FY2026-27")
        self.assertEqual(tc.fy_label(date(2026, 3, 31)), "FY2025-26")
        self.assertEqual(tc.fy_label(date(2026, 4, 1)), "FY2026-27")
        self.assertEqual(tc.fy_bounds("FY2026-27"), (date(2026, 4, 1), date(2027, 3, 31)))
        self.assertEqual(tc.fy_label(date(2099, 5, 1)), "FY2099-00")
        with self.assertRaises(ValueError):
            tc.fy_bounds("2026-27")

    def test_quarters(self):
        self.assertEqual(tc.fy_quarter(date(2026, 4, 1)), 1)
        self.assertEqual(tc.fy_quarter(date(2026, 9, 30)), 2)
        self.assertEqual(tc.fy_quarter(date(2026, 10, 16)), 3)
        self.assertEqual(tc.fy_quarter(date(2027, 1, 5)), 4)
        self.assertEqual(tc.quarter_bounds("FY2026-27", 3), (date(2026, 10, 1), date(2026, 12, 31)))
        self.assertEqual(tc.quarter_bounds("FY2026-27", 4), (date(2027, 1, 1), date(2027, 3, 31)))
        self.assertEqual(tc.quarter_label(date(2026, 10, 16)), "Q3 FY2026-27")
        with self.assertRaises(ValueError):
            tc.quarter_bounds("FY2026-27", 5)

    def test_advance_tax_schedule(self):
        schedule = tc.advance_tax_schedule("FY2026-27")
        self.assertEqual([(d.due, d.cumulative_pct) for d in schedule], [
            (date(2026, 6, 15), 15), (date(2026, 9, 15), 45),
            (date(2026, 12, 15), 75), (date(2027, 3, 15), 100),
        ])
        nxt = tc.next_advance_tax_due(date(2026, 10, 16))
        self.assertEqual((nxt.due, nxt.ordinal), (date(2026, 12, 15), "3rd"))
        self.assertEqual(nxt.days_until(date(2026, 12, 10)), 5)
        self.assertEqual(tc.next_advance_tax_due(date(2027, 3, 15)).ordinal, "4th")
        self.assertIsNone(tc.next_advance_tax_due(date(2027, 3, 16)))


if __name__ == "__main__":
    unittest.main()
