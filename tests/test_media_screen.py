from datetime import date

from media_screen.catalog import load_catalog
from media_screen.filter import classify
from media_screen.types import Extracted
from utils.supabase_writer import SupabaseWriteError


def _ex():
    return Extracted("NPL ratio", 32.26, date(2026, 3, 31), "q", "http://x", "tbs")


# Real headline from the 2026-08-05 production evidence (media-screen-applog.txt):
# thedailystar, 2026-08-02 & 2026-08-04 collections, both nights ending
# "0 candidate(s) inserted". BB actually cut the repo rate 10.00% -> 9.50% on
# 2026-07-30 (AGENTS.md landmine 39) -- exactly the kind of figure this screen
# exists to catch.
_POLICY_RATE_CUT_URL = (
    "https://www.thedailystar.net/business/banking/news/"
    "bb-cuts-policy-rate-95-after-2-years-4236481"
)
_POLICY_RATE_CUT_PRESS_VALUE = 9.50
_POLICY_RATE_CUT_PERIOD = date(2026, 7, 30)


def _policy_rate_cut_ex() -> Extracted:
    return Extracted(
        "policy repo rate", _POLICY_RATE_CUT_PRESS_VALUE, _POLICY_RATE_CUT_PERIOD,
        "BB cut the repo rate to 9.50 percent, the first cut in two years.",
        _POLICY_RATE_CUT_URL, "thedailystar",
    )


def _policy_rate_spec():
    return next(s for s in load_catalog() if s.metric_id == "policy_rate_repo")


def test_policy_rate_cut_headline_is_a_candidate_when_parsed_as_of_genuinely_lags():
    """Regression pin: when metric_history correctly carries the PRE-cut
    period (a monthly bulletin that hasn't caught up yet), classify() must
    recognise the real 2026-08-05-evidence policy-rate-cut headline as a
    fresher_period Candidate. This is the "obvious candidate" the 62-day
    silent-zero incident should have caught. If this test ever goes red, the
    screening/classify logic itself has regressed (as opposed to the
    diagnosed upstream as_of-forgery bug pinned below, which is a separate,
    still-open defect)."""
    spec = _policy_rate_spec()
    result = classify(
        spec.metric_id,
        10.00, date(2026, 6, 30),  # stale pre-cut bulletin value, correctly dated
        _policy_rate_cut_ex(),
        tolerance=spec.tolerance, valid_range=spec.valid_range,
    )
    assert result.__class__.__name__ == "Candidate"
    assert result.kind == "fresher_period"
    assert result.press_value == _POLICY_RATE_CUT_PRESS_VALUE


def test_policy_rate_cut_headline_is_masked_when_parsed_as_of_is_forged_to_today():
    """Characterises the diagnosed root cause (AGENTS.md landmine 47):
    parsers/html_labeled_value.py never recovers source_as_of for
    policy_rate_repo, so aggregate_latest.py falls back to stamping
    metric_history.as_of with the RUN's own date every night. Fed that forged
    "today" as parsed_as_of, classify() cannot tell the difference from a
    genuinely fresh read and skips the exact headline that should have fired
    -- reproducing the 62-consecutive-night silent-zero incident.
    This test documents current (buggy-but-undiagnosed-until-now) behaviour;
    it is intentionally NOT asserting the "fixed" outcome, because the actual
    fix belongs upstream in aggregate_latest.py's source_as_of recovery (out
    of scope for this PR — see AGENT_LEARNINGS.md 2026-08-05)."""
    spec = _policy_rate_spec()
    forged_as_of = date.today()
    result = classify(
        spec.metric_id,
        _POLICY_RATE_CUT_PRESS_VALUE, forged_as_of,  # forged: "today", not the true period
        _policy_rate_cut_ex(),
        tolerance=spec.tolerance, valid_range=spec.valid_range,
    )
    assert result.__class__.__name__ == "Skip"
    assert result.reason == "older-period"


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
    monkeypatch.setattr(ms, "update_zero_insert_streak", lambda *a, **k: 0)
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
    monkeypatch.setattr(ms, "update_zero_insert_streak", lambda *a, **k: 0)
    rc = ms.run_screen(dry_run=False)
    assert rc == 0 and called["insert"] is False


def _setup_one_candidate(monkeypatch, ms, open_rows=None):
    monkeypatch.setattr(ms, "_collect_articles",
                        lambda specs: [("text", "http://x", "tbsnews")])
    monkeypatch.setattr(ms, "extract_numbers", lambda *a, **k: [_ex()])
    monkeypatch.setattr(ms, "_parsed_for", lambda mid: (35.73, date(2025, 9, 30)))
    monkeypatch.setattr(ms, "get_open_media_review", lambda **k: open_rows or [])
    monkeypatch.setattr(ms, "update_zero_insert_streak", lambda *a, **k: 0)


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
    monkeypatch.setattr(ms, "update_zero_insert_streak", lambda *a, **k: 0)
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
    monkeypatch.setattr(ms, "update_zero_insert_streak", lambda *a, **k: 0)
    rc = ms.run_screen(dry_run=False)
    assert rc == 0 and called["insert"] is False


