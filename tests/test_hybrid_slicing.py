"""Tests for PDF page-window slicing and debug logging in parsers.hybrid."""
from __future__ import annotations

import logging

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from parsers.hybrid import _extract_pdf_text, _parse_page_hint


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
