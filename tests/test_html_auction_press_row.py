"""S7 — slf_draw_cr + bb_repo_usage_cr (BB auction-result press release).

Two metrics fed by ONE daily Bangladesh Bank press release
("Result of the Auction of Repo, ALS, SLF, SDF and IBLF held on <date>",
under mediaroom/press_release_details/rrpt/<id>):

  * slf_draw_cr      — SLF (Standing Lending Facility) accepted amount; draw/
                       usage level only (SLF is uncapped-on-demand, no limit).
  * bb_repo_usage_cr — central-bank Repo accepted amount; ABSENT on many days
                       because BB largely stopped routine daily repo lending.

This file proves, fully locally (the live fetch/discovery is BD-egress and
VPS-deferred):

  1. rrpt discovery picks the highest rrpt id and honours the title filter.
  2. The parser reads the named instrument's accepted amount from BOTH a
     table-shaped and a prose-shaped release (BB renders releases both ways).
  3. The NULL-vs-ZERO contract: an ABSENT instrument (no Repo line) raises
     ParseError (-> LLM null -> dropped, no fabricated 0); a GENUINE measured 0
     (instrument present, nothing accepted) is returned as 0.0.
  4. source_as_of is recovered from the "held on <date>" title.
"""

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import parsers.html_auction_press_row  # noqa: F401  (registers the parser)
from fetchers.base import FetchResult
from fetchers.rrpt_discovery import discover_latest_rrpt_link
from parsers.base import ParseError
from parsers.registry import get_parser

SLF_ACCEPTED_CR = 12500.0
REPO_ACCEPTED_CR = 8000.0

# A release rendered as an HTML table, WITH a Repo row present.
_TABLE_WITH_REPO = f"""
<html><body>
<h3>Result of the Auction of Repo, ALS, SLF, SDF and IBLF held on 28 May, 2025</h3>
<table>
  <tr><th>Instrument</th><th>Accepted Amount (Tk. crore)</th><th>Rate (%)</th></tr>
  <tr><td>Repo</td><td>{REPO_ACCEPTED_CR:,.1f}</td><td>10.00</td></tr>
  <tr><td>SLF</td><td>{SLF_ACCEPTED_CR:,.1f}</td><td>11.50</td></tr>
  <tr><td>SDF</td><td>3,200.0</td><td>7.50</td></tr>
</table>
</body></html>
"""

# A release rendered as PROSE, with SLF but NO Repo line (no-repo day).
_PROSE_NO_REPO = f"""
<html><body>
<p>Result of the Auction of ALS, SLF, SDF and IBLF held on May 29, 2025</p>
<p>Total accepted amount of SLF (Standing Lending Facility) stood at
Tk. {SLF_ACCEPTED_CR:,.1f} crore at a rate of 11.50 percent.</p>
<p>Total accepted amount of SDF stood at Tk. 4,100.0 crore.</p>
</body></html>
"""

# A release where Repo was held but NOTHING was accepted (genuine measured 0).
_TABLE_REPO_ZERO = """
<html><body>
<h3>Result of the Auction of Repo, ALS, SLF, SDF and IBLF held on 30 May, 2025</h3>
<table>
  <tr><th>Instrument</th><th>Accepted Amount (Tk. crore)</th></tr>
  <tr><td>Repo</td><td>0</td></tr>
  <tr><td>SLF</td><td>9,000.0</td></tr>
</table>
</body></html>
"""


def _artifact(tmp_path: Path, html: str, name: str) -> FetchResult:
    p = tmp_path / f"{name}.html"
    p.write_text(html)
    return FetchResult(
        indicator_id=name,
        artifact_path=p,
        artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.bb.org.bd/en/index.php/mediaroom/press_release_details/rrpt/12345",
        sha256="a" * 64,
        cache_hit=False,
    )


# --- rrpt discovery -------------------------------------------------------

_LISTING = """
<html><body>
<a href="/en/index.php/mediaroom/press_release_details/circular/55">Circular 55</a>
<a href="/en/index.php/mediaroom/press_release_details/rrpt/12340">Result of the Auction of Repo, ALS, SLF, SDF and IBLF held on 25 May, 2025</a>
<a href="/en/index.php/mediaroom/press_release_details/rrpt/12345">Result of the Auction of Repo, ALS, SLF, SDF and IBLF held on 28 May, 2025</a>
<a href="/en/index.php/mediaroom/press_release_details/notice/9">Office closed notice</a>
</body></html>
"""


