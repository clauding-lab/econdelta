from datetime import date

from media_screen.types import Extracted
from utils.supabase_writer import SupabaseWriteError


def _ex():
    return Extracted("NPL ratio", 32.26, date(2026, 3, 31), "q", "http://x", "tbs")


def test_run_screen_inserts_filtered_candidates(monkeypatch):
    import scrapers.media_screen as ms
    monkeypatch.setattr(ms, "_collect_articles", lambda specs: [("text", "http://x", "tbs")])
    monkeypatch.setattr(ms, "extract_numbers", lambda *a, **k: [_ex()])
    # parsed value older + different → fresher_period candidate
    monkeypatch.setattr(ms, "_parsed_for", lambda mid: (35.73, date(2025, 9, 30)))
    monkeypatch.setattr(ms, "get_open_media_review", lambda **k: [])
    captured = {}
    monkeypatch.setattr(ms, "insert_media_review_rows",
                        lambda cands, **k: captured.setdefault("c", cands) or len(cands))
    monkeypatch.setattr(ms, "notify", lambda *a, **k: True)
    rc = ms.run_screen(dry_run=False)
    assert rc == 0
    assert len(captured["c"]) == 1 and captured["c"][0].kind == "fresher_period"


def test_dry_run_does_not_insert(monkeypatch):
    import scrapers.media_screen as ms
    monkeypatch.setattr(ms, "_collect_articles", lambda specs: [("text", "http://x", "tbs")])
    monkeypatch.setattr(ms, "extract_numbers", lambda *a, **k: [_ex()])
    monkeypatch.setattr(ms, "_parsed_for", lambda mid: (35.73, date(2025, 9, 30)))
    monkeypatch.setattr(ms, "get_open_media_review", lambda **k: [])
    called = {"insert": False}
    monkeypatch.setattr(ms, "insert_media_review_rows",
                        lambda *a, **k: called.update(insert=True))
    monkeypatch.setattr(ms, "notify", lambda *a, **k: True)
    rc = ms.run_screen(dry_run=True)
    assert rc == 0 and called["insert"] is False


def test_no_articles_returns_zero_without_insert(monkeypatch):
    """Empty article sweep → rc==0, no insert, no crash (screen fails safe)."""
    import scrapers.media_screen as ms
    monkeypatch.setattr(ms, "_collect_articles", lambda specs: [])
    monkeypatch.setattr(ms, "extract_numbers", lambda *a, **k: [])
    monkeypatch.setattr(ms, "_parsed_for", lambda mid: (None, None))
    monkeypatch.setattr(ms, "get_open_media_review", lambda **k: [])
    called = {"insert": False}
    monkeypatch.setattr(ms, "insert_media_review_rows",
                        lambda *a, **k: called.update(insert=True))
    monkeypatch.setattr(ms, "notify", lambda *a, **k: True)
    rc = ms.run_screen(dry_run=False)
    assert rc == 0 and called["insert"] is False


def test_insert_write_error_returns_one_and_notifies(monkeypatch):
    """A SupabaseWriteError on insert is caught → rc==1 + error notify (no crash)."""
    import scrapers.media_screen as ms
    monkeypatch.setattr(ms, "_collect_articles", lambda specs: [("text", "http://x", "tbs")])
    monkeypatch.setattr(ms, "extract_numbers", lambda *a, **k: [_ex()])
    monkeypatch.setattr(ms, "_parsed_for", lambda mid: (35.73, date(2025, 9, 30)))
    monkeypatch.setattr(ms, "get_open_media_review", lambda **k: [])

    def _raise(*a, **k):
        raise SupabaseWriteError("boom")

    monkeypatch.setattr(ms, "insert_media_review_rows", _raise)
    notified = {}
    monkeypatch.setattr(ms, "notify",
                        lambda level, *a, **k: notified.setdefault("level", level))
    rc = ms.run_screen(dry_run=False)
    assert rc == 1
    assert notified["level"] == "error"


def test_open_review_match_drops_candidate(monkeypatch):
    """A candidate already present in open review rows is deduped out → no insert."""
    import scrapers.media_screen as ms
    monkeypatch.setattr(ms, "_collect_articles", lambda specs: [("text", "http://x", "tbs")])
    monkeypatch.setattr(ms, "extract_numbers", lambda *a, **k: [_ex()])
    monkeypatch.setattr(ms, "_parsed_for", lambda mid: (35.73, date(2025, 9, 30)))
    # An open row matching (metric_id, press_as_of) → drop_already_open removes it.
    monkeypatch.setattr(ms, "get_open_media_review",
                        lambda **k: [{"metric_id": "gross_npl_ratio", "press_as_of": "2026-03-31"}])
    called = {"insert": False}
    monkeypatch.setattr(ms, "insert_media_review_rows",
                        lambda *a, **k: called.update(insert=True))
    monkeypatch.setattr(ms, "notify", lambda *a, **k: True)
    rc = ms.run_screen(dry_run=False)
    assert rc == 0 and called["insert"] is False
