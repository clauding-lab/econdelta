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
    """Offline stand-in for the local git calls; records what was asked. Every guarded file is
    tracked and byte-identical to HEAD unless named in `untracked` / `modified`."""

    def __init__(self, head: str = HEAD, porcelain: str = "", on_main: bool = True,
                 ls_files: str = "H scripts/nbr_round4_candidates.py\n",
                 toplevel: str | None = None, untracked: frozenset[str] = frozenset(),
                 modified: frozenset[str] = frozenset(), replace_refs: str = "") -> None:
        self.head, self.porcelain, self.on_main = head, porcelain, on_main
        self.replace_refs = replace_refs
        self.ls_files = ls_files
        self.toplevel = str(r4.REPO_ROOT) if toplevel is None else toplevel
        self.untracked, self.modified = untracked, modified
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(args)
        if args == ["rev-parse", "--show-toplevel"]:
            return 0, self.toplevel + "\n"
        if args == ["rev-parse", "HEAD"]:
            return 0, self.head + "\n"
        if args == ["for-each-ref", "--format=%(refname)", "refs/replace/"]:
            return 0, self.replace_refs
        if args == ["status", "--porcelain", "--untracked-files=all"]:
            return 0, self.porcelain
        if args == ["ls-files", "-v"]:
            return 0, self.ls_files
        if args == ["merge-base", "--is-ancestor", "HEAD", "origin/main"]:
            return (0 if self.on_main else 1), ""
        if args[:3] == ["ls-files", "--error-unmatch", "--"]:
            return (1 if args[3] in self.untracked else 0), ""
        if args[:2] == ["rev-parse", "--verify"] and args[2].startswith("HEAD:"):
            rel = args[2].removeprefix("HEAD:")
            return (128, "") if rel in self.untracked else (0, f"blob-{rel}\n")
        if args[:3] == ["hash-object", "--no-filters", "--"]:
            return 0, ("edited-" if args[3] in self.modified else "blob-") + args[3] + "\n"
        raise AssertionError(f"unexpected git call {args}")


GUARDED = {"scripts/nbr_round4_candidates.py", "scripts/nbr_round4_guard.py",
           "scripts/repair_observation_history.py", "scripts/history_repair_candidates.py",
           "utils/observations.py"}


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
        (FakeGit(toplevel="/elsewhere/econdelta"), "is not the module's repo root"),
        (FakeGit(replace_refs=f"refs/replace/{HEAD}\n"), "git replace refs are present"),
        (FakeGit(untracked=frozenset({"scripts/nbr_round4_candidates.py"})), "is not tracked"),
        (FakeGit(modified=frozenset({"scripts/nbr_round4_candidates.py"})),
         "differs from its committed bytes"),
        (FakeGit(modified=frozenset({"scripts/repair_observation_history.py"})),
         "differs from its committed bytes"),
    ):
        assert _main_build(world, git, output=output) == 1
        assert refusal in capsys.readouterr().out and not output.exists()

    git = FakeGit()
    assert _main_build(world, git, output=output) == 0
    assert ["rev-parse", "--show-toplevel"] in git.calls
    assert {call[-1] for call in git.calls if call[0] == "hash-object"} == GUARDED
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
    (repo / ".gitignore").write_text("data/\n")  # as in econdelta: data/ is git-ignored
    _git(repo, "add", "reviewed.py", ".gitignore")
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
    assert _guard(repo, head) == head  # clean: passes
    if hide == "untracked-with-showUntrackedFiles-no":
        _git(repo, "config", "status.showUntrackedFiles", "no")
        (repo / "stray.py").write_text("x = 1\n")
    else:
        _git(repo, "update-index", f"--{hide}", "reviewed.py")
        (repo / "reviewed.py").write_text("REVIEWED = 'hand-edited'\n")
    assert _git(repo, "status", "--porcelain") == ""  # plain status is blind here

    with pytest.raises(r4.RepairConflict, match="checkout is not clean"):
        _guard(repo, head)


def _guard(repo: Path, head: str, *files: Path, root: Path | None = None) -> str:
    return r4.commit_guard(_runner(root or repo), head, check_main=False, repo_root=root or repo,
                           files=files or (repo / "reviewed.py",))


def _shadow(repo: Path) -> Path:
    """A copy of the reviewed module in a git-IGNORED folder: git status cannot see it."""
    shadow = repo / "data" / "shadow"
    shadow.mkdir(parents=True)
    (shadow / "reviewed.py").write_text("REVIEWED = 'HAND-EDITED, NOT REVIEWED'\n")
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""
    return shadow


