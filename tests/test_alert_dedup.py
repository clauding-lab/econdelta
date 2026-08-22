"""Tests for utils/alert_dedup.py (MEDIUM-5, 2026-08-22 round-1 review)."""

from datetime import date
from pathlib import Path

import pytest

from utils.alert_dedup import should_alert_today

DAY0 = date(2026, 5, 1)


@pytest.fixture
def state_path(tmp_path) -> Path:
    return tmp_path / "alert_dedup_state.json"


def test_first_call_for_a_key_today_alerts(state_path):
    assert should_alert_today("parse_all_preflight", state_path, today=DAY0) is True


def test_second_call_same_key_same_day_is_suppressed(state_path):
    assert should_alert_today("parse_all_preflight", state_path, today=DAY0) is True
    assert should_alert_today("parse_all_preflight", state_path, today=DAY0) is False
    assert should_alert_today("parse_all_preflight", state_path, today=DAY0) is False


def test_new_day_resets_the_dedup(state_path):
    from datetime import timedelta

    assert should_alert_today("parse_all_preflight", state_path, today=DAY0) is True
    assert should_alert_today("parse_all_preflight", state_path, today=DAY0) is False
    assert should_alert_today(
        "parse_all_preflight", state_path, today=DAY0 + timedelta(days=1)
    ) is True


def test_different_keys_are_independent(state_path):
    assert should_alert_today("wrap_run_crash:bb_forex", state_path, today=DAY0) is True
    assert should_alert_today("wrap_run_crash:dse_market", state_path, today=DAY0) is True
    assert should_alert_today("wrap_run_crash:bb_forex", state_path, today=DAY0) is False
    assert should_alert_today("wrap_run_crash:dse_market", state_path, today=DAY0) is False


def test_persists_across_separate_calls_simulating_separate_processes(state_path):
    """The whole point: systemd restarts are FRESH processes, so the state
    must survive on disk, not just in memory."""
    assert should_alert_today("wrap_run_crash:bb_forex", state_path, today=DAY0) is True
    # A brand-new call with no shared in-memory state (as a fresh process
    # restart would be) still sees the persisted record.
    assert should_alert_today("wrap_run_crash:bb_forex", state_path, today=DAY0) is False


def test_corrupt_state_file_defaults_to_alerting_not_raising(state_path):
    state_path.write_text("{not valid json")
    assert should_alert_today("parse_all_preflight", state_path, today=DAY0) is True


def test_non_dict_state_file_defaults_to_alerting_not_raising(state_path):
    state_path.write_text("[1, 2, 3]")
    assert should_alert_today("parse_all_preflight", state_path, today=DAY0) is True


def test_unwritable_state_dir_still_alerts(state_path, monkeypatch):
    """A write failure must never suppress a real alert -- only READING an
    existing "already alerted" record may suppress one."""
    import utils.alert_dedup as dedup_mod

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(dedup_mod.os, "replace", _boom)
    assert should_alert_today("parse_all_preflight", state_path, today=DAY0) is True
