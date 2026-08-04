"""scripts/seed_npl_structure.py

One-shot static seed of the Mar-2026 band-wise + CMSME NPL figures.

Source: Bangladesh Bank data as reported by Prothom Alo, 1 August 2026
(position end-March 2026), hand-transcribed from the owner's deck "Small
Loans Big Numbers" (slides 5, 6, 9) and cross-checked against the deck's
reference table. Provenance "bb_via_press_static" (precedent:
mof_mfr_static in scripts/backfill_fiscal.py). These series have NO
scheduled BB source (verified 2026-08-03: absent from both QFSAR and FSR)
— they update only if a future press-capture decision lands.

Excluded by design: derived figures (implied stocks/averages), the vague
"just over 4%" agriculture share, press-taxonomy sector values (the
ongoing sector family uses the FSR taxonomy — see spec amendment), and
defaulter counts (out of scope).

total_bank_advances (Tk 17.84 lakh crore = 1,784,000 crore) is shared
with the FSR-written series: this seed writes its Mar-2026 press value;
the scraper writes FSR vintages at other as_of dates. Merge-upsert on
(metric_id, as_of) keeps both.

DRY-RUN BY DEFAULT — writes require --execute plus live creds, owner
sign-off, and before/after SELECT proofs (house DB rules).

Usage:
    .venv/bin/python -m scripts.seed_npl_structure            # dry run
    .venv/bin/python -m scripts.seed_npl_structure --execute  # writes
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timezone

from scrapers.bb_npl_structure import METRIC_SPECS, build_definitions_rows
from utils.supabase_writer import upsert_metric_definitions_seed, upsert_metric_history

logger = logging.getLogger("seed_npl_structure")

SEED_AS_OF = date(2026, 3, 31)
SEED_SOURCE = "bb_via_press_static"

# Percents as printed; amounts converted lakh crore -> crore (x100,000).
SEED_VALUES: dict[str, float] = {
    "npl_rate_band_lt1cr": 15.0,        # deck slide 6: Under Tk 1 crore, 15.0%
    "npl_rate_band_1_10cr": 26.5,
    "npl_rate_band_10_20cr": 45.0,
    "npl_rate_band_20_30cr": 36.0,
    "npl_rate_band_30_40cr": 39.0,
    "npl_rate_band_40_50cr": 45.0,
    "npl_rate_band_gt50cr": 42.5,
    "loans_outstanding_band_lt1cr": 410_000,   # Tk 4.10 lakh crore
    "loans_outstanding_band_1_10cr": 361_000,  # Tk 3.61 lakh crore
    "loans_outstanding_band_gt50cr": 576_000,  # Tk 5.76 lakh crore
    "npl_rate_cmsme_overall": 34.0,     # deck slide 9
    "npl_rate_cmsme_cottage": 53.0,
    "npl_rate_cmsme_medium": 38.0,
    "total_bank_advances": 1_784_000,   # Tk 17.84 lakh crore
}


def run(*, execute: bool) -> int:
    unknown = set(SEED_VALUES) - set(METRIC_SPECS)
    if unknown:
        raise ValueError(f"seed ids not in METRIC_SPECS: {sorted(unknown)}")
    if not execute:
        for mid, value in sorted(SEED_VALUES.items()):
            logger.info("DRY RUN  %-32s %12s  as_of=%s source=%s",
                        mid, value, SEED_AS_OF, SEED_SOURCE)
        logger.info("DRY RUN — %d rows, nothing written. Re-run with --execute.",
                    len(SEED_VALUES))
        return 0
    new_defs = upsert_metric_definitions_seed(build_definitions_rows())
    count = upsert_metric_history(
        data=dict(SEED_VALUES),
        as_of=SEED_AS_OF,
        source=SEED_SOURCE,
        ingested_at=datetime.now(timezone.utc),
    )
    logger.info("seeded %d history rows (+%d new definitions)", count, new_defs)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed Mar-2026 NPL structure primitives")
    parser.add_argument("--execute", action="store_true", help="actually write (default: dry run)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    return run(execute=args.execute)


if __name__ == "__main__":
    sys.exit(main())
