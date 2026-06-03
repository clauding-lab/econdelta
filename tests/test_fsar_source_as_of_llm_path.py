"""Regression tests for the FSAR ``source_as_of`` repair (LLM-fallback path).

Context: the BB QFSAR is parsed via the ``pdf_component`` deterministic parser,
which is built for "Component <ID>" labels in the Monthly Economic Indicators
bulletin. On the QFSAR's exec-summary prose it fails value extraction and
``parsers/hybrid.parse_one`` falls through to the LLM extract path. That path
historically did NOT recover ``source_as_of`` ("not recoverable here"), so the
quarter-end date was lost and the metric was stamped with today's run date —
which made a stale Q3-2025 figure (NPL = 35.73%) look fresh on The Brief.

These tests cover the three-part fix:
  1. ``_extract_quarter_end`` understands the QFSAR's real cover phrasing
     ("...available as of end-September 2025"), not only "Quarter ending ...".
  2. ``PdfComponentParser.recover_source_as_of`` recovers the date from the
     cover even when value extraction would fail.
  3. ``parse_one`` attaches that date to the snapshot on the LLM-extract path.
  4. ``aggregate_latest._build_source_as_of_map`` warns when a slow-cadence
     metric has no ``source_as_of`` (the false-freshness guardrail).

conftest.py sets ECONDELTA_SKIP_SUPABASE=1 so no real Supabase calls occur.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import parsers.pdf_component  # noqa: F401 — registers the pdf_component parser
from fetchers.base import FetchResult
from parsers.hybrid import parse_one
from parsers.pdf_component import _extract_quarter_end
from parsers.registry import get_parser


def _make_fsar_pdf(tmp_path: Path, cover_text: str) -> FetchResult:
    """Build a minimal one-page PDF whose page contains ``cover_text``."""
    from reportlab.pdfgen import canvas

    pdf_path = tmp_path / "qfsar.pdf"
    c = canvas.Canvas(str(pdf_path))
    # One line per physical line so pdfplumber reconstructs them separately.
    y = 760
    for line in cover_text.splitlines() or [cover_text]:
        c.drawString(72, y, line)
        y -= 18
    c.showPage()
    c.save()
    return FetchResult(
        indicator_id="gross_npl_ratio",
        artifact_path=pdf_path,
        artifact_type="pdf",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.bb.org.bd/en/index.php/publication/publictn/2/60",
        sha256="a" * 64,
        cache_hit=False,
    )


# The exact line the QFSAR (July-September 2025) prints on its cover page.
_QFSAR_REFERENCE_LINE = (
    "The report is based on data and information available as of "
    "end-September 2025, unless stated otherwise."
)


# ---------------------------------------------------------------------------
# Part 1: _extract_quarter_end understands the QFSAR's real phrasings
# ---------------------------------------------------------------------------

class TestExtractQuarterEndBroadenedPhrasings:
    def test_qfsar_as_of_end_month(self):
        """The real QFSAR cover line maps to the September quarter-end."""
        assert _extract_quarter_end(_QFSAR_REFERENCE_LINE) == date(2025, 9, 30)

    def test_end_hyphen_december(self):
        assert _extract_quarter_end("position as at end-December 2025") == date(2025, 12, 31)

    def test_end_of_june(self):
        assert _extract_quarter_end("figures as of end of June 2025") == date(2025, 6, 30)

    def test_end_march(self):
        assert _extract_quarter_end("data available as of end-March 2026") == date(2026, 3, 31)

    def test_index_base_trap_is_not_matched(self):
        """'... as on 01 July 2025' (the index-base note) must NOT be read as a
        quarter-end — that bug would mislabel the whole report as 1 July."""
        assert _extract_quarter_end("Note: Index base was 100 as on 01 July 2025.") is None

    def test_weekend_substring_does_not_false_match(self):
        """'end' inside another word must not trigger a match."""
        assert _extract_quarter_end("Published on a weekend September 2025 review.") is None

    def test_existing_quarter_ending_still_works(self):
        """The original phrasing must keep working (no regression)."""
        assert _extract_quarter_end("Quarter ending 30 September 2025") == date(2025, 9, 30)

    def test_no_period_stamp_returns_none(self):
        assert _extract_quarter_end("Component 5b Net NPL: 22.10") is None

    def test_comparison_quarter_without_as_of_is_ignored(self):
        """A bare comparison reference ('compared to end-June 2025') is NOT the
        reporting period — it lacks the 'as of/at/on' anchor, so it must not match."""
        assert _extract_quarter_end("Compared to end-June 2025, NPL rose sharply.") is None

    def test_reference_line_wins_over_earlier_comparison(self):
        """When a comparison quarter precedes the reference line, .search() must
        still resolve to the report's own period (the anchored phrasing wins)."""
        txt = (
            "Compared to end-June 2025, asset quality deteriorated. "
            "The report is based on data available as of end-September 2025."
        )
        assert _extract_quarter_end(txt) == date(2025, 9, 30)

    def test_hyphenated_compound_is_ignored(self):
        """'front-end March 2026' must not match — the hyphen would trip a bare
        \\bend anchor, but the required 'as of/at/on' prefix rejects it."""
        assert _extract_quarter_end("The front-end March 2026 release notes.") is None

    def test_zero_separator_requires_real_separator(self):
        """'as of endApril 2025' must not match — a separator after 'end' is required."""
        assert _extract_quarter_end("data as of endApril 2025") is None


