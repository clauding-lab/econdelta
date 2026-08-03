# BB NPL Structure Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** EconDelta gains ~23 new quarterly metrics (band-wise NPL rates and outstandings, sectoral lending shares and NPL rates, CMSME segment rates) extracted from BB's QFSAR in one guarded LLM pass, plus a one-time press-provenance seed of the Mar-2026 values.

**Architecture:** A dedicated scraper `scrapers/bb_npl_structure.py` (precedent: `scrapers/fiscal_gdp_ratios.py`) that REUSES the QFSAR PDF artifact the existing pipeline already fetches daily for `gross_npl_ratio` (`data/_pdfs/gross_npl_ratio/<YYYY-MM>/*.pdf` on the box — no new BB fetch path, no F5 exposure). It derives the position date deterministically by regex, short-circuits (exit 3 = skip) when that date is already captured, otherwise runs ONE `run_max` extraction returning strict JSON for all metrics, hard-gates the result with arithmetic self-checks (zero rows written on failure), then upserts via `upsert_metric_history`. A separate seeder `scripts/seed_npl_structure.py` writes the deck's Mar-2026 primitives with `source="bb_via_press_static"`.

**Tech Stack:** Python 3 (repo `.venv`), pdfplumber (via `parsers.hybrid._extract_pdf_text`), `claude_max.max_client.run_max` (Max OAuth CLI), `utils.supabase_writer` / `utils.supabase_reader`, pytest + unittest.mock, systemd on ExonVPS.

**Spec:** `docs/superpowers/specs/2026-08-03-bb-npl-structure-design.md` (owner-approved). All five owner decisions in that spec bind this plan.

## Global Constraints

- Gate: `.venv/bin/python -m pytest -q` and `.venv/bin/ruff check .` — run BARE, never piped (a hook enforces this). No `ruff format`.
- Tests never touch network/Supabase: `tests/conftest.py` sets `ECONDELTA_SKIP_SUPABASE=1` and `ECONDELTA_SKIP_OPUS_REVIEW=1`; mock `run_max` and Supabase writers with `unittest.mock`.
- NEVER pass `url=` to `upsert_metric_history` — it is the Supabase base-URL override, not provenance (AGENTS.md landmine 22).
- The new metric ids must NEVER be added to `config/sources-v3.json` (would put 23 metrics through the daily fetch/parse LLM path) and NEVER to `CORE_METRIC_IDS` in `briefing/config.py:15-21` (would gate the Monday briefing). Both are enforced by tests in Task 6.
- `config/sources-v3.json` is hand-maintained: this plan never edits it (landmine 36). `docs/indicator-catalog.md` is generated: edit `scripts/build_catalog.py`, then `python3 scripts/build_catalog.py > docs/indicator-catalog.md` (landmine 15).
- Deploy: never run full `deploy/install.sh` on the live box (Persistent=true catch-up storm, landmine 37) — targeted `install -m 0644` + `daemon-reload` + `enable --now` only. New timer must ALSO be added to the `TIMERS=()` array in `install.sh` (landmine 19) for fresh-box installs.
- `notify()` levels are `Literal["info", "warning", "error"]` — use exactly these strings.
- `metric_history` columns: `metric_id, as_of, value, source, ingested_at` — there is NO `source_as_of` column (the seeder precedents that write one target the separate monthly tables, landmine 20). Vintage is carried in `as_of` itself.
- Exit codes map via `wrap_run`: 0=ok, 1=fail, 2=stale, 3=skip.
- House rules: TDD every task, files <800 lines, no bare excepts, immutable-style transforms, conventional commits.
- Box facts (for Task 0/9 only): `ssh exonhost` = adnan-local@103.187.23.22, repo `/home/adnan-local/econdelta`. `/etc/econdelta.env` is mode 640 — never cat it. This Mac cannot fetch bb.org.bd (F5 wall) — do not try.

---

### Task 0: QFSAR fixture capture + family verification (SUPERVISED — needs owner-approved ssh)

**Files:**
- Create: `tests/_pdfs/qfsar_fixture.pdf` (copied from the box)
- Create: `tests/fixtures/qfsar_fixture_text.txt` (extracted text, for fast tests)
- Modify: THIS PLAN — record findings in the checklist below

**Interfaces:**
- Produces: the real QFSAR PDF fixture every later task's tests run against, and a verified statement of which data families the QFSAR publishes.

This task requires ssh to the box (read-only + one `scp`). **Get explicit owner approval for the ssh actions before running them.** This Mac cannot fetch bb.org.bd directly.

- [ ] **Step 1: Locate the newest QFSAR artifact on the box**

Run:
```bash
ssh exonhost 'ls -lt /home/adnan-local/econdelta/data/_pdfs/gross_npl_ratio/*/ | head -20; cat /home/adnan-local/econdelta/data/_pdfs/gross_npl_ratio/*/*.meta.json 2>/dev/null | head -40'
```
Expected: at least one month-dir containing a QFSAR PDF + `.meta.json` sidecar with a `period` field. Note the newest issue's period.

- [ ] **Step 2: Copy it into the repo as a test fixture**

```bash
scp "exonhost:/home/adnan-local/econdelta/data/_pdfs/gross_npl_ratio/<NEWEST_MONTH_DIR>/<NEWEST>.pdf" tests/_pdfs/qfsar_fixture.pdf
```

- [ ] **Step 3: Extract its text and save the text fixture**

```bash
.venv/bin/python -c "
from parsers.hybrid import _extract_pdf_text
text = _extract_pdf_text(__import__('pathlib').Path('tests/_pdfs/qfsar_fixture.pdf'), page_hint=None, indicator_id='bb_npl_structure_fixture')
open('tests/fixtures/qfsar_fixture_text.txt', 'w').write(text)
print(len(text), 'chars')
"
```
Expected: tens of thousands of chars, non-empty.

- [ ] **Step 4: Verify which families the QFSAR publishes** — search the text for each family and tick honestly:

  - [ ] Band-wise NPL rates by loan size (7 ticket bands) — present? On which page / table name?
  - [ ] Band-wise outstandings — present? Which bands?
  - [ ] Sectoral NPL / lending composition (trade, consumer, construction, agriculture) — present?
  - [ ] CMSME segment NPL (cottage / medium / overall) — present?
  - [ ] Overall gross NPL ratio (needed for the reconciliation gate) — present? (It is — `gross_npl_ratio` parses it today.)

  Also check the annual FSR listing (`https://www.bb.org.bd/en/index.php/publication/publictn/0/37` — fetch FROM THE BOX if needed) for any family QFSAR lacks.

- [ ] **Step 5: STOP if any chosen family is missing from BOTH QFSAR and FSR**

Per the spec's verification gate: report to the owner and get a decision (drop the family, or downgrade it to FSR-annual) BEFORE implementing. Amend `REQUIRED_EXTRACTION_KEYS` in Task 1 and the schema in Task 3 to match what is actually published. If everything is present, proceed unchanged.

- [ ] **Step 6: Commit the fixtures**

```bash
git add tests/_pdfs/qfsar_fixture.pdf tests/fixtures/qfsar_fixture_text.txt docs/superpowers/plans/2026-08-03-bb-npl-structure.md
git commit -m "test(npl-structure): real QFSAR fixture + family verification findings"
```

---

### Task 1: Metric inventory + definitions rows

**Files:**
- Create: `scrapers/bb_npl_structure.py`
- Test: `tests/test_bb_npl_structure_inventory.py`

