"""Round 4 (owner decision D2, 2 Oct 2026): the commit guard binds a candidate to its code.

Business rule: ``--build`` stamps ``code_commits.econdelta`` only when HEAD is the named commit,
the checkout is clean (even against local settings that hide changes), HEAD is on origin/main
unless the owner waived it, git's toplevel IS the module's repo root, and every module that
decides the candidate's bytes is tracked and byte-identical to HEAD. Split out of
tests/test_nbr_round4_candidates.py (whose world and helpers this file imports) for the 800-line
cap. Every row dated after 2026-09-24 is SYNTHETIC.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import scripts.nbr_round4_candidates as r4
from scripts.repair_observation_history import file_hash
from tests.test_nbr_round4_candidates import CHILD, HEAD, PARENT, _main_build, _tree, _world


@pytest.fixture(autouse=True)
def _service_key(monkeypatch):
    """The world's snapshots come from the real exporter, which needs a service key."""
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "sb_secret_synthetic-service-key")


# --- T18: the commit guard binds the candidate to the clean, merged code that built it -----


class FakeGit:
    """Offline stand-in for the four local git calls; records what was asked."""

    def __init__(self, head: str = HEAD, porcelain: str = "", on_main: bool = True,
                 ls_files: str = "H scripts/nbr_round4_candidates.py\n") -> None:
        self.head, self.porcelain, self.on_main = head, porcelain, on_main
        self.ls_files = ls_files
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(args)
        if args == ["rev-parse", "HEAD"]:
            return 0, self.head + "\n"
        if args == ["status", "--porcelain", "--untracked-files=all"]:
            return 0, self.porcelain
        if args == ["ls-files", "-v"]:
            return 0, self.ls_files
        if args == ["merge-base", "--is-ancestor", "HEAD", "origin/main"]:
            return (0 if self.on_main else 1), ""
        raise AssertionError(f"unexpected git call {args}")


def test_build_refuses_a_head_that_is_not_the_named_commit_a_dirty_tree_or_a_commit_off_main(
    tmp_path, monkeypatch, capsys
):
    world = _world(tmp_path)
    monkeypatch.setattr(r4, "REVIEWED", world.review)
    output = tmp_path / "candidate" / "candidate.json"
    for git, refusal in (
        (FakeGit(head="f" * 40), "HEAD ffff"),
        (FakeGit(porcelain="?? stray.py\n"), "checkout is not clean"),
        (FakeGit(ls_files="h scripts/nbr_round4_candidates.py\n"), "assume-unchanged"),
        (FakeGit(ls_files="S scripts/nbr_round4_candidates.py\n"), "skip-worktree"),
        (FakeGit(on_main=False), "not an ancestor of origin/main"),
    ):
        assert _main_build(world, git, output=output) == 1
        assert refusal in capsys.readouterr().out and not output.exists()

    git = FakeGit()
    assert _main_build(world, git, output=output) == 0
    candidate = json.loads(output.read_text())
    assert candidate["code_commits"] == {"econdelta": HEAD, "brief": "d" * 40}
    assert len(candidate["operations"]) == 21
    assert f"21 exact operations; candidate sha256={file_hash(output)}" in capsys.readouterr().out
    assert _main_build(world, FakeGit(), output=output) == 1  # never overwrites reviewed bytes
    assert "candidate already exists" in capsys.readouterr().out


def test_owner_ruled_unmerged_records_the_ruling_and_head_in_the_candidate(
    tmp_path, monkeypatch
):
    world = _world(tmp_path)
    monkeypatch.setattr(r4, "REVIEWED", world.review)
    output = tmp_path / "candidate.json"
    git = FakeGit(on_main=False)
    ruling = "D2(b): owner rules the PR-B head may build before merge (SYNTHETIC)"

    assert _main_build(world, git, "--owner-ruled-unmerged", ruling, output=output) == 0

    unresolved = json.loads(output.read_text())["unresolved"]
    assert any(ruling in line and HEAD in line for line in unresolved)
    assert ["merge-base", "--is-ancestor", "HEAD", "origin/main"] not in git.calls


def test_preview_writes_no_file_and_skips_only_the_origin_main_check(
    tmp_path, monkeypatch, capsys
):
    world = _world(tmp_path)
    monkeypatch.setattr(r4, "REVIEWED", world.review)
    before = _tree(tmp_path)
    git = FakeGit(on_main=False)

    assert _main_build(world, git, "--preview") == 0

    out = capsys.readouterr().out
    assert _tree(tmp_path) == before
    assert "PREVIEW: 21 exact operations; no file written" in out
    assert f"  {CHILD}: 2026-09-25, 2026-09-26, 2026-09-27, 2026-09-29" in out
    assert f"  kept {PARENT} 2026-06-30 415473.0" in out
    assert ["merge-base", "--is-ancestor", "HEAD", "origin/main"] not in git.calls
    assert _main_build(world, FakeGit(porcelain=" M scripts/x.py\n"), "--preview") == 1
    assert _main_build(world, FakeGit(head="f" * 40), "--preview") == 1


# --- the commit guard, against a REAL local git repo (offline) ------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=round4", "-c", "user.email=r4@example.invalid",
                           *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "reviewed.py").write_text("REVIEWED = None\n")
    _git(repo, "add", "reviewed.py")
    _git(repo, "commit", "-q", "-m", "reviewed")
    return repo, _git(repo, "rev-parse", "HEAD").strip()


def _runner(repo: Path):
    def run(args: list[str]) -> tuple[int, str]:
        done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
        return done.returncode, done.stdout
    return run


@pytest.mark.parametrize(
    "hide",
    [
        pytest.param("untracked-with-showUntrackedFiles-no", id="untracked-hidden-by-config"),
        pytest.param("skip-worktree", id="edit-hidden-by-skip-worktree"),
        pytest.param("assume-unchanged", id="edit-hidden-by-assume-unchanged"),
    ],
)
def test_commit_guard_sees_changes_that_local_git_settings_hide(tmp_path, hide):
    repo, head = _repo(tmp_path)
    assert r4.commit_guard(_runner(repo), head, check_main=False) == head  # clean: passes
    if hide == "untracked-with-showUntrackedFiles-no":
        _git(repo, "config", "status.showUntrackedFiles", "no")
        (repo / "stray.py").write_text("x = 1\n")
    else:
        _git(repo, "update-index", f"--{hide}", "reviewed.py")
        (repo / "reviewed.py").write_text("REVIEWED = 'hand-edited'\n")
    assert _git(repo, "status", "--porcelain") == ""  # plain status is blind here

    with pytest.raises(r4.RepairConflict, match="checkout is not clean"):
        r4.commit_guard(_runner(repo), head, check_main=False)
