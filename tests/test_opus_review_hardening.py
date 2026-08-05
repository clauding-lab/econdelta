"""Tests for E4+E5: Opus-review safety-net hardening.

E4: aggregate_latest.py's two loudest notify() calls (Opus hard-reject,
    field quarantine) passed "warn" -- not a valid notify() level -- so they
    rendered grey/emoji-less, below routine "info"/"warning"/"error" chatter.

E5: utils/opus_review.py's claude-CLI subprocess call was the only call site
    missing the --no-session-persistence / --tools "" / --strict-mcp-config /
    --permission-mode bypassPermissions hardening every sibling call site
    carries (claude_max/max_client.py, parse_all.py's preflight). It also
    silently self-disabled (logger.warning only) on any operational failure.
"""
from __future__ import annotations

import ast
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import aggregate_latest as agg  # noqa: E402
from tests.test_aggregator import _build_data_tree  # noqa: E402
from utils.opus_review import review_data  # noqa: E402


class TestOpusReviewArgvHardening:
    """review_data() must invoke the claude CLI with the same lockdown flags
    as claude_max/max_client.py's run_max() -- the ONLY call site missing them."""

    def test_argv_carries_the_four_hardening_flags(self, monkeypatch):
        captured: dict = {}

        class _FakeCompletedProcess:
            returncode = 0
            stdout = '{"status": "ok", "reason": "fine"}'
            stderr = ""

        def _fake_run(argv, **kwargs):
            captured["argv"] = argv
            return _FakeCompletedProcess()

        monkeypatch.setattr("utils.opus_review.subprocess.run", _fake_run)

        review_data({"dsex": 5000.0}, [{"data": {"dsex": 4990.0}}], binary="claude")

        argv = captured["argv"]
        assert "--no-session-persistence" in argv, argv
        assert "--strict-mcp-config" in argv, argv
        assert "--permission-mode" in argv, argv
        assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions", argv
        assert "--tools" in argv, argv
        assert argv[argv.index("--tools") + 1] == "", argv

    def test_timeout_behavior_is_unchanged(self, monkeypatch):
        """The hardening must not touch the timeout_s -> subprocess timeout wiring."""
        captured: dict = {}

        class _FakeCompletedProcess:
            returncode = 0
            stdout = '{"status": "ok", "reason": "fine"}'
            stderr = ""

        def _fake_run(argv, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return _FakeCompletedProcess()

        monkeypatch.setattr("utils.opus_review.subprocess.run", _fake_run)

        review_data(
            {"dsex": 5000.0}, [{"data": {"dsex": 4990.0}}], binary="claude", timeout_s=123
        )

        assert captured["timeout"] == 123


@pytest.fixture(autouse=True)
def _skip_supabase(monkeypatch):
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "1")
    yield


