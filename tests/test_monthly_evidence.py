"""Fresh receipt time cannot turn an old source period into current data."""
import json
from datetime import date, datetime, timezone

import pytest

from sentinel.freshness import assess


def test_yield_freshness_uses_underlying_evidence_not_current_grouping_key():
    report = assess(rows_daily=[], rows_monthly=[{
        "metric_id": "yield_20y_monthly", "as_of": "2026-09-01",
        "source_as_of": "2026-05-20", "ingested_at": "2026-09-25T00:00:00Z"}],
        cadence_map={}, today=date(2026, 9, 25))
    assert [m.metric_id for m in report.breaches] == ["yield_20y_monthly"]
    assert report.breaches[0].latest_as_of == date(2026, 5, 20)


def test_monthly_source_date_is_independent_of_collection_time():
    report = assess(rows_daily=[], rows_monthly=[{
        "metric_id": "exports_usd_mn_monthly", "as_of": "2026-08-01",
        "source_as_of": "2026-08-31", "ingested_at": "2026-09-25T00:00:00Z"}],
        cadence_map={}, today=date(2026, 9, 25))
    assert [m.metric_id for m in report.fresh] == ["exports_usd_mn_monthly"]
    assert report.fresh[0].latest_as_of == date(2026, 8, 31)


def test_observed_old_release_and_poll_liveness_are_independent(tmp_path):
    from utils.monthly_evidence import source_monitor
    receipt = {"evidence_kind": "upstream-source",
               "checked_at": "2026-09-25T00:00:00Z", "status": "observed-older-period",
               "latest_source_period": "2026-06-30"}
    (tmp_path / "imports_usd_mn_monthly.json").write_text(json.dumps(receipt))
    rows = source_monitor(directory=tmp_path, now=datetime(2026, 9, 25, tzinfo=timezone.utc))
    assert rows["imports_usd_mn_monthly"]["source_status"] == "observed-older-period"
    assert rows["imports_usd_mn_monthly"]["job_status"] == "checked"
    rows = source_monitor(directory=tmp_path, now=datetime(2026, 9, 30, tzinfo=timezone.utc))
    assert rows["imports_usd_mn_monthly"]["source_status"] == "observed-older-period"
    assert rows["imports_usd_mn_monthly"]["job_status"] == "not-recently-checked"
    assert rows["reer_monthly"]["source_status"] == "unsupported"
    assert rows["non_nbr_tax_revenue"]["source_status"] == "owner-blocked"
    assert rows["non_tax_revenue"]["source_status"] == "owner-blocked"


def test_revision_diff_keeps_old_month_and_metric_separate():
    from utils.monthly_evidence import revision_diff
    candidates = [{"metric_id": "imports_usd_mn_monthly", "as_of": "2026-05-01", "value": 10}]
    old = [{"metric_id": "imports_usd_mn_monthly", "as_of": "2026-05-01", "value": 9}]
    assert revision_diff(candidates, old)[0]["stored_value"] == 9
    assert old[0]["value"] == 9
    assert revision_diff(candidates, [{**old[0], "metric_id": "exports_usd_mn_monthly"}]) == []


def test_macro_appender_emits_revision_without_overwriting_accepted_cpi(monkeypatch, tmp_path):
    import aggregate_latest as agg
    import utils.monthly_evidence as evidence
    import utils.supabase_reader as reader
    import utils.supabase_writer as writer

    monkeypatch.setattr(evidence, "DEFAULT_DIRECTORY", tmp_path)
    monkeypatch.setattr(agg, "notify", lambda *args: None)
    def history(mid, **kwargs):
        value = {"general_inflation": 8.70, "point_to_point_inflation": 8.26,
                 "food_inflation": 7.5, "non_food_inflation": 9.3, "m2_growth_yoy_pct": 11.7}[mid]
        return [{"metric_id": mid, "as_of": "2026-08-31", "value": value}]
    def monthly(mid):
        return [{"metric_id": mid, "as_of": "2026-08-01", "value": 8.66, "source": "accepted"}]
    monkeypatch.setattr(reader, "get_metric_history", history)
    monkeypatch.setattr(reader, "get_metric_history_monthly", monthly)
    monkeypatch.setattr(writer, "upsert_metric_history_monthly", lambda rows: pytest.fail("accepted months must not be rewritten"))
    assert agg._write_macro_monthly_append(date(2026, 9, 25)) == 0
    report = json.loads((tmp_path / "cpi_12m_avg_monthly.json").read_text())
    assert report["revisions"][0]["stored_value"] == 8.66
    assert report["revisions"][0]["source_value"] == 8.70


