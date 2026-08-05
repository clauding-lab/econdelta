import json
from pathlib import Path

from media_screen.catalog import load_catalog
from media_screen.types import MetricSpec

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_all_catalog_ids_exist_in_registry():
    """Every catalog metric_id must resolve to a real config/sources-v3.json
    indicator. Regression for tbill_91d_yield_pct (PR #116): that id had NO
    registry entry, so _parsed_for() always returned (None, None) for it, and
    classify() treats a missing parsed_value as an automatic fresher_period
    Candidate -- any press mention of a 91-day T-bill yield became a
    guaranteed auto-candidate for a series EconDelta doesn't track at all.
    test_catalog_only_bb_sourced_metrics (below) only checked that metric_id
    is a non-empty string, which is exactly why this slipped through."""
    registry_ids = {
        i["id"] for i in json.loads((REPO_ROOT / "config" / "sources-v3.json").read_text())["indicators"]
    }
    specs = load_catalog()
    missing = sorted({s.metric_id for s in specs if s.metric_id not in registry_ids})
    assert not missing, (
        f"catalog metric_id(s) with no config/sources-v3.json registry entry: {missing} "
        "-- _parsed_for() will always return (None, None) for these, which "
        "classify() treats as an automatic Candidate on any match."
    )


def test_catalog_includes_npl_with_press_names():
    specs = load_catalog()
    npl = next(s for s in specs if s.metric_id == "gross_npl_ratio")
    assert isinstance(npl, MetricSpec)
    assert any("npl" in n.lower() for n in npl.press_names)
    assert npl.tolerance > 0


def test_catalog_only_bb_sourced_metrics():
    """Every spec maps to a real BB indicator id from the config."""
    specs = load_catalog()
    assert len(specs) >= 5
    assert all(s.metric_id and s.press_names for s in specs)


def test_no_alias_collisions():
    """The screen matches press numbers via {name.lower(): spec} (last-writer-wins),
    so a press alias shared by two specs would silently route a figure to the WRONG
    metric. Every alias must be unique across the whole catalog."""
    specs = load_catalog()
    keys = [n.lower() for s in specs for n in s.press_names]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    assert not dupes, f"duplicate press alias(es) across specs: {dupes}"
