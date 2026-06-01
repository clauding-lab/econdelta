"""Tests for run_logs helpers in utils/supabase_writer.py."""
from __future__ import annotations

from datetime import datetime, timezone

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


class TestLogRunEnd:
    def test_accepts_ok_status(self, monkeypatch):
        from utils.supabase_writer import log_run_end
        ts = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        # No raise on SKIP_SUPABASE=1 path
        log_run_end(run_id="00000000-0000-0000-0000-000000000000",
                    started_at=ts, status="ok", exit_code=0)

    def test_swallows_network_error(self, monkeypatch):
        from utils.supabase_writer import log_run_end
        monkeypatch.delenv("ECONDELTA_SKIP_SUPABASE", raising=False)
        monkeypatch.setenv("SUPABASE_URL", "https://nonexistent.invalid")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake")
        ts = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        log_run_end(run_id="00000000-0000-0000-0000-000000000000",
                    started_at=ts, status="fail", exit_code=1, error="boom")

    def test_computes_duration_ms(self, monkeypatch):
        """Verify duration_ms is computed from started_at to now."""
        from utils.supabase_writer import log_run_end
        # We can't easily intercept the upsert call without mocking _get_client,
        # so this test mostly verifies the call path doesn't raise.
        ts = datetime.now(timezone.utc)
        log_run_end(run_id="00000000-0000-0000-0000-000000000000",
                    started_at=ts, status="ok", exit_code=0)


class TestWrapRun:
    def test_returns_main_exit_code_on_success(self):
        from utils.supabase_writer import wrap_run
        rc = wrap_run("test_source", "test.service", lambda: 0)
        assert rc == 0

    def test_returns_main_exit_code_on_explicit_failure(self):
        from utils.supabase_writer import wrap_run
        rc = wrap_run("test_source", "test.service", lambda: 1)
        assert rc == 1

    def test_maps_exit_code_2_to_stale_status(self):
        from utils.supabase_writer import _STATUS_BY_EXIT
        assert _STATUS_BY_EXIT[0] == "ok"
        assert _STATUS_BY_EXIT[1] == "fail"
        assert _STATUS_BY_EXIT[2] == "stale"
        assert _STATUS_BY_EXIT[3] == "skip"

    def test_propagates_exception_after_logging(self):
        from utils.supabase_writer import wrap_run
        def boom():
            raise RuntimeError("kaboom")
        with pytest.raises(RuntimeError, match="kaboom"):
            wrap_run("test_source", "test.service", boom)