**Interfaces:**
- Produces: `METRIC_SPECS: dict[str, MetricSpec]` (all 23 metric ids → label/unit/family), `REQUIRED_EXTRACTION_KEYS: frozenset[str]`, `build_definitions_rows() -> list[dict]`, `SOURCE_LABEL = "BB QFSAR"`. Later tasks import these names exactly.

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_bb_npl_structure_inventory.py"""
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_inventory_has_23_ids_with_valid_shapes():
    from scrapers.bb_npl_structure import METRIC_SPECS
    assert len(METRIC_SPECS) == 23
    for mid, spec in METRIC_SPECS.items():
        assert mid == mid.lower() and " " not in mid
        assert spec.label
        assert spec.unit in ("percent", "amount_bdt_crore")
        assert spec.family in (
            "band_rate", "band_outstanding", "sector_share", "sector_rate", "cmsme", "total"
        )


def test_no_collision_with_sources_v3_ids():
    from scrapers.bb_npl_structure import METRIC_SPECS
    cfg = json.loads((REPO_ROOT / "config" / "sources-v3.json").read_text())
    existing = {ind["id"] for ind in cfg["indicators"]}
    assert not (set(METRIC_SPECS) & existing)


def test_required_keys_are_band_rates_plus_overall():
    from scrapers.bb_npl_structure import METRIC_SPECS, REQUIRED_EXTRACTION_KEYS
    band_rates = {m for m, s in METRIC_SPECS.items() if s.family == "band_rate"}
    assert band_rates == {
        "npl_rate_band_lt1cr", "npl_rate_band_1_10cr", "npl_rate_band_10_20cr",
        "npl_rate_band_20_30cr", "npl_rate_band_30_40cr", "npl_rate_band_40_50cr",
        "npl_rate_band_gt50cr",
    }
    assert REQUIRED_EXTRACTION_KEYS == band_rates | {"overall_npl_ratio"}


def test_definitions_rows_seed_shape():
    from scrapers.bb_npl_structure import METRIC_SPECS, build_definitions_rows
    rows = build_definitions_rows()
    assert len(rows) == len(METRIC_SPECS)
    for row in rows:
        assert row["metric_id"] in METRIC_SPECS
        assert row["label"] and row["domain"] == "money_market"
        assert row["cadence"] == "quarterly"
        assert row["source"] == "BB QFSAR"
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python -m pytest tests/test_bb_npl_structure_inventory.py -v`
Expected: FAIL — `ModuleNotFoundError` / `ImportError` on `scrapers.bb_npl_structure`.

- [ ] **Step 3: Implement the inventory**

```python
"""scrapers/bb_npl_structure.py

Banking-structure NPL metrics from BB's Quarterly Financial Stability
Assessment Report (QFSAR): band-wise NPL rates/outstandings, sectoral
lending shares and NPL rates, CMSME segment rates.

Reuses the QFSAR artifact fetched daily for gross_npl_ratio
(data/_pdfs/gross_npl_ratio/). One LLM extraction pass for the whole
document; an arithmetic self-check gate rejects the WHOLE extraction on
any inconsistency (all-or-nothing — no partial writes, no ratchet shape).

These metric ids are deliberately NOT in config/sources-v3.json (they
would enter the daily fetch/parse LLM path) and must never join
briefing.config.CORE_METRIC_IDS (owner decision: non-gating).
"""
from __future__ import annotations

from dataclasses import dataclass

SOURCE_LABEL = "BB QFSAR"


@dataclass(frozen=True)
class MetricSpec:
    label: str
    unit: str      # "percent" | "amount_bdt_crore"
    family: str    # band_rate | band_outstanding | sector_share | sector_rate | cmsme | total


METRIC_SPECS: dict[str, MetricSpec] = {
    # --- band-wise NPL rates (percent) ---
    "npl_rate_band_lt1cr": MetricSpec("NPL rate — loans under Tk 1 crore", "percent", "band_rate"),
    "npl_rate_band_1_10cr": MetricSpec("NPL rate — loans Tk 1–10 crore", "percent", "band_rate"),
    "npl_rate_band_10_20cr": MetricSpec("NPL rate — loans Tk 10–20 crore", "percent", "band_rate"),
    "npl_rate_band_20_30cr": MetricSpec("NPL rate — loans Tk 20–30 crore", "percent", "band_rate"),
    "npl_rate_band_30_40cr": MetricSpec("NPL rate — loans Tk 30–40 crore", "percent", "band_rate"),
    "npl_rate_band_40_50cr": MetricSpec("NPL rate — loans Tk 40–50 crore", "percent", "band_rate"),
    "npl_rate_band_gt50cr": MetricSpec("NPL rate — loans above Tk 50 crore", "percent", "band_rate"),
    # --- band outstandings (Tk crore) ---
    "loans_outstanding_band_lt1cr": MetricSpec("Outstanding loans — under Tk 1 crore", "amount_bdt_crore", "band_outstanding"),
    "loans_outstanding_band_1_10cr": MetricSpec("Outstanding loans — Tk 1–10 crore", "amount_bdt_crore", "band_outstanding"),
    "loans_outstanding_band_gt50cr": MetricSpec("Outstanding loans — above Tk 50 crore", "amount_bdt_crore", "band_outstanding"),
    # --- sector lending shares (percent of total loans) ---
    "lending_share_trade": MetricSpec("Share of lending — trade & commerce", "percent", "sector_share"),
    "lending_share_consumer": MetricSpec("Share of lending — consumer", "percent", "sector_share"),
    "lending_share_construction": MetricSpec("Share of lending — construction", "percent", "sector_share"),
    "lending_share_agri": MetricSpec("Share of lending — agriculture, fisheries & forestry", "percent", "sector_share"),
    # --- sector NPL rates (percent) ---
    "npl_rate_consumer": MetricSpec("NPL rate — consumer loans", "percent", "sector_rate"),
    "npl_rate_trade": MetricSpec("NPL rate — trade & commerce", "percent", "sector_rate"),
    "npl_rate_construction": MetricSpec("NPL rate — construction", "percent", "sector_rate"),
    "npl_rate_agri": MetricSpec("NPL rate — agriculture, fisheries & forestry", "percent", "sector_rate"),
    # --- CMSME segments (percent) ---
    "npl_rate_cmsme_overall": MetricSpec("NPL rate — CMSME overall", "percent", "cmsme"),
    "npl_rate_cmsme_cottage": MetricSpec("NPL rate — cottage industry", "percent", "cmsme"),
    "npl_rate_cmsme_medium": MetricSpec("NPL rate — medium enterprise", "percent", "cmsme"),
    "npl_rate_industry": MetricSpec("NPL rate — industry", "percent", "cmsme"),
    # --- sector total (Tk crore) ---
    "total_bank_advances": MetricSpec("Total loans disbursed by the banking sector", "amount_bdt_crore", "total"),
}

# The extraction is rejected outright if any of these are missing/non-numeric.
# overall_npl_ratio is extracted for the reconciliation gate only — it is NOT
# a METRIC_SPECS id (gross_npl_ratio already owns that series) and is never
# written. Everything else is optional: write-if-published.
# NOTE: Task 0's family-verification findings may amend this set.
REQUIRED_EXTRACTION_KEYS: frozenset[str] = frozenset(
    m for m, s in METRIC_SPECS.items() if s.family == "band_rate"
) | {"overall_npl_ratio"}


def build_definitions_rows() -> list[dict]:
    """metric_definitions seed rows. Seeding is first-insert-wins (ON CONFLICT
    DO NOTHING) — these values must be correct on day one."""
    return [
        {
            "metric_id": mid,
            "label": spec.label,
            "domain": "money_market",
            "unit": spec.unit,
            "cadence": "quarterly",
            "source": SOURCE_LABEL,
        }
        for mid, spec in METRIC_SPECS.items()
    ]
```

- [ ] **Step 4: Run the tests, expect PASS**

Run: `.venv/bin/python -m pytest tests/test_bb_npl_structure_inventory.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add scrapers/bb_npl_structure.py tests/test_bb_npl_structure_inventory.py
git commit -m "feat(npl-structure): metric inventory + definitions rows (23 ids)"
```

---

### Task 2: Artifact location + position-date derivation

**Files:**
- Modify: `scrapers/bb_npl_structure.py`
- Test: `tests/test_bb_npl_structure_dating.py`

**Interfaces:**
- Consumes: `parse_all._load_artifact_for(indicator: dict, data_root: Path) -> FetchResult | None` (selects newest ISSUE by `.meta.json` period, not mtime — carries the E1 lesson; do NOT reimplement it); `parsers.hybrid._extract_pdf_text(pdf_path, page_hint=None, indicator_id=...) -> str`.
- Produces: `locate_latest_qfsar(data_root: Path) -> "FetchResult | None"`, `extract_pdf_text_full(pdf_path) -> str`, `derive_position_date(text: str) -> date` (raises `PositionDateError`), `class PositionDateError(ValueError)`.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_bb_npl_structure_dating.py"""
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_TEXT = REPO_ROOT / "tests" / "fixtures" / "qfsar_fixture_text.txt"


