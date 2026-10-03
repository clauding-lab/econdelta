"""Refresh a reviewed R1 manifest's before-images from the writer-paused Night-1 backup. Offline.

Why (2 Oct 2026 BDT): the reviewed R1 manifest was built from the 25 Sep backup, but the old
nightly producer re-upserts some rows (same value, a new ``ingested_at``), so the apply engine's
whole-batch before-image check (``repair_observation_history.load_manifest`` and the live recheck
in ``_execute``) can never pass. This tool writes a NEW candidate whose before-images describe the
Night-1 backup, under rules that never widen what the reviewed manifest changes:

* insert (``before`` is null): the key must be absent from the backup;
* otherwise the backup row must exist with the same column set, and the per-column difference
  (the engine's own ``same()``) must be empty (unchanged), exactly ``ingested_at`` (refreshed), or
  exactly ``ingested_at`` + ``value`` for a key the owner listed in ``--rebind`` with the exact
  old before value and current value (re-bound);
* every ``--rebind`` entry is used exactly once; ``after`` rows never change; a refreshed before
  equal to its after (a no-op) is refused.

Every deviation is a refusal (exit 1, nothing written), never a filter.

    python -m scripts.repair_refresh_before_images --manifest PLAN/manifest.json \\
        --expected-sha256 SHA --backup-dir NIGHT1 --rebind rebind.json --out candidate.json \\
        [--report report.json]

The output still needs the engine's ``--plan`` and the owner's review. Row values are never
printed, except the owner-approved re-bind values.
"""

from __future__ import annotations