def test_discovery_picks_highest_rrpt_id():
    """BB mints rrpt ids monotonically; the highest id is the most recent
    auction-result release."""
    url = discover_latest_rrpt_link(
        html=_LISTING,
        base_url="https://www.bb.org.bd/en/index.php/mediaroom/press_release",
        title_pattern="Result of the Auction",
    )
    assert url.endswith("/rrpt/12345")


def test_discovery_title_filter_excludes_non_auction_rrpt():
    """A title filter keeps only auction-result notices even when other
    /rrpt/ links exist."""
    listing = (
        '<a href="/x/rrpt/999">Monthly economic review</a>'
        '<a href="/x/rrpt/100">Result of the Auction held on 1 Jan, 2025</a>'
    )
    url = discover_latest_rrpt_link(
        html=listing, base_url="https://www.bb.org.bd/x",
        title_pattern="Result of the Auction",
    )
    assert url.endswith("/rrpt/100")


def test_discovery_raises_when_no_rrpt_anchor():
    with pytest.raises(ValueError, match="no /rrpt"):
        discover_latest_rrpt_link(
            html='<a href="/x/circular/1">Circular</a>',
            base_url="https://www.bb.org.bd/x",
            title_pattern="Result of the Auction",
        )


# --- parser: SLF (always present, draw-only) ------------------------------

def test_slf_from_table(tmp_path):
    parser = get_parser("html_auction_press_row")
    res = parser.parse(_artifact(tmp_path, _TABLE_WITH_REPO, "slf_draw_cr"),
                       instruction="instrument=SLF")
    assert res.value == SLF_ACCEPTED_CR


def test_slf_from_prose(tmp_path):
    """BB also renders releases as prose; the parser must read the SLF amount
    from a sentence, not only a <table>."""
    parser = get_parser("html_auction_press_row")
    res = parser.parse(_artifact(tmp_path, _PROSE_NO_REPO, "slf_draw_cr"),
                       instruction="instrument=SLF")
    assert res.value == SLF_ACCEPTED_CR


def test_slf_recovers_held_on_date(tmp_path):
    """source_as_of comes from the 'held on <date>' title so metric_history.as_of
    reflects the auction date, not the run date."""
    parser = get_parser("html_auction_press_row")
    res = parser.parse(_artifact(tmp_path, _TABLE_WITH_REPO, "slf_draw_cr"),
                       instruction="instrument=SLF")
    assert res.source_as_of == date(2025, 5, 28)


# --- parser: Repo (present / absent / measured-zero) ----------------------

def test_repo_present_returns_amount(tmp_path):
    parser = get_parser("html_auction_press_row")
    res = parser.parse(_artifact(tmp_path, _TABLE_WITH_REPO, "bb_repo_usage_cr"),
                       instruction="instrument=Repo")
    assert res.value == REPO_ACCEPTED_CR


def test_repo_absent_raises_parse_error_not_zero(tmp_path):
    """NULL-vs-ZERO contract: on a no-repo day the Repo line is absent, so the
    parser RAISES ParseError (-> LLM null -> needs_review -> dropped). It must
    NOT silently return a fabricated 0 that would land as a measured row."""
    parser = get_parser("html_auction_press_row")
    with pytest.raises(ParseError, match="not found"):
        parser.parse(_artifact(tmp_path, _PROSE_NO_REPO, "bb_repo_usage_cr"),
                     instruction="instrument=Repo")


def test_repo_measured_zero_is_returned_not_dropped(tmp_path):
    """A GENUINE measured 0 (Repo held, nothing accepted) is a real data point
    and IS returned as 0.0 — distinct from the absent-instrument case above."""
    parser = get_parser("html_auction_press_row")
    res = parser.parse(_artifact(tmp_path, _TABLE_REPO_ZERO, "bb_repo_usage_cr"),
                       instruction="instrument=Repo")
    assert res.value == 0.0


def test_instruction_missing_instrument_raises(tmp_path):
    parser = get_parser("html_auction_press_row")
    with pytest.raises(ParseError, match="missing instrument"):
        parser.parse(_artifact(tmp_path, _TABLE_WITH_REPO, "slf_draw_cr"),
                     instruction="unit=crore")
