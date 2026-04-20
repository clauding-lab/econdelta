"""Bangladesh trading-day calendar.

DSE trades Sunday through Thursday. Friday and Saturday are weekend non-trading days.
Python's date.weekday() returns: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6.
"""

import json
import logging
from datetime import date, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_BD_WEEKEND = frozenset({4, 5})  # Friday=4, Saturday=5
_MAX_LOOKBACK_DAYS = 14


def is_bd_trading_day(d: date, holidays: set[date] | None = None) -> bool:
    """Return True if the given date is a DSE trading day.

    Args:
        d: The date to check.
        holidays: Optional set of public holiday dates to exclude.

    Returns:
        True if the market trades on this date; False otherwise.
    """
    if d.weekday() in _BD_WEEKEND:
        return False
    if holidays and d in holidays:
        return False
    return True


def previous_trading_day(d: date, holidays: set[date] | None = None) -> date:
    """Return the most recent trading day strictly before the given date.

    Args:
        d: The reference date (not included in the search).
        holidays: Optional set of public holiday dates to skip.

    Returns:
        The closest prior trading day.

    Raises:
        RuntimeError: If no trading day is found within 14 days (holiday data likely wrong).
    """
    candidate = d - timedelta(days=1)
    for _ in range(_MAX_LOOKBACK_DAYS):
        if is_bd_trading_day(candidate, holidays):
            return candidate
        candidate -= timedelta(days=1)

    raise RuntimeError(
        f"No trading day found in {_MAX_LOOKBACK_DAYS} days before {d}. "
        "Check holidays_2026.json for errors."
    )


def load_holidays(path: str | Path) -> set[date]:
    """Load Bangladesh public holidays from a JSON config file.

    Args:
        path: Path to holidays_2026.json.

    Returns:
        Set of date objects for all listed holidays.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If a date string cannot be parsed as YYYY-MM-DD.
    """
    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        raw: dict = json.load(fh)

    holidays: set[date] = set()
    for entry in raw.get("holidays", []):
        date_str: str = entry["date"]
        try:
            holidays.add(date.fromisoformat(date_str))
        except ValueError as exc:
            raise ValueError(f"Invalid holiday date {date_str!r} in {p}") from exc

    return holidays
