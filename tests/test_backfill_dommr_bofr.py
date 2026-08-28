"""Tests for scripts/backfill_dommr_bofr.py — parse/filter logic ONLY.

No fetch is exercised here (the real dry-run happens on the ExonVPS box; a
hostile, rate-limited host earns zero test-suite requests). The parse path
is tested two ways:

1. Against the REAL single-day capture
   (tests/fixtures/bb_money_market_ref_rate.html, fetched 2026-08-28 on the
   box through fetchers/html_fetcher — landmine 45: real captures only).
2. Against a synthetic MULTI-date document built FROM that capture's real
   blocks — the range-POST response shape (several date-header blocks per
   tbody, newest first) — including a pre-launch staging block carrying the
   '7D' tenor tell, to prove both hard filters.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

import scripts.backfill_dommr_bofr as bf

FIXTURE = Path(__file__).parent / "fixtures" / "bb_money_market_ref_rate.html"
FIXTURE_DATE = date(2026, 8, 27)

# The real capture's per-tbody block strings (verbatim), used to synthesize
# a multi-date range response from REAL building blocks.
_DOMMR_BLOCK = (
    '<tr><td colspan="5" class="page_header" style="font-weight: 400!important">'
    "27 August, 2026</td></tr>"
    "<tr><td>Overnight</td><td>4025.00</td><td>9.18</td><td>59</td></tr>"
    "<tr><td>1W</td><td>6052.37</td><td>9.33</td><td>68</td></tr>"
    "<tr><td>1M</td><td>330.00</td><td>9.86</td><td>5</td></tr>"
    "<tr><td>3M</td><td>230.20</td><td>9.74</td><td>9</td></tr>"
)
_BOFR_BLOCK = (
    '<tr><td colspan="5" class="page_header" style="font-weight: 400!important">'
    "27 August, 2026</td></tr>"
    "<tr><td>Overnight</td><td>3372.43</td><td>9.23</td><td>42</td></tr>"
    "<tr><td>1W</td><td>14004.01</td><td>9.28</td><td>116</td></tr>"
)


def _real_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _multi_date_html() -> str:
    """Append, inside each real tbody, an older business-day block plus a
    PRE-LAUNCH staging block (14 April, 2026 — the day before launch) whose
    one-week tenor carries the '7D' test-data label. All blocks are copies
    of the capture's real rows with only date/value text substituted."""
    older_dommr = (
        _DOMMR_BLOCK.replace("27 August, 2026", "25 August, 2026")
        .replace("9.18", "9.15")
        .replace("9.33", "9.30")
    )
    staging_dommr = (
        _DOMMR_BLOCK.replace("27 August, 2026", "14 April, 2026")
        .replace("<td>1W</td>", "<td>7D</td>")
    )
    older_bofr = (
        _BOFR_BLOCK.replace("27 August, 2026", "25 August, 2026")
        .replace("9.23", "9.20")
        .replace("9.28", "9.25")
    )
    staging_bofr = (
        _BOFR_BLOCK.replace("27 August, 2026", "14 April, 2026")
        .replace("<td>1W</td>", "<td>7D</td>")
    )
    html = _real_html()
    html = html.replace(_DOMMR_BLOCK, _DOMMR_BLOCK + older_dommr + staging_dommr)
    html = html.replace(_BOFR_BLOCK, _BOFR_BLOCK + older_bofr + staging_bofr)
    return html


def _by_key(rows) -> dict[tuple[str, str], float]:
    return {(r.metric_id, r.as_of.isoformat()): r.value for r in rows}


class TestSyntheticBlocksAreReal:
    def test_block_strings_verbatim_in_fixture(self):
        """Guard: the building blocks above must stay byte-identical to the
        real capture, or the synthetic multi-date doc quietly stops being
        'built from real blocks'."""
        html = _real_html()
        assert _DOMMR_BLOCK in html
        assert _BOFR_BLOCK in html


class TestParseHistoryHtmlSingleDay:
    def test_real_capture_yields_five_rows_for_the_business_day(self):
        rows = bf.parse_history_html(_real_html())
        got = _by_key(rows)
        assert got == {
            ("dommr", "2026-08-27"): 9.18,
            ("dommr_1w", "2026-08-27"): 9.33,
            ("bofr", "2026-08-27"): 9.23,
            ("bofr_1w", "2026-08-27"): 9.28,
            ("money_market_ref_rate", "2026-08-27"): 9.18,
        }

    def test_1m_3m_never_minted(self):
        rows = bf.parse_history_html(_real_html())
        values = {r.value for r in rows}
        assert 9.86 not in values  # 1M
        assert 9.74 not in values  # 3M


