"""E2.5 — deterministic zero-rows / high-failure floors."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import fetch_all
import parse_all
from utils.floor import assess_fetch_floor, assess_parse_floor

# --- pure verdicts ----------------------------------------------------------

def test_fetch_floor_breaches_on_total_outage():
    assert assess_fetch_floor(due=74, fetched=0).breached is True


def test_fetch_floor_breaches_when_majority_fail():
    v = assess_fetch_floor(due=10, fetched=4)  # 6 failed = 60% > 50%
    assert v.breached is True


def test_fetch_floor_tolerates_a_few_flaky_sources():
    v = assess_fetch_floor(due=10, fetched=9)  # 1 failed = 10%
    assert v.breached is False


def test_fetch_floor_no_due_never_breaches():
    assert assess_fetch_floor(due=0, fetched=0).breached is False


def test_parse_floor_breaches_on_zero_snapshots():
    assert assess_parse_floor(due=74, produced=0).breached is True


def test_parse_floor_breaches_below_min_rate():
    assert assess_parse_floor(due=10, produced=4).breached is True   # 40% < 50%


def test_parse_floor_healthy_at_majority():
    assert assess_parse_floor(due=10, produced=8).breached is False


# --- main() wiring ----------------------------------------------------------

def _write_cfg(tmp_path, n):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"indicators": [{"id": f"m{i}"} for i in range(n)]}))
    return p


def test_fetch_main_alerts_on_outage(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, 4)
    monkeypatch.setattr(fetch_all, "run", lambda **k: [])
    calls = []
    monkeypatch.setattr(fetch_all, "notify", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(sys, "argv",
                        ["fetch_all", "--config", str(cfg), "--data-root", str(tmp_path)])
    assert fetch_all.main() == 0
    assert calls and calls[0][0] == "error"
    assert "fetch floor" in calls[0][1]


def test_fetch_main_silent_when_healthy(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, 4)
    monkeypatch.setattr(fetch_all, "run", lambda **k: [SimpleNamespace(cache_hit=False)] * 4)
    calls = []
    monkeypatch.setattr(fetch_all, "notify", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(sys, "argv",
                        ["fetch_all", "--config", str(cfg), "--data-root", str(tmp_path)])
    assert fetch_all.main() == 0
    assert calls == []


def test_parse_main_alerts_when_empty(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, 4)
    monkeypatch.setattr(parse_all, "run", lambda **k: [])
    calls = []
    monkeypatch.setattr(parse_all, "notify", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(sys, "argv",
                        ["parse_all", "--config", str(cfg), "--data-root", str(tmp_path),
                         "--skip-claude-preflight"])
    assert parse_all.main() == 0
    assert calls and calls[0][0] == "error"
    assert "parse floor" in calls[0][1]


def test_parse_main_silent_when_healthy(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, 4)
    monkeypatch.setattr(parse_all, "run", lambda **k: [{"_provenance": "ok"}] * 4)
    calls = []
    monkeypatch.setattr(parse_all, "notify", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(sys, "argv",
                        ["parse_all", "--config", str(cfg), "--data-root", str(tmp_path),
                         "--skip-claude-preflight"])
    assert parse_all.main() == 0
    assert calls == []
