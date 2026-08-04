# BB NPL Structure Tracking Implementation Plan (v2 — post-verification, FSR shape)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** EconDelta gains 35 new metrics — 22 written annually from BB's FSR Table 2.3 (sector-wise NPL distribution) by a guarded one-pass LLM extractor, plus 13 seed-only series (band-wise + CMSME) preserved from the owner's Mar-2026 press-sourced deck.

**Architecture:** A dedicated scraper `scrapers/bb_npl_structure.py` (shape precedent: `scrapers/fiscal_gdp_ratios.py`) that discovers and fetches the latest FSR from BB's annual listing using the pipeline's proven fetch helpers (works from the box's BD IP; live-proven 2026-08-03), derives the position date by regex from the document's own text, short-circuits (exit 3 = skip) when that exact position is already captured, slices the Table 2.3 window out of the full text, runs ONE `run_max` extraction returning strict JSON, hard-gates the result with FULL-reconciliation arithmetic (shares ≈ 100, weighted rates ≈ printed overall ratio, stock/advances ≈ ratio; zero rows written on any failure), then upserts via `upsert_metric_history`. A separate seeder `scripts/seed_npl_structure.py` writes 14 deck primitives with `source="bb_via_press_static"`.

**Tech Stack:** Python 3 (repo `.venv`), pdfplumber (via `parsers.hybrid._extract_pdf_text`), `fetch_all._download_index_html` + `fetchers.pdf_discovery.discover_latest_pdf` + `fetchers.pdf_fetcher.fetch_pdf`, `claude_max.max_client.run_max`, `utils.supabase_writer`/`supabase_reader`, pytest + unittest.mock, systemd on ExonVPS.

**Spec:** `docs/superpowers/specs/2026-08-03-bb-npl-structure-design.md` — the AMENDMENT section governs.

## Global Constraints

- Gate: `.venv/bin/python -m pytest -q` and `.venv/bin/ruff check .` — run BARE from the worktree, never piped. No `ruff format`.
- Tests never touch network/Supabase: conftest sets `ECONDELTA_SKIP_SUPABASE=1`; mock `run_max`, fetch helpers, and Supabase writers with `unittest.mock`.
- NEVER pass `url=` to `upsert_metric_history` (Supabase base-URL override, landmine 22).
- The 35 metric ids must NEVER be added to `config/sources-v3.json` and NEVER to `CORE_METRIC_IDS` in `briefing/config.py:15-21`. Enforced by tests in Task 6.
- `config/sources-v3.json` is never edited by this plan (landmine 36). `docs/indicator-catalog.md` is generated — edit `scripts/build_catalog.py` then regenerate (landmine 15).
- Deploy: targeted unit install only, never full `install.sh` on the live box (landmine 37); new timer also joins `TIMERS=()` in `install.sh` (landmine 19).
- `notify()` levels: exactly `"info" | "warning" | "error"`.
- `metric_history` columns: `metric_id, as_of, value, source, ingested_at` — NO `source_as_of` column. Vintage lives in `as_of`.
- FSR prints amounts in **billion BDT**; the LLM extracts VERBATIM billions; code converts ×100 to crore before storage. The LLM never does arithmetic.
- Exit codes via `wrap_run`: 0=ok, 1=fail, 2=stale, 3=skip.
- House rules: TDD, files <800 lines, no bare excepts, conventional commits.

---

### Task 0: Verification + fixtures — **COMPLETE** (2026-08-03)

Findings recorded in the spec Amendment: QFSAR has none of the families and is stalled at Jul–Sep-2025; FSR 2025 Table 2.3 carries the sectoral family in full; band/CMSME are press-only → seed-only. Owner approved the FSR pivot + `accepted_stale` posture. Fixtures: `tests/_pdfs/fsr_fixture.pdf` (6,102,260 bytes) + `tests/fixtures/fsr_fixture_text.txt` (397,499 chars). Known-good Dec-2025 values for tests are in Task 4's `GOOD` payload.

---

### Task 1: Metric inventory + definitions rows

**Files:**
- Create: `scrapers/bb_npl_structure.py`
- Test: `tests/test_bb_npl_structure_inventory.py`

**Interfaces:**
- Produces: `METRIC_SPECS: dict[str, MetricSpec]` (35 ids), `FSR_EXTRACTION_KEYS: tuple[str, ...]`, `REQUIRED_EXTRACTION_KEYS: frozenset[str]`, `build_definitions_rows() -> list[dict]`, `SOURCE_LABEL = "BB FSR"`, `SEED_ONLY_SOURCE_NOTE = "bb_via_press_static"`. `MetricSpec` has fields `label, unit, family, fsr: bool`.

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_bb_npl_structure_inventory.py"""
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_inventory_has_35_ids_with_valid_shapes():
    from scrapers.bb_npl_structure import METRIC_SPECS
    assert len(METRIC_SPECS) == 35
    for mid, spec in METRIC_SPECS.items():
        assert mid == mid.lower() and " " not in mid
        assert spec.label
        assert spec.unit in ("percent", "amount_bdt_crore")
        assert spec.family in (
            "sector_rate", "sector_share", "sub_rate", "total",
            "band_rate", "band_outstanding", "cmsme",
        )
        assert isinstance(spec.fsr, bool)


def test_fsr_vs_seed_only_split():
    from scrapers.bb_npl_structure import METRIC_SPECS
    fsr = {m for m, s in METRIC_SPECS.items() if s.fsr}
    seed_only = {m for m, s in METRIC_SPECS.items() if not s.fsr}
    assert len(fsr) == 22 and len(seed_only) == 13
    assert "npl_rate_sector_trade_commerce" in fsr
    assert "npl_rate_band_lt1cr" in seed_only
    assert "npl_rate_cmsme_cottage" in seed_only


def test_no_collision_with_sources_v3_ids():
    from scrapers.bb_npl_structure import METRIC_SPECS
    cfg = json.loads((REPO_ROOT / "config" / "sources-v3.json").read_text())
    assert not (set(METRIC_SPECS) & {i["id"] for i in cfg["indicators"]})


def test_extraction_keys_and_required_set():
    from scrapers.bb_npl_structure import (
        FSR_EXTRACTION_KEYS, METRIC_SPECS, REQUIRED_EXTRACTION_KEYS,
    )
    fsr_ids = {m for m, s in METRIC_SPECS.items() if s.fsr}
    assert set(FSR_EXTRACTION_KEYS) == fsr_ids | {"overall_npl_ratio_fsr"}
    subs = {m for m, s in METRIC_SPECS.items() if s.family == "sub_rate"}
    assert REQUIRED_EXTRACTION_KEYS == (fsr_ids - subs) | {"overall_npl_ratio_fsr"}
    assert len(REQUIRED_EXTRACTION_KEYS) == 19


def test_definitions_rows_cover_all_35():
    from scrapers.bb_npl_structure import METRIC_SPECS, build_definitions_rows
    rows = build_definitions_rows()
    assert len(rows) == 35
    for row in rows:
        spec = METRIC_SPECS[row["metric_id"]]
        assert row["domain"] == "money_market"
        assert row["cadence"] == "fiscal_year"
        assert row["unit"] == spec.unit
        assert row["source"] == ("BB FSR" if spec.fsr else "bb_via_press_static")
```

- [ ] **Step 2: Run it to verify failure**

Run: `.venv/bin/python -m pytest tests/test_bb_npl_structure_inventory.py -v`
Expected: FAIL — ModuleNotFoundError.

- [ ] **Step 3: Implement**

```python
"""scrapers/bb_npl_structure.py

Banking-structure NPL metrics.

FSR-written family (annual): sector-wise NPL distribution from BB's
Financial Stability Report Table 2.3 — 8 top-level sectors (rate + share
of lending), 4 sub-sector rates, total advances and gross NPL stock.
One LLM extraction pass over a deterministic slice of the document, hard
arithmetic gate (full reconciliation), all-or-nothing upsert.

Seed-only family (no scheduled source — press/parliament disclosures):
band-wise NPL rates/outstandings and CMSME segment rates, written once by
scripts/seed_npl_structure.py with source "bb_via_press_static".

These ids are deliberately NOT in config/sources-v3.json and must never
join briefing.config.CORE_METRIC_IDS (owner decision: non-gating) nor
leave sentinel ACCEPTED_STALE (structural source lag). See the spec
amendment: docs/superpowers/specs/2026-08-03-bb-npl-structure-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass

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
    """
    return [
        {
            "metric_id": mid,
            "label": spec.label,
            "domain": "money_market",
            "unit": spec.unit,
            "cadence": "fiscal_year",
            "source": SOURCE_LABEL if spec.fsr else SEED_ONLY_SOURCE_NOTE,
        }
        for mid, spec in METRIC_SPECS.items()
    ]
```

Implementation note: check whether `utils.supabase_writer._normalize_definition` passes a `grace_days` key through (the live `metric_definitions` table HAS that column; the writer's `_DEFAULT_DEFINITION_FIELDS` does not list it). If unknown keys pass through, add `"grace_days": 400` to each row. If they're stripped, leave rows as-is and say so in your report — Task 9's post-merge checklist then carries a supervised `grace_days` PATCH.

- [ ] **Step 4: Run tests, expect PASS** — `.venv/bin/python -m pytest tests/test_bb_npl_structure_inventory.py -v`

- [ ] **Step 5: Commit**

```bash
git add scrapers/bb_npl_structure.py tests/test_bb_npl_structure_inventory.py
git commit -m "feat(npl-structure): 35-id metric inventory (22 FSR-written + 13 seed-only)"
```

---

### Task 2: FSR fetch + position date + table-window slice

**Files:**
- Modify: `scrapers/bb_npl_structure.py`
- Test: `tests/test_bb_npl_structure_dating.py`

**Interfaces:**
- Consumes: `fetch_all._download_index_html(url) -> str`; `fetchers.pdf_discovery.discover_latest_pdf(*, html, base_url) -> tuple[str, tuple[int, int]]`; `fetchers.pdf_fetcher.fetch_pdf(*, url, indicator_id, snapshot_dir, as_of_month, period=None) -> FetchResult` (writes `<snapshot_dir>/_pdfs/<indicator_id>/<as_of_month>/<name>.pdf`); `parsers.hybrid._extract_pdf_text(pdf_path, page_hint=None, indicator_id=...) -> str`; `fetchers.base.FetchResult` (attrs `artifact_path`, `artifact_type`, `source_url`, `sha256`).
- Produces: `FSR_LISTING_URL`, `fetch_latest_fsr(data_root: Path) -> "FetchResult"`, `extract_pdf_text_full(pdf_path) -> str`, `derive_position_date(text) -> date` (raises `PositionDateError`), `slice_table_window(text) -> str` (raises `TableMarkerError`), `class PositionDateError(ValueError)`, `class TableMarkerError(ValueError)`.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_bb_npl_structure_dating.py"""
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_TEXT = (REPO_ROOT / "tests" / "fixtures" / "fsr_fixture_text.txt").read_text()


def test_derive_position_date_from_real_fixture_is_end_dec_2025():
    from scrapers.bb_npl_structure import derive_position_date
    assert derive_position_date(FIXTURE_TEXT) == date(2025, 12, 31)


def test_latest_idiom_wins_and_end_of_variant_parses():
    from scrapers.bb_npl_structure import derive_position_date
    text = "as at end-December 2024 ... at the end of December 2025 the sector"
    assert derive_position_date(text) == date(2025, 12, 31)


def test_no_recognizable_date_raises():
    from scrapers.bb_npl_structure import PositionDateError, derive_position_date
    with pytest.raises(PositionDateError):
        derive_position_date("no dates here")


def test_slice_table_window_contains_table_and_total_row():
    from scrapers.bb_npl_structure import slice_table_window
    window = slice_table_window(FIXTURE_TEXT)
    assert "SECTOR-WISE NON-PERFORMING LOANS DISTRIBUTION" in window
    assert "Trade and Commerce" in window
    assert "18,204.30" in window          # the table's own total row
    assert len(window) < 20_000            # a slice, not the whole document


def test_slice_uses_last_marker_not_toc():
    from scrapers.bb_npl_structure import slice_table_window
    window = slice_table_window(FIXTURE_TEXT)
    assert "705.90" in window              # Agriculture data row, only near the real table


def test_slice_missing_marker_raises():
    from scrapers.bb_npl_structure import TableMarkerError, slice_table_window
    with pytest.raises(TableMarkerError):
        slice_table_window("an FSR whose layout changed completely")


def test_fetch_latest_fsr_wires_discovery_to_fetch(tmp_path):
    import scrapers.bb_npl_structure as mod
    fr = MagicMock(artifact_path=tmp_path / "fsr.pdf")
    with patch.object(mod, "_download_index_html", return_value="<html>") as dl, \
         patch.object(mod, "discover_latest_pdf", return_value=("https://x/f.pdf", (2026, 6))) as disc, \
         patch.object(mod, "fetch_pdf", return_value=fr) as fp:
        out = mod.fetch_latest_fsr(tmp_path)
    assert out is fr
    dl.assert_called_once_with(mod.FSR_LISTING_URL)
    disc.assert_called_once_with(html="<html>", base_url=mod.FSR_LISTING_URL)
    kwargs = fp.call_args.kwargs
    assert kwargs["indicator_id"] == "bb_npl_structure"
    assert kwargs["snapshot_dir"] == tmp_path
    assert kwargs["period"] == (2026, 6)
```

- [ ] **Step 2: Run to verify failure** — ImportError expected.

- [ ] **Step 3: Implement** — append to `scrapers/bb_npl_structure.py`:

```python
import re
from datetime import date, datetime, timezone
from pathlib import Path

from fetch_all import _download_index_html
from fetchers.pdf_discovery import discover_latest_pdf
from fetchers.pdf_fetcher import fetch_pdf

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
```

- [ ] **Step 4: Run tests, expect PASS** — `.venv/bin/python -m pytest tests/test_bb_npl_structure_dating.py tests/test_bb_npl_structure_inventory.py -v`

- [ ] **Step 5: Commit**

```bash
git add scrapers/bb_npl_structure.py tests/test_bb_npl_structure_dating.py
git commit -m "feat(npl-structure): FSR discovery/fetch + position date + table-window slice"
```

---

### Task 3: Extraction prompt + guarded LLM call

**Files:**
- Modify: `scrapers/bb_npl_structure.py`
- Test: `tests/test_bb_npl_structure_extract.py`

**Interfaces:**
- Consumes: `claude_max.max_client.run_max(*, prompt, model="claude-opus-4-8", timeout_s=1800, claude_binary=None, effort="high") -> MaxCallResult` (`.parsed` = fence-stripped `json.loads` or `None`); `MaxCallError`.
- Produces: `build_extraction_prompt(window: str) -> str`, `run_extraction(window: str) -> dict` (one retry, then raises `ExtractionError`), `class ExtractionError(RuntimeError)`.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_bb_npl_structure_extract.py"""
from unittest.mock import MagicMock, patch

import pytest


def test_prompt_names_every_key_and_demands_verbatim_billions():
    from scrapers.bb_npl_structure import FSR_EXTRACTION_KEYS, build_extraction_prompt
    prompt = build_extraction_prompt("TABLE WINDOW TEXT")
    for key in FSR_EXTRACTION_KEYS:
        assert key in prompt
    assert "null" in prompt
    assert "billion" in prompt.lower()      # verbatim billions, no conversion
    assert "TABLE WINDOW TEXT" in prompt


def test_run_extraction_returns_parsed_dict():
    from scrapers.bb_npl_structure import run_extraction
    ok = MagicMock(parsed={"overall_npl_ratio_fsr": 30.60})
    with patch("scrapers.bb_npl_structure.run_max", return_value=ok) as rm:
        assert run_extraction("w") == {"overall_npl_ratio_fsr": 30.60}
    assert rm.call_count == 1


def test_run_extraction_retries_once_then_raises():
    from scrapers.bb_npl_structure import ExtractionError, run_extraction
    bad = MagicMock(parsed=None, raw_text="prose")
    with patch("scrapers.bb_npl_structure.run_max", return_value=bad) as rm:
        with pytest.raises(ExtractionError):
            run_extraction("w")
    assert rm.call_count == 2


def test_run_extraction_wraps_maxcallerror():
    from claude_max.max_client import MaxCallError
    from scrapers.bb_npl_structure import ExtractionError, run_extraction
    with patch("scrapers.bb_npl_structure.run_max", side_effect=MaxCallError("boom")):
        with pytest.raises(ExtractionError):
            run_extraction("w")
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — append:

```python
from claude_max.max_client import MaxCallError, run_max

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
```

- [ ] **Step 4: Run tests, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add scrapers/bb_npl_structure.py tests/test_bb_npl_structure_extract.py
git commit -m "feat(npl-structure): Table 2.3 extraction prompt + retried run_max call"
```

---

### Task 4: Full-reconciliation gate (all-or-nothing)

> **EXECUTED WITH AMENDMENTS (2026-08-04, adversarial review rounds 1-2):** `_num` is finite-only (`math.isfinite` — bare NaN from `json.loads` must reject, not disable the reconciliations); `overall_npl_ratio_fsr` gets an explicit RATE_RANGE check; `_POSITION_MAX_AGE_DAYS=800` and `RATE_RANGE=(0,80)` are both mutation-pinned by boundary tests (672-day position; sub-rate 65.0); stock range vs stock/advances ratio checks are separately pinned; module docstring states measured gate coverage (only mfg/trade/services/other rates are wrong-column-proof). The shipped code + tests in git are canonical over the snippets below.

**Files:**
- Modify: `scrapers/bb_npl_structure.py`
- Test: `tests/test_bb_npl_structure_gate.py`

**Interfaces:**
- Produces: `validate_extraction(payload: dict, position_date: date, today: date) -> list[str]` (empty = pass), constants `SHARE_SUM_TOLERANCE = 0.5`, `WEIGHTED_TOLERANCE_PP = 1.0`, `STOCK_RATIO_TOLERANCE_PP = 0.5`, `RATE_RANGE = (0.0, 80.0)`, `ADVANCES_RANGE_BN = (12_000.0, 40_000.0)`, `NPL_STOCK_RANGE_BN = (1_000.0, 20_000.0)`, `_POSITION_MAX_AGE_DAYS = 800`.

The gate is per-document internal consistency ONLY — it never compares against prior DB values, so the bb_forex ratchet shape (landmine 38) is structurally impossible.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_bb_npl_structure_gate.py"""
from datetime import date

TODAY = date(2026, 8, 3)
POS = date(2025, 12, 31)

# The REAL FSR 2025 Table 2.3 figures (billion BDT / percent, verbatim).
GOOD = {
    "npl_rate_sector_agriculture": 29.59, "lending_share_sector_agriculture": 3.88,
    "npl_rate_sector_industrial_mfg": 28.91, "lending_share_sector_industrial_mfg": 51.35,
    "npl_rate_sector_industrial_services": 27.88, "lending_share_sector_industrial_services": 11.49,
    "npl_rate_sector_consumer_credit": 8.01, "lending_share_sector_consumer_credit": 6.83,
    "npl_rate_sector_trade_commerce": 49.88, "lending_share_sector_trade_commerce": 18.16,
    "npl_rate_sector_nbfi": 21.61, "lending_share_sector_nbfi": 0.48,
    "npl_rate_sector_capital_market": 7.35, "lending_share_sector_capital_market": 0.46,
    "npl_rate_sector_other": 22.63, "lending_share_sector_other": 7.36,
    "npl_rate_sub_rmg": 31.15, "npl_rate_sub_construction": 31.54,
    "npl_rate_sub_housing_finance": 13.10, "npl_rate_sub_smc_industries": 24.09,
    "total_bank_advances": 18204.30, "gross_npl_stock": 5570.32,
    "overall_npl_ratio_fsr": 30.60,
}


def _gate(payload, pos=POS):
    from scrapers.bb_npl_structure import validate_extraction
    return validate_extraction(payload, pos, TODAY)


def test_real_fsr_2025_figures_pass():
    assert _gate(dict(GOOD)) == []


def test_missing_required_key_rejects():
    bad = dict(GOOD); bad["npl_rate_sector_trade_commerce"] = None
    assert any("npl_rate_sector_trade_commerce" in r for r in _gate(bad))


def test_missing_sub_rate_is_fine():
    ok = dict(GOOD); ok["npl_rate_sub_rmg"] = None
    assert _gate(ok) == []


def test_wrong_column_read_fails_weighted_reconciliation():
    bad = dict(GOOD); bad["npl_rate_sector_industrial_mfg"] = 48.51  # Share-of-NPLs column
    assert any("weighted" in r for r in _gate(bad))


def test_decimal_slip_in_stock_fails_stock_ratio_check():
    bad = dict(GOOD); bad["gross_npl_stock"] = 557.032
    assert any("stock" in r for r in _gate(bad))


def test_shares_not_summing_to_100_rejects():
    bad = dict(GOOD); bad["lending_share_sector_other"] = 17.36
    assert any("share" in r for r in _gate(bad))


def test_rate_out_of_range_rejects():
    bad = dict(GOOD); bad["npl_rate_sub_construction"] = 85.0
    assert any("npl_rate_sub_construction" in r for r in _gate(bad))


def test_advances_out_of_range_rejects():
    bad = dict(GOOD); bad["total_bank_advances"] = 1820430.0   # crore slipped in
    assert any("total_bank_advances" in r for r in _gate(bad))


def test_future_position_rejects():
    assert any("position" in r for r in _gate(dict(GOOD), pos=date(2027, 12, 31)))


def test_ancient_position_rejects():
    assert any("position" in r for r in _gate(dict(GOOD), pos=date(2023, 12, 31)))
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — append:

```python
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
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


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
```

- [ ] **Step 4: Run tests, expect PASS — then sabotage-prove**: temporarily gut `validate_extraction` to `return []`, confirm every mutation test fails, restore (`PYTHONDONTWRITEBYTECODE=1`, clear `__pycache__`).

- [ ] **Step 5: Commit**

```bash
git add scrapers/bb_npl_structure.py tests/test_bb_npl_structure_gate.py
git commit -m "feat(npl-structure): full-reconciliation arithmetic gate (all-or-nothing)"
```

---

### Task 5: Skip logic, unit conversion, upsert, main()

**Files:**
- Modify: `scrapers/bb_npl_structure.py`
- Test: `tests/test_bb_npl_structure_main.py`

**Interfaces:**
- Consumes: `utils.supabase_writer.upsert_metric_history(*, data, as_of, source, ingested_at=None) -> int`, `upsert_metric_definitions_seed(list[dict]) -> int`, `verify_landed_count(expected, *, since, metric_ids, source_label)`, `wrap_run`, `SupabaseWriteError`; `utils.supabase_reader.get_metric_history(metric_id, *, days) -> list[dict]` (newest first), `SupabaseReadError`; `utils.notifier.notify`.
- Produces: `already_captured(position_date) -> bool` (EXACT-date match so older issues can backfill), `payload_to_rows(payload) -> dict[str, float]` (billions→crore ×100 for the two totals), `main() -> int` (0/1/3), `_BELLWETHER_ID = "npl_rate_sector_trade_commerce"`.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_bb_npl_structure_main.py"""
from datetime import date
from unittest.mock import MagicMock, patch

import scrapers.bb_npl_structure as mod
from tests.test_bb_npl_structure_gate import GOOD

POS = date(2025, 12, 31)


def test_already_captured_exact_date_match_only():
    rows = [{"as_of": "2026-12-31"}, {"as_of": "2025-12-31"}]
    with patch.object(mod, "get_metric_history", return_value=rows):
        assert mod.already_captured(POS) is True
        assert mod.already_captured(date(2024, 12, 31)) is False  # older issue → backfillable


def test_already_captured_false_on_empty_or_read_error():
    from utils.supabase_reader import SupabaseReadError
    with patch.object(mod, "get_metric_history", return_value=[]):
        assert mod.already_captured(POS) is False
    with patch.object(mod, "get_metric_history", side_effect=SupabaseReadError("down")):
        assert mod.already_captured(POS) is False  # fail-open: duplicate run is idempotent


def test_payload_to_rows_converts_billions_to_crore_and_drops_check_field():
    rows = mod.payload_to_rows(dict(GOOD))
    assert rows["total_bank_advances"] == 1_820_430.0     # 18,204.30 bn -> crore
    assert rows["gross_npl_stock"] == 557_032.0
    assert rows["npl_rate_sector_trade_commerce"] == 49.88  # percents untouched
    assert "overall_npl_ratio_fsr" not in rows              # check-only, never stored
    assert "npl_rate_band_lt1cr" not in rows                # seed-only ids never written here


def test_payload_to_rows_skips_null_sub_rates():
    p = dict(GOOD); p["npl_rate_sub_rmg"] = None
    assert "npl_rate_sub_rmg" not in mod.payload_to_rows(p)


def test_main_skips_before_llm_when_position_captured(tmp_path):
    fr = MagicMock(artifact_path=tmp_path / "f.pdf")
    with patch.object(mod, "fetch_latest_fsr", return_value=fr), \
         patch.object(mod, "extract_pdf_text_full", return_value="end-December 2025"), \
         patch.object(mod, "already_captured", return_value=True), \
         patch.object(mod, "run_extraction") as rex:
        assert mod.main() == 3
    rex.assert_not_called()


def test_main_gate_reject_writes_nothing_and_notifies(tmp_path):
    fr = MagicMock(artifact_path=tmp_path / "f.pdf")
    bad = dict(GOOD); bad["gross_npl_stock"] = 557.032
    with patch.object(mod, "fetch_latest_fsr", return_value=fr), \
         patch.object(mod, "extract_pdf_text_full", return_value="end-December 2025"), \
         patch.object(mod, "slice_table_window", return_value="w"), \
         patch.object(mod, "already_captured", return_value=False), \
         patch.object(mod, "run_extraction", return_value=bad), \
         patch.object(mod, "upsert_metric_history") as up, \
         patch.object(mod, "notify") as noti:
        assert mod.main() == 1
    up.assert_not_called()
    assert noti.call_args.args[0] == "error"
    assert "stock" in noti.call_args.args[2]


def test_main_happy_path_seeds_definitions_then_writes(tmp_path):
    fr = MagicMock(artifact_path=tmp_path / "f.pdf")
    with patch.object(mod, "fetch_latest_fsr", return_value=fr), \
         patch.object(mod, "extract_pdf_text_full", return_value="end-December 2025"), \
         patch.object(mod, "slice_table_window", return_value="w"), \
         patch.object(mod, "already_captured", return_value=False), \
         patch.object(mod, "run_extraction", return_value=dict(GOOD)), \
         patch.object(mod, "upsert_metric_definitions_seed", return_value=0) as seed, \
         patch.object(mod, "upsert_metric_history", return_value=22) as up, \
         patch.object(mod, "verify_landed_count"):
        assert mod.main() == 0
    seed.assert_called_once()
    kwargs = up.call_args.kwargs
    assert kwargs["as_of"] == POS
    assert kwargs["source"] == "BB FSR"
    assert "url" not in kwargs


def test_main_fetch_failure_notifies_and_fails():
    with patch.object(mod, "fetch_latest_fsr", side_effect=RuntimeError("wall")), \
         patch.object(mod, "notify") as noti:
        assert mod.main() == 1
    assert noti.call_args.args[0] == "error"
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — append:

```python
import logging
import sys

from utils.notifier import notify
from utils.supabase_reader import SupabaseReadError, get_metric_history
from utils.supabase_writer import (
    SupabaseWriteError,
    upsert_metric_definitions_seed,
    upsert_metric_history,
    verify_landed_count,
)

logger = logging.getLogger("bb_npl_structure")

_DATA_ROOT = REPO_ROOT / "data"
_BELLWETHER_ID = "npl_rate_sector_trade_commerce"
_RECENT_ISSUES_WINDOW = 10  # rows; annual series → a decade of coverage
_BN_TO_CRORE = 100.0
_CRORE_IDS = ("total_bank_advances", "gross_npl_stock")


def already_captured(position_date: date) -> bool:
    """True only if THIS exact position date already has a row.

    Exact-match (not >=) so an older FSR issue can still backfill history.
    Fail-open on read errors: a duplicate run costs one LLM call and an
    idempotent merge-upsert; a false 'captured' would drop an issue.
    """
    try:
        rows = get_metric_history(_BELLWETHER_ID, days=_RECENT_ISSUES_WINDOW)
    except SupabaseReadError as e:
        logger.warning("capture check failed (%s) — proceeding", e)
        return False
    return position_date.isoformat() in {r["as_of"] for r in rows}


def payload_to_rows(payload: dict) -> dict[str, float]:
    """FSR-written metrics only; billions→crore for the two amount ids;
    check field and null sub-rates dropped."""
    rows: dict[str, float] = {}
    for mid, spec in METRIC_SPECS.items():
        if not spec.fsr:
            continue
        v = _num(payload.get(mid))
        if v is None:
            continue
        rows[mid] = v * _BN_TO_CRORE if mid in _CRORE_IDS else v
    return rows


def main() -> int:
    try:
        artifact = fetch_latest_fsr(_DATA_ROOT)
    except Exception as e:
        notify("error", "bb_npl_structure: FSR fetch failed", str(e))
        return 1

    try:
        text = extract_pdf_text_full(artifact.artifact_path)
        position_date = derive_position_date(text)
    except PositionDateError as e:
        notify("error", "bb_npl_structure: cannot date the FSR", str(e))
        return 1
    except Exception as e:
        logger.exception("pdf text extraction failed")
        notify("error", "bb_npl_structure: FSR text extraction failed", str(e))
        return 1

    if already_captured(position_date):
        logger.info("FSR position %s already captured — skip", position_date)
        return 3

    try:
        window = slice_table_window(text)
    except TableMarkerError as e:
        notify("error", "bb_npl_structure: FSR layout changed", str(e))
        return 1

    try:
        payload = run_extraction(window)
    except ExtractionError as e:
        notify("error", "bb_npl_structure: extraction failed", str(e))
        return 1

    today = datetime.now(timezone.utc).date()
    rejects = validate_extraction(payload, position_date, today)
    if rejects:
        notify(
            "error",
            f"bb_npl_structure: gate rejected {position_date} extraction — ZERO rows written",
            "\n".join(rejects),
        )
        return 1

    rows = payload_to_rows(payload)
    write_ts = datetime.now(timezone.utc)
    try:
        upsert_metric_definitions_seed(build_definitions_rows())  # first-insert-wins no-op later
        count = upsert_metric_history(
            data=rows, as_of=position_date, source=SOURCE_LABEL, ingested_at=write_ts,
        )
    except SupabaseWriteError as e:
        notify("error", "bb_npl_structure: Supabase write failed", str(e))
        return 1
    verify_landed_count(count, since=write_ts, metric_ids=list(rows), source_label="bb_npl_structure")
    logger.info("captured FSR %s: %d metrics", position_date, count)
    return 0


if __name__ == "__main__":
    from utils.supabase_writer import wrap_run

    sys.exit(wrap_run("bb_npl_structure", "econdelta-npl-structure.service", main))
```

- [ ] **Step 4: Run tests, expect PASS** — all five module test files together.

- [ ] **Step 5: Commit**

```bash
git add scrapers/bb_npl_structure.py tests/test_bb_npl_structure_main.py
git commit -m "feat(npl-structure): exact-date skip, bn->crore conversion, main() with wrap_run"
```

---

### Task 6: Sentinel (accepted_stale + cadence) + catalog + gating-protection tests

**Files:**
- Modify: `sentinel/cadence.py` (`_SCRAPER_CADENCE` dict) and `sentinel/freshness.py` (`ACCEPTED_STALE_METRIC_IDS`, line ~33)
- Modify: `scripts/build_catalog.py` (`DERIVED_KEYS`)
- Regenerate: `docs/indicator-catalog.md`
- Test: `tests/test_bb_npl_structure_wiring.py`

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_bb_npl_structure_wiring.py"""
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_every_metric_resolves_fiscal_year_in_sentinel():
    from scrapers.bb_npl_structure import METRIC_SPECS
    from sentinel.cadence import load_cadence_map, resolve_cadence
    cmap = load_cadence_map()
    for mid in METRIC_SPECS:
        assert resolve_cadence(mid, cmap) == "fiscal_year", mid


def test_every_metric_is_accepted_stale():
    # Owner decision: structural source lag (annual FSR ~6mo lag; press-only
    # families with no schedule) → tracked, never paged.
    from scrapers.bb_npl_structure import METRIC_SPECS
    from sentinel.freshness import ACCEPTED_STALE_METRIC_IDS
    assert set(METRIC_SPECS) <= ACCEPTED_STALE_METRIC_IDS


def test_no_metric_ever_gates_the_briefing():
    from briefing.config import CORE_METRIC_IDS
    from scrapers.bb_npl_structure import METRIC_SPECS
    assert not (set(METRIC_SPECS) & CORE_METRIC_IDS)


def test_no_metric_in_sources_v3():
    from scrapers.bb_npl_structure import METRIC_SPECS
    cfg = json.loads((REPO_ROOT / "config" / "sources-v3.json").read_text())
    assert not (set(METRIC_SPECS) & {i["id"] for i in cfg["indicators"]})


def test_catalog_lists_every_metric():
    from scrapers.bb_npl_structure import METRIC_SPECS
    catalog = (REPO_ROOT / "docs" / "indicator-catalog.md").read_text()
    for mid in METRIC_SPECS:
        assert f"`{mid}`" in catalog, mid
```

- [ ] **Step 2: Run to verify failure** (cadence + accepted_stale + catalog tests fail; the two negative tests pass from day one as regression guards).

- [ ] **Step 3: Wire sentinel.** In `sentinel/cadence.py`, append all 35 ids to `_SCRAPER_CADENCE` as `"fiscal_year"` under a comment naming this plan — write all 35 lines explicitly (explicit beats prefix magic; note `npl_*` ids would otherwise hit the quarterly prefix rule, which is WRONG for these). In `sentinel/freshness.py`, extend `ACCEPTED_STALE_METRIC_IDS` with the same 35 ids, matching the existing frozenset style, comment: structural source lag — FSR annual ~6mo lag / press-only seed series; spec amendment 2026-08-03. Alphabetize within each block.

- [ ] **Step 4: Catalog.** In `scripts/build_catalog.py` `DERIVED_KEYS`, add one `(metric_id, unit, cadence, description)` tuple per id — all 35 written out in full: unit per `METRIC_SPECS` (`percent` / `amount_bdt_crore`), cadence `"fiscal_year"`, description = MetricSpec label + for FSR ids " — from BB FSR Table 2.3 via scrapers/bb_npl_structure.py (annual)." / for seed-only ids " — static press-sourced series (bb_via_press_static), no scheduled writer." Then regenerate:

```bash
.venv/bin/python scripts/build_catalog.py > docs/indicator-catalog.md
```

- [ ] **Step 5: Run tests, expect PASS** — the wiring file plus the sentinel's existing test files (find them with `ls tests/ | grep -i -E "sentinel|cadence|freshness"`) to confirm nothing regressed from the frozenset/dict growth.

- [ ] **Step 6: Commit**

```bash
git add sentinel/cadence.py sentinel/freshness.py scripts/build_catalog.py docs/indicator-catalog.md tests/test_bb_npl_structure_wiring.py
git commit -m "feat(npl-structure): sentinel fiscal_year + accepted_stale wiring, catalog entries, gating-protection tests"
```

---

### Task 7: Static seeder (14 deck primitives, press provenance)

**Files:**
- Create: `scripts/seed_npl_structure.py`
- Test: `tests/test_seed_npl_structure.py`

**Interfaces:**
- Consumes: `scrapers.bb_npl_structure.METRIC_SPECS`, `build_definitions_rows`; `utils.supabase_writer.upsert_metric_history`, `upsert_metric_definitions_seed`.
- Produces: `SEED_VALUES: dict[str, float]` (14), `SEED_AS_OF = date(2026, 3, 31)`, `SEED_SOURCE = "bb_via_press_static"`, `run(*, execute: bool) -> int`, CLI `--execute` (dry-run default).

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_seed_npl_structure.py"""
from datetime import date
from unittest.mock import patch


def test_seed_values_are_the_deck_primitives_exactly():
    from scripts.seed_npl_structure import SEED_AS_OF, SEED_SOURCE, SEED_VALUES
    assert SEED_AS_OF == date(2026, 3, 31)
    assert SEED_SOURCE == "bb_via_press_static"
    assert len(SEED_VALUES) == 14
    assert SEED_VALUES["npl_rate_band_lt1cr"] == 15.0
    assert SEED_VALUES["npl_rate_band_1_10cr"] == 26.5
    assert SEED_VALUES["npl_rate_band_10_20cr"] == 45.0
    assert SEED_VALUES["npl_rate_band_20_30cr"] == 36.0
    assert SEED_VALUES["npl_rate_band_30_40cr"] == 39.0
    assert SEED_VALUES["npl_rate_band_40_50cr"] == 45.0
    assert SEED_VALUES["npl_rate_band_gt50cr"] == 42.5
    assert SEED_VALUES["loans_outstanding_band_lt1cr"] == 410_000
    assert SEED_VALUES["loans_outstanding_band_1_10cr"] == 361_000
    assert SEED_VALUES["loans_outstanding_band_gt50cr"] == 576_000
    assert SEED_VALUES["npl_rate_cmsme_overall"] == 34.0
    assert SEED_VALUES["npl_rate_cmsme_cottage"] == 53.0
    assert SEED_VALUES["npl_rate_cmsme_medium"] == 38.0
    assert SEED_VALUES["total_bank_advances"] == 1_784_000
    # Press-taxonomy sector values deliberately ABSENT (spec amendment:
    # the sector family lives in the FSR taxonomy; press cut would orphan).
    for absent in ("lending_share_trade", "npl_rate_consumer", "npl_rate_industry"):
        assert absent not in SEED_VALUES


def test_every_seed_id_is_known_and_seed_only_or_shared_total():
    from scrapers.bb_npl_structure import METRIC_SPECS
    from scripts.seed_npl_structure import SEED_VALUES
    assert set(SEED_VALUES) <= set(METRIC_SPECS)
    for mid in SEED_VALUES:
        assert (not METRIC_SPECS[mid].fsr) or mid == "total_bank_advances"


def test_dry_run_writes_nothing():
    import scripts.seed_npl_structure as seeder
    with patch.object(seeder, "upsert_metric_history") as up, \
         patch.object(seeder, "upsert_metric_definitions_seed") as seed:
        assert seeder.run(execute=False) == 0
    up.assert_not_called()
    seed.assert_not_called()


def test_execute_seeds_definitions_then_history():
    import scripts.seed_npl_structure as seeder
    with patch.object(seeder, "upsert_metric_definitions_seed", return_value=35) as seed, \
         patch.object(seeder, "upsert_metric_history", return_value=14) as up:
        assert seeder.run(execute=True) == 0
    seed.assert_called_once()
    kwargs = up.call_args.kwargs
    assert kwargs["source"] == "bb_via_press_static"
    assert kwargs["as_of"] == date(2026, 3, 31)
    assert "url" not in kwargs
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

```python
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
```

- [ ] **Step 4: Run tests + the real dry-run** — `.venv/bin/python -m pytest tests/test_seed_npl_structure.py -v`; then `.venv/bin/python -m scripts.seed_npl_structure` (expect 14 DRY RUN lines, exit 0, nothing written).

- [ ] **Step 5: Commit**

```bash
git add scripts/seed_npl_structure.py tests/test_seed_npl_structure.py
git commit -m "feat(npl-structure): press-provenance static seeder, dry-run default (14 values)"
```

---

### Task 8: systemd units, install wiring, docs

**Files:**
- Create: `deploy/econdelta-npl-structure.service`, `deploy/econdelta-npl-structure.timer`
- Modify: `deploy/install.sh` (`TIMERS=()` array — landmine 19)
- Modify: `docs/data-contract.md`, `AGENTS.md`

- [ ] **Step 1: Service unit** (mirror `deploy/econdelta-fiscal-gdp.service` verbatim except names/paths and `TimeoutStartSec=2700` — worst case is 2x900s of LLM retry alone before fetch + 148-page pdfplumber extraction; the repo's largest existing unit ceiling (1800s) would kill the run mid-retry):

```ini
[Unit]
Description=EconDelta — FSR sector-wise NPL extractor (bb_npl_structure)
Documentation=https://github.com/clauding-lab/econdelta
After=network-online.target
Wants=network-online.target

StartLimitIntervalSec=1800
StartLimitBurst=3

[Service]
Type=oneshot
User=adnan-local
Group=adnan-local
WorkingDirectory=/home/adnan-local/econdelta
EnvironmentFile=/etc/econdelta.env
ExecStart=/home/adnan-local/econdelta/.venv/bin/python -m scrapers.bb_npl_structure

MemoryMax=500M
MemoryHigh=400M
CPUQuota=50%
TasksMax=512

PrivateTmp=true
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/adnan-local/econdelta/data /home/adnan-local/econdelta/logs /home/adnan-local/.claude

TimeoutStartSec=2700
Restart=on-failure
RestartSec=300

StandardOutput=append:/home/adnan-local/econdelta/logs/bb_npl_structure-systemd.log
StandardError=append:/home/adnan-local/econdelta/logs/bb_npl_structure-systemd.log
SyslogIdentifier=econdelta-npl-structure

[Install]
WantedBy=multi-user.target
```

Deploy-time note (Task 9 checklist, not a unit line — fix round 1, owner-approved
2026-08-04): the `.claude` **directory** carve-out is no longer a Task 9
deploy-time step — it's now baked into the unit's own `ReadWritePaths=` above,
in-repo, matching `econdelta-parse.service` / `econdelta-briefing.service`
exactly. What Task 9's deploy checklist must still do is mirror the separate
`~/.claude.json` **file** drop-in that `econdelta-parse.service` needed on the
box (the May-2026 parse-401 incident, AGENTS.md landmine 17): the claude CLI
writes `~/.claude.json` (a *sibling* of the `.claude/` dir, at home root) on
every run, and under `ProtectHome=read-only` that write hits EROFS unless it's
separately carved out — `ReadWritePaths` covering the directory does NOT cover
that file. Both carve-outs are needed; only the directory one shipped in this
unit.

- [ ] **Step 2: Timer** (weekly poll; exit-3 skip makes idle weeks free — no LLM call after the position-date short-circuit):

```ini
[Unit]
Description=Run EconDelta FSR NPL-structure extractor weekly (Sun 23:29 UTC / Mon 05:29 BDT). FSR is annual; weekly polling + in-scraper skip captures a new issue within a week of publication at zero idle cost.
Requires=econdelta-npl-structure.service

[Timer]
OnCalendar=Sun *-*-* 23:29:00 UTC
Persistent=true
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
```

(Minute slot `:29` — `:20`/`:23`/`:26` are taken by imf-eff/imf-debt/fiscal-gdp.)

- [ ] **Step 3: `install.sh`** — add `econdelta-npl-structure.timer` to the `TIMERS=()` array (one line, matching style).

- [ ] **Step 4: Docs.**
  - `docs/data-contract.md`: (a) a new-indicator entry for the family per §8's convention (35 ids, cadence fiscal_year, sources `BB FSR` / `bb_via_press_static`, extractor path, accepted_stale posture + one-line rationale); (b) one paragraph in §3's provenance semantics documenting the static-seed label convention (`mof_mfr_static`, `mof_mfr_static_provisional`, `bb_via_press_static`) — previously undocumented.
  - `AGENTS.md`: next-numbered landmine:

```
NN. **bb_npl_structure ids live OUTSIDE the pipeline config.** The 35
    banking-structure metrics (22 FSR-written, 13 seed-only press series)
    are written by scrapers/bb_npl_structure.py and
    scripts/seed_npl_structure.py. Never add these ids to
    config/sources-v3.json (each would become a daily LLM parse), never to
    briefing CORE_METRIC_IDS (owner: non-gating), and never remove them
    from sentinel ACCEPTED_STALE_METRIC_IDS (structural source lag — FSR
    is annual with ~6mo lag; band/CMSME have no scheduled source at all).
    tests/test_bb_npl_structure_wiring.py enforces all three.
```

- [ ] **Step 5: FULL gate** — `.venv/bin/python -m pytest -q` and `.venv/bin/ruff check .`, both bare, both exit 0.

- [ ] **Step 6: Commit**

```bash
git add deploy/econdelta-npl-structure.service deploy/econdelta-npl-structure.timer deploy/install.sh docs/data-contract.md AGENTS.md
git commit -m "feat(npl-structure): systemd units, install wiring, contract + landmine docs"
```

---

### Task 9: Final review, PR, deploy + seed (deploy steps OWNER-GATED)

- [ ] **Step 1: Full gate one final time**, citing exit codes and counts.
- [ ] **Step 2: Push + PR** (`git push -u origin feat/bb-npl-structure`; `gh pr create` with house-format body citing spec amendment + plan + test counts; `gh pr checks --watch`).
- [ ] **Step 3: Merge only with owner approval.**
- [ ] **Step 4 (POST-MERGE, OWNER-GATED batch — enumerate, get ONE approval):**
  1. Box pull via healed gitpull (or manual `git pull --ff-only`).
  2. Targeted unit install (landmine 37): `install -m 0644` both units → `daemon-reload` → `enable --now econdelta-npl-structure.timer`. The `.claude` directory carve-out already shipped in the unit's `ReadWritePaths=` (fix round 1, owner-approved 2026-08-04) — no action needed there. Still check whether the separate `~/.claude.json` file drop-in (landmine 17, e.g. `econdelta-parse.service.d/10-claude-json-writable.conf`) is needed here too; mirror if so.
  3. Supervised seed: before-SELECT proof (0 rows for the 35 ids) → box-side `.venv/bin/python -m scripts.seed_npl_structure --execute` (env sourced server-side, status codes only) → after-SELECT proof (14 rows at 2026-03-31, source `bb_via_press_static`).
  4. First live run: `systemctl start econdelta-npl-structure.service` → expect run_logs `ok` with 22 rows at `as_of 2025-12-31` source `BB FSR`, then a second manual run → `skip` (exit 3).
  5. If `grace_days` didn't seed via the writer (Task 1 note): supervised PATCH of `metric_definitions.grace_days=400` for the 35 ids, with proofs.
  6. Sentinel check on next run: all 35 ids in `accepted_stale` bucket, zero new breaches, zero unmapped.
- [ ] **Step 5: Memory + session note** per house practice.

---

## Self-Review (v2 — checked after rewrite)

1. **Spec coverage:** every amendment clause has a task — FSR pivot (T2), taxonomy + 35 ids (T1), full-reconciliation gate (T4), bn→crore (T5), accepted_stale + fiscal_year (T6), 14-value seed with press-taxonomy drops (T7), fixtures (T0 done). ✓
2. **Placeholders:** Task 6 Steps 3–4 instruct "write all 35 lines/tuples in full" with exact value rules — pattern fully specified, two worked description formats given. ✓
3. **Type consistency:** `validate_extraction(payload, position_date, today)` (T4↔T5); `payload_to_rows` drops the check field (T5 tests); `run(execute=...)` (T7); the `MetricSpec.fsr` flag used consistently (T1/T5/T7); `GOOD` keys = `FSR_EXTRACTION_KEYS`. ✓
