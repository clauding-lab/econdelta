"""Unit tests for the call-money monthly backfill.

Tests verify the BUSINESS RULES, not just code shape (per testing.md principle 6):
  - the headline value equals the WAR of the *Interest Rates* table (not Turnover)
  - tenor buckets map to the right metric_ids with the right WARs
  - Table XVI emits ONLY monthly rows (annual context rows are skipped) and reads
    the 'Average' column
  - as_of is always YYYY-MM-01
  - dedupe is FIRST-write-wins so MMD summary beats trailing/ET for an overlap month
  - filename-drift: a homepage-redirect (non-PDF) fetch is skipped, not parsed

Fixtures are REAL Firecrawl-parsed BB PDF markdown:
  - fixtures/mmd_2025_04.md  (Money Market Dynamics, Apr 2025)
  - fixtures/et_2024_07.md   (Monthly Economic Trends, July 2024, Table XVI)
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

import scripts.backfill_call_money_monthly as b

FIXTURES = Path(__file__).parent / "fixtures"
MMD_APR2025 = (FIXTURES / "mmd_2025_04.md").read_text()
ET_JULY2024 = (FIXTURES / "et_2024_07.md").read_text()


# ---------------------------------------------------------------------------
# Money Market Dynamics — Interest Rates summary table
# ---------------------------------------------------------------------------


def test_mmd_summary_headline_is_interest_rate_war_not_turnover():
    """Headline must be 10.07 (WAR from Interest Rates table), NOT 7433.24
    (a Turnover-table number with the same 'A. Call Money Transaction' label)."""
    rows = b.parse_mmd_summary(MMD_APR2025, 2025, 4)
    headline = [r for r in rows if r.metric_id == "call_money_rate_monthly"]
    assert len(headline) == 1
    assert headline[0].value == 10.07
    assert headline[0].as_of == date(2025, 4, 1)


def test_mmd_summary_maps_three_tenor_buckets():
    rows = {r.metric_id: r.value for r in b.parse_mmd_summary(MMD_APR2025, 2025, 4)}
    assert rows["call_money_rate_1d_monthly"] == 9.93    # Overnight WAR
    assert rows["call_money_rate_14d_monthly"] == 10.80  # Short-notice 2-14d WAR
    assert rows["call_money_rate_90d_monthly"] == 11.34  # Term 15-364d WAR


def test_mmd_summary_as_of_is_month_first():
    for r in b.parse_mmd_summary(MMD_APR2025, 2025, 4):
        assert r.as_of.day == 1


def test_mmd_summary_missing_headline_raises():
    with pytest.raises(b.BackfillError):
        b.parse_mmd_summary("no relevant table here", 2025, 4)


# ---------------------------------------------------------------------------
# Money Market Dynamics — embedded trailing WAR tables
# ---------------------------------------------------------------------------


# Anchors = summary WARs for Apr-2025 (short-notice 10.80, term 11.34).
_APR2025_ANCHORS = {
    "call_money_rate_14d_monthly": 10.80,
    "call_money_rate_90d_monthly": 11.34,
}


def test_mmd_trailing_short_notice_series():
    rows = b.parse_mmd_trailing(MMD_APR2025, _APR2025_ANCHORS)
    sn = {r.as_of.isoformat(): r.value
          for r in rows if r.metric_id == "call_money_rate_14d_monthly"}
    # Jul-24 .. Apr-25 from the Short-notice WAR row (last value 10.80 anchors it).
    assert sn["2024-07-01"] == 10.08
    assert sn["2024-12-01"] == 11.36
    assert sn["2025-04-01"] == 10.80


def test_mmd_trailing_term_series():
    rows = b.parse_mmd_trailing(MMD_APR2025, _APR2025_ANCHORS)
    term = {r.as_of.isoformat(): r.value
            for r in rows if r.metric_id == "call_money_rate_90d_monthly"}
    assert term["2024-07-01"] == 11.45
    assert term["2025-04-01"] == 11.34


def test_mmd_trailing_assigns_bucket_by_anchor_not_heading_order():
    """Regression: a Short-notice WAR table physically rendered under the
    '3. Term' heading (observed across firecrawl re-fetches) must still be
    attributed to _14d via its snapshot-month anchor (10.80), not to _90d."""
    md = (
        "3. Term Call Money\n"
        "|  | Jul-24 | Aug-24 | Sep-24 | Oct-24 | Nov-24 | Dec-24 | Jan-25 | Feb-25 | Mar-25 | Apr-25 |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| Turnover | 9996 | 5736 | 6875 | 6541 | 7732 | 10446 | 13160 | 9478 | 12337 | 10390 |\n"
        "| WAR | 10.08 | 10.15 | 10.78 | 10.95 | 11.12 | 11.36 | 11.46 | 11.10 | 10.80 | 10.80 |\n"
    )
    rows = b.parse_mmd_trailing(md, _APR2025_ANCHORS)
    # Last value 10.80 == short-notice anchor -> all rows are _14d, NOT _90d.
    assert {r.metric_id for r in rows} == {"call_money_rate_14d_monthly"}
    assert rows[0].value == 10.08


def test_mmd_trailing_drops_unanchored_table():
    """A WAR table whose snapshot value matches no anchor is dropped, not
    mis-attributed."""
    md = (
        "|  | Jul-24 | Aug-24 | Sep-24 | Oct-24 | Nov-24 | Dec-24 | Jan-25 | Feb-25 | Mar-25 | Apr-25 |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| WAR | 9.71 | 8.77 | 9.07 | 9.51 | 9.87 | 10.23 | 9.90 | 9.88 | 9.90 | 8.42 |\n"
    )
    rows = b.parse_mmd_trailing(md, _APR2025_ANCHORS)  # 8.42 matches nothing
    assert rows == []


def test_mmd_trailing_does_not_emit_headline_or_overnight():
    """The headline + overnight trailing series live only in charts; the
    trailing parser must NOT fabricate them from the WAR tables."""
    rows = b.parse_mmd_trailing(MMD_APR2025, _APR2025_ANCHORS)
    metrics = {r.metric_id for r in rows}
    assert "call_money_rate_monthly" not in metrics
    assert "call_money_rate_1d_monthly" not in metrics


# ---------------------------------------------------------------------------
# Economic Trends — Table XVI
# ---------------------------------------------------------------------------


def test_et_table_xvi_reads_average_column_not_highest():
    rows = b.parse_et_table_xvi(ET_JULY2024)
    by_date = {r.as_of.isoformat(): r.value for r in rows}
    # Jan-2022 Average is 2.43 (Highest is 5.25, Lowest is 1.00).
    assert by_date["2022-01-01"] == 2.43
    # Jan-2023 Average is 6.66.
    assert by_date["2023-01-01"] == 6.66


def test_et_table_xvi_emits_only_monthly_rows():
    """Annual context rows (2009, 2010, ... 2022) must NOT become history rows;
    they only set the year for the monthly rows beneath them."""
    rows = b.parse_et_table_xvi(ET_JULY2024)
    # No row should land on a non-real month or carry an annual-only value
    # like 4.39 (the 2009 annual Average).
    values = [r.value for r in rows]
    assert 4.39 not in values  # 2009 annual Average
    assert 11.16 not in values  # 2011 annual Average
    # Every emitted row is a real monthly row -> day == 1 and a valid month.
    for r in rows:
        assert r.metric_id == "call_money_rate_monthly"
        assert r.as_of.day == 1
        assert 1 <= r.as_of.month <= 12


def test_et_table_xvi_year_context_rolls_forward():
    """Monthly rows after the '2023' header must be stamped 2023, not 2022."""
    rows = b.parse_et_table_xvi(ET_JULY2024)
    by_date = {r.as_of.isoformat(): r.value for r in rows}
    assert by_date["2022-12-01"] == 5.80  # last 2022 month
    assert by_date["2023-01-01"] == 6.66  # first 2023 month


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def test_parse_month_token_mon_yy():
    assert b.parse_month_token("Apr-25") == (2025, 4)
    assert b.parse_month_token("Jul-24") == (2024, 7)


def test_parse_month_token_bare_month_defers_year():
    assert b.parse_month_token("April") == (-1, 4)
    assert b.parse_month_token("September") == (-1, 9)


def test_parse_month_token_rejects_non_month():
    assert b.parse_month_token("Turnover") is None
    assert b.parse_month_token("2023") is None


def test_normalise_as_of_is_month_first():
    assert b.normalise_as_of(2025, 4) == date(2025, 4, 1)


# ---------------------------------------------------------------------------
# Dedupe + Supabase row shape
# ---------------------------------------------------------------------------


def test_dedupe_first_write_wins():
    """First row for a (metric_id, as_of) wins — caller feeds MMD-summary first
    so it beats the trailing-table / ET value for the same month."""
    summary = b.Row("call_money_rate_14d_monthly", date(2025, 4, 1), 10.80, "mmd_summary")
    trailing = b.Row("call_money_rate_14d_monthly", date(2025, 4, 1), 99.9, "mmd_trailing")
    out = b.dedupe_rows([summary, trailing])
    assert len(out) == 1
    assert out[0].value == 10.80
    assert out[0].source_label == "mmd_summary"


def test_to_supabase_row_shape_matches_seed_macro_monthly():
    r = b.Row("call_money_rate_monthly", date(2025, 4, 1), 10.07, "mmd_summary")
    row = r.to_supabase()
    assert row == {
        "metric_id": "call_money_rate_monthly",
        "as_of": "2025-04-01",
        "value": 10.07,
        "source": b.DEFAULT_SOURCE,
        "source_as_of": "2025-04-01",
    }


def test_definitions_cover_all_metric_ids():
    defs = {d["metric_id"] for d in b.build_definition_rows()}
    assert defs == set(b.METRIC_DEFS)
    for d in b.build_definition_rows():
        assert d["domain"] == b.DOMAIN
        assert d["unit"] == "%"


# ---------------------------------------------------------------------------
# Fetch layer — filename-drift guard
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload

    def post(self, *a, **k):
        return _FakeResp(self._payload)


def test_fetch_skips_homepage_redirect_non_pdf():
    """A constructed filename that 404s -> BB serves homepage HTML
    (contentType text/html, no numPages). Must return None, not parse junk."""
    payload = {"data": {"markdown": "<homepage>", "metadata": {
        "contentType": "text/html; charset=UTF-8", "url": "https://www.bb.org.bd/en/index.php"}}}
    out = b.fetch_pdf_markdown(
        "https://www.bb.org.bd/pub/monthly/moneymarket/money%20market%20dynamics_jan2026.pdf",
        api_key="test", session=_FakeSession(payload))
    assert out is None


def test_fetch_returns_markdown_for_real_pdf():
    payload = {"data": {"markdown": "Summary ...", "metadata": {
        "contentType": "application/pdf", "numPages": 18}}}
    out = b.fetch_pdf_markdown(
        "https://www.bb.org.bd/pub/monthly/moneymarket/money%20market%20dynamics_apr2025.pdf",
        api_key="test", session=_FakeSession(payload))
    assert out == "Summary ..."


def test_fetch_requires_api_key(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    with pytest.raises(b.BackfillError):
        b.fetch_pdf_markdown("https://x", api_key=None)


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------


def test_load_manifest_constructs_missing_url(tmp_path):
    m = tmp_path / "manifest.json"
    m.write_text(json.dumps([
        {"source": "mmd", "year": 2025, "month": 4},  # url omitted -> constructed
        {"source": "et", "year": 2024, "month": 7,
         "url": "https://www.bb.org.bd/pub/monthly/econtrds/etjuly24.pdf"},
    ]))
    entries = b.load_manifest(m)
    assert entries[0].url.endswith("apr2025.pdf")
    assert entries[1].url.endswith("etjuly24.pdf")
