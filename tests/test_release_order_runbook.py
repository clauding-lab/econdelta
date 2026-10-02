"""The deploy runbook carries the measured release order of the data-reliability repair (R3d).

Controller ruling 28 Sep 2026 (The Brief's G2 compatibility evidence), step 2 amended 29 Sep
2026: the producer ships FIRST (with the H5 NBR source-period dating), merged before the 01:00
BDT econdelta-gitpull pulls it; the R1 history repair is applied after that merge but before
the new producer's first metric_history write (02:55 BDT aggregate, 03:15 BDT retry); only then
The Brief's migration 0006 and the new Brief. A rollback starts with The Brief. The older
'Brief consumer first' order was derived from a never-deployed hypothetical and is superseded.

Static checks of deploy/README.md only: nothing here runs a command or touches a box.
"""
from __future__ import annotations

import re
from pathlib import Path

RUNBOOK = Path(__file__).resolve().parent.parent / "deploy" / "README.md"

MILESTONES = (
    ("producer", (r"\bH5\b", r"before the 01:00 BDT")),
    ("history repair", (r"\bR1\b", r"metric_history", r"02:55 BDT", r"03:15 BDT")),
    ("brief migration", (r"0006",)),
    ("brief", (r"08:00 BDT", r"the-brief")),
)


def _order() -> str:
    match = re.search(r"^## [^\n]*release order[^\n]*\n(.*?)(?=^## |\Z)", RUNBOOK.read_text(),
                      flags=re.MULTILINE | re.DOTALL | re.IGNORECASE)
    assert match, "no '## …release order…' section in deploy/README.md"
    return match.group(1)


def _numbered_steps(body: str) -> list[str]:
    return [" ".join(m.group(0).split()) for m in re.finditer(r"^\d+\.\s.*?(?=^\d+\.\s|^\S|\Z)", body,
                                            flags=re.MULTILINE | re.DOTALL)]


def _step_of(steps: list[str], patterns: tuple[str, ...]) -> int | None:
    found = [i for i, s in enumerate(steps) if all(re.search(p, s, flags=re.DOTALL) for p in patterns)]
    return found[0] if found else None


def test_producer_ships_first_then_history_repair_then_the_brief() -> None:
    steps = _numbered_steps(_order())
    positions = {name: _step_of(steps, patterns) for name, patterns in MILESTONES}
    assert None not in positions.values(), positions
    ordered = [positions[name] for name, _ in MILESTONES]
    assert ordered == sorted(ordered) and len(set(ordered)) == len(ordered), positions


def _rollback_paragraph(body: str) -> str:
    match = re.search(r"^\*\*Roll ?back\b.*?(?=\n\s*\n|\Z)", body, flags=re.MULTILINE | re.DOTALL | re.IGNORECASE)
    assert match, "no bold '**Roll back…' paragraph in the release-order section"
    return " ".join(match.group(0).split())


def _timing_words_before(step: str, event: str) -> list[str]:
    """The word (before/after) that qualifies each mention of `event` in one step."""
    return [w.lower() for w in re.findall(rf"\b(before|after)\s+{event}", step, flags=re.IGNORECASE)]


def test_rollback_starts_with_the_brief() -> None:
    rollback = _rollback_paragraph(_order())
    assert re.match(r"\*\*(?:Rollback:\s*)?Roll back the Brief first\b", rollback, flags=re.IGNORECASE), rollback[:80]
    assert not re.search(r"\b(never|not|n't)\b[^.]{0,40}roll back the Brief first", rollback, flags=re.IGNORECASE)
    assert rollback.find("Brief") < rollback.find("producer")


def test_history_repair_lands_before_the_producers_first_history_write() -> None:
    steps = _numbered_steps(_order())
    repair = steps[_step_of(steps, MILESTONES[1][1])]
    words = _timing_words_before(repair, r"(?:its|the (?:new )?producer's) first `?metric_history`? write")
    assert words and set(words) == {"before"}, words


def test_producer_is_merged_before_the_nightly_gitpull() -> None:
    steps = _numbered_steps(_order())
    producer = steps[_step_of(steps, MILESTONES[0][1])]
    words = _timing_words_before(producer, r"(?:the )?01:00 BDT")
    assert words and set(words) == {"before"}, words


# G2 §8 (The Brief's fixG2-compat.md) and the 28 Sep ruling: the deployed Brief on this producer
# is NOT unchanged; the measured transition-window effects belong in the rollout plan.
def test_release_order_carries_the_measured_transition_window_caveats() -> None:
    order = _order()
    for caveat in (r"Trade Gap", r"quarantin", r"02:55 BDT archive"):
        assert re.search(caveat, order), caveat


