"""Bangladesh trading-day calendar.

DSE trades Sunday through Thursday. Friday and Saturday are weekend non-trading days.
Python's date.weekday() returns: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6.
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_BD_WEEKEND = frozenset({4, 5})  # Friday=4, Saturday=5
_MAX_LOOKBACK_DAYS = 14

# DSE trades Sun–Thu and closes ~14:30 Asia/Dhaka. Bangladesh keeps a fixed UTC+6
# (no DST), so a constant offset is exact.
_BD_TZ = timezone(timedelta(hours=6))
DSE_CLOSE_HOUR = 14
DSE_CLOSE_MINUTE = 30


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


def last_trading_close(now: datetime, holidays: set[date] | None = None) -> datetime:
    """Return the UTC datetime of the most recent DSE session close at or before ``now``.

    DSE closes ~14:30 Asia/Dhaka on a trading day. Over the weekend (Fri/Sat), a
    holiday, or before today's close, this returns the *previous* trading day's close —
    so a snapshot holding that session's data is fresh, not stale. This is what makes
    DSE staleness weekend-aware instead of a raw 24h age check.

    Args:
        now: Reference time. Naive values are treated as UTC; aware values are honoured.
        holidays: Optional set of public-holiday dates to skip (Fri/Sat are always skipped).

    Returns:
        The session close as a timezone-aware UTC datetime.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    bd_now = now.astimezone(_BD_TZ)
    close_today = bd_now.replace(
        hour=DSE_CLOSE_HOUR, minute=DSE_CLOSE_MINUTE, second=0, microsecond=0
    )
    if is_bd_trading_day(bd_now.date(), holidays) and bd_now >= close_today:
        close_day = bd_now.date()
    else:
        close_day = previous_trading_day(bd_now.date(), holidays)
    close = datetime(
        close_day.year, close_day.month, close_day.day,
        DSE_CLOSE_HOUR, DSE_CLOSE_MINUTE, tzinfo=_BD_TZ,
    )
    return close.astimezone(timezone.utc)


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
