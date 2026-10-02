"""Refresh a reviewed R1 manifest's before-images from the writer-paused Night-1 backup.

Why (2 Oct 2026 BDT): the reviewed R1 manifest was built from the 25 Sep backup, but the old
nightly producer re-upserts some rows (same value, a new ``ingested_at``), so the engine's
whole-batch before-image check can never pass. The refresh may move a before-image only when the
backup row differs in ``ingested_at`` alone, or in ``ingested_at`` + ``value`` for a key the owner
listed in ``--rebind`` with the exact old and current values. The reviewed ``after`` rows never
change. Every other difference is a refusal, never a filter. All rows here are SYNTHETIC.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.export_history import R1_FINAL_BACKUP_TABLES, export_repair_snapshot
from scripts.repair_observation_history import KEYS, apply_manifest
from scripts.repair_observation_history import main as engine_main
from scripts.repair_refresh_before_images import main

_PROJECT = "ssbliukchgibjcjohibi"
_TARGET = f"supabase:{_PROJECT}"
_OLD_TS = "2026-09-20T21:18:08.437914+00:00"
_NEW_TS = "2026-10-01T21:05:00.123456+00:00"
_SECRET = 424242.125  # SYNTHETIC value that must never reach the report or stdout

_A = {"metric_id": "tax_revenue", "as_of": "2026-09-20", "value": 415473.0, "source": "EconDelta",
      "provenance": None, "ingested_at": _OLD_TS}
_B = {"metric_id": "dsex", "as_of": "2026-09-21", "value": 5300.5, "source": "DSE",
      "provenance": None, "ingested_at": _OLD_TS}
_B_AFTER = _B | {"value": 5311.25}
_LC = {"metric_id": "monthly_import_lc_opening", "as_of": "2026-07-31", "value": 6067.72,
       "source": "BB", "provenance": None, "ingested_at": _OLD_TS}
_LC_AFTER = _LC | {"value": 6901.7}
_LC_NOW = _LC | {"value": 6623.97, "ingested_at": _NEW_TS}
_DEF = {"metric_id": "fx_reserve_gross_and_bpm6", "label": "FX reserves", "unit": "USD bn",
        "updated_at": "2026-05-04T15:15:36.629102+00:00"}
_INSERT = {"metric_id": "cpi_headline_monthly", "as_of": "2026-08-01", "value": 8.29,
           "source": "BBS"}
_REBIND = [{"table": "metric_history",
            "key": {"metric_id": "monthly_import_lc_opening", "as_of": "2026-07-31"},
            "before_value": 6067.72, "current_value": 6623.97}]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(**overrides: list[dict]) -> dict[str, list[dict]]:
    rows = {table: [{"id": f"{table}-1", "note": "x"}] for table in R1_FINAL_BACKUP_TABLES}
    rows["media_review"] = [{"id": 1, "kind": "fresher_period"}]
    rows["auction_results"] = [{"auction_date": "2026-09-23", "tenor": "2y", "cutoff": 10.7}]
    rows["metric_history"] = [_A, _B | {"ingested_at": _NEW_TS}, _LC_NOW]
    rows["metric_history_monthly"] = [{"metric_id": "cpi_headline_monthly", "as_of": "2026-07-01",
                                       "value": 8.48, "source": "BBS"}]
    rows["metric_definitions"] = [_DEF]
    rows["metric_definitions_monthly"] = [{"metric_id": "bb_repo_rate_monthly", "unit": "%"}]
    rows.update(overrides)
    return rows


def _backup(tmp_path: Path, monkeypatch, *, started: datetime | None = None,
            **overrides: list[dict]) -> Path:
    for var in ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_KEY", "SUPABASE_ANON_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SUPABASE_URL", f"https://{_PROJECT}.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "sb_secret_synthetic-service-key")
    rows = _rows(**overrides)
    started = started or datetime(2026, 10, 3, 17, 30, tzinfo=timezone.utc)
    return export_repair_snapshot(
        tmp_path / "night1-final", R1_FINAL_BACKUP_TABLES, lambda table, key: rows[table],
        now=started, clock=lambda: started.replace(minute=31),
    )


def _op(n: int, table: str, before: dict | None, after: dict | None, evidence: list) -> dict:
    row = before or after
    assert row is not None
    return {"operation_id": f"{table}:op-{n}", "table": table,
            "key": {k: row[k] for k in KEYS[table]}, "before": before, "after": after,
            "reason": f"SYNTHETIC reason {n}", "evidence": evidence, "requires": []}


def _manifest(tmp_path: Path, *, operations: list[dict] | None = None,
              generated_at: str = "2026-09-28T22:11:12.258399+00:00",
              backup_manifest_sha256: str = "1" * 64, project: str = _PROJECT,
              backups: tuple[str, ...] = R1_FINAL_BACKUP_TABLES) -> Path:
    source = tmp_path / "evidence.pdf"
    source.write_bytes(b"SYNTHETIC evidence")
    evidence = [{"path": str(source), "sha256": _sha(source), "locator": "page 1 row 2"}]
    ops = operations if operations is not None else [
        _op(1, "metric_history", _A, None, evidence),
        _op(2, "metric_history", _B, _B_AFTER, evidence),
        _op(3, "metric_history", _LC, _LC_AFTER, evidence),
        _op(4, "metric_definitions", _DEF, _DEF | {"label": "FX reserves (BPM6 gross)"},
            evidence),
        _op(5, "metric_history_monthly", None, _INSERT, evidence),
    ]
    for op in ops:
        op["evidence"] = op["evidence"] or evidence
    if operations is None:
        ops[-1]["requires"] = [ops[0]["operation_id"]]
    manifest = {
        "version": 1, "target_project": project, "target": f"supabase:{project}",
        "code_commits": {"econdelta": "a" * 40, "brief": "b" * 40},
        "generated_at": generated_at, "backup_manifest_sha256": backup_manifest_sha256,
        "backups": [{"table": t, "path": f"/old/2026-09-25-initial/{t}.json", "sha256": "0" * 64,
                     "rows": 1} for t in backups],
        "operations": ops, "unresolved": ["reviewed: earlier note"],
    }
    path = tmp_path / "r1-plan-manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    return path


def _rebind_file(tmp_path: Path, entries: list[dict] | None = None) -> Path:
    path = tmp_path / "r1-rebind.json"
    path.write_text(json.dumps(_REBIND if entries is None else entries))
    return path


def _run(manifest: Path, backup: Path, out: Path, rebind: Path, *extra: str,
         sha: str | None = None) -> int:
    return main(["--manifest", str(manifest), "--expected-sha256", sha or _sha(manifest),
                 "--backup-dir", str(backup), "--rebind", str(rebind), "--out", str(out),
                 *extra])


def _head() -> str:
    root = Path(__file__).resolve().parent.parent
    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


# --- the happy path: unchanged, ingested_at-only refresh, one listed re-bind -------------------


def test_refreshes_ingested_at_and_listed_rebind_and_keeps_every_after_row_byte_identical(
    tmp_path, monkeypatch, capsys
):
    backup = _backup(tmp_path, monkeypatch)
    manifest = _manifest(tmp_path)
    inputs = {p: _sha(p) for p in [manifest, *backup.iterdir()]}
    out, report = tmp_path / "candidate.json", tmp_path / "report.json"

    assert _run(manifest, backup, out, _rebind_file(tmp_path), "--report", str(report)) == 0

    old, new = json.loads(manifest.read_text()), json.loads(out.read_text())
    ops = {op["operation_id"]: op for op in new["operations"]}
    assert ops["metric_history:op-1"]["before"] == _A  # unchanged
    assert ops["metric_history:op-2"]["before"] == _B | {"ingested_at": _NEW_TS}  # refreshed
    assert ops["metric_history:op-3"]["before"] == _LC_NOW  # owner re-bind
    assert ops["metric_definitions:op-4"]["before"] == _DEF
    assert ops["metric_history_monthly:op-5"]["before"] is None
    for was, now in zip(old["operations"], new["operations"], strict=True):
        assert json.dumps(now["after"]) == json.dumps(was["after"])  # after: byte-identical
        for field in ("operation_id", "table", "key", "reason", "evidence", "requires"):
            assert now[field] == was[field]
    backup_manifest = json.loads((backup / "manifest.json").read_text())
    assert new["backups"] == [
        {"table": t, "path": str((backup / f"{t}.json").resolve()),
         "sha256": backup_manifest["tables"][t]["sha256"],
         "rows": backup_manifest["tables"][t]["rows"]} for t in R1_FINAL_BACKUP_TABLES]
    assert new["backup_manifest_sha256"] == _sha(backup / "manifest.json")
    generated = datetime.fromisoformat(new["generated_at"])  # now, in UTC
    assert generated.utcoffset().total_seconds() == 0
    assert abs((datetime.now(timezone.utc) - generated).total_seconds()) < 120
    assert new["code_commits"] == old["code_commits"]
    assert new["target"] == old["target"] and new["version"] == 1
    assert new["unresolved"][:-1] == old["unresolved"]
    note = new["unresolved"][-1]
    for fragment in (_sha(backup / "manifest.json"), _head(), _sha(manifest), "3 unchanged",
                     "1 ingested_at-refreshed", "1 re-bound"):
        assert fragment in note
    assert {p: _sha(p) for p in inputs} == inputs  # read-only toward every input

    data = json.loads(report.read_text())
    assert data["counts"] == {"unchanged": 3, "ingested_at_refreshed": 1, "rebound": 1,
                              "refused": 0, "operations": 5}
    assert data["refreshed"] == [{"table": "metric_history",
                                  "key": {"metric_id": "dsex", "as_of": "2026-09-21"}}]
    assert data["rebound"] == [_REBIND[0]]
    assert data["input_manifest_sha256"] == _sha(manifest)
    assert data["output_sha256"] == _sha(out)
    assert data["backup_manifest_sha256"] == _sha(backup / "manifest.json")
    stdout = capsys.readouterr().out
    assert "3 unchanged, 1 ingested_at-refreshed, 1 re-bound, 0 refused" in stdout
    assert "dsex 2026-09-21" in stdout and "6067.72" in stdout and "6623.97" in stdout
    for secret in ("5300.5", "5311.25", "415473", "6901.7"):  # non-re-bind values: never shown
        assert secret not in stdout and secret not in report.read_text()


def test_output_passes_the_engine_plan_and_applies_cleanly_to_the_refreshed_database(
    tmp_path, monkeypatch, capsys
):
    backup = _backup(tmp_path, monkeypatch)
    out = tmp_path / "candidate.json"
    assert _run(_manifest(tmp_path), backup, out, _rebind_file(tmp_path)) == 0
    capsys.readouterr()

    assert engine_main(["--plan", "--candidate", str(out), "--target", _TARGET,
                        "--expected-sha256", _sha(out), "--out-dir",
                        str(tmp_path / "plan")]) == 0
    assert "Validated 5 operations" in capsys.readouterr().out

    store = _Store(backup)
    apply_manifest(out, expected_sha256=_sha(out), target=_TARGET, store=store,
                   receipts_path=tmp_path / "receipts" / "receipts.json")
    for op in json.loads(out.read_text())["operations"]:
        assert store.get(op["table"], op["key"]) == op["after"]
    assert store.writes == 5


class _Store:
    """In-memory four-table store loaded from the backup files: the engine's Store protocol.

    tests/test_r1_nbr_exclusion_release_order.MetricHistory holds metric_history only, and this
    manifest also touches metric_definitions and metric_history_monthly."""

    target = _TARGET

    def __init__(self, backup: Path) -> None:
        self.rows = {
            (table, tuple(r[k] for k in KEYS[table])): r
            for table in KEYS
            for r in json.loads((backup / f"{table}.json").read_text())
        }
        self.writes = 0

    def get(self, table: str, key: dict) -> dict | None:
        return copy.deepcopy(self.rows.get((table, tuple(key[k] for k in KEYS[table]))))

    def change(self, op: dict) -> None:
        identity = (op["table"], tuple(op["key"][k] for k in KEYS[op["table"]]))
        if op["after"] is None:
            del self.rows[identity]
        else:
            self.rows[identity] = copy.deepcopy(op["after"])
        self.writes += 1


def test_database_equivalent_timestamp_spelling_counts_as_unchanged(tmp_path, monkeypatch):
    """Per-column comparison uses the engine's own same(): Z vs +00:00 is not a difference."""
    backup = _backup(tmp_path, monkeypatch, metric_history=[
        _A | {"ingested_at": "2026-09-20T21:18:08.437914Z", "value": 415473},
        _B | {"ingested_at": _NEW_TS}, _LC_NOW])
    out, report = tmp_path / "candidate.json", tmp_path / "report.json"

    assert _run(_manifest(tmp_path), backup, out, _rebind_file(tmp_path),
                "--report", str(report)) == 0

    assert json.loads(report.read_text())["counts"]["unchanged"] == 3


