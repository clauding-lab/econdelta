"""Tests for parsers.html_money_market_ref_rate — BB's DOMMR/BOFR page.

The fixture (tests/fixtures/bb_money_market_ref_rate.html) is a REAL capture
of the live page, fetched 2026-08-28 on the ExonVPS box through
fetchers/html_fetcher.fetch_html — never hand-built (AGENTS.md landmine 45:
a synthetic fixture can invert the real producer's semantics unnoticed).
Its latest business day is 27 August 2026:

    DOMMR: Overnight 9.18 (4025.00 cr, 59 deals), 1W 9.33; 1M 9.86, 3M 9.74
    BOFR:  Overnight 9.23 (3372.43 cr, 42 deals), 1W 9.28

Mutated variants below are string surgery ON that real capture, so the
surrounding structure stays production-true.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import parsers.html_money_market_ref_rate as m
from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.registry import get_parser

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES_DIR / "bb_money_market_ref_rate.html"

EXPECTED_VALUE = {"dommr": 9.18, "dommr_1w": 9.33, "bofr": 9.23, "bofr_1w": 9.28}
EXPECTED_DATE = date(2026, 8, 27)


def _artifact(path: Path) -> FetchResult:
    return FetchResult(
        indicator_id="money_market_ref_rate",
        artifact_path=path,
        artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.bb.org.bd/en/index.php/monetaryactivity/money_market_ref_rate",
        sha256="x" * 64,
        cache_hit=False,
    )


def _mutated(tmp_path: Path, transform) -> FetchResult:
    """Apply ``transform(html) -> html`` to the REAL fixture, write to tmp."""
    html = FIXTURE.read_text(encoding="utf-8")
    out = tmp_path / "mutated.html"
    out.write_text(transform(html), encoding="utf-8")
    return _artifact(out)


def _parse(artifact: FetchResult):
    return get_parser("html_money_market_ref_rate").parse(artifact, "")


class TestHeaderDate:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("27 August, 2026", date(2026, 8, 27)),
            ("07 May, 2026", date(2026, 5, 7)),
            ("7 May, 2026", date(2026, 5, 7)),  # tolerate unpadded day
            ("  15 April, 2026 ", date(2026, 4, 15)),
            ("Overnight", None),
            ("August 27, 2026", None),  # wrong order is NOT a date header
            ("27 Aug, 2026", None),  # abbreviated month is not this page's format
            ("", None),
        ],
    )
    def test_parse_header_date(self, text, expected):
        assert m.parse_header_date(text) == expected


class TestRealFixture:
    def test_exact_four_values_and_iso_date(self):
        result = _parse(_artifact(FIXTURE))
        assert result.value == EXPECTED_VALUE
        assert result.source_as_of == EXPECTED_DATE
        assert result._parse_strategy == "html_money_market_ref_rate"

    def test_1m_and_3m_are_ignored(self):
        result = _parse(_artifact(FIXTURE))
        assert set(result.value) == m._SERIES_KEYS
        # the 1M/3M rates present in the fixture must not leak in anywhere
        assert 9.86 not in result.value.values()
        assert 9.74 not in result.value.values()

    def test_recover_source_as_of_returns_page_date(self):
        parser = get_parser("html_money_market_ref_rate")
        assert parser.recover_source_as_of(_artifact(FIXTURE)) == EXPECTED_DATE

    def test_registered_under_expected_name(self):
        assert isinstance(
            get_parser("html_money_market_ref_rate"), m.HtmlMoneyMarketRefRateParser
        )


def _swap_tables(html: str) -> str:
    """Swap the DOMMR and BOFR sections (header div + table div each) so
    BOFR comes FIRST in document order — anchoring must not care."""
    i_dommr = html.index(">Dhaka Overnight Money Market Rate (DOMMR)</div>")
    i_dommr = html.rindex("<div", 0, i_dommr)
    i_bofr = html.index(">Bangladesh Overnight Financing Rate (BOFR)</div>")
    i_bofr = html.rindex("<div", 0, i_bofr)
    j = html.index("</table>", i_bofr)
    i_end = html.index("</div>", j) + len("</div>")
    seg_dommr = html[i_dommr:i_bofr]
    seg_bofr = html[i_bofr:i_end]
    return html[:i_dommr] + seg_bofr + "\n" + seg_dommr + html[i_end:]


class TestTableOrderIndependence:
    def test_swapped_tables_parse_identically(self, tmp_path):
        result = _parse(_mutated(tmp_path, _swap_tables))
        assert result.value == EXPECTED_VALUE
        assert result.source_as_of == EXPECTED_DATE


class TestFailClosed:
    """Every structural surprise must raise ParseError (→ LLM fallback),
    never return a plausible-looking wrong dict."""

    def test_7d_tenor_label_is_refused(self, tmp_path):
        # The staging-test-data tell: relabel DOMMR's 1W row (first '1W'
        # cell in document order) as '7D'.
        artifact = _mutated(
            tmp_path, lambda h: h.replace("<td>1W</td>", "<td>7D</td>", 1)
        )
        with pytest.raises(ParseError, match="7D"):
            _parse(artifact)

    def test_missing_bofr_table_raises(self, tmp_path):
        artifact = _mutated(
            tmp_path,
            lambda h: h.replace(
                "Bangladesh Overnight Financing Rate (BOFR)", "Something Else"
            ),
        )
        with pytest.raises(ParseError, match="BOFR"):
            _parse(artifact)

    def test_missing_dommr_table_raises(self, tmp_path):
        artifact = _mutated(
            tmp_path,
            lambda h: h.replace(
                "Dhaka Overnight Money Market Rate (DOMMR)", "Something Else"
            ),
        )
        with pytest.raises(ParseError, match="DOMMR"):
            _parse(artifact)

    def test_deleted_dommr_table_raises_not_bleeds(self, tmp_path):
        """2026-08-28 review finding 1 (CONFIRMED pre-fix): with only the
        DOMMR <table> deleted (heading LEFT IN PLACE), the old unbounded
        ``find_next("table")`` walk resolved BOTH anchors to the BOFR table
        and parsed {'dommr': 9.23, 'dommr_1w': 9.28, ...} — BOFR published
        as DOMMR with no error, and same table meant same date, so the
        date-disagreement guard could never fire. The bounded walk must
        refuse instead. (test_missing_dommr_table_raises above renames the
        HEADER; this one deletes the TABLE — different failure mode.)"""

        def delete_dommr_table(h: str) -> str:
            i_head = h.index("Dhaka Overnight Money Market Rate (DOMMR)")
            i_tab = h.index("<table>", i_head)
            j = h.index("</table>", i_tab) + len("</table>")
            return h[:i_tab] + h[j:]

        with pytest.raises(ParseError, match="DOMMR"):
            _parse(_mutated(tmp_path, delete_dommr_table))

    def test_same_table_resolution_refused_even_past_the_bounded_walk(
        self, tmp_path, monkeypatch
    ):
        """Belt-and-braces layer of finding 1: even if the bounded walk were
        somehow defeated, parse() must refuse two anchors resolving to the
        SAME <table> object."""
        real_find = m.find_rate_table
        monkeypatch.setattr(
            m, "find_rate_table", lambda soup, header: real_find(soup, m.BOFR_HEADER)
        )
        with pytest.raises(ParseError, match="SAME"):
            _parse(_artifact(FIXTURE))

    def test_missing_overnight_tenor_raises(self, tmp_path):
        artifact = _mutated(
            tmp_path,
            lambda h: h.replace(
                "<tr><td>Overnight</td><td>4025.00</td><td>9.18</td><td>59</td></tr>",
                "",
            ),
        )
        with pytest.raises(ParseError, match="overnight"):
            _parse(artifact)

    def test_missing_1w_tenor_raises(self, tmp_path):
        artifact = _mutated(
            tmp_path,
            lambda h: h.replace(
                "<tr><td>1W</td><td>14004.01</td><td>9.28</td><td>116</td></tr>",
                "",
            ),
        )
        with pytest.raises(ParseError, match="1w"):
            _parse(artifact)

    def test_missing_date_header_raises(self, tmp_path):
        # Strip BOTH date-header rows — a table with data rows but no date
        # block must refuse, not stamp the run date.
        artifact = _mutated(
            tmp_path,
            lambda h: h.replace(
                '<tr><td colspan="5" class="page_header" '
                'style="font-weight: 400!important">27 August, 2026</td></tr>',
                "",
            ),
        )
        with pytest.raises(ParseError, match="no date-header block"):
            _parse(artifact)

    def test_disagreeing_table_dates_raise(self, tmp_path):
        # Retard only the BOFR table's date header (the SECOND occurrence)
        # — a half-updated page must not publish.
        def retard_bofr_date(h: str) -> str:
            i_bofr = h.index("Bangladesh Overnight Financing Rate (BOFR)")
            return h[:i_bofr] + h[i_bofr:].replace(
                "27 August, 2026", "25 August, 2026", 1
            )

        with pytest.raises(ParseError, match="half-updated"):
            _parse(_mutated(tmp_path, retard_bofr_date))


class TestRateColumnResolution:
    """2026-08-28 review finding 2 (CONFIRMED pre-fix): the rate column was a
    hardcoded ``texts[2]`` — the thead was never read — so one inserted
    column published 4025.0 (the Amount in crore Taka) as the DOMMR
    percentage, provenance "deterministic", no exception. AND hybrid's dict
    branch skips validate_value, so config valid_range never caught it. The
    column is now resolved per table from its own '(%)' header cell, and
    parse() range-checks all four floats itself."""

    def test_inserted_data_column_raises_not_publishes_amount(self, tmp_path):
        """Data rows shifted against an unchanged header (the reviewer's
        4025.0 scenario): the resolved '(%)' column now lands on the Amount
        cell and the in-parser range check refuses it."""

        def insert_data_column(h: str) -> str:
            h = h.replace(
                "<tr><td>Overnight</td><td>4025.00</td>",
                "<tr><td>Overnight</td><td>-</td><td>4025.00</td>", 1,
            )
            return h.replace(
                "<tr><td>1W</td><td>6052.37</td>",
                "<tr><td>1W</td><td>-</td><td>6052.37</td>", 1,
            )

        with pytest.raises(ParseError, match="outside the sanity envelope"):
            _parse(_mutated(tmp_path, insert_data_column))

    def test_genuine_new_column_header_and_data_reads_true_rate(self, tmp_path):
        """When BB genuinely inserts a column (header AND data rows), the
        header-resolved column keeps reading the TRUE '(%)' cell — the old
        positional index read the shifted neighbour (4025.0)."""

        def insert_full_column(h: str) -> str:
            h = h.replace("<th>Product</th>", "<th>Product</th><th>Extra</th>", 1)
            for row_start in (
                "<tr><td>Overnight</td><td>4025.00</td>",
                "<tr><td>1W</td><td>6052.37</td>",
                "<tr><td>1M</td><td>330.00</td>",
                "<tr><td>3M</td><td>230.20</td>",
            ):
                first_cell, rest = row_start.split("</td>", 1)
                h = h.replace(row_start, first_cell + "</td><td>-</td>" + rest, 1)
            return h

        result = _parse(_mutated(tmp_path, insert_full_column))
        assert result.value == EXPECTED_VALUE
        assert result.source_as_of == EXPECTED_DATE

    def test_no_percent_header_cell_raises(self, tmp_path):
        """Zero '(%)' candidates (landmine-44 ambiguity discipline)."""
        artifact = _mutated(tmp_path, lambda h: h.replace("DOMMR (%)", "DOMMR", 1))
        with pytest.raises(ParseError, match="exactly ONE"):
            _parse(artifact)

    def test_multiple_percent_header_cells_raise(self, tmp_path):
        """Two '(%)' candidates must refuse, never silently pick one."""
        artifact = _mutated(
            tmp_path,
            lambda h: h.replace("<th>Amount<br>(Crore Taka)</th>",
                                "<th>Amount (%)</th>", 1),
        )
        with pytest.raises(ParseError, match="exactly ONE"):
            _parse(artifact)

    def test_parser_envelope_matches_config_valid_range(self):
        """Drift guard: the in-parser envelope (also imported by the backfill
        script) must stay equal to the config entry's valid_range."""
        cfg = json.loads(
            (Path(__file__).resolve().parent.parent / "config" / "sources-v3.json")
            .read_text()
        )
        ind = [i for i in cfg["indicators"] if i["id"] == "money_market_ref_rate"]
        assert len(ind) == 1
        assert ind[0]["parse"]["valid_range"] == [m._MIN_RATE, m._MAX_RATE]


