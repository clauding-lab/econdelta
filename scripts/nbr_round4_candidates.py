"""Round 4 (owner decision D2, 2 Oct 2026): build the post-24-Sep NBR restamp exclusion, never apply it.

After R1 (decisions (d)+(l), the 140 reviewed days to 2026-09-24) the deployed producer kept
re-stamping the standing fiscal-year NBR total with each capture day (landmine 47) until the new
producer replaced it. Those rows are removed here by EXACT key and value, transcribed into
``REVIEWED`` from a writer-paused Day-2 recapture after a signed human review. Nothing is derived
by predicate: source and provenance cannot tell a restamp from the producer's period row
(utils/supabase_writer.py _DEFAULT_SOURCE). Every check is a refusal, never a filter. The
producer's period rows, pre-window rows and the retired corroborators are never touched.

Modes: ``--draft`` (proposal only, writes nothing), ``--build`` (candidate for the unchanged
``repair_observation_history --plan/--apply/--restore`` engine), ``--verify-unchanged`` (read-only
drift receipt). Runbook: docs/reviews/2026-09-25-history-repair-manifest.md "Round 4".
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from scripts.history_repair_candidates import (
    NBR_PARENT_RESTAMP_RUNS,
    NBR_RESTAMP_ID,
    NBR_RESTAMP_RUNS,
    NBR_RESTAMP_WINDOW,
    nbr_restamp_rows,
)
from scripts.repair_observation_history import RepairConflict, Row, file_hash, same
from utils.observations import BRIEF_CONVERSIONS


@dataclass(frozen=True)
class Round4Review:
    """The reviewed Day-2 recapture, transcribed by hand. The only thing PR-B changes."""

    last_capture_day: str  # the old producer's last restamp as_of, FROM DATA, never the calendar
    missing_days: frozenset[str]  # capture days in [first, last] with no row (all three ids)
    runs: dict[str, tuple[tuple[float, str, str], ...]]  # id -> (value, first, last) inclusive
    alias_quirks: dict[str, tuple[tuple[str, float], ...]]  # alias id -> ((as_of, alias value),)
    acknowledged_month_end_days: frozenset[str]  # month-end capture days a signed line accepted
    keep_period_rows: dict[str, tuple[tuple[str, float], ...]]  # id -> ((as_of, value), ...)
    recapture_sha256: str  # Day-2 recapture metric_history.json
    night1_sha256: str  # Night-1 metric_history snapshot (must equal the run-sheet record)


ROUND4_FIRST_DAY = "2026-09-25"  # = NBR_RESTAMP_WINDOW[1] + 1 day
ROUND4_IDS = (NBR_RESTAMP_ID, *NBR_PARENT_RESTAMP_RUNS)  # child, parent, alias
REVIEWED: Round4Review | None = None  # PLACEHOLDER until the Day-2 review


# Owner decision D2 (2 Oct 2026). Wording PENDING OWNER CONFIRMATION (round-4 design OQ6): it
# becomes bytes in every operation, so a change here changes the reviewed candidate hash.
ROUND4_REASON = (
    "Owner decision D2 (2 Oct 2026), extending (d)+(l): archive/exclude a daily restamp, dated "
    "after 2026-09-24, of the fiscal-year cumulative NBR collection (fiscal_nbr_collected_trn, "
    "tax_revenue or its alias nbr_fytd_collected_cr); as_of is the capture date, not the "
    "collection period. Original complete row remains in the hashed Day-2 recapture. The "
    "producer's period rows, pre-window rows and retired corroborators "
    "nbr_fytd_collected_dailystar/_tbs unchanged."
)
EXPECTED_PROJECT = "ssbliukchgibjcjohibi"
WINDOW_START, WINDOW_END = (day.isoformat() for day in NBR_RESTAMP_WINDOW)
PARENT_ID, CHILD_FACTOR = BRIEF_CONVERSIONS[NBR_RESTAMP_ID]
(ALIAS_ID,) = (mid for mid in NBR_PARENT_RESTAMP_RUNS if mid != PARENT_ID)
RETIRED_CORROBORATORS = ("nbr_fytd_collected_dailystar", "nbr_fytd_collected_tbs")
BANNER = "PROPOSAL ONLY: a human must check and transcribe this; it decides nothing"
LITERAL_HEADER = "# DRAFT: paste into REVIEWED only after review"
BDT = timezone(timedelta(hours=6))
Key = tuple[str, str]


@dataclass(frozen=True)
class Snapshot:
    """One verified split-layout metric_history snapshot (scripts/export_history.py)."""

    label: str
    directory: Path
    manifest: dict
    manifest_sha256: str
    table_path: Path
    table_sha256: str
    rows: list[Row]
    index: dict[Key, Row]


def load_snapshot(directory: Path, label: str) -> Snapshot:
    """Read only metric_history.json, verified against its own manifest.json."""
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("target_project") != EXPECTED_PROJECT:
        raise RepairConflict(
            f"{label} snapshot target project {manifest.get('target_project')!r} "
            f"is not {EXPECTED_PROJECT}"
        )
    ref = manifest.get("tables", {}).get("metric_history")
    if not ref or list(ref.get("key", [])) != ["metric_id", "as_of"]:
        raise RepairConflict(f"{label} manifest has no metric_history entry keyed metric_id, as_of")
    if not manifest.get("started_at"):
        raise RepairConflict(f"{label} manifest has no started_at")
    path = directory / "metric_history.json"
    table_sha256 = file_hash(path)
    if table_sha256 != ref["sha256"]:
        raise RepairConflict(f"{label} metric_history.json hash differs from its manifest")
    rows = json.loads(path.read_text())
    if not isinstance(rows, list) or len(rows) != ref["rows"]:
        raise RepairConflict(f"{label} metric_history row count differs from its manifest")
    index: dict[Key, Row] = {}
    for row in rows:
        key = (str(row["metric_id"]), str(row["as_of"]))
        if key in index:
            raise RepairConflict(f"{label} snapshot has a duplicate key {key}")
        index[key] = row
    return Snapshot(label, directory, manifest, file_hash(manifest_path), path, table_sha256,
                    rows, index)


def _days(first: str, last: str) -> list[str]:
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    return [(start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)]


def _month_end(day: str) -> bool:
    return (date.fromisoformat(day) + timedelta(days=1)).day == 1


def _family(snapshot: Snapshot, mid: str, first: str = "", last: str = "9999") -> list[Row]:
    return sorted(
        (r for r in snapshot.rows if r["metric_id"] == mid and first <= str(r["as_of"]) <= last),
        key=lambda r: str(r["as_of"]),
    )


def _bdt(stamp: object) -> str:
    try:
        return datetime.fromisoformat(str(stamp)).astimezone(BDT).strftime("%Y-%m-%d %H:%M BDT")
    except ValueError:
        return f"{stamp!r} (unparsed)"


def capture_days(review: Round4Review) -> list[str]:
    """The reviewed post-window capture days: [first..last] minus the missing days."""
    days = _days(ROUND4_FIRST_DAY, review.last_capture_day)
    return [day for day in days if day not in review.missing_days]


def _run_value(runs: tuple[tuple[float, str, str], ...], day: str) -> float | None:
    values = [value for value, first, last in runs if first <= day <= last]
    return values[0] if len(values) == 1 else None


def compress_runs(rows: list[Row]) -> tuple[tuple[float, str, str], ...]:
    """Consecutive equal values (in as_of order, across missing days) as (value, first, last)."""
    runs: list[tuple[float, str, str]] = []
    for row in rows:
        if runs and same(runs[-1][0], row["value"]):
            runs[-1] = (runs[-1][0], runs[-1][1], str(row["as_of"]))
        else:
            runs.append((row["value"], str(row["as_of"]), str(row["as_of"])))  # type: ignore[arg-type]
    return tuple(runs)


def check_night1_provenance(n1: Snapshot) -> None:
    """N1 is the pre-R1 Night-1 snapshot: its window rows are exactly R1's 420 reviewed restamps."""
    for mid in ROUND4_IDS:
        window = _family(n1, mid, WINDOW_START, WINDOW_END)
        runs, decision = (
            (NBR_RESTAMP_RUNS, "(d)") if mid == NBR_RESTAMP_ID
            else (NBR_PARENT_RESTAMP_RUNS[mid], "(l)")
        )
        try:
            nbr_restamp_rows(window, mid, runs, decision)
        except RepairConflict as exc:
            raise RepairConflict(f"Night-1 snapshot is not the pre-R1 backup: {exc}") from exc