# --- every deviation is a refusal, never a filter -----------------------------------------------


def _mh(*rows: dict) -> dict:
    return {"metric_history": list(rows)}


_LC_KEY = "monthly_import_lc_opening 2026-07-31"

_REFUSALS = [
    pytest.param(_mh(_A, _B | {"ingested_at": _NEW_TS, "value": _SECRET}, _LC_NOW), None,
                 "metric_history dsex 2026-09-21 differs in ingested_at, value",
                 id="unlisted-value-change"),
    pytest.param(_mh(_A, _B | {"source": "SECRET-SOURCE"}, _LC_NOW), None,
                 "metric_history dsex 2026-09-21 differs in source", id="other-column-change"),
    # A new ingested_at never excuses another changed column: only diff == {ingested_at} refreshes.
    pytest.param(_mh(_A, _B | {"ingested_at": _NEW_TS, "source": "SECRET-SOURCE"}, _LC_NOW), None,
                 "metric_history dsex 2026-09-21 differs in ingested_at, source",
                 id="ingested-at-plus-other-column"),
    # A listed re-bind covers exactly {ingested_at, value}; a third changed column refuses.
    pytest.param(_mh(_A, _B | {"ingested_at": _NEW_TS}, _LC_NOW | {"source": "SECRET-SOURCE"}),
                 None, f"metric_history {_LC_KEY} differs in ingested_at, source, value",
                 id="rebind-plus-other-column"),
    pytest.param(_mh(_A, _B | {"extra": "SECRET-COLUMN"}, _LC_NOW), None,
                 "metric_history dsex 2026-09-21 column set differs", id="column-set-change"),
    pytest.param(_mh(_A, _B | {"value": _SECRET}, _LC_NOW), None,
                 "metric_history dsex 2026-09-21 differs in value", id="value-only-change"),
    pytest.param({}, [_REBIND[0] | {"before_value": 6067.73}],
                 f"metric_history {_LC_KEY} differs in ingested_at, value",
                 id="rebind-wrong-before-value"),
    pytest.param({}, [_REBIND[0] | {"current_value": 6623.98}],
                 f"metric_history {_LC_KEY} differs in ingested_at, value",
                 id="rebind-wrong-current-value"),
    pytest.param({}, [], f"metric_history {_LC_KEY} differs in ingested_at, value",
                 id="rebind-not-listed"),
    pytest.param({}, [*_REBIND, {"table": "metric_history",
                                 "key": {"metric_id": "tax_revenue", "as_of": "2026-09-20"},
                                 "before_value": 1.0, "current_value": 2.0}],
                 "unused re-bind entry: metric_history tax_revenue 2026-09-20",
                 id="unused-rebind-entry"),
    pytest.param({}, [*_REBIND, *_REBIND], "duplicate re-bind entry", id="duplicate-rebind"),
    pytest.param({}, [_REBIND[0] | {"note": "x"}], "re-bind entry", id="malformed-rebind"),
    pytest.param({}, [_REBIND[0] | {"key": {"metric_id": "monthly_import_lc_opening"}}],
                 "re-bind entry", id="rebind-partial-key"),
    pytest.param({"metric_history_monthly": [_INSERT]}, None,
                 "metric_history_monthly cpi_headline_monthly 2026-08-01 insert key is present",
                 id="insert-key-present"),
    pytest.param(_mh(_B | {"ingested_at": _NEW_TS}, _LC_NOW), None,
                 "metric_history tax_revenue 2026-09-20 before-image row is missing",
                 id="missing-row"),
]


