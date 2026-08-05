"""Unit tests for parsers.pdf_table_latest.

Most tests exercise the pure text-extraction function so they run without
pdfplumber installed. The source_as_of / page= scoping section near the end
(added in the same-day Opus review, H2/H4/M4) needs real PDFs and therefore
pdfplumber — same as the broader parse_all suite.
"""
from __future__ import annotations

import importlib
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate

from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.registry import get_parser

REPO_ROOT = Path(__file__).resolve().parents[1]
MEI_FIXTURE = REPO_ROOT / "tests" / "_pdfs" / "bb_mei_2026_june.pdf"


@pytest.fixture(scope="module")
def mod():
    """Import the parser module. pdfplumber is lazy-imported inside
    ``PdfTableLatestParser.parse``, so module-import here is dependency-free."""
    return importlib.import_module("parsers.pdf_table_latest")


# Real WSEI Item 11 text as extracted by pdfplumber.
WSEI_ITEM_11 = """
Percentage change
February, 2025 June, 2025 February, 2026
Feb.'26 over Feb.'25 Feb.'26 over June'25 Feb.'25 over June'24 June'25 over June'24
11.
a) Reserve Money (RM) (BDT in crore) 374602.90 413179.00 424618.80 13.35 2.77 -9.44 -0.11
b) Broad Money (M2) (BDT in crore) 2064660.20 2174621.80 2281865.40 10.52 4.93 1.55 6.95
Total Domestic Credit (BDT in crore) 2168760.80 2284353.00 2413769.10 11.30 5.67 2.52 7.98
""".strip()


def test_broad_money_latest_value(mod):
    v = mod._find_latest_in_text(WSEI_ITEM_11, "b) Broad Money", min_value=1000.0)
    assert v == 2281865.40


def test_reserve_money_latest_value(mod):
    v = mod._find_latest_in_text(WSEI_ITEM_11, "a) Reserve Money", min_value=1000.0)
    assert v == 424618.80


def test_total_domestic_credit_latest(mod):
    v = mod._find_latest_in_text(WSEI_ITEM_11, "Total Domestic Credit", min_value=1000.0)
    assert v == 2413769.10


def test_min_filter_excludes_pct_columns(mod):
    """Without min, the last number on the row would be the trailing pct value."""
    v_no_min = mod._find_latest_in_text(WSEI_ITEM_11, "a) Reserve Money", min_value=0.0)
    assert v_no_min == -0.11  # last pct column
    v_with_min = mod._find_latest_in_text(WSEI_ITEM_11, "a) Reserve Money", min_value=1000.0)
    assert v_with_min == 424618.80


def test_label_case_insensitive(mod):
    v = mod._find_latest_in_text(WSEI_ITEM_11, "BROAD MONEY", min_value=1000.0)
    assert v == 2281865.40


def test_missing_label_returns_none(mod):
    assert mod._find_latest_in_text(WSEI_ITEM_11, "Nonexistent Row", 0.0) is None


def test_no_numbers_above_min_returns_none(mod):
    text = "label_only_with_small_numbers 1.2 3.4"
    assert mod._find_latest_in_text(text, "label", min_value=1000.0) is None


def test_handles_thousands_separators(mod):
    text = "Series 1,234,567.89 0.5"
    assert mod._find_latest_in_text(text, "Series", min_value=1000.0) == 1234567.89


def test_handles_negative_absolute_values(mod):
    """Some indicators (e.g., current account balance) can go negative — must
    be kept when |value| >= min."""
    text = "Current Account Balance -1471.55 -1000.21 -139.00"
    assert mod._find_latest_in_text(text, "Current Account", min_value=100.0) == -139.00


def test_parse_instruction_row_only(mod):
    label, mn, page = mod._parse_instruction('row="a) Reserve Money"')
    assert label == "a) Reserve Money"
    assert mn == 0.0
    assert page is None


def test_parse_instruction_with_min(mod):
    label, mn, page = mod._parse_instruction('row="b) Broad Money" min=1000')
    assert label == "b) Broad Money"
    assert mn == 1000.0
    assert page is None


def test_parse_instruction_missing_row_raises(mod):
    from parsers.base import ParseError
    with pytest.raises(ParseError):
        mod._parse_instruction("min=100")


def test_min_with_decimal(mod):
    label, mn, page = mod._parse_instruction('row="x" min=0.5')
    assert mn == 0.5


