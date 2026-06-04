from unittest.mock import MagicMock, patch

import utils.notifier as notifier


def _clear():
    notifier._recent_alerts.clear()


def test_webhook_url_param_overrides_env(monkeypatch):
    _clear()
    monkeypatch.delenv("ECONDELTA_DRY_RUN", raising=False)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://ops/webhook")
    with patch.object(notifier.requests, "post") as post:
        post.return_value = MagicMock(status_code=204, raise_for_status=lambda: None)
        ok = notifier.notify("info", "t", "m", webhook_url="https://brief/webhook")
    assert ok is True
    assert post.call_args.args[0] == "https://brief/webhook"


def test_none_webhook_url_falls_back_to_env(monkeypatch):
    _clear()
    monkeypatch.delenv("ECONDELTA_DRY_RUN", raising=False)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://ops/webhook")
    with patch.object(notifier.requests, "post") as post:
        post.return_value = MagicMock(status_code=204, raise_for_status=lambda: None)
        notifier.notify("info", "t2", "m", webhook_url=None)
    assert post.call_args.args[0] == "https://ops/webhook"


def test_empty_webhook_url_is_treated_as_unset(monkeypatch):
    _clear()
    monkeypatch.delenv("ECONDELTA_DRY_RUN", raising=False)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://ops/webhook")
    with patch.object(notifier.requests, "post") as post:
        post.return_value = MagicMock(status_code=204, raise_for_status=lambda: None)
        notifier.notify("info", "t3", "m", webhook_url="   ")
    assert post.call_args.args[0] == "https://ops/webhook"
