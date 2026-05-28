"""Unit + integration tests for parsers.pdf_table_column_latest.

The pure helpers (instruction parsing, table walking, header normalization)
are exercised on synthetic fixtures so they run without a real PDF.

The integration tests exercise the full @register-wired parser against the
April 2026 BB Monthly Economic Indicators bulletin (probe download at
/tmp/econdelta_probe/_pdfs/probe_mei/2026-04/2026_april.pdf). They are
skipped when the fixture isn't present locally so the unit suite still
runs in CI / on a fresh checkout.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult

_PROBE_PDF = Path("/tmp/econdelta_probe/_pdfs/probe_mei/2026-04/2026_april.pdf")


@pytest.fixture(scope="module")
def mod():
    """Import the parser module. pdfplumber is lazy-imported inside
    ``PdfTableColumnLatestParser.parse``, so module-import here is
    dependency-free."""
    return importlib.import_module("parsers.pdf_table_column_latest")


# Reconstructed shape of the BB MEI bulletin page 10 table 0, as extracted
# by pdfplumber. Row 0 is a units banner, row 1 is the column header (with
# embedded newlines from multi-line headers), row 2 is sub-headers, row 3
# is a fiscal-year group label, rows 4-15 are monthly data, rows 16-17 are
# the Source: / Note: footer.
_PAGE10_TABLE: list[list[str | None]] = [
    [
        "(Percent)",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    ],
    [
        "",
        "Treasury bills",
        None,
        None,
        None,
        "BGTB",
        None,
        None,
        None,
        None,
        "FRTB",
        "BB Bills",
        "Policy rate\n(repo)",
        "SLF rate",
        "SDF rate",
        "Call\nmoney\nrate",
    ],
    [
        None,
        "14-Day",
        "91-Day",
        "182-Day",
        "364-Day",
        "2-Year",
        "5-Year",
        "10-Year",
        "15-Year",
        "20-Year",
        "3-Year",
        "90-Day",
        None,
        None,
        None,
        None,
    ],
    [
        "FY25",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        "",
    ],
    [
        "June",
        "--",
        "11.94",
        "11.98",
        "12.01",
        "12.20",
        "12.34",
        "12.28",
        "12.56",
        "12.44",
        "13.06",
        "12.10",
        "10.00",
        "11.50",
        "8.50",
        "10.14",
    ],
    [
        "FY26",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        "",
    ],
    [
        "July",
        "11.09",
        "11.03",
        "11.16",
        "10.95",
        "11.57",
        "11.00",
        "10.41",
        "10.44",
        "10.46",
        "12.81",
        "---",
        "10.00",
        "11.50",
        "8.00",
        "10.03",
    ],
    [
        "August",
        "10.22",
        "10.13",
        "10.32",
        "10.30",
        "10.14",
        "10.15",
        "10.17",
        "10.22",
        "10.21",
        "12.32",
        "---",
        "10.00",
        "11.50",
        "8.00",
        "9.98",
    ],
    [
        "September",
        "10.05",
        "9.96",
        "9.93",
        "9.88",
        "10.06",
        "10.01",
        "9.86",
        "9.60",
        "9.64",
        "11.64",
        "---",
        "10.00",
        "11.50",
        "8.00",
        "9.97",
    ],
    [
        "October",
        "---",
        "9.46",
        "9.64",
        "9.55",
        "9.38",
        "9.25",
        "9.57",
        "9.98",
        "10.10",
        "10.69",
        "---",
        "10.00",
        "11.50",
        "8.00",
        "9.74",
    ],
    [
        "November",
        "---",
        "10.00",
        "10.03",
        "10.01",
        "10.02",
        "10.64",
        "10.28",
        "10.56",
        "10.62",
        "10.91",
        "---",
        "10.00",
        "11.50",
        "8.00",
        "9.79",
    ],
    [
        "December",
        "---",
        "10.42",
        "10.46",
        "10.61",
        "10.48",
        "10.76",
        "10.82",
        "10.87",
        "10.88",
        "10.99",
        "---",
        "10.00",
        "11.50",
        "8.00",
        "9.99",
    ],
    [
        "January",
        "---",
        "10.12",
        "10.30",
        "10.36",
        "10.46",
        "10.27",
        "10.39",
        "10.45",
        "10.52",
        "10.67",
        "---",
        "10.00",
        "11.50",
        "8.00",
        "9.94",
    ],
    [
        "February",
        "---",
        "10.11",
        "10.19",
        "10.24",
        "10.43",
        "10.28",
        "10.32",
        "10.29",
        "10.37",
        "10.58",
        "---",
        "10.00",
        "11.50",
        "7.50",
        "9.90",
    ],
    [
        "March",
        "---",
        "9.81",
        "9.93",
        "9.94",
        "9.72",
        "10.13",
        "10.20",
        "10.35",
        "10.45",
        "9.98",
        "---",
        "10.00",
        "11.50",
        "7.50",
        "10.01",
    ],
    [
        "April",
        "---",
        "9.98",
        "10.25",
        "10.33",
        "10.14",
        "10.63",
        "10.88",
        "11.11",
        "11.17",
        "10.54",
        "---",
        "10.00",
        "11.50",
        "7.50",
        "9.95",
    ],
    [
        "Source: Monetary Policy Department and Debt Management Department, Bangladesh Bank.",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    ],
    [
        "Note: * Policy rate, SLF rate and SDF rate are re-fixed at 10.00 %, 11.50% and 7.50 % "
        "respectively, effective from 15 February 2026. ---- = no auction conducted.",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    ],
]


# ---------------------------------------------------------------------------
# Instruction parsing
# ---------------------------------------------------------------------------


def test_parse_instruction_basic(mod):
    page, col = mod._parse_instruction("page=10 col=Policy rate (repo)")
    assert page == 10
    assert col == "Policy rate (repo)"


def test_parse_instruction_col_with_spaces(mod):
    page, col = mod._parse_instruction("page=10 col=SLF rate")
    assert page == 10
    assert col == "SLF rate"


def test_parse_instruction_missing_page_raises(mod):
    with pytest.raises(ParseError, match="page="):
        mod._parse_instruction("col=Policy rate (repo)")


def test_parse_instruction_missing_col_raises(mod):
    with pytest.raises(ParseError, match="col="):
        mod._parse_instruction("page=10")


def test_parse_instruction_non_numeric_page_raises(mod):
    with pytest.raises(ParseError):
        mod._parse_instruction("page=ten col=Policy rate (repo)")


# ---------------------------------------------------------------------------
# Header normalization — pdfplumber returns multi-line headers with \n inside,
# the matcher must collapse all whitespace and be case-insensitive.
# ---------------------------------------------------------------------------


def test_normalize_header_collapses_newlines(mod):
    assert mod._normalize_header("Policy rate\n(repo)") == "policy rate (repo)"


def test_normalize_header_collapses_multiple_spaces(mod):
    assert mod._normalize_header("  Policy   rate  (repo)  ") == "policy rate (repo)"


def test_normalize_header_handles_none(mod):
    assert mod._normalize_header(None) == ""


# ---------------------------------------------------------------------------
# Column resolution — find the right column index from the header row.
# ---------------------------------------------------------------------------


def test_find_column_index_policy_rate(mod):
    """The Policy rate (repo) header is in column 12 (0-indexed) in the
    MEI bulletin — across the embedded newline in the source label."""
    idx = mod._find_column_index(_PAGE10_TABLE, "Policy rate (repo)")
    assert idx == 12


def test_find_column_index_slf_rate(mod):
    idx = mod._find_column_index(_PAGE10_TABLE, "SLF rate")
    assert idx == 13


def test_find_column_index_sdf_rate(mod):
    idx = mod._find_column_index(_PAGE10_TABLE, "SDF rate")
    assert idx == 14


def test_find_column_index_case_insensitive(mod):
    idx = mod._find_column_index(_PAGE10_TABLE, "POLICY RATE (REPO)")
    assert idx == 12


def test_find_column_index_unknown_label_raises(mod):
    with pytest.raises(ParseError, match="column"):
        mod._find_column_index(_PAGE10_TABLE, "Made Up Column")


# ---------------------------------------------------------------------------
# Latest data-row walk — skip Source:/Note:/group-label rows, take the last
# row whose first cell is a month name and whose target cell parses as float.
# ---------------------------------------------------------------------------


def test_latest_value_policy_rate_is_april(mod):
    """The most recent month in the April 2026 bulletin is April: 10.00."""
    v = mod._latest_value_in_column(_PAGE10_TABLE, col_idx=12)
    assert v == 10.00


def test_latest_value_slf_rate_is_april(mod):
    v = mod._latest_value_in_column(_PAGE10_TABLE, col_idx=13)
    assert v == 11.50


def test_latest_value_sdf_rate_is_april(mod):
    """SDF was re-fixed at 7.50 effective 15 Feb 2026 — confirm we get the
    re-fixed value, not the pre-Feb 8.50."""
    v = mod._latest_value_in_column(_PAGE10_TABLE, col_idx=14)
    assert v == 7.50


def test_latest_value_skips_source_and_note_footers(mod):
    """If we stripped only based on numeric-cell presence we'd grab the
    last row that happens to have a number; the parser must explicitly
    require a month name in column 0."""
    # The footer rows (16, 17) have None in cols 12-14 anyway, but this
    # test guards against a future change that copies values down.
    table = [row[:] for row in _PAGE10_TABLE]
    table[16][12] = "999.99"  # forge a number in the Source: row
    table[17][12] = "888.88"  # forge a number in the Note: row
    v = mod._latest_value_in_column(table, col_idx=12)
    assert v == 10.00  # still picks April, not the forged footer numbers


def test_latest_value_skips_fy_group_label_rows(mod):
    """Rows 3 ('FY25') and 5 ('FY26') are group separators, not data."""
    # Already covered by the structural test above, but be explicit so a
    # future schema change that introduces another group label is caught.
    table = [row[:] for row in _PAGE10_TABLE]
    # If the parser were month-name-naive it might pick up FY26's empty
    # row. Add a number there and confirm it's still ignored.
    table[5][12] = "777.77"
    v = mod._latest_value_in_column(table, col_idx=12)
    assert v == 10.00


def test_latest_value_no_data_rows_raises(mod):
    """A table that has a header but no month rows should error clearly."""
    table = [_PAGE10_TABLE[0], _PAGE10_TABLE[1], _PAGE10_TABLE[2]]  # headers only
    with pytest.raises(ParseError, match="no data row"):
        mod._latest_value_in_column(table, col_idx=12)


# ---------------------------------------------------------------------------
# Sanity range — refuse to return implausible values.
# ---------------------------------------------------------------------------


def test_latest_value_rejects_out_of_sanity_range(mod):
    table = [row[:] for row in _PAGE10_TABLE]
    table[15][12] = "9999.99"  # April policy rate = 9999.99 is absurd
    with pytest.raises(ParseError, match="sanity"):
        mod._latest_value_in_column(table, col_idx=12)


# ---------------------------------------------------------------------------
# Registry wiring.
# ---------------------------------------------------------------------------


def test_parser_registered():
    """Confirm the @register decorator wired the parser into the registry."""
    import parsers.pdf_table_column_latest  # noqa: F401  triggers registration
    from parsers.registry import REGISTRY

    assert "pdf_table_column_latest" in REGISTRY


# ---------------------------------------------------------------------------
# Integration tests against the real PDF (skipped if the probe fixture is
# absent — keeps the suite green on a fresh checkout without the local probe).
# ---------------------------------------------------------------------------


@pytest.fixture
def real_pdf_artifact() -> FetchResult:
    if not _PROBE_PDF.exists():
        pytest.skip(f"PDF probe fixture not present at {_PROBE_PDF}")
    return FetchResult(
        indicator_id="policy_rate_repo",
        artifact_path=_PROBE_PDF,
        artifact_type="pdf",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.bb.org.bd/en/index.php/publication/publictn/3/11",
        sha256="x" * 64,
        cache_hit=False,
    )


def test_integration_policy_rate_repo(real_pdf_artifact):
    """End-to-end: real April 2026 MEI PDF -> 10.00 for Policy rate (repo)."""
    import parsers.pdf_table_column_latest  # noqa: F401
    from parsers.registry import get_parser

    parser = get_parser("pdf_table_column_latest")
    result = parser.parse(real_pdf_artifact, "page=10 col=Policy rate (repo)")
    assert isinstance(result, ParseResult)
    assert result.value == 10.00
    assert result._parse_strategy == "pdf_table_column_latest"


def test_integration_slf_rate(real_pdf_artifact):
    """End-to-end: real April 2026 MEI PDF -> 11.50 for SLF rate."""
    import parsers.pdf_table_column_latest  # noqa: F401
    from parsers.registry import get_parser

    parser = get_parser("pdf_table_column_latest")
    result = parser.parse(real_pdf_artifact, "page=10 col=SLF rate")
    assert result.value == 11.50


def test_integration_sdf_rate(real_pdf_artifact):
    """End-to-end: real April 2026 MEI PDF -> 7.50 for SDF rate
    (re-fixed effective 15 Feb 2026; April is post-rebase)."""
    import parsers.pdf_table_column_latest  # noqa: F401
    from parsers.registry import get_parser

    parser = get_parser("pdf_table_column_latest")
    result = parser.parse(real_pdf_artifact, "page=10 col=SDF rate")
    assert result.value == 7.50


def test_integration_unknown_column_raises(real_pdf_artifact):
    import parsers.pdf_table_column_latest  # noqa: F401
    from parsers.registry import get_parser

    parser = get_parser("pdf_table_column_latest")
    with pytest.raises(ParseError):
        parser.parse(real_pdf_artifact, "page=10 col=Made Up Column")


def test_integration_page_out_of_range_raises(real_pdf_artifact):
    import parsers.pdf_table_column_latest  # noqa: F401
    from parsers.registry import get_parser

    parser = get_parser("pdf_table_column_latest")
    with pytest.raises(ParseError, match="page"):
        parser.parse(real_pdf_artifact, "page=999 col=Policy rate (repo)")
