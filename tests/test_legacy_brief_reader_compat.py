"""R2 G2: the repaired producer's latest.json stays readable by the Brief that production runs.

"Old consumer + new producer": EconDelta self-deploys at the 01:00 BDT gitpull, so its new
snapshot can reach Hetzner (five-minute rsync) while The Brief still runs the pre-repair
reader -- the-brief origin/main a810fcb, whose `brief/econdelta.py::load_snapshot` is copied
verbatim below. That reader uses only `updated_at`, `sources_status` and the flat `data`
mapping; builders print `data[<id>]`. Every repair addition (observations, write_status and
its legs, the CPI/M2 upstream-poll entries) must therefore be purely additive:

* the old reader still parses the file, and its per-source helpers work on every entry,
  the new upstream-poll entries included;
* every accepted (verified/held) observation is projected into the flat `data` value the
  old reader prints.

What the old reader does NOT get from the repair (pinned below, so a change is deliberate):

* an observation the repaired producer marks `unavailable` (e.g. no source period) keeps
  its number in the flat `data` -- E1 retains existing flat data, so the old reader prints
  it exactly as it does today; only the new reader honours the quality;
* on a review-quarantine night while the archive holds only pre-repair nights (the first
  night(s) after EconDelta deploys), the rejected field and its aliases are DROPPED from
  `data` -- a pre-repair night has no dated evidence to hold -- so the old reader shows no
  value where the pre-repair producer substituted the last archived one. Once one repaired
  night is archived, the dated value is held and printed again.

The same reader is what a Brief rollback would run. No database, model or Discord call.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import aggregate_latest as agg
from tests.test_aggregator import _build_data_tree


# --- the-brief a810fcb brief/econdelta.py, verbatim logic (the deployed reader) ---------
def _deployed_brief_load(payload: dict[str, Any]) -> tuple[datetime, dict, dict]:
    updated_at = datetime.fromisoformat(payload["updated_at"].replace("Z", "+00:00"))
    return updated_at, payload.get("sources_status", {}), payload.get("data", {})


def _deployed_source_status(sources_status: dict, source_id: str) -> str | None:
    s = sources_status.get(source_id)
    return s.get("status") if s else None


def _deployed_source_age_hours(sources_status: dict, source_id: str) -> float | None:
    s = sources_status.get(source_id)
    if not s:
        return None
    v = s.get("age_hours")
    return float(v) if v is not None else None
# ---------------------------------------------------------------------------------------


def _run_repaired_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    extra_snapshots: dict[str, dict[str, Any]] | None = None,
    archived_night: dict[str, Any] | None = None,
    rejected_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The real aggregate main() of this tree, over the end-to-end test data tree.

    ``archived_night`` is placed in the archive as yesterday's (UTC) latest.json and
    ``rejected_ids`` makes a FAKED reviewer reject exactly those fields (no model call).
    """
    data_dir, cfg_path = _build_data_tree(tmp_path)
    for metric_id, snapshot in (extra_snapshots or {}).items():
        (data_dir / metric_id).mkdir(parents=True, exist_ok=True)
        (data_dir / metric_id / "2026-04-20.json").write_text(json.dumps(snapshot))
    latest_path = data_dir / "latest.json"
    archive_dir = data_dir / "archive"
    monkeypatch.setattr(agg, "DATA_DIR", data_dir)
    monkeypatch.setattr(agg, "LATEST_PATH", latest_path)
    monkeypatch.setattr(agg, "ARCHIVE_DIR", archive_dir)
    monkeypatch.setattr(agg, "CONFIG_PATH", cfg_path)
    for name in ("STALENESS_STATE_PATH", "WATCHLIST_STALENESS_STATE_PATH", "STALE_FALLBACK_ALERT_STATE_PATH"):
        monkeypatch.setattr(agg, name, tmp_path / f"{name.lower()}.json")
    monkeypatch.setattr(agg, "notify", lambda *a, **k: None)
    monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
    if archived_night is not None:
        archive_dir.mkdir(parents=True, exist_ok=True)
        yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        (archive_dir / f"latest_{yesterday}.json").write_text(json.dumps(archived_night))
    if rejected_ids:
        monkeypatch.setenv("ECONDELTA_SKIP_OPUS_REVIEW", "0")
        monkeypatch.setattr(agg, "review_data", lambda *a, **k: {
            "status": "reject", "reason": "SYNTHETIC fake reviewer verdict", "missing": [],
            "anomalies": [{"indicator": metric_id} for metric_id in rejected_ids]})
    assert agg.main() == 0
    return json.loads(latest_path.read_text())


