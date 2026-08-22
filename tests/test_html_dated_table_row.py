"""Tests for parsers.html_dated_table_row -- the deterministic parser
behind the PR-C CPI/private-credit/M2 repoint (build-brief items 2 and 4).

Fixtures (tests/fixtures/bb_inflation.html, bb_monetarysurvey.html,
bb_moneysupply.html) are TRIMMED REAL CAPTURES of the three live BB pages
(fetched 2026-08-22 via fetchers.html_fetcher.fetch_html), not hand-built --
AGENT_LEARNINGS.md's explicit lesson that a synthetic fixture can invert
the real producer's column/row semantics without anyone noticing.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import parsers.html_dated_table_row as m
from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.registry import get_parser

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _artifact(name: str) -> FetchResult:
    return FetchResult(
        indicator_id="x",
        artifact_path=FIXTURES_DIR / name,
        artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.bb.org.bd/en/index.php/econdata/x",
        sha256="x" * 64,
        cache_hit=False,
    )


class TestInstructionParsing:
    def test_requires_row_and_col(self):
        with pytest.raises(ParseError):
            m._parse_instruction("col=latest")

    def test_rejects_unknown_col_slot(self):
        with pytest.raises(ParseError):
            m._parse_instruction('row="X" col=bogus')

    def test_parses_row_section_col(self):
        row, section, col = m._parse_instruction(
            'row="Claims on Private Sector" section="deposit money banks" col=yoy_pct'
        )
        assert row == "Claims on Private Sector"
        assert section == "deposit money banks"
        assert col == "yoy_pct"

    def test_section_is_optional(self):
        row, section, col = m._parse_instruction('row="Point to point" col=latest')
        assert section is None


class TestMonthYear:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Jul, 2026", (2026, 7)),
            ("Jun,2026", (2026, 6)),
            ("May, 2025", (2025, 5)),
            ("not a month", None),
            ("", None),
        ],
    )
    def test_parses_common_forms(self, text, expected):
        assert m._month_year(text) == expected


class TestToNumber:
    def test_strips_percent_sign(self):
        assert m._to_number("8.32%") == pytest.approx(8.32)

    def test_negative_plain(self):
        assert m._to_number("-5.49") == pytest.approx(-5.49)

    def test_accounting_negative_parens(self):
        assert m._to_number("(1.23)") == pytest.approx(-1.23)

    def test_unparseable_raises(self):
        with pytest.raises(ParseError):
            m._to_number("--")


class TestInflationFixture:
    """econdata/inflation -- 2 plain rows, NO Percentage Changes group."""

    def test_p2p_latest_month(self):
        p = get_parser("html_dated_table_row")
        r = p.parse(_artifact("bb_inflation.html"), 'row="Point to point" col=latest')
        assert r.value == pytest.approx(8.32)
        assert r.source_as_of == date(2026, 7, 31)

    def test_12m_avg_latest_month(self):
        p = get_parser("html_dated_table_row")
        r = p.parse(
            _artifact("bb_inflation.html"),
            'row="Monthly Average(Twelve Month)" col=latest',
        )
        assert r.value == pytest.approx(8.66)
        assert r.source_as_of == date(2026, 7, 31)

    def test_yoy_pct_raises_when_no_pct_group(self):
        p = get_parser("html_dated_table_row")
        with pytest.raises(ParseError, match="no 'Percentage"):
            p.parse(_artifact("bb_inflation.html"), 'row="Point to point" col=yoy_pct')

    def test_unmatched_row_raises(self):
        p = get_parser("html_dated_table_row")
        with pytest.raises(ParseError, match="not found"):
            p.parse(_artifact("bb_inflation.html"), 'row="Nonexistent Row" col=latest')

    def test_row_match_is_exact_not_substring(self):
        """'Point to' (a substring of the real label) must NOT match --
        landmine 46's quoted-row discipline: exact match only."""
        p = get_parser("html_dated_table_row")
        with pytest.raises(ParseError, match="not found"):
            p.parse(_artifact("bb_inflation.html"), 'row="Point to" col=latest')


class TestMonetarySurveyFixture:
    """econdata/monetarysurvey -- the confirmed-live ambiguity: 'Claims on
    Private Sector' appears once under (a) BANGLADESH BANK and once under
    (b) DEPOSIT MONEY BANKS, with materially different values."""

    def test_section_required_when_row_is_ambiguous(self):
        p = get_parser("html_dated_table_row")
        with pytest.raises(ParseError, match="ambiguous"):
            p.parse(_artifact("bb_monetarysurvey.html"), 'row="Claims on Private Sector" col=yoy_pct')

    def test_dmb_section_resolves_to_the_correct_row(self):
        p = get_parser("html_dated_table_row")
        r = p.parse(
            _artifact("bb_monetarysurvey.html"),
            'row="Claims on Private Sector" section="deposit money banks" col=yoy_pct',
        )
        assert r.value == pytest.approx(4.53)
        assert r.source_as_of == date(2026, 6, 30)

    def test_bb_section_resolves_to_the_other_row(self):
        p = get_parser("html_dated_table_row")
        r = p.parse(
            _artifact("bb_monetarysurvey.html"),
            'row="Claims on Private Sector" section="bangladesh bank" col=yoy_pct',
        )
        assert r.value == pytest.approx(-5.49)

    def test_mom_pct_is_the_first_pct_column(self):
        p = get_parser("html_dated_table_row")
        r = p.parse(
            _artifact("bb_monetarysurvey.html"),
            'row="Claims on Private Sector" section="deposit money banks" col=mom_pct',
        )
        assert r.value == pytest.approx(0.52)

    def test_latest_absolute_value(self):
        p = get_parser("html_dated_table_row")
        r = p.parse(
            _artifact("bb_monetarysurvey.html"),
            'row="Claims on Private Sector" section="deposit money banks" col=latest',
        )
        assert r.value == pytest.approx(18170873.0)

    def test_unrelated_top_level_row_is_unambiguous(self):
        """A row with no sibling of the same label anywhere else needs no
        section= at all."""
        p = get_parser("html_dated_table_row")
        r = p.parse(_artifact("bb_monetarysurvey.html"), 'row="1.  NET   FOREIGN   ASSETS" col=yoy_pct')
        assert r.value == pytest.approx(20.94)


class TestMoneySupplyFixture:
    def test_m2_yoy_pct(self):
        p = get_parser("html_dated_table_row")
        r = p.parse(
            _artifact("bb_moneysupply.html"),
            'row="6. Money Supply(M2) (4+5)" col=yoy_pct',
        )
        assert r.value == pytest.approx(11.11)
        assert r.source_as_of == date(2026, 6, 30)

    def test_m2_latest_absolute(self):
        p = get_parser("html_dated_table_row")
        r = p.parse(
            _artifact("bb_moneysupply.html"),
            'row="6. Money Supply(M2) (4+5)" col=latest',
        )
        assert r.value == pytest.approx(24162863.0)


class TestNoSortableTable:
    def test_raises_when_table_missing(self, tmp_path):
        path = tmp_path / "empty.html"
        path.write_text("<html><body>no table here</body></html>", encoding="utf-8")
        artifact = FetchResult(
            indicator_id="x", artifact_path=path, artifact_type="html",
            fetched_at=datetime.now(timezone.utc), source_url="x", sha256="x" * 64,
            cache_hit=False,
        )
        p = get_parser("html_dated_table_row")
        with pytest.raises(ParseError, match="sortableTable"):
            p.parse(artifact, 'row="X" col=latest')
