"""Tests for the BB primary-auction scraper (scrapers/bb_auction.py).

Fully local / NO egress: the parse helpers are pure and run against synthetic
HTML fixtures; fetch + LLM + Supabase are mocked. The LIVE BB pages are behind a
BD-egress CAPTCHA wall (verified), so the real fetch/parse is VPS-deferred — the
unit tests pin the row SHAPE + field SEMANTICS the VPS run must reproduce.

Cross-step contract (S8): result rows match auction_results
{auction_date, tenor, size, bid, cover, wam, cutoff}; calendar rows match
auction_calendar {auction_date, tenor, notional}.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scrapers.bb_auction import (
    _coerce_calendar_rows,
    _coerce_result_rows,
    _llm_rows,
    _tenor_label,
    parse_auction_calendar,
    parse_auction_results,
    recover_held_on,
    scrape_calendar,
    scrape_results,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def results_html() -> str:
    return (FIXTURES / "bb_auction_results.html").read_text(encoding="utf-8")


@pytest.fixture
def calendar_html() -> str:
    return (FIXTURES / "bb_auction_calendar.html").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# tenor labelling — header-LABEL matching (landmine E)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,expected",
    [
        ("91-Day T-Bill", "91d"),
        ("182 Day Treasury Bill", "182d"),
        ("364-Day T-Bill", "364d"),
        ("5-Year BGTB", "5y"),
        ("10 Yr T-Bond", "10y"),
        ("2-Year BGTB", "2y"),
        ("Repo", None),
        ("Standing Lending Facility", None),
        ("", None),
    ],
)
def test_tenor_label_maps_by_words_not_index(text, expected):
    assert _tenor_label(text) == expected


# --------------------------------------------------------------------------- #
# RESULTS — per-tenor rows with all 7 fields (PK + 5 data fields)
# --------------------------------------------------------------------------- #


def test_results_recovers_auction_date_from_held_on(results_html):
    """as_of comes from the 'held on <date>' press-release title."""
    assert recover_held_on(results_html) == date(2026, 5, 28)


def test_results_emits_one_row_per_tenor(results_html):
    rows = parse_auction_results(results_html)
    tenors = {r["tenor"] for r in rows}
    assert tenors == {"91d", "182d", "364d", "5y", "10y"}


def test_results_row_carries_all_fields_for_a_bond(results_html):
    """The 5-Year BGTB row must carry size/bid/cover/wam/cutoff + the PK fields."""
    rows = parse_auction_results(results_html)
    bond = next(r for r in rows if r["tenor"] == "5y")
    assert bond["auction_date"] == date(2026, 5, 28)
    assert bond["size"] == 1500.0
    assert bond["bid"] == 2610.0
    assert bond["cover"] == 1.74
    assert bond["wam"] == 4.98
    assert bond["cutoff"] == 12.10


def test_results_tbill_omits_wam_gracefully(results_html):
    """T-bills have no weighted-average maturity — the field must be ABSENT, not 0."""
    rows = parse_auction_results(results_html)
    bill = next(r for r in rows if r["tenor"] == "182d")
    assert "wam" not in bill
    assert bill["size"] == 2500.0
    assert bill["cutoff"] == 11.20


def test_results_columns_mapped_by_header_label_not_position(results_html):
    """size must be the ACCEPTED column, not the (earlier) Notified column."""
    rows = parse_auction_results(results_html)
    bill = next(r for r in rows if r["tenor"] == "91d")
    # Notified=3000, Accepted=3000 here are equal, but bid=7820.50 proves the
    # header map picked the Total-Bid column (not e.g. cut-off) for `bid`.
    assert bill["bid"] == 7820.50
    assert bill["cover"] == 2.61


def test_results_empty_when_no_results_table():
    """A non-results page yields [] so the caller falls through to the LLM."""
    assert parse_auction_results("<html><body><p>no table here</p></body></html>") == []


# --------------------------------------------------------------------------- #
# CALENDAR — forward multi-week per-tenor strip; partial-horizon handling
# --------------------------------------------------------------------------- #


def test_calendar_returns_only_future_rows(calendar_html):
    """Past-dated auctions (14/05, 21/05) are dropped; future weeks kept."""
    rows = parse_auction_calendar(calendar_html, today=date(2026, 6, 1))
    dates = {r["auction_date"] for r in rows}
    assert date(2026, 5, 14) not in dates and date(2026, 5, 21) not in dates
    assert date(2026, 6, 4) in dates and date(2026, 6, 25) in dates


def test_calendar_is_multi_week_per_tenor(calendar_html):
    """The strip spans multiple weeks with per-tenor rows."""
    rows = parse_auction_calendar(calendar_html, today=date(2026, 6, 1))
    weeks = {r["auction_date"] for r in rows}
    assert len(weeks) >= 4  # 04/06, 11/06, 18/06, 25/06, 02/07
    jun4 = {r["tenor"] for r in rows if r["auction_date"] == date(2026, 6, 4)}
    assert jun4 == {"91d", "182d", "364d"}


def test_calendar_row_carries_notional(calendar_html):
    rows = parse_auction_calendar(calendar_html, today=date(2026, 6, 1))
    bond = next(r for r in rows if r["auction_date"] == date(2026, 6, 25))
    assert bond["tenor"] == "10y"
    assert bond["notional"] == 1000.0


def test_calendar_blank_notional_omitted_not_zeroed(calendar_html):
    """A row with a blank notional is still emitted, with `notional` ABSENT."""
    rows = parse_auction_calendar(calendar_html, today=date(2026, 6, 1))
    row = next(r for r in rows if r["auction_date"] == date(2026, 7, 2))
    assert row["tenor"] == "364d"
    assert "notional" not in row


def test_calendar_horizon_caps_the_strip(calendar_html):
    """A short horizon drops weeks beyond it (partial-horizon graceful handling)."""
    rows = parse_auction_calendar(
        calendar_html, today=date(2026, 6, 1), horizon_weeks=1
    )
    # Only auctions within 1 week of 2026-06-01 (i.e. <= 2026-06-08) survive.
    assert all(r["auction_date"] <= date(2026, 6, 8) for r in rows)
    assert any(r["auction_date"] == date(2026, 6, 4) for r in rows)


def test_calendar_empty_when_no_calendar_table():
    assert parse_auction_calendar("<html><body>nothing</body></html>") == []


# --------------------------------------------------------------------------- #
# LLM fallback — multi-row strict-JSON coercion
# --------------------------------------------------------------------------- #


def test_llm_rows_parses_rows_envelope():
    fake = MagicMock()
    fake.parsed = {"rows": [{"tenor": "182d", "size": 2500.0, "cutoff": 11.2}]}
    rows = _llm_rows("auction_results_extract.txt", "<html></html>", run_max_fn=lambda **k: fake)
    assert rows == [{"tenor": "182d", "size": 2500.0, "cutoff": 11.2}]


def test_llm_rows_returns_empty_on_bad_json():
    fake = MagicMock()
    fake.parsed = None
    fake.raw_text = "not json at all"
    rows = _llm_rows("auction_results_extract.txt", "<html></html>", run_max_fn=lambda **k: fake)
    assert rows == []


def test_coerce_result_rows_gates_and_normalises():
    """LLM rows are tenor-labelled, range-gated, and stamped with the auction date."""
    raw = [
        {"tenor": "182-Day T-Bill", "size": "2,500.0", "cutoff": "11.20"},
        {"tenor": "junk", "size": 1.0},          # unknown tenor -> dropped
        {"tenor": "5y", "cutoff": "999"},        # cutoff out of range -> field dropped, no other field -> row dropped
    ]
    out = _coerce_result_rows(raw, auction_date=date(2026, 5, 28))
    assert len(out) == 1
    assert out[0] == {
        "tenor": "182d", "auction_date": date(2026, 5, 28),
        "size": 2500.0, "cutoff": 11.20,
    }


def test_coerce_calendar_rows_forward_filters():
    raw = [
        {"auction_date": "2026-06-04", "tenor": "91d", "notional": "3000"},
        {"auction_date": "2026-05-01", "tenor": "91d", "notional": "3000"},  # past -> dropped
        {"auction_date": "2027-01-01", "tenor": "91d"},                       # beyond horizon -> dropped
    ]
    out = _coerce_calendar_rows(raw, today=date(2026, 6, 1), horizon_weeks=12)
    assert out == [{"auction_date": date(2026, 6, 4), "tenor": "91d", "notional": 3000.0}]


# --------------------------------------------------------------------------- #
# Orchestration — deterministic-first, LLM only on empty
# --------------------------------------------------------------------------- #


def test_scrape_results_uses_deterministic_and_skips_llm(results_html):
    spy = MagicMock()
    with patch("scrapers.bb_auction.fetch_latest_results_html", return_value=results_html):
        rows = scrape_results(run_max_fn=spy)
    assert len(rows) == 5
    spy.assert_not_called()  # deterministic parse succeeded; no LLM call


def test_scrape_results_falls_back_to_llm_when_deterministic_empty():
    empty_html = "<html><body><p>auction held, results below</p></body></html>"
    fake = MagicMock()
    fake.parsed = {"rows": [{"auction_date": "2026-05-28", "tenor": "182d", "size": 2500.0}]}
    with patch("scrapers.bb_auction.fetch_latest_results_html", return_value=empty_html):
        rows = scrape_results(run_max_fn=lambda **k: fake)
    assert rows == [{"tenor": "182d", "auction_date": date(2026, 5, 28), "size": 2500.0}]


def test_scrape_calendar_uses_deterministic(calendar_html):
    spy = MagicMock()
    with patch("scrapers.bb_auction.fetch_calendar_html", return_value=calendar_html):
        rows = scrape_calendar(today=date(2026, 6, 1), run_max_fn=spy)
    assert len(rows) >= 4
    spy.assert_not_called()