@pytest.fixture
def repaired_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The real aggregate main() of this tree, over the end-to-end test data tree."""
    return _run_repaired_main(tmp_path, monkeypatch)


def test_the_repaired_snapshot_carries_every_additive_block(repaired_snapshot):
    # Guards the test itself: the old reader below is fed the additions, not a v1 file.
    assert repaired_snapshot["observations"]
    assert set(repaired_snapshot["write_status"]) >= {"daily", "monthly"}
    assert {"cpi_upstream_poll", "m2_upstream_poll"} <= set(repaired_snapshot["sources_status"])


def test_the_deployed_brief_reader_still_parses_the_repaired_snapshot(repaired_snapshot):
    updated_at, sources_status, data = _deployed_brief_load(repaired_snapshot)
    assert updated_at.utcoffset() is not None  # the old reader dates the capture, never naive
    assert isinstance(data, dict) and data
    for source_id in sources_status:  # the new upstream-poll entries included
        assert isinstance(_deployed_source_status(sources_status, source_id), str)
        _deployed_source_age_hours(sources_status, source_id)  # float or None; never raises


def test_every_accepted_observation_is_the_flat_value_the_deployed_brief_prints(repaired_snapshot):
    _, _, data = _deployed_brief_load(repaired_snapshot)
    accepted = {mid: item for mid, item in repaired_snapshot["observations"].items()
                if item["quality"] in ("verified", "held")}
    assert accepted
    assert {mid: data.get(mid) for mid in accepted} == {mid: item["value"] for mid, item in accepted.items()}


# --- quality and quarantine paths (review round 1) -------------------------------------
# SYNTHETIC values throughout: 11.08 and 121.5 are labelled test numbers, not source data.
_UNDATED_REGISTRY_READING = {  # a registry reading that carries no source period
    "indicator_id": "banking_sector_crar", "name": "Banking Sector CAR (Capital Adequacy Ratio)",
    "domain": "money_market", "cadence": "quarterly", "scraped_at": datetime.now(timezone.utc).isoformat(),
    "source_url": "https://example.test/fsr", "value": 11.08, "value_type": "percent",
    "_provenance": "deterministic", "evidence": "SYNTHETIC undated reading",
}
# A night archived by the pre-repair producer (origin/main 6676887): the seven legacy
# top-level keys only -- no `observations`, and USD/BDT carries no per-id source date.
_PRE_REPAIR_NIGHT = {
    "schema_version": "3.0", "updated_at": "2026-04-19T20:56:00Z", "alerts": [], "freshness": {},
    "domains": {}, "sources_status": {"bb_forex": {"status": "ok", "age_hours": 1.0}},
    "data": {"usd_bdt_mid": 121.5, "usd_bdt_exchange_rate": 121.5},
}


def _repaired_night(archived_as_of: str) -> dict[str, Any]:
    """The same night archived by the repaired producer: the value travels with its date."""
    return {**_PRE_REPAIR_NIGHT, "observations": {"usd_bdt_mid": {
        "metric_id": "usd_bdt_mid", "value": 121.5, "as_of": archived_as_of, "unit": "BDT",
        "source": "bb_forex", "source_url": "https://example.test/forex",
        "captured_at": f"{archived_as_of}T12:00:00+00:00", "quality": "verified", "date_basis": "observation",
        "evidence": "SYNTHETIC archived bb_forex usd_bdt_mid", "dependencies": [], "release_status": "unknown"}}}


def test_an_unavailable_observation_keeps_its_flat_number_so_the_deployed_brief_still_prints_it(
        tmp_path, monkeypatch):
    # Known gap, not a regression: the pre-repair producer printed the same undated scalar.
    snapshot = _run_repaired_main(tmp_path, monkeypatch,
                                  extra_snapshots={"banking_sector_crar": _UNDATED_REGISTRY_READING})
    observation = snapshot["observations"]["banking_sector_crar"]
    assert observation["quality"] == "unavailable" and observation["as_of"] is None
    _, _, data = _deployed_brief_load(snapshot)
    assert data["banking_sector_crar"] == observation["value"] == 11.08


def test_a_quarantine_night_on_pre_repair_archives_drops_the_field_the_deployed_brief_reads(
        tmp_path, monkeypatch):
    snapshot = _run_repaired_main(tmp_path, monkeypatch, archived_night=_PRE_REPAIR_NIGHT,
                                  rejected_ids=("usd_bdt_mid",))
    _, _, data = _deployed_brief_load(snapshot)
    # Neither today's rejected 122.7 nor the undated archived 121.5: the old reader gets None
    # for the whole family (buy/sell are separate, unrejected fields and stay).
    assert data.get("usd_bdt_mid") is None and data.get("usd_bdt_exchange_rate") is None
    assert "usd_bdt_mid" not in snapshot["observations"]
    assert [mid for mid, value in data.items() if value == 121.5] == []


def test_a_quarantine_night_after_one_repaired_night_holds_the_dated_value_for_the_deployed_brief(
        tmp_path, monkeypatch):
    archived_as_of = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    snapshot = _run_repaired_main(tmp_path, monkeypatch, archived_night=_repaired_night(archived_as_of),
                                  rejected_ids=("usd_bdt_mid",))
    held = snapshot["observations"]["usd_bdt_mid"]
    assert (held["value"], held["as_of"], held["quality"]) == (121.5, archived_as_of, "held")
    _, _, data = _deployed_brief_load(snapshot)
    assert data["usd_bdt_mid"] == 121.5
