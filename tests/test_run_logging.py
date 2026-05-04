"""Tests for run_logs helpers in utils/supabase_writer.py."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture(autouse=True)
def skip_supabase_env(monkeypatch):
    """Don't actually hit Supabase in unit tests."""
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "1")
    yield


class TestLogRunStart:
    def test_returns_uuid_string(self, monkeypatch):
        from utils.supabase_writer import log_run_start
        # When SKIP_SUPABASE=1, helper short-circuits and returns a local uuid.
        run_id = log_run_start(source="bb_forex", unit="econdelta-forex.service")
        assert isinstance(run_id, str)
        assert len(run_id) == 36  # uuid format

    def test_uses_provided_started_at(self):
        from utils.supabase_writer import log_run_start
        ts = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        run_id = log_run_start(source="bb_forex", started_at=ts)
        assert isinstance(run_id, str)

    def test_swallows_network_error(self, monkeypatch):
        """Logging failure must NOT raise — would mask scrape outcome."""
        from utils.supabase_writer import log_run_start
        monkeypatch.delenv("ECONDELTA_SKIP_SUPABASE", raising=False)
        monkeypatch.setenv("SUPABASE_URL", "https://nonexistent.invalid")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake")
        # Should return a uuid even on network failure
        run_id = log_run_start(source="bb_forex")
        assert isinstance(run_id, str)
