from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table

import parsers.pdf_table_row  # noqa: F401
from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.registry import get_parser


@pytest.fixture
def pdf_artifact(tmp_path: Path) -> FetchResult:
    pdf_path = tmp_path / "table.pdf"
    doc = SimpleDocTemplate(str(pdf_path), pagesize=letter)
    table = Table([["Tenor", "Outstanding"], ["91-day", "50000"], ["Total", "100000"]])
    doc.build([table])
    return FetchResult(
        indicator_id="x", artifact_path=pdf_path, artifact_type="pdf",
        fetched_at=datetime.now(timezone.utc), source_url="x", sha256="x"*64, cache_hit=False,
    )


def test_extracts_total_row(pdf_artifact):
    p = get_parser("pdf_table_row")
    r = p.parse(pdf_artifact, instruction="page=1 table=1 row=Total col=2")
    assert r.value == 100_000.0


# ---------------------------------------------------------------------------
# Config-conversion batch 1 (2026-08-05): the 4 deficit-financing metrics,
# against the real captured fixture (tests/_pdfs/bb_mei_2026_june.pdf, page
# 16 / printed page 13, "C. Government deficit financing" grid table).
#
# History (same-day Opus review, H1): the FIRST version of this batch used
# the bare token form (`row=26`), reasoning that a stale token merely fails
# closed. Proven false against the real fixture — see
# `test_bare_row_token_collides_with_fy_annual_row_not_hypothetical` below,
# which reproduces the exact live collision. Fixed by switching to the
# quoted `row="<label>"` form (also added to the parser's grammar in this
# same review round — see parsers/pdf_table_row.py's module docstring).
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
MEI_FIXTURE = REPO_ROOT / "tests" / "_pdfs" / "bb_mei_2026_june.pdf"
_HEADER = 'header="1 2 3 4 = 2+3 5 6 = 4+5 7 8 9"'


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
    "col,expected",
    [
        (2, 94158.90),   # bank_borrowing_for_deficit_financing
        (3, -567.67),    # non_bank_borrowing_for_deficit_financing (legitimately negative)
        (4, 93591.23),   # domestic_borrowing_for_budget_deficit
        (5, 21944.28),   # foreign_borrowing_for_budget_deficit
    ],
)
def test_deficit_financing_columns_against_real_fixture(mei_artifact, col, expected):
    p = get_parser("pdf_table_row")
    r = p.parse(mei_artifact, instruction=f'page=16 table=1 {_HEADER} row="July-May of FY 26" col={col}')
    assert r.value == pytest.approx(expected)
    assert r.source_as_of == date(2026, 6, 30)


def test_bare_row_token_collides_with_fy_annual_row_not_hypothetical(mei_artifact):
    """The exact collision H1 proved: BB's deficit-financing table lists
    annual rows ("FY20".."FY25") ABOVE the current fiscal year's cumulative
    "July-May of FY 26" row. A bare digit token collides with the ANNUAL
    row's cell first — proven here one year back (row=25 col=3, the
    non_bank column) against the real fixture: it returns the FY25 ANNUAL
    figure (44137.95), not "July-May of FY 25"'s value. Nothing raises; a
    plausible wrong number comes back silently. row=26 (this PR's original
    choice) would break identically the moment BB adds an "FY26" row —
    i.e. the next edition after FY26 closes, not "~11 months away" as first
    assumed. This is why the shipped config uses the quoted form instead."""
    p = get_parser("pdf_table_row")
    r = p.parse(mei_artifact, instruction="page=16 table=1 row=25 col=3")
    assert r.value == pytest.approx(44137.95)  # FY25 ANNUAL figure — the wrong row


def test_quoted_row_anchor_immune_to_the_same_collision(mei_artifact):
    """The fix: the quoted form matches the FULL label, which is not a
    substring of the annual row's bare "FY25" cell in either direction."""
    p = get_parser("pdf_table_row")
    r = p.parse(mei_artifact, instruction='page=16 table=1 row="July-May of FY 25" col=3')
    assert r.value == pytest.approx(39510.11)  # the real July-May-of-FY25 non-bank figure


def test_quoted_row_anchor_fails_closed_when_absent(mei_artifact):
    """Fails CLOSED, not silently-wrong: a quoted anchor that no longer
    matches ANY row (simulating the FY26->FY27 rollover, where the config
    still says "FY 26" but the report has moved on to "FY 27") raises
    ParseError rather than reading a neighbouring row."""
    p = get_parser("pdf_table_row")
    with pytest.raises(ParseError):
        p.parse(mei_artifact, instruction='page=16 table=1 row="July-May of FY 27" col=2')


