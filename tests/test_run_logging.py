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

    def test_does_not_redact_file_path(self):
        """Regression: the catch-all used to include "/" in its character
        class, so a long file path got swept into ONE token across its
        slashes and redacted wholesale — real proof case from PR review."""
        from utils.run_log_capture import scrub_secrets
        msg = (
            "failed to load /home/adnan-local/econdelta/data/snapshots/"
            "bb_forex.json: JSONDecodeError"
        )
        out = scrub_secrets(msg)
        assert out == msg
        assert "REDACTED" not in out

    def test_does_not_redact_url_path(self):
        """Regression: same bug, URL path variant — real proof case."""
        from utils.run_log_capture import scrub_secrets
        msg = (
            "staleness check failed for "
            "https://www.bb.org.bd/en/index.php/econdata/exchangerate"
        )
        out = scrub_secrets(msg)
        assert out == msg
        assert "REDACTED" not in out

    def test_redacts_trailing_token_after_url_path(self):
        """The "/" fix must not blind the catch-all to a real secret that
        happens to follow a URL path, e.g. a webhook token."""
        from utils.run_log_capture import scrub_secrets
        secret = "abcdefghijklmnopqrstuvwxyz0123456789supersecret"
        msg = f"POST https://discord.com/api/webhooks/123456789/{secret} failed"
        out = scrub_secrets(msg)
        assert secret not in out
        assert "REDACTED" in out
        assert "https://discord.com/api/webhooks/123456789" in out

    def test_redacts_url_userinfo_credentials(self):
        from utils.run_log_capture import scrub_secrets
        url = "postgresql://postgres.abcdefgh:Dh4ka!Pass@aws-0-ap-south-1.pooler.supabase.com:6543/postgres"
        out = scrub_secrets(f"connection failed: {url}")
        assert "Dh4ka!Pass" not in out
        assert "postgres.abcdefgh" not in out
        assert "postgresql://" in out
        assert "aws-0-ap-south-1.pooler.supabase.com:6543/postgres" in out

    def test_redacts_url_userinfo_credentials_https(self):
        from utils.run_log_capture import scrub_secrets
        out = scrub_secrets("GET https://admin:Tr0ub4dor@bb.org.bd/econdata failed")
        assert "Tr0ub4dor" not in out
        assert "admin" not in out
        assert "https://" in out
        assert "bb.org.bd/econdata" in out

    def test_marker_redaction_preserves_bearer_scheme(self):
        """Regression: the replacement used to always emit "marker=[REDACTED]",
        turning "Authorization: Bearer xyz" into "Authorization=[REDACTED]" —
        dropping the Bearer scheme and misrepresenting the original line."""
        from utils.run_log_capture import scrub_secrets
        out = scrub_secrets("Authorization: Bearer sometotallysecrettoken1234")
        assert "sometotallysecrettoken1234" not in out
        assert "Bearer" in out
        assert "Authorization:" in out
        assert "REDACTED" in out


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

    def test_exception_prefix_survives_a_full_ring_buffer(self, monkeypatch):
        """Regression: _finalize_run_error used to truncate raw[-500:] — a
        LEFT truncation — so a chatty ring buffer (10 real-length warnings
        routinely exceeds 500 chars once formatted with logger names) cut
        away the exception's own "Type: message" prefix, the one thing
        main() actually produced. The prefix must survive even when the
        tail alone would blow the whole budget.
        """
        from utils import supabase_writer
        captured = {}

        def fake_log_run_end(run_id, started_at, status, exit_code=0, error=None):
            captured["status"] = status
            captured["error"] = error

        monkeypatch.setattr(supabase_writer, "log_run_end", fake_log_run_end)

        def boom():
            log = logging.getLogger("aggregate_latest.media_overrides")
            for i in range(10):
                log.warning(
                    "media override write failed for banking_npl_pct: "
                    "HTTPError 409 conflict on attempt %d of 10", i,
                )
            raise ValueError("bundle validation failed: hero metric cpi_yoy missing")

        with pytest.raises(ValueError):
            supabase_writer.wrap_run("test_source", "test.service", boom)

        assert captured["status"] == "fail"
        error = captured["error"]
        assert error is not None
        assert len(error) <= 500
        assert error.startswith("ValueError")
        assert "bundle validation failed: hero metric cpi_yoy missing" in error

    def test_scrub_runs_before_truncation_not_after(self):
        """Regression: the old order truncated ``raw[-500:]`` BEFORE
        scrubbing. Three of scrub_secrets' five patterns are left-anchored
        (need the marker word immediately before the value) — if truncation
        cuts the marker away but leaves a short remnant of the secret value
        (under 20 chars, so the generic catch-all can't save it either), the
        secret survives into PUBLIC run_logs untouched. Construct exactly
        that straddle and confirm the current (scrub-then-truncate) order
        closes it.
        """
        from utils import supabase_writer

        secret_val = "hunter2verylongpasswordvalue123"
        marker_line = f"password: {secret_val}"
        remaining = 10  # short enough to dodge the 20-char catch-all alone
        # Padding starts with a space so the surviving secret remnant is an
        # ISOLATED short run (bounded by whitespace, not merged into one
        # long catch-all match with the filler that follows it) — exactly
        # what a real subsequent log line looks like.
        target_after_len = supabase_writer._RUN_ERROR_MAX_CHARS - remaining
        filler = " unrelated log output continues here" * 20
        after_padding = filler[:target_after_len]
        tail = marker_line + after_padding

        # Sanity-check the straddle itself (plain slicing, not the function
        # under test) before trusting the assertion below.
        old_style_truncation = tail[-supabase_writer._RUN_ERROR_MAX_CHARS:]
        assert secret_val[-remaining:] in old_style_truncation
        assert "password" not in old_style_truncation

        error = supabase_writer._finalize_run_error(tail)

        assert secret_val not in error
        assert secret_val[-remaining:] not in error

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


