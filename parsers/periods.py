"""Calendar helpers for source observation periods.

Bangladesh fiscal years run from July through June.  A label such as FY26 is
therefore not a calendar-year label: it ends on 30 June 2026.
"""
from __future__ import annotations

import calendar
import re
from datetime import date

_MONTHS = {
    name.lower(): number for number, name in enumerate(calendar.month_name) if name
}


def fy_month_end(fy_end_year: int, month: int) -> date:
    """Return the calendar month-end for ``month`` inside Bangladesh FY ending in year.

    FY26 spans 1 July 2025 through 30 June 2026.
    """
    if not 1 <= month <= 12:
        raise ValueError(f"month must be 1..12, got {month}")
    year = fy_end_year - 1 if month >= 7 else fy_end_year
    return date(year, month, calendar.monthrange(year, month)[1])


def month_end(year: int, month: int) -> date:
    """Return a validated calendar month-end."""
    if not 1 <= month <= 12:
        raise ValueError(f"month must be 1..12, got {month}")
    return date(year, month, calendar.monthrange(year, month)[1])


def parse_month_year(label: str) -> date | None:
    """Read an explicit calendar month and year from a table header."""
    match = re.search(r"\b([A-Za-z]+)[,\s]+(\d{4})(?:[PR])?\b", label, re.IGNORECASE)
    if not match:
        return None
    month = _MONTHS.get(match.group(1).lower())
    if month is None:
        return None
    try:
        return month_end(int(match.group(2)), month)
    except ValueError:
        return None


def parse_fy_end(label: str) -> int | None:
    """Read FY26/FY 26 and return the fiscal year ending in 2026."""
    # BB may append a provisional/revised footnote digit (FY26P2, FY25R1).
    match = re.search(r"\bFY\s*(\d{2})(?:[PR]\d*)?\b", label, re.IGNORECASE)
    return 2000 + int(match.group(1)) if match else None


def parse_wsei_period(label: str) -> date | None:
    """Read a WSEI calendar-month or fiscal-year column header."""
    explicit = parse_month_year(label)
    if explicit:
        return explicit
    fy = parse_fy_end(label)
    if fy is None:
        return None
    month_match = re.search(r"\b([A-Za-z]+)[,\s]+FY\s*\d{2}\b", label, re.IGNORECASE)
    if month_match:
        month = _MONTHS.get(month_match.group(1).lower())
        if month:
            return fy_month_end(fy, month)
    return date(fy, 6, 30)
