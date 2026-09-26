"""Reviewed exact-key history repair. No default target and no implicit network writes.

Plan from a reviewed candidate JSON plus hashed backup/evidence files; apply only
its exact byte hash. Every write has a durable intent and a verified receipt.
All overlapping writers MUST remain paused from final backup through read-back.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Literal, NotRequired, Protocol, TypeAlias, TypedDict

JSONValue: TypeAlias = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
Row: TypeAlias = dict[str, JSONValue]


class RowKey(TypedDict):
    metric_id: str
    as_of: NotRequired[str]


class Evidence(TypedDict):
    path: str
    sha256: str
    locator: str


class Backup(TypedDict):
    table: str
    path: str
    sha256: str
    rows: int


class CodeCommits(TypedDict):
    econdelta: str
    brief: str


class Operation(TypedDict):
    operation_id: str
    table: str
    key: RowKey
    before: Row | None
    after: Row | None
    reason: str
    evidence: list[Evidence]
    requires: list[str]


class Manifest(TypedDict):
    version: int
    target_project: str
    target: str
    code_commits: CodeCommits
    generated_at: str
    backup_manifest_sha256: str
    backups: list[Backup]
    operations: list[Operation]
    unresolved: list[str]


States: TypeAlias = dict[str, Literal["intent", "confirmed"]]


class ApplyReceipt(TypedDict):
    manifest: Manifest
    manifest_sha256: str
    states: States


class RestoreReceipt(TypedDict):
    receipt_sha256: str
    operations: list[Operation]
    states: States


KEYS = {
    "metric_history": ("metric_id", "as_of"),
    "metric_history_monthly": ("metric_id", "as_of"),
    "metric_definitions": ("metric_id",),
    "metric_definitions_monthly": ("metric_id",),
}


class RepairConflict(RuntimeError):
    """Evidence, target or current database state differs from reviewed inputs."""


class Store(Protocol):
    target: str

    def get(self, table: str, key: RowKey) -> Row | None: ...
    def change(self, op: Operation) -> None: ...


def file_hash(path: Path) -> str:
    """SHA-256 of the exact bytes used for review."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    """Private, atomic, fsynced receipt write (file AND containing directory)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def _typed(value: Any) -> Any:
    # JSON numbers compare numerically; booleans, strings and nested JSON retain
    # their types (Python's True == 1 must not make two before-images equal).
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("number", Decimal(str(value)))
    if isinstance(value, dict):
        return {key: _typed(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_typed(item) for item in value]
    return (type(value).__name__, value)


def same(left: Any, right: Any) -> bool:
    """Compare entire rows with database-equivalent timestamp/JSON semantics."""

    def normalized(row: Any) -> Any:
        if not isinstance(row, dict):
            return row
        row = copy.deepcopy(row)
        for field in ("ingested_at", "created_at", "updated_at"):
            if isinstance(row.get(field), str):
                row[field] = datetime.fromisoformat(row[field]).isoformat()
        return row

    return _typed(normalized(left)) == _typed(normalized(right))


def _read_checked(path: Path, expected: str) -> Any:
    try:
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise RepairConflict(f"hash mismatch: {path.name}")
        return json.loads(
            raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON"))
        )
    except (OSError, ValueError) as exc:
        raise RepairConflict(f"missing or invalid file: {path.name}") from exc


def load_manifest(path: Path, *, expected_sha256: str, target: str) -> Manifest:
    """Validate all evidence and exact backup images before any target access."""
    manifest = _read_checked(path, expected_sha256)
    if manifest.get("version") != 1 or manifest.get("target") != target:
        raise RepairConflict("wrong target or manifest version")
    if not manifest.get("target_project") or not manifest.get("generated_at"):
        raise RepairConflict("missing project or generation timestamp")
    if target.startswith("supabase:") and target != f"supabase:{manifest['target_project']}":
        raise RepairConflict("target project and connection locator differ")
    commits = manifest.get("code_commits", {})
    if set(commits) != {"econdelta", "brief"} or any(
        not re.fullmatch(r"[0-9a-f]{40}", c) for c in commits.values()
    ):
        raise RepairConflict("both exact code commits required")
    backups = {}
    for ref in manifest.get("backups", []):
        rows = _read_checked(Path(ref["path"]), ref["sha256"])
        if not isinstance(rows, list) or len(rows) != ref["rows"] or ref["table"] in backups:
            raise RepairConflict("backup count or duplicate table mismatch")
        backups[ref["table"]] = rows
    ids, keys = set(), set()
    for op in manifest.get("operations", []):
        table, key = op["table"], op["key"]
        if table not in KEYS or set(key) != set(KEYS[table]):
            raise RepairConflict("unsupported table or non-exact key")
        identity = (table, tuple(key[k] for k in KEYS[table]))
        if op["operation_id"] in ids or identity in keys:
            raise RepairConflict("duplicate operation or key")
        if not set(op.get("requires", [])) <= ids:
            raise RepairConflict("replacement prerequisite missing or out of order")
        ids.add(op["operation_id"])
        keys.add(identity)
        if table not in backups:
            raise RepairConflict("missing table backup")
        rows = backups[table]
        matches = [r for r in rows if all(r.get(k) == v for k, v in key.items())]
        if len(matches) > 1 or not same(matches[0] if matches else None, op["before"]):
            raise RepairConflict("before-image differs from full backup / destination collision")
        before, after = op["before"], op["after"]
        columns = set(before or (rows[0] if rows else {}))
        if same(before, after) or not op.get("reason") or not op.get("evidence"):
            raise RepairConflict("no-op or missing reason/evidence")
        for row in (before, after):
            if row is not None and (
                set(row) != columns or any(row.get(k) != v for k, v in key.items())
            ):
                raise RepairConflict(
                    "partial image or key mutation; moves require separate operations"
                )
        for evidence in op["evidence"]:
            if not evidence.get("locator"):
                raise RepairConflict("source page/row locator missing")
            try:
                valid = file_hash(Path(evidence["path"])) == evidence["sha256"]
            except OSError:
                valid = False
            if not valid:
                raise RepairConflict("missing evidence or source hash mismatch")
    if not ids:
        raise RepairConflict("empty repair")
    return manifest


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _execute(
    operations: list[Operation], store: Store, receipt: ApplyReceipt | RestoreReceipt, path: Path
) -> None:
    # Check the whole batch first. On resume, only OUR durable intent can explain
    # an after-image. Otherwise a coincidentally identical row is still a conflict.
    for op in operations:
        current = store.get(op["table"], op["key"])
        state = receipt["states"].get(op["operation_id"])
        if state == "confirmed":
            valid = same(current, op["after"])
        elif state == "intent":
            valid = same(current, op["before"]) or same(current, op["after"])
        else:
            valid = same(current, op["before"])
        if not valid:
            raise RepairConflict(f"row changed / missing owning receipt: {op['operation_id']}")
    for op in operations:
        op_id = op["operation_id"]
        if receipt["states"].get(op_id) == "confirmed":
            continue
        current = store.get(op["table"], op["key"])
        if same(current, op["before"]):
            receipt["states"][op_id] = "intent"
            write_json(path, receipt)  # MUST be durable before the database write.
            store.change(op)
        elif not (receipt["states"].get(op_id) == "intent" and same(current, op["after"])):
            raise RepairConflict(f"row changed before write: {op_id}")
        if not same(store.get(op["table"], op["key"]), op["after"]):
            raise RepairConflict(f"after-image read-back mismatch: {op_id}")
        receipt["states"][op_id] = "confirmed"
        write_json(path, receipt)


def apply_manifest(
    path: Path, *, expected_sha256: str, target: str, store: Store, receipts_path: Path
) -> ApplyReceipt:
    """Apply/resume one reviewed manifest; no network credentials accepted here."""
    manifest = load_manifest(path, expected_sha256=expected_sha256, target=target)
    if store.target != target:
        raise RepairConflict("store target mismatch")
    with _lock(receipts_path):
        if receipts_path.with_suffix(".restore.json").exists():
            raise RepairConflict("restore has started; cannot resume apply")
        receipt: ApplyReceipt = {
            "manifest": manifest,
            "manifest_sha256": expected_sha256,
            "states": {},
        }
        if receipts_path.exists():
            receipt = json.loads(receipts_path.read_text())
            if (
                receipt.get("manifest_sha256") != expected_sha256
                or receipt.get("manifest") != manifest
            ):
                raise RepairConflict("receipts belong to another manifest")
        _execute(manifest["operations"], store, receipt, receipts_path)
        return receipt


def restore_receipts(
    path: Path, *, expected_sha256: str, target: str, store: Store
) -> RestoreReceipt:
    """Restore only intent/confirmed operations, refusing intervening updates."""
    with _lock(path):
        receipt = _read_checked(path, expected_sha256)
        manifest = receipt["manifest"]
        if manifest["target"] != target or store.target != target:
            raise RepairConflict("wrong restore target")
        # Revalidate original backups/evidence as well as the reviewed receipt.
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "manifest.json"
            write_json(original, manifest)
            load_manifest(original, expected_sha256=file_hash(original), target=target)
        output = path.with_suffix(".restore.json")
        if output.exists():
            reverse = json.loads(output.read_text())
            if reverse.get("receipt_sha256") != expected_sha256:
                raise RepairConflict("restore receipt hash changed")
        else:
            operations = []
            for op in reversed(manifest["operations"]):
                state = receipt["states"].get(op["operation_id"])
                if state not in ("intent", "confirmed"):
                    continue
                if state == "intent" and same(store.get(op["table"], op["key"]), op["before"]):
                    continue
                operations.append(op | {"before": op["after"], "after": op["before"]})
            # Freeze the inverse set BEFORE rollback starts. Re-deriving it from
            # live rows after an interrupted rollback would lose recovered intents.
            reverse = {"receipt_sha256": expected_sha256, "operations": operations, "states": {}}
            write_json(output, reverse)
        _execute(reverse["operations"], store, reverse, output)
        return reverse


def main(argv: list[str] | None = None) -> int:
    """Explicit plan/apply/restore interface; see the committed repair runbook."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    parser.add_argument("--candidate", type=Path)
    mode.add_argument("--apply", type=Path, metavar="MANIFEST")
    mode.add_argument("--restore", type=Path, metavar="RECEIPTS")
    parser.add_argument("--target", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--writers-paused", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.plan:
            if args.candidate is None:
                raise RepairConflict("--plan requires an explicit --candidate file")
            manifest = load_manifest(
                args.candidate, expected_sha256=args.expected_sha256, target=args.target
            )
            destination = args.out_dir / "manifest.json"
            if destination.exists():
                raise RepairConflict("plan output already exists; use a new directory")
            write_json(destination, manifest)
            print(
                f"Validated {len(manifest['operations'])} operations; sha256={file_hash(destination)}"
            )
        else:
            if not args.writers_paused:
                raise RepairConflict("all overlapping writers must be paused")
            from scripts.repair_history_store import open_store

            store = open_store(args.target)
            if args.apply:
                apply_manifest(
                    args.apply,
                    expected_sha256=args.expected_sha256,
                    target=args.target,
                    store=store,
                    receipts_path=args.out_dir / "receipts.json",
                )
            else:
                restore_receipts(
                    args.restore,
                    expected_sha256=args.expected_sha256,
                    target=args.target,
                    store=store,
                )
            print("Exact-image read-back complete; retain receipts and backups")
    except (RepairConflict, OSError, ValueError, KeyError) as exc:
        print(f"Repair stopped: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