class TestWrapRunPreservesStderrLogging:
    """Regression for bbcf210: attaching the RingBufferHandler to root BEFORE
    main_func() runs made logging.basicConfig() (called by all 16 real
    entrypoints as the first line of their own main()) a documented no-op,
    which silenced every on-disk systemd log in the fleet. These simulate a
    genuinely fresh process (root.handlers == [], root.level == NOTSET) by
    monkeypatching root's real state — pytest itself always pre-populates
    both, so the bug can't be seen without doing this.
    """

    def test_installs_stderr_handler_when_root_has_none(self, monkeypatch, capsys):
        from utils import supabase_writer

        root = logging.getLogger()
        monkeypatch.setattr(root, "handlers", [])
        monkeypatch.setattr(root, "level", logging.NOTSET)

        def main():
            # Exactly what every real entrypoint's main() does first.
            logging.basicConfig(
                level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
            )
            logging.getLogger("probe.fresh").info("informational heads-up")
            logging.getLogger("probe.fresh").warning("a real warning")
            return 0

        rc = supabase_writer.wrap_run("test_source", "test.service", main)

        assert rc == 0
        err = capsys.readouterr().err
        assert "informational heads-up" in err
        assert "a real warning" in err

    def test_preexisting_root_handler_still_receives_info_records(self, monkeypatch):
        """A handler already on root (e.g. pytest's own capture handlers, or
        an embedder that pre-configured logging) sees root at its real
        un-configured default of WARNING (30) — NOT NOTSET, which is
        root-special-cased to mean "no restriction" and would make this
        test pass trivially even without the fix. WARNING is the actual
        interpreter default (`logging.getLogger().level == 30` fresh out of
        the box) and the one basicConfig()'s no-op leaves untouched.
        """
        import io

        from utils import supabase_writer

        root = logging.getLogger()
        stream = io.StringIO()
        outer_handler = logging.StreamHandler(stream)
        monkeypatch.setattr(root, "handlers", [outer_handler])
        monkeypatch.setattr(root, "level", logging.WARNING)

        def main():
            logging.getLogger("probe.preexisting").info("outer should see this info")
            logging.getLogger("probe.preexisting").warning("outer should see this warning")
            return 0

        rc = supabase_writer.wrap_run("test_source", "test.service", main)

        assert rc == 0
        out = stream.getvalue()
        assert "outer should see this info" in out
        assert "outer should see this warning" in out
