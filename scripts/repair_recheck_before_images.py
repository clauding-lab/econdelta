"""Night-1 recheck of every R1 before-image against the final writer-paused backup. Read-only.

Runbook item 4 ("Safety contract", docs/reviews/2026-09-25-history-repair-manifest.md): after the
final twelve-table backup (``python -m scripts.export_history --repair-snapshot DIR
--r1-final-backup``) and before R1 is applied, every reviewed operation must still describe the
frozen database: an update/delete's ``before`` row is in the backup, full row, identical under the
apply engine's own rule (``repair_observation_history.same``); an insert's key is absent.

    python -m scripts.repair_recheck_before_images --manifest r1-candidate.json \\
        --backup-dir DIR --out receipt.json

Exit 0 only for ``match``; 1 for ``mismatch`` (the receipt names every key, never a row value);
2 when an input cannot be verified (no receipt is written). It never connects to a database.
A mismatch needs a fresh proposal and review, never a force flag.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from scripts.repair_observation_history import (
    KEYS,
    Backup,
    RepairConflict,
    Row,
    _read_checked,
    file_hash,
    same,
    write_json,
)

Identity = tuple[tuple[str, Any], ...]


def _identity(row: dict, columns: list[str]) -> Identity:
    # Typed, so a JSON true never collides with 1 and "1" never with 1.
    return tuple((type(row.get(c)).__name__, row.get(c)) for c in columns)


def _load_backup(backup_dir: Path) -> tuple[dict, str, list[Backup], dict[str, tuple[list[str],
                                                                                   dict]]]:
    """Verify the backup against its own manifest.json: hashes, row counts, unique keys."""
    manifest_path = backup_dir / "manifest.json"
    manifest_sha256 = file_hash(manifest_path)
    manifest = _read_checked(manifest_path, manifest_sha256)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("tables"), dict):
        raise RepairConflict("backup manifest.json has no tables")
    if not manifest.get("target_project") or not manifest.get("started_at"):
        raise RepairConflict("backup manifest.json lacks target_project or started_at")
    if manifest.get("non_transactional") is not True:
        raise RepairConflict("backup manifest.json does not record non_transactional: true")
    if "key_role" in manifest and manifest["key_role"] != "service":
        raise RepairConflict(f"backup key_role is {manifest['key_role']!r}, not 'service'")
    backups: list[Backup] = []
    tables: dict[str, tuple[list[str], dict]] = {}
    for table, ref in manifest["tables"].items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", table):
            raise RepairConflict(f"backup table name {table!r} is not a plain table name")
        columns = ref.get("key")
        if not isinstance(columns, list) or not columns or (
            table in KEYS and tuple(columns) != KEYS[table]
        ):
            raise RepairConflict(f"backup {table} key columns {columns!r} are not its row key")
        path = backup_dir / f"{table}.json"
        rows = _read_checked(path, ref.get("sha256", ""))
        if not isinstance(rows, list) or len(rows) != ref.get("rows"):
            raise RepairConflict(f"backup {table} row count differs from its manifest")
        index: dict[Identity, Row] = {}
        for row in rows:
            if not isinstance(row, dict) or any(row.get(c) is None for c in columns):
                raise RepairConflict(f"backup {table} has a row with a missing key")
            identity = _identity(row, columns)
            if identity in index:
                raise RepairConflict(f"backup {table} has a duplicate key")
            index[identity] = row
        tables[table] = (columns, index)
        backups.append({"table": table, "path": str(path.resolve()), "sha256": ref["sha256"],
                        "rows": len(rows)})
    return manifest, manifest_sha256, backups, tables


def _load_candidate(path: Path) -> tuple[dict, str]:
    sha256 = file_hash(path)
    candidate = _read_checked(path, sha256)
    if not isinstance(candidate, dict) or candidate.get("version") != 1:
        raise RepairConflict("candidate/plan manifest is not version 1")
    if not candidate.get("target_project"):
        raise RepairConflict("candidate/plan manifest has no target project")
    if not isinstance(candidate.get("operations"), list) or not candidate["operations"]:
        raise RepairConflict("candidate/plan manifest has no operations")
    return candidate, sha256


def _compare(candidate: dict, tables: dict[str, tuple[list[str], dict]]) -> tuple[dict, list]:
    counts: dict[str, dict[str, int]] = {}
    mismatches: list[dict] = []
    seen: set[tuple[str, Identity]] = set()
    for op in candidate["operations"]:
        table, key = op["table"], op["key"]
        if table not in tables:
            raise RepairConflict(f"operation table {table} is not in the backup")
        columns, index = tables[table]
        if not isinstance(key, dict) or set(key) != set(columns):
            raise RepairConflict(f"operation {op.get('operation_id')} key columns differ from "
                                 f"the backup's {table} key")
        identity = _identity(key, columns)
        if (table, identity) in seen:
            raise RepairConflict(f"duplicate operation key in {table}")
        seen.add((table, identity))
        current = index.get(identity)
        if op["before"] is None:
            kind = None if current is None else "unexpected-present"
        elif current is None:
            kind = "missing"
        else:
            kind = None if same(current, op["before"]) else "changed"
        tally = counts.setdefault(table, {"operations": 0, "match": 0, "mismatch": 0})
        tally["operations"] += 1
        tally["mismatch" if kind else "match"] += 1
        if kind:
            mismatches.append({"table": table, "key": {c: key[c] for c in columns}, "kind": kind})
    return counts, mismatches


def recheck(manifest_path: Path, backup_dir: Path, out: Path) -> dict:
    """Write and return the receipt; raises RepairConflict when an input cannot be verified."""
    if out.exists():
        raise RepairConflict(f"{out} already exists; a recheck never overwrites a receipt")
    candidate, candidate_sha256 = _load_candidate(manifest_path)
    backup, backup_sha256, backups, tables = _load_backup(backup_dir)
    if backup["target_project"] != candidate["target_project"]:
        raise RepairConflict(f"backup target project {backup['target_project']!r} is not the "
                             f"manifest target project {candidate['target_project']!r}")
    for ref in candidate.get("backups", []):
        if ref.get("table") not in tables:
            raise RepairConflict(f"the manifest's backup table {ref.get('table')} is not in the "
                                 "new backup")
    counts, mismatches = _compare(candidate, tables)
    receipt = {
        "result": "mismatch" if mismatches else "match",
        "manifest_path": str(manifest_path.resolve()),
        "manifest_sha256": candidate_sha256,
        "backup_dir": str(backup_dir.resolve()),
        "backup_manifest_sha256": backup_sha256,
        "backup_started_at": backup["started_at"],
        "backup_finished_at": backup.get("finished_at"),
        "target_project": candidate["target_project"],
        "backups": backups,
        "operations": len(candidate["operations"]),
        "counts": counts,
        "mismatches": mismatches,
    }
    if out.exists():
        raise RepairConflict(f"{out} appeared during the recheck; not overwritten")
    write_json(out, receipt)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, required=True,
                        help="the reviewed R1 candidate or its --plan manifest.json")
    parser.add_argument("--backup-dir", type=Path, required=True,
                        help="the new writer-paused twelve-table backup (with its manifest.json)")
    parser.add_argument("--out", type=Path, required=True, help="receipt path (must not exist)")
    args = parser.parse_args(argv)
    try:
        receipt = recheck(args.manifest, args.backup_dir, args.out)
    except (RepairConflict, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Recheck stopped: {exc}")
        return 2
    print(f"{receipt['result']}: {receipt['operations']} operations, "
          f"{len(receipt['mismatches'])} mismatching; receipt sha256={file_hash(args.out)}")
    for item in receipt["mismatches"]:
        print(f"  {item['kind']}: {item['table']} {' '.join(map(str, item['key'].values()))}")
    return 0 if receipt["result"] == "match" else 1


if __name__ == "__main__":
    raise SystemExit(main())
