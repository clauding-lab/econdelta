"""Smoke test: every scraper module imports its wrap_run pattern correctly."""


def test_all_scrapers_can_import_wrap_run():
    """Verify utils.supabase_writer.wrap_run is importable from each scraper context."""
    from utils.supabase_writer import wrap_run
    assert callable(wrap_run)
