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

    def test_second_bb_dmb_pair_under_net_other_assets_does_not_collide(self):
        """M4: the FULL fixture (Opus review round 1) has a SECOND (a)/(b)
        BANGLADESH BANK / DEPOSIT MONEY BANKS pair under 'B. NET OTHER
        ASSETS' -- neither has a "Claims on Private Sector" child, so the
        original ambiguity (only 2 real matches, both under 'A. DOMESTIC
        CREDIT') must still resolve exactly as before; the second pair
        existing at all must not silently introduce a third candidate."""
        p = get_parser("html_dated_table_row")
        r = p.parse(
            _artifact("bb_monetarysurvey.html"),
            'row="Claims on Private Sector" section="deposit money banks" col=yoy_pct',
        )
        assert r.value == pytest.approx(4.53)  # unchanged from the trimmed-fixture assertion


class TestBracketedLetterSectionKnownLimitation:
    """M4 (Opus review round 1): documents a KNOWN, deliberately-NOT-fixed
    limitation of _SECTION_RE (r'^\\([a-zA-Z]\\)\\s') -- it matches ANY
    single bracketed Latin letter as a section marker, including a
    Roman-numeral-style sub-item like "(i)" that isn't semantically a new
    section. None of the 3 real pages this parser currently serves (bb_
    inflation/monetarysurvey/moneysupply) contain such a row -- this is a
    synthetic, hand-built table specifically to PIN the current behavior
    as a documented limitation, not a real capture (contrast with the
    fixture-based tests above)."""

    def test_an_intervening_i_row_is_wrongly_treated_as_a_new_section(self, tmp_path):
        html = """
        <table id="sortableTable">
        <thead><tr><th>Label</th><th>Jul, 2026</th><th>Jun, 2026</th></tr></thead>
        <tbody>
        <tr><td>(a) SECTION A</td><td></td><td></td></tr>
        <tr><td>(i) a romanette sub-list intro, not a real section</td><td></td><td></td></tr>
        <tr><td>Foo</td><td>1.0</td><td>0.9</td></tr>
        <tr><td>(b) SECTION B</td><td></td><td></td></tr>
        <tr><td>Foo</td><td>3.0</td><td>2.9</td></tr>
        </tbody>
        </table>
        """
        p = get_parser("html_dated_table_row")
        # "Foo" under (a) SECTION A is now unreachable via section="section a"
        # -- the intervening "(i)" row overwrote current_section before "Foo"
        # was reached. This is the documented limitation, not the desired
        # behavior; a future fix would need a real observed table shape to
        # design against (see the module docstring's section= discussion).
        with pytest.raises(ParseError, match="not found"):
            p.parse(_artifact_from_html(tmp_path, html), 'row="Foo" section="section a" col=latest')
        # section="b" (after the (i) row) is unaffected either way.
        r = p.parse(_artifact_from_html(tmp_path, html), 'row="Foo" section="section b" col=latest')
        assert r.value == pytest.approx(3.0)


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


def _artifact_from_html(tmp_path: Path, html: str) -> FetchResult:
    path = tmp_path / "synthetic.html"
    path.write_text(html, encoding="utf-8")
    return FetchResult(
        indicator_id="x", artifact_path=path, artifact_type="html",
        fetched_at=datetime.now(timezone.utc), source_url="x", sha256="x" * 64,
        cache_hit=False,
    )


# ---------------------------------------------------------------------------
# Opus review round 1, M1/M2: latest column resolved by comparing the
# actual (year, month) VALUES, never by trusting column position.
# ---------------------------------------------------------------------------


class TestLatestColumnResolvedByValueNotPosition:
    def test_out_of_order_header_still_finds_the_true_latest_column(self, tmp_path):
        """Columns deliberately OLDEST-first (the reverse of BB's real
        convention) -- if the parser trusted position 1 == "latest" (the
        pre-fix hardcoding), it would return June's value instead of
        July's. M1 fixes _resolve_header to sort by date; M2 fixes parse()
        to use the resolved index instead of a hardcoded 1."""
        html = """
        <table id="sortableTable">
        <thead><tr><th>Label</th><th>Jun, 2026</th><th>Jul, 2026</th></tr></thead>
        <tbody><tr><td>Point to point</td><td>9.16%</td><td>8.32%</td></tr></tbody>
        </table>
        """
        p = get_parser("html_dated_table_row")
        r = p.parse(_artifact_from_html(tmp_path, html), 'row="Point to point" col=latest')
        assert r.value == pytest.approx(8.32)  # July's value, not June's (position 1)
        assert r.source_as_of == date(2026, 7, 31)

    def test_three_way_shuffle_still_resolves_correctly(self, tmp_path):
        html = """
        <table id="sortableTable">
        <thead><tr><th>Label</th><th>May, 2026</th><th>Jul, 2026</th><th>Jun, 2026</th></tr></thead>
        <tbody><tr><td>X</td><td>1.0</td><td>3.0</td><td>2.0</td></tr></tbody>
        </table>
        """
        p = get_parser("html_dated_table_row")
        r = p.parse(_artifact_from_html(tmp_path, html), 'row="X" col=latest')
        assert r.value == pytest.approx(3.0)  # July's column, wherever it sits
        assert r.source_as_of == date(2026, 7, 31)