class TestNewestBlockByDate:
    def test_ordering_flip_still_returns_newest_date(self, tmp_path):
        """Finding 3: _newest_block used blocks[0] (document order). Prepend
        an OLDER 25 Aug block inside BOTH tbodys — both tables then AGREE on
        the wrong day, so the date-disagreement guard can't help. Picking
        max() by date must still return the 27 Aug values."""
        older_dommr = (
            '<tr><td colspan="5" class="page_header" style="font-weight: '
            '400!important">25 August, 2026</td></tr>'
            "<tr><td>Overnight</td><td>4000.00</td><td>9.15</td><td>50</td></tr>"
            "<tr><td>1W</td><td>6000.00</td><td>9.30</td><td>60</td></tr>"
        )
        older_bofr = (
            '<tr><td colspan="5" class="page_header" style="font-weight: '
            '400!important">25 August, 2026</td></tr>'
            "<tr><td>Overnight</td><td>3300.00</td><td>9.20</td><td>40</td></tr>"
            "<tr><td>1W</td><td>14000.00</td><td>9.25</td><td>110</td></tr>"
        )

        def flip_order(h: str) -> str:
            # First tbody is DOMMR's, second is BOFR's (real capture layout).
            h = h.replace("</thead><tbody>", "</thead><tbody>" + older_dommr, 1)
            i_bofr = h.index("Bangladesh Overnight Financing Rate (BOFR)")
            return h[:i_bofr] + h[i_bofr:].replace(
                "</thead><tbody>", "</thead><tbody>" + older_bofr, 1
            )

        result = _parse(_mutated(tmp_path, flip_order))
        assert result.value == EXPECTED_VALUE
        assert result.source_as_of == EXPECTED_DATE


class TestRecoverSourceAsOfBestEffort:
    def test_returns_none_when_no_date_headers(self, tmp_path):
        artifact = _mutated(tmp_path, lambda h: h.replace("27 August, 2026", "n/a"))
        parser = get_parser("html_money_market_ref_rate")
        assert parser.recover_source_as_of(artifact) is None

    def test_never_raises_on_unreadable_artifact(self, tmp_path):
        parser = get_parser("html_money_market_ref_rate")
        artifact = _artifact(tmp_path / "does_not_exist.html")
        assert parser.recover_source_as_of(artifact) is None