# R3d fix round 2: the caveat's keyword alone let "keep its Trade Gap card" pass (reviewer probe
# review-scratch/fixR3d-spec-r1/probe.py, mutant trade-gap-kept); pin what the old Brief does.
def test_transition_window_says_the_old_brief_hides_its_trade_gap_card() -> None:
    order = " ".join(_order().replace("**", "").split())
    assert re.search(r"\bhide its Trade Gap card\b", order)
    assert not re.search(r"\b(?:keeps?|shows?|still shows?)\s+its Trade Gap card", order, flags=re.IGNORECASE)


def test_no_release_doc_claims_the_deployed_brief_is_unchanged_on_this_producer() -> None:
    root = RUNBOOK.parent.parent
    for name in ("AGENTS.md", "AGENT_LEARNINGS.md", "deploy/README.md"):
        assert not re.search(r"byte-identical cards|builds the same cards", (root / name).read_text()), name


# R3d controller close (2 Oct 2026): round-2 reviewers inverted step 4's timing, its wait for the
# producer's first write, and step 2's timer stop, and all tests still passed
# (review-scratch/fixR3d-adversarial-r2/r2_mutants.py). These sentences are pinned verbatim.
_BINDING_RELEASE_SENTENCES = (
    "**The new Brief, before its 08:00 BDT fire**, once this producer's first daily write has landed",
    "If R1 cannot finish in time, stop `econdelta-aggregate.timer` and `econdelta-aggregate-retry.timer` until it has",
    "Before the new Brief's first fire the owner also applies the round-4 NBR exclusion",
)


def test_binding_release_sentences_are_stated_exactly() -> None:
    order = " ".join(_order().split())
    missing = [s for s in _BINDING_RELEASE_SENTENCES if s not in order]
    assert missing == [], missing


def test_release_order_never_inverts_its_timing_rules() -> None:
    order = " ".join(_order().split())
    inversions = (
        r"\bafter (?:its|the new Brief's) (?:08:00 BDT|first) fire",
        r"\b(?:no need|not need|without)\b[^.]{0,40}first daily write",
        r"\blet\b[^.]{0,60}aggregate[^.]{0,40}\brun\b",
    )
    found = [p for p in inversions if re.search(p, order, flags=re.IGNORECASE)]
    assert found == [], found


# R1 refresh close (2 Oct 2026 BDT): the reviewed R1 manifest was built from the 25 Sep backup and
# the old producer re-stamps rows, so Night-1 refreshes the before-images from the final backup
# (scripts/repair_refresh_before_images.py). The recheck run against that same backup always
# refuses (exit 2: backup-is-candidate-source, backup-not-after-candidate), so a runbook that still
# demanded its `match` would strand the operator. Pin the new sequence in all three Night-1 texts.
_FINAL_BACKUP_CMD = "python -m scripts.export_history --repair-snapshot DIR --r1-final-backup"
_NIGHT1_SEQUENCE = (
    _FINAL_BACKUP_CMD,
    "python -m scripts.repair_refresh_before_images --manifest",
    "python -m scripts.repair_observation_history --plan --candidate",
    "python -m scripts.repair_observation_history --apply",
)


def _night1_texts() -> dict[str, str]:
    root = RUNBOOK.parent.parent
    agents = re.search(r"^60\. .*$", (root / "AGENTS.md").read_text(), re.MULTILINE)
    runbook = (root / "docs/reviews/2026-09-25-history-repair-manifest.md").read_text()
    item4 = re.search(r"^4\. .*$", runbook.split("## Safety contract", 1)[1], re.MULTILINE)
    steps = _numbered_steps(_order())
    assert agents and item4
    return {"landmine 60": agents.group(0), "runbook item 4": item4.group(0),
            "deploy step 2": steps[_step_of(steps, MILESTONES[1][1])]}


def test_night1_runs_backup_then_refresh_then_engine_plan_then_apply() -> None:
    for name, text in _night1_texts().items():
        text = " ".join(text.split())
        positions = [text.find(command) for command in _NIGHT1_SEQUENCE]
        assert -1 not in positions and positions == sorted(positions), (name, positions)
        assert "Validated 2016 operations" in text, name
        assert re.search(r"refresh run replaces the recheck for a refreshed candidate", text), name
        assert re.search(r"--reference-backup`? is (?:still )?never a Night-1 pass", text), name


def test_no_night1_text_demands_a_recheck_match_for_the_refreshed_candidate() -> None:
    for name, text in _night1_texts().items():
        text = " ".join(text.split())
        assert not re.search(r"refreshed candidate[^;]{0,80}must print `?match", text), name
        # Any surviving "must print `match`" demand is scoped to a candidate that was NOT refreshed.
        for demand in re.finditer(r"must print `?match", text):
            assert "not refreshed" in text[max(0, demand.start() - 400):demand.start()], name
