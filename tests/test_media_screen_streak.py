"""media_screen/streak.py — the N-consecutive-zero-insert alert.

Regression context: econdelta-media-screen.service ran 62 consecutive nights
(2026-06-04 -> 2026-08-04) inserting 0 candidates every time, exiting 0 every
time, with no signal anywhere that anything was wrong. These tests pin the
tripwire that would have caught it well before 62 nights.
"""
from __future__ import annotations

from datetime import date

from media_screen.streak import ALERT_THRESHOLD, update_zero_insert_streak


def test_zero_insert_increments_streak(tmp_path):
    path = tmp_path / "streak.json"
    s1 = update_zero_insert_streak(0, today=date(2026, 8, 1), state_path=path, notifier=lambda *a, **k: None)
    s2 = update_zero_insert_streak(0, today=date(2026, 8, 2), state_path=path, notifier=lambda *a, **k: None)
    s3 = update_zero_insert_streak(0, today=date(2026, 8, 3), state_path=path, notifier=lambda *a, **k: None)
    assert (s1, s2, s3) == (1, 2, 3)


def test_real_insert_resets_streak_to_zero(tmp_path):
    path = tmp_path / "streak.json"
    update_zero_insert_streak(0, today=date(2026, 8, 1), state_path=path, notifier=lambda *a, **k: None)
    update_zero_insert_streak(0, today=date(2026, 8, 2), state_path=path, notifier=lambda *a, **k: None)
    streak = update_zero_insert_streak(3, today=date(2026, 8, 3), state_path=path, notifier=lambda *a, **k: None)
    assert streak == 0
    # And the reset persists to the next run.
    next_streak = update_zero_insert_streak(0, today=date(2026, 8, 4), state_path=path, notifier=lambda *a, **k: None)
    assert next_streak == 1


def test_alert_fires_exactly_at_threshold(tmp_path):
    path = tmp_path / "streak.json"
    calls = []

    def notifier(level, title, message, **k):
        calls.append((level, title))

    today = date(2026, 8, 1)
    for _ in range(ALERT_THRESHOLD - 1):
        update_zero_insert_streak(0, today=today, state_path=path, notifier=notifier)
        assert calls == []  # must stay silent before the threshold
    streak = update_zero_insert_streak(0, today=today, state_path=path, notifier=notifier)
    assert streak == ALERT_THRESHOLD
    assert len(calls) == 1
    level, title = calls[0]
    assert level == "warning"
    assert str(ALERT_THRESHOLD) in title


def test_alert_refires_every_threshold_after_first(tmp_path):
    """A screen that stays broken must not go quiet again for another 62
    nights -- the alert re-fires every ALERT_THRESHOLD nights, not just once."""
    path = tmp_path / "streak.json"
    calls = []

    def notifier(level, title, message, **k):
        calls.append(title)

    today = date(2026, 8, 1)
    for _ in range(ALERT_THRESHOLD * 2):
        update_zero_insert_streak(0, today=today, state_path=path, notifier=notifier)
    assert len(calls) == 2  # fired at 7 and again at 14


def test_no_alert_before_threshold(tmp_path):
    path = tmp_path / "streak.json"
    calls = []

    def notifier(*a, **k):
        calls.append(a)

    for _ in range(ALERT_THRESHOLD - 1):
        update_zero_insert_streak(0, today=date(2026, 8, 1), state_path=path, notifier=notifier)
    assert calls == []


def test_corrupt_state_file_restarts_at_zero(tmp_path):
    """A damaged state file must never crash the run -- restart the counter,
    same non-authoritative-cache posture as utils/staleness.py."""
    path = tmp_path / "streak.json"
    path.write_text("{not json")
    streak = update_zero_insert_streak(0, today=date(2026, 8, 1), state_path=path, notifier=lambda *a, **k: None)
    assert streak == 1


def test_state_persists_across_calls_reading_the_same_path(tmp_path):
    path = tmp_path / "streak.json"
    update_zero_insert_streak(0, today=date(2026, 8, 1), state_path=path, notifier=lambda *a, **k: None)
    assert path.exists()
    import json
    blob = json.loads(path.read_text())
    assert blob["consecutive_zero_insert_nights"] == 1