def test_build_refuses_a_module_copy_whose_repo_root_is_not_the_git_toplevel(
    tmp_path, monkeypatch, capsys
):
    """Probe p_guard (round-4 safety review r3): a copy of the module under an ignored data/
    folder of a clean checkout derived REPO_ROOT from its own location and passed the guard."""
    world = _world(tmp_path)
    repo, head = _repo(tmp_path)
    monkeypatch.setattr(r4, "REVIEWED", world.review)
    monkeypatch.setattr(r4, "REPO_ROOT", _shadow(repo))  # where the shadow copy would live
    argv = ["--build", "--preview", "--recapture-dir", str(world.r4), "--night1-backup-dir",
            str(world.n1), "--target", "snapshot:round4-nbr", "--econdelta-commit", head,
            "--brief-commit", "d" * 40]

    assert r4.main(argv) == 1  # the REAL local git runner, offline

    out = capsys.readouterr().out
    assert "is not the module's repo root" in out and "PREVIEW" not in out


def test_commit_guard_refuses_a_guarded_file_that_is_untracked_or_outside_the_repo(tmp_path):
    repo, head = _repo(tmp_path)
    shadow = _shadow(repo)
    outside = tmp_path / "elsewhere.py"
    outside.write_text("REVIEWED = None\n")

    with pytest.raises(r4.RepairConflict, match="is not the module's repo root"):
        _guard(repo, head, root=shadow)
    with pytest.raises(r4.RepairConflict, match="data/shadow/reviewed.py is not tracked"):
        _guard(repo, head, shadow / "reviewed.py")
    with pytest.raises(r4.RepairConflict, match="outside the repo root"):
        _guard(repo, head, outside)
    assert _guard(repo, head, repo / "reviewed.py", repo / ".gitignore") == head


# --- T18: git replace refs and a caller's GIT_* environment cannot fake the comparison ------


def _replace_head_with_lookalike(repo: Path, head: str, base: str = "refs/replace/") -> None:
    """Probe p_replace (round-4 close safety review r1): hand-edit REVIEWED, stage it, and point
    a replace ref at a look-alike commit (same parent: none) whose tree holds the edited blob.
    `rev-parse HEAD` still prints the real commit; status and HEAD:path follow the replacement."""
    (repo / "reviewed.py").write_text("REVIEWED = 'HAND-EDITED, NOT REVIEWED'\n")
    _git(repo, "add", "reviewed.py")
    lookalike = _git(repo, "commit-tree", _git(repo, "write-tree").strip(), "-m",
                     "reviewed").strip()
    _git(repo, "update-ref", f"{base}{head}", lookalike)


def _real_guard(repo: Path, head: str) -> str:
    """The production runner (scripts/nbr_round4_guard.local_git), not the test's plain one."""
    return r4.commit_guard(r4.local_git(repo), head, check_main=False, repo_root=repo,
                           files=(repo / "reviewed.py",))


def test_commit_guard_refuses_a_replace_ref_that_disguises_a_hand_edit(tmp_path):
    repo, head = _repo(tmp_path)
    _replace_head_with_lookalike(repo, head)
    assert _git(repo, "rev-parse", "HEAD").strip() == head  # the real commit's name
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""  # blind

    with pytest.raises(r4.RepairConflict, match="git replace refs are present"):
        _real_guard(repo, head)


def test_commit_guard_ignores_replace_objects_and_the_callers_git_environment(
    tmp_path, monkeypatch
):
    """A replace ref under a base named by the caller's GIT_REPLACE_REF_BASE is not listed under
    refs/replace, so only --no-replace-objects and a scrubbed environment catch it."""
    repo, head = _repo(tmp_path)
    _replace_head_with_lookalike(repo, head, base="refs/disguise/")
    monkeypatch.setenv("GIT_REPLACE_REF_BASE", "refs/disguise/")
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""  # blind

    with pytest.raises(r4.RepairConflict, match="checkout is not clean"):
        _real_guard(repo, head)


def test_commit_guard_answers_for_the_modules_repo_not_a_git_dir_in_the_environment(
    tmp_path, monkeypatch
):
    repo, head = _repo(tmp_path)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _git(decoy, "init", "-q")
    (decoy / "other.py").write_text("x = 1\n")
    _git(decoy, "add", "other.py")
    _git(decoy, "commit", "-q", "-m", "decoy")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    monkeypatch.setenv("GIT_INDEX_FILE", str(decoy / ".git" / "index"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(decoy / ".git" / "objects"))

    assert _real_guard(repo, head) == head