def test_no_catalog_match_extraction_is_visible_not_silently_dropped(monkeypatch):
    """Regression for the invisible sink: an LLM finding whose press_name
    string doesn't match any catalog spec used to vanish via a bare
    `continue` -- no skip, no log, no way to distinguish it from "found
    nothing." It must now surface as a Skip(reason="no-catalog-match") so
    every extraction is accounted for (the "12 collected, N skips" arithmetic
    from the 2026-08-05 evidence has to add up)."""
    import scrapers.media_screen as ms
    monkeypatch.setenv("MEDIA_SCREEN_WEBHOOK_URL", "https://brief/wh")
    off_catalog = Extracted("some paraphrase the LLM invented", 12.3, date(2026, 7, 1),
                            "q", "http://x", "tbsnews")
    monkeypatch.setattr(ms, "_collect_articles", lambda specs: [("text", "http://x", "tbsnews")])
    monkeypatch.setattr(ms, "extract_numbers", lambda *a, **k: [off_catalog])
    monkeypatch.setattr(ms, "_parsed_for", lambda mid: (None, None))
    monkeypatch.setattr(ms, "get_open_media_review", lambda **k: [])
    monkeypatch.setattr(ms, "update_zero_insert_streak", lambda *a, **k: 0)
    called = {"insert": False}
    monkeypatch.setattr(ms, "insert_media_review_rows",
                        lambda *a, **k: called.update(insert=True))
    calls = []
    monkeypatch.setattr(ms, "notify",
                        lambda level, title, message, **k: calls.append(message))
    rc = ms.run_screen(dry_run=False)
    assert rc == 0 and called["insert"] is False
    assert "not a tracked indicator" in calls[-1]


def test_partial_insert_write_exits_nonzero(monkeypatch):
    """A 2xx insert response carrying FEWER ids than candidates posted is a
    silent partial write (e.g. a conflicting constraint dropping rows) --
    this must be treated the same as a hard write failure: exit 1 + error
    notify, never rc==0."""
    import scrapers.media_screen as ms
    monkeypatch.setattr(ms, "_collect_articles", lambda specs: [("text", "http://x", "tbs")])
    # Two distinct candidates (different press_as_of) from one article.
    ex2 = Extracted("NPL ratio", 40.0, date(2026, 6, 30), "q2", "http://y", "tbs")
    monkeypatch.setattr(ms, "extract_numbers", lambda *a, **k: [_ex(), ex2])
    monkeypatch.setattr(ms, "_parsed_for", lambda mid: (35.73, date(2025, 9, 30)))
    monkeypatch.setattr(ms, "get_open_media_review", lambda **k: [])
    # Insert "succeeds" (2xx) but PostgREST returns only 1 id for 2 posted rows.
    monkeypatch.setattr(ms, "insert_media_review_rows", lambda cands, **k: [1])
    notified = {}
    monkeypatch.setattr(ms, "notify",
                        lambda level, *a, **k: notified.setdefault("level", level))
    monkeypatch.setattr(ms, "update_zero_insert_streak", lambda *a, **k: 0)
    rc = ms.run_screen(dry_run=False)
    assert rc == 1
    assert notified["level"] == "error"


def test_run_screen_wires_real_streak_tracker_on_zero_insert(monkeypatch, tmp_path):
    """Integration check: a live run_screen() with 0 candidates really does
    call the streak tracker (not a mock standing in for it), and the count
    persists to a second run -- proving N=7 has a working delivery mechanism,
    not just a unit-tested function nobody calls."""
    import scrapers.media_screen as ms
    from media_screen.streak import update_zero_insert_streak as real_update

    state_path = tmp_path / "streak.json"
    captured: list[int] = []

    def _wired(n_inserted, **kwargs):
        captured.append(n_inserted)
        kwargs.pop("state_path", None)
        return real_update(n_inserted, state_path=state_path, notifier=lambda *a, **k: None, **kwargs)

    monkeypatch.setattr(ms, "update_zero_insert_streak", _wired)
    monkeypatch.setattr(ms, "_collect_articles", lambda specs: [])
    monkeypatch.setattr(ms, "extract_numbers", lambda *a, **k: [])
    monkeypatch.setattr(ms, "get_open_media_review", lambda **k: [])
    monkeypatch.setattr(ms, "notify", lambda *a, **k: True)

    assert ms.run_screen(dry_run=False) == 0
    assert ms.run_screen(dry_run=False) == 0
    assert captured == [0, 0]
    import json
    assert json.loads(state_path.read_text())["consecutive_zero_insert_runs"] == 2