def test_parse_instruction_with_page(mod):
    """M4: optional page= scoping, mirroring pdf_table_row's convention."""
    label, mn, page = mod._parse_instruction('row="Money multiplier" page=7')
    assert label == "Money multiplier"
    assert mn == 0.0
    assert page == 7


def test_parse_instruction_page_defaults_to_none(mod):
    """Backward compatible: existing entries without page= are unaffected."""
    _, _, page = mod._parse_instruction('row="b) Broad Money" min=1000')
    assert page is None


def test_parser_registered():
    """Confirm the @register decorator wired the parser into the registry —
    calls get_parser (the real production lookup path), not just checking
    dict membership."""
    import parsers.pdf_table_latest  # noqa: F401  triggers registration
    from parsers.registry import get_parser
    parser = get_parser("pdf_table_latest")
    assert isinstance(parser, parsers.pdf_table_latest.PdfTableLatestParser)


# ---------------------------------------------------------------------------
# Config-conversion batch 1 (2026-08-05): real text as extracted by pdfplumber
# from tests/_pdfs/bb_mei_2026_june.pdf, pages 3 and 4 ("Money and credit
# developments" / "Reserve money developments"). Pins the exact `min=` value
# each new config entry needs to skip the trailing y-o-y FLOW columns (which,
# unlike WSEI Item 11's small pct-change columns above, are themselves
# BDT-crore-scale numbers — a plain min=1000 would NOT separate them from the
# level columns here; see the PR body for the full reasoning).
# ---------------------------------------------------------------------------

# Page 3, "1. Money and credit developments" — currency_outside_bank / deposits_of_the_system.
MEI_PAGE3_MONEY_CREDIT = """
Particulars June, 2024R May, 2025R June, 2025R May, 2026P FY25R FY26P
1 2 3 4 5 6=3-2 7=5-4
A. Currency outside 290436.50 293778.60 296451.90 349374.00 3342.10 52922.10
banks (-0.51) (+8.54) (+2.07) (+18.92) (+3424.63) (+103.45)
B. Deposits of the 1742797.50 1832572.00 1878169.80 2041692.70 89774.50 163522.90
banking system (+9.25) (+7.73) (+7.77) (+11.41) (-55.05) (-55.19)
""".strip()

# Page 4, "2. Reserve money developments" — deposits_held_with_bb_crr / money_multiplier.
MEI_PAGE4_RESERVE_MONEY = """
B. Deposits held with BB* 93338.10 78899.40 86482.40 115326.70 -14438.70 28844.30
(+30.29) (+18.35) (-7.35) (+46.17) (+56.70) (-365.63)
Money multiplier 4.92 5.33 5.26 4.92 N/A N/A
""".strip()


def test_currency_outside_bank_latest_value_skips_flow_columns(mod):
    """Without a large enough min=, the naive "last number" would be the
    FY26P flow column (52922.10), not the May-2026 level (349374.00).
    min=250000 (H3 review recalibration — was 200000, too close to the real
    flow magnitude for comfortable headroom) still cleanly separates both."""
    v_no_min = mod._find_latest_in_text(MEI_PAGE3_MONEY_CREDIT, "Currency outside", min_value=0.0)
    assert v_no_min == 52922.10
    v_with_min = mod._find_latest_in_text(MEI_PAGE3_MONEY_CREDIT, "Currency outside", min_value=250000.0)
    assert v_with_min == 349374.00


def test_deposits_of_the_system_latest_value_skips_flow_columns(mod):
    """min=1000000 (H3 recalibration — was 200000)."""
    v = mod._find_latest_in_text(MEI_PAGE3_MONEY_CREDIT, "Deposits of the", min_value=1000000.0)
    assert v == 2041692.70


def test_deposits_held_with_bb_crr_latest_value_skips_negative_flow_column(mod):
    """The trailing flow figures include a NEGATIVE one (-14438.70); min=
    filters by abs(value), so it must still be excluded, not accidentally
    kept because "negative < min" reads as false only for the raw value.
    min=60000 (H3 recalibration — was 50000)."""
    v = mod._find_latest_in_text(MEI_PAGE4_RESERVE_MONEY, "Deposits held with BB", min_value=60000.0)
    assert v == 115326.70


