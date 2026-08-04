"""tests/test_bb_npl_structure_wiring.py"""
import json
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