def _alias_differences(r4: Snapshot) -> list[tuple[str, object, object]]:
    out = []
    for row in _family(r4, ALIAS_ID, ROUND4_FIRST_DAY):
        parent = r4.index.get((PARENT_ID, str(row["as_of"])))
        if parent is not None and not same(parent["value"], row["value"]):
            out.append((str(row["as_of"]), row["value"], parent["value"]))
    return out


def propose_review(r4: Snapshot, n1: Snapshot) -> Round4Review:
    """What the data determines; a human checks it line by line before anything is pasted."""
    post = {mid: _family(r4, mid, ROUND4_FIRST_DAY) for mid in ROUND4_IDS}
    found = sorted({str(r["as_of"]) for rows in post.values() for r in rows})
    last = found[-1] if found else ROUND4_FIRST_DAY
    return Round4Review(
        last_capture_day=last,
        missing_days=frozenset(set(_days(ROUND4_FIRST_DAY, last)) - set(found)),
        runs={mid: compress_runs(rows) for mid, rows in post.items()},
        alias_quirks={ALIAS_ID: tuple((day, alias) for day, alias, _ in _alias_differences(r4))},
        acknowledged_month_end_days=frozenset(day for day in found if _month_end(day)),
        keep_period_rows={
            mid: tuple((str(r["as_of"]), r["value"]) for r in _family(
                r4, mid, WINDOW_START, WINDOW_END))
            for mid in ROUND4_IDS
        },
        recapture_sha256=r4.table_sha256,
        night1_sha256=n1.table_sha256,
    )


