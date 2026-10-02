"""Night-1 final backup: the twelve-table repair snapshot in the 25 Sep layout.

The 25 Sep 2026 backup that R1 (12abc596...) was built from was a one-off export. These tests pin
the committed tool that rewrites that layout: ``<table>.json`` per table (a JSON array, indent 2,
non-ASCII kept as UTF-8, trailing newline, rows in server key order) plus ``manifest.json`` with
``target_project, started_at, non_transactional, key_role, tables, finished_at``. All rows here
are SYNTHETIC.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

import scripts.export_history as eh
from scripts.export_history import ExportError, export_repair_snapshot, main

_PROJECT = "ssbliukchgibjcjohibi"
_URL = f"https://{_PROJECT}.supabase.co"
_SERVICE_KEY = "sb_secret_synthetic-service-key"  # SYNTHETIC opaque secret-key shape

_TWELVE = (
    "metric_history", "metric_history_monthly", "metric_definitions",
    "metric_definitions_monthly", "auction_results", "media_review",
    "briefs", "sections", "metrics", "news", "chart_series", "chart_notes",
)
_KEYS_25_SEP = {
    "metric_history": ["metric_id", "as_of"],
    "metric_history_monthly": ["metric_id", "as_of"],
    "metric_definitions": ["metric_id"],
    "metric_definitions_monthly": ["metric_id"],
    "auction_results": ["auction_date", "tenor"],
    "media_review": ["id"],
    "briefs": ["id"],
    "sections": ["id"],
    "metrics": ["id"],
    "news": ["id"],
    "chart_series": ["id"],
    "chart_notes": ["id"],
}


def _rows(table: str) -> list[dict]:
    """Two SYNTHETIC rows keyed per the 25 Sep manifest; one string carries non-ASCII text."""
    out = []
    for n in (1, 2):
        row: dict = {column: f"{table}-{column}-{n}" for column in _KEYS_25_SEP[table]}
        if table == "media_review":
            row["id"] = n  # integer key, as in the real table
        row["note"] = "Tk 1,000 crore — ৳ taka" if n == 1 else None
        row["value"] = 1.5 * n
        out.append(row)
    return out


def _env(monkeypatch) -> None:
    for var in ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_KEY", "SUPABASE_ANON_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SUPABASE_URL", _URL)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", _SERVICE_KEY)


def _expected_table_bytes(rows: list[dict]) -> bytes:
    """How every 25 Sep table file is written (verified byte-for-byte on the real backup)."""
    return (json.dumps(rows, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def test_r1_final_backup_writes_the_twelve_tables_in_the_25_sep_order_layout_and_bytes(
    tmp_path, monkeypatch
):
    _env(monkeypatch)
    calls = []

    def fetch(table, key):
        calls.append((table, key))
        return _rows(table)

    started = datetime(2026, 10, 3, 18, 0, 1, tzinfo=timezone.utc)
    finished = datetime(2026, 10, 3, 18, 0, 45, tzinfo=timezone.utc)
    out = tmp_path / "night1-final"

    def clock():
        # finished_at closes the writer-paused read window: stamped only after all twelve reads.
        assert len(calls) == len(_TWELVE), "finished_at stamped before every table was read"
        return finished

    export_repair_snapshot(out, eh.R1_FINAL_BACKUP_TABLES, fetch, now=started, clock=clock)

    assert eh.R1_FINAL_BACKUP_TABLES == _TWELVE
    assert calls == [(table, _SERVICE_KEY) for table in _TWELVE]
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [f"{t}.json" for t in _TWELVE] + ["manifest.json"]
    )
    raw_manifest = (out / "manifest.json").read_bytes()
    manifest = json.loads(raw_manifest)
    assert list(manifest) == [
        "target_project", "started_at", "non_transactional", "key_role", "tables", "finished_at"
    ]
    assert raw_manifest == (json.dumps(manifest, indent=2) + "\n").encode()
    assert manifest["target_project"] == _PROJECT
    assert manifest["started_at"] == started.isoformat()
    assert manifest["finished_at"] == finished.isoformat()
    assert manifest["non_transactional"] is True and manifest["key_role"] == "service"
    assert list(manifest["tables"]) == list(_TWELVE)
    for table in _TWELVE:
        raw = (out / f"{table}.json").read_bytes()
        assert raw == _expected_table_bytes(_rows(table)), table
        assert manifest["tables"][table] == {
            "rows": 2, "sha256": hashlib.sha256(raw).hexdigest(), "key": _KEYS_25_SEP[table]
        }


@pytest.mark.parametrize("table", ["briefs", "sections", "metrics", "news", "chart_series",
                                   "chart_notes"])
def test_repair_snapshot_accepts_each_brief_table_keyed_by_id(tmp_path, monkeypatch, table):
    _env(monkeypatch)
    out = tmp_path / f"snap-{table}"
    export_repair_snapshot(out, (table,), lambda t, key: _rows(t))
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["tables"][table]["key"] == ["id"]


def test_routine_export_keeps_its_six_tables_and_never_reads_a_brief_table(tmp_path):
    fetched = []
    eh.export_history(tmp_path, fetcher=lambda table: fetched.append(table) or [])
    assert fetched == ["metric_history_monthly", "metric_history", "metric_definitions",
                       "metric_definitions_monthly", "auction_results", "media_review"]
    assert set(eh._TABLE_KEYS) == set(fetched)

    class NoNetwork:
        def get(self, url, **kwargs):
            raise AssertionError("must refuse before any read")

    with pytest.raises(ExportError, match="unsupported table"):
        eh.paginate_table("briefs", url=_URL, key="k", session=NoNetwork())


def test_live_repair_read_of_a_brief_table_orders_and_checks_by_id(tmp_path, monkeypatch):
    """The default (non-injected) path pages a Brief table through paginate_table by its id key."""
    _env(monkeypatch)
    urls = []
    pages = [_rows("chart_notes"), []]

    class Session:
        def get(self, url, **kwargs):
            urls.append(url)
            page = pages.pop(0)
            return type("Response", (), {"status_code": 200, "json": lambda _: page})()

    monkeypatch.setattr(eh.requests, "Session", Session)
    out = tmp_path / "snap"
    export_repair_snapshot(out, ("chart_notes",))
    assert urls[0].startswith(f"{_URL}/rest/v1/chart_notes?select=*&order=id.asc&limit=1000")
    assert json.loads((out / "chart_notes.json").read_text()) == _rows("chart_notes")


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([{"id": "a"}, {"id": "a"}], id="duplicate-id"),
        pytest.param([{"id": "a"}, {"id": None}], id="null-id"),
        pytest.param([{"id": "a"}, {"other": 1}], id="missing-id"),
    ],
)
def test_live_read_refuses_a_bad_brief_key_before_creating_the_folder(tmp_path, monkeypatch, rows):
    """paginate_table's key checks run on a Brief table's id (the repair key map), live path."""
    _env(monkeypatch)
    pages = [rows, []]

    class Session:
        def get(self, url, **kwargs):
            page = pages.pop(0)
            return type("Response", (), {"status_code": 200, "json": lambda _: page})()

    monkeypatch.setattr(eh.requests, "Session", Session)
    out = tmp_path / "snap"
    with pytest.raises(ExportError, match="read briefs: (duplicate key|missing row key)"):
        export_repair_snapshot(out, ("briefs",))
    assert not out.exists()


def test_repair_snapshot_refuses_a_table_named_twice(tmp_path, monkeypatch):
    _env(monkeypatch)
    out = tmp_path / "snap"
    with pytest.raises(ExportError, match="more than once"):
        export_repair_snapshot(out, ("news", "news"), lambda t, key: _rows(t))
    assert not out.exists()


def _patched_reader(monkeypatch) -> list:
    calls = []

    def paginate(table, *, url=None, key=None, **kwargs):
        calls.append(table)
        return _rows(table)

    monkeypatch.setattr(eh, "paginate_table", paginate)
    return calls


def test_cli_r1_final_backup_selects_exactly_the_twelve_tables_in_order(tmp_path, monkeypatch):
    _env(monkeypatch)
    calls = _patched_reader(monkeypatch)
    out = tmp_path / "night1-final"
    assert main(["--repair-snapshot", str(out), "--r1-final-backup"]) == 0
    assert calls == list(_TWELVE)
    assert list(json.loads((out / "manifest.json").read_text())["tables"]) == list(_TWELVE)


def test_cli_table_is_repeatable_and_keeps_the_given_order(tmp_path, monkeypatch):
    _env(monkeypatch)
    calls = _patched_reader(monkeypatch)
    out = tmp_path / "snap"
    assert main(["--repair-snapshot", str(out), "--table", "news", "--table", "briefs"]) == 0
    assert calls == ["news", "briefs"]


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["--r1-final-backup", "--table", "metric_history"], id="with-table"),
        pytest.param(["--r1-final-backup", "--no-snapshot-dir"], id="without-repair-snapshot"),
    ],
)
def test_cli_r1_final_backup_refuses_a_mixed_or_incomplete_call(tmp_path, monkeypatch, argv):
    _env(monkeypatch)
    calls = _patched_reader(monkeypatch)
    out = tmp_path / "snap"
    if "--no-snapshot-dir" in argv:
        args = ["--r1-final-backup", "--out-dir", str(tmp_path / "routine")]
    else:
        args = ["--repair-snapshot", str(out), *argv]
    assert main(args) == 1
    assert calls == [] and not out.exists() and not (tmp_path / "routine").exists()
