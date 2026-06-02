"""Tests for the treasury-page auction-results parser (scrapers/bb_auction.py).

Fully local / NO egress. BB retired the per-business-day ``/rrpt/`` press release
(now a PDF behind an F5 + image-CAPTCHA wall that does not yield to automation);
the per-tenor auction RESULTS instead come from the already-scraped, solver-served
HTML page ``monetaryactivity/treasury`` — the same page that already lands the
scalar cut-off yields (``bill_bond_rates`` / ``tbill_*_yield``).

The fixture ``bb_treasury_auctions.html`` is a REAL capture of that page (pulled
from the ExonVPS box through the CAPTCHA solver), so these tests pin the exact
row SHAPE + field SEMANTICS the live VPS run must reproduce. Its results table
has a TWO-ROW grouped header — ``Bids received`` (colspan 3) and ``Bids accepted``
(colspan 7) — so ``Face value`` / ``Range of yields`` appear twice; the parser
MUST disambiguate by header GROUP, not column position (landmine E).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from scrapers.bb_auction import parse_treasury_results

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def treasury_html() -> str:
    return (FIXTURES / "bb_treasury_auctions.html").read_text(encoding="utf-8")


def _row(rows: list[dict], tenor: str) -> dict:
    return next(r for r in rows if r["tenor"] == tenor)


# --------------------------------------------------------------------------- #
# Tenor coverage — canonical set only; non-canonical tenors are skipped.
# --------------------------------------------------------------------------- #


def test_treasury_results_emits_one_row_per_canonical_tenor(treasury_html):
    """Bills 91/182/364d + bonds 2/5/10/15/20y are emitted, one row each."""
    rows = parse_treasury_results(treasury_html)
    assert {r["tenor"] for r in rows} == {
        "91d", "182d", "364d", "2y", "5y", "10y", "15y", "20y",
    }


def test_treasury_results_skips_non_canonical_tenors(treasury_html):
    """The 14-day bill and the 3-year FRTB are NOT in the canonical set -> dropped."""
    rows = parse_treasury_results(treasury_html)
    assert all(r["tenor"] not in {"14d", "3y"} for r in rows)
    assert len(rows) == 8


# --------------------------------------------------------------------------- #
# Field extraction — bills (no WAM) and bonds (WAM from the re-issuance note).
# --------------------------------------------------------------------------- #


def test_treasury_results_tbill_row_fields(treasury_html):
    """91-day bill: issue date as auction_date, accepted size, total bid, cutoff."""
    rows = parse_treasury_results(treasury_html)
    bill = _row(rows, "91d")
    assert bill["auction_date"] == date(2026, 5, 24)
    assert bill["size"] == 3500.0
    assert bill["bid"] == 6904.22
    assert bill["cutoff"] == 10.15
    assert bill["cover"] == 1.97
    assert "wam" not in bill  # bills have no weighted-average maturity


def test_treasury_results_bond_row_carries_wam(treasury_html):
    """5-year bucket (a re-issued 20yr bond): WAM comes from '(Re-issuance: 4.96 Yr.)'."""
    rows = parse_treasury_results(treasury_html)
    bond = _row(rows, "5y")
    assert bond["auction_date"] == date(2026, 5, 13)
    assert bond["size"] == 3000.0
    assert bond["bid"] == 8555.59
    assert bond["cutoff"] == 10.78
    assert bond["cover"] == 2.85
    assert bond["wam"] == 4.96


# --------------------------------------------------------------------------- #
# Grouped-header disambiguation (landmine E): size = ACCEPTED, bid = RECEIVED.
# --------------------------------------------------------------------------- #


def test_treasury_results_size_is_accepted_face_value_not_received(treasury_html):
    """`size` must read the Bids-ACCEPTED Face value (3500), not Bids-RECEIVED (6904).

    Both columns are literally 'Face value (Cr.Tk.)'; only the parent group header
    ('Bids received' vs 'Bids accepted') tells them apart. A positional parser would
    grab the wrong one — this asserts group-aware mapping.
    """
    rows = parse_treasury_results(treasury_html)
    bill = _row(rows, "91d")
    assert bill["size"] == 3500.0      # Bids accepted -> Face value
    assert bill["bid"] == 6904.22      # Bids received -> Face value
    assert bill["size"] != bill["bid"]


def test_treasury_results_cover_is_bid_over_size(treasury_html):
    """cover (bid-to-cover) is derived bid/size, rounded to 2dp (no published column)."""
    rows = parse_treasury_results(treasury_html)
    bond = _row(rows, "10y")
    assert bond["bid"] == 7938.02
    assert bond["size"] == 3000.0
    assert bond["cover"] == 2.65  # round(7938.02 / 3000, 2)


# --------------------------------------------------------------------------- #
# Empty / non-results input -> [] (caller falls through to the LLM fallback).
# --------------------------------------------------------------------------- #


def test_treasury_results_empty_when_no_results_table():
    assert parse_treasury_results("<html><body><p>no table</p></body></html>") == []