def _lit(value: object) -> str:
    return json.dumps(value) if isinstance(value, str) else repr(value)


def _days_literal(days: frozenset[str]) -> str:
    return "frozenset({" + ", ".join(_lit(d) for d in sorted(days)) + "})" if days else "frozenset()"


def _table_literal(name: str, table: dict[str, tuple[tuple, ...]]) -> list[str]:
    lines = [f"    {name}={{"]
    for mid, entries in table.items():
        lines.append(f"        {_lit(mid)}: (")
        lines += [f"            ({', '.join(_lit(v) for v in entry)})," for entry in entries]
        lines.append("        ),")
    return lines + ["    },"]


def format_literal(review: Round4Review) -> list[str]:
    """The Round4Review as pasteable Python (C/round4/derive.jq re-derives it byte for byte)."""
    return [
        "REVIEWED = Round4Review(",
        f"    last_capture_day={_lit(review.last_capture_day)},",
        f"    missing_days={_days_literal(review.missing_days)},",
        *_table_literal("runs", review.runs),
        *_table_literal("alias_quirks", review.alias_quirks),
        f"    acknowledged_month_end_days={_days_literal(review.acknowledged_month_end_days)},",
        *_table_literal("keep_period_rows", review.keep_period_rows),
        f"    recapture_sha256={_lit(review.recapture_sha256)},",
        f"    night1_sha256={_lit(review.night1_sha256)},",
        ")",
    ]


def _input_lines(snapshot: Snapshot) -> list[str]:
    manifest = snapshot.manifest
    return [
        f"  {snapshot.label}: {snapshot.directory}",
        f"    manifest.json sha256={snapshot.manifest_sha256}",
        f"    metric_history.json sha256={snapshot.table_sha256} rows={len(snapshot.rows)}",
        f"    started_at={_bdt(manifest['started_at'])} target_project="
        f"{manifest['target_project']} key_role={manifest.get('key_role')}",
    ]


