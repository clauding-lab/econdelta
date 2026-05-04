"""Tests for the source_as_of fix — the four-layer bug repair.

Layer 1: ParseResult carries source_as_of (date | None).
Layer 2: FSAR PDF parser (pdf_component) extracts quarter-end date.
Layer 3: DAM ticker parser extracts "Date of report" from the page header.
Layer 4: NBR news parser (html_footer_ticker) extracts article byline date.
Layer 5: supabase_writer threads per-metric source_as_of through to Supabase rows.
Layer 6: aggregate_latest threads source_as_of from v3 snapshots to writer.

All tests run with ECONDELTA_SKIP_SUPABASE=1 (set in conftest.py) so no real
Supabase calls occur. External dependencies (reportlab, BeautifulSoup) are the
same libraries used by the existing test suite.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
from reportlab.pdfgen import canvas

import parsers.pdf_component  # noqa: F401 — registers pdf_component parser
import parsers.dam_ticker  # noqa: F401 — registers dam_ticker parser
import parsers.html_footer_ticker  # noqa: F401 — registers html_footer_ticker parser
from fetchers.base import FetchResult
from parsers.base import ParseResult
from parsers.registry import get_parser
from utils.supabase_writer import _rows_from_data, upsert_metric_history


# ---------------------------------------------------------------------------
# Layer 1: ParseResult.source_as_of field
# ---------------------------------------------------------------------------

class TestParseResultSourceAsOf:
    def test_source_as_of_defaults_to_none(self):
        """Existing code that doesn't supply source_as_of still works."""
        pr = ParseResult(value=35.73, _parse_strategy="pdf_component")
        assert pr.source_as_of is None

    def test_source_as_of_accepts_date(self):
        """Parser can supply a real publication date."""
        q_end = date(2025, 9, 30)
        pr = ParseResult(value=35.73, _parse_strategy="pdf_component", source_as_of=q_end)
        assert pr.source_as_of == q_end

    def test_source_as_of_none_and_date_are_both_valid(self):
        """Both sentinel values round-trip without error."""
        assert ParseResult(value=1.0, source_as_of=None).source_as_of is None
        assert ParseResult(value=1.0, source_as_of=date(2025, 6, 30)).source_as_of == date(2025, 6, 30)

    def test_parse_result_is_still_frozen(self):
        """Frozen dataclass must not regress — source_as_of is immutable."""
        pr = ParseResult(value=10.0, source_as_of=date(2025, 9, 30))
        with pytest.raises((AttributeError, TypeError)):
            pr.source_as_of = date(2025, 12, 31)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Layer 2: FSAR PDF parser extracts quarter-end date
# ---------------------------------------------------------------------------

def _make_fsar_pdf(tmp_path: Path, cover_text: str) -> FetchResult:
    """Build a minimal PDF whose first page contains cover_text."""
    pdf_path = tmp_path / "fsar.pdf"
    c = canvas.Canvas(str(pdf_path))
    c.drawString(72, 750, cover_text)
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


class TestFsarPdfSourceAsOf:
    def test_extracts_quarter_end_date_from_cover(self, tmp_path: Path):
        """Parsing 'Quarter ending 30 September 2025' yields date(2025, 9, 30)."""
        cover = (
            "Financial Stability Assessment Report\n"
            "Quarter ending 30 September 2025\n"
            "Component 11a NPL ratio: 35.73"
        )
        artifact = _make_fsar_pdf(tmp_path, cover)
        parser = get_parser("pdf_component")
        result = parser.parse(artifact, instruction="Component 11a")
        assert result.value == 35.73
        assert result.source_as_of == date(2025, 9, 30)

    def test_extracts_december_quarter(self, tmp_path: Path):
        """Q4 quarter 'Quarter ending 31 December 2025' yields date(2025, 12, 31)."""
        cover = (
            "BB Financial Stability Report\n"
            "Quarter ending 31 December 2025\n"
            "Component 1a CRAR: 11.56"
        )
        artifact = _make_fsar_pdf(tmp_path, cover)
        parser = get_parser("pdf_component")
        result = parser.parse(artifact, instruction="Component 1a")
        assert result.source_as_of == date(2025, 12, 31)

    def test_falls_back_to_none_when_no_quarter_stamp(self, tmp_path: Path):
        """PDF without a recognizable quarter line yields source_as_of=None."""
        cover = "Component 5b Net NPL: 22.10"
        artifact = _make_fsar_pdf(tmp_path, cover)
        parser = get_parser("pdf_component")
        result = parser.parse(artifact, instruction="Component 5b")
        assert result.source_as_of is None

    def test_march_quarter(self, tmp_path: Path):
        """Q1 'Quarter ending 31 March 2026' yields date(2026, 3, 31)."""
        cover = (
            "FSAR Q1 2026\n"
            "Quarter ending 31 March 2026\n"
            "Component 2b CAR: 9.85"
        )
        artifact = _make_fsar_pdf(tmp_path, cover)
        parser = get_parser("pdf_component")
        result = parser.parse(artifact, instruction="Component 2b")
        assert result.source_as_of == date(2026, 3, 31)


