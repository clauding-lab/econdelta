import logging
from unittest.mock import MagicMock, patch

import requests

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


def test_request_exception_does_not_leak_webhook_url_or_token(monkeypatch, caplog):
    """requests embeds the full webhook URL (token included) in HTTPError's
    string, e.g. '404 Client Error ... for url: https://discord.com/api/
    webhooks/<ID>/<TOKEN>'. The token IS the credential — it must never reach
    the on-disk unit logs that every systemd unit appends stderr to.
    """
    _clear()
    monkeypatch.delenv("ECONDELTA_DRY_RUN", raising=False)
    webhook_url = (
        "https://discord.com/api/webhooks/123456789012345678/"
        "FAKEtoken-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789fake"
    )
    response = requests.Response()
    response.status_code = 404
    response.reason = "Not Found"
    response.url = webhook_url

    with patch.object(notifier.requests, "post") as post:
        post.return_value = response
        with caplog.at_level(logging.ERROR, logger="utils.notifier"):
            ok = notifier.notify("error", "t4", "m", webhook_url=webhook_url)

    assert ok is False
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "discord.com/api/webhooks" not in logged
    assert "FAKEtoken-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789fake" not in logged
    # Still useful for debugging: exception class + HTTP status survive.
    assert "HTTPError" in logged
    assert "404" in logged