@pytest.mark.parametrize(("overrides", "rebind", "refusal"), _REFUSALS)
def test_refuses_any_deviation_naming_table_key_and_columns_and_writes_nothing(
    tmp_path, monkeypatch, capsys, overrides, rebind, refusal
):
    backup = _backup(tmp_path, monkeypatch, **overrides)
    out, report = tmp_path / "candidate.json", tmp_path / "report.json"

    assert _run(_manifest(tmp_path), backup, out, _rebind_file(tmp_path, rebind),
                "--report", str(report)) == 1

    stdout = capsys.readouterr().out
    assert refusal in stdout
    assert not out.exists() and not report.exists()
    for secret in (str(_SECRET), "SECRET-SOURCE", "SECRET-COLUMN", "5300.5", "415473"):
        assert secret not in stdout


def test_refuses_a_refresh_that_would_make_the_before_equal_the_reviewed_after(
    tmp_path, monkeypatch, capsys
):
    """A re-bind onto the reviewed target itself would turn the operation into a no-op."""
    after = _LC | {"value": 6901.7, "ingested_at": _NEW_TS}
    backup = _backup(tmp_path, monkeypatch, metric_history=[_A, after])
    manifest = _manifest(tmp_path, operations=[_op(1, "metric_history", _A, None, []),
                                               _op(2, "metric_history", _LC, after, [])])
    out = tmp_path / "candidate.json"

    assert _run(manifest, backup, out, _rebind_file(
        tmp_path, [_REBIND[0] | {"current_value": 6901.7}])) == 1

    assert f"metric_history {_LC_KEY} refreshed before equals the reviewed after" in (
        capsys.readouterr().out)
    assert not out.exists()