# ---------------------------------------------------------------------------
# Layer 3: DAM ticker parser extracts report date
# ---------------------------------------------------------------------------

def _make_dam_html(tmp_path: Path, date_line: str, ticker_body: str) -> FetchResult:
    html = f"<html><body>{date_line}{ticker_body}</body></html>"
    p = tmp_path / "dam.html"
    p.write_text(html, encoding="utf-8")
    return FetchResult(
        indicator_id="food_rice_coarse",
        artifact_path=p,
        artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="http://market.dam.gov.bd/market_daily_price_report",
        sha256="b" * 64,
        cache_hit=False,
    )


_DAM_TICKER = (
    "আমন চাল - মোটা :&nbsp;৪৮.০০ - ৫০.০০ ▲০.০০% "
    "চিনি (দেশী) :&nbsp;১৩২.০০ - ১৩৫.০০ ▲০.০০%"
)


class TestDamTickerSourceAsOf:
    def test_extracts_date_of_report_english(self, tmp_path: Path):
        """'Date of report: 04-05-2026' yields date(2026, 5, 4)."""
        date_line = "<p>Date of report: 04-05-2026</p>"
        artifact = _make_dam_html(tmp_path, date_line, _DAM_TICKER)
        parser = get_parser("dam_ticker")
        result = parser.parse(artifact, instruction="আমন চাল - মোটা")
        assert result.source_as_of == date(2026, 5, 4)

    def test_extracts_date_with_slashes(self, tmp_path: Path):
        """'Date of report: 04/05/2026' also parses correctly."""
        date_line = "<p>Date of report: 04/05/2026</p>"
        artifact = _make_dam_html(tmp_path, date_line, _DAM_TICKER)
        parser = get_parser("dam_ticker")
        result = parser.parse(artifact, instruction="চিনি (দেশী)")
        assert result.source_as_of == date(2026, 5, 4)

    def test_falls_back_to_none_when_no_date_header(self, tmp_path: Path):
        """Page without a date header yields source_as_of=None (not a crash)."""
        artifact = _make_dam_html(tmp_path, "", _DAM_TICKER)
        parser = get_parser("dam_ticker")
        result = parser.parse(artifact, instruction="চিনি (দেশী)")
        assert result.source_as_of is None

    def test_value_extraction_unaffected_by_date(self, tmp_path: Path):
        """Midpoint math is unchanged after adding date extraction."""
        date_line = "<p>Date of report: 01-05-2026</p>"
        artifact = _make_dam_html(tmp_path, date_line, _DAM_TICKER)
        parser = get_parser("dam_ticker")
        result = parser.parse(artifact, instruction="চিনি (দেশী)")
        assert result.value == 133.5  # mid of 132 and 135


# ---------------------------------------------------------------------------
# Layer 4: NBR news parser extracts article byline date
# ---------------------------------------------------------------------------

def _make_news_html(tmp_path: Path, meta_date: str | None, body: str, name: str = "nbr.html") -> FetchResult:
    if meta_date:
        meta_tag = f'<meta property="article:published_time" content="{meta_date}" />'
    else:
        meta_tag = ""
    html = f"<html><head>{meta_tag}</head><body>{body}</body></html>"
    p = tmp_path / name
    p.write_text(html, encoding="utf-8")
    return FetchResult(
        indicator_id="nbr_fytd_collected_tbs",
        artifact_path=p,
        artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.tbsnews.net/nbr/some-article",
        sha256="c" * 64,
        cache_hit=False,
    )


_NBR_BODY = "<p>NBR collected BDT 2,73,000 crore Policy Rate 10.00% blah blah</p>"


