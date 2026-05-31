"""Tests for definition seeding logic in aggregate_latest.py."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def skip_supabase(monkeypatch):
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "1")
    yield


class TestBuildDefinitionSeeds:
    def test_maps_v3_indicator_to_definition_row(self):
        from aggregate_latest import _build_definition_seeds
        sources_v3 = {
            "indicators": [
                {
                    "id": "banking_npl_pct",
                    "domain": "monetary",
                    "label": "Gross NPL Ratio",
                    "unit": "%",
                    "cadence": "quarterly",
                    "fetch": {"type": "pdf", "url": "https://www.bb.org.bd/..."},
                },
            ]
        }
        seeds = _build_definition_seeds(sources_v3)
        # 1 config indicator + the runtime-derived CRR/SLR utilisation seeds (S2).
        by_id = {s["metric_id"]: s for s in seeds}
        assert "banking_npl_pct" in by_id
        d = by_id["banking_npl_pct"]
        assert d["metric_id"] == "banking_npl_pct"
        assert d["label"] == "Gross NPL Ratio"
        assert d["unit"] == "%"
        assert d["domain"] == "monetary"
        assert d["cadence"] == "quarterly"
        assert d["source_url"] == "https://www.bb.org.bd/..."

    def test_falls_back_to_titleized_id_when_label_missing(self):
        from aggregate_latest import _build_definition_seeds
        sources_v3 = {"indicators": [{"id": "test_metric", "domain": "macro", "fetch": {"type": "html"}}]}
        seeds = _build_definition_seeds(sources_v3)
        assert seeds[0]["label"] == "Test Metric"

    def test_handles_missing_optional_fields(self):
        from aggregate_latest import _build_definition_seeds
        sources_v3 = {"indicators": [{"id": "x", "domain": "macro", "fetch": {"type": "html"}}]}
        seeds = _build_definition_seeds(sources_v3)
        assert seeds[0]["unit"] is None
        assert seeds[0]["cadence"] is None
