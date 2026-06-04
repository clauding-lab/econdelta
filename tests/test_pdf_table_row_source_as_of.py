"""source_as_of recovery for the generic pdf_table_row parser.

pdf_table_row is shared by 25 indicators across two sources that each carry the
report's as-of date INSIDE the PDF, but in different idioms:

  - Bangladesh Bank "Major Economic Indicators: Monthly Update" — every page
    header reads "Monthly Update (April 2026)"; the cover adds
    "Volume 04/2026 April 2026". The figure is as-of the END of that month.
  - MoF "Quarterly Debt Bulletin" — the cover carries NO date; the body states
    "As of 31 December 2025" and "... up to Dec FY26" beside the debt-stock
    table. Maps to that quarter-end.

The report is identified by a stable CONTENT marker ("Major Economic Indicators"
/ "Debt Bulletin"), NOT by URL host: latest_pdf_link discovery rewrites the
source_url to the resolved PDF link, which for MoF is a third-party object-store
host (not mof.gov.bd). Content detection is host-independent and future-proof.
Unrecognised report or no date → None (the slow-cadence guard in aggregate_latest
is the safety net — never fabricate a wrong date).

Idiom strings below are copied verbatim from the live April-2026 BB PDF and the
live MoF Debt Bulletin Issue 17 (confirmed 2026-06-04).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table

import parsers.pdf_table_row as ptr  # noqa: F401 — registers parser + exposes helper
from fetchers.base import FetchResult
from parsers.registry import get_parser

# Resolved PDF links as the fetcher actually records them (note: the MoF link is
# a third-party object store, NOT mof.gov.bd — the reason gating is content-based).
BB_URL = "https://www.bb.org.bd//pub/monthly/selectedecooind/2026_april.pdf"
MOF_URL = "https://objectstorage.ap-dcc-gazipur-1.oraclecloud15.com/office-mof/x.pdf"

_BB_MARK = "Major Economic Indicators: Monthly Update"
_MOF_MARK = "Quarterly Debt Bulletin"


# ---------------------------------------------------------------------------
# Unit: the content-gated date recovery helper (raw strings, no PDF)
# ---------------------------------------------------------------------------

class TestRecoverReportDate:
    def test_bb_monthly_update_header(self):
        text = f"{_BB_MARK} (April 2026)\n10. Agricultural credit"
        assert ptr._recover_report_date(text) == date(2026, 4, 30)

    def test_bb_cover_volume_line(self):
        text = "Volume 04/2026 April 2026\nMajor Economic Indicators: Monthly Update"
        assert ptr._recover_report_date(text) == date(2026, 4, 30)

    def test_bb_february_is_month_end_leap_safe(self):
        text = f"{_BB_MARK} (February 2024)"
        assert ptr._recover_report_date(text) == date(2024, 2, 29)

    def test_mof_as_of_explicit_day(self):
        text = f"{_MOF_MARK}\nAs of 31 December 2025, total outstanding government guarantees were BDT"
        assert ptr._recover_report_date(text) == date(2025, 12, 31)

    def test_mof_up_to_fy_fallback_when_no_as_of(self):
        text = f"{_MOF_MARK}\nTotal Debt Stock: Domestic vs. External up to Dec FY26"
        assert ptr._recover_report_date(text) == date(2025, 12, 31)

    def test_mof_up_to_fy_second_half_maps_to_fy_year(self):
        # March falls in the SECOND half of the BD fiscal year → calendar 2026.
        assert ptr._recover_report_date(f"{_MOF_MARK} up to Mar FY26") == date(2026, 3, 31)

    def test_mof_as_of_preferred_over_up_to(self):
        text = f"{_MOF_MARK} up to Sep FY26 ... As of 31 December 2025 ..."
        assert ptr._recover_report_date(text) == date(2025, 12, 31)

    def test_bb_report_ignores_stray_as_of_sentence(self):
        # A BB report whose prose contains an "As of <date>" sentence but no
        # "(Month Year)" header must NOT be dated by the MoF idiom.
        text = f"{_BB_MARK}\nAs of 31 December 2025 something unrelated"
        assert ptr._recover_report_date(text) is None

    def test_mof_report_ignores_bb_idiom(self):
        text = f"{_MOF_MARK}\nMonthly Update (April 2026)"
        assert ptr._recover_report_date(text) is None

    def test_unrecognised_report_returns_none(self):
        # The idiom alone, with no report marker, is not trusted.
        assert ptr._recover_report_date("Monthly Update (April 2026)") is None

    def test_no_recognised_date_returns_none(self):
        assert ptr._recover_report_date("Quarterly Debt Bulletin Issue 17") is None

    # --- stale-date defence: latest match wins, never an earlier comparison date ---

    def test_mof_historical_as_of_before_current_picks_latest(self):
        # A prior-period comparison date appears BEFORE the current one. Picking the
        # first match would return the stale 2024-06-30 — the NPL-class failure.
        text = (f"{_MOF_MARK}\nCompared to the position as of 30 June 2024, "
                "the stock as of 31 December 2025 rose")
        assert ptr._recover_report_date(text) == date(2025, 12, 31)

    def test_mof_historical_up_to_fy_before_current_picks_latest(self):
        text = f"{_MOF_MARK}\nSeries up to Jun FY25 ... current position up to Dec FY26"
        assert ptr._recover_report_date(text) == date(2025, 12, 31)

    def test_mof_malformed_as_of_day_returns_none_not_fy_fallthrough(self):
        # The 'As of' idiom is present but the day is impossible (OCR garble / typo).
        # The report's own date is corrupt → None, NOT the unrelated 'up to FY' month.
        text = f"{_MOF_MARK}\nAs of 31 February 2025 ... debt stock up to Dec FY25"
        assert ptr._recover_report_date(text) is None

    def test_bb_prior_edition_before_current_picks_latest(self):
        text = (f"{_BB_MARK}\nRevised since Monthly Update (December 2025). "
                "Monthly Update (April 2026)")
        assert ptr._recover_report_date(text) == date(2026, 4, 30)

    # --- marker gating: dual marker priority + stray-mention precision ---

    def test_dual_marker_prefers_bb(self):
        # If both report titles somehow appear, BB is checked first — pin the order.
        text = f"{_BB_MARK} (April 2026)\nQuarterly Debt Bulletin"
        assert ptr._recover_report_date(text) == date(2026, 4, 30)

    def test_stray_debt_bulletin_mention_is_not_gated(self):
        # A sibling MoF fiscal report that merely references "the debt bulletin"
        # (not titled "Quarterly Debt Bulletin") must NOT be dated by the debt idiom.
        text = "MoF Fiscal Report. See also the debt bulletin. As of 30 September 2025 revenue rose"
        assert ptr._recover_report_date(text) is None


# ---------------------------------------------------------------------------
# Integration: through parse() and recover_source_as_of() with real-idiom PDFs
# ---------------------------------------------------------------------------

def _build_pdf(path: Path, header_lines: list[str], table_rows: list[list[str]]) -> None:
    styles = getSampleStyleSheet()
    story = [Paragraph(line, styles["Normal"]) for line in header_lines]
    story.append(Spacer(1, 12))
    story.append(Table(table_rows))
    SimpleDocTemplate(str(path), pagesize=letter).build(story)


def _artifact(path: Path, url: str) -> FetchResult:
    return FetchResult(
        indicator_id="x", artifact_path=path, artifact_type="pdf",
        fetched_at=datetime.now(timezone.utc), source_url=url,
        sha256="a" * 64, cache_hit=False,
    )


def test_parse_bb_sets_source_as_of_and_value(tmp_path: Path):
    pdf = tmp_path / "bb.pdf"
    _build_pdf(
        pdf,
        [f"{_BB_MARK} (April 2026)"],
        [["Category", "Value"], ["Woven garments", "1234"], ["Total", "5678"]],
    )
    p = get_parser("pdf_table_row")
    r = p.parse(_artifact(pdf, BB_URL), instruction="page=1 table=1 row=Total col=2")
    assert r.value == 5678.0
    assert r.source_as_of == date(2026, 4, 30)


def test_parse_mof_sets_quarter_end(tmp_path: Path):
    pdf = tmp_path / "mof.pdf"
    _build_pdf(
        pdf,
        [f"{_MOF_MARK} Issue 17",
         "As of 31 December 2025, total outstanding government guarantees were BDT"],
        [["Source", "Stock"], ["Domestic Debt", "1247151"], ["External Debt", "959311"]],
    )
    p = get_parser("pdf_table_row")
    r = p.parse(_artifact(pdf, MOF_URL), instruction="page=1 table=1 row=Domestic Debt col=2")
    assert r.value == 1247151.0
    assert r.source_as_of == date(2025, 12, 31)


def test_parse_value_unaffected_when_no_date(tmp_path: Path):
    # Regression: a PDF with no recognised report/date still parses the value.
    pdf = tmp_path / "plain.pdf"
    _build_pdf(pdf, ["Some report"], [["Tenor", "Outstanding"], ["91-day", "5"], ["Total", "100000"]])
    p = get_parser("pdf_table_row")
    r = p.parse(_artifact(pdf, MOF_URL), instruction="page=1 table=1 row=Total col=2")
    assert r.value == 100000.0
    assert r.source_as_of is None


def test_recover_source_as_of_llm_path_bb(tmp_path: Path):
    # When deterministic value extraction fails, the LLM fallback still recovers
    # the date via recover_source_as_of (mirrors pdf_component).
    pdf = tmp_path / "bb2.pdf"
    _build_pdf(pdf, [f"{_BB_MARK} (March 2026)"], [["Category", "Value"], ["Total", "10"]])
    p = get_parser("pdf_table_row")
    assert p.recover_source_as_of(_artifact(pdf, BB_URL)) == date(2026, 3, 31)


def test_recover_source_as_of_llm_path_mof(tmp_path: Path):
    pdf = tmp_path / "mof2.pdf"
    _build_pdf(pdf, [f"{_MOF_MARK}", "Debt Stock up to Dec FY26"],
               [["Source", "Stock"], ["External Debt", "959311"]])
    p = get_parser("pdf_table_row")
    assert p.recover_source_as_of(_artifact(pdf, MOF_URL)) == date(2025, 12, 31)


def test_recover_source_as_of_never_raises_on_bad_pdf(tmp_path: Path):
    bad = tmp_path / "not.pdf"
    bad.write_text("not a real pdf", encoding="utf-8")
    p = get_parser("pdf_table_row")
    assert p.recover_source_as_of(_artifact(bad, MOF_URL)) is None