def _zone_lines(r4: Snapshot, n1: Snapshot, mid: str) -> list[str]:
    pre = [r for r in _family(r4, mid) if str(r["as_of"]) < WINDOW_START]
    n1_pre = [r for r in _family(n1, mid) if str(r["as_of"]) < WINDOW_START]
    differ = sorted({str(r["as_of"]) for r in pre if not same(n1.index.get((mid, str(r["as_of"]))), r)}
                    | {str(r["as_of"]) for r in n1_pre} - {str(r["as_of"]) for r in pre})
    span = f"{pre[0]['as_of']}..{pre[-1]['as_of']}" if pre else "-"
    lines = [f"  {mid}", f"    pre-window: {len(pre)} rows {span}; identical to N1: "
             + ("yes" if not differ else f"NO ({', '.join(differ)})")]
    lines.append("    R1 window (each row: as_of value source provenance ingested_at):")
    for row in _family(r4, mid, WINDOW_START, WINDOW_END):
        day = str(row["as_of"])
        flags = [flag for flag, bad in (
            ("NOT-MONTH-END", not _month_end(day)),
            ("NOT-ECONDELTA", row.get("source") != "EconDelta"),
            ("NOT-IN-NIGHT1-AS-NEW", same(n1.index.get((mid, day)), row)),
        ) if bad]
        lines.append(f"      {day} {_lit(row['value'])} {row.get('source')} {row.get('provenance')}"
                     f" {_bdt(row.get('ingested_at'))} {' '.join(flags)}".rstrip())
    post = _family(r4, mid, ROUND4_FIRST_DAY)
    lines.append("    post-window runs (value first..last n_days ingested_at min..max):")
    for value, first, last in compress_runs(post):
        rows = [r for r in post if first <= str(r["as_of"]) <= last]
        stamps = sorted(str(r.get("ingested_at")) for r in rows)
        lines.append(f"      {_lit(value)} {first}..{last} {len(rows)} days "
                     f"{_bdt(stamps[0])}..{_bdt(stamps[-1])}")
    lines.append(f"    post-window sources={sorted({str(r.get('source')) for r in post})} "
                 f"provenance={sorted({str(r.get('provenance')) for r in post})}")
    return lines


def _check(ok: bool, good: str, bad: str) -> str:
    return f"  OK {good}" if ok else f"  CHECK: {bad}"


def _cross_check_lines(r4: Snapshot, n1: Snapshot, proposal: Round4Review) -> list[str]:
    try:
        check_night1_provenance(n1)
        provenance = None
    except RepairConflict as exc:
        provenance = str(exc)
    post = {mid: _family(r4, mid, ROUND4_FIRST_DAY) for mid in ROUND4_IDS}
    date_sets = {mid: [str(r["as_of"]) for r in rows] for mid, rows in post.items()}
    factor_bad = [
        str(row["as_of"]) for row in post[NBR_RESTAMP_ID]
        if (parent := r4.index.get((PARENT_ID, str(row["as_of"])))) is None
        or not same(row["value"], round(float(parent["value"]) * CHILD_FACTOR, 2))
    ]
    alias_bad = [f"{day} (alias {_lit(a)}, parent {_lit(p)})" for day, a, p in _alias_differences(r4)]
    not_in_n1 = [f"{mid} {r['as_of']}" for mid, rows in post.items() for r in rows
                 if not same(n1.index.get((mid, str(r["as_of"]))), r)]
    lacking = [f"{mid} {day}" for mid in ROUND4_IDS for day in
               (str(r["as_of"]) for r in _family(n1, mid, ROUND4_FIRST_DAY))
               if (mid, day) not in r4.index]
    after = [f"{mid} {r['as_of']}" for mid in ROUND4_IDS
             for r in _family(n1, mid, proposal.last_capture_day + "~")]
    corroborators = [r for mid in RETIRED_CORROBORATORS for r in _family(r4, mid)]
    lines = [
        _check(provenance is None, "N1 provenance: Night-1 window rows are R1's 420 reviewed "
               "restamps (pre-R1 snapshot)", f"N1 provenance: {provenance}"),
        _check(len({tuple(days) for days in date_sets.values()}) == 1,
               "the three ids have identical post-window date sets",
               f"post-window date sets differ: { {m: len(d) for m, d in date_sets.items()} }"),
        _check(not factor_bad, f"child = round(parent x {CHILD_FACTOR}, 2) on every day",
               f"child != round(parent x {CHILD_FACTOR}, 2) on: {', '.join(factor_bad)}"),
        _check(not alias_bad, "alias = parent on every post-window day",
               f"alias != parent on: {'; '.join(alias_bad)} (alias_quirks only after review)"),
        _check(not not_in_n1, "every post-window row is identical in N1",
               f"post-window rows not identical in N1: {', '.join(not_in_n1)}"),
        _check(not lacking, f"no Night-1 row at or after {ROUND4_FIRST_DAY} is missing from R4",
               f"Night-1 rows at or after {ROUND4_FIRST_DAY} missing from the recapture: "
               f"{', '.join(lacking)}"),
        _check(not after, "no Night-1 row after the last capture day",
               f"Night-1 rows after the last capture day: {', '.join(after)}"),
    ]
    lines += [
        f"  CHECK: month-end capture day {day} inside the post-window range: confirm the Night-1 "
        f"and Day-2 latest.json source period for {PARENT_ID} is not this day"
        for day in sorted(proposal.acknowledged_month_end_days)
    ]
    ids = sorted({str(r["metric_id"]) for r in corroborators})
    lines.append(f"  OK corroborators untouched, listed for information: {len(corroborators)} "
                 f"rows {ids}")
    return lines


