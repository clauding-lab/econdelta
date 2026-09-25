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


def test_bpm6_definition_is_identical_across_all_three_writers():
    from aggregate_latest import _reserves_monthly_definitions
    from scripts.seed_macro_monthly import build_definitions_rows
    from scripts.seed_reserves_monthly_bpm6 import build_definition_rows

    key = "net_reserves_bpm6_usd_bn_monthly"
    live = next(d for d in _reserves_monthly_definitions() if d["metric_id"] == key)
    history_seed = next(d for d in build_definition_rows() if d["metric_id"] == key)
    macro_seed = next(d for d in build_definitions_rows() if d["metric_id"] == key)

    assert live["metric_id"] == key
    assert live["display_name"] == history_seed["display_name"] == macro_seed["display_name"]
    assert live["description"] == history_seed["description"] == macro_seed["description"]
    assert "gross" in live["display_name"].lower()
    assert "bpm6" in live["display_name"].lower()
    assert "gross" in live["description"].lower()
    assert "bpm6" in live["description"].lower()


def test_gdp_growth_and_imf_history_have_explicit_definitions():
    import json
    from pathlib import Path

    from aggregate_latest import _build_definition_seeds

    config = json.loads((Path(__file__).resolve().parents[1] / "config/sources-v3.json").read_text())
    seeds = {row["metric_id"]: row for row in _build_definition_seeds(config)}
    assert "gdp" not in {indicator["id"] for indicator in config["indicators"]}
    assert seeds["gdp_growth_fy_pct"]["unit"] == "percent"
    assert seeds["gdp_growth_fy_pct"]["cadence"] == "fiscal_year"
    assert seeds["gdp_growth_fy_pct"]["description"]
    assert seeds["imf_general_govt_debt_pct_gdp"]["unit"] == "percent"
    assert seeds["imf_general_govt_debt_pct_gdp"]["cadence"] == "fiscal_year"
    assert "IMF" in seeds["imf_general_govt_debt_pct_gdp"]["source"]
    assert "estimate" in seeds["imf_general_govt_debt_pct_gdp"]["description"].lower()
    mof = seeds["debt_gdp_ratio"]
    assert "mof.gov.bd" in mof["source_url"]
    assert "MoF Debt Bulletin" in mof["description"]
    assert "imf_general_govt_debt_pct_gdp" in mof["description"]