def test_header_guard_passes_on_real_fixtures_unchanged_column_layout(mei_artifact):
    p = get_parser("pdf_table_row")
    r = p.parse(mei_artifact, instruction=f'page=16 table=1 {_HEADER} row="July-May of FY 26" col=2')
    assert r.value == pytest.approx(94158.90)


def test_header_guard_raises_when_column_layout_would_have_shifted(mei_artifact):
    """M2: BB inserting a new column shifts every downstream col= silently
    (the row is still found, a number is still returned) unless something
    checks the layout FIRST. Simulates that shift with a header= anchor that
    doesn't match the real page's numbering line."""
    p = get_parser("pdf_table_row")
    with pytest.raises(ParseError, match="header line"):
        p.parse(
            mei_artifact,
            instruction='page=16 table=1 header="1 2 3 3a 4 = 2+3+3a" row="July-May of FY 26" col=2',
        )


def test_header_guard_is_optional_existing_entries_unaffected(mei_artifact):
    """No header= present -> the guard is simply skipped, matching every
    pre-existing pdf_table_row config entry that doesn't use it."""
    p = get_parser("pdf_table_row")
    r = p.parse(mei_artifact, instruction='page=16 table=1 row="July-May of FY 26" col=2')
    assert r.value == pytest.approx(94158.90)


# ---------------------------------------------------------------------------
# Synthetic-table version of the same collision + fix, independent of the
# real fixture's specific numbers (per H1: "add a test that injects an FY26
# annual row above it and asserts the July-May row is still selected").
# ---------------------------------------------------------------------------

@pytest.fixture
def fy_rollover_pdf_artifact(tmp_path: Path) -> FetchResult:
    """A minimal synthetic table shaped exactly like the real deficit-
    financing table's danger zone: an annual "FY26" row (as BB will add once
    FY26 closes) sitting ABOVE the "July-May of FY 26" cumulative row, with
    deliberately distinct decoy values so a wrong-row read is unambiguous."""
    pdf_path = tmp_path / "fy_rollover.pdf"
    doc = SimpleDocTemplate(str(pdf_path), pagesize=letter)
    table = Table([
        ["FY", "Bank", "Non-bank"],
        ["FY25", "11111", "22222"],
        ["FY26", "99999", "88888"],       # the future annual row this test injects
        ["July-May of FY 26", "94158.90", "-567.67"],  # the row we actually want
    ])
    doc.build([table])
    return FetchResult(
        indicator_id="x", artifact_path=pdf_path, artifact_type="pdf",
        fetched_at=datetime.now(timezone.utc), source_url="x", sha256="x" * 64, cache_hit=False,
    )


def test_bare_token_would_pick_the_injected_fy26_annual_row(fy_rollover_pdf_artifact):
    """Proves the failure mode generalizes beyond the one real-fixture
    instance above: once an "FY26" annual row exists (any edition from the
    one right after FY26 closes onward), row=26 (bare) matches IT first."""
    p = get_parser("pdf_table_row")
    r = p.parse(fy_rollover_pdf_artifact, instruction="page=1 table=1 row=26 col=2")
    assert r.value == 99999.0  # the decoy FY26 annual row, NOT the July-May row


def test_quoted_row_still_selects_july_may_row_with_fy26_annual_row_present(fy_rollover_pdf_artifact):
    """The actual H1 ask: with an FY26 annual row now present ABOVE the
    cumulative row, the quoted anchor must still select the July-May row."""
    p = get_parser("pdf_table_row")
    r_bank = p.parse(fy_rollover_pdf_artifact, instruction='page=1 table=1 row="July-May of FY 26" col=2')
    r_non_bank = p.parse(fy_rollover_pdf_artifact, instruction='page=1 table=1 row="July-May of FY 26" col=3')
    assert r_bank.value == pytest.approx(94158.90)
    assert r_non_bank.value == pytest.approx(-567.67)


def test_quoted_row_fails_closed_when_label_has_moved_on(fy_rollover_pdf_artifact):
    """The other half of H1's ask: once the fiscal year truly rolls over and
    "July-May of FY 26" no longer exists at all, the quoted anchor raises
    rather than falling back to a neighbouring row."""
    p = get_parser("pdf_table_row")
    with pytest.raises(ParseError):
        p.parse(fy_rollover_pdf_artifact, instruction='page=1 table=1 row="July-May of FY 27" col=2')
