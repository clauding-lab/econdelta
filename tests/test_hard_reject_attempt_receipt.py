"""A held daily attempt leaves an operations receipt, never a new capture.

On an Opus hard reject `main()` keeps the previous accepted `latest.json`
byte-for-byte (landmine 7), still runs the independent monthly appenders
(landmine 53), and records THIS attempt in `latest.attempt.json`. The Brief
does not read that sidecar; it is an operations receipt only.

These tests drive the real `main()` hard-reject branch and the real
`_run_chart_feeding_monthly_appenders` receipt machinery with fake writers.
No database, model or Discord call is made.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

PRIOR_LATEST = '{"data":{"prior":5.0},"updated_at":"2026-09-26T20:56:00+00:00"}'
PRIOR_RECEIPT = '{"daily":{"status":"skipped","attempted_at":"2026-09-20T20:56:00+00:00"}}'


@pytest.fixture
def held_run(tmp_path, monkeypatch):
    """A zero-field Opus reject (always a hard reject) with a realistic mixed monthly outcome.

    macro leg raises (contained + notified), yield leg reports 8 rows with no exact
    readback (a failure only the receipt records), exports finds nothing new.
    """
    import aggregate_latest as a

    config, data = tmp_path / "config", tmp_path / "data"
    archive = data / "archive"
    config.mkdir()
    archive.mkdir(parents=True)
    (config / "sources.json").write_text(json.dumps({"sources": {}}))
    registry = config / "sources-v3.json"
    registry.write_text(json.dumps({"version": "3.0", "indicators": []}))
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    (archive / f"latest_{yesterday}.json").write_text(json.dumps({
        "updated_at": f"{yesterday}T00:00:00+00:00", "data": {"prior": 5.0}}))
    latest = data / "latest.json"
    latest.write_text(PRIOR_LATEST)

    for name, path in (("REPO_ROOT", tmp_path), ("DATA_DIR", data), ("LATEST_PATH", latest),
                       ("ARCHIVE_DIR", archive), ("CONFIG_PATH", config / "sources.json"),
                       ("SOURCES_V3_PATH", registry),
                       ("STALENESS_STATE_PATH", data / "staleness_state.json"),
                       ("WATCHLIST_STALENESS_STATE_PATH", data / "watchlist_staleness_state.json"),
                       ("STALE_FALLBACK_ALERT_STATE_PATH", data / "stale_fallback_alert_state.json")):
        monkeypatch.setattr(a, name, path)
    monkeypatch.setattr(a, "review_data", lambda *args, **kwargs: {
        "status": "reject", "reason": "no safe field mapping", "missing": [], "anomalies": []})
    sent: list[tuple] = []
    monkeypatch.setattr(a, "notify", lambda *args, **kw: sent.append(args))

    def macro_source_down() -> int:
        raise RuntimeError("remittance page down")

    monkeypatch.setattr(a, "_write_macro_monthly_append", macro_source_down)
    monkeypatch.setattr(a, "_write_yield_ladder_monthly_append", lambda: 8)
    monkeypatch.setattr("utils.epb_monthly.write_exports_monthly", lambda: 0)
    real_appenders = a._run_chart_feeding_monthly_appenders

    def appenders_with_database_enabled() -> dict:
        # The daily stage stays offline (no seed/upsert before the verdict); the
        # independent monthly legs run for real against the fake writers above.
        monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "0")
        return real_appenders()

    monkeypatch.setattr(a, "_run_chart_feeding_monthly_appenders", appenders_with_database_enabled)
    monkeypatch.setenv("ECONDELTA_SKIP_OPUS_REVIEW", "0")
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "1")
    monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
    return a, latest, latest.with_suffix(".attempt.json"), archive, sent


def _run(a) -> tuple[int, datetime, datetime]:
    before = datetime.now(timezone.utc)
    code = a.main()
    return code, before, datetime.now(timezone.utc)


def _leg_categories(monthly: dict) -> dict[str, list[str]]:
    return {name: [f["category"] for f in part.get("failures", [])]
            for name, part in monthly["legs"].items()}


def test_held_day_receipt_records_this_attempt_beside_the_untouched_snapshot(held_run):
    a, latest, receipt_path, archive, _ = held_run
    archived_before = sorted(p.name for p in archive.iterdir())

    code, before, after = _run(a)

    assert code == 1
    assert latest.read_text() == PRIOR_LATEST  # accepted capture kept byte-for-byte
    assert sorted(p.name for p in archive.iterdir()) == archived_before
    receipt = json.loads(receipt_path.read_text())
    # A receipt, never a capture: no data, observations or capture time to mistake for one.
    assert set(receipt) == {"daily", "monthly"}
    daily = receipt["daily"]
    assert daily["status"] == "skipped"
    assert "previous accepted snapshot retained" in daily["reason"]
    assert before <= datetime.fromisoformat(daily["attempted_at"]) <= after
    monthly = receipt["monthly"]
    # One unconfirmed leg fails the stage without erasing what each leg did.
    assert monthly["status"] == "failed"
    assert _leg_categories(monthly) == {"macro": ["unhandled exception"],
                                        "yield": ["write unconfirmed"], "exports": []}
    assert monthly["legs"]["exports"]["status"] == "skipped"
    assert before <= datetime.fromisoformat(monthly["attempted_at"]) <= after
    assert not list(latest.parent.glob("*.tmp"))


@pytest.mark.parametrize("failing_step", ["write", "replace"])
def test_receipt_write_failure_keeps_the_snapshot_and_logs_the_monthly_outcome(
        held_run, monkeypatch, caplog, failing_step):
    """A sidecar that cannot be written is contained: the run still reports the
    held day (exit 1), the accepted snapshot and any older receipt are untouched,
    no half-written temp file is left, and the independent monthly outcome -
    including a failure no alert carries - reaches the log instead of vanishing."""
    a, latest, receipt_path, _, _ = held_run
    receipt_path.write_text(PRIOR_RECEIPT)
    tmp = receipt_path.with_suffix(".json.tmp")
    if failing_step == "write":
        real_write = Path.write_text

        def disk_full(self, *args, **kwargs):
            if self == tmp:
                real_write(self, '{"daily": {"sta')  # partial bytes, then the device fills
                raise OSError(28, "No space left on device")
            return real_write(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", disk_full)
    else:
        real_replace = os.replace

        def refused(src, dst, *args, **kwargs):
            if Path(dst) == receipt_path:
                raise PermissionError(13, "Permission denied")
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "replace", refused)

    with caplog.at_level(logging.ERROR, logger="aggregate_latest"):
        code, _, _ = _run(a)

    assert code == 1
    assert latest.read_text() == PRIOR_LATEST
    assert receipt_path.read_text() == PRIOR_RECEIPT  # an older receipt is never rewritten
    assert not tmp.exists()
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    receipt_errors = [m for m in errors if "latest.attempt.json" in m]
    assert len(receipt_errors) == 1, errors
    message = receipt_errors[0]
    assert "not written" in message
    logged = json.loads(message.split("this run's outcome: ", 1)[1])
    assert logged["monthly"]["status"] == "failed"
    assert _leg_categories(logged["monthly"]) == {"macro": ["unhandled exception"],
                                                  "yield": ["write unconfirmed"], "exports": []}
    assert logged["daily"]["status"] == "skipped"