def test_money_multiplier_latest_value_needs_no_min(mod):
    """Money multiplier has no trailing flow columns (ratios aren't
    flow-summed) — the default min=0 already returns the right value."""
    v = mod._find_latest_in_text(MEI_PAGE4_RESERVE_MONEY, "Money multiplier", min_value=0.0)
    assert v == 4.92


# ---------------------------------------------------------------------------
# Same-day Opus review (H2, H4, M4): source_as_of recovery + page= scoping,
# exercised through the real registered parser (needs pdfplumber — the file
# docstring's "no pdfplumber needed" note above covers the _find_latest_in_text
# unit tests only, not this integration section).
# ---------------------------------------------------------------------------

@pytest.fixture
def mei_artifact() -> FetchResult:
    return FetchResult(
        indicator_id="x", artifact_path=MEI_FIXTURE, artifact_type="pdf",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.bb.org.bd//pub/monthly/selectedecooind/2026_june.pdf",
        sha256="30f593863230aaa744d61652f8c8a11f198a06541bfcbf5b4fb7a81a82354b8f",
        cache_hit=False,
    )


@pytest.mark.parametrize(
    "task,expected_value",
    [
        ('row="Money multiplier" page=7', 4.92),
        ('row="Currency outside" min=250000 page=6', 349374.0),
        ('row="Deposits of the" min=1000000 page=6', 2041692.7),
        ('row="Deposits held with BB" min=60000 page=7', 115326.7),
    ],
)
def test_source_as_of_recovered_on_success_for_all_4_conversions(mei_artifact, task, expected_value):
    """H2: before this fix, parse() never set source_as_of at all — since
    the deterministic parse now SUCCEEDS, the old LLM-fallback-only recovery
    in hybrid.py never ran, so these rows would have been silently stamped
    with today's run date (plus tripped aggregate_latest.py's undated-metric
    warning every run, since pdf_table_latest wasn't in
    _NEVER_DATED_PARSE_STRATEGIES). Now recovered directly in parse()."""
    p = get_parser("pdf_table_latest")
    r = p.parse(mei_artifact, task)
    assert r.value == pytest.approx(expected_value)
    assert r.source_as_of == date(2026, 6, 30)


def test_recover_source_as_of_llm_fallback_path(mei_artifact):
    """The other half of the contract (mirrors pdf_component/pdf_table_row):
    recover_source_as_of must also work standalone, for the case where
    value extraction fails and the LLM path supplies the value instead."""
    p = get_parser("pdf_table_latest")
    assert p.recover_source_as_of(mei_artifact) == date(2026, 6, 30)


def test_page_scoping_prevents_a_prose_line_on_an_earlier_page_from_winning(tmp_path: Path):
    """M4: proves page= scoping's value with a cross-page collision (the
    live same-page collision on the real MEI fixture — 'Money multiplier'
    appears both in the page-4 data row AND a later page-4 prose bullet —
    happens to still resolve correctly today purely because the data row
    comes first in document order; page= doesn't change that same-page
    case, see the module docstring. This test instead proves the case
    page= DOES fully close: a decoy mention on an EARLIER, unrelated page)."""
    pdf_path = tmp_path / "cross_page.pdf"
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(pdf_path), pagesize=letter)
    doc.build([
        Paragraph("Money multiplier was a topic of discussion, roughly 1.23 in some other context.", styles["Normal"]),
        PageBreak(),
        Paragraph("Money multiplier 4.92 5.33 5.26 4.92 N/A N/A", styles["Normal"]),
    ])
    artifact = FetchResult(
        indicator_id="x", artifact_path=pdf_path, artifact_type="pdf",
        fetched_at=datetime.now(timezone.utc), source_url="x", sha256="x" * 64, cache_hit=False,
    )
    p = get_parser("pdf_table_latest")

    # Without page= scoping, the whole-document search finds the decoy on
    # page 1 FIRST (document order) and returns its number, wrong.
    r_unscoped = p.parse(artifact, 'row="Money multiplier"')
    assert r_unscoped.value == 1.23

    # With page=2, the search is scoped to just the real data row's page.
    r_scoped = p.parse(artifact, 'row="Money multiplier" page=2')
    assert r_scoped.value == 4.92


def test_page_out_of_range_raises(mei_artifact):
    p = get_parser("pdf_table_latest")
    with pytest.raises(ParseError, match="out of range"):
        p.parse(mei_artifact, 'row="Money multiplier" page=9999')
