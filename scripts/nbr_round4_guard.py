"""Round 4 commit guard: a candidate's ``code_commits.econdelta`` names the code that built it.

Split out of scripts/nbr_round4_candidates.py (800-line cap). Local git only (rev-parse, status,
ls-files, hash-object, merge-base); the guard never touches the network.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

from scripts.repair_observation_history import RepairConflict

GitRunner = Callable[[list[str]], tuple[int, str]]  # git args -> (exit code, stdout)


def _git_env() -> dict[str, str]:
    """The caller's environment minus every GIT_* variable: GIT_DIR, GIT_WORK_TREE,
    GIT_INDEX_FILE, GIT_OBJECT_DIRECTORY, GIT_ALTERNATE_OBJECT_DIRECTORIES, GIT_REPLACE_REF_BASE
    or GIT_CONFIG_* would point the guard at another repo, index, object store or replace base."""
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def local_git(repo_root: Path) -> GitRunner:
    """Run git in ``repo_root``; the module's own location, never the caller's cwd.

    ``--no-replace-objects``: a ``refs/replace/*`` entry makes ``HEAD:<path>`` and status read a
    look-alike commit while ``rev-parse HEAD`` still prints the real one (probe p_replace)."""

    def run(args: list[str]) -> tuple[int, str]:
        done = subprocess.run(["git", "--no-replace-objects", *args], cwd=repo_root,
                              env=_git_env(), capture_output=True, text=True, check=False)
        return done.returncode, done.stdout

    return run


def _check_no_replace_refs(git: GitRunner) -> None:
    """Belt and braces with --no-replace-objects: name the disguise instead of a generic
    'not clean'. Grafts (.git/info/grafts) change only parents, never a commit's tree, so they
    cannot fake this comparison; they can fake the origin/main check (see commit_guard)."""
    code, refs = git(["for-each-ref", "--format=%(refname)", "refs/replace/"])
    if code != 0 or refs.strip():
        raise RepairConflict("git replace refs are present (refs/replace/*), which can disguise "
                             f"an edited commit: {refs.split() or '?'}")


def _check_toplevel(git: GitRunner, repo_root: Path) -> None:
    """A module copied into an ignored folder (data/<x>/) derives its own REPO_ROOT; git status
    there is clean, so the root must BE git's toplevel, not merely inside a clean checkout."""
    code, top = git(["rev-parse", "--show-toplevel"])
    top = top.strip()
    if code != 0 or not top or Path(top).resolve() != repo_root.resolve():
        raise RepairConflict(f"git toplevel {top or '?'} is not the module's repo root {repo_root}")


def _check_files(git: GitRunner, repo_root: Path, files: tuple[Path, ...]) -> None:
    """Each module that decides the candidate's bytes is tracked and byte-identical to HEAD.

    Raw bytes (``--no-filters``) against the HEAD blob: index flags and attribute filters can
    hide an edit from status and diff, never from this comparison."""
    root = repo_root.resolve()
    for path in files:
        try:
            rel = path.resolve().relative_to(root).as_posix()
        except ValueError:
            raise RepairConflict(f"{path} is outside the repo root {repo_root}") from None
        if git(["ls-files", "--error-unmatch", "--", rel])[0] != 0:
            raise RepairConflict(f"{rel} is not tracked by git")
        blob_code, blob = git(["rev-parse", "--verify", f"HEAD:{rel}"])
        hash_code, actual = git(["hash-object", "--no-filters", "--", rel])
        if blob_code != 0 or hash_code != 0 or not blob.strip() or blob.strip() != actual.strip():
            raise RepairConflict(f"{rel} differs from its committed bytes at HEAD")


def commit_guard(
    git: GitRunner, commit: str, *, check_main: bool, repo_root: Path, files: tuple[Path, ...]
) -> str:
    """The module's root is git's toplevel, HEAD is the named commit, the checkout is clean, every
    guarded file is tracked and unmodified, and (unless waived) HEAD is on origin/main.

    "Clean" overrides local settings that hide changes: status.showUntrackedFiles=no (forced to
    --untracked-files=all) and assume-unchanged / skip-worktree entries (lowercase or S tag in
    `git ls-files -v`), either of which would hide a hand-edited REVIEWED literal; replace refs
    are refused and ignored (``local_git`` runs ``--no-replace-objects`` with GIT_* scrubbed).

    The origin/main check catches honest mistakes only: origin/main is a local, writable ref,
    and grafts can fake ``--is-ancestor``. It is not tamper-proof and claims nothing more."""
    _check_toplevel(git, repo_root)
    _check_no_replace_refs(git)
    code, head = git(["rev-parse", "HEAD"])
    head = head.strip()
    if code != 0 or head != commit:
        raise RepairConflict(f"HEAD {head or '?'} is not --econdelta-commit {commit}")
    code, status = git(["status", "--porcelain", "--untracked-files=all"])
    hidden_code, listing = git(["ls-files", "-v"])
    hidden = [line for line in listing.splitlines() if line[:1].islower() or line[:1] == "S"]
    if code != 0 or status.strip() or hidden_code != 0 or hidden:
        raise RepairConflict("checkout is not clean (edited, staged or untracked files, or "
                             "files marked assume-unchanged / skip-worktree)")
    _check_files(git, repo_root, files)
    if check_main:
        code, _ = git(["merge-base", "--is-ancestor", "HEAD", "origin/main"])
        if code != 0:
            raise RepairConflict(f"HEAD {head} is not an ancestor of origin/main (run "
                                 "`git fetch origin`; or the owner rules D2(b))")
    return head