# ---------------------------------------------------------------------------
# Opus review round 1, M3: a dash/placeholder in the latest column falls
# back to the prior month's column + ITS OWN source_as_of, rather than
# raising ParseError (which would otherwise fall through to an LLM that
# has no way to know BB simply hasn't published this month's figure yet).
# ---------------------------------------------------------------------------


class TestPlaceholderFallsBackToPriorMonth:
    def test_dash_in_latest_column_falls_back_to_prior_month(self, tmp_path):
        html = """
        <table id="sortableTable">
        <thead><tr><th>Label</th><th>Jul, 2026</th><th>Jun, 2026</th></tr></thead>
        <tbody><tr><td>Point to point</td><td>-</td><td>9.16%</td></tr></tbody>
        </table>
        """
        p = get_parser("html_dated_table_row")
        r = p.parse(_artifact_from_html(tmp_path, html), 'row="Point to point" col=latest')
        assert r.value == pytest.approx(9.16)
        assert r.source_as_of == date(2026, 6, 30)  # prior month's OWN vintage, not July's

    @pytest.mark.parametrize("placeholder", ["--", "---", "", "N/A", "n/a"])
    def test_various_placeholder_spellings_all_fall_back(self, tmp_path, placeholder):
        html = f"""
        <table id="sortableTable">
        <thead><tr><th>Label</th><th>Jul, 2026</th><th>Jun, 2026</th></tr></thead>
        <tbody><tr><td>X</td><td>{placeholder}</td><td>5.0</td></tr></tbody>
        </table>
        """
        p = get_parser("html_dated_table_row")
        r = p.parse(_artifact_from_html(tmp_path, html), 'row="X" col=latest')
        assert r.value == pytest.approx(5.0)
        assert r.source_as_of == date(2026, 6, 30)

    def test_no_prior_month_available_raises(self, tmp_path):
        """A dash with nothing to fall back to must still raise -- there is
        no real reading anywhere on the page for this row."""
        html = """
        <table id="sortableTable">
        <thead><tr><th>Label</th><th>Jul, 2026</th></tr></thead>
        <tbody><tr><td>X</td><td>-</td></tr></tbody>
        </table>
        """
        p = get_parser("html_dated_table_row")
        with pytest.raises(ParseError):
            p.parse(_artifact_from_html(tmp_path, html), 'row="X" col=latest')

    def test_real_value_in_latest_column_does_not_fall_back(self, tmp_path):
        """The common case -- no placeholder involved at all -- must be
        unaffected by the fallback machinery."""
        html = """
        <table id="sortableTable">
        <thead><tr><th>Label</th><th>Jul, 2026</th><th>Jun, 2026</th></tr></thead>
        <tbody><tr><td>X</td><td>8.32</td><td>9.16</td></tr></tbody>
        </table>
        """
        p = get_parser("html_dated_table_row")
        r = p.parse(_artifact_from_html(tmp_path, html), 'row="X" col=latest')
        assert r.value == pytest.approx(8.32)
        assert r.source_as_of == date(2026, 7, 31)

    def test_placeholder_fallback_does_not_apply_to_yoy_pct(self, tmp_path):
        """col=yoy_pct has no "prior column" concept -- a dash there must
        still raise, never silently substitute the pct group's other slot."""
        html = """
        <table id="sortableTable">
        <thead>
        <tr><th>Label</th><th>Jul, 2026</th><th>Jun, 2026</th><th>Jul, 2025</th><th colspan="2">Percentage Changes</th></tr>
        <tr><th>Jul over Jun</th><th>Jul over Jul25</th></tr>
        </thead>
        <tbody><tr><td>X</td><td>10.0</td><td>9.0</td><td>8.0</td><td>1.0</td><td>-</td></tr></tbody>
        </table>
        """
        p = get_parser("html_dated_table_row")
        with pytest.raises(ParseError, match="no number"):
            p.parse(_artifact_from_html(tmp_path, html), 'row="X" col=yoy_pct')
