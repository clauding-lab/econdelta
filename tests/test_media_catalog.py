from media_screen.catalog import load_catalog
from media_screen.types import MetricSpec


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
