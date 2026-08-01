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

class TestSourceAsOfPropagatesToBriefAliases:
    """The Brief's banking builder calls get_latest("banking_npl_pct") — the
    brief-side ALIAS, not the EconDelta indicator id ("gross_npl_ratio"). The
    publication-date override must follow that rename, or the SPA never sees it."""

    def test_alias_inherits_source_indicator_date(self):
        import aggregate_latest as agg

        domains = {
            "money_market": {
                "gross_npl_ratio": {
                    "value": 35.73, "cadence": "quarterly",
                    "source_as_of": "2025-09-30",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
                "banking_sector_crar": {
                    "value": 1.56, "cadence": "quarterly",
                    "source_as_of": "2025-09-30",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        }
        m = agg._build_source_as_of_map(domains)
        assert m["gross_npl_ratio"] == date(2025, 9, 30)
        # The keys the brief actually reads — must inherit the same date:
        assert m["banking_npl_pct"] == date(2025, 9, 30)
        assert m["banking_car_pct"] == date(2025, 9, 30)

    def test_unit_conversion_alias_inherits_date(self, monkeypatch):
        """A converted brief key (value scaled) still reports the SOURCE's date —
        a unit conversion changes the number, not the reporting period."""
        import aggregate_latest as agg

        monkeypatch.setitem(agg.BRIEF_CONVERSIONS, "fake_brief_tn", ("some_quarterly_cr", 1e-5))
        domains = {
            "fiscal": {
                "some_quarterly_cr": {
                    "value": 1000.0, "cadence": "quarterly",
                    "source_as_of": "2025-09-30",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        }
        m = agg._build_source_as_of_map(domains)
        assert m["some_quarterly_cr"] == date(2025, 9, 30)
        assert m["fake_brief_tn"] == date(2025, 9, 30)


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

    def test_daily_metric_without_date_now_warns_log_only(self, caplog):
        """Tier-1 PR: the silent-freeze warning now covers ALL cadences, not
        just quarterly/fiscal_year -- a daily/monthly source that stops
        recovering its date is just as capable of forging a stale-reads-fresh
        row (this is exactly the bug class the Tier-1 as_of forgery fix
        addresses for bb_forex/dse_market/commodity_prices). This synthetic
        snapshot carries no `_parse_strategy`, so it is NOT on the by-design-
        undated allow-list (_NEVER_DATED_PARSE_STRATEGIES) and DOES warn --
        previously (pre-fix) it silently did not. See
        test_live_scrape_parser_stays_silent_on_the_allow_list below for the
        sibling case that correctly stays silent."""
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
        assert any(
            "food_rice_coarse" in r.getMessage() for r in caplog.records
        ), "daily/monthly cadences must now warn too -- only the parser allow-list is silent"

    def test_live_scrape_parser_stays_silent_on_the_allow_list(self, caplog):
        """html_table_row / html_call_money / dse_sector_heat scrape a LIVE
        page with no publication date printed anywhere to recover --
        source_as_of is legitimately always absent for these, so warning here
        would be pure noise on every single aggregate run (~9 registry
        indicators). The allow-list keys off `_parse_strategy`, not cadence,
        because these parsers also back some "weekly" registry entries
        (tbond_5y_yield / tbond_10y_yield) where the scrape is still same-day
        HTML with nothing to date."""
        import aggregate_latest as agg

        domains = {
            "money_market": {
                "bill_bond_rates": {
                    "value": 10.5,
                    "cadence": "daily",
                    "_parse_strategy": "html_table_row",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
                "tbond_5y_yield": {
                    "value": 11.2,
                    "cadence": "weekly",
                    "_parse_strategy": "html_table_row",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
                "call_money_rate": {
                    "value": 9.8,
                    "cadence": "daily",
                    "_parse_strategy": "html_call_money",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        }
        with caplog.at_level(logging.WARNING, logger="aggregate_latest"):
            agg._build_source_as_of_map(domains)
        for indicator_id in ("bill_bond_rates", "tbond_5y_yield", "call_money_rate"):
            assert not any(indicator_id in r.getMessage() for r in caplog.records), (
                f"{indicator_id} uses a live-scrape parser with no date to "
                "recover -- must stay silent (allow-list)"
            )


class TestUndatedWarningDiscordSplit:
    """Item 3 of the Tier-1 as_of forgery fix: quarterly/fiscal_year undated
    metrics fire a Discord `notify()` (few enough to be useful signal);
    daily/weekly/monthly stay log-only (dozens possible on a broad outage --
    the repo's alert-noise rule, see feedback_observability_allow_list_pattern.md)."""

    def test_quarterly_undated_metric_fires_discord_notify(self, monkeypatch):
        import aggregate_latest as agg

        notify_calls: list[tuple] = []
        monkeypatch.setattr(agg, "notify", lambda *a, **k: notify_calls.append(a))
        domains = {
            "money_market": {
                "gross_npl_ratio": {
                    "value": 35.73, "cadence": "quarterly",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        }
        agg._build_source_as_of_map(domains)
        assert len(notify_calls) == 1
        assert notify_calls[0][0] == "warning"
        # Title is now stable (batched alert, review round 1 item 4) — the id
        # lives in the body/message, not the title.
        assert "gross_npl_ratio" in notify_calls[0][2]

    def test_multiple_undated_quarterly_ids_fire_exactly_one_notify(self, monkeypatch):
        """Review round 1, item 4: systemd starts a fresh process per aggregate
        run, so the notifier's (level, title) dedup can't collapse a per-id
        title across runs -- a chronically-undated id (landmine 26's
        debt_gdp_ratio / gdp / fy_* / debt stocks, up to 11 quarterly+
        fiscal_year indicators) would otherwise fire a separate Discord
        message EVERY run, forever. All undated quarterly/fiscal_year ids in
        one run must collapse into ONE notify call, listing every id in the
        body."""
        import aggregate_latest as agg

        notify_calls: list[tuple] = []
        monkeypatch.setattr(agg, "notify", lambda *a, **k: notify_calls.append(a))
        domains = {
            "money_market": {
                "gross_npl_ratio": {
                    "value": 35.73, "cadence": "quarterly",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
            },
            "fiscal": {
                "debt_gdp_ratio": {
                    "value": 34.5, "cadence": "fiscal_year",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
                "gdp": {
                    "value": 45000000.0, "cadence": "fiscal_year",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
            },
        }
        agg._build_source_as_of_map(domains)
        assert len(notify_calls) == 1, "3 undated ids must collapse into exactly 1 notify call"
        level, title, message = notify_calls[0]
        assert level == "warning"
        assert title == "aggregate — undated slow-cadence indicators"
        for indicator_id in ("gross_npl_ratio", "debt_gdp_ratio", "gdp"):
            assert indicator_id in message

    def test_monthly_undated_metric_logs_only_no_discord(self, monkeypatch, caplog):
        import aggregate_latest as agg

        notify_calls: list[tuple] = []
        monkeypatch.setattr(agg, "notify", lambda *a, **k: notify_calls.append(a))
        domains = {
            "fiscal": {
                "tax_revenue": {
                    "value": 30000.0, "cadence": "monthly",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        }
        with caplog.at_level(logging.WARNING, logger="aggregate_latest"):
            agg._build_source_as_of_map(domains)
        assert notify_calls == [], "monthly must stay log-only -- no Discord"
        assert any("tax_revenue" in r.getMessage() for r in caplog.records)

    def test_weekly_undated_metric_logs_only_no_discord(self, monkeypatch, caplog):
        import aggregate_latest as agg

        notify_calls: list[tuple] = []
        monkeypatch.setattr(agg, "notify", lambda *a, **k: notify_calls.append(a))
        domains = {
            "forex_and_reserves": {
                "fx_reserve_gross_and_bpm6": {
                    "value": 34.5, "cadence": "weekly",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        }
        with caplog.at_level(logging.WARNING, logger="aggregate_latest"):
            agg._build_source_as_of_map(domains)
        assert notify_calls == [], "weekly must stay log-only -- no Discord"
        assert any("fx_reserve_gross_and_bpm6" in r.getMessage() for r in caplog.records)