class TestNbrNewsSourceAsOf:
    def test_extracts_iso_datetime_meta(self, tmp_path: Path):
        """article:published_time ISO 8601 datetime yields date portion."""
        artifact = _make_news_html(tmp_path, "2026-04-15T06:30:00+06:00", _NBR_BODY)
        parser = get_parser("html_footer_ticker")
        result = parser.parse(artifact, instruction="Policy Rate")
        assert result.source_as_of == date(2026, 4, 15)

    def test_extracts_date_only_meta(self, tmp_path: Path):
        """article:published_time with date-only value also works."""
        artifact = _make_news_html(tmp_path, "2026-03-20", _NBR_BODY)
        parser = get_parser("html_footer_ticker")
        result = parser.parse(artifact, instruction="Policy Rate")
        assert result.source_as_of == date(2026, 3, 20)

    def test_falls_back_to_none_when_no_meta(self, tmp_path: Path):
        """Page without article:published_time yields source_as_of=None."""
        artifact = _make_news_html(tmp_path, None, _NBR_BODY)
        parser = get_parser("html_footer_ticker")
        result = parser.parse(artifact, instruction="Policy Rate")
        assert result.source_as_of is None

    def test_value_extraction_unaffected_by_meta(self, tmp_path: Path):
        """Value parsing is not broken by the meta date extraction."""
        artifact = _make_news_html(tmp_path, "2026-04-15T10:00:00Z", _NBR_BODY)
        parser = get_parser("html_footer_ticker")
        result = parser.parse(artifact, instruction="Policy Rate")
        assert result.value == 10.0


# ---------------------------------------------------------------------------
# Layer 5: supabase_writer threads source_as_of per-metric
# ---------------------------------------------------------------------------

def _make_session(status: int = 201) -> MagicMock:
    sess = MagicMock(spec=requests.Session)
    resp = MagicMock()
    resp.status_code = status
    resp.text = ""
    sess.post.return_value = resp
    return sess


class TestSupabaseWriterSourceAsOf:
    def test_rows_from_data_uses_global_as_of_when_no_per_metric_override(self):
        """When source_as_of_map is absent/empty, all rows use the global as_of."""
        from utils.supabase_writer import _rows_from_data
        rows = _rows_from_data({"npl": 35.73, "car": 11.56}, date(2026, 5, 4), "EconDelta")
        for row in rows:
            assert row["as_of"] == "2026-05-04"

    def test_rows_from_data_uses_per_metric_source_as_of_when_provided(self):
        """When source_as_of_map has an entry, that date wins for that metric."""
        from utils.supabase_writer import _rows_from_data
        source_as_of_map = {"npl": date(2025, 9, 30)}
        rows = _rows_from_data(
            {"npl": 35.73, "car": 11.56},
            date(2026, 5, 4),
            "EconDelta",
            source_as_of_map=source_as_of_map,
        )
        by_id = {r["metric_id"]: r for r in rows}
        assert by_id["npl"]["as_of"] == "2025-09-30"   # overridden
        assert by_id["car"]["as_of"] == "2026-05-04"   # global fallback

    def test_upsert_accepts_source_as_of_map_kwarg(self):
        """upsert_metric_history accepts source_as_of_map without raising."""
        sess = _make_session()
        n = upsert_metric_history(
            data={"npl": 35.73},
            as_of=date(2026, 5, 4),
            source_as_of_map={"npl": date(2025, 9, 30)},
            url="https://example.supabase.co",
            service_key="sk_test",
            session=sess,
        )
        assert n == 1
        payload = sess.post.call_args[1]["json"]
        assert payload[0]["as_of"] == "2025-09-30"

    def test_upsert_source_as_of_map_partial_override(self):
        """Map overrides only the listed metrics; others use global as_of."""
        sess = _make_session()
        upsert_metric_history(
            data={"npl": 35.73, "dsex": 5100.0, "car": 11.56},
            as_of=date(2026, 5, 4),
            source_as_of_map={"npl": date(2025, 9, 30), "car": date(2025, 9, 30)},
            url="https://example.supabase.co",
            service_key="sk_test",
            session=sess,
        )
        payload = sess.post.call_args[1]["json"]
        by_id = {r["metric_id"]: r for r in payload}
        assert by_id["npl"]["as_of"] == "2025-09-30"
        assert by_id["car"]["as_of"] == "2025-09-30"
        assert by_id["dsex"]["as_of"] == "2026-05-04"