def _seed_one_archive_day(archive_dir: Path) -> None:
    """Write one archived latest.json so load_history() is non-empty and
    main() actually enters the Opus-review branch instead of the
    'no archive history yet' early-out."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    blob = {
        "updated_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "data": {"dsex": 5000.0},
    }
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    (archive_dir / f"latest_{yesterday}.json").write_text(json.dumps(blob), encoding="utf-8")


class TestOpusReviewSkipVisibility:
    """The review must stay advisory (never blocks publication on its own
    failure) but an involuntary self-disable must now be visible."""

    def test_involuntary_skip_notifies_at_warning(self, tmp_path, monkeypatch):
        data_dir, cfg_path = _build_data_tree(tmp_path)
        latest_path = data_dir / "latest.json"
        archive_dir = data_dir / "archive"
        _seed_one_archive_day(archive_dir)

        monkeypatch.setattr(agg, "DATA_DIR", data_dir)
        monkeypatch.setattr(agg, "LATEST_PATH", latest_path)
        monkeypatch.setattr(agg, "CONFIG_PATH", cfg_path)
        monkeypatch.setattr(agg, "ARCHIVE_DIR", archive_dir)
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        # Not the kill-switch -- must actually enter the review branch.
        monkeypatch.setenv("ECONDELTA_SKIP_OPUS_REVIEW", "0")

        def _fake_review_data(today_data, history, **kwargs):
            return {
                "status": "ok",
                "reason": "review_skipped: claude_exit_1",
                "skipped": True,
            }

        monkeypatch.setattr(agg, "review_data", _fake_review_data)

        notify_calls: list[tuple] = []

        def _fake_notify(level, title, message, fields=None):
            notify_calls.append((level, title, message))
            return True

        monkeypatch.setattr(agg, "notify", _fake_notify)

        exit_code = agg.main()
        assert exit_code == 0  # advisory -- never blocks publication

        warning_calls = [c for c in notify_calls if c[0] == "warning"]
        assert warning_calls, (
            f"expected a 'warning' notify on involuntary Opus-review skip, "
            f"got levels: {[c[0] for c in notify_calls]}"
        )
        assert any("claude_exit_1" in c[2] for c in warning_calls), (
            "the warning notify should name the skip reason"
        )

    def test_kill_switch_skip_stays_silent(self, tmp_path, monkeypatch):
        """ECONDELTA_SKIP_OPUS_REVIEW=1 is an explicit operator choice --
        it must NOT trigger the new visibility notify, and review_data must
        not even be called."""
        data_dir, cfg_path = _build_data_tree(tmp_path)
        latest_path = data_dir / "latest.json"
        archive_dir = data_dir / "archive"
        _seed_one_archive_day(archive_dir)

        monkeypatch.setattr(agg, "DATA_DIR", data_dir)
        monkeypatch.setattr(agg, "LATEST_PATH", latest_path)
        monkeypatch.setattr(agg, "CONFIG_PATH", cfg_path)
        monkeypatch.setattr(agg, "ARCHIVE_DIR", archive_dir)
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        monkeypatch.setenv("ECONDELTA_SKIP_OPUS_REVIEW", "1")

        review_data_calls: list = []

        def _fake_review_data(today_data, history, **kwargs):
            review_data_calls.append((today_data, history))
            return {"status": "ok", "reason": "should not be called", "skipped": True}

        monkeypatch.setattr(agg, "review_data", _fake_review_data)

        notify_calls: list[tuple] = []

        def _fake_notify(level, title, message, fields=None):
            notify_calls.append((level, title, message))
            return True

        monkeypatch.setattr(agg, "notify", _fake_notify)

        exit_code = agg.main()
        assert exit_code == 0

        assert not review_data_calls, "kill-switch must skip calling review_data entirely"
        opus_calls = [c for c in notify_calls if "opus" in c[1].lower() or "review" in c[1].lower()]
        assert not opus_calls, (
            f"kill-switch path must stay silent about Opus review, got: {opus_calls}"
        )


class TestNotifyLevelLiterals:
    """Guard against a repeat of the 'warn' typo: every notify() call site in
    aggregate_latest.py must pass a level literal notify() actually accepts."""

    _VALID_LEVELS = {"info", "warning", "error"}

    def test_every_notify_call_uses_a_valid_level_literal(self):
        source = (REPO_ROOT / "aggregate_latest.py").read_text(encoding="utf-8")
        tree = ast.parse(source, filename="aggregate_latest.py")

        offenders: list[tuple[int, object]] = []
        checked = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_notify_call = (isinstance(func, ast.Name) and func.id == "notify") or (
                isinstance(func, ast.Attribute) and func.attr == "notify"
            )
            if not is_notify_call or not node.args:
                continue
            level_arg = node.args[0]
            checked += 1
            if not (isinstance(level_arg, ast.Constant) and isinstance(level_arg.value, str)):
                # Non-literal level (e.g. a variable) -- can't statically check, skip.
                continue
            if level_arg.value not in self._VALID_LEVELS:
                offenders.append((node.lineno, level_arg.value))

        assert checked > 0, "expected to find notify() call sites in aggregate_latest.py"
        assert not offenders, (
            f"notify() call(s) with an invalid level literal (not in "
            f"{self._VALID_LEVELS}): {offenders}"
        )