import argparse
import copy
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.repair_observation_history import (
    KEYS,
    RepairConflict,
    _read_checked,
    file_hash,
    same,
    write_json,
)
from scripts.repair_recheck_before_images import (
    _identity,
    _load_backup,
    _load_candidate,
    _not_night1_reasons,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
_REBIND_FIELDS = {"table", "key", "before_value", "current_value"}


def _label(table: str, key: dict) -> str:
    return f"{table} {' '.join(str(key[c]) for c in KEYS[table])}"


def _git_head() -> str:
    try:
        result = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                                capture_output=True, text=True, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RepairConflict("cannot read this tool's git HEAD") from exc
    head = result.stdout.strip()
    if len(head) != 40:
        raise RepairConflict("cannot read this tool's git HEAD")
    return head


def _load_rebinds(path: Path) -> dict[tuple[str, Any], dict]:
    """Owner-approved value re-binds, keyed like the backup index; exact fields only."""
    entries = _read_checked(path, file_hash(path))
    if not isinstance(entries, list):
        raise RepairConflict("re-bind file must be a JSON list")
    rebinds: dict[tuple[str, Any], dict] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != _REBIND_FIELDS:
            raise RepairConflict(f"re-bind entry must have exactly {sorted(_REBIND_FIELDS)}")
        table, key = entry["table"], entry["key"]
        if table not in KEYS or not isinstance(key, dict) or set(key) != set(KEYS[table]):
            raise RepairConflict("re-bind entry names an unsupported table or a non-exact key")
        identity = (table, _identity(key, list(KEYS[table])))
        if identity in rebinds:
            raise RepairConflict(f"duplicate re-bind entry: {_label(table, key)}")
        rebinds[identity] = entry
    return rebinds


def _differing(current: dict, before: dict) -> set[str]:
    return {c for c in before if not same({c: current[c]}, {c: before[c]})}


def _op_identity(op: dict, named: list[str], tables: dict) -> tuple[str, Any]:
    """The operation's exact (table, typed key); refuses a table or key the backup cannot index."""
    table, key = op["table"], op["key"]
    if table not in KEYS or table not in named:
        raise RepairConflict(f"operation table {table} is not in the manifest's backups")
    columns = tables[table][0]
    if not isinstance(key, dict) or set(key) != set(columns) or tuple(columns) != KEYS[table]:
        raise RepairConflict(f"operation {op.get('operation_id')} key differs from the backup's "
                             f"{table} key")
    return table, _identity(key, columns)


def _refresh(op: dict, tables: dict, rebinds: dict, used: set) -> tuple[str, dict | None]:
    """Classify one (already identity-checked) operation and return (category, new before)."""
    table, key, before = op["table"], op["key"], op["before"]
    label = _label(table, key)
    columns, index = tables[table]
    identity = _identity(key, columns)
    current = index.get(identity)
    if before is None:
        if current is not None:
            raise RepairConflict(f"{label} insert key is present in the backup")
        return "unchanged", None
    if current is None:
        raise RepairConflict(f"{label} before-image row is missing from the backup")
    if set(current) != set(before):
        raise RepairConflict(f"{label} column set differs from the reviewed before-image")
    diff = _differing(current, before)
    entry = rebinds.get((table, identity))
    if not diff:
        return "unchanged", before
    if diff == {"ingested_at"}:
        category = "ingested_at_refreshed"
    elif diff == {"ingested_at", "value"} and entry is not None and same(
        {"value": entry["before_value"]}, {"value": before["value"]}
    ) and same({"value": entry["current_value"]}, {"value": current["value"]}):
        category = "rebound"
        used.add((table, identity))
    else:
        listed = "" if entry is None else " (listed re-bind values do not match exactly)"
        raise RepairConflict(f"{label} differs in {', '.join(sorted(diff))}{listed}")
    refreshed = copy.deepcopy(current)
    if same(refreshed, op["after"]):
        raise RepairConflict(f"{label} refreshed before equals the reviewed after (a no-op)")
    return category, refreshed


def refresh(manifest_path: Path, expected_sha256: str, backup_dir: Path, rebind_path: Path,
            out: Path, report_path: Path | None = None) -> dict:
    """Write the refreshed candidate (and optional report); return the report."""
    for path in (out, report_path):
        if path is not None and path.exists():
            raise RepairConflict(f"{path} already exists; a refresh never overwrites")
    manifest, manifest_sha256 = _load_candidate(manifest_path)
    if manifest_sha256 != expected_sha256:
        raise RepairConflict(f"hash mismatch: {manifest_path.name}")
    backup, backup_sha256, backups, tables = _load_backup(backup_dir)
    if backup["target_project"] != manifest["target_project"]:
        raise RepairConflict(f"backup target project {backup['target_project']!r} is not the "
                             f"manifest target project {manifest['target_project']!r}")
    if backup.get("key_role") != "service":
        raise RepairConflict("backup key_role is not 'service' (key-role-not-service)")
    reasons = _not_night1_reasons(manifest, backup, backup_sha256)
    if reasons:
        raise RepairConflict(f"this backup cannot refresh the manifest ({', '.join(reasons)})")
    named = [ref.get("table") for ref in manifest.get("backups", [])]
    by_table = {ref["table"]: ref for ref in backups}
    for table in named:
        if table not in tables:
            raise RepairConflict(f"the manifest's backup table {table} is not in the new backup")
    if len(set(named)) != len(named):
        raise RepairConflict("the manifest names a backup table twice")
    rebinds = _load_rebinds(rebind_path)

    counts = {"unchanged": 0, "ingested_at_refreshed": 0, "rebound": 0}
    refreshed_keys: list[dict] = []
    used: set = set()
    seen: set = set()
    operations = []
    for op in manifest["operations"]:
        identity = _op_identity(op, named, tables)
        if identity in seen:
            raise RepairConflict(f"duplicate operation key: {_label(op['table'], op['key'])}")
        seen.add(identity)
        category, before = _refresh(op, tables, rebinds, used)
        counts[category] += 1
        if category == "ingested_at_refreshed":
            refreshed_keys.append({"table": op["table"], "key": op["key"]})
        operations.append(op | {"before": before})
    unused = [entry for identity, entry in rebinds.items() if identity not in used]
    if unused:
        raise RepairConflict("unused re-bind entry: "
                             + "; ".join(_label(e["table"], e["key"]) for e in unused))
    rebound = list(rebinds.values())  # every entry was used exactly once

    head = _git_head()
    note = (f"Before-images refreshed from Night-1 backup manifest sha256 {backup_sha256} by "
            f"scripts/repair_refresh_before_images.py at econdelta commit {head}: "
            f"{counts['unchanged']} unchanged, {counts['ingested_at_refreshed']} "
            f"ingested_at-refreshed, {counts['rebound']} re-bound (owner-approved value "
            f"re-binds; after rows unchanged); original reviewed manifest sha256 "
            f"{manifest_sha256}")
    candidate = manifest | {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "backup_manifest_sha256": backup_sha256,
        "backups": [by_table[table] for table in named],
        "operations": operations,
        "unresolved": [*manifest.get("unresolved", []), note],
    }
    if out.exists():
        raise RepairConflict(f"{out} appeared during the refresh; not overwritten")
    write_json(out, candidate)
    report = {
        "counts": counts | {"refused": 0, "operations": len(operations)},
        "refreshed": refreshed_keys,
        "rebound": rebound,
        "input_manifest_path": str(manifest_path.resolve()),
        "input_manifest_sha256": manifest_sha256,
        "backup_dir": str(backup_dir.resolve()),
        "backup_manifest_sha256": backup_sha256,
        "rebind_sha256": file_hash(rebind_path),
        "tool_commit": head,
        "output_path": str(out.resolve()),
        "output_sha256": file_hash(out),
    }
    if report_path is not None:
        write_json(report_path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, required=True,
                        help="the reviewed plan manifest.json")
    parser.add_argument("--expected-sha256", required=True, help="its reviewed sha256")
    parser.add_argument("--backup-dir", type=Path, required=True,
                        help="the writer-paused Night-1 twelve-table backup (with manifest.json)")
    parser.add_argument("--rebind", type=Path, required=True,
                        help="JSON list of owner-approved value re-binds (may be [])")
    parser.add_argument("--out", type=Path, required=True, help="new candidate (must not exist)")
    parser.add_argument("--report", type=Path, help="report JSON (must not exist)")
    args = parser.parse_args(argv)
    try:
        report = refresh(args.manifest, args.expected_sha256, args.backup_dir, args.rebind,
                         args.out, args.report)
    except (RepairConflict, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Refresh stopped: {exc}")
        return 1
    c = report["counts"]
    print(f"Refreshed {c['operations']} operations: {c['unchanged']} unchanged, "
          f"{c['ingested_at_refreshed']} ingested_at-refreshed, {c['rebound']} re-bound, "
          f"{c['refused']} refused")
    print(f"input sha256={report['input_manifest_sha256']} "
          f"backup manifest sha256={report['backup_manifest_sha256']}")
    print(f"output sha256={report['output_sha256']} ({report['output_path']})")
    for item in report["refreshed"]:
        print(f"  ingested_at-refreshed: {_label(item['table'], item['key'])}")
    for item in report["rebound"]:
        print(f"  re-bound: {_label(item['table'], item['key'])} before {item['before_value']}"
              f" -> current {item['current_value']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
