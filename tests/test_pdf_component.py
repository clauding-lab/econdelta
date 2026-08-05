from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

import parsers.pdf_component as pc
from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.registry import get_parser

REPO_ROOT = Path(__file__).resolve().parents[1]
MEI_FIXTURE = REPO_ROOT / "tests" / "_pdfs" / "bb_mei_2026_june.pdf"
FSAR_FIXTURE_TEXT = REPO_ROOT / "tests" / "fixtures" / "fsr_fixture_text.txt"


@pytest.fixture
def pdf_artifact(tmp_path: Path) -> FetchResult:
    pdf_path = tmp_path / "test.pdf"
    c = canvas.Canvas(str(pdf_path))
    c.drawString(100, 800, "Component 11a Broad Money: 1900000")
    c.drawString(100, 780, "Component 12c Private Sector Credit: 1500000")
    c.showPage()
    c.save()
    return FetchResult(
        indicator_id="broad_money", artifact_path=pdf_path, artifact_type="pdf",
        fetched_at=datetime.now(timezone.utc), source_url="x", sha256="x"*64, cache_hit=False,
    )


def test_extracts_component_value(pdf_artifact):
    p = get_parser("pdf_component")
    r = p.parse(pdf_artifact, instruction="Component 11a")
    assert r.value == 1_900_000.0


def test_raises_when_component_missing(pdf_artifact):
    p = get_parser("pdf_component")
    with pytest.raises(ParseError):
        p.parse(pdf_artifact, instruction="Component 99z")


# ---------------------------------------------------------------------------
# source_as_of: BB "Major Economic Indicators: Monthly Update" idiom
#
# Batch-1 conversion (2026-08-05) activated pdf_component's deterministic
# success path for the FIRST time in production (point_to_point_inflation) —
# every prior pdf_component-declared config entry had a prose `fetch.task`
# that never matched literally, so this code path had never actually run.
# Doing so surfaced two real bugs, both found against REAL captured document
# text (never synthetic guesses) — see the module docstring for the full
# account:
#
#   1. The generic "as of end-Month YYYY" fallback can false-positive-match
#      an UNRELATED table header elsewhere in the doc (the MEI PDF's own
#      page-5 liquidity table).
#   2. A loose MEI marker (the bare "major economic indicators" substring,
#      mirroring pdf_table_row.py's gate) ALSO false-positive-matches real
#      FSAR text — tests/fixtures/fsr_fixture_text.txt cites "Major Economic
#      Indicators" as a data SOURCE, not as its own title. Fixed by gating on
#      the fuller title phrase instead (_MEI_TITLE_MARK).
# ---------------------------------------------------------------------------

class TestMeiReportDate:
    def test_monthly_update_header(self):
        text = "Monthly Update (June 2026)\n7. Price and wage index"
        assert pc._mei_report_date(text) == date(2026, 6, 30)

    def test_volume_cover_line(self):
        text = "Volume 06/2026 June 2026\nMajor Economic Indicators: Monthly Update"
        assert pc._mei_report_date(text) == date(2026, 6, 30)

    def test_february_is_month_end_leap_safe(self):
        text = "Monthly Update (February 2024)"
        assert pc._mei_report_date(text) == date(2024, 2, 29)

    def test_no_mei_idiom_returns_none(self):
        assert pc._mei_report_date("Quarterly Debt Bulletin Issue 17") is None

    def test_latest_repeated_header_wins_not_first(self):
        # The MEI header repeats on every page — must not regress if editions
        # ever mix (mirrors pdf_table_row's "latest match wins" guard).
        text = "Monthly Update (May 2026)\n...\nMonthly Update (June 2026)"
        assert pc._mei_report_date(text) == date(2026, 6, 30)


class TestExtractQuarterEndGate:
    """`_extract_quarter_end` gates on the REPORT FAMILY (a content marker),
    not just idiom priority — see module docstring bugs 1 and 2 above for why
    "try MEI first, fall through on failure" is not equivalent to a real gate.
    """

    def test_mei_gate_wins_over_coexisting_end_month_text(self):
        """Regression for bug 1: an MEI-titled document (using the REAL full
        title, as every real MEI page carries — not just the bare 'Monthly
        Update (...)' fragment) containing an UNRELATED 'As of end <Month>
        <Year>' table header (BB's own page-5 liquidity table) must be dated
        by the MEI cover, not by that unrelated header."""
        text = (
            "Major Economic Indicators: Monthly Update (June 2026)\n"
            "3. Liquidity situation of the scheduled banks\n"
            "As of end As of end May 2026P\nJune 2025\n"
        )
        assert pc._extract_quarter_end(text) == date(2026, 6, 30)

    def test_bare_monthly_update_without_full_title_does_not_gate(self):
        """Precision check: the gate requires the FULL title phrase, not just
        'Monthly Update (...)' in isolation — a fragment that could plausibly
        appear elsewhere. Without the full title, the doc is NOT recognised
        as MEI and falls through to the FSAR/QFSAR idioms (here, the generic
        end-month fallback correctly fires on the only date-shaped text
        present — demonstrating this is a real gate, not just a try-order)."""
        text = "Monthly Update (June 2026)\nas of end-May 2026 for one series only"
        assert pc._extract_quarter_end(text) == date(2026, 5, 31)

    def test_bare_major_economic_indicators_phrase_alone_does_not_gate(self):
        """Pins the exact distinction from pdf_table_row.py's looser marker:
        the bare phrase (a plausible citation/reference, not a title) must
        NOT trigger the MEI branch on its own."""
        text = "Source: Major Economic Indicators, January 2026 issue, BB."
        assert pc._MEI_TITLE_MARK not in text.lower()

    def test_gate_is_noop_on_real_fsar_fixture(self):
        """The critical regression: a REAL captured FSAR document (which DOES
        contain the bare 'major economic indicators' substring as a source
        citation) must still recover its own QFSAR date, not be misrouted
        into the MEI branch and lose it."""
        fsar_text = FSAR_FIXTURE_TEXT.read_text()
        assert "major economic indicators" in fsar_text.lower()  # the citation is really there
        assert pc._MEI_TITLE_MARK not in fsar_text.lower()  # but not the full title
        assert pc._extract_quarter_end(fsar_text) == date(2025, 12, 31)

    def test_qfsar_idiom_still_works_when_no_mei_marker(self):
        text = "Quarter ending 30 September 2025"
        assert pc._extract_quarter_end(text) == date(2025, 9, 30)

    def test_qfsar_end_month_fallback_still_works_when_no_mei_marker(self):
        text = "data and information available as of end-September 2025"
        assert pc._extract_quarter_end(text) == date(2025, 9, 30)

    def test_no_idiom_returns_none(self):
        assert pc._extract_quarter_end("nothing relevant here") is None


def test_parse_point_to_point_inflation_against_real_mei_fixture():
    """Integration: the real batch-1 conversion, against the real captured
    fixture. Pins both the value AND the source_as_of fix together."""
    artifact = FetchResult(
        indicator_id="point_to_point_inflation", artifact_path=MEI_FIXTURE,
        artifact_type="pdf", fetched_at=datetime.now(timezone.utc),
        source_url="https://www.bb.org.bd//pub/monthly/selectedecooind/2026_june.pdf",
        sha256="30f593863230aaa744d61652f8c8a11f198a06541bfcbf5b4fb7a81a82354b8f",
        cache_hit=False,
    )
    p = get_parser("pdf_component")
    r = p.parse(artifact, "Headline point-to-point inflation")
    assert r.value == 9.16
    assert r.source_as_of == date(2026, 6, 30)  # NOT 2026-05-31 (the false positive)
