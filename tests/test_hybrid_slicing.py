"""Tests for PDF page-window slicing and debug logging in parsers.hybrid."""
from __future__ import annotations

import logging

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from parsers.hybrid import _extract_pdf_text, _parse_page_hint


@pytest.fixture(autouse=True)
def _disable_ocr_by_default(monkeypatch):
    """Test fixtures use brief text well under the OCR threshold; opt-in per-test."""
    monkeypatch.setenv("ECONDELTA_DISABLE_OCR", "1")


def _make_multipage_pdf(path, page_count: int = 5) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    for i in range(1, page_count + 1):
        c.drawString(100, 750, f"PAGE_MARKER_{i}")
        c.drawString(100, 700, f"page body content number {i}")
        c.showPage()
    c.save()


@pytest.mark.parametrize(
    "instruction,expected",
    [
        ("Go to page 15 of the doc, first table", 15),
        ("Page 14 of doc, first table", 14),
        ("on PAGE 7", 7),
        ("see pages 22-24 for details", 22),
        ("page  3", 3),
        ("Self explanatory", None),
        ("", None),
        ("row=Total col=2", None),
    ],
)
def test_parse_page_hint(instruction, expected):
    assert _parse_page_hint(instruction) == expected


def test_extract_pdf_text_with_page_hint_returns_window(tmp_path):
    pdf = tmp_path / "doc.pdf"
    _make_multipage_pdf(pdf, page_count=5)
    text = _extract_pdf_text(pdf, page_hint=3, window=1)
    # Window of 1 around page 3 = pages 2,3,4
    assert "PAGE_MARKER_2" in text
    assert "PAGE_MARKER_3" in text
    assert "PAGE_MARKER_4" in text
    assert "PAGE_MARKER_1" not in text
    assert "PAGE_MARKER_5" not in text


def test_extract_pdf_text_default_window_is_3(tmp_path):
    """Default window=3 catches typical 1-3 page front-matter offset."""
    pdf = tmp_path / "doc.pdf"
    _make_multipage_pdf(pdf, page_count=20)
    text = _extract_pdf_text(pdf, page_hint=10)  # no window kwarg
    # Default window=3 around page 10 → pages 7..13
    for i in range(7, 14):
        assert f"PAGE_MARKER_{i}" in text, f"page {i} missing from default window"
    assert "PAGE_MARKER_6" not in text
    assert "PAGE_MARKER_14" not in text


def test_extract_pdf_text_no_hint_returns_all_pages(tmp_path):
    pdf = tmp_path / "doc.pdf"
    _make_multipage_pdf(pdf, page_count=5)
    text = _extract_pdf_text(pdf, page_hint=None)
    for i in range(1, 6):
        assert f"PAGE_MARKER_{i}" in text


def test_extract_pdf_text_clamps_window_at_doc_boundaries(tmp_path):
    pdf = tmp_path / "doc.pdf"
    _make_multipage_pdf(pdf, page_count=3)
    # Hint = page 1, window=1 → pages 1,2 (no page 0)
    text = _extract_pdf_text(pdf, page_hint=1, window=1)
    assert "PAGE_MARKER_1" in text
    assert "PAGE_MARKER_2" in text
    assert "PAGE_MARKER_3" not in text
    # Hint = page 3 (last), window=1 → pages 2,3 (no page 4)
    text = _extract_pdf_text(pdf, page_hint=3, window=1)
    assert "PAGE_MARKER_2" in text
    assert "PAGE_MARKER_3" in text


def test_extract_pdf_text_hint_beyond_doc_returns_empty_or_full(tmp_path):
    pdf = tmp_path / "doc.pdf"
    _make_multipage_pdf(pdf, page_count=3)
    # Hint = page 99 → out of range, should not crash
    text = _extract_pdf_text(pdf, page_hint=99, window=1)
    # We don't assert exact behavior; just no crash and returns a string
    assert isinstance(text, str)


def test_debug_log_emits_when_env_set(tmp_path, monkeypatch, caplog):
    pdf = tmp_path / "doc.pdf"
    _make_multipage_pdf(pdf, page_count=2)
    monkeypatch.setenv("ECONDELTA_DEBUG_PDF", "1")
    with caplog.at_level(logging.INFO, logger="hybrid"):
        _extract_pdf_text(pdf, page_hint=None, indicator_id="test_ind")
    debug_lines = [r for r in caplog.records if "pdf_text" in r.getMessage()]
    assert debug_lines, "expected at least one pdf_text debug log line"
    msg = debug_lines[0].getMessage()
    assert "test_ind" in msg
    assert "len=" in msg


def test_debug_log_silent_when_env_unset(tmp_path, monkeypatch, caplog):
    pdf = tmp_path / "doc.pdf"
    _make_multipage_pdf(pdf, page_count=2)
    monkeypatch.delenv("ECONDELTA_DEBUG_PDF", raising=False)
    with caplog.at_level(logging.INFO, logger="hybrid"):
        _extract_pdf_text(pdf, page_hint=None, indicator_id="test_ind")
    debug_lines = [r for r in caplog.records if "pdf_text" in r.getMessage()]
    assert not debug_lines, f"expected no debug logs, got: {[r.getMessage() for r in debug_lines]}"