class TestParseHistoryHtmlMultiDate:
    def test_all_business_days_minted(self):
        rows = bf.parse_history_html(_multi_date_html())
        got = _by_key(rows)
        # newest day (real values)
        assert got[("dommr", "2026-08-27")] == 9.18
        assert got[("bofr_1w", "2026-08-27")] == 9.28
        # older day (edited values)
        assert got[("dommr", "2026-08-25")] == 9.15
        assert got[("dommr_1w", "2026-08-25")] == 9.30
        assert got[("bofr", "2026-08-25")] == 9.20
        assert got[("bofr_1w", "2026-08-25")] == 9.25
        assert got[("money_market_ref_rate", "2026-08-25")] == 9.15

    def test_pre_launch_dates_hard_filtered(self):
        rows = bf.parse_history_html(_multi_date_html())
        assert not [r for r in rows if r.as_of < bf.LAUNCH_DATE]
        assert not [r for r in rows if r.as_of == date(2026, 4, 14)]

    def test_7d_tenor_never_minted(self):
        """Even if a 7D-labelled block slipped PAST the date filter, only
        Overnight/1W ever mint — prove it with a post-launch 7D block."""
        html = _real_html().replace(
            _DOMMR_BLOCK,
            _DOMMR_BLOCK
            + _DOMMR_BLOCK.replace("27 August, 2026", "20 August, 2026").replace(
                "<td>1W</td>", "<td>7D</td>"
            ),
        )
        rows = bf.parse_history_html(html)
        aug20 = [r for r in rows if r.as_of == date(2026, 8, 20)]
        # Overnight minted (a real reading), the 7D-labelled one-week is NOT.
        assert {r.metric_id for r in aug20} == {"dommr", "money_market_ref_rate"}

    def test_out_of_range_rate_skipped(self):
        html = _real_html().replace(
            "<tr><td>Overnight</td><td>3372.43</td><td>9.23</td><td>42</td></tr>",
            "<tr><td>Overnight</td><td>3372.43</td><td>92.30</td><td>42</td></tr>",
        )
        rows = bf.parse_history_html(html)
        assert ("bofr", "2026-08-27") not in _by_key(rows)
        # sibling series unaffected
        assert ("bofr_1w", "2026-08-27") in _by_key(rows)


class TestChunking:
    def test_single_chunk_when_window_fits(self):
        assert bf.chunk_ranges(date(2026, 4, 15), date(2026, 5, 14), 50) == [
            (date(2026, 4, 15), date(2026, 5, 14))
        ]

    def test_chunks_cover_window_without_overlap_or_gap(self):
        start, end = date(2026, 4, 15), date(2026, 8, 28)
        chunks = bf.chunk_ranges(start, end, 50)
        assert chunks[0][0] == start
        assert chunks[-1][1] == end
        for (_a_start, a_end), (b_start, _b_end) in zip(chunks, chunks[1:]):
            assert (b_start - a_end).days == 1  # contiguous, no overlap, ordered
        assert all((c_end - c_start).days + 1 <= 50 for c_start, c_end in chunks)

    def test_rejects_inverted_window(self):
        with pytest.raises(ValueError):
            bf.chunk_ranges(date(2026, 5, 1), date(2026, 4, 1), 50)


class TestDatePickerFormat:
    def test_space_hyphen_space_ddmmyyyy(self):
        # BB's form contract, verified live: 'dd/mm/yyyy - dd/mm/yyyy'.
        assert (
            bf.format_date_picker_range(date(2026, 4, 15), date(2026, 8, 28))
            == "15/04/2026 - 28/08/2026"
        )

    def test_day_and_month_zero_padded(self):
        assert (
            bf.format_date_picker_range(date(2026, 5, 7), date(2026, 5, 9))
            == "07/05/2026 - 09/05/2026"
        )


class TestSupabasePayload:
    def test_per_day_payload_maps_every_metric_to_its_own_date(self):
        rows = bf.parse_history_html(_multi_date_html())
        grouped = bf.group_rows_by_date(rows)
        assert set(grouped) == {date(2026, 8, 27), date(2026, 8, 25)}
        for business_day, day_rows in grouped.items():
            data, as_of_map = bf.rows_to_supabase_payload(day_rows)
            assert set(data) == set(as_of_map)
            assert all(d == business_day for d in as_of_map.values()), (
                "a fanned row's as_of drifted off its own business day — "
                "the run-date-forgery class this backfill must never ship"
            )

    def test_dedupe_keeps_one_row_per_metric_day(self):
        rows = bf.parse_history_html(_real_html())
        doubled = rows + rows
        assert bf.dedupe_rows(doubled) == sorted(
            rows, key=lambda r: (r.as_of, r.metric_id)
        )