def _edit_backup_manifest(backup: Path, **edits) -> None:
    manifest = json.loads((backup / "manifest.json").read_text())
    for key, value in edits.items():
        if value is None:
            del manifest[key]
        else:
            manifest[key] = value
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def _input_refusals():
    def source_backup(t, b):
        return _manifest(t, backup_manifest_sha256=_sha(b / "manifest.json")), None

    def older_backup(t, b):
        return _manifest(t, generated_at="2026-10-03T23:30:00+06:00"), None  # same instant

    def no_key_role(t, b):
        _edit_backup_manifest(b, key_role=None)
        return _manifest(t), None

    def anon_key_role(t, b):
        _edit_backup_manifest(b, key_role="anon")
        return _manifest(t), None

    def other_project(t, b):
        return _manifest(t, project="abcdefghijklmnopqrst"), None

    def wrong_sha(t, b):
        return _manifest(t), "f" * 64

    def table_hash(t, b):
        (b / "metric_history.json").write_bytes(b"[]\n")
        return _manifest(t), None

    def table_missing(t, b):
        manifest = json.loads((b / "manifest.json").read_text())
        del manifest["tables"]["chart_notes"]
        (b / "manifest.json").write_text(json.dumps(manifest))
        return _manifest(t), None

    def op_table_not_in_manifest_backups(t, b):
        return _manifest(t, backups=tuple(x for x in R1_FINAL_BACKUP_TABLES
                                          if x != "metric_definitions")), None

    return [
        pytest.param(source_backup, "backup-is-candidate-source", id="source-backup"),
        pytest.param(older_backup, "backup-not-after-candidate", id="backup-not-after-manifest"),
        pytest.param(no_key_role, "key-role-not-service", id="no-key-role"),
        pytest.param(anon_key_role, "key_role", id="anon-key-role"),
        pytest.param(other_project, "target project", id="other-project"),
        pytest.param(wrong_sha, "hash mismatch", id="wrong-manifest-sha"),
        pytest.param(table_hash, "hash mismatch: metric_history.json", id="backup-table-hash"),
        pytest.param(table_missing, "chart_notes", id="backup-lacks-a-manifest-table"),
        pytest.param(op_table_not_in_manifest_backups, "metric_definitions",
                     id="op-table-not-in-manifest-backups"),
    ]


