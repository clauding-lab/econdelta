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


def test_load_artifact_picks_newest_pdf_by_mtime(tmp_path: Path):
    """E1 leftover: a month-dir that accumulated a stale + fresh MEI issue (as
    ExonVPS's 2026-06/ held 2026_april.pdf AND 2026_may.pdf) must parse the
    NEWEST-fetched one, not an arbitrary glob[0] that read stale April."""
    import os

    ind = {"id": "money_multiplier", "fetch": {"type": "pdf", "url": "https://bb/mei"}}
    month = tmp_path / "_pdfs" / "money_multiplier" / "2026-06"
    month.mkdir(parents=True)
    stale = month / "2026_april.pdf"
    fresh = month / "2026_may.pdf"
    stale.write_bytes(b"%PDF-april")
    fresh.write_bytes(b"%PDF-may")
    now = datetime.now(timezone.utc).timestamp()
    os.utime(stale, (now - 1000, now - 1000))  # April fetched earlier
    os.utime(fresh, (now, now))                 # May fetched when BB published it

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
