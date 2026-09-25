"""Tests for granular Opus-reject quarantine in aggregate_latest.py."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from utils.observations import Observation, expand_aliases


@pytest.fixture(autouse=True)
def skip_supabase(monkeypatch):
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "1")
    yield


def _observation(metric_id: str, value: float, as_of: str, *, quality="verified"):
    return Observation(
        metric_id=metric_id,
        value=value,
        as_of=date.fromisoformat(as_of),
        unit="%",
        source="bbs",
        source_url="https://example.test/cpi",
        captured_at=datetime.fromisoformat(as_of).replace(tzinfo=timezone.utc),
        quality=quality,
        date_basis="observation",
        evidence=f"official CPI release {as_of}",
    )


class TestObservationQuarantine:
    @pytest.mark.parametrize(
        ("field", "bad_value", "usable"),
        [
            ("quality", [], False),
            ("date_basis", {}, False),
            ("release_status", ["final"], True),
        ],
    )
    def test_malformed_archived_observation_metadata_is_safe(
        self, field, bad_value, usable
    ):
        from utils.observations import _history_observation

        record = {
            "metric_id": "general_inflation",
            "value": 8.0,
            "as_of": "2026-07-31",
            "unit": "%",
            "source": "BBS",
            "captured_at": "2026-08-25T00:00:00+00:00",
            "quality": "verified",
            "date_basis": "observation",
            "evidence": "July official source observation",
            "release_status": "final",
        }
        record[field] = bad_value

        observation = _history_observation(record, "general_inflation")

        assert (observation is not None) is usable
        if field == "release_status" and observation is not None:
            assert observation.release_status == "unknown"

    def test_rejecting_base_restores_dated_base_and_rebuilds_alias(self):
        from utils.observations import quarantine_observations

        current = expand_aliases(
            {"general_inflation": _observation("general_inflation", 20, "2026-08-31")}
        )
        history = [
            {
                "observations": {
                    "general_inflation": _observation("general_inflation", 8, "2026-07-31")
                }
            }
        ]
        accepted, quarantined, hard_reject = quarantine_observations(
            current, ["general_inflation"], history, breadth_count=1
        )

        assert hard_reject is False
        assert quarantined == ["general_inflation"]
        assert accepted["general_inflation"].value == 8
        assert accepted["general_inflation"].as_of == date(2026, 7, 31)
        assert accepted["general_inflation"].quality == "held"
        assert accepted["macro_cpi_headline"].value == 8
        assert accepted["macro_cpi_headline"].as_of == date(2026, 7, 31)
        assert accepted["macro_cpi_headline"].dependencies == ("general_inflation",)

    def test_missing_dated_predecessor_removes_entire_family(self):
        from utils.observations import quarantine_observations

        current = expand_aliases(
            {"general_inflation": _observation("general_inflation", 20, "2026-08-31")}
        )
        accepted, _, hard_reject = quarantine_observations(
            current, ["general_inflation"], [{"data": {"general_inflation": 8}}], breadth_count=1
        )

        assert hard_reject is False
        assert "general_inflation" not in accepted
        assert "macro_cpi_headline" not in accepted

    def test_rejecting_derived_alias_uses_historical_dependency(self):
        from utils.observations import quarantine_observations

        current = expand_aliases(
            {"general_inflation": _observation("general_inflation", 20, "2026-08-31")}
        )
        history = [
            {
                "observations": {
                    "general_inflation": _observation("general_inflation", 8, "2026-07-31")
                }
            }
        ]
        accepted, _, hard_reject = quarantine_observations(
            current, ["macro_cpi_headline"], history, breadth_count=1
        )

        assert hard_reject is False
        assert accepted["general_inflation"].value == 8
        assert accepted["macro_cpi_headline"].value == 8
        assert accepted["macro_cpi_headline"].as_of == date(2026, 7, 31)

    def test_unknown_id_and_breadth_remain_hard_holds(self):
        from utils.observations import quarantine_observations

        current = expand_aliases(
            {"general_inflation": _observation("general_inflation", 20, "2026-08-31")}
        )
        _, _, hard_reject = quarantine_observations(current, ["unknown"], [], breadth_count=1)
        assert hard_reject is True
        _, _, hard_reject = quarantine_observations(
            current, ["general_inflation"], [], breadth_count=6
        )
        assert hard_reject is True


@pytest.mark.parametrize("dated_predecessor", [True, False])
def test_main_quarantines_rejected_value_from_latest_domains_and_observations(
    tmp_path, monkeypatch, dated_predecessor
):
    """The published bundle must never retain the rejected current value."""
    import aggregate_latest as agg

    config = tmp_path / "config"
    data_dir = tmp_path / "data"
    archive_dir = data_dir / "archive"
    config.mkdir()
    data_dir.mkdir()
    registry = {
        "version": "3.0",
        "indicators": [
            {
                "id": "general_inflation",
                "name": "Inflation",
                "domain": "macro",
                "cadence": "monthly",
                "parse": {"value_type": "percent"},
            }
        ],
    }
    (config / "sources.json").write_text(json.dumps({"sources": {}}))
    registry_path = config / "sources-v3.json"
    registry_path.write_text(json.dumps(registry))
    source_dir = data_dir / "general_inflation"
    source_dir.mkdir()
    captured_at = datetime.now(timezone.utc).isoformat()
    (source_dir / "2026-08-31.json").write_text(
        json.dumps(
            {
                "indicator_id": "general_inflation",
                "name": "Inflation",
                "domain": "macro",
                "cadence": "monthly",
                "scraped_at": captured_at,
                "source_url": "https://example.test/cpi",
                "source_as_of": "2026-08-31",
                "value": 20.0,
                "value_type": "percent",
                "_provenance": "deterministic",
                "evidence": "August source observation",
            }
        )
    )

    archive_dir.mkdir()
    archived_day = (date.today() - timedelta(days=1)).isoformat()
    archive_observations = {}
    archive_data = {"general_inflation": 8.0, "macro_cpi_headline": 8.0}
    archive_domains = {
        "macro": {
            "general_inflation": {
                "value": 8.0,
                "source_as_of": "2026-07-31",
                "source": "BBS",
                "evidence": "July official source observation",
            }
        }
    }
    if dated_predecessor:
        archive_observations["general_inflation"] = {
            "metric_id": "general_inflation",
            "value": 8.0,
            "as_of": "2026-07-31",
            "unit": "%",
            "source": "BBS",
            "source_url": "https://example.test/cpi",
            "captured_at": "2026-08-25T00:00:00+00:00",
            "quality": "verified",
            "date_basis": "observation",
            "evidence": "July official source observation",
            "dependencies": [],
            "release_status": "final",
        }
    else:
        # Old scalar-only data is deliberately insufficient evidence.
        archive_domains["macro"]["general_inflation"]["source_as_of"] = None
    (archive_dir / f"latest_{archived_day}.json").write_text(
        json.dumps(
            {
                "updated_at": "2026-08-25T00:00:00+00:00",
                "data": archive_data,
                "domains": archive_domains,
                "observations": archive_observations,
            }
        )
    )
    original_archive_names = {path.name for path in archive_dir.glob("latest_*.json")}

    monkeypatch.setattr(agg, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(agg, "DATA_DIR", data_dir)
    monkeypatch.setattr(agg, "LATEST_PATH", data_dir / "latest.json")
    monkeypatch.setattr(agg, "ARCHIVE_DIR", archive_dir)
    monkeypatch.setattr(agg, "CONFIG_PATH", config / "sources.json")
    monkeypatch.setattr(agg, "SOURCES_V3_PATH", registry_path)
    monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "1")
    monkeypatch.setenv("ECONDELTA_SKIP_OPUS_REVIEW", "0")
    monkeypatch.setattr(
        agg,
        "review_data",
        lambda *a, **kw: {
            "status": "reject",
            "reason": "implausible CPI jump",
            "missing": [],
            "anomalies": [{"indicator": "general_inflation"}],
        },
    )

    assert agg.main() == 0
    published = json.loads((data_dir / "latest.json").read_text())
    new_archives = [
        path for path in archive_dir.glob("latest_*.json")
        if path.name not in original_archive_names
    ]
    assert len(new_archives) == 1
    archived = json.loads(new_archives[0].read_text())

    if dated_predecessor:
        assert published["data"]["general_inflation"] == 8.0
        assert published["data"]["macro_cpi_headline"] == 8.0
        assert published["observations"]["general_inflation"]["as_of"] == "2026-07-31"
        assert published["observations"]["general_inflation"]["quality"] == "held"
        assert published["domains"]["macro"]["general_inflation"]["value"] == 8.0
        assert published["domains"]["macro"]["general_inflation"]["source_as_of"] == "2026-07-31"
    else:
        assert "general_inflation" not in published["data"]
        assert "macro_cpi_headline" not in published["data"]
        assert "general_inflation" not in published["observations"]
        assert "general_inflation" not in published["domains"].get("macro", {})
    assert 20.0 not in published["data"].values()
    assert all(
        entry.get("value") != 20.0
        for domain in published["domains"].values()
        for entry in domain.values()
    )
    assert all(entry.get("value") != 20.0 for entry in published["observations"].values())
    assert 20.0 not in archived["data"].values()
    assert all(
        entry.get("value") != 20.0
        for domain in archived["domains"].values()
        for entry in domain.values()
    )
    assert all(entry.get("value") != 20.0 for entry in archived["observations"].values())