def test_derive_position_date_from_real_fixture():
    from scrapers.bb_npl_structure import derive_position_date
    d = derive_position_date(FIXTURE_TEXT.read_text())
    # Must be a quarter-end. Pin the EXACT expected date once the Task 0
    # fixture's issue is known and assert equality, not just shape.
    assert (d.month, d.day) in ((3, 31), (6, 30), (9, 30), (12, 31))


def test_latest_idiom_wins_over_stale_comparison_dates():
    from scrapers.bb_npl_structure import derive_position_date
    # Gov reports print prior-period comparison dates; the LATEST match must win.
    text = (
        "compared with the position as at end-June 2025 ... "
        "The overall position as at end-March 2026 shows ..."
    )
    assert derive_position_date(text) == date(2026, 3, 31)


def test_no_recognizable_date_raises():
    from scrapers.bb_npl_structure import PositionDateError, derive_position_date
    with pytest.raises(PositionDateError):
        derive_position_date("no dates here at all")


def test_locate_latest_qfsar_uses_gross_npl_ratio_artifact_dir(tmp_path):
    from scrapers.bb_npl_structure import locate_latest_qfsar
    # Empty data root → None (box-only artifact; absent on this Mac).
    assert locate_latest_qfsar(tmp_path) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bb_npl_structure_dating.py -v`
Expected: FAIL — `ImportError: cannot import name 'derive_position_date'`.

- [ ] **Step 3: Implement**

Append to `scrapers/bb_npl_structure.py`:

```python
import json
import re
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_SOURCES_V3 = REPO_ROOT / "config" / "sources-v3.json"
# The pipeline indicator whose daily fetch supplies our artifact.
_ARTIFACT_INDICATOR_ID = "gross_npl_ratio"

_QUARTER_END = {3: 31, 6: 30, 9: 30, 12: 31}
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}
# "as at end-March 2026", "position as at end June 2026", "end-March, 2026"
_POSITION_RE = re.compile(
    r"end[\s\-]+(" + "|".join(_MONTHS) + r")[\s,]+(\d{4})", re.IGNORECASE
)


class PositionDateError(ValueError):
    """QFSAR text carries no recognizable quarter-end position date."""


def locate_latest_qfsar(data_root: Path):
    """Newest QFSAR artifact from the gross_npl_ratio fetch dir, or None.

    Delegates issue selection to parse_all._load_artifact_for, which picks by
    the .meta.json sidecar period (immune to mtime races / filename drift).
    """
    from parse_all import _load_artifact_for

    cfg = json.loads(_SOURCES_V3.read_text())
    indicator = next(i for i in cfg["indicators"] if i["id"] == _ARTIFACT_INDICATOR_ID)
    return _load_artifact_for(indicator, data_root)


def extract_pdf_text_full(pdf_path: Path) -> str:
    from parsers.hybrid import _extract_pdf_text

    return _extract_pdf_text(pdf_path, page_hint=None, indicator_id="bb_npl_structure")


def derive_position_date(text: str) -> date:
    """Quarter-end 'position as at' date from the document's OWN text.

    Takes the LATEST date among all matches — gov PDFs print stale
    comparison-period dates alongside the current one. Non-quarter-end
    month matches (rare) are ignored.
    """
    candidates: list[date] = []
    for month_name, year in _POSITION_RE.findall(text):
        month = _MONTHS[month_name.lower()]
        day = _QUARTER_END.get(month)
        if day is not None:
            candidates.append(date(int(year), month, day))
    if not candidates:
        raise PositionDateError("no quarter-end position date found in QFSAR text")
    return max(candidates)
```

- [ ] **Step 4: Run tests, expect PASS**

Run: `.venv/bin/python -m pytest tests/test_bb_npl_structure_dating.py tests/test_bb_npl_structure_inventory.py -v`
Expected: all pass. Then tighten `test_derive_position_date_from_real_fixture` to assert the exact date of the Task 0 issue (e.g. `== date(2026, 3, 31)`) and re-run.

- [ ] **Step 5: Commit**

```bash
git add scrapers/bb_npl_structure.py tests/test_bb_npl_structure_dating.py
git commit -m "feat(npl-structure): artifact reuse + regex position-date derivation"
```

---

### Task 3: Extraction prompt + guarded LLM call

**Files:**
- Modify: `scrapers/bb_npl_structure.py`
- Test: `tests/test_bb_npl_structure_extract.py`

**Interfaces:**
- Consumes: `claude_max.max_client.run_max(*, prompt, model="claude-opus-4-8", timeout_s=1800, claude_binary=None, effort="high") -> MaxCallResult` (`.parsed` is `json.loads` of the fence-stripped reply, or `None`); `MaxCallError`.
- Produces: `build_extraction_prompt(text: str) -> str`, `run_extraction(text: str) -> dict` (raises `ExtractionError` after one retry), `class ExtractionError(RuntimeError)`, `LLM_TEXT_CAP = 120_000`.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_bb_npl_structure_extract.py"""
from unittest.mock import MagicMock, patch

import pytest


def test_prompt_names_every_extraction_key_and_demands_null_for_absent():
    from scrapers.bb_npl_structure import METRIC_SPECS, build_extraction_prompt
    prompt = build_extraction_prompt("SOME QFSAR TEXT")
    for mid in METRIC_SPECS:
        assert mid in prompt
    assert "overall_npl_ratio" in prompt
    assert "null" in prompt
    assert "SOME QFSAR TEXT" in prompt


def test_run_extraction_returns_parsed_dict():
    from scrapers.bb_npl_structure import run_extraction
    ok = MagicMock(parsed={"overall_npl_ratio": 32.7})
    with patch("scrapers.bb_npl_structure.run_max", return_value=ok) as rm:
        out = run_extraction("text")
    assert out == {"overall_npl_ratio": 32.7}
    assert rm.call_count == 1


def test_run_extraction_retries_once_on_unparsed_then_raises():
    from scrapers.bb_npl_structure import ExtractionError, run_extraction
    bad = MagicMock(parsed=None, raw_text="not json")
    with patch("scrapers.bb_npl_structure.run_max", return_value=bad) as rm:
        with pytest.raises(ExtractionError):
            run_extraction("text")
    assert rm.call_count == 2


def test_run_extraction_wraps_maxcallerror():
    from claude_max.max_client import MaxCallError
    from scrapers.bb_npl_structure import ExtractionError, run_extraction
    with patch("scrapers.bb_npl_structure.run_max", side_effect=MaxCallError("boom")):
        with pytest.raises(ExtractionError):
            run_extraction("text")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bb_npl_structure_extract.py -v`
Expected: FAIL — ImportError on `build_extraction_prompt`.

- [ ] **Step 3: Implement**

Append to `scrapers/bb_npl_structure.py`:

```python
from claude_max.max_client import MaxCallError, run_max

# Whole-document extraction needs far more context than the per-indicator
# hybrid path; QFSAR text runs ~100-250k chars. Cap defensively.
LLM_TEXT_CAP = 120_000
_EXTRACTION_MODEL = "claude-opus-4-8"
_EXTRACTION_EFFORT = "high"
_EXTRACTION_TIMEOUT_S = 900


class ExtractionError(RuntimeError):
    """LLM extraction failed twice (unparseable JSON) or the CLI call errored."""


def build_extraction_prompt(text: str) -> str:
    keys = ["overall_npl_ratio"] + list(METRIC_SPECS)
    field_lines = "\n".join(
        f'  "{k}": <number or null>,' for k in keys
    ).rstrip(",")
    return (
        "You are extracting banking-sector figures from Bangladesh Bank's "
        "Quarterly Financial Stability Assessment Report text below.\n"
        "Rules:\n"
        "- Copy numbers VERBATIM from the text. Never derive, average, or infer.\n"
        "- Percent fields: the printed percentage as a number (32.7 not 0.327).\n"
        "- Amount fields: Tk CRORE as a plain number (17.84 lakh crore = 1784000).\n"
        "- If a figure is not printed in the text, use null. Do not guess.\n"
        "- Use figures for the CURRENT reporting quarter only, not comparison periods.\n"
        "- Reply with ONLY a JSON object, no prose, exactly these keys:\n"
        "{\n" + field_lines + "\n}\n\n"
        "Field meanings:\n"
        "- overall_npl_ratio: banking-sector gross NPL ratio (%%)\n"
        + "\n".join(f"- {mid}: {spec.label}" for mid, spec in METRIC_SPECS.items())
        + "\n\nREPORT TEXT:\n" + text[:LLM_TEXT_CAP]
    )


def run_extraction(text: str) -> dict:
    prompt = build_extraction_prompt(text)
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

- [ ] **Step 4: Run tests, expect PASS**

Run: `.venv/bin/python -m pytest tests/test_bb_npl_structure_extract.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add scrapers/bb_npl_structure.py tests/test_bb_npl_structure_extract.py
git commit -m "feat(npl-structure): strict-JSON extraction prompt + retried run_max call"
```

---

### Task 4: Arithmetic self-check gate (all-or-nothing)

**Files:**
- Modify: `scrapers/bb_npl_structure.py`
- Test: `tests/test_bb_npl_structure_gate.py`

**Interfaces:**
- Produces: `validate_extraction(payload: dict, position_date: date, today: date) -> list[str]` — empty list = pass; each entry is a granular human-readable reject reason. Constants `RECON_TOLERANCE_PP = 6.0`, `RATE_RANGE = (0.0, 60.0)`, `BAND_OUT_RANGE_CR = (50_000, 3_000_000)`, `TOTAL_ADVANCES_RANGE_CR = (1_000_000, 4_000_000)`.

The gate is per-document internal consistency ONLY. It never compares against previous DB values — a reject blocks nothing next quarter, so the bb_forex ratchet shape (landmine 38) is structurally impossible here.

- [ ] **Step 1: Write the failing tests (sabotage-discrimination style)**

```python
"""tests/test_bb_npl_structure_gate.py"""
from datetime import date

TODAY = date(2026, 8, 3)
POS = date(2026, 3, 31)

# Mirrors the deck's real Mar-2026 figures (the known-good shape).
GOOD = {
    "overall_npl_ratio": 32.7,
    "npl_rate_band_lt1cr": 15.0, "npl_rate_band_1_10cr": 26.5,
    "npl_rate_band_10_20cr": 45.0, "npl_rate_band_20_30cr": 36.0,
    "npl_rate_band_30_40cr": 39.0, "npl_rate_band_40_50cr": 45.0,
    "npl_rate_band_gt50cr": 42.5,
    "loans_outstanding_band_lt1cr": 410_000,
    "loans_outstanding_band_1_10cr": 361_000,
    "loans_outstanding_band_gt50cr": 576_000,
    "lending_share_trade": 32.0, "lending_share_consumer": 9.0,
    "lending_share_construction": 7.0, "lending_share_agri": 4.0,
    "npl_rate_consumer": 7.0, "npl_rate_trade": None,
    "npl_rate_construction": None, "npl_rate_agri": None,
    "npl_rate_cmsme_overall": 34.0, "npl_rate_cmsme_cottage": 53.0,
    "npl_rate_cmsme_medium": 38.0, "npl_rate_industry": 32.0,
    "total_bank_advances": 1_784_000,
}


def _gate(payload):
    from scrapers.bb_npl_structure import validate_extraction
    return validate_extraction(payload, POS, TODAY)


def test_known_good_extraction_passes():
    assert _gate(dict(GOOD)) == []


def test_missing_required_band_rate_rejects():
    bad = dict(GOOD); bad["npl_rate_band_lt1cr"] = None
    assert any("npl_rate_band_lt1cr" in r for r in _gate(bad))


def test_wrong_column_read_fails_reconciliation():
    # The BPM6-style wrong-value class: one band rate wildly off makes the
    # weighted average irreconcilable with the document's own overall ratio.
    bad = dict(GOOD); bad["npl_rate_band_gt50cr"] = 4.25   # decimal-point slip
    assert any("reconcil" in r for r in _gate(bad))


def test_percent_as_fraction_rejects():
    bad = dict(GOOD); bad["npl_rate_band_lt1cr"] = 0.15    # 15% mis-scaled
    assert any("reconcil" in r or "range" in r for r in _gate(bad))


def test_rate_out_of_range_rejects():
    bad = dict(GOOD); bad["npl_rate_cmsme_cottage"] = 75.0
    assert any("npl_rate_cmsme_cottage" in r for r in _gate(bad))


def test_outstanding_out_of_range_rejects():
    bad = dict(GOOD); bad["loans_outstanding_band_lt1cr"] = 4.10  # lakh-crore mis-scale
    assert any("loans_outstanding_band_lt1cr" in r for r in _gate(bad))


def test_shares_over_100_reject():
    bad = dict(GOOD); bad["lending_share_trade"] = 95.0
    assert any("share" in r for r in _gate(bad))


def test_trade_not_largest_share_rejects():
    bad = dict(GOOD); bad["lending_share_consumer"] = 40.0
    assert any("trade" in r for r in _gate(bad))


def test_future_position_date_rejects():
    assert any("position" in r for r in _gate_at(date(2027, 3, 31)))


def test_ancient_position_date_rejects():
    assert any("position" in r for r in _gate_at(date(2024, 3, 31)))


def _gate_at(pos):
    from scrapers.bb_npl_structure import validate_extraction
    return validate_extraction(dict(GOOD), pos, TODAY)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bb_npl_structure_gate.py -v`
Expected: FAIL — ImportError on `validate_extraction`.

- [ ] **Step 3: Implement the gate**

Append to `scrapers/bb_npl_structure.py`:

```python
# Reconciliation tolerance between the outstanding-weighted band-rate average
# and the document's own overall ratio. Only 3 of 7 band outstandings are
# published (≈78% of the book) and the unreported mid bands run hotter, so the
# weighted average biases LOW: Mar-2026 real figures give 29.84% vs 32.7%
# (2.86pp gap). 6pp passes every legitimate shape while a single band's
# decimal-point slip (42.5 → 4.25 = 10.9pp gap) still rejects.
RECON_TOLERANCE_PP = 6.0
RATE_RANGE = (0.0, 60.0)
BAND_OUT_RANGE_CR = (50_000, 3_000_000)
TOTAL_ADVANCES_RANGE_CR = (1_000_000, 4_000_000)
_POSITION_MAX_AGE_DAYS = 400

