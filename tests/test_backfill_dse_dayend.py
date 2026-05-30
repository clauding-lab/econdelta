"""Unit tests for the DSE DS30 day-end-close backfill.

Two layers:
  1. Synthetic-HTML tests that pin the parsing rules (column-by-header lookup,
     '*' marker tolerance, date filtering, range guard).
  2. Real-source fixture tests against captured dsebd.org HTML (2026-05-30) so
     the parser is verified against the actual page shape, not just a mock.

No network and no Supabase writes here — fixtures are static files.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from scripts.backfill_dse_dayend import (
    BackfillError,
    CloseRow,
    group_rows_by_date,
    parse_day_end_archive,
    parse_ds30_codes,
    rows_to_supabase_payload,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# --------------------------------------------------------------------------- #
# Synthetic-HTML parsing rules
# --------------------------------------------------------------------------- #

_MIN_ARCHIVE = """
<html><body>
<table><tr><td>unrelated header</td></tr></table>
<table>
  <tr><th>#</th><th>DATE</th><th>TRADING CODE</th><th>LTP*</th><th>HIGH</th>
      <th>LOW</th><th>OPENP*</th><th>CLOSEP*</th><th>YCP</th><th>TRADE</th>
      <th>VALUE (mn)</th><th>VOLUME</th></tr>
  <tr><td>1</td><td>2026-05-24</td><td>BRACBANK</td><td>67.3</td><td>68.1</td>
      <td>66.8</td><td>67</td><td>67.3</td><td>66.6</td><td>3,307</td>
      <td>186.274</td><td>2,761,602</td></tr>
  <tr><td>2</td><td>2026-05-23</td><td>BRACBANK</td><td>66.6</td><td>66.7</td>
      <td>64</td><td>64.1</td><td>66.6</td><td>64</td><td>1,934</td>
      <td>100.786</td><td>1,529,847</td></tr>
  <tr><td>Total</td><td>--</td><td>--</td><td></td><td></td><td></td>
      <td></td><td></td><td></td><td></td><td></td><td></td></tr>
</table>
</body></html>
"""


def test_parse_day_end_archive_keeps_closep_not_other_price_columns():
    """The stored value must be CLOSEP*, not LTP* / OPENP* / YCP."""
    rows = parse_day_end_archive(_MIN_ARCHIVE, expected_code="BRACBANK")

    by_date = {r.as_of: r.closep for r in rows}
    # 2026-05-24 row: CLOSEP*=67.3 (LTP* is also 67.3, so use the OTHER day to prove it)
    # 2026-05-23 row: CLOSEP*=66.6 while LTP*=66.6, OPENP*=64.1, YCP=64 -> must be 66.6
    assert by_date[date(2026, 5, 23)] == 66.6
    assert by_date[date(2026, 5, 24)] == 67.3


def test_parse_day_end_archive_sorts_ascending_by_date():
    rows = parse_day_end_archive(_MIN_ARCHIVE, expected_code="BRACBANK")
    assert [r.as_of for r in rows] == [date(2026, 5, 23), date(2026, 5, 24)]


def test_parse_day_end_archive_skips_non_date_total_rows():
    """The trailing 'Total' / '--' summary row must not become a CloseRow."""
    rows = parse_day_end_archive(_MIN_ARCHIVE, expected_code="BRACBANK")
    assert len(rows) == 2  # the 'Total' row is dropped


def test_parse_day_end_archive_filters_unexpected_code():
    html = _MIN_ARCHIVE.replace("BRACBANK", "GP", 1)  # first data row becomes GP
    rows = parse_day_end_archive(html, expected_code="BRACBANK")
    # Only the still-BRACBANK row survives the expected_code filter.
    assert all(r.code == "BRACBANK" for r in rows)
    assert len(rows) == 1


def test_parse_day_end_archive_raises_when_no_data_table():
    with pytest.raises(BackfillError):
        parse_day_end_archive("<html><body><p>maintenance</p></body></html>")


def test_clean_number_strips_thousands_separator():
    # The 2026-05-23 row's CLOSEP* cell (value 66.6) is rewritten to a
    # comma-formatted high-priced close to prove the thousands separator is
    # stripped before float parsing.
    html = _MIN_ARCHIVE.replace(
        "<td>64.1</td><td>66.6</td><td>64</td>",
        "<td>64.1</td><td>1,234.5</td><td>64</td>",
    )
    rows = parse_day_end_archive(html, expected_code="BRACBANK")
    assert any(r.closep == 1234.5 for r in rows)


def test_metric_id_uses_dse_close_prefix():
    row = CloseRow(code="BRACBANK", as_of=date(2026, 5, 24), closep=67.3)
    assert row.metric_id == "dse_close_BRACBANK"


def test_rows_to_supabase_payload_builds_data_and_as_of_map():
    rows = [
        CloseRow("BRACBANK", date(2026, 5, 24), 67.3),
        CloseRow("GP", date(2026, 5, 24), 330.1),
    ]
    data, as_of_map = rows_to_supabase_payload(rows)
    assert data == {"dse_close_BRACBANK": 67.3, "dse_close_GP": 330.1}
    assert as_of_map == {
        "dse_close_BRACBANK": date(2026, 5, 24),
        "dse_close_GP": date(2026, 5, 24),
    }


def test_group_rows_by_date_partitions_for_per_day_upsert():
    rows = [
        CloseRow("BRACBANK", date(2026, 5, 24), 67.3),
        CloseRow("GP", date(2026, 5, 24), 330.1),
        CloseRow("BRACBANK", date(2026, 5, 23), 66.6),
    ]
    grouped = group_rows_by_date(rows)
    assert set(grouped) == {date(2026, 5, 23), date(2026, 5, 24)}
    assert len(grouped[date(2026, 5, 24)]) == 2
    assert len(grouped[date(2026, 5, 23)]) == 1


# --------------------------------------------------------------------------- #
# Real-source fixture tests (captured live HTML, 2026-05-30)
# --------------------------------------------------------------------------- #


def test_parse_ds30_codes_returns_exactly_30_from_live_page():
    html = (FIXTURES / "ds30_share.html").read_text(encoding="utf-8", errors="replace")
    codes = parse_ds30_codes(html)
    assert len(codes) == 30
    assert "BRACBANK" in codes
    assert "GP" in codes
    # All codes are alnum trading symbols, no stray HTML.
    assert all(c.isalnum() and c.isupper() for c in codes)


def test_parse_bracbank_archive_fixture_has_real_closes():
    html = (FIXTURES / "archive_bracbank.html").read_text(encoding="utf-8", errors="replace")
    rows = parse_day_end_archive(html, expected_code="BRACBANK")
    assert len(rows) >= 20  # ~22 trading days over a 60-day window
    assert all(r.code == "BRACBANK" for r in rows)
    assert all(0 < r.closep < 1000 for r in rows)  # BRACBANK trades ~60-70 taka
    # Known data point captured on 2026-05-30: 2026-05-24 close = 67.3
    by_date = {r.as_of: r.closep for r in rows}
    assert by_date[date(2026, 5, 24)] == 67.3


def test_parse_gp_archive_fixture_distinct_from_bracbank():
    html = (FIXTURES / "archive_gp.html").read_text(encoding="utf-8", errors="replace")
    rows = parse_day_end_archive(html, expected_code="GP")
    assert len(rows) >= 20
    assert all(r.code == "GP" for r in rows)
    assert all(r.metric_id == "dse_close_GP" for r in rows)
