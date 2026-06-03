from datetime import date

from media_screen.types import Extracted


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