_BAND_PAIRS = (  # (rate_key, outstanding_key) for reconciliation
    ("npl_rate_band_lt1cr", "loans_outstanding_band_lt1cr"),
    ("npl_rate_band_1_10cr", "loans_outstanding_band_1_10cr"),
    ("npl_rate_band_gt50cr", "loans_outstanding_band_gt50cr"),
)


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def validate_extraction(payload: dict, position_date: date, today: date) -> list[str]:
    """Granular reject reasons; empty list = extraction is internally consistent."""
    rejects: list[str] = []

    for key in sorted(REQUIRED_EXTRACTION_KEYS):
        if _num(payload.get(key)) is None:
            rejects.append(f"required key missing or non-numeric: {key}")
    if rejects:
        return rejects  # everything below needs the required keys

    for mid, spec in METRIC_SPECS.items():
        v = _num(payload.get(mid))
        if v is None:
            continue  # optional and unpublished — fine
        if spec.unit == "percent" and not (RATE_RANGE[0] <= v <= RATE_RANGE[1]):
            rejects.append(f"{mid} out of range {RATE_RANGE}: {v}")
        elif spec.family == "band_outstanding" and not (BAND_OUT_RANGE_CR[0] <= v <= BAND_OUT_RANGE_CR[1]):
            rejects.append(f"{mid} out of range {BAND_OUT_RANGE_CR} crore: {v}")
        elif spec.family == "total" and not (TOTAL_ADVANCES_RANGE_CR[0] <= v <= TOTAL_ADVANCES_RANGE_CR[1]):
            rejects.append(f"{mid} out of range {TOTAL_ADVANCES_RANGE_CR} crore: {v}")

    overall = _num(payload["overall_npl_ratio"])
    pairs = [
        (_num(payload[r]), _num(payload[o]))
        for r, o in _BAND_PAIRS
        if _num(payload.get(r)) is not None and _num(payload.get(o)) is not None
    ]
    if pairs:
        total_out = sum(o for _, o in pairs)
        weighted = sum(r * o for r, o in pairs) / total_out
        if abs(weighted - overall) > RECON_TOLERANCE_PP:
            rejects.append(
                "band rates fail reconciliation: weighted "
                f"{weighted:.2f}%% vs overall {overall}%% "
                f"(tolerance {RECON_TOLERANCE_PP}pp)"
            )

    shares = {
        mid: _num(payload.get(mid))
        for mid, spec in METRIC_SPECS.items()
        if spec.family == "sector_share" and _num(payload.get(mid)) is not None
    }
    if shares:
        if sum(shares.values()) > 100.01:
            rejects.append(f"sector shares sum over 100: {sum(shares.values()):.1f}")
        trade = shares.get("lending_share_trade")
        if trade is not None and trade < max(shares.values()):
            rejects.append("trade & commerce is not the largest lending share")

    if position_date > today:
        rejects.append(f"position date in the future: {position_date}")
    elif (today - position_date).days > _POSITION_MAX_AGE_DAYS:
        rejects.append(f"position date implausibly old: {position_date}")

    return rejects
```

- [ ] **Step 4: Run tests, expect PASS — then sabotage-prove the gate**

Run: `.venv/bin/python -m pytest tests/test_bb_npl_structure_gate.py -v` → all pass.
Sabotage check: temporarily replace `validate_extraction`'s body with `return []`, re-run — every mutation test MUST fail; restore, re-run (clear `__pycache__` / use `PYTHONDONTWRITEBYTECODE=1` if experimenting with reverts).

- [ ] **Step 5: Commit**

```bash
git add scrapers/bb_npl_structure.py tests/test_bb_npl_structure_gate.py
git commit -m "feat(npl-structure): all-or-nothing arithmetic self-check gate"
```

---

### Task 5: Skip logic, upsert, main(), wrap_run

**Files:**
- Modify: `scrapers/bb_npl_structure.py`
- Test: `tests/test_bb_npl_structure_main.py`

**Interfaces:**
- Consumes: `utils.supabase_writer.upsert_metric_history(*, data, as_of, source, source_as_of_map=None, ingested_at=None) -> int`, `upsert_metric_definitions_seed(list[dict]) -> int`, `verify_landed_count(expected, *, since, metric_ids, source_label)`, `wrap_run(source, unit, main_func)`, `SupabaseWriteError`; `utils.supabase_reader.get_metric_history(metric_id, *, days) -> list[dict]`, `SupabaseReadError`; `utils.notifier.notify(level, title, detail)`.
- Produces: `already_captured(position_date: date) -> bool`, `upsert_extraction(payload: dict, position_date: date) -> int`, `main() -> int` (0 ok / 1 fail / 3 skip).

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_bb_npl_structure_main.py"""
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import scrapers.bb_npl_structure as mod
from tests.test_bb_npl_structure_gate import GOOD

POS = date(2026, 3, 31)


def test_already_captured_true_when_db_at_or_past_position():
    with patch.object(mod, "get_metric_history", return_value=[{"as_of": "2026-03-31"}]):
        assert mod.already_captured(POS) is True


def test_already_captured_false_on_empty_or_read_error():
    from utils.supabase_reader import SupabaseReadError
    with patch.object(mod, "get_metric_history", return_value=[]):
        assert mod.already_captured(POS) is False
    with patch.object(mod, "get_metric_history", side_effect=SupabaseReadError("down")):
        assert mod.already_captured(POS) is False  # fail-open: idempotent upsert makes a re-run harmless


def test_upsert_extraction_writes_only_published_metrics_no_overall():
    with patch.object(mod, "upsert_metric_history", return_value=20) as up, \
         patch.object(mod, "verify_landed_count"):
        mod.upsert_extraction(dict(GOOD), POS)
    kwargs = up.call_args.kwargs
    assert "overall_npl_ratio" not in kwargs["data"]          # check-only, never stored
    assert "npl_rate_trade" not in kwargs["data"]             # null in GOOD → not written
    assert kwargs["data"]["npl_rate_band_lt1cr"] == 15.0
    assert kwargs["as_of"] == POS
    assert kwargs["source"] == "BB QFSAR"
    assert "url" not in kwargs                                 # landmine 22


def test_main_skips_when_position_already_captured(tmp_path):
    art = MagicMock(artifact_path=tmp_path / "q.pdf", artifact_type="pdf")
    with patch.object(mod, "locate_latest_qfsar", return_value=art), \
         patch.object(mod, "extract_pdf_text_full", return_value="as at end-March 2026"), \
         patch.object(mod, "already_captured", return_value=True), \
         patch.object(mod, "run_extraction") as rex:
        assert mod.main() == 3
    rex.assert_not_called()  # the LLM is never invoked for an already-captured issue


def test_main_rejects_write_nothing_and_notify_on_gate_failure(tmp_path):
    art = MagicMock(artifact_path=tmp_path / "q.pdf", artifact_type="pdf")
    bad = dict(GOOD); bad["npl_rate_band_gt50cr"] = 4.25
    with patch.object(mod, "locate_latest_qfsar", return_value=art), \
         patch.object(mod, "extract_pdf_text_full", return_value="as at end-March 2026"), \
         patch.object(mod, "already_captured", return_value=False), \
         patch.object(mod, "run_extraction", return_value=bad), \
         patch.object(mod, "upsert_metric_history") as up, \
         patch.object(mod, "notify") as noti:
        assert mod.main() == 1
    up.assert_not_called()
    assert noti.call_args.args[0] == "error"
    assert "reconcil" in noti.call_args.args[2]


def test_main_happy_path_writes_and_seeds_definitions(tmp_path):
    art = MagicMock(artifact_path=tmp_path / "q.pdf", artifact_type="pdf")
    with patch.object(mod, "locate_latest_qfsar", return_value=art), \
         patch.object(mod, "extract_pdf_text_full", return_value="as at end-March 2026"), \
         patch.object(mod, "already_captured", return_value=False), \
         patch.object(mod, "run_extraction", return_value=dict(GOOD)), \
         patch.object(mod, "upsert_metric_definitions_seed", return_value=0) as seed, \
         patch.object(mod, "upsert_metric_history", return_value=20) as up, \
         patch.object(mod, "verify_landed_count"):
        assert mod.main() == 0
    seed.assert_called_once()
    up.assert_called_once()


def test_main_fails_loud_when_artifact_missing():
    with patch.object(mod, "locate_latest_qfsar", return_value=None), \
         patch.object(mod, "notify") as noti:
        assert mod.main() == 1
    assert noti.call_args.args[0] == "error"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bb_npl_structure_main.py -v`