# ---------------------------------------------------------------------------
# Part 2: recover_source_as_of works even when value extraction fails
# ---------------------------------------------------------------------------

class TestRecoverSourceAsOf:
    def test_recovers_quarter_end_from_cover(self, tmp_path: Path):
        artifact = _make_fsar_pdf(
            tmp_path, "Issue: 33, 2025 (III)\nJuly-September 2025\n" + _QFSAR_REFERENCE_LINE
        )
        parser = get_parser("pdf_component")
        assert parser.recover_source_as_of(artifact) == date(2025, 9, 30)

    def test_returns_none_when_cover_has_no_stamp(self, tmp_path: Path):
        artifact = _make_fsar_pdf(tmp_path, "A report with no recognizable period stamp")
        parser = get_parser("pdf_component")
        assert parser.recover_source_as_of(artifact) is None


# ---------------------------------------------------------------------------
# Part 3: the LLM-extract path attaches source_as_of (the actual fix)
# ---------------------------------------------------------------------------

class TestHybridLlmPathRecoversDate:
    def _indicator(self) -> dict:
        return {
            "id": "gross_npl_ratio",
            "name": "Gross NPL Ratio (Banking Sector)",
            "domain": "money_market",
            "cadence": "quarterly",
            # A task string that does NOT appear in the PDF, so the deterministic
            # pdf_component parser raises ParseError and we fall to the LLM path.
            "fetch": {"type": "pdf", "task": "Go to page 13 and read the NPL ratio"},
            "parse": {
                "deterministic": "pdf_component",
                "llm_prompt": "pdf_component.txt",
                "value_type": "percent",
                "valid_range": [0.0, 50.0],
            },
        }

    def test_llm_fallback_snapshot_carries_source_as_of(self, tmp_path: Path):
        artifact = _make_fsar_pdf(
            tmp_path, "Issue: 33, 2025 (III)\nJuly-September 2025\n" + _QFSAR_REFERENCE_LINE
        )
        fake_extract = type("R", (), {"parsed": {"value": 35.73}, "raw_text": ""})()
        with patch("parsers.hybrid._llm_extract", return_value=fake_extract):
            snap = parse_one(artifact, self._indicator(), history=[])
        assert snap["value"] == 35.73
        assert snap["_provenance"] == "llm_extracted"
        # The fix: the Q3-2025 date is recovered even though the LLM produced the value.
        assert snap.get("source_as_of") == "2025-09-30"

    def test_llm_fallback_without_date_omits_source_as_of(self, tmp_path: Path):
        """A PDF with no recoverable date still works — source_as_of just absent."""
        artifact = _make_fsar_pdf(tmp_path, "Some FSAR text with no period stamp at all")
        fake_extract = type("R", (), {"parsed": {"value": 35.73}, "raw_text": ""})()
        with patch("parsers.hybrid._llm_extract", return_value=fake_extract):
            snap = parse_one(artifact, self._indicator(), history=[])
        assert snap["value"] == 35.73
        assert snap["_provenance"] == "llm_extracted"
        assert "source_as_of" not in snap


# ---------------------------------------------------------------------------
# Part 4: false-freshness guardrail — warn on undated slow-cadence metrics
# ---------------------------------------------------------------------------

class TestUndatedQuarterlyWarns:
    def test_warns_when_quarterly_metric_lacks_source_as_of(self, caplog):
        import aggregate_latest as agg

        domains = {
            "money_market": {
                "gross_npl_ratio": {
                    "value": 35.73,
                    "cadence": "quarterly",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    # no source_as_of
                },
            }
        }
        with caplog.at_level(logging.WARNING, logger="aggregate_latest"):
            result = agg._build_source_as_of_map(domains)
        assert "gross_npl_ratio" not in result  # behaviour unchanged: still absent
        assert any("gross_npl_ratio" in r.getMessage() for r in caplog.records), (
            "expected a WARNING naming the undated quarterly metric"
        )

    def test_fiscal_year_metric_without_date_also_warns(self, caplog):
        """fiscal_year is the other slow cadence in the config — it must warn too
        (the bug this guards against would otherwise skip all 7 FY indicators)."""
        import aggregate_latest as agg

        domains = {
            "fiscal": {
                "fy_export": {
                    "value": 4500.0,
                    "cadence": "fiscal_year",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        }
        with caplog.at_level(logging.WARNING, logger="aggregate_latest"):
            agg._build_source_as_of_map(domains)
        assert any("fy_export" in r.getMessage() for r in caplog.records)

    def test_daily_metric_without_date_does_not_warn(self, caplog):
        import aggregate_latest as agg

        domains = {
            "commodities": {
                "food_rice_coarse": {
                    "value": 49.0,
                    "cadence": "daily",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        }
        with caplog.at_level(logging.WARNING, logger="aggregate_latest"):
            agg._build_source_as_of_map(domains)
        assert not any(
            "food_rice_coarse" in r.getMessage() for r in caplog.records
        ), "daily metrics legitimately lack source_as_of — must not warn"
