import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import parse_all


def test_parse_all_writes_per_indicator_snapshots(tmp_path: Path):
    cfg = tmp_path / "sources-v3.json"
    cfg.write_text(json.dumps({
        "version": "3.0",
        "indicators": [
            {"id": "x", "name": "X", "domain": "money_market", "cadence": "daily",
             "fetch": {"type": "html", "url": "https://example.com", "task": "x"},
             "parse": {"deterministic": "html_footer_ticker", "value_type": "percent",
                       "valid_range": [0, 100], "llm_prompt": "html_footer_ticker.txt"}},
        ],
    }))
    fake_artifact_path = tmp_path / "x.html"
    fake_artifact_path.write_text("<html></html>")
    fake_artifact = type("FR", (), {
        "indicator_id": "x", "artifact_path": fake_artifact_path, "artifact_type": "html",
        "fetched_at": datetime.now(timezone.utc), "source_url": "x", "sha256": "y"*64, "cache_hit": False,
    })()
    fake_snapshot = {"indicator_id": "x", "value": 10.0, "_provenance": "deterministic"}
    with patch("parse_all._load_artifact_for", return_value=fake_artifact), \
         patch("parse_all.parse_one", return_value=fake_snapshot):
        results = parse_all.run(config_path=cfg, data_root=tmp_path / "data")
    assert results
    assert results[0]["value"] == 10.0
    out_files = list((tmp_path / "data" / "x").glob("*.json"))
    assert len(out_files) == 1


def test_load_artifact_prefers_recorded_period_over_mtime(tmp_path: Path):
    """E1 leftover, hardened: a month-dir that accumulated a stale + fresh MEI
    issue (ExonVPS's 2026-06/ held 2026_april.pdf AND 2026_may.pdf) must parse
    the newest ISSUE by the (year, month) recorded in each .meta.json sidecar —
    NOT file mtime. Here the STALE April file is given the NEWER mtime (a
    re-fetch / rsync race, landmine 13), so an mtime heuristic would pick the
    wrong issue; period selection must still pick May."""
    import os

    ind = {"id": "money_multiplier", "fetch": {"type": "pdf", "url": "https://bb/mei"}}
    month = tmp_path / "_pdfs" / "money_multiplier" / "2026-06"
    month.mkdir(parents=True)
    stale = month / "2026_april.pdf"
    fresh = month / "2026_may.pdf"
    stale.write_bytes(b"%PDF-april")
    fresh.write_bytes(b"%PDF-may")
    stale.with_suffix(".meta.json").write_text(json.dumps({"period": "2026-04"}))
    fresh.with_suffix(".meta.json").write_text(json.dumps({"period": "2026-05"}))
    # Adversarial: give the STALE issue the NEWER mtime — mtime alone would lie.
    now = datetime.now(timezone.utc).timestamp()
    os.utime(fresh, (now - 1000, now - 1000))
    os.utime(stale, (now, now))

    artifact = parse_all._load_artifact_for(ind, tmp_path)
    assert artifact is not None
    assert artifact.artifact_path.name == "2026_may.pdf"


def test_load_artifact_falls_back_to_mtime_without_sidecars(tmp_path: Path):
    """Legacy dirs fetched before the period sidecar field carry no recorded
    period, so selection falls back to newest-by-mtime (the pre-existing
    behaviour) — the fresh issue must still win."""
    import os

    ind = {"id": "money_multiplier", "fetch": {"type": "pdf", "url": "https://bb/mei"}}
    month = tmp_path / "_pdfs" / "money_multiplier" / "2026-06"
    month.mkdir(parents=True)
    older = month / "2026_april.pdf"
    newer = month / "2026_may.pdf"
    older.write_bytes(b"%PDF-april")
    newer.write_bytes(b"%PDF-may")  # no .meta.json sidecars — legacy dir
    now = datetime.now(timezone.utc).timestamp()
    os.utime(older, (now - 1000, now - 1000))
    os.utime(newer, (now, now))

    artifact = parse_all._load_artifact_for(ind, tmp_path)
    assert artifact is not None
    assert artifact.artifact_path.name == "2026_may.pdf"


def test_load_artifact_period_outranks_periodless_legacy_sibling(tmp_path: Path):
    """Transition case: a legacy period-less April file coexists with a fresh
    May file that recorded its period. The one WITH a period must win even if the
    legacy file happens to have the newer mtime."""
    import os

    ind = {"id": "money_multiplier", "fetch": {"type": "pdf", "url": "https://bb/mei"}}
    month = tmp_path / "_pdfs" / "money_multiplier" / "2026-06"
    month.mkdir(parents=True)
    legacy = month / "2026_april.pdf"
    fresh = month / "2026_may.pdf"
    legacy.write_bytes(b"%PDF-april")  # no sidecar (pre-change fetch)
    fresh.write_bytes(b"%PDF-may")
    fresh.with_suffix(".meta.json").write_text(json.dumps({"period": "2026-05"}))
    now = datetime.now(timezone.utc).timestamp()
    os.utime(fresh, (now - 1000, now - 1000))
    os.utime(legacy, (now, now))  # legacy file has the newer mtime

    artifact = parse_all._load_artifact_for(ind, tmp_path)
    assert artifact is not None
    assert artifact.artifact_path.name == "2026_may.pdf"


def _ok_result():
    return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")


def _fail_result(rc=1, stderr=""):
    return SimpleNamespace(returncode=rc, stdout="", stderr=stderr)


def test_preflight_returns_true_on_first_success():
    with patch("parse_all.subprocess.run", return_value=_ok_result()) as run, \
         patch("parse_all.time.sleep") as sleep:
        assert parse_all._claude_preflight() is True
    assert run.call_count == 1
    sleep.assert_not_called()


def test_preflight_retries_then_succeeds():
    seq = [_fail_result(rc=1), _ok_result()]
    with patch("parse_all.subprocess.run", side_effect=seq) as run, \
         patch("parse_all.time.sleep") as sleep:
        assert parse_all._claude_preflight() is True
    assert run.call_count == 2
    assert sleep.call_count == 1
    assert sleep.call_args.args[0] == parse_all._PREFLIGHT_BACKOFF_SEC[0]


def test_preflight_exhausts_attempts_then_returns_false():
    with patch("parse_all.subprocess.run", return_value=_fail_result(rc=1)) as run, \
         patch("parse_all.time.sleep") as sleep:
        assert parse_all._claude_preflight() is False
    assert run.call_count == parse_all._PREFLIGHT_MAX_ATTEMPTS
    assert sleep.call_count == parse_all._PREFLIGHT_MAX_ATTEMPTS - 1


def test_preflight_handles_timeout_and_retries():
    seq = [subprocess.TimeoutExpired(cmd=["claude"], timeout=60), _ok_result()]
    with patch("parse_all.subprocess.run", side_effect=seq) as run, \
         patch("parse_all.time.sleep"):
        assert parse_all._claude_preflight() is True
    assert run.call_count == 2


def test_preflight_logs_stdout_stderr_and_exit(caplog):
    failing = _fail_result(rc=7, stderr="boom")
    failing.stdout = "partial-stdout"
    with patch("parse_all.subprocess.run", return_value=failing), \
         patch("parse_all.time.sleep"):
        with caplog.at_level("ERROR", logger="parse_all"):
            parse_all._claude_preflight()
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "exited 7" in joined
    assert "partial-stdout" in joined
    assert "boom" in joined