Expected: FAIL — AttributeError/ImportError on the new names.

- [ ] **Step 3: Implement**

Append to `scrapers/bb_npl_structure.py`:

```python
import logging
import sys
from datetime import datetime, timezone

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
_BELLWETHER_ID = "npl_rate_band_lt1cr"  # newest as_of here == newest capture


def already_captured(position_date: date) -> bool:
    """True if this issue's position date is already in the DB.

    Fail-open on read errors: a duplicate run costs one LLM call and an
    idempotent merge-upsert; a false 'captured' would silently drop an issue.
    """
    try:
        rows = get_metric_history(_BELLWETHER_ID, days=1)
    except SupabaseReadError as e:
        logger.warning("capture check failed (%s) — proceeding", e)
        return False
    if not rows:
        return False
    return date.fromisoformat(rows[0]["as_of"]) >= position_date


def upsert_extraction(payload: dict, position_date: date) -> int:
    data = {
        mid: _num(payload.get(mid))
        for mid in METRIC_SPECS
        if _num(payload.get(mid)) is not None
    }
    write_ts = datetime.now(timezone.utc)
    count = upsert_metric_history(
        data=data,
        as_of=position_date,
        source=SOURCE_LABEL,
        ingested_at=write_ts,
    )
    verify_landed_count(
        count, since=write_ts, metric_ids=list(data), source_label="bb_npl_structure"
    )
    return count


def main() -> int:
    artifact = locate_latest_qfsar(_DATA_ROOT)
    if artifact is None:
        notify("error", "bb_npl_structure: no QFSAR artifact",
               "data/_pdfs/gross_npl_ratio has no usable issue — check fetch stage")
        return 1
    try:
        text = extract_pdf_text_full(artifact.artifact_path)
        position_date = derive_position_date(text)
    except (PositionDateError, Exception) as e:
        if not isinstance(e, PositionDateError):
            logger.exception("pdf text extraction failed")
        notify("error", "bb_npl_structure: cannot date the QFSAR", str(e))
        return 1

    if already_captured(position_date):
        logger.info("issue %s already captured — skip", position_date)
        return 3

    try:
        payload = run_extraction(text)
    except ExtractionError as e:
        notify("error", "bb_npl_structure: extraction failed", str(e))
        return 1

    rejects = validate_extraction(payload, position_date, datetime.now(timezone.utc).date())
    if rejects:
        notify("error",
               f"bb_npl_structure: gate rejected extraction for {position_date} — ZERO rows written",
               "\n".join(rejects))
        return 1

    try:
        upsert_metric_definitions_seed(build_definitions_rows())  # first-insert-wins, no-op after day one
        count = upsert_extraction(payload, position_date)
    except SupabaseWriteError as e:
        notify("error", "bb_npl_structure: Supabase write failed", str(e))
        return 1
    logger.info("captured QFSAR %s: %d metrics", position_date, count)
    return 0


if __name__ == "__main__":
    from utils.supabase_writer import wrap_run

    sys.exit(wrap_run("bb_npl_structure", "econdelta-npl-structure.service", main))
```

Note: fix the `except (PositionDateError, Exception)` tuple — write it as two separate excepts (`except PositionDateError as e:` then `except Exception as e:` with `logger.exception`), both notifying and returning 1. Ruff will flag the redundant tuple.

- [ ] **Step 4: Run tests, expect PASS**

Run: `.venv/bin/python -m pytest tests/test_bb_npl_structure_main.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add scrapers/bb_npl_structure.py tests/test_bb_npl_structure_main.py
git commit -m "feat(npl-structure): skip/upsert/main wiring with wrap_run"
```

---

### Task 6: Sentinel cadence + catalog + gating-protection tests

**Files:**
- Modify: `sentinel/cadence.py` (the `_SCRAPER_CADENCE` dict, lines ~40-95)
- Modify: `scripts/build_catalog.py` (the `DERIVED_KEYS` list, line ~45)
- Regenerate: `docs/indicator-catalog.md`
- Test: `tests/test_bb_npl_structure_wiring.py`

**Interfaces:**
- Consumes: `sentinel.cadence.resolve_cadence(metric_id, cadence_map, from_monthly_table=False)` via `load_cadence_map()`; `briefing.config.CORE_METRIC_IDS`; `scrapers.bb_npl_structure.METRIC_SPECS`.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_bb_npl_structure_wiring.py"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_every_metric_resolves_quarterly_in_sentinel():
    from scrapers.bb_npl_structure import METRIC_SPECS
    from sentinel.cadence import load_cadence_map, resolve_cadence
    cmap = load_cadence_map()
    for mid in METRIC_SPECS:
        assert resolve_cadence(mid, cmap) == "quarterly", mid


def test_no_metric_ever_gates_the_briefing():
    # Owner decision: non-gating. This test is the enforcement.
    from briefing.config import CORE_METRIC_IDS
    from scrapers.bb_npl_structure import METRIC_SPECS
    assert not (set(METRIC_SPECS) & CORE_METRIC_IDS)


def test_no_metric_in_sources_v3():
    # These ids must never enter the daily fetch/parse pipeline.
    import json
    from scrapers.bb_npl_structure import METRIC_SPECS
    cfg = json.loads((REPO_ROOT / "config" / "sources-v3.json").read_text())
    assert not (set(METRIC_SPECS) & {i["id"] for i in cfg["indicators"]})


def test_catalog_lists_every_metric():
    from scrapers.bb_npl_structure import METRIC_SPECS
    catalog = (REPO_ROOT / "docs" / "indicator-catalog.md").read_text()
    for mid in METRIC_SPECS:
        assert f"`{mid}`" in catalog, mid
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bb_npl_structure_wiring.py -v`
Expected: `test_every_metric_resolves_quarterly_in_sentinel` fails for the non-`npl_`-prefixed ids (no cadence source), and `test_catalog_lists_every_metric` fails for all. The two negative tests pass from day one — they are regression guards.

- [ ] **Step 3: Add the ids to `_SCRAPER_CADENCE` in `sentinel/cadence.py`**

Append inside the existing `_SCRAPER_CADENCE` dict (match its existing comment style; all 23 listed explicitly — explicit beats the `npl_*` prefix rule):

```python
    # bb_npl_structure (QFSAR banking-structure family, PR #<this>): quarterly.
    "npl_rate_band_lt1cr": "quarterly",
    "npl_rate_band_1_10cr": "quarterly",
    "npl_rate_band_10_20cr": "quarterly",
    "npl_rate_band_20_30cr": "quarterly",
    "npl_rate_band_30_40cr": "quarterly",
    "npl_rate_band_40_50cr": "quarterly",
    "npl_rate_band_gt50cr": "quarterly",
    "loans_outstanding_band_lt1cr": "quarterly",
    "loans_outstanding_band_1_10cr": "quarterly",
    "loans_outstanding_band_gt50cr": "quarterly",
    "lending_share_trade": "quarterly",
    "lending_share_consumer": "quarterly",
    "lending_share_construction": "quarterly",
    "lending_share_agri": "quarterly",
    "npl_rate_consumer": "quarterly",
    "npl_rate_trade": "quarterly",
    "npl_rate_construction": "quarterly",
    "npl_rate_agri": "quarterly",
    "npl_rate_cmsme_overall": "quarterly",
    "npl_rate_cmsme_cottage": "quarterly",
    "npl_rate_cmsme_medium": "quarterly",
    "npl_rate_industry": "quarterly",
    "total_bank_advances": "quarterly",
