"""Tests for granular Opus-reject quarantine in aggregate_latest.py."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def skip_supabase(monkeypatch):
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "1")
    yield


class TestQuarantineFlagged:
    def _history(self):
        # newest-last list of archived `.data` dicts (matches load_history shape)
        return [
            {"data": {"nbr_fytd_collected_cr": 287862.59, "usd_bdt_mid": 121.0}},
            {"data": {"nbr_fytd_collected_cr": 287862.59, "usd_bdt_mid": 121.5}},
        ]

    def test_quarantines_mappable_flagged_field_from_history(self):
        from aggregate_latest import _quarantine_flagged
        data = {"nbr_fytd_collected_cr": 33522.0, "usd_bdt_mid": 121.6}
        cleaned, quarantined, hard_reject = _quarantine_flagged(
            data, ["nbr_fytd_collected_cr"], self._history()
        )
        assert hard_reject is False
        assert "nbr_fytd_collected_cr" in quarantined
        assert cleaned["nbr_fytd_collected_cr"] == 287862.59  # last-good from history
        assert cleaned["usd_bdt_mid"] == 121.6                 # untouched

    def test_unmappable_field_forces_hard_reject(self):
        from aggregate_latest import _quarantine_flagged
        data = {"nbr_fytd_collected_cr": 33522.0}
        cleaned, quarantined, hard_reject = _quarantine_flagged(
            data, ["totally_unknown_metric"], self._history()
        )
        assert hard_reject is True

    def test_too_many_flagged_forces_hard_reject(self):
        from aggregate_latest import _quarantine_flagged
        data = {f"m{i}": float(i) for i in range(10)}
        flagged = [f"m{i}" for i in range(6)]  # > MAX_QUARANTINE_FIELDS (5)
        _, _, hard_reject = _quarantine_flagged(data, flagged, [{"data": data}])
        assert hard_reject is True

    def test_no_history_value_drops_the_field(self):
        from aggregate_latest import _quarantine_flagged
        data = {"nbr_fytd_collected_cr": 33522.0, "usd_bdt_mid": 121.6}
        cleaned, quarantined, hard_reject = _quarantine_flagged(
            data, ["nbr_fytd_collected_cr"], [{"data": {"usd_bdt_mid": 121.0}}]
        )
        assert hard_reject is False
        assert "nbr_fytd_collected_cr" not in cleaned  # dropped, no last-good
