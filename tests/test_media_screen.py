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
                        lambda cands, **k: captured.setdefault("c", cands) or [1])
    monkeypatch.setenv("MEDIA_SCREEN_WEBHOOK_URL", "https://brief/wh")
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


def _setup_one_candidate(monkeypatch, ms, open_rows=None):
    monkeypatch.setattr(ms, "_collect_articles",
                        lambda specs: [("text", "http://x", "tbsnews")])
    monkeypatch.setattr(ms, "extract_numbers", lambda *a, **k: [_ex()])
    monkeypatch.setattr(ms, "_parsed_for", lambda mid: (35.73, date(2025, 9, 30)))
    monkeypatch.setattr(ms, "get_open_media_review", lambda **k: open_rows or [])


def test_zero_candidates_still_posts_heartbeat(monkeypatch):
    """Goal 1: a 0-candidate live run posts exactly one report. Mutation: re-gating
    the post behind a None/empty check must turn this red."""
    import scrapers.media_screen as ms
    monkeypatch.setenv("MEDIA_SCREEN_WEBHOOK_URL", "https://brief/wh")
    monkeypatch.setattr(ms, "_collect_articles",
                        lambda specs: [("t", "http://x", "tbsnews")])
    monkeypatch.setattr(ms, "extract_numbers", lambda *a, **k: [])
    monkeypatch.setattr(ms, "_parsed_for", lambda mid: (None, None))
    monkeypatch.setattr(ms, "get_open_media_review", lambda **k: [])
    calls = []
    monkeypatch.setattr(ms, "notify",
                        lambda level, title, message, **k: calls.append((level, title, message, k)))
    rc = ms.run_screen(dry_run=False)
    assert rc == 0
    assert len(calls) == 1
    level, title, message, kw = calls[0]
    assert level == "info" and "no change" in title.lower()
    assert kw.get("webhook_url") == "https://brief/wh"


def test_report_routes_to_media_screen_webhook(monkeypatch):
    """Goal 4: the report carries webhook_url=MEDIA_SCREEN_WEBHOOK_URL.
    Mutation: dropping webhook_url= must fail this."""
    import scrapers.media_screen as ms
    monkeypatch.setenv("MEDIA_SCREEN_WEBHOOK_URL", "https://brief/wh")
    _setup_one_candidate(monkeypatch, ms)
    monkeypatch.setattr(ms, "insert_media_review_rows", lambda cands, **k: [42])
    calls = []
    monkeypatch.setattr(ms, "notify",
                        lambda level, title, message, **k: calls.append((level, k)))
    rc = ms.run_screen(dry_run=False)
    assert rc == 0
    level, kw = calls[-1]
    assert level == "warning" and kw.get("webhook_url") == "https://brief/wh"


def test_unset_webhook_skips_post_no_ops_fallback(monkeypatch):
    """An unset MEDIA_SCREEN_WEBHOOK_URL must NOT route the report to the ops channel."""
    import scrapers.media_screen as ms
    monkeypatch.delenv("MEDIA_SCREEN_WEBHOOK_URL", raising=False)
    _setup_one_candidate(monkeypatch, ms)
    monkeypatch.setattr(ms, "insert_media_review_rows", lambda cands, **k: [42])
    calls = []
    monkeypatch.setattr(ms, "notify", lambda *a, **k: calls.append((a, k)))
    rc = ms.run_screen(dry_run=False)
    assert rc == 0 and calls == []


def test_dry_run_prints_report_does_not_notify(monkeypatch, capsys):
    import scrapers.media_screen as ms
    monkeypatch.setenv("MEDIA_SCREEN_WEBHOOK_URL", "https://brief/wh")
    _setup_one_candidate(monkeypatch, ms)
    notified = []
    monkeypatch.setattr(ms, "notify", lambda *a, **k: notified.append(a))
    rc = ms.run_screen(dry_run=True)
    assert rc == 0 and notified == []
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "needs approval" in out


def test_already_open_candidate_reported_as_skip(monkeypatch):
    """An open-row match: not inserted AND shown in the report as already-in-review."""
    import scrapers.media_screen as ms
    monkeypatch.setenv("MEDIA_SCREEN_WEBHOOK_URL", "https://brief/wh")
    _setup_one_candidate(monkeypatch, ms,
                         open_rows=[{"metric_id": "gross_npl_ratio", "press_as_of": "2026-03-31"}])
    inserted = {"called": False}
    monkeypatch.setattr(ms, "insert_media_review_rows",
                        lambda *a, **k: inserted.update(called=True) or [])
    calls = []
    monkeypatch.setattr(ms, "notify",
                        lambda level, title, message, **k: calls.append(message))
    rc = ms.run_screen(dry_run=False)
    assert rc == 0 and inserted["called"] is False
    assert "already in review queue" in calls[-1]


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