# ---------------------------------------------------------------------------
# Layer 6: aggregate_latest threads source_as_of from v3 snapshots to writer
# ---------------------------------------------------------------------------

class TestAggregateSourceAsOfThreading:
    """Test that aggregate_latest._build_v3_blocks preserves source_as_of in snapshots,
    and that the main() call to upsert_metric_history passes a populated source_as_of_map."""

    def test_v3_snapshot_with_source_as_of_is_preserved_in_domains(self, tmp_path: Path, monkeypatch):
        """A v3 snapshot that carries source_as_of survives the _build_v3_blocks pipeline."""
        import json
        from datetime import datetime, timezone

        # Minimal indicator registry
        registry = {
            "indicators": [
                {
                    "id": "gross_npl_ratio",
                    "name": "Gross NPL Ratio",
                    "domain": "banking",
                    "cadence": "quarterly",
                    "parse": {"deterministic": "pdf_component", "llm_prompt": "x.txt",
                               "value_type": "percent", "valid_range": [0.0, 50.0]},
                    "fetch": {"type": "pdf", "url": "https://x", "discover": "latest_pdf_link"},
                }
            ]
        }
        reg_path = tmp_path / "sources-v3.json"
        reg_path.write_text(json.dumps(registry))

        # Write a snapshot that includes source_as_of
        snap_dir = tmp_path / "data" / "gross_npl_ratio"
        snap_dir.mkdir(parents=True)
        snap = {
            "indicator_id": "gross_npl_ratio",
            "name": "Gross NPL Ratio",
            "domain": "banking",
            "cadence": "quarterly",
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "source_url": "https://x",
            "value": 35.73,
            "value_type": "percent",
            "previous_value": None,
            "change_pct": None,
            "_provenance": "deterministic",
            "_artifact_sha256": "a" * 64,
            "_parse_strategy": "pdf_component",
            "sanity_note": None,
            "source_as_of": "2025-09-30",  # the fix: date is stored in snapshot JSON
        }
        (snap_dir / "2026-05-04.json").write_text(json.dumps(snap))

        # Patch the module-level paths used by aggregate_latest
        import aggregate_latest as agg
        monkeypatch.setattr(agg, "SOURCES_V3_PATH", reg_path)
        monkeypatch.setattr(agg, "DATA_DIR", tmp_path / "data")

        now = datetime.now(timezone.utc)
        data_additions, domains, freshness, alerts = agg._build_v3_blocks(now)

        # The snapshot should appear in the domains dict
        assert "gross_npl_ratio" in domains.get("banking", {})
        snap_back = domains["banking"]["gross_npl_ratio"]
        assert snap_back.get("source_as_of") == "2025-09-30"

    def test_build_source_as_of_map_from_domains(self, tmp_path: Path, monkeypatch):
        """_build_source_as_of_map extracts date from snapshot source_as_of strings."""
        import json
        from datetime import datetime, timezone

        import aggregate_latest as agg

        # Build a domains dict as _build_v3_blocks would return it
        domains = {
            "banking": {
                "gross_npl_ratio": {
                    "value": 35.73, "cadence": "quarterly",
                    "source_as_of": "2025-09-30",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
                "banking_sector_crar": {
                    "value": 11.56, "cadence": "quarterly",
                    "source_as_of": "2025-09-30",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
            },
            "commodities": {
                "food_rice_coarse": {
                    "value": 49.0, "cadence": "daily",
                    # No source_as_of — daily, fallback to write date
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
            },
        }

        result = agg._build_source_as_of_map(domains)

        assert result["gross_npl_ratio"] == date(2025, 9, 30)
        assert result["banking_sector_crar"] == date(2025, 9, 30)
        assert "food_rice_coarse" not in result  # no source_as_of → not in map

    def test_build_source_as_of_map_skips_invalid_dates(self, tmp_path: Path, monkeypatch):
        """Malformed date strings are silently skipped (no crash)."""
        import aggregate_latest as agg

        domains = {
            "banking": {
                "gross_npl_ratio": {
                    "value": 35.73,
                    "source_as_of": "not-a-date",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        }
        result = agg._build_source_as_of_map(domains)
        assert "gross_npl_ratio" not in result