# ---------------- HTML cleaner tests (Category C) ----------------
from parsers.hybrid import _clean_html  # noqa: E402


def test_clean_html_PRESERVES_script_blocks():
    """BB.org.bd embeds real table data inside inline <script> JSON / JS arrays.
    Stripping scripts regressed bill_bond_rates, policy_rate_slf_sdf, etc."""
    raw = "<html><body><script>var data = [11.99, 8.5];</script><table>DATA</table></body></html>"
    out = _clean_html(raw)
    assert "var data" in out
    assert "11.99" in out
    assert "DATA" in out


def test_clean_html_strips_style_blocks():
    raw = "<html><head><style>.t1{color:red}</style></head><body>DATA</body></html>"
    out = _clean_html(raw)
    assert ".t1" not in out
    assert "color:red" not in out
    assert "DATA" in out


def test_clean_html_strips_noscript_and_meta():
    raw = """<html><head>
    <meta charset="utf-8">
    <noscript>js required</noscript>
    </head><body>DATA</body></html>"""
    out = _clean_html(raw)
    assert "js required" not in out
    assert "DATA" in out


def test_clean_html_preserves_table_content():
    raw = """<html><body><table>
    <tr><td>row1</td><td>123</td></tr>
    <tr><td>total</td><td>456</td></tr>
    </table></body></html>"""
    out = _clean_html(raw)
    assert "row1" in out
    assert "total" in out
    assert "456" in out


def test_clean_html_handles_case_insensitive_style():
    raw = "<html><body><STYLE>noise</STYLE>DATA</body></html>"
    out = _clean_html(raw)
    assert "noise" not in out
    assert "DATA" in out


# ---------------- OCR fallback tests (Category A) ----------------
from unittest.mock import patch  # noqa: E402

from parsers.hybrid import _should_ocr  # noqa: E402


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", True),
        ("   \n  \t  ", True),
        ("a" * 199, True),
        ("a" * 200, False),
        ("Major Economic Indicators: Monthly Update " * 10, False),
    ],
)
def test_should_ocr_threshold(text, expected):
    """OCR fires when extracted text is empty or sub-200-char (likely scanned)."""
    assert _should_ocr(text) is expected


def test_extract_pdf_text_falls_back_to_ocr_when_text_empty(tmp_path, monkeypatch):
    """When pdfplumber returns nothing, OCR helper is called and its output is used."""
    pdf = tmp_path / "scanned.pdf"
    _make_multipage_pdf(pdf, page_count=2)  # has text — but we'll mock pdfplumber to return ''
    monkeypatch.delenv("ECONDELTA_DISABLE_OCR", raising=False)  # override autouse

    fake_ocr_text = "OCR_RECOVERED_TABLE_DATA: 1234.56"
    with patch("parsers.hybrid._ocr_pdf_pages", return_value=fake_ocr_text) as mock_ocr, \
         patch("pdfplumber.open") as mock_open:
        mock_open.return_value.__enter__.return_value.pages = [
            type("P", (), {"extract_text": lambda self: ""})() for _ in range(2)
        ]
        result = _extract_pdf_text(pdf, page_hint=None, indicator_id="scanned_test")

    assert mock_ocr.called, "OCR should be invoked when extracted text is empty"
    assert "OCR_RECOVERED_TABLE_DATA" in result


def test_extract_pdf_text_skips_ocr_when_text_present(tmp_path, monkeypatch):
    """When pdfplumber returns real text, OCR helper must NOT be called."""
    pdf = tmp_path / "text_pdf.pdf"
    _make_multipage_pdf(pdf, page_count=2)
    monkeypatch.delenv("ECONDELTA_DISABLE_OCR", raising=False)  # override autouse

    # Use enough fake text to clear the 200-char OCR threshold
    long_text = "x" * 300
    with patch("parsers.hybrid._ocr_pdf_pages") as mock_ocr, \
         patch("pdfplumber.open") as mock_open:
        mock_open.return_value.__enter__.return_value.pages = [
            type("P", (), {"extract_text": lambda self: long_text})() for _ in range(2)
        ]
        result = _extract_pdf_text(pdf, page_hint=None, indicator_id="text_test")

    assert not mock_ocr.called, "OCR must not run when pdfplumber returns text"
    assert long_text in result


def test_extract_pdf_text_disable_ocr_env_skips_fallback(tmp_path, monkeypatch):
    """ECONDELTA_DISABLE_OCR=1 disables the OCR fallback even on empty PDFs."""
    pdf = tmp_path / "scanned.pdf"
    _make_multipage_pdf(pdf, page_count=1)
    monkeypatch.setenv("ECONDELTA_DISABLE_OCR", "1")

    with patch("parsers.hybrid._ocr_pdf_pages") as mock_ocr, \
         patch("pdfplumber.open") as mock_open:
        mock_open.return_value.__enter__.return_value.pages = [
            type("P", (), {"extract_text": lambda self: ""})()
        ]
        result = _extract_pdf_text(pdf, page_hint=None, indicator_id="scanned_test")

    assert not mock_ocr.called, "OCR must be disabled when env var set"
    assert result == ""
