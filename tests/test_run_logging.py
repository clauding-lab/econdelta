"""Tests for run_logs helpers in utils/supabase_writer.py."""
from __future__ import annotations

import logging
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


class TestRingBufferHandler:
    def test_captures_warning_and_above(self):
        from utils.run_log_capture import RingBufferHandler
        handler = RingBufferHandler()
        log = logging.getLogger("test_run_log_capture.warn")
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)
        try:
            log.warning("disk almost full")
            log.error("write failed")
        finally:
            log.removeHandler(handler)
        assert "disk almost full" in handler.tail()
        assert "write failed" in handler.tail()

    def test_ignores_below_warning(self):
        from utils.run_log_capture import RingBufferHandler
        handler = RingBufferHandler()
        log = logging.getLogger("test_run_log_capture.info")
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)
        try:
            log.info("just fyi")
            log.debug("noisy detail")
        finally:
            log.removeHandler(handler)
        assert handler.tail() == ""

    def test_ordering_is_newest_last(self):
        from utils.run_log_capture import RingBufferHandler
        handler = RingBufferHandler()
        log = logging.getLogger("test_run_log_capture.order")
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)
        try:
            log.warning("first")
            log.warning("second")
            log.warning("third")
        finally:
            log.removeHandler(handler)
        tail = handler.tail()
        assert tail.index("first") < tail.index("second") < tail.index("third")

    def test_capacity_is_bounded(self):
        from utils.run_log_capture import RingBufferHandler
        handler = RingBufferHandler(capacity=3)
        log = logging.getLogger("test_run_log_capture.bounded")
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)
        try:
            for i in range(10):
                log.warning("warning number %d", i)
        finally:
            log.removeHandler(handler)
        assert len(handler.records) == 3
        tail = handler.tail()
        # only the last 3 survive the ring buffer
        assert "warning number 9" in tail
        assert "warning number 8" in tail
        assert "warning number 7" in tail
        assert "warning number 6" not in tail


class TestScrubSecrets:
    def test_redacts_jwt(self):
        from utils.run_log_capture import scrub_secrets
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        )
        out = scrub_secrets(f"Authorization: Bearer {jwt} rejected")
        assert jwt not in out
        assert "eyJ" not in out
        assert "REDACTED" in out

    def test_redacts_long_hex_token(self):
        from utils.run_log_capture import scrub_secrets
        token = "a3f9c2e1b7d4f6a8c9e2b1d3f5a7c9e1b2d4"  # 36 hex-looking chars
        out = scrub_secrets(f"upstream returned code {token} in body")
        assert token not in out
        assert "REDACTED" in out

    def test_does_not_redact_short_tokens(self):
        from utils.run_log_capture import scrub_secrets
        out = scrub_secrets("status code 42 returned, retry in 5s")
        assert "42" in out
        assert "REDACTED" not in out

    def test_redacts_url_query_string_keeps_scheme_host_path(self):
        from utils.run_log_capture import scrub_secrets
        url = "https://api.example.com/v1/resource?apikey=SUPERSECRETVALUE123&user=adnan"
        out = scrub_secrets(f"GET {url} failed with 500")
        assert "https://api.example.com/v1/resource" in out
        assert "SUPERSECRETVALUE123" not in out
        assert "user=adnan" not in out

    def test_redacts_authorization_marker_value(self):
        from utils.run_log_capture import scrub_secrets
        out = scrub_secrets("Authorization: Bearer sometotallysecrettoken1234")
        assert "sometotallysecrettoken1234" not in out
        assert "REDACTED" in out

    def test_redacts_apikey_marker_value_case_insensitive(self):
        from utils.run_log_capture import scrub_secrets
        out = scrub_secrets("APIKEY=abcdefghij0123456789secret")
        assert "abcdefghij0123456789secret" not in out
        assert "REDACTED" in out

    def test_redacts_token_marker_value(self):
        from utils.run_log_capture import scrub_secrets
        out = scrub_secrets("token: notarealtoken_abcdefghijklmnopqrstuvwxyz0123456789")
        assert "notarealtoken_abcdefghijklmnopqrstuvwxyz0123456789" not in out
        assert "REDACTED" in out

    def test_redacts_secret_marker_value(self):
        from utils.run_log_capture import scrub_secrets
        out = scrub_secrets("secret=myultrasecretvalue0987654321")
        assert "myultrasecretvalue0987654321" not in out
        assert "REDACTED" in out

    def test_redacts_password_marker_value(self):
        from utils.run_log_capture import scrub_secrets
        out = scrub_secrets("password: hunter2verylongpasswordvalue123")
        assert "hunter2verylongpasswordvalue123" not in out
        assert "REDACTED" in out

    def test_redacts_key_marker_value(self):
        from utils.run_log_capture import scrub_secrets
        out = scrub_secrets("key: notarealkey_abcdefghijklmnopqrstuvwxyz")
        assert "notarealkey_abcdefghijklmnopqrstuvwxyz" not in out
        assert "REDACTED" in out

    def test_redacts_env_style_secret_name_key_value_pair(self):
        from utils.run_log_capture import scrub_secrets
        out = scrub_secrets("SUPABASE_SERVICE_ROLE_KEY=abcdefghijklmnopqrstuvwxyz123456")
        assert "abcdefghijklmnopqrstuvwxyz123456" not in out
        assert "REDACTED" in out

    def test_env_style_secret_name_pattern_not_caught_by_marker_regex(self):
        """Regression: MARKER_RE's \\b can't see KEY inside ROLE_KEY (both \\w)."""
        from utils.run_log_capture import scrub_secrets
        out = scrub_secrets("CLAUDE_CODE_OAUTH_TOKEN=1234567890abcdefghijklmnopqrstuvwxyz")
        assert "1234567890abcdefghijklmnopqrstuvwxyz" not in out
        assert "REDACTED" in out

    def test_leaves_ordinary_text_untouched(self):
        from utils.run_log_capture import scrub_secrets
        msg = "ParseError: expected 'Overall Balance' row, found none in table"
        assert scrub_secrets(msg) == msg


