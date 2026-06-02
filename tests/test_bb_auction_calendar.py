"""Tests for the yearly-calendar parser (scrapers/bb_auction.py).

Fully local / NO egress. BB also restructured the auction CALENDAR: the old
``auc_calendar`` ("Yet to bid") page no longer renders a server-side ``<table>``,
and the forward issuance strip moved to ``auc_calendar/1`` ("Yearly calendar"),
which renders as a CSS DIV-GRID (``div.row-header`` + ``div.row-data`` with
``div.column`` cells), NOT a ``<table>``. Two grids in document order — a BILLS
grid (14/91/182/364 days) then a BONDS grid (2/5/10/15/20 yr + 3 yr FRTB) — each
preceded by its own header row, so columns map by the CURRENT grid's header.

The fixture ``bb_auction_yearly_calendar.html`` is a REAL capture of that page.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from scrapers.bb_auction import parse_yearly_calendar

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 6, 2)  # matches the fixture's "as captured" date


@pytest.fixture
def yearly_html() -> str:
    return (FIXTURES / "bb_auction_yearly_calendar.html").read_text(encoding="utf-8")


def _row(rows: list[dict], d: date, tenor: str) -> dict:
    return next(r for r in rows if r["auction_date"] == d and r["tenor"] == tenor)


def test_yearly_calendar_emits_future_per_tenor_rows(yearly_html):
    """One row per (future date, canonical tenor) with a non-zero notified amount."""
    rows = parse_yearly_calendar(yearly_html, today=TODAY)
    # 8 future dates: bills (91/182/364d ×4 dates) + bonds (2y,5y,10y,15y,20y across 4 dates)
    assert len(rows) == 17


def test_yearly_calendar_bill_row_carries_notional(yearly_html):
    rows = parse_yearly_calendar(yearly_html, today=TODAY)
    r = _row(rows, date(2026, 6, 7), "91d")
    assert r["notional"] == 4000.0


def test_yearly_calendar_bond_row_uses_its_grid_header(yearly_html):
    """A bond row maps via the BONDS grid header (2/5/10/15/20 yr), not the bills one."""
    rows = parse_yearly_calendar(yearly_html, today=TODAY)
    assert _row(rows, date(2026, 6, 9), "5y")["notional"] == 3500.0
    assert _row(rows, date(2026, 6, 23), "15y")["notional"] == 1500.0
    assert _row(rows, date(2026, 6, 23), "20y")["notional"] == 1500.0


def test_yearly_calendar_skips_non_canonical_tenors(yearly_html):
    """`14 days` and `3 yr(FRTB)` are not canonical tenors -> never emitted."""
    rows = parse_yearly_calendar(yearly_html, today=TODAY)
    assert all(r["tenor"] not in {"14d", "3y"} for r in rows)


def test_yearly_calendar_skips_zero_notional_cells(yearly_html):
    """09-Jun has only a 5y auction (other tenors 0.00) -> exactly one row that date."""
    rows = parse_yearly_calendar(yearly_html, today=TODAY)
    jun9 = [r for r in rows if r["auction_date"] == date(2026, 6, 9)]
    assert {r["tenor"] for r in jun9} == {"5y"}


def test_yearly_calendar_drops_past_rows(yearly_html):
    """The grid is a full year (incl. 2025) — past-dated rows are dropped."""
    rows = parse_yearly_calendar(yearly_html, today=TODAY)
    assert all(r["auction_date"] >= TODAY for r in rows)


def test_yearly_calendar_horizon_caps_the_strip(yearly_html):
    """horizon_weeks=1 keeps only auctions within a week of today (<= 2026-06-09)."""
    rows = parse_yearly_calendar(yearly_html, today=TODAY, horizon_weeks=1)
    assert all(r["auction_date"] <= date(2026, 6, 9) for r in rows)
    assert any(r["auction_date"] == date(2026, 6, 9) for r in rows)
    assert not any(r["auction_date"] == date(2026, 6, 14) for r in rows)


def test_yearly_calendar_empty_when_no_grid():
    assert parse_yearly_calendar("<html><body><p>no grid</p></body></html>") == []
