"""Tests for utils/calendar.py."""

import json
import tempfile
from datetime import date
from pathlib import Path

import pytest

from utils.calendar import is_bd_trading_day, load_holidays, previous_trading_day


class TestIsBdTradingDay:
    def test_friday_is_not_trading_day(self):
        # Friday 2026-04-24
        friday = date(2026, 4, 24)
        assert friday.weekday() == 4  # sanity check
        assert is_bd_trading_day(friday) is False

    def test_saturday_is_not_trading_day(self):
        saturday = date(2026, 4, 25)
        assert saturday.weekday() == 5
        assert is_bd_trading_day(saturday) is False

    def test_sunday_is_trading_day(self):
        # Sunday 2026-04-26
        sunday = date(2026, 4, 26)
        assert sunday.weekday() == 6
        assert is_bd_trading_day(sunday) is True

    def test_monday_is_trading_day(self):
        monday = date(2026, 4, 27)
        assert monday.weekday() == 0
        assert is_bd_trading_day(monday) is True

    def test_thursday_is_trading_day(self):
        thursday = date(2026, 4, 23)
        assert thursday.weekday() == 3
        assert is_bd_trading_day(thursday) is True

    def test_public_holiday_is_not_trading_day(self):
        independence_day = date(2026, 3, 26)  # Thursday — would normally be trading
        holidays = {independence_day}
        assert is_bd_trading_day(independence_day, holidays=holidays) is False

    def test_holiday_on_weekend_still_not_trading(self):
        # If a holiday falls on a weekend, it's already non-trading
        friday = date(2026, 1, 2)  # a Friday
        assert is_bd_trading_day(friday, holidays={friday}) is False


class TestPreviousTradingDay:
    def test_previous_from_sunday_skips_friday_and_saturday(self):
        # Sunday 2026-04-26 -> previous trading day should be Thursday 2026-04-23
        sunday = date(2026, 4, 26)
        result = previous_trading_day(sunday)
        thursday = date(2026, 4, 23)
        assert result == thursday

    def test_previous_from_monday(self):
        # Monday 2026-04-27 -> previous is Sunday 2026-04-26
        monday = date(2026, 4, 27)
        result = previous_trading_day(monday)
        assert result == date(2026, 4, 26)

    def test_previous_from_thursday(self):
        # Thursday -> previous is Wednesday
        thursday = date(2026, 4, 23)
        result = previous_trading_day(thursday)
        assert result == date(2026, 4, 22)  # Wednesday

    def test_skips_holiday(self):
        # If Wednesday is a holiday, previous from Thursday should land on Tuesday
        thursday = date(2026, 4, 23)
        wednesday = date(2026, 4, 22)
        holidays = {wednesday}
        result = previous_trading_day(thursday, holidays=holidays)
        assert result == date(2026, 4, 21)  # Tuesday

    def test_raises_if_no_trading_day_found(self):
        # Block all weekdays in holidays to force exhaustion
        base = date(2026, 4, 26)  # Sunday
        # Mark all 14 prior days as holidays
        from datetime import timedelta
        all_days = {base - timedelta(days=i) for i in range(1, 15)}
        with pytest.raises(RuntimeError, match="No trading day found"):
            previous_trading_day(base, holidays=all_days)


class TestLoadHolidays:
    def _write_holidays_json(self, holidays: list[dict]) -> str:
        data = {
            "_meta": {"country": "Bangladesh", "year": 2026},
            "holidays": holidays,
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(data, f)
            return f.name

    def test_loads_holiday_dates(self):
        path = self._write_holidays_json([
            {"date": "2026-01-01", "name": "New Year"},
            {"date": "2026-03-26", "name": "Independence Day"},
        ])
        result = load_holidays(path)
        assert date(2026, 1, 1) in result
        assert date(2026, 3, 26) in result
        assert len(result) == 2

    def test_raises_on_bad_date_format(self):
        path = self._write_holidays_json([{"date": "26-01-2026", "name": "Bad"}])
        with pytest.raises(ValueError, match="Invalid holiday date"):
            load_holidays(path)

    def test_raises_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_holidays("/nonexistent/holidays.json")

    def test_loads_actual_config_file(self):
        config_path = Path(__file__).parent.parent / "config" / "holidays_2026.json"
        result = load_holidays(config_path)
        assert date(2026, 1, 1) in result
        assert date(2026, 3, 26) in result
        assert len(result) >= 7