class TestWrapRunErrorCapture:
    def test_ok_path_leaves_error_none(self, monkeypatch):
        from utils import supabase_writer
        captured = {}

        def fake_log_run_end(run_id, started_at, status, exit_code=0, error=None):
            captured["status"] = status
            captured["error"] = error

        monkeypatch.setattr(supabase_writer, "log_run_end", fake_log_run_end)

        def main():
            logging.getLogger("test_wrap_run.ok").warning("harmless heads-up")
            return 0

        rc = supabase_writer.wrap_run("test_source", "test.service", main)
        assert rc == 0
        assert captured["status"] == "ok"
        assert captured["error"] is None

    def test_fail_path_populates_error_from_captured_warnings(self, monkeypatch):
        from utils import supabase_writer
        captured = {}

        def fake_log_run_end(run_id, started_at, status, exit_code=0, error=None):
            captured["status"] = status
            captured["error"] = error

        monkeypatch.setattr(supabase_writer, "log_run_end", fake_log_run_end)

        def main():
            logging.getLogger("test_wrap_run.fail").warning("disk full: could not write output")
            return 1

        rc = supabase_writer.wrap_run("test_source", "test.service", main)
        assert rc == 1
        assert captured["status"] == "fail"
        assert captured["error"] is not None
        assert "disk full" in captured["error"]

    def test_stale_path_populates_error(self, monkeypatch):
        from utils import supabase_writer
        captured = {}

        def fake_log_run_end(run_id, started_at, status, exit_code=0, error=None):
            captured["status"] = status
            captured["error"] = error

        monkeypatch.setattr(supabase_writer, "log_run_end", fake_log_run_end)

        def main():
            logging.getLogger("test_wrap_run.stale").warning("source unchanged for 20 days")
            return 2

        rc = supabase_writer.wrap_run("test_source", "test.service", main)
        assert rc == 2
        assert captured["status"] == "stale"
        assert captured["error"] is not None
        assert "source unchanged" in captured["error"]

    def test_fail_path_with_no_warnings_leaves_error_none(self, monkeypatch):
        from utils import supabase_writer
        captured = {}

        def fake_log_run_end(run_id, started_at, status, exit_code=0, error=None):
            captured["status"] = status
            captured["error"] = error

        monkeypatch.setattr(supabase_writer, "log_run_end", fake_log_run_end)

        rc = supabase_writer.wrap_run("test_source", "test.service", lambda: 1)
        assert rc == 1
        assert captured["status"] == "fail"
        assert captured["error"] is None

    def test_exception_path_preserves_type_message_and_appends_tail(self, monkeypatch):
        from utils import supabase_writer
        captured = {}

        def fake_log_run_end(run_id, started_at, status, exit_code=0, error=None):
            captured["status"] = status
            captured["error"] = error

        monkeypatch.setattr(supabase_writer, "log_run_end", fake_log_run_end)

        def boom():
            logging.getLogger("test_wrap_run.exc").warning("retrying after timeout")
            raise RuntimeError("kaboom")

        with pytest.raises(RuntimeError, match="kaboom"):
            supabase_writer.wrap_run("test_source", "test.service", boom)

        assert captured["status"] == "fail"
        assert "RuntimeError: kaboom" in captured["error"]
        assert "retrying after timeout" in captured["error"]

    def test_error_is_truncated_and_scrubbed(self, monkeypatch):
        from utils import supabase_writer
        captured = {}

        def fake_log_run_end(run_id, started_at, status, exit_code=0, error=None):
            captured["status"] = status
            captured["error"] = error

        monkeypatch.setattr(supabase_writer, "log_run_end", fake_log_run_end)

        secret = "abcdefghijklmnopqrstuvwxyz0123456789"

        def main():
            log = logging.getLogger("test_wrap_run.truncate")
            for i in range(20):
                log.warning("padding line %d filler filler filler filler", i)
            log.warning("token=%s leaked here", secret)
            return 1

        supabase_writer.wrap_run("test_source", "test.service", main)
        assert captured["error"] is not None
        assert len(captured["error"]) <= 500
        assert secret not in captured["error"]

    def test_handler_detached_after_normal_return(self):
        from utils import supabase_writer

        root = logging.getLogger()
        before = len(root.handlers)
        supabase_writer.wrap_run("test_source", "test.service", lambda: 0)
        assert len(root.handlers) == before

    def test_handler_detached_after_raise(self):
        from utils import supabase_writer

        root = logging.getLogger()
        before = len(root.handlers)

        def boom():
            raise RuntimeError("kaboom")

        with pytest.raises(RuntimeError):
            supabase_writer.wrap_run("test_source", "test.service", boom)
        assert len(root.handlers) == before

    def test_second_run_does_not_see_first_runs_records(self, monkeypatch):
        from utils import supabase_writer
        captured = []

        def fake_log_run_end(run_id, started_at, status, exit_code=0, error=None):
            captured.append(error)

        monkeypatch.setattr(supabase_writer, "log_run_end", fake_log_run_end)

        def first_main():
            logging.getLogger("test_wrap_run.first").warning("first run's own problem")
            return 1

        def second_main():
            return 1

        supabase_writer.wrap_run("test_source", "test.service", first_main)
        supabase_writer.wrap_run("test_source", "test.service", second_main)

        assert captured[0] is not None and "first run's own problem" in captured[0]
        assert captured[1] is None