@pytest.mark.parametrize(("setup", "refusal"), _input_refusals())
def test_refuses_an_unverifiable_or_ineligible_input_and_writes_nothing(
    tmp_path, monkeypatch, capsys, setup, refusal
):
    backup = _backup(tmp_path, monkeypatch)
    manifest, sha = setup(tmp_path, backup)
    out = tmp_path / "candidate.json"

    assert _run(manifest, backup, out, _rebind_file(tmp_path), sha=sha) == 1

    assert refusal in capsys.readouterr().out
    assert not out.exists()


def test_refuses_an_existing_out_and_leaves_it_untouched(tmp_path, monkeypatch, capsys):
    backup = _backup(tmp_path, monkeypatch)
    out = tmp_path / "candidate.json"
    out.write_text("earlier candidate")

    assert _run(_manifest(tmp_path), backup, out, _rebind_file(tmp_path)) == 1

    assert "already exists" in capsys.readouterr().out
    assert out.read_text() == "earlier candidate"


def test_refuses_an_existing_report_and_writes_no_candidate(tmp_path, monkeypatch, capsys):
    backup = _backup(tmp_path, monkeypatch)
    out, report = tmp_path / "candidate.json", tmp_path / "report.json"
    report.write_text("earlier report")

    assert _run(_manifest(tmp_path), backup, out, _rebind_file(tmp_path),
                "--report", str(report)) == 1

    assert "already exists" in capsys.readouterr().out
    assert not out.exists() and report.read_text() == "earlier report"
