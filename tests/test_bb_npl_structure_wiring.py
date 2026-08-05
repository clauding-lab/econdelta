"""tests/test_bb_npl_structure_wiring.py"""
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_every_metric_resolves_fiscal_year_in_sentinel():
    from scrapers.bb_npl_structure import METRIC_SPECS
    from sentinel.cadence import load_cadence_map, resolve_cadence
    cmap = load_cadence_map()
    for mid in METRIC_SPECS:
        assert resolve_cadence(mid, cmap) == "fiscal_year", mid


def test_every_metric_is_accepted_stale():
    # Owner decision: structural source lag (annual FSR ~6mo lag; press-only
    # families with no schedule) → tracked, never paged.
    from scrapers.bb_npl_structure import METRIC_SPECS
    from sentinel.freshness import ACCEPTED_STALE_METRIC_IDS
    assert set(METRIC_SPECS) <= ACCEPTED_STALE_METRIC_IDS


def test_no_metric_ever_gates_the_briefing():
    from briefing.config import CORE_METRIC_IDS
    from scrapers.bb_npl_structure import METRIC_SPECS
    assert not (set(METRIC_SPECS) & CORE_METRIC_IDS)


def test_no_metric_in_sources_v3():
    from scrapers.bb_npl_structure import METRIC_SPECS
    cfg = json.loads((REPO_ROOT / "config" / "sources-v3.json").read_text())
    assert not (set(METRIC_SPECS) & {i["id"] for i in cfg["indicators"]})


def test_catalog_lists_every_metric():
    from scrapers.bb_npl_structure import METRIC_SPECS
    catalog = (REPO_ROOT / "docs" / "indicator-catalog.md").read_text()
    for mid in METRIC_SPECS:
        assert f"`{mid}`" in catalog, mid


def test_catalog_regeneration_is_a_noop():
    # Drift guard: docs/indicator-catalog.md must always be exactly what
    # `scripts/build_catalog.py` produces right now — a stale committed copy
    # would silently disagree with DERIVED_KEYS/METRIC_SPECS.
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "build_catalog.py")],
        capture_output=True, text=True, cwd=REPO_ROOT, check=True,
    )
    committed = (REPO_ROOT / "docs" / "indicator-catalog.md").read_text()
    assert result.stdout == committed


def test_derived_keys_match_metric_specs_for_every_npl_id():
    from scrapers.bb_npl_structure import METRIC_SPECS
    from scripts.build_catalog import DERIVED_KEYS
    by_id = {mid: (unit, cadence, desc) for mid, unit, cadence, desc in DERIVED_KEYS}
    assert set(METRIC_SPECS) <= set(by_id)
    for mid, spec in METRIC_SPECS.items():
        unit, cadence, desc = by_id[mid]
        assert unit == spec.unit, mid
        assert desc.startswith(spec.label), mid
        assert cadence == "fiscal_year", mid
