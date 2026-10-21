"""Night-1 recheck: every R1 before-image against the writer-paused final twelve-table backup.

Runbook "Safety contract" item 4 (docs/reviews/2026-09-25-history-repair-manifest.md): before R1
runs, take a final backup and recheck complete before-images. A reviewed operation whose
``before`` row differs from the frozen database (or an insert whose key now exists) needs a fresh
proposal, never a force flag. All rows here are SYNTHETIC.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.export_history import R1_FINAL_BACKUP_TABLES, export_repair_snapshot
from scripts.repair_recheck_before_images import main

_PROJECT = "ssbliukchgibjcjohibi"
_SECRET_VALUE = 987654.321  # SYNTHETIC: a changed value that must never reach any output

_A = {"metric_id": "tax_revenue", "as_of": "2026-09-20", "value": 415473.0,
      "source": "EconDelta", "provenance": None,
      "ingested_at": "2026-09-20T21:18:08.437914+00:00"}
_B = {"metric_id": "dsex", "as_of": "2026-09-21", "value": 5300.5, "source": "DSE",
      "provenance": None, "ingested_at": "2026-09-21T09:00:00+00:00"}
_DEF = {"metric_id": "fx_reserve_gross_and_bpm6", "label": "FX reserves", "unit": "USD bn"}
_INSERT = {"metric_id": "cpi_headline_monthly", "as_of": "2026-08-01", "value": 8.29,
           "source": "BBS"}
_KEYS = {"metric_history": ("metric_id", "as_of"),
         "metric_history_monthly": ("metric_id", "as_of"),
         "metric_definitions": ("metric_id",), "metric_definitions_monthly": ("metric_id",),
         "auction_results": ("auction_date", "tenor")}


def _tables(**overrides: list[dict]) -> dict[str, list[dict]]:
    rows = {table: [{"id": f"{table}-1", "note": "x"}] for table in R1_FINAL_BACKUP_TABLES}
    rows["media_review"] = [{"id": 1, "kind": "fresher_period"}]
    rows["auction_results"] = [{"auction_date": "2026-09-23", "tenor": "2y", "cutoff": 10.7}]
    rows["metric_history"] = [_A, _B]
    rows["metric_history_monthly"] = [{"metric_id": "cpi_headline_monthly", "as_of": "2026-07-01",
                                       "value": 8.48, "source": "BBS"}]
    rows["metric_definitions"] = [_DEF]
    rows["metric_definitions_monthly"] = [{"metric_id": "bb_repo_rate_monthly", "unit": "%"}]
    rows.update(overrides)
    return rows


def _backup(tmp_path: Path, monkeypatch, name: str = "night1-final", **overrides) -> Path:
    for var in ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_KEY", "SUPABASE_ANON_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SUPABASE_URL", f"https://{_PROJECT}.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "sb_secret_synthetic-service-key")
    rows = _tables(**overrides)
    return export_repair_snapshot(
        tmp_path / name, R1_FINAL_BACKUP_TABLES, lambda table, key: rows[table],
        now=datetime(2026, 10, 3, 17, 30, tzinfo=timezone.utc),
        clock=lambda: datetime(2026, 10, 3, 17, 31, tzinfo=timezone.utc),
    )


def _op(n: int, table: str, before: dict | None, after: dict | None) -> dict:
    row = before or after
    assert row is not None
    return {"operation_id": f"op-{n}", "table": table,
            "key": {k: row[k] for k in _KEYS[table]}, "before": before, "after": after,
            "reason": "SYNTHETIC", "evidence": [], "requires": []}


def _candidate(tmp_path: Path, *, project: str = _PROJECT, operations: list[dict] | None = None,
               backups: tuple[str, ...] = R1_FINAL_BACKUP_TABLES,
               generated_at: str = "2026-09-28T22:11:12+00:00",
               backup_manifest_sha256: str = "0" * 64, version: int = 1) -> Path:
    ops = operations if operations is not None else [
        _op(1, "metric_history", _A, None),
        _op(2, "metric_definitions", _DEF, _DEF | {"label": "FX reserves (BPM6 gross)"}),
        _op(3, "metric_history_monthly", None, _INSERT),
    ]
    candidate = {
        "version": version, "target_project": project, "target": f"supabase:{project}",
        "code_commits": {"econdelta": "a" * 40, "brief": "b" * 40},
        "generated_at": generated_at, "backup_manifest_sha256": backup_manifest_sha256,
        "backups": [{"table": t, "path": f"/old/{t}.json", "sha256": "0" * 64, "rows": 1}
                    for t in backups],
        "operations": ops, "unresolved": [],
    }
    path = tmp_path / "r1-candidate.json"
    path.write_text(json.dumps(candidate, indent=2))
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(candidate: Path, backup: Path, out: Path, *extra: str) -> int:
    return main(["--manifest", str(candidate), "--backup-dir", str(backup), "--out", str(out),
                 *extra])


def _rewrite(backup: Path, table: str, rows: list[dict], **manifest_edits) -> None:
    """Replace one table file and keep its manifest entry consistent (hash and rows)."""
    raw = (json.dumps(rows, indent=2) + "\n").encode()
    (backup / f"{table}.json").write_bytes(raw)
    manifest = json.loads((backup / "manifest.json").read_text())
    manifest["tables"][table].update(rows=len(rows), sha256=hashlib.sha256(raw).hexdigest())
    manifest.update(manifest_edits)
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def test_match_when_every_before_image_is_in_the_backup_and_every_insert_key_is_absent(
    tmp_path, monkeypatch, capsys
):
    backup = _backup(tmp_path, monkeypatch)
    candidate = _candidate(tmp_path)
    before = {p.name: _sha(p) for p in [candidate, *backup.iterdir()]}
    out = tmp_path / "receipt.json"

    assert _run(candidate, backup, out) == 0

    receipt = json.loads(out.read_text())
    assert receipt["result"] == "match" and receipt["mismatches"] == []
    assert receipt["operations"] == 3
    assert receipt["manifest_sha256"] == _sha(candidate)
    assert receipt["backup_manifest_sha256"] == _sha(backup / "manifest.json")
    assert receipt["backup_started_at"] == "2026-10-03T17:30:00+00:00"
    assert receipt["target_project"] == _PROJECT
    assert receipt["night1_eligible"] is True and receipt["reference_backup"] is False
    assert receipt["not_night1_eligible_because"] == []
    assert receipt["backup_is_candidate_source"] is False
    assert receipt["candidate_generated_at"] == "2026-09-28T22:11:12+00:00"
    assert receipt["counts"] == {
        "metric_history": {"operations": 1, "match": 1, "mismatch": 0},
        "metric_definitions": {"operations": 1, "match": 1, "mismatch": 0},
        "metric_history_monthly": {"operations": 1, "match": 1, "mismatch": 0},
    }
    assert [b["table"] for b in receipt["backups"]] == list(R1_FINAL_BACKUP_TABLES)
    stdout = capsys.readouterr().out
    assert "match" in stdout and f"receipt sha256={_sha(out)}" in stdout
    assert {p.name: _sha(p) for p in [candidate, *backup.iterdir()]} == before  # read-only


def test_database_equivalent_timestamps_and_numbers_still_match(tmp_path, monkeypatch):
    """The comparison is repair_observation_history.same (the apply engine's own rule)."""
    backup = _backup(tmp_path, monkeypatch)
    before = _A | {"value": 415473, "ingested_at": "2026-09-20T21:18:08.437914Z"}
    candidate = _candidate(tmp_path, operations=[_op(1, "metric_history", before, None)])
    assert _run(candidate, backup, tmp_path / "receipt.json") == 0


@pytest.mark.parametrize(
    ("overrides", "kind", "table", "key"),
    [
        pytest.param({"metric_history": [_A | {"value": _SECRET_VALUE}, _B]}, "changed",
                     "metric_history", {"metric_id": "tax_revenue", "as_of": "2026-09-20"},
                     id="value-changed"),
        pytest.param({"metric_history": [_A | {"source": "SECRET-SOURCE"}, _B]}, "changed",
                     "metric_history", {"metric_id": "tax_revenue", "as_of": "2026-09-20"},
                     id="other-column-changed"),
        pytest.param({"metric_definitions": [_DEF | {"extra": "SECRET-COLUMN"}]}, "changed",
                     "metric_definitions", {"metric_id": "fx_reserve_gross_and_bpm6"},
                     id="column-added"),
        pytest.param({"metric_history": [_B]}, "missing", "metric_history",
                     {"metric_id": "tax_revenue", "as_of": "2026-09-20"}, id="row-gone"),
        pytest.param({"metric_history_monthly": [_INSERT | {"value": _SECRET_VALUE}]},
                     "unexpected-present", "metric_history_monthly",
                     {"metric_id": "cpi_headline_monthly", "as_of": "2026-08-01"},
                     id="insert-key-taken"),
    ],
)
def test_mismatch_names_the_key_and_kind_and_never_a_row_value(
    tmp_path, monkeypatch, capsys, overrides, kind, table, key
):
    backup = _backup(tmp_path, monkeypatch, **overrides)
    out = tmp_path / "receipt.json"

    assert _run(_candidate(tmp_path), backup, out) == 1

    receipt = json.loads(out.read_text())
    assert receipt["result"] == "mismatch"
    assert receipt["mismatches"] == [{"table": table, "key": key, "kind": kind}]
    assert receipt["counts"][table]["mismatch"] == 1
    output = out.read_text() + capsys.readouterr().out
    for secret in (str(_SECRET_VALUE), "SECRET-SOURCE", "SECRET-COLUMN", "415473", "5300.5"):
        assert secret not in output
    assert kind in output


def _refusal_cases():
    def table_hash(b, c):
        (b / "metric_history.json").write_bytes(b"[]\n")

    def row_count(b, c):
        manifest = json.loads((b / "manifest.json").read_text())
        manifest["tables"]["metric_history"]["rows"] = 3
        (b / "manifest.json").write_text(json.dumps(manifest))

    def dup_key(b, c):
        _rewrite(b, "metric_history", [_A, _A | {"value": 1.0}])

    def null_key(b, c):
        _rewrite(b, "metric_definitions", [_DEF | {"metric_id": None}])

    def not_non_transactional(b, c):
        _rewrite(b, "news", [{"id": "news-1"}], non_transactional=False)

    def key_role_anon(b, c):
        _rewrite(b, "news", [{"id": "news-1"}], key_role="anon")

    def table_missing(b, c):
        manifest = json.loads((b / "manifest.json").read_text())
        del manifest["tables"]["chart_notes"]
        (b / "manifest.json").write_text(json.dumps(manifest))

    def wrong_key_columns(b, c):
        manifest = json.loads((b / "manifest.json").read_text())
        manifest["tables"]["metric_history"]["key"] = ["metric_id"]
        (b / "manifest.json").write_text(json.dumps(manifest))

    def op_table_not_backed_up(b, c):
        manifest = json.loads((b / "manifest.json").read_text())
        del manifest["tables"]["metric_history_monthly"]
        (b / "manifest.json").write_text(json.dumps(manifest))

    def no_started_at(b, c):
        manifest = json.loads((b / "manifest.json").read_text())
        del manifest["started_at"]
        (b / "manifest.json").write_text(json.dumps(manifest))

    def naive_started_at(b, c):
        _rewrite(b, "news", [{"id": "news-1"}], started_at="2026-10-03T17:30:00")

    def path_escape(b, c):
        manifest = json.loads((b / "manifest.json").read_text())
        manifest["tables"]["../escape"] = {"rows": 0, "sha256": "0" * 64, "key": ["id"]}
        (b / "manifest.json").write_text(json.dumps(manifest))

    return [
        pytest.param(table_hash, {}, "hash mismatch: metric_history.json", id="table-hash"),
        pytest.param(row_count, {}, "row count", id="row-count"),
        pytest.param(dup_key, {}, "duplicate key", id="duplicate-key"),
        pytest.param(null_key, {}, "missing key", id="null-key"),
        pytest.param(not_non_transactional, {}, "non_transactional", id="non-transactional"),
        pytest.param(key_role_anon, {}, "key_role", id="key-role-anon"),
        pytest.param(table_missing, {}, "chart_notes", id="candidate-table-not-backed-up"),
        pytest.param(wrong_key_columns, {}, "are not its row key", id="wrong-key-columns"),
        pytest.param(path_escape, {}, "table name", id="table-name-path-escape"),
        pytest.param(op_table_not_backed_up,
                     {"backups": tuple(t for t in R1_FINAL_BACKUP_TABLES
                                       if t != "metric_history_monthly")},
                     "metric_history_monthly is not in the backup", id="op-table-not-backed-up"),
        pytest.param(no_started_at, {}, "started_at", id="no-started-at"),
        pytest.param(naive_started_at, {}, "started_at", id="naive-started-at"),
        pytest.param(None, {"version": 2}, "not version 1", id="candidate-version-2"),
        pytest.param(None, {"generated_at": ""}, "generated_at", id="candidate-no-generated-at"),
        pytest.param(None, {"backup_manifest_sha256": "abc"}, "backup_manifest_sha256",
                     id="candidate-bad-backup-sha"),
        pytest.param(None, {"project": "abcdefghijklmnopqrst"}, "target project",
                     id="other-project"),
        pytest.param(None, {"operations": [_op(1, "metric_history", _A, None),
                                           _op(2, "metric_history", _A, None)]},
                     "duplicate operation key", id="duplicate-operation-key"),
        pytest.param(None, {"operations": []}, "no operations", id="empty"),
        pytest.param(None, {"operations": [
            _op(1, "metric_history", _A, None) | {"key": {"metric_id": "tax_revenue"}}]},
            "key columns", id="partial-operation-key"),
    ]


@pytest.mark.parametrize(("edit_backup", "candidate_kwargs", "refusal"), _refusal_cases())
def test_refuses_an_unverifiable_backup_or_candidate_and_writes_no_receipt(
    tmp_path, monkeypatch, capsys, edit_backup, candidate_kwargs, refusal
):
    backup = _backup(tmp_path, monkeypatch)
    candidate = _candidate(tmp_path, **candidate_kwargs)
    if edit_backup:
        edit_backup(backup, candidate)
    out = tmp_path / "receipt.json"

    assert _run(candidate, backup, out) == 2

    assert refusal in capsys.readouterr().out
    assert not out.exists()


def test_refuses_an_existing_receipt_and_leaves_it_untouched(tmp_path, monkeypatch):
    backup = _backup(tmp_path, monkeypatch)
    out = tmp_path / "receipt.json"
    out.write_text("earlier receipt")

    assert _run(_candidate(tmp_path), backup, out) == 2
    assert out.read_text() == "earlier receipt"


# Night-1 eligibility: the recheck must catch drift SINCE the candidate was built, so a backup the
# candidate was built from (or any backup no newer than the candidate) proves nothing. The 25 Sep
# source backup sits in the same vault as the Night-1 one, so pointing at it is an honest mistake.

def _drop_key_role(b: Path) -> None:
    manifest = json.loads((b / "manifest.json").read_text())
    del manifest["key_role"]
    (b / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def _source_backup_candidate(tmp_path: Path, backup: Path) -> Path:
    return _candidate(tmp_path, backup_manifest_sha256=_sha(backup / "manifest.json"))


_NOT_NIGHT1_CASES = [
    pytest.param(None, lambda t, b: _source_backup_candidate(t, b),
                 "backup-is-candidate-source", id="candidate-source-backup"),
    pytest.param(None, lambda t, b: _candidate(t, generated_at="2026-10-04T00:00:00+00:00"),
                 "backup-not-after-candidate", id="backup-older-than-candidate"),
    pytest.param(None, lambda t, b: _candidate(t, generated_at="2026-10-03T23:30:00+06:00"),
                 "backup-not-after-candidate", id="backup-same-instant-as-candidate"),
    pytest.param(_drop_key_role, lambda t, b: _candidate(t), "key-role-not-service",
                 id="no-key-role"),
]


@pytest.mark.parametrize(("edit_backup", "make_candidate", "reason"), _NOT_NIGHT1_CASES)
def test_refuses_a_backup_that_cannot_be_the_night1_final_backup_by_default(
    tmp_path, monkeypatch, capsys, edit_backup, make_candidate, reason
):
    backup = _backup(tmp_path, monkeypatch)
    if edit_backup:
        edit_backup(backup)
    candidate = make_candidate(tmp_path, backup)
    out = tmp_path / "receipt.json"

    assert _run(candidate, backup, out) == 2

    stdout = capsys.readouterr().out
    assert reason in stdout and "--reference-backup" in stdout
    assert not out.exists()


@pytest.mark.parametrize(("edit_backup", "make_candidate", "reason"), _NOT_NIGHT1_CASES)
def test_reference_backup_flag_compares_but_is_never_a_night1_pass(
    tmp_path, monkeypatch, capsys, edit_backup, make_candidate, reason
):
    backup = _backup(tmp_path, monkeypatch)
    if edit_backup:
        edit_backup(backup)
    candidate = make_candidate(tmp_path, backup)
    out = tmp_path / "receipt.json"

    assert _run(candidate, backup, out, "--reference-backup") == 3

    receipt = json.loads(out.read_text())
    assert receipt["result"] == "match" and receipt["mismatches"] == []
    assert receipt["reference_backup"] is True and receipt["night1_eligible"] is False
    assert reason in receipt["not_night1_eligible_because"]
    assert receipt["backup_is_candidate_source"] is (reason == "backup-is-candidate-source")
    stdout = capsys.readouterr().out
    assert "NOT Night-1 eligible" in stdout and reason in stdout


def test_the_real_shape_source_backup_lists_every_reason(tmp_path, monkeypatch):
    """The 25 Sep backup: the candidate's own source, older than it, and with no key_role."""
    backup = _backup(tmp_path, monkeypatch)
    _drop_key_role(backup)
    candidate = _candidate(tmp_path, generated_at="2026-10-04T00:00:00+00:00",
                           backup_manifest_sha256=_sha(backup / "manifest.json"))
    out = tmp_path / "receipt.json"

    assert _run(candidate, backup, out, "--reference-backup") == 3

    assert json.loads(out.read_text())["not_night1_eligible_because"] == [
        "backup-is-candidate-source", "backup-not-after-candidate", "key-role-not-service"]


def test_reference_backup_flag_still_reports_a_mismatch_as_exit_1(tmp_path, monkeypatch):
    backup = _backup(tmp_path, monkeypatch, metric_history=[_B])
    candidate = _source_backup_candidate(tmp_path, backup)
    out = tmp_path / "receipt.json"

    assert _run(candidate, backup, out, "--reference-backup") == 1

    receipt = json.loads(out.read_text())
    assert receipt["result"] == "mismatch" and receipt["night1_eligible"] is False


def test_reference_backup_flag_on_an_eligible_backup_is_still_a_night1_pass(
    tmp_path, monkeypatch
):
    backup = _backup(tmp_path, monkeypatch)
    out = tmp_path / "receipt.json"

    assert _run(_candidate(tmp_path), backup, out, "--reference-backup") == 0

    receipt = json.loads(out.read_text())
    assert receipt["night1_eligible"] is True and receipt["reference_backup"] is True


def test_reference_backup_flag_never_admits_a_non_service_key_role(tmp_path, monkeypatch, capsys):
    """An explicit anon/authenticated read may be RLS-trimmed: refused even as a reference."""
    backup = _backup(tmp_path, monkeypatch)
    _rewrite(backup, "news", [{"id": "news-1"}], key_role="anon")
    out = tmp_path / "receipt.json"

    assert _run(_candidate(tmp_path), backup, out, "--reference-backup") == 2

    assert "key_role" in capsys.readouterr().out and not out.exists()


_ROOT = Path(__file__).resolve().parent.parent
_FINAL_BACKUP_CMD = "python -m scripts.export_history --repair-snapshot DIR --r1-final-backup"
_RECHECK_CMD = "python -m scripts.repair_recheck_before_images --manifest"


def test_runbook_item_4_and_landmine_60_name_the_night1_backup_and_recheck_commands():
    runbook = (_ROOT / "docs/reviews/2026-09-25-history-repair-manifest.md").read_text()
    item4 = re.search(r"^4\. .*$", runbook.split("## Safety contract", 1)[1], re.MULTILINE)
    agents = (_ROOT / "AGENTS.md").read_text()
    landmine60 = re.search(r"^60\. .*$", agents, re.MULTILINE)
    assert item4 and landmine60
    for text in (item4.group(0), landmine60.group(0)):
        assert _FINAL_BACKUP_CMD in text and _RECHECK_CMD in text
        assert ".venv" in text  # the round-4 guard skips only REPO_ROOT/.venv
        # The source/older/no-key_role backup is refused; the opt-in is never a Night-1 pass.
        assert "--reference-backup" in text and "exits 3" in text
