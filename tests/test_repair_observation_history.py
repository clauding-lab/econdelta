"""Exact-image repair safety, including crash windows and typed JSON values."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.repair_observation_history import (
    RepairConflict,
    apply_manifest,
    file_hash,
    load_manifest,
    restore_receipts,
    write_json,
)


class MemoryStore:
    target = "snapshot:test"

    def __init__(self, row):
        self.rows = {("metric_history", "x", "2026-07-31"): copy.deepcopy(row)}
        self.writes = 0
        self.crash_after_write = False

    def get(self, table, key):
        return copy.deepcopy(self.rows.get((table, *key.values())))

    def change(self, op):
        identity = (op["table"], *op["key"].values())
        assert self.get(op["table"], op["key"]) == op["before"]
        if op["after"] is None:
            self.rows.pop(identity, None)
        else:
            self.rows[identity] = copy.deepcopy(op["after"])
        self.writes += 1
        if self.crash_after_write:
            self.crash_after_write = False
            raise InterruptedError("power loss after database commit")


@pytest.fixture
def bundle(tmp_path):
    before = {
        "metric_id": "x",
        "as_of": "2026-07-31",
        "value": {"amount": "5.2"},
        "source": "BB",
        "ingested_at": "2026-09-25T00:00:00+00:00",
        "provenance": None,
    }
    backup = tmp_path / "metric_history.json"
    write_json(backup, [before])
    source = tmp_path / "source.txt"
    source.write_text("June 2026: 5.2")
    after = before | {"value": {"amount": 5.2}, "provenance": "reviewed source"}
    manifest = {
        "version": 1,
        "target_project": "test",
        "target": "snapshot:test",
        "code_commits": {"econdelta": "a" * 40, "brief": "b" * 40},
        "generated_at": "2026-09-26T00:00:00+00:00",
        "backups": [
            {"table": "metric_history", "path": str(backup), "sha256": file_hash(backup), "rows": 1}
        ],
        "operations": [
            {
                "operation_id": "x-fix",
                "table": "metric_history",
                "key": {"metric_id": "x", "as_of": "2026-07-31"},
                "before": before,
                "after": after,
                "reason": "source verified",
                "evidence": [
                    {
                        "path": str(source),
                        "sha256": file_hash(source),
                        "locator": "line 1 June 2026",
                    }
                ],
                "requires": [],
            }
        ],
    }
    path = tmp_path / "manifest.json"
    write_json(path, manifest)
    return path, manifest, MemoryStore(before), tmp_path / "receipts.json"


def run(bundle):
    path, _, store, receipts = bundle
    return apply_manifest(
        path,
        expected_sha256=file_hash(path),
        target=store.target,
        store=store,
        receipts_path=receipts,
    )


def test_apply_and_restore_preserve_jsonb_types(bundle):
    path, manifest, store, receipts = bundle
    run(bundle)
    assert (
        store.get("metric_history", manifest["operations"][0]["key"])
        == manifest["operations"][0]["after"]
    )
    restore_receipts(
        receipts, expected_sha256=file_hash(receipts), target=store.target, store=store
    )
    assert (
        store.get("metric_history", manifest["operations"][0]["key"])
        == manifest["operations"][0]["before"]
    )
    assert path.exists()


@pytest.mark.parametrize(
    "case",
    [
        "target",
        "hash",
        "before",
        "collision",
        "duplicate",
        "duplicate_key",
        "evidence",
        "backup",
        "backup_hash",
        "partial_image",
    ],
)
def test_invalid_plan_cannot_write(bundle, case):
    path, manifest, store, receipts = bundle
    if case == "target":
        store.target = "snapshot:wrong"
    elif case == "before":
        store.rows[("metric_history", "x", "2026-07-31")]["source"] = "newer"
    elif case == "collision":
        manifest["operations"][0]["before"] = None
    elif case == "duplicate":
        manifest["operations"].append(copy.deepcopy(manifest["operations"][0]))
    elif case == "duplicate_key":
        manifest["operations"].append(manifest["operations"][0] | {"operation_id": "different-id"})
    elif case == "evidence":
        Path(manifest["operations"][0]["evidence"][0]["path"]).unlink()
    elif case == "backup":
        Path(manifest["backups"][0]["path"]).unlink()
    elif case == "backup_hash":
        Path(manifest["backups"][0]["path"]).write_text("[]")
    elif case == "partial_image":
        del manifest["operations"][0]["before"]["source"]
    write_json(path, manifest)
    with pytest.raises(RepairConflict):
        apply_manifest(
            path,
            expected_sha256="0" * 64 if case == "hash" else file_hash(path),
            target=store.target,
            store=store,
            receipts_path=receipts,
        )
    assert store.writes == 0


def test_interrupted_commit_resumes_once_then_already_applied(bundle):
    _, _, store, receipts = bundle
    store.crash_after_write = True
    with pytest.raises(InterruptedError):
        run(bundle)
    assert json.loads(receipts.read_text())["states"] == {"x-fix": "intent"}
    run(bundle)
    run(bundle)
    assert store.writes == 1
    assert json.loads(receipts.read_text())["states"] == {"x-fix": "confirmed"}


def test_restore_refuses_newer_legitimate_update(bundle):
    _, _, store, receipts = bundle
    run(bundle)
    store.rows[("metric_history", "x", "2026-07-31")]["source"] = "newer legitimate release"
    with pytest.raises(RepairConflict, match="changed"):
        restore_receipts(
            receipts, expected_sha256=file_hash(receipts), target=store.target, store=store
        )
    assert store.writes == 1


def test_restore_crash_resumes_without_repeating_write(bundle):
    _, _, store, receipts = bundle
    run(bundle)
    store.crash_after_write = True
    receipt_hash = file_hash(receipts)
    with pytest.raises(InterruptedError):
        restore_receipts(receipts, expected_sha256=receipt_hash, target=store.target, store=store)
    restore_receipts(receipts, expected_sha256=receipt_hash, target=store.target, store=store)
    assert store.writes == 2


def test_after_image_without_owned_intent_is_not_claimed_as_our_write(bundle):
    _, manifest, store, _ = bundle
    store.rows[("metric_history", "x", "2026-07-31")] = copy.deepcopy(
        manifest["operations"][0]["after"]
    )
    with pytest.raises(RepairConflict, match="receipt|changed"):
        run(bundle)
    assert store.writes == 0


def test_numeric_and_boolean_json_are_not_equal(bundle):
    path, manifest, _, _ = bundle
    manifest["operations"][0]["after"] = manifest["operations"][0]["before"] | {"value": True}
    write_json(path, manifest)
    assert load_manifest(path, expected_sha256=file_hash(path), target="snapshot:test")


def test_batch_conflict_is_detected_before_first_mutation(bundle):
    path, manifest, store, _ = bundle
    first = copy.deepcopy(manifest["operations"][0])
    first["operation_id"] = "new-date"
    first["key"]["as_of"] = "2026-06-30"
    first["before"] = None
    first["after"]["as_of"] = "2026-06-30"
    manifest["operations"].insert(0, first)
    store.rows[("metric_history", "x", "2026-07-31")]["source"] = "changed"
    write_json(path, manifest)
    with pytest.raises(RepairConflict, match="changed"):
        run(bundle)
    assert store.writes == 0


def test_restore_of_apply_intent_survives_its_own_interruption(bundle):
    _, _, store, receipts = bundle
    store.crash_after_write = True
    with pytest.raises(InterruptedError):
        run(bundle)
    store.crash_after_write = True
    digest = file_hash(receipts)
    with pytest.raises(InterruptedError):
        restore_receipts(receipts, expected_sha256=digest, target=store.target, store=store)
    restore_receipts(receipts, expected_sha256=digest, target=store.target, store=store)
    reverse = json.loads(receipts.with_suffix(".restore.json").read_text())
    assert reverse["states"] == {"x-fix": "confirmed"}


def test_pause_and_explicit_target_required_before_connection(tmp_path):
    from scripts.repair_observation_history import main

    with pytest.raises(SystemExit):
        main(["--apply", "absent"])
    assert (
        main(
            [
                "--apply",
                "absent",
                "--target",
                "supabase:" + "a" * 20,
                "--expected-sha256",
                "0" * 64,
                "--out-dir",
                str(tmp_path),
            ]
        )
        == 1
    )


def test_rest_environment_cannot_redirect_to_another_project(monkeypatch):
    from scripts.repair_history_store import RestStore

    monkeypatch.setenv("SUPABASE_URL", "https://wrong.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake")
    with pytest.raises(RepairConflict, match="target"):
        RestStore("supabase:" + "a" * 20)


def test_manifest_project_must_match_supabase_locator(bundle):
    path, manifest, _, _ = bundle
    manifest["target"] = "supabase:" + "a" * 20
    write_json(path, manifest)
    with pytest.raises(RepairConflict, match="project"):
        load_manifest(path, expected_sha256=file_hash(path), target=manifest["target"])


def test_current_json_boolean_does_not_match_reviewed_number(bundle):
    path, manifest, store, _ = bundle
    before = manifest["operations"][0]["before"]
    before["value"] = 1
    write_json(Path(manifest["backups"][0]["path"]), [before])
    manifest["backups"][0]["sha256"] = file_hash(Path(manifest["backups"][0]["path"]))
    store.rows[("metric_history", "x", "2026-07-31")]["value"] = True
    write_json(path, manifest)
    with pytest.raises(RepairConflict, match="changed"):
        run(bundle)
    assert store.writes == 0
