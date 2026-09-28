"""A successful aggregate cannot disguise stopped upstream CPI/M2 source jobs (R2 fix 5).

The CPI and M2 monthly legs derive from EconDelta's own daily table. Their database
reread proves the database answered, not that the Bangladesh Bank CPI/M2 jobs polled
anything (E6 monitoring boundary). The E6 source-poll receipts
(utils/monthly_evidence.source_monitor) are the only liveness evidence, and latest.json's
sources_status must carry them to The Brief under the shared contract's keys.

Limits: the integration run fakes every network and database edge (the in-memory
metric_history_monthly of test_monthly_leg_receipts, a confirmed daily readback) and no
real upstream job runs. It proves how the producer reports liveness; it is no evidence
that any real upstream CPI/M2 source is being polled.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import aggregate_latest as agg
import utils.monthly_evidence as evidence
from tests.test_aggregator import _build_data_tree
from tests.test_monthly_leg_receipts import _daily_scrapers_stuck_at_july, world  # noqa: F401
from utils import supabase_writer as sw

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = json.loads((ROOT / "tests/fixtures/contracts/brief-observations-v1.json")
                      .read_text())["upstream_liveness_contract"]
FAMILIES = CONTRACT["families"]
NOW = datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc)


def _receipt(directory: Path, mid: str, **fields) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{mid}.json").write_text(json.dumps(fields))


def _upstream_receipt(directory: Path, mid: str, checked_at: datetime) -> None:
    _receipt(directory, mid, evidence_kind="upstream-source", checked_at=checked_at.isoformat(),
             status="observed-older-period", latest_source_period="2026-07-31")


def _find_leg(receipt: dict, name: str) -> dict | None:
    legs = receipt.get("legs") or {}
    if name in legs:
        return legs[name]
    return next((found for child in legs.values() if (found := _find_leg(child, name))), None)


def _run_main_with_stopped_upstream_jobs(tmp_path: Path, monkeypatch) -> dict:
    """aggregate_latest.main: daily write confirmed, CPI/M2 database reread succeeds, and the
    upstream CPI/M2 jobs last polled on 1 Aug (their typed receipts sit on disk)."""
    data_dir, cfg_path = _build_data_tree(tmp_path)
    for name in ("DATA_DIR", "LATEST_PATH", "ARCHIVE_DIR", "STALENESS_STATE_PATH",
                 "WATCHLIST_STALENESS_STATE_PATH", "STALE_FALLBACK_ALERT_STATE_PATH"):
        target = {"DATA_DIR": data_dir, "LATEST_PATH": data_dir / "latest.json",
                  "ARCHIVE_DIR": data_dir / "archive"}.get(name, tmp_path / "state" / f"{name}.json")
        monkeypatch.setattr(agg, name, target)
    monkeypatch.setattr(agg, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
    monkeypatch.setattr(agg, "_derive_daily_yields_from_auctions", lambda **kw: ({}, {}))
    monkeypatch.setattr(agg, "_apply_media_overrides", lambda *a, **kw: {"written": [], "failures": []})
    monkeypatch.setattr(agg, "_write_reserves_monthly_split", lambda *a: 0)
    monkeypatch.setattr(sw, "upsert_metric_definitions_seed", lambda *a, **kw: 0)
    monkeypatch.setattr(sw, "upsert_metric_history", lambda **kw: 1)
    monkeypatch.setattr(sw, "verify_landed_count", lambda *a, **kw: True)
    for family in FAMILIES.values():
        for mid in family["metric_ids"]:
            _upstream_receipt(evidence.DEFAULT_DIRECTORY, mid, datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert agg.main() == 0
    return json.loads((data_dir / "latest.json").read_text())


def test_successful_aggregate_and_database_reread_still_report_stopped_cpi_m2_jobs(
        world, tmp_path, monkeypatch):  # noqa: F811
    table, sent = world
    _daily_scrapers_stuck_at_july(table, monkeypatch)

    payload = _run_main_with_stopped_upstream_jobs(tmp_path, monkeypatch)

    # The aggregate and its database work succeeded: nothing here is a failure.
    assert payload["write_status"]["daily"]["status"] == "ok"
    # No approved press override needed writing: the override stage is a clean skip, and its
    # operator alert (owner decision k) stays silent.
    assert payload["write_status"]["media_overrides"]["status"] == "skipped"
    assert payload["write_status"]["media_overrides"]["reason"] == "no active approved override needed writing"
    assert not [call for call in sent if agg.MEDIA_OVERRIDE_ALERT_TITLE in call]
    for family in FAMILIES.values():
        leg = _find_leg(payload["write_status"]["monthly"], family["monthly_leg"])
        assert leg is not None and leg["status"] == "skipped", family["monthly_leg"]
        assert {s["category"] for s in leg["skips"]} == {"no newer database vintage"}
        for mid in family["metric_ids"]:  # this run's reread replaced the old poll receipts
            receipt = json.loads((evidence.DEFAULT_DIRECTORY / f"{mid}.json").read_text())
            assert receipt["evidence_kind"] == "database-observations", mid
    # ...yet the snapshot tells The Brief the upstream CPI/M2 jobs are not proven alive.
    expected = CONTRACT["stopped_upstream_jobs"]["sources_status"]
    assert {key: payload["sources_status"].get(key) for key in expected} == expected
    assert all(entry["status"] != "ok" for entry in expected.values())
    # The scraper sources are reported exactly as before, and no new alert names CPI/M2.
    assert {"bb_forex", "dse_market", "commodity_prices"} <= set(payload["sources_status"])
    assert not [call for call in sent if any(key in str(call) for key in FAMILIES)]


def test_contract_families_are_the_producers_families():
    assert {key: tuple(f["metric_ids"]) for key, f in FAMILIES.items()} == evidence.UPSTREAM_FAMILIES
    assert set(FAMILIES) == set(CONTRACT["stopped_upstream_jobs"]["sources_status"])


def test_every_family_metric_polled_within_the_job_window_is_ok(tmp_path):
    for family in FAMILIES.values():
        for mid in family["metric_ids"]:
            _upstream_receipt(tmp_path, mid, NOW - timedelta(hours=25))
    status = evidence.upstream_poll_status(evidence.source_monitor(directory=tmp_path, now=NOW))
    assert status == CONTRACT["polled_upstream_jobs"]["sources_status"]


def test_one_upstream_poll_older_than_the_job_window_makes_its_family_stale(tmp_path):
    for family in FAMILIES.values():
        for mid in family["metric_ids"]:
            _upstream_receipt(tmp_path, mid, NOW - timedelta(hours=1))
    _upstream_receipt(tmp_path, "cpi_p2p_food_monthly", NOW - timedelta(hours=27))
    status = evidence.upstream_poll_status(evidence.source_monitor(directory=tmp_path, now=NOW))
    assert status["m2_upstream_poll"]["status"] == "ok"
    assert status["cpi_upstream_poll"]["status"] == "stale"
    assert status["cpi_upstream_poll"]["error"] == (
        "CPI upstream source-poll liveness not established: last upstream poll older than "
        "the 26-hour job-check window (cpi_p2p_food_monthly)")


@pytest.mark.parametrize("write, phrase", [
    (None, "no readable source-poll receipt"),
    (lambda d, mid: _receipt(d, mid, checked_at=NOW.isoformat(), status="release-lag",
                             latest_source_period="2026-08-31"),
     "untyped receipt, not proof of an upstream poll"),
    (lambda d, mid: _receipt(d, mid, evidence_kind="database-observations", checked_at=NOW.isoformat(),
                             status="unknown", latest_database_period="2026-08-31"),
     "database reread only, not an upstream source poll"),
], ids=["no-receipt", "legacy-untyped", "database-reread"])
def test_no_upstream_poll_evidence_is_missing_never_ok(tmp_path, write, phrase):
    for family in FAMILIES.values():
        for mid in family["metric_ids"]:
            if write:
                write(tmp_path, mid)
    status = evidence.upstream_poll_status(evidence.source_monitor(directory=tmp_path, now=NOW))
    assert status["m2_upstream_poll"] == {
        "status": "missing", "last_success": None, "age_hours": None, "url": None,
        "error": f"M2 upstream source-poll liveness not established: {phrase} (m2_growth_yoy_monthly)"}
    assert status["cpi_upstream_poll"]["status"] == "missing"


def test_a_missing_poll_outranks_a_stale_one_and_both_are_named(tmp_path):
    _upstream_receipt(tmp_path, "cpi_12m_avg_monthly", NOW - timedelta(days=3))
    _upstream_receipt(tmp_path, "cpi_p2p_food_monthly", NOW)
    status = evidence.upstream_poll_status(evidence.source_monitor(directory=tmp_path, now=NOW))
    assert status["cpi_upstream_poll"]["status"] == "missing"
    assert status["cpi_upstream_poll"]["error"] == (
        "CPI upstream source-poll liveness not established: last upstream poll older than the "
        "26-hour job-check window (cpi_12m_avg_monthly); no readable source-poll receipt "
        "(cpi_p2p_nonfood_monthly)")


@pytest.mark.parametrize("checked_at", [1727222160, None, ["2026-09-25"]], ids=["number", "null", "list"])
def test_a_corrupt_upstream_receipt_is_no_readable_poll_and_cannot_stop_the_snapshot(tmp_path, checked_at):
    """main() now reads these receipts before writing latest.json: a damaged local file must
    read as missing liveness, never raise and cost the day's snapshot."""
    _receipt(tmp_path, "m2_growth_yoy_monthly", evidence_kind="upstream-source", checked_at=checked_at,
             status="observed-older-period", latest_source_period="2026-07-31")
    status = evidence.upstream_poll_status(evidence.source_monitor(directory=tmp_path, now=NOW))
    assert status["m2_upstream_poll"]["status"] == "missing"
    assert status["m2_upstream_poll"]["error"].endswith("no readable source-poll receipt (m2_growth_yoy_monthly)")
