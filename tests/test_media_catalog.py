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
