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

import scrapers.bb_auction as bb_auction
from scrapers.bb_auction import (
    _coerce_calendar_rows,
    _coerce_result_rows,
    _llm_rows,
    _tenor_label,
    scrape_calendar,
    scrape_results,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def treasury_html() -> str:
    """Real capture of monetaryactivity/treasury — the post-restructure RESULTS source."""
    return (FIXTURES / "bb_treasury_auctions.html").read_text(encoding="utf-8")


@pytest.fixture
def yearly_calendar_html() -> str:
    """Real capture of auc_calendar/1 — the post-restructure CALENDAR source."""
    return (FIXTURES / "bb_auction_yearly_calendar.html").read_text(encoding="utf-8")


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


# NOTE: per-tenor RESULTS parsing (the post-restructure monetaryactivity/treasury
# table) is covered in test_bb_auction_treasury.py. The orchestration tests below
# exercise the deterministic-first / LLM-fallback wiring around it.


# NOTE: forward-calendar parsing (the post-restructure auc_calendar/1 div-grid) is
# covered in test_bb_auction_calendar.py.


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


def test_scrape_results_uses_deterministic_and_skips_llm(treasury_html):
    spy = MagicMock()
    with patch("scrapers.bb_auction.fetch_results_html", return_value=treasury_html):
        rows = scrape_results(run_max_fn=spy)
    assert len(rows) == 8  # 91/182/364d bills + 2/5/10/15/20y bonds
    spy.assert_not_called()  # deterministic parse succeeded; no LLM call


def test_scrape_results_falls_back_to_llm_when_deterministic_empty():
    empty_html = "<html><body><p>auction held, results below</p></body></html>"
    fake = MagicMock()
    fake.parsed = {"rows": [{"auction_date": "2026-05-28", "tenor": "182d", "size": 2500.0}]}
    with patch("scrapers.bb_auction.fetch_results_html", return_value=empty_html):
        rows = scrape_results(run_max_fn=lambda **k: fake)
    assert rows == [{"tenor": "182d", "auction_date": date(2026, 5, 28), "size": 2500.0}]


def test_scrape_calendar_uses_deterministic(yearly_calendar_html):
    spy = MagicMock()
    with patch("scrapers.bb_auction.fetch_calendar_html", return_value=yearly_calendar_html):
        rows = scrape_calendar(today=date(2026, 6, 2), run_max_fn=spy)
    assert len(rows) == 17  # 8 future dates × per-tenor non-zero notionals
    spy.assert_not_called()


# --------------------------------------------------------------------------- #
# BB image-CAPTCHA reroute: RESULTS (treasury) + CALENDAR fetch through the solver
# --------------------------------------------------------------------------- #

# All four markers _is_captcha_page requires (id="ans", id="jar", class="thumbnails", "support ID").
_CAPTCHA_HTML = (
    '<form><input id="ans"><button id="jar"></button>'
    '<div class="thumbnails"></div>support ID: 12345</form>'
)


def test_results_fetched_via_solver_from_treasury_page():
    """RESULTS must go through the CAPTCHA-solving rendered path against the treasury
    auction-results page (the /rrpt/ press release is a PDF behind a wall the renderer
    cannot clear)."""
    with patch(
        "scrapers.bb_forex.fetch_rendered_html", return_value="RESULTS_HTML"
    ) as m_render:
        out = bb_auction.fetch_results_html()
    m_render.assert_called_once_with(bb_auction.AUCTION_RESULTS_URL)
    assert out == "RESULTS_HTML"


def test_calendar_fetched_via_solver():
    with patch(
        "scrapers.bb_forex.fetch_rendered_html", return_value="CAL_HTML"
    ) as m_render:
        out = bb_auction.fetch_calendar_html()
    m_render.assert_called_once_with(bb_auction.AUCTION_CALENDAR_URL)
    assert out == "CAL_HTML"


def test_get_rendered_raises_clear_error_on_unsolved_captcha():
    """If the solver returns but the page is STILL the CAPTCHA wall, surface a clear
    FetchError (→ needs_review), not a misleading downstream parse failure."""
    with patch("scrapers.bb_forex.fetch_rendered_html", return_value=_CAPTCHA_HTML):
        with pytest.raises(bb_auction.FetchError, match="CAPTCHA unsolved"):
            bb_auction._get_rendered("https://www.bb.org.bd/x")


def test_get_rendered_wraps_solver_error():
    with patch(
        "scrapers.bb_forex.fetch_rendered_html", side_effect=RuntimeError("boom")
    ):
        with pytest.raises(bb_auction.FetchError, match="rendered fetch failed"):
            bb_auction._get_rendered("https://www.bb.org.bd/x")
