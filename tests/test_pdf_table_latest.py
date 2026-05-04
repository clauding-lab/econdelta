"""Unit tests for parsers.pdf_table_latest.

These tests exercise the pure text-extraction function so they run without
pdfplumber installed (the integration via the @register decorator is exercised
in the broader parse_all suite on the VPS where pdfplumber is available).
"""
from __future__ import annotations

import importlib

import pytest


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
    label, mn = mod._parse_instruction('row="a) Reserve Money"')
    assert label == "a) Reserve Money"
    assert mn == 0.0


def test_parse_instruction_with_min(mod):
    label, mn = mod._parse_instruction('row="b) Broad Money" min=1000')
    assert label == "b) Broad Money"
    assert mn == 1000.0


def test_parse_instruction_missing_row_raises(mod):
    from parsers.base import ParseError
    with pytest.raises(ParseError):
        mod._parse_instruction("min=100")


def test_min_with_decimal(mod):
    label, mn = mod._parse_instruction('row="x" min=0.5')
    assert mn == 0.5


def test_parser_registered():
    """Confirm the @register decorator wired the parser into the registry."""
    import parsers.pdf_table_latest  # noqa: F401  triggers registration
    from parsers.registry import REGISTRY
    assert "pdf_table_latest" in REGISTRY