def test_successful_aggregate_database_reread_cannot_refresh_stale_source_job(monkeypatch, tmp_path):
    """Repeated accepted CPI/M2 rows do not prove their stopped upstream job ran."""
    import aggregate_latest as agg
    import utils.monthly_evidence as evidence
    import utils.supabase_reader as reader
    import utils.supabase_writer as writer

    monkeypatch.setattr(evidence, "DEFAULT_DIRECTORY", tmp_path)
    monkeypatch.setattr(agg, "notify", lambda *args: None)
    values = {"general_inflation": 8.7, "point_to_point_inflation": 8.26,
              "food_inflation": 7.5, "non_food_inflation": 9.3, "m2_growth_yoy_pct": 11.7}
    accepted = {"cpi_12m_avg_monthly": 8.7, "cpi_p2p_food_monthly": 7.5,
                "cpi_p2p_nonfood_monthly": 9.3, "m2_growth_yoy_monthly": 11.7}
    for mid in accepted:
        (tmp_path / f"{mid}.json").write_text(json.dumps({
            "evidence_kind": "upstream-source", "checked_at": "2026-08-02T00:00:00Z",
            "latest_source_period": "2026-07-31", "status": "observed-older-period"}))
    monkeypatch.setattr(reader, "get_metric_history", lambda mid, **kw: [
        {"metric_id": mid, "as_of": "2026-07-31", "value": values[mid]}])
    monkeypatch.setattr(reader, "get_metric_history_monthly", lambda mid: [
        {"metric_id": mid, "as_of": "2026-07-01" if mid in accepted else "2026-08-01",
         "value": accepted.get(mid, 1.0), "source": "accepted"}])
    monkeypatch.setattr(writer, "upsert_metric_history_monthly",
                        lambda rows: pytest.fail("accepted rows must remain unchanged"))
    assert agg._write_macro_monthly_append(date(2026, 9, 25)) == 0
    monitored = evidence.source_monitor(directory=tmp_path)
    for mid in accepted:
        receipt = json.loads((tmp_path / f"{mid}.json").read_text())
        assert receipt["evidence_kind"] == "database-observations"
        assert receipt["latest_database_period"] == "2026-07-31"
        assert receipt["latest_source_period"] is None
        assert monitored[mid]["source_status"] == "unknown"
        assert monitored[mid]["job_status"] == "unknown"


def test_legacy_untyped_receipt_does_not_prove_upstream_liveness(tmp_path):
    from utils.monthly_evidence import source_monitor
    (tmp_path / "m2_growth_yoy_monthly.json").write_text(json.dumps({
        "checked_at": "2026-09-25T00:00:00Z", "status": "release-lag",
        "latest_source_period": "2026-07-31"}))
    result = source_monitor(directory=tmp_path, now=datetime(2026, 9, 25, tzinfo=timezone.utc))
    assert result["m2_growth_yoy_monthly"]["source_status"] == "unknown"
    assert result["m2_growth_yoy_monthly"]["job_status"] == "unknown"


def test_fetched_old_period_does_not_prove_legitimate_publication_lag(tmp_path):
    from utils.monthly_evidence import record_source_check
    receipt = record_source_check(
        "imports_usd_mn_monthly", [(date(2026, 6, 1), 7512.52)], [],
        today=date(2026, 9, 25), revisions=[], source_url="official.pdf",
        evidence_kind="upstream-source", directory=tmp_path)
    assert receipt["status"] == "observed-older-period"
    assert receipt["latest_source_period"] == "2026-06-30"
    assert receipt["latest_database_period"] is None