```

Sentinel grace comes from the existing `GRACE_DAYS_BY_CADENCE["quarterly"] = 165` — no change needed (matches the spec's honest-window requirement).

- [ ] **Step 4: Add catalog entries and regenerate**

In `scripts/build_catalog.py`, append one `DERIVED_KEYS` tuple per metric, following the existing `(metric_id, unit, cadence, description)` shape:

```python
    # bb_npl_structure — QFSAR banking-structure family (quarterly, LLM-extracted, gate-checked)
    ("npl_rate_band_lt1cr", "percent", "quarterly",
     "NPL rate for loans under Tk 1 crore — from BB QFSAR via scrapers/bb_npl_structure.py."),
    ("npl_rate_band_1_10cr", "percent", "quarterly",
     "NPL rate for loans of Tk 1-10 crore — from BB QFSAR via scrapers/bb_npl_structure.py."),
    # ... one tuple for each remaining METRIC_SPECS id, unit "percent" except
    # loans_outstanding_band_* and total_bank_advances which use "amount_bdt_crore",
    # description = the MetricSpec label + "— from BB QFSAR via scrapers/bb_npl_structure.py."
```

Write all 23 tuples out fully in the file (no ellipsis in real code). Then regenerate:

```bash
.venv/bin/python scripts/build_catalog.py > docs/indicator-catalog.md
git diff --stat docs/indicator-catalog.md
```
Expected: only additions in the derived (cross-source) section.

- [ ] **Step 5: Run tests, expect PASS**

Run: `.venv/bin/python -m pytest tests/test_bb_npl_structure_wiring.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add sentinel/cadence.py scripts/build_catalog.py docs/indicator-catalog.md tests/test_bb_npl_structure_wiring.py
git commit -m "feat(npl-structure): sentinel quarterly cadence + catalog entries + gating-protection tests"
```

---

### Task 7: Static seeder (deck's Mar-2026 primitives, press provenance)

**Files:**
- Create: `scripts/seed_npl_structure.py`
- Test: `tests/test_seed_npl_structure.py`

**Interfaces:**
- Consumes: `scrapers.bb_npl_structure.METRIC_SPECS`, `build_definitions_rows`; `utils.supabase_writer.upsert_metric_history`, `upsert_metric_definitions_seed`.
- Produces: `SEED_VALUES: dict[str, float]`, `SEED_AS_OF = date(2026, 3, 31)`, `SEED_SOURCE = "bb_via_press_static"`, `build_history_data() -> dict[str, float]`, CLI with `--execute` (default is dry-run — a deliberate tightening of the `backfill_fiscal.py` precedent for an owner-gated one-shot).

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_seed_npl_structure.py"""
from datetime import date
from unittest.mock import patch


def test_seed_values_are_the_deck_primitives_exactly():
    from scripts.seed_npl_structure import SEED_AS_OF, SEED_SOURCE, SEED_VALUES
    assert SEED_AS_OF == date(2026, 3, 31)
    assert SEED_SOURCE == "bb_via_press_static"
    assert len(SEED_VALUES) == 19
    assert SEED_VALUES["npl_rate_band_lt1cr"] == 15.0
    assert SEED_VALUES["npl_rate_band_gt50cr"] == 42.5
    assert SEED_VALUES["loans_outstanding_band_lt1cr"] == 410_000
    assert SEED_VALUES["total_bank_advances"] == 1_784_000
    assert SEED_VALUES["npl_rate_cmsme_cottage"] == 53.0
    # Deliberately absent: derived figures, vague agri share, overall ratio.
    for absent in ("overall_npl_ratio", "lending_share_agri",
                   "npl_rate_trade", "npl_rate_construction", "npl_rate_agri"):
        assert absent not in SEED_VALUES


def test_every_seed_id_is_a_known_metric():
    from scrapers.bb_npl_structure import METRIC_SPECS
    from scripts.seed_npl_structure import SEED_VALUES
    assert set(SEED_VALUES) <= set(METRIC_SPECS)


def test_dry_run_writes_nothing():
    import scripts.seed_npl_structure as seeder
    with patch.object(seeder, "upsert_metric_history") as up, \
         patch.object(seeder, "upsert_metric_definitions_seed") as seed:
        seeder.run(execute=False)
    up.assert_not_called()
    seed.assert_not_called()


def test_execute_seeds_definitions_then_history():
    import scripts.seed_npl_structure as seeder
    with patch.object(seeder, "upsert_metric_definitions_seed", return_value=23) as seed, \
         patch.object(seeder, "upsert_metric_history", return_value=19) as up:
        seeder.run(execute=True)
    seed.assert_called_once()
    kwargs = up.call_args.kwargs
    assert kwargs["source"] == "bb_via_press_static"
    assert kwargs["as_of"] == date(2026, 3, 31)
    assert "url" not in kwargs
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_seed_npl_structure.py -v`
Expected: FAIL — no module `scripts.seed_npl_structure`.

- [ ] **Step 3: Implement the seeder**

```python
"""scripts/seed_npl_structure.py

One-shot static seed of the Mar-2026 banking-structure NPL figures.

Source: Bangladesh Bank data as reported by Prothom Alo, 1 August 2026
(position as at end-March 2026), hand-transcribed from the owner's deck
"Small Loans Big Numbers" and cross-checked against the article's own
table. Provenance label "bb_via_press_static" marks every row as
press-derived (precedent: mof_mfr_static in scripts/backfill_fiscal.py).

Values deliberately EXCLUDED: derived figures (implied NPL stock, implied
impaired values, average exposures — downstream arithmetic, never stored);
the agriculture lending share (reported only as "just over 4%" — too vague);
the overall NPL ratio (owned by the gross_npl_ratio series); defaulter
counts (out of scope per the 2026-08-03 spec).

DRY-RUN BY DEFAULT. Writes require --execute plus live Supabase creds,
and per house rules an owner sign-off + before/after SELECT proofs.

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

# Units: percents as printed; amounts converted lakh crore → crore
# (Tk 4.10 lakh crore = 410,000 crore).
SEED_VALUES: dict[str, float] = {
    "npl_rate_band_lt1cr": 15.0,
    "npl_rate_band_1_10cr": 26.5,
    "npl_rate_band_10_20cr": 45.0,
    "npl_rate_band_20_30cr": 36.0,
    "npl_rate_band_30_40cr": 39.0,
    "npl_rate_band_40_50cr": 45.0,
    "npl_rate_band_gt50cr": 42.5,
    "loans_outstanding_band_lt1cr": 410_000,
    "loans_outstanding_band_1_10cr": 361_000,
    "loans_outstanding_band_gt50cr": 576_000,
    "lending_share_trade": 32.0,
    "lending_share_consumer": 9.0,
    "lending_share_construction": 7.0,
    "npl_rate_consumer": 7.0,
    "npl_rate_cmsme_overall": 34.0,
    "npl_rate_cmsme_cottage": 53.0,
    "npl_rate_cmsme_medium": 38.0,
    "npl_rate_industry": 32.0,
    "total_bank_advances": 1_784_000,
}


def build_history_data() -> dict[str, float]:
    unknown = set(SEED_VALUES) - set(METRIC_SPECS)
    if unknown:
        raise ValueError(f"seed ids not in METRIC_SPECS: {sorted(unknown)}")
    return dict(SEED_VALUES)


def run(*, execute: bool) -> int:
    data = build_history_data()
    if not execute:
        for mid, value in sorted(data.items()):
            logger.info("DRY RUN  %-34s %s  as_of=%s source=%s",
                        mid, value, SEED_AS_OF, SEED_SOURCE)
        logger.info("DRY RUN — %d rows, nothing written. Re-run with --execute.", len(data))
        return 0
    new_defs = upsert_metric_definitions_seed(build_definitions_rows())
    count = upsert_metric_history(
        data=data,
        as_of=SEED_AS_OF,
        source=SEED_SOURCE,
        ingested_at=datetime.now(timezone.utc),
    )
    logger.info("seeded %d history rows (+%d new definitions)", count, new_defs)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true",
                        help="actually write (default: dry run)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    return run(execute=args.execute)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests, expect PASS; also run the dry-run for real**

Run: `.venv/bin/python -m pytest tests/test_seed_npl_structure.py -v` → 4 passed.
Run: `.venv/bin/python -m scripts.seed_npl_structure` → 19 DRY RUN lines, exit 0, nothing written.

- [ ] **Step 5: Commit**

```bash
git add scripts/seed_npl_structure.py tests/test_seed_npl_structure.py
git commit -m "feat(npl-structure): press-provenance static seeder (dry-run default)"
```

---

### Task 8: systemd units, install wiring, docs

**Files:**
- Create: `deploy/econdelta-npl-structure.service`
- Create: `deploy/econdelta-npl-structure.timer`
- Modify: `deploy/install.sh` (the `TIMERS=()` array — landmine 19)
- Modify: `docs/data-contract.md` (new-indicator entry + static-seed provenance note)
- Modify: `AGENTS.md` (one new landmine)

- [ ] **Step 1: Write the service unit** (mirrors `deploy/econdelta-fiscal-gdp.service` exactly; only names/paths differ)

```ini
[Unit]
Description=EconDelta — QFSAR banking-structure NPL extractor (band/sector/CMSME)
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
ReadWritePaths=/home/adnan-local/econdelta/data /home/adnan-local/econdelta/logs

