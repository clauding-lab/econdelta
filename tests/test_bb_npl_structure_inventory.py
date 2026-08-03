"""tests/test_bb_npl_structure_inventory.py"""
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_inventory_has_35_ids_with_valid_shapes():
    from scrapers.bb_npl_structure import METRIC_SPECS
    assert len(METRIC_SPECS) == 35
    for mid, spec in METRIC_SPECS.items():
        assert mid == mid.lower() and " " not in mid
        assert spec.label
        assert spec.unit in ("percent", "amount_bdt_crore")
        assert spec.family in (
            "sector_rate", "sector_share", "sub_rate", "total",
            "band_rate", "band_outstanding", "cmsme",
        )
        assert isinstance(spec.fsr, bool)


def test_fsr_vs_seed_only_split():
    from scrapers.bb_npl_structure import METRIC_SPECS
    fsr = {m for m, s in METRIC_SPECS.items() if s.fsr}
    seed_only = {m for m, s in METRIC_SPECS.items() if not s.fsr}
    assert len(fsr) == 22 and len(seed_only) == 13
    assert "npl_rate_sector_trade_commerce" in fsr
    assert "npl_rate_band_lt1cr" in seed_only
    assert "npl_rate_cmsme_cottage" in seed_only


def test_no_collision_with_sources_v3_ids():
    from scrapers.bb_npl_structure import METRIC_SPECS
    cfg = json.loads((REPO_ROOT / "config" / "sources-v3.json").read_text())
    assert not (set(METRIC_SPECS) & {i["id"] for i in cfg["indicators"]})


def test_extraction_keys_and_required_set():
    from scrapers.bb_npl_structure import (
        FSR_EXTRACTION_KEYS,
        METRIC_SPECS,
        REQUIRED_EXTRACTION_KEYS,
    )
    fsr_ids = {m for m, s in METRIC_SPECS.items() if s.fsr}
    assert set(FSR_EXTRACTION_KEYS) == fsr_ids | {"overall_npl_ratio_fsr"}
    subs = {m for m, s in METRIC_SPECS.items() if s.family == "sub_rate"}
    assert REQUIRED_EXTRACTION_KEYS == (fsr_ids - subs) | {"overall_npl_ratio_fsr"}
    assert len(REQUIRED_EXTRACTION_KEYS) == 19


def test_definitions_rows_cover_all_35():
    from scrapers.bb_npl_structure import METRIC_SPECS, build_definitions_rows
    rows = build_definitions_rows()
    assert len(rows) == 35
    for row in rows:
        spec = METRIC_SPECS[row["metric_id"]]
        assert row["domain"] == "money_market"
        assert row["cadence"] == "fiscal_year"
        assert row["unit"] == spec.unit
        assert row["source"] == ("BB FSR" if spec.fsr else "bb_via_press_static")
        assert row["grace_days"] == 400