def draft_lines(r4: Snapshot, n1: Snapshot) -> list[str]:
    """The --draft report: banner first, literal last. It reads data only, never REVIEWED."""
    proposal = propose_review(r4, n1)
    lines = [BANNER, "", "1. Inputs", *_input_lines(r4), *_input_lines(n1), "",
             "2. Per id, zone summaries"]
    for mid in ROUND4_IDS:
        lines += _zone_lines(r4, n1, mid)
    missing = ", ".join(sorted(proposal.missing_days)) or "none"
    lines += [f"  missing capture days: {missing}", "", "3. Cross-checks",
              *_cross_check_lines(r4, n1, proposal), "", "4. Literal", LITERAL_HEADER,
              *format_literal(proposal)]
    return lines


PLACEHOLDER_MESSAGE = (
    "round-4 review is a placeholder; transcribe the reviewed Day-2 recapture first"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--draft", action="store_true", help="proposal only; writes nothing")
    mode.add_argument("--build", action="store_true", help="write the reviewed candidate")
    mode.add_argument("--verify-unchanged", action="store_true", help="read-only drift receipt")
    parser.add_argument("--recapture-dir", type=Path, required=True)
    parser.add_argument("--night1-backup-dir", type=Path)
    parser.add_argument("--fresh-dir", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--expect-applied", type=Path, metavar="MANIFEST")
    parser.add_argument("--target")
    parser.add_argument("--econdelta-commit")
    parser.add_argument("--brief-commit")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--preview", action="store_true", help="--build without writing a file")
    parser.add_argument("--owner-ruled-unmerged", metavar="RULING")
    return parser


def _run_draft(args: argparse.Namespace) -> int:
    if args.night1_backup_dir is None:
        raise RepairConflict("--draft needs --night1-backup-dir")
    r4 = load_snapshot(args.recapture_dir, "recapture R4")
    n1 = load_snapshot(args.night1_backup_dir, "Night-1 N1")
    print("\n".join(draft_lines(r4, n1)))
    return 0


def _run_build(args: argparse.Namespace) -> int:
    review = REVIEWED
    if review is None:
        raise RepairConflict(PLACEHOLDER_MESSAGE)
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    """Draft, build or verify; never touches a database or the network."""
    args = _parser().parse_args(argv)
    try:
        if args.draft:
            return _run_draft(args)
        return _run_build(args)
    except (RepairConflict, OSError, ValueError, KeyError) as exc:
        print(f"Round 4 stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