TimeoutStartSec=1200
Restart=on-failure
RestartSec=300

StandardOutput=append:/home/adnan-local/econdelta/logs/bb_npl_structure-systemd.log
StandardError=append:/home/adnan-local/econdelta/logs/bb_npl_structure-systemd.log
SyslogIdentifier=econdelta-npl-structure

[Install]
WantedBy=multi-user.target
```

(`TimeoutStartSec=1200`, not 120: the happy path includes a whole-document Opus extraction that can run several minutes. The claude CLI must be reachable for `User=adnan-local` — same runtime environment the parse service already uses; if parse needed `ReadWritePaths=/home/adnan-local/.claude.json` drop-ins, mirror them here at deploy time.)

- [ ] **Step 2: Write the timer** (weekly-poll idiom — the scraper itself skips already-captured issues with exit 3)

```ini
[Unit]
Description=Run EconDelta QFSAR NPL-structure extractor weekly (Sun 23:29 UTC / Mon 05:29 BDT). QFSAR is quarterly; weekly polling + in-scraper skip keeps capture prompt without wasted LLM calls.
Requires=econdelta-npl-structure.service

[Timer]
OnCalendar=Sun *-*-* 23:29:00 UTC
Persistent=true
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
```

(Minute slot `:29` — `:20`/`:23`/`:26` are taken by imf-eff/imf-debt/fiscal-gdp.)

- [ ] **Step 3: Add the timer to `install.sh`'s `TIMERS=()` array** (landmine 19 — copied-but-never-enabled otherwise). One line, matching the array's existing style: `econdelta-npl-structure.timer`.

- [ ] **Step 4: Document.**
In `docs/data-contract.md`: follow §8's "Adding a new indicator (non-breaking)" convention with a short entry for the bb_npl_structure family (23 ids, quarterly, source `BB QFSAR`, extractor path), and add one paragraph to the provenance semantics (§3) documenting the static-seed labels: `bb_via_press_static` (this build) alongside the existing `mof_mfr_static` convention — the recon found this convention was previously undocumented.
In `AGENTS.md`: add the next-numbered landmine:

```
NN. **bb_npl_structure ids live OUTSIDE the pipeline config.** The 23 QFSAR
    banking-structure metrics are written by scrapers/bb_npl_structure.py,
    which REUSES the gross_npl_ratio artifact. Never add these ids to
    config/sources-v3.json (each would become a daily LLM parse) and never
    to briefing CORE_METRIC_IDS (owner decision: non-gating).
    tests/test_bb_npl_structure_wiring.py enforces both.
```

- [ ] **Step 5: Run the FULL gate**

Run: `.venv/bin/python -m pytest -q` then `.venv/bin/ruff check .`
Expected: both exit 0; test count = pre-plan baseline + all new tests, 0 failures.

- [ ] **Step 6: Commit**

```bash
git add deploy/econdelta-npl-structure.service deploy/econdelta-npl-structure.timer deploy/install.sh docs/data-contract.md AGENTS.md
git commit -m "feat(npl-structure): systemd units, install wiring, contract + landmine docs"
```

---

### Task 9: PR, deploy, supervised seed (deploy steps are OWNER-GATED)

**Files:** none new — this is the ship-and-operate task.

- [ ] **Step 1: Full gate one final time** (bare, exit codes cited): `.venv/bin/python -m pytest -q` and `.venv/bin/ruff check .`.

- [ ] **Step 2: Push branch + open PR** (use /ship conventions; never push main):

```bash
git push -u origin feat/bb-npl-structure
gh pr create --title "feat: QFSAR banking-structure NPL tracking (band/sector/CMSME) + press seed" --body "<summary per house PR format; cite spec + plan paths, test counts, gate evidence>"
gh pr checks --watch
```

- [ ] **Step 3: Merge only with owner approval** (per-action approval, house rule).

- [ ] **Step 4 (POST-MERGE, OWNER-GATED — enumerate and get ONE approval for the batch):**

  1. Box pull: the healed gitpull timer picks up main on its next run (or `ssh exonhost 'cd /home/adnan-local/econdelta && git pull --ff-only'`).
  2. Targeted unit install (landmine 37 — NEVER full install.sh):
     ```bash
     ssh exonhost 'sudo install -m 0644 /home/adnan-local/econdelta/deploy/econdelta-npl-structure.service /home/adnan-local/econdelta/deploy/econdelta-npl-structure.timer /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now econdelta-npl-structure.timer'
     ```
  3. Supervised seed with proofs: before-SELECT (anon, expect 0 rows for the 23 ids) → `ssh exonhost 'cd /home/adnan-local/econdelta && set -a && source /etc/econdelta.env && set +a && .venv/bin/python -m scripts.seed_npl_structure --execute'` → after-SELECT (expect 19 rows at as_of 2026-03-31, source `bb_via_press_static`). Print status codes only, never env contents.
  4. First live run: `ssh exonhost 'sudo systemctl start econdelta-npl-structure.service'`, then check run_logs — expected `status=skip` (exit 3) if the newest QFSAR is the Mar-2026 issue the seed just covered, or `status=ok` with fresh rows if BB has published a newer issue.
  5. Sentinel check next sentinel run: all 23 ids classified quarterly/fresh, zero new breaches.

- [ ] **Step 5: Update memory + session note** per house practice (auto-memory entry: what shipped, watch items — e.g. first genuine capture expected when BB publishes the Jun-2026 QFSAR).

---

## Self-Review (run after writing — issues found and fixed inline)

1. **Spec coverage:** all five owner decisions have tasks (capacity → Tasks 1-6; BB-only source → artifact reuse in Task 2; three families → METRIC_SPECS; non-gating+watched → Task 6 tests + `_SCRAPER_CADENCE`; seed → Task 7). Verification gate = Task 0 with an explicit STOP. Same-document reconciliation (the spec's self-review fix) is in Task 4. ✓
2. **Placeholders:** Task 6 Step 4 contains one deliberate "write all 23 tuples out fully" instruction with 2 worked examples — the pattern is fully specified (unit/cadence/description rules given), not a TBD. ✓
3. **Type consistency:** `validate_extraction(payload, position_date, today)` matches between Tasks 4 and 5; `run(execute=...)` matches seeder tests; `METRIC_SPECS`/`REQUIRED_EXTRACTION_KEYS`/`SOURCE_LABEL` names consistent across Tasks 1-7. ✓
