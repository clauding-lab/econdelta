"""scrapers/bb_npl_structure.py

Banking-structure NPL metrics.

FSR-written family (annual): sector-wise NPL distribution from BB's
Financial Stability Report Table 2.3 — 8 top-level sectors (rate + share
of lending), 4 sub-sector rates, total advances and gross NPL stock.
One LLM extraction pass over a deterministic slice of the document, hard
arithmetic gate (full reconciliation), all-or-nothing upsert.

Gate coverage is uneven by design, not oversight: the 8 sector rates/shares
and the two totals are reconciliation-checked (share-sum, weighted-average
vs overall, stock/advances ratio) — a wrong-column or unit-slip read moves
those checks by multiple points. nbfi and capital_market carry <0.5% of
lending share each, so a bad read there barely moves the weighted average;
they and the 4 sub-sector rates are range-checked only (RATE_RANGE), never
reconciled against another figure in the document.

Seed-only family (no scheduled source — press/parliament disclosures):
band-wise NPL rates/outstandings and CMSME segment rates, written once by
scripts/seed_npl_structure.py with source "bb_via_press_static".

These ids are deliberately NOT in config/sources-v3.json and must never
join briefing.config.CORE_METRIC_IDS (owner decision: non-gating) nor
leave sentinel ACCEPTED_STALE (structural source lag). See the spec
amendment: docs/superpowers/specs/2026-08-03-bb-npl-structure-design.md.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from claude_max.max_client import MaxCallError, run_max
from fetch_all import _download_index_html
from fetchers.pdf_discovery import discover_latest_pdf
from fetchers.pdf_fetcher import fetch_pdf

SOURCE_LABEL = "BB FSR"
SEED_ONLY_SOURCE_NOTE = "bb_via_press_static"


@dataclass(frozen=True)
class MetricSpec:
    label: str
    unit: str      # "percent" | "amount_bdt_crore"
    family: str    # sector_rate | sector_share | sub_rate | total | band_rate | band_outstanding | cmsme
    fsr: bool      # True = written by this scraper from the FSR; False = seed-only


_SECTORS = {
    "agriculture": "Agriculture",
    "industrial_mfg": "Industrial (Manufacturing)",
    "industrial_services": "Industrial (Services)",
    "consumer_credit": "Consumer Credit",
    "trade_commerce": "Trade and Commerce (Commercial Loans)",
    "nbfi": "Credit to NBFI",
    "capital_market": "Loans to Capital Market",
    "other": "Other Loans",
}

METRIC_SPECS: dict[str, MetricSpec] = {}
for _key, _name in _SECTORS.items():
    METRIC_SPECS[f"npl_rate_sector_{_key}"] = MetricSpec(
        f"NPL rate — {_name}", "percent", "sector_rate", True)
    METRIC_SPECS[f"lending_share_sector_{_key}"] = MetricSpec(
        f"Share of lending — {_name}", "percent", "sector_share", True)
METRIC_SPECS.update({
    "npl_rate_sub_rmg": MetricSpec("NPL rate — RMG", "percent", "sub_rate", True),
    "npl_rate_sub_construction": MetricSpec("NPL rate — construction loans", "percent", "sub_rate", True),
    "npl_rate_sub_housing_finance": MetricSpec("NPL rate — housing finance", "percent", "sub_rate", True),
    "npl_rate_sub_smc_industries": MetricSpec(
        "NPL rate — other industries (small, medium and cottage)", "percent", "sub_rate", True),
    "total_bank_advances": MetricSpec(
        "Total loans and advances of the banking sector", "amount_bdt_crore", "total", True),
    "gross_npl_stock": MetricSpec(
        "Gross non-performing loans of the banking sector", "amount_bdt_crore", "total", True),
    # --- seed-only: band-wise + CMSME (press/parliament disclosures) ---
    "npl_rate_band_lt1cr": MetricSpec("NPL rate — loans under Tk 1 crore", "percent", "band_rate", False),
    "npl_rate_band_1_10cr": MetricSpec("NPL rate — loans Tk 1-10 crore", "percent", "band_rate", False),
    "npl_rate_band_10_20cr": MetricSpec("NPL rate — loans Tk 10-20 crore", "percent", "band_rate", False),
    "npl_rate_band_20_30cr": MetricSpec("NPL rate — loans Tk 20-30 crore", "percent", "band_rate", False),
    "npl_rate_band_30_40cr": MetricSpec("NPL rate — loans Tk 30-40 crore", "percent", "band_rate", False),
    "npl_rate_band_40_50cr": MetricSpec("NPL rate — loans Tk 40-50 crore", "percent", "band_rate", False),
    "npl_rate_band_gt50cr": MetricSpec("NPL rate — loans above Tk 50 crore", "percent", "band_rate", False),
    "loans_outstanding_band_lt1cr": MetricSpec(
        "Outstanding loans — under Tk 1 crore", "amount_bdt_crore", "band_outstanding", False),
    "loans_outstanding_band_1_10cr": MetricSpec(
        "Outstanding loans — Tk 1-10 crore", "amount_bdt_crore", "band_outstanding", False),
    "loans_outstanding_band_gt50cr": MetricSpec(
        "Outstanding loans — above Tk 50 crore", "amount_bdt_crore", "band_outstanding", False),
    "npl_rate_cmsme_overall": MetricSpec("NPL rate — CMSME overall", "percent", "cmsme", False),
    "npl_rate_cmsme_cottage": MetricSpec("NPL rate — cottage industry", "percent", "cmsme", False),
    "npl_rate_cmsme_medium": MetricSpec("NPL rate — medium enterprise", "percent", "cmsme", False),
})

# LLM payload keys: every FSR-written id + the check-only overall ratio.
# overall_npl_ratio_fsr is NEVER stored — gross_npl_ratio (QFSAR-sourced)
# owns the overall series; the FSR figure is a different vintage.
FSR_EXTRACTION_KEYS: tuple[str, ...] = tuple(
    m for m, s in METRIC_SPECS.items() if s.fsr
) + ("overall_npl_ratio_fsr",)

REQUIRED_EXTRACTION_KEYS: frozenset[str] = frozenset(
    m for m, s in METRIC_SPECS.items() if s.fsr and s.family != "sub_rate"
) | {"overall_npl_ratio_fsr"}


def build_definitions_rows() -> list[dict]:
    """metric_definitions seed rows (first-insert-wins — right on day one).

    cadence "fiscal_year" is truth-in-labeling for annual-with-lag series;
    the sentinel additionally carries every id in ACCEPTED_STALE_METRIC_IDS.

    grace_days=400 is included because ``utils.supabase_writer._normalize_definition``
    passes unknown keys through untouched (it merges caller fields over its
    defaults with no allow-list) and the live ``metric_definitions`` table has
    a ``grace_days`` column the writer's own default set doesn't cover. These
    rows are annual-with-lag (FSR is published well over a year in arrears),
    so the freshness grace window must be wide from the first insert.
    """
    return [
        {
            "metric_id": mid,
            "label": spec.label,
            "domain": "money_market",
            "unit": spec.unit,
            "cadence": "fiscal_year",
            "source": SOURCE_LABEL if spec.fsr else SEED_ONLY_SOURCE_NOTE,
            "grace_days": 400,
        }
        for mid, spec in METRIC_SPECS.items()
    ]


REPO_ROOT = Path(__file__).resolve().parents[1]
FSR_LISTING_URL = "https://www.bb.org.bd/en/index.php/publication/publictn/0/37"

_TABLE_MARKER = "SECTOR-WISE NON-PERFORMING LOANS DISTRIBUTION"
_SLICE_BEFORE = 2_000
_SLICE_AFTER = 10_000

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}
_QUARTER_END = {3: 31, 6: 30, 9: 30, 12: 31}
# Matches "end-December 2025", "END-DECEMBER 2025", "end of December, 2025".
_POSITION_RE = re.compile(
    r"end[\s\-]+(?:of[\s\-]+)?(" + "|".join(_MONTHS) + r")[\s,]+(\d{4})",
    re.IGNORECASE,
)


class PositionDateError(ValueError):
    """Document text carries no recognizable quarter-end position date."""


class TableMarkerError(ValueError):
    """FSR text no longer contains the Table 2.3 marker — layout changed."""


def fetch_latest_fsr(data_root: Path):
    """Discover + download the newest FSR from BB's annual listing.

    Runs on the box (BD IP). The helpers raise FetchError on failure;
    main() catches and notifies.
    """
    html = _download_index_html(FSR_LISTING_URL)
    url, period = discover_latest_pdf(html=html, base_url=FSR_LISTING_URL)
    as_of_month = datetime.now(timezone.utc).strftime("%Y-%m")
    return fetch_pdf(
        url=url,
        indicator_id="bb_npl_structure",
        snapshot_dir=data_root,
        as_of_month=as_of_month,
        period=period,
    )


def extract_pdf_text_full(pdf_path: Path) -> str:
    from parsers.hybrid import _extract_pdf_text

    return _extract_pdf_text(pdf_path, page_hint=None, indicator_id="bb_npl_structure")


def derive_position_date(text: str) -> date:
    """Latest quarter-end 'end-<Month> <Year>' date in the document's own text.

    max() beats the stale comparison-period dates gov reports print alongside
    the current one (pdf_table_row landmine).
    """
    candidates = [
        date(int(year), _MONTHS[m.lower()], _QUARTER_END[_MONTHS[m.lower()]])
        for m, year in _POSITION_RE.findall(text)
        if _MONTHS[m.lower()] in _QUARTER_END
    ]
    if not candidates:
        raise PositionDateError("no quarter-end position date found in FSR text")
    return max(candidates)


def slice_table_window(text: str) -> str:
    """The Table 2.3 neighborhood, centered on the LAST marker occurrence
    (the first is the TOC line). Missing marker = loud failure, not a guess."""
    idx = text.rfind(_TABLE_MARKER)
    if idx == -1:
        raise TableMarkerError(f"marker not found: {_TABLE_MARKER!r}")
    return text[max(0, idx - _SLICE_BEFORE): idx + _SLICE_AFTER]


_EXTRACTION_MODEL = "claude-opus-4-8"
_EXTRACTION_EFFORT = "high"
_EXTRACTION_TIMEOUT_S = 900


class ExtractionError(RuntimeError):
    """LLM extraction failed twice (unparseable JSON) or the CLI call errored."""


def build_extraction_prompt(window: str) -> str:
    field_lines = "\n".join(f'  "{k}": <number or null>,' for k in FSR_EXTRACTION_KEYS).rstrip(",")
    sector_lines = "\n".join(
        f'- npl_rate_sector_{k} / lending_share_sector_{k}: row "{name}" —'
        " Gross NPL Ratio column / Share of Loans Extended column"
        for k, name in _SECTORS.items()
    )
    return (
        "Below is the sector-wise non-performing-loans table from Bangladesh"
        " Bank's Financial Stability Report, as raw extracted text.\n"
        "Rules:\n"
        "- Copy numbers VERBATIM. Never derive, convert, sum, or infer.\n"
        "- Percent columns: the printed number (49.88 not 0.4988).\n"
        "- Amount fields: the printed BILLION BDT number exactly as shown"
        " (e.g. 18,204.30 -> 18204.30). Do NOT convert units.\n"
        "- If a row or figure is absent from the text, use null. Do not guess.\n"
        "- Reply with ONLY a JSON object, exactly these keys:\n"
        "{\n" + field_lines + "\n}\n\n"
        "Field meanings (top-level sector rows):\n" + sector_lines + "\n"
        '- npl_rate_sub_rmg: sub-row "RMG" Gross NPL Ratio\n'
        '- npl_rate_sub_construction: sub-row "Construction Loans" Gross NPL Ratio\n'
        '- npl_rate_sub_housing_finance: sub-row "Housing Finance" Gross NPL Ratio\n'
        '- npl_rate_sub_smc_industries: sub-row "Other Industries (Small, Medium and'
        ' Cottage)" Gross NPL Ratio\n'
        "- total_bank_advances: Total row, Total Loans Outstanding column (billion BDT)\n"
        "- gross_npl_stock: Total row, Gross NPL column (billion BDT)\n"
        "- overall_npl_ratio_fsr: Total row, Gross NPL Ratio column (percent)\n\n"
        "TABLE TEXT:\n" + window
    )


def run_extraction(window: str) -> dict:
    prompt = build_extraction_prompt(window)
    last_raw = ""
    for _attempt in (1, 2):
        try:
            result = run_max(
                prompt=prompt,
                model=_EXTRACTION_MODEL,
                effort=_EXTRACTION_EFFORT,
                timeout_s=_EXTRACTION_TIMEOUT_S,
            )
        except MaxCallError as e:
            raise ExtractionError(f"max CLI call failed: {e}") from e
        if isinstance(result.parsed, dict):
            return result.parsed
        last_raw = (result.raw_text or "")[:200]
    raise ExtractionError(f"unparseable extraction after 2 attempts: {last_raw}")


# Full reconciliation is possible here (unlike the abandoned QFSAR band
# design): the FSR prints EVERY sector's share and rate plus the totals.
# Real 2025 figures: weighted 30.61 vs printed 30.60; shares sum 100.01;
# 5570.32/18204.30 = 30.60% — all three checks pass with tight tolerances,
# while a single wrong-column read (e.g. mfg 48.51 for 28.91) moves the
# weighted average ~10pp and rejects.
SHARE_SUM_TOLERANCE = 0.5
WEIGHTED_TOLERANCE_PP = 1.0
STOCK_RATIO_TOLERANCE_PP = 0.5
RATE_RANGE = (0.0, 80.0)
ADVANCES_RANGE_BN = (12_000.0, 40_000.0)
NPL_STOCK_RANGE_BN = (1_000.0, 20_000.0)
_POSITION_MAX_AGE_DAYS = 800


def _num(v) -> float | None:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def validate_extraction(payload: dict, position_date: date, today: date) -> list[str]:
    """Granular reject reasons; empty list = internally consistent."""
    rejects: list[str] = []

    for key in sorted(REQUIRED_EXTRACTION_KEYS):
        if _num(payload.get(key)) is None:
            rejects.append(f"required key missing or non-numeric: {key}")
    if rejects:
        return rejects

    for mid, spec in METRIC_SPECS.items():
        if not spec.fsr:
            continue
        v = _num(payload.get(mid))
        if v is None:
            continue  # optional sub-rate not published
        if spec.unit == "percent" and not (RATE_RANGE[0] <= v <= RATE_RANGE[1]):
            rejects.append(f"{mid} out of range {RATE_RANGE}: {v}")
        elif mid == "total_bank_advances" and not (ADVANCES_RANGE_BN[0] <= v <= ADVANCES_RANGE_BN[1]):
            rejects.append(f"total_bank_advances out of range {ADVANCES_RANGE_BN} bn: {v}")
        elif mid == "gross_npl_stock" and not (NPL_STOCK_RANGE_BN[0] <= v <= NPL_STOCK_RANGE_BN[1]):
            rejects.append(f"gross_npl_stock out of range {NPL_STOCK_RANGE_BN} bn: {v}")

    overall = _num(payload["overall_npl_ratio_fsr"])
    if not (RATE_RANGE[0] <= overall <= RATE_RANGE[1]):
        rejects.append(f"overall_npl_ratio_fsr out of range {RATE_RANGE}: {overall}")

    shares = {k: _num(payload[f"lending_share_sector_{k}"]) for k in _SECTORS}
    rates = {k: _num(payload[f"npl_rate_sector_{k}"]) for k in _SECTORS}

    share_sum = sum(shares.values())
    if abs(share_sum - 100.0) > SHARE_SUM_TOLERANCE:
        rejects.append(f"sector shares sum {share_sum:.2f}, expected 100±{SHARE_SUM_TOLERANCE}")

    weighted = sum(rates[k] * shares[k] for k in _SECTORS) / 100.0
    if abs(weighted - overall) > WEIGHTED_TOLERANCE_PP:
        rejects.append(
            f"weighted sector rates {weighted:.2f} vs overall {overall}"
            f" (tolerance {WEIGHTED_TOLERANCE_PP}pp)"
        )

    advances = _num(payload["total_bank_advances"])
    stock = _num(payload["gross_npl_stock"])
    stock_ratio = 100.0 * stock / advances if advances else 0.0
    if abs(stock_ratio - overall) > STOCK_RATIO_TOLERANCE_PP:
        rejects.append(
            f"npl stock/advances {stock_ratio:.2f} vs overall {overall}"
            f" (tolerance {STOCK_RATIO_TOLERANCE_PP}pp)"
        )

    if position_date > today:
        rejects.append(f"position date in the future: {position_date}")
    elif (today - position_date).days > _POSITION_MAX_AGE_DAYS:
        rejects.append(f"position date implausibly old: {position_date}")

    return rejects
