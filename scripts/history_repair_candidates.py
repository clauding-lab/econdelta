"""Build the bounded 25 September source-evidence proposal, never apply it.

Facts below are transcribed from the preserved primary documents and reviewed
physical pages. They are NOT derived from equal stored values or capture dates.
Unverified originals remain in the hashed twelve-table export. Every exclusion
is a separate exact-key operation requiring review of the generated manifest.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, TypedDict

from scripts.repair_observation_history import (
    KEYS,
    Backup,
    CodeCommits,
    Evidence,
    Manifest,
    Operation,
    RepairConflict,
    Row,
    RowKey,
    file_hash,
    write_json,
)
from utils.observations import BRIEF_ALIASES, BRIEF_CONVERSIONS

SOURCES = {
    "june": (
        "tests/_pdfs/bb_mei_2026_june.pdf",
        "30f593863230aaa744d61652f8c8a11f198a06541bfcbf5b4fb7a81a82354b8f",
    ),
    "july": (
        "tests/_pdfs/bb_mei_2026_july.pdf",
        "40d510b44117fc597675a58c142d56c71fdbe9f11fec2f693aecc225dd6f117e",
    ),
    "wsei": (
        "tests/_pdfs/bb_wsei_2026_09_20.pdf",
        "c6972f35b5a63668b4a76770b32fa582938e9ceef04ae76147b08a4274cd5516",
    ),
    "auction": (
        "tests/fixtures/bb_treasury_auctions.html",
        "d9cc004c34629f628368c21c76683d6564691472781daab7bc1bdae056e3efc0",
    ),
}

# metric_history.provenance is an extraction-method enum in production (CHECK
# metric_history_provenance_check: 'deterministic', 'llm', 'hybrid', 'manual' or NULL).
REPAIRED_ROW_PROVENANCE = "manual"


# Owner decision (d), 26 Sep 2026 BDT. EconDelta re-stamped the standing fiscal-year
# cumulative NBR collection (BRIEF_CONVERSIONS child of tax_revenue, x0.00001) with each
# capture date (E landmine 47), so these as_of dates are capture days, not periods.
# Exactly the reviewed backup keys, each with its exact reviewed value, are excluded; any
# other shape (e.g. a later recapture with more restamps, or a value that differs on any
# reviewed day) is refused and needs a new owner decision. Owner decision (l) extends this
# to the parent tax_revenue and its alias (NBR_PARENT_RESTAMP_RUNS below).
NBR_RESTAMP_ID = "fiscal_nbr_collected_trn"
NBR_RESTAMP_WINDOW = (date(2026, 5, 2), date(2026, 9, 24))
NBR_RESTAMP_MISSING_DAYS = frozenset(
    {"2026-06-14", "2026-06-15", "2026-08-30", "2026-08-31", "2026-09-01", "2026-09-09"}
)
# (reviewed value, first as_of, last as_of), inclusive, exactly as in the hashed 25 Sep
# backup export (controller ruling, R2 fix H1: bind every reviewed day to its value).
NBR_RESTAMP_RUNS: tuple[tuple[float, str, str], ...] = (
    (1.19, "2026-05-02", "2026-05-02"),
    (2.88, "2026-05-03", "2026-06-01"),
    (3.27, "2026-06-02", "2026-06-28"),
    (3.61, "2026-06-29", "2026-09-05"),
    (4.15, "2026-09-06", "2026-09-24"),
)
NBR_RESTAMP_REASON = (
    "Owner decision (d): archive/exclude a daily restamp of the fiscal-year cumulative NBR "
    "collection (tax_revenue x0.00001); as_of is the capture date, not the collection "
    "period. Original complete row remains in hashed backup. Parent tax_revenue and alias "
    "nbr_fytd_collected_cr unchanged pending a separate owner decision."
)
# Owner decision (l), 28 Sep 2026 BDT, extends (d): the parent tax_revenue (BDT crore) and its
# plain alias nbr_fytd_collected_cr carry the same restamp on the same 140 reviewed dates.
# Each id is bound to its own reviewed (value, first as_of, last as_of) runs, inclusive,
# exactly as in the hashed 25 Sep backup export (the alias's first run differs from the
# parent's). The retired news corroborators nbr_fytd_collected_dailystar/_tbs (deprecated,
# alias_of tax_revenue) are in neither decision and are never touched here.
NBR_PARENT_RESTAMP_RUNS: dict[str, tuple[tuple[float, str, str], ...]] = {
    "tax_revenue": (
        (119478.0, "2026-05-02", "2026-05-02"),
        (287862.59, "2026-05-03", "2026-06-01"),
        (326928.16, "2026-06-02", "2026-06-28"),
        (360642.0, "2026-06-29", "2026-09-05"),
        (415473.0, "2026-09-06", "2026-09-24"),
    ),
    "nbr_fytd_collected_cr": (
        (287431.0, "2026-05-02", "2026-05-24"),
        (287862.59, "2026-05-25", "2026-06-01"),
        (326928.16, "2026-06-02", "2026-06-28"),
        (360642.0, "2026-06-29", "2026-09-05"),
        (415473.0, "2026-09-06", "2026-09-24"),
    ),
}
NBR_PARENT_RESTAMP_REASON = (
    "Owner decision (l), extending (d): archive/exclude a daily restamp of the fiscal-year "
    "cumulative NBR collection in BDT crore (tax_revenue or its alias nbr_fytd_collected_cr); "
    "as_of is the capture date, not the collection period. Original complete row remains in "
    "hashed backup. Retired corroborators nbr_fytd_collected_dailystar/_tbs unchanged."
)


def reviewed_nbr_restamp_dates() -> list[str]:
    """The 140 as_of dates the owner reviewed under decisions (d) and (l)."""
    first, last = NBR_RESTAMP_WINDOW
    days = (first + timedelta(days=n) for n in range((last - first).days + 1))
    return [d.isoformat() for d in days if d.isoformat() not in NBR_RESTAMP_MISSING_DAYS]


def reviewed_nbr_restamp_values(
    runs: tuple[tuple[float, str, str], ...] = NBR_RESTAMP_RUNS,
) -> list[float]:
    """The reviewed value of each reviewed_nbr_restamp_dates() day, in the same order."""
    return [_reviewed_nbr_restamp_value(day, runs) for day in reviewed_nbr_restamp_dates()]


def _reviewed_nbr_restamp_value(as_of: str, runs: tuple[tuple[float, str, str], ...]) -> float:
    (value,) = (v for v, first, last in runs if first <= as_of <= last)
    return value


def nbr_restamp_rows(
    history: list[Row],
    metric_id: str = NBR_RESTAMP_ID,
    runs: tuple[tuple[float, str, str], ...] = NBR_RESTAMP_RUNS,
    decision: str = "(d)",
) -> list[Row]:
    """Backup rows for decision (d) or (l); refuse unless they are exactly the reviewed restamps."""
    family = sorted(
        (r for r in history if r["metric_id"] == metric_id), key=lambda r: str(r["as_of"])
    )
    if (
        [r["as_of"] for r in family] != reviewed_nbr_restamp_dates()
        or [r["value"] for r in family] != reviewed_nbr_restamp_values(runs)
        or any(r["source"] != "EconDelta" or r["provenance"] is not None for r in family)
    ):
        raise RepairConflict(
            f"{metric_id} backup differs from the 140 reviewed restamp keys and values of "
            f"owner decision {decision}; a recaptured snapshot needs a new owner decision"
        )
    return family


class SourceFact(TypedDict):
    value: int | float
    unit: str
    release_status: Literal["unknown", "provisional", "revised"]
    evidence: list[Evidence]
    parents: list[str]


def evidence(root: Path, source: str, locator: str) -> Evidence:
    """Bind each source fact to a preserved, hash-pinned primary document."""
    relative, digest = SOURCES[source]
    path = root / relative
    if not path.exists() or file_hash(path) != digest:
        raise RepairConflict(f"missing/changed primary source {source}")
    return {"path": str(path.resolve()), "sha256": digest, "locator": locator}


def source_observations(root: Path) -> dict[str, dict[str, SourceFact]]:
    """Explicit period/value facts and aliases rebuilt only from aligned parents."""
    facts: dict[str, dict[str, SourceFact]] = {}

    def add(
        mid: str,
        period: str,
        value: int | float,
        source: str,
        locator: str,
        unit: str = "BDT crore",
        status: Literal["unknown", "provisional", "revised"] = "provisional",
    ) -> None:
        facts.setdefault(mid, {})[period] = {
            "value": value,
            "unit": unit,
            "release_status": status,
            "evidence": [evidence(root, source, locator)],
            "parents": [],
        }

    money = {
        "deposits_of_the_system": (2041692.7, 2079911.1, "B. Deposits of the banking system", 6, 5),
        "currency_outside_bank": (349374, 336375.2, "A. Currency outside banks", 6, 5),
        "broad_money": (2391066.7, 2416286.3, "C. Broad Money (M2)", 6, 5),
        "reserve_money": (485542.3, 476368.6, "Reserve money", 7, 6),
        "deposits_held_with_bb_crr": (
            115326.7,
            106100.1,
            "Deposits held with BB (including NBFCs)",
            7,
            6,
        ),
        "money_multiplier": (4.92, 5.07, "Money multiplier", 7, 6),
    }
    for mid, (may, june, row, page_may, page_june) in money.items():
        unit = "ratio" if mid == "money_multiplier" else "BDT crore"
        add(
            mid,
            "2026-05-31",
            may,
            "june",
            f"physical page {page_may}; {row}; May 2026 P column",
            unit,
        )
        add(
            mid,
            "2026-06-30",
            june,
            "july",
            f"physical page {page_june}; {row}; June 2026 P column",
            unit,
        )
    for mid, may, june, label in (
        ("bank_borrowing_for_deficit_financing", 94158.9, 165538.20, "Bank"),
        ("non_bank_borrowing_for_deficit_financing", -567.67, -847.17, "Non-bank"),
        ("domestic_borrowing_for_budget_deficit", 93591.23, 164691.03, "Domestic financing"),
        ("foreign_borrowing_for_budget_deficit", 21944.28, 57743.60, "Foreign financing"),
    ):
        add(
            mid,
            "2026-05-31",
            may,
            "june",
            f"physical page 16; Government deficit financing; July-May FY26; {label}",
            status="unknown",
        )
        add(
            mid,
            "2026-06-30",
            june,
            "july",
            f"physical page 15; Government deficit financing; FY26 full year; {label}",
            status="unknown",
        )
    for mid, prior_july, may, june, july, label in (
        ("monthly_import_lc_opening", 6067.72, 6212.75, 6198.59, 6901.7, "Opening"),
        ("monthly_import_lc_settlement", 6104.03, 5837.67, 7116.88, 6279.3, "Settlement"),
    ):
        add(
            mid,
            "2025-07-31",
            prior_july,
            "july",
            f"physical page 24; Imports; FY26 July=July 2025; LC {label}",
            "USD million",
        )
        add(
            mid,
            "2026-05-31",
            may,
            "june",
            f"physical page 25; Imports; FY26 May; LC {label}; retain accepted vintage pending revision review",
            "USD million",
        )
        add(
            mid,
            "2026-06-30",
            june,
            "july",
            f"physical page 24; Imports; FY26 June; LC {label}",
            "USD million",
        )
        add(
            mid,
            "2026-07-31",
            july,
            "wsei",
            f"physical page 2; LC {label}; July FY27; USD billion multiplied by 1000",
            "USD million",
            status="unknown",
        )
    add(
        "broad_money",
        "2026-07-31",
        2422923.9,
        "wsei",
        "physical page 1; Broad Money (M2); July 2026",
        status="unknown",
    )
    add(
        "reserve_money",
        "2026-07-31",
        463461.4,
        "wsei",
        "physical page 1; Reserve Money; July 2026",
        status="unknown",
    )
    add(
        "gdp_growth_fy_pct",
        "2025-06-30",
        3.49,
        "wsei",
        "physical page 2; GDP Growth Rate Base 2015-16; FY25 R",
        "percent",
        "revised",
    )
    add(
        "gdp_growth_fy_pct",
        "2026-06-30",
        4.14,
        "wsei",
        "physical page 2; GDP Growth Rate Base 2015-16; FY26 P",
        "percent",
    )
    for child, parent in BRIEF_ALIASES.items():
        if parent in facts:
            facts[child] = {
                period: fact | {"parents": [parent]} for period, fact in facts[parent].items()
            }
    for child, (parent, factor) in BRIEF_CONVERSIONS.items():
        if parent in facts:
            facts[child] = {
                period: fact
                | {"value": fact["value"] * factor, "unit": "BDT trillion", "parents": [parent]}
                for period, fact in facts[parent].items()
            }
    for period, numerator in facts["deposits_held_with_bb_crr"].items():
        denominator = facts["deposits_of_the_system"][period]
        facts.setdefault("crr_utilisation_pct", {})[period] = numerator | {
            "value": round(numerator["value"] / denominator["value"] * 100, 4),
            "unit": "percent",
            "evidence": numerator["evidence"] + denominator["evidence"],
            "parents": ["deposits_held_with_bb_crr", "deposits_of_the_system"],
        }
    return facts


def build_candidate(
    backup_dir: Path, root: Path, *, target: str, commits: CodeCommits, generated_at: str
) -> Manifest:
    """Generate exact images from the verified export, with unsupported dispositions."""
    backup_manifest = json.loads((backup_dir / "manifest.json").read_text())
    if (
        backup_manifest["target_project"] != "ssbliukchgibjcjohibi"
        or len(backup_manifest["tables"]) != 12
    ):
        raise RepairConflict("expected verified twelve-table project backup")
    tables: dict[str, list[Row]] = {}
    backups: list[Backup] = []
    for table, ref in backup_manifest["tables"].items():
        path = backup_dir / f"{table}.json"
        if file_hash(path) != ref["sha256"]:
            raise RepairConflict(f"backup hash mismatch: {table}")
        rows = json.loads(path.read_text())
        if len(rows) != ref["rows"] or len({tuple(r[k] for k in ref["key"]) for r in rows}) != len(
            rows
        ):
            raise RepairConflict(f"backup count/key mismatch: {table}")
        tables[table] = rows
        backups.append(
            {
                "table": table,
                "path": str(path.resolve()),
                "sha256": ref["sha256"],
                "rows": len(rows),
            }
        )
    operations: list[Operation] = []
    indexes = {
        table: {tuple(r[k] for k in KEYS[table]): r for r in rows}
        for table, rows in tables.items()
        if table in KEYS
    }

    def operation(
        table: str,
        key: RowKey,
        after: Row | None,
        reason: str,
        refs: list[Evidence],
        requires: list[str] | tuple[str, ...] = (),
    ) -> str:
        before = indexes[table].get(tuple(key[k] for k in KEYS[table]))
        op_id = f"{table}:" + ":".join(str(key[k]) for k in KEYS[table])
        operations.append(
            {
                "operation_id": op_id,
                "table": table,
                "key": key,
                "before": before,
                "after": after,
                "reason": reason,
                "evidence": refs,
                "requires": list(requires),
            }
        )
        return op_id

    facts = source_observations(root)
    # Definition creation precedes the new GDP ID; all columns remain explicit.
    old_gdp = indexes["metric_definitions"][("gdp",)]
    gdp_ref = evidence(
        root,
        "wsei",
        "physical page 2; GDP Growth Rate (%) FY25 R/FY26 P; percent, not monetary GDP",
    )
    new_gdp = old_gdp | {
        "metric_id": "gdp_growth_fy_pct",
        "label": "GDP growth rate",
        "unit": "percent",
        "cadence": "fiscal_year",
        "grace_days": 400,
        "description": "Bangladesh fiscal-year GDP growth. FY25 revised; FY26 provisional. Fiscal years end 30 June.",
        "source": "Bangladesh Bank WSEI",
        "deprecated": False,
        "alias_of": None,
    }
    gdp_op = operation(
        "metric_definitions",
        {"metric_id": "gdp_growth_fy_pct"},
        new_gdp,
        "Create correctly typed GDP growth ID before history",
        [gdp_ref],
    )
    operation(
        "metric_definitions",
        {"metric_id": "gdp"},
        old_gdp
        | {
            "deprecated": True,
            "description": "Deprecated ambiguous GDP ID; verified growth observations use gdp_growth_fy_pct. Original rows retained in reviewed repair backup.",
        },
        "Retire ambiguous legacy GDP definition",
        [gdp_ref],
        [gdp_op],
    )
    verified_ids = {}
    daily_template = tables["metric_history"][0]
    for mid, periods in facts.items():
        for period, fact in sorted(periods.items()):
            key = {"metric_id": mid, "as_of": period}
            original = indexes["metric_history"].get((mid, period), daily_template)
            after = (
                original
                | key
                | {
                    "value": fact["value"],
                    "source": "Bangladesh Bank (reviewed source repair)",
                    "ingested_at": generated_at,
                    # Owner decision, 2 Oct 2026 ~20:58 BDT: production CHECK
                    # metric_history_provenance_check allows only deterministic/llm/
                    # hybrid/manual (or NULL). A reviewed hand transcription is 'manual';
                    # the source detail stays in this manifest's evidence/receipts.
                    "provenance": REPAIRED_ROW_PROVENANCE,
                }
            )
            deps = [verified_ids[(parent, period)] for parent in fact["parents"]]
            # Preserve accepted May before replacing an occupied June destination.
            if period == "2026-06-30" and (mid, "2026-05-31") in verified_ids:
                deps.append(verified_ids[(mid, "2026-05-31")])
            if mid == "gdp_growth_fy_pct":
                deps.append(gdp_op)
            verified_ids[(mid, period)] = operation(
                "metric_history",
                key,
                after,
                "Explicit source period/value; rebuild aliases/conversions from aligned parents; occupied destination retained in backup",
                fact["evidence"],
                deps,
            )
    unsupported = {
        "gdp",
        "slr_utilisation_pct",
        "excess_liquid_asset_total_minimum",
        "banking_excess_liquid",
    }
    backup_ref = {
        "path": str((backup_dir / "metric_history.json").resolve()),
        "sha256": file_hash(backup_dir / "metric_history.json"),
        "locator": "exact key and source label in complete export; archive/exclusion ONLY, no numerical or period corroboration",
    }
    for row in tables["metric_history"]:
        mid, period = row["metric_id"], row["as_of"]
        mixed_imf = mid == "debt_gdp_ratio" and row["source"] == "IMF DataMapper"
        forecasts = mid == "debt_gdp_ratio_proj"
        if (
            (mid in facts and (mid, period) not in verified_ids)
            or mid in unsupported
            or mixed_imf
            or forecasts
        ):
            refs = [backup_ref]
            reason = "Archive/exclude unsupported legacy date or derived row; do not infer its true period/value. Original complete row remains in hashed backup."
            if mixed_imf or forecasts:
                reason = "Archive/exclude explicitly identified mixed IMF estimate/forecast namespace; retain original source/year/value in backup. No raw IMF numerical payload recovered; no re-dating or new-ID numerical migration."
            deps = [
                op
                for (name, _), op in verified_ids.items()
                if name == mid or (mid == "gdp" and name == "gdp_growth_fy_pct")
            ]
            operation(
                "metric_history", {"metric_id": mid, "as_of": period}, None, reason, refs, deps
            )
    # No prerequisite: the parent tax_revenue has no verified fact here, so no conversion
    # child is rebuilt and nothing in this manifest depends on these rows.
    for row in nbr_restamp_rows(tables["metric_history"]):
        key = {"metric_id": NBR_RESTAMP_ID, "as_of": str(row["as_of"])}
        operation("metric_history", key, None, NBR_RESTAMP_REASON, [backup_ref])
    # Decision (l): same rule for the parent and its alias. Neither has a verified fact here
    # (nor rebuilds one as alias/conversion), so these exclusions have no prerequisite either.
    for metric_id, runs in NBR_PARENT_RESTAMP_RUNS.items():
        for row in nbr_restamp_rows(tables["metric_history"], metric_id, runs, "(l)"):
            key = {"metric_id": metric_id, "as_of": str(row["as_of"])}
            operation("metric_history", key, None, NBR_PARENT_RESTAMP_REASON, [backup_ref])
    dates = {
        "tbill_91d_yield_monthly": ("2026-05-24", 10.15),
        "tbill_182d_yield_monthly": ("2026-05-24", 10.4085),
        "tbill_364d_yield_monthly": ("2026-05-24", 10.5),
        "yield_2y_monthly": ("2026-05-06", 10.728),
        "yield_5y_monthly": ("2026-05-13", 10.78),
        "yield_10y_monthly": ("2026-05-17", 10.9099),
        "yield_15y_monthly": ("2026-05-20", 11.0198),
        "yield_20y_monthly": ("2026-05-20", 11.0875),
    }
    for mid, (source_date, value) in dates.items():
        before = indexes["metric_history_monthly"][(mid, "2026-05-01")]
        if before["value"] != value:
            raise RepairConflict(f"May auction source value mismatch: {mid}")
        after = before | {
            "source_as_of": source_date,
            "notes": "Source date is BB issue/settlement date, not independently verified auction-held date. Preserved official May 2026 table.",
        }
        operation(
            "metric_history_monthly",
            {"metric_id": mid, "as_of": "2026-05-01"},
            after,
            "Correct false month-first evidence date to verified ISSUE/SETTLEMENT date; value and grouping bucket unchanged",
            [
                evidence(
                    root,
                    "auction",
                    f"HTML lines 740-768; {mid}; Issue date {source_date}; Cut off yield {value}%",
                )
            ],
        )
    reserve_path = root / "tests/fixtures/bb_forex_reserves.html"
    bpm6_ref = {
        "path": str(reserve_path.resolve()),
        "sha256": file_hash(reserve_path),
        "locator": "Foreign Exchange Reserve table; Gross and BPM6 methodology columns; E4 approved gross-label contract",
    }
    mid = "net_reserves_bpm6_usd_bn_monthly"
    before = indexes["metric_definitions_monthly"][(mid,)]
    operation(
        "metric_definitions_monthly",
        {"metric_id": mid},
        before
        | {
            "display_name": "FX reserves (BPM6 gross)",
            "description": "Gross foreign exchange reserves reported by Bangladesh Bank under the IMF BPM6 methodology.",
        },
        "Correct gross/net description, preserving stable ID and every historical value",
        [bpm6_ref],
    )
    return {
        "version": 1,
        "target_project": backup_manifest["target_project"],
        "target": target,
        "code_commits": commits,
        "generated_at": generated_at,
        "backup_manifest_sha256": file_hash(backup_dir / "manifest.json"),
        "backups": backups,
        "operations": operations,
        "unresolved": [
            "May LC revision to 6422.17/5864.42 requires separate reviewed revision proposal; accepted June-MEI vintage preserved",
            "June/July auction dates have backup corroboration only; 16 rows unchanged",
            "CPI provenance/value disputes unchanged; no retrospective trusted-source relabel",
            "SLR/excess-liquidity numerator period unresolved; excluded active rows remain in backup",
            "Historical IMF raw numerical source missing; 29 mixed and 6 projection rows archived/excluded, no inferred migration; 5 MoF/EconDelta rows retained unresolved",
            "All other slow-series historical families remain outside this bounded manifest pending independent evidence",
            "EPB accepted-via-BSS history and frozen NBR monthly archive preserved; no speculative splice",
            "Previous published editions/children unchanged; R3 owns durable identity/visibility blocker",
        ],
    }


def main() -> None:
    """Emit a review candidate only; cannot access a database."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--econdelta-commit", required=True)
    parser.add_argument("--brief-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise RepairConflict("candidate already exists; preserve reviewed bytes")
    candidate = build_candidate(
        args.backup_dir,
        args.source_root,
        target=args.target,
        commits={"econdelta": args.econdelta_commit, "brief": args.brief_commit},
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    write_json(args.output, candidate)
    print(
        f"{len(candidate['operations'])} exact operations; candidate sha256={file_hash(args.output)}"
    )


if __name__ == "__main__":
    main()
