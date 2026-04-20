"""Tests for utils/notifier.py."""

import time
from unittest.mock import patch

import pytest

import utils.notifier as notifier_module
from utils.notifier import notify


@pytest.fixture(autouse=True)
def clear_dedup_cache():
    """Reset the in-memory dedup dict before each test."""
    notifier_module._recent_alerts.clear()
    yield
    notifier_module._recent_alerts.clear()


class TestDryRunMode:
    def test_dry_run_returns_true(self, capsys, monkeypatch):
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

        result = notify("info", "Test Title", "Test message")

        assert result is True

    def test_dry_run_prints_to_stdout(self, capsys, monkeypatch):
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

        notify("warning", "Alert Title", "Something happened")

        captured = capsys.readouterr()
        assert "[DRY-RUN DISCORD]" in captured.out
        assert "warning" in captured.out
        assert "Alert Title" in captured.out

    def test_dry_run_does_not_call_requests(self, monkeypatch):
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

        with patch("utils.notifier.requests.post") as mock_post:
            notify("error", "Err", "msg")

        mock_post.assert_not_called()


class TestMissingWebhook:
    def test_returns_false_when_no_webhook_url(self, monkeypatch):
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "0")
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

        result = notify("info", "No URL test", "message")

        assert result is False

    def test_logs_warning_when_no_webhook_url(self, monkeypatch, caplog):
        import logging

        monkeypatch.setenv("ECONDELTA_DRY_RUN", "0")
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

        with caplog.at_level(logging.WARNING, logger="utils.notifier"):
            notify("info", "No URL", "msg")

        assert any("DISCORD_WEBHOOK_URL" in record.message for record in caplog.records)


class TestDedup:
    def test_second_identical_call_is_suppressed(self, capsys, monkeypatch):
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")

        result1 = notify("error", "Duplicate", "first")
        result2 = notify("error", "Duplicate", "second same title")

        assert result1 is True
        assert result2 is False

    def test_suppressed_prints_notice(self, capsys, monkeypatch):
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")

        notify("warning", "Dup Title", "first message")
        capsys.readouterr()  # clear buffer

        notify("warning", "Dup Title", "second message")
        captured = capsys.readouterr()

        assert "Suppressed" in captured.out or "suppressed" in captured.out.lower()

    def test_different_titles_are_not_deduped(self, monkeypatch):
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")

        result1 = notify("info", "Title A", "msg")
        result2 = notify("info", "Title B", "msg")

        assert result1 is True
        assert result2 is True

    def test_dedup_window_expires(self, monkeypatch):
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")

        # Manually insert a stale entry (older than window)
        notifier_module._recent_alerts[("info", "Stale Title")] = (
            time.monotonic() - notifier_module._DEDUP_WINDOW_SECONDS - 1
        )

        result = notify("info", "Stale Title", "should go through")

        assert result is True
