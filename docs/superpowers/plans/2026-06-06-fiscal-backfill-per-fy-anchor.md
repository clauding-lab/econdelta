# Per-FY-Anchor Fiscal Backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the MoF Monthly Fiscal Report (MFR) backfill from a hardcoded FY26 budget anchor to per-fiscal-year anchors, gated by the FYTD self-check, so FY25/FY24/older reports parse and `govt_bank_borrow_monthly_cr` + `nbr_revenue_monthly_cr` gain ~24+ months of depth.

**Architecture:** Keep the proven value-anchor extraction in `scripts/mfr_parser.py`. In `scripts/backfill_fiscal.py`, swap the two FY26 constants for per-FY anchor dicts, select the anchor by the report's own fiscal year, and turn the FYTD self-check into a hard gate that drops (logs) any month whose single-month figure can't be reconciled against its FYTD difference. Code lands first (TDD, CI-deterministic synthetic-PDF tests); the real-data backfill is an operational dry-run → VPS write at the end.

**Tech Stack:** Python 3, pdfplumber (parse), reportlab (synthetic test PDFs), pytest, ruff. Target table `metric_history_monthly` (upsert on `metric_id,as_of`).

**Spec:** `docs/superpowers/specs/2026-06-06-fiscal-backfill-per-fy-anchor-design.md`
**Branch:** `feat/fiscal-backfill-per-fy-anchor`

**Verify gate (run from repo root `~/Projects/clauding-lab/econdelta`):**
```
.venv/bin/ruff check . && .venv/bin/python -m pytest tests/test_backfill_fiscal.py -q
```

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `scripts/mfr_parser.py` | Value-anchor PDF extraction | Rename `FiscalRow.fy26_budget`→`fy_budget` (provenance only). No algorithm change. |
| `scripts/backfill_fiscal.py` | Orchestration: discover→parse→self-check→build rows→upsert | Per-FY anchor dicts; `fiscal_year_of`; FY-aware `parse_one_mfr`; `self_check_failures` helper; gated `build_history_rows`; main() drops failing months. |
| `tests/test_backfill_fiscal.py` | Unit + real-PDF tests | Update FY26-constant refs to dict; add `fiscal_year_of`, per-FY anchor (synthetic PDFs), `self_check_failures`, gated-row-build tests. |

**Empirically-sourced anchors (BDT crore, from 2026-06-06 token-dump experiment; each cross-checked across ≥2 fiscal years' reports):**

| FY-end | BORROW (Table 6) | NBR (Table 4) |
|---|---|---|
| 2026 | 104000 | 499001 |
| 2025 | 137500 | 480000 |
| 2024 | 132395 | 430000 |

(FY23↓ anchors are sourced + confirmed during the operational dry-run, Task 8.)

---

## Task 1: `fiscal_year_of` helper

**Files:**
- Modify: `scripts/backfill_fiscal.py` (add after `fiscal_year_start`, ~line 101)
- Test: `tests/test_backfill_fiscal.py`

- [ ] **Step 1: Write the failing test** — add this class after `class TestFiscalYearStart` (~line 84):

```python
class TestFiscalYearOf:
    def test_july_is_first_month_of_next_fy(self):
        assert bf.fiscal_year_of(2025, 7) == 2026

    def test_december_is_same_fy_as_following_june(self):
        assert bf.fiscal_year_of(2024, 12) == 2025

    def test_june_is_last_month_of_its_fy(self):
        assert bf.fiscal_year_of(2025, 6) == 2025

    def test_october_2025_is_fy26(self):
        assert bf.fiscal_year_of(2025, 10) == 2026
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_backfill_fiscal.py::TestFiscalYearOf -q`
Expected: FAIL — `AttributeError: module 'scripts.backfill_fiscal' has no attribute 'fiscal_year_of'`

- [ ] **Step 3: Write minimal implementation** — add after `fiscal_year_start` (the function ending at ~line 101):

```python
def fiscal_year_of(year: int, month: int) -> int:
    """Return the FY-END year for a report month. Bangladesh fiscal year runs
    1 July -> 30 June, named by its end year (Jul 2025..Jun 2026 = FY26)."""
    return year + 1 if month >= 7 else year
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_backfill_fiscal.py::TestFiscalYearOf -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add scripts/backfill_fiscal.py tests/test_backfill_fiscal.py
git commit -m "feat(fiscal): add fiscal_year_of helper (FY-end year from report month)"
```

---

## Task 2: Per-FY anchor dicts (replace FY26 constants)

**Files:**
- Modify: `scripts/backfill_fiscal.py:56-60` (the FY26 constants)
- Modify: `tests/test_backfill_fiscal.py` (lines 169, 175, 182, 183 — constant refs)
- Test: `tests/test_backfill_fiscal.py`

- [ ] **Step 1: Write the failing test** — add after `class TestFiscalYearOf`:

```python
class TestFyAnchorTables:
    def test_fy26_anchors_match_known_values(self):
        assert bf.FY_BORROW_BUDGET[2026] == 104000.0
        assert bf.FY_NBR_BUDGET[2026] == 499001.0

    def test_fy25_and_fy24_anchors_present(self):
        assert bf.FY_BORROW_BUDGET[2025] == 137500.0
        assert bf.FY_NBR_BUDGET[2025] == 480000.0
        assert bf.FY_BORROW_BUDGET[2024] == 132395.0
        assert bf.FY_NBR_BUDGET[2024] == 430000.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_backfill_fiscal.py::TestFyAnchorTables -q`
Expected: FAIL — `AttributeError: ... has no attribute 'FY_BORROW_BUDGET'`

- [ ] **Step 3: Write minimal implementation** — replace `scripts/backfill_fiscal.py:56-60` (the block from `# FY26 annual-budget anchors` through `FY26_NBR_BUDGET_CRORE = 499001.0`) with:

```python
# Per-fiscal-year annual-budget anchors (BDT crore), keyed by FY-END year.
# The MFR table layout is not fixed-position, so the parser locates the
# current fiscal year's annual *Budget* value (stable across all 12 monthly
# issues of that FY) and reads the single-month + FYTD figures right after it.
# Each value is read straight off the printed table and CROSS-CHECKED across
# two fiscal years' reports (every report prints both its own year's Budget
# and the prior year's). Verified 2026-06-06.
#   FY26: reproduces the live DB (borrow 5,720 / NBR 28,027 for Oct 2025).
#   FY25/FY24: confirmed in both the year's own reports and the next year's
#   prior-year column. FY23 and older are added during the dry-run (Task 8),
#   each confirmed the same way; unconfirmable years are skipped, not guessed.
FY_BORROW_BUDGET: dict[int, float] = {
    2026: 104000.0,   # Table 6 "Borrowing from Banking System (Net)" Budget FY26
    2025: 137500.0,   # Budget FY25
    2024: 132395.0,   # Budget FY24
}
FY_NBR_BUDGET: dict[int, float] = {
    2026: 499001.0,   # Table 4 "a. NBR" Budget FY26
    2025: 480000.0,   # Budget FY25
    2024: 430000.0,   # Budget FY24
}
```

- [ ] **Step 4: Update existing real-PDF test references** — in `tests/test_backfill_fiscal.py`, replace every `bf.FY26_BORROW_BUDGET_CRORE` with `bf.FY_BORROW_BUDGET[2026]` and every `bf.FY26_NBR_BUDGET_CRORE` with `bf.FY_NBR_BUDGET[2026]` (lines 169, 175, 182, 183).

```bash
sed -i '' 's/bf\.FY26_BORROW_BUDGET_CRORE/bf.FY_BORROW_BUDGET[2026]/g; s/bf\.FY26_NBR_BUDGET_CRORE/bf.FY_NBR_BUDGET[2026]/g' tests/test_backfill_fiscal.py
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_backfill_fiscal.py -q`
Expected: PASS (real-PDF tests may report `skipped` if fixture PDFs absent — that is fine; no failures).

- [ ] **Step 6: Commit**

```bash
git add scripts/backfill_fiscal.py tests/test_backfill_fiscal.py
git commit -m "feat(fiscal): replace FY26 budget constants with per-FY anchor dicts"
```

---

## Task 3: Rename `FiscalRow.fy26_budget` → `fy_budget`

**Files:**
- Modify: `scripts/mfr_parser.py:74-81` (dataclass field + comment)
- Test: `tests/test_backfill_fiscal.py` (new synthetic-PDF helper + assertion)

- [ ] **Step 1: Add the synthetic-PDF test helper + failing test** — append to `tests/test_backfill_fiscal.py`. This helper (reportlab `Preformatted`, mirroring `tests/test_pdf_mfr_row.py`) builds a deterministic MFR page the parser can read, and is reused in Task 4.

```python
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Preformatted, SimpleDocTemplate


def _make_mfr_pdf(tmp_path, *, month_title, borrow_row, nbr_row):
    """Render a synthetic single-page MFR PDF (preformatted text) containing a
    page-1 month title plus the Table-6 borrowing row and Table-4 NBR row, so
    scripts.mfr_parser reads them back via pdfplumber.extract_text()."""
    text = (
        "Monthly Report on Fiscal Position\n"
        f"{month_title}\n"
        "Table 6: Financing (Taka in Crore)\n"
        f"2.1 Borrowing from Banking System (Net) {borrow_row}\n"
        "Table 4: Revenue (Taka in Crore)\n"
        f"a. NBR {nbr_row}\n"
    )
    pdf_path = tmp_path / "mfr.pdf"
    doc = SimpleDocTemplate(str(pdf_path))
    style = getSampleStyleSheet()["Code"]
    doc.build([Preformatted(text, style)])
    return str(pdf_path)


class TestFiscalRowProvenanceField:
    def test_fy_budget_field_holds_anchor(self, tmp_path):
        # FY26 Oct-2025 borrow layout: anchor 104000 -> single 5720, fytd 7570.
        path = _make_mfr_pdf(
            tmp_path, month_title="October 2025",
            borrow_row="137500 99000 16715 15651 114161 104000 5720 7570",
            nbr_row="480000 463500 27289 101442 368715 21.1 79.6 499001 28027 117420 23.5",
        )
        row = mfr.parse_bank_borrowing(path, fy_budget_crore=104000.0)
        assert row.fy_budget == 104000.0
        assert row.single_month == 5720.0
        assert row.fytd == 7570.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_backfill_fiscal.py::TestFiscalRowProvenanceField -q`
Expected: FAIL — `AttributeError: 'FiscalRow' object has no attribute 'fy_budget'`

- [ ] **Step 3: Write minimal implementation** — in `scripts/mfr_parser.py`, change the `FiscalRow` dataclass (lines 74-81) so the 4th field reads:

```python
@dataclass(frozen=True)
class FiscalRow:
    """A single extracted single-month + FYTD pair, in BDT crore."""

    metric: str          # "govt_bank_borrow" | "nbr_revenue"
    single_month: float  # this report-month's stand-alone figure
    fytd: float          # fiscal-year-to-date figure as of this report month
    fy_budget: float     # the annual current-FY budget anchor used (provenance)
```

(The two `return FiscalRow(...)` statements pass the anchor positionally — no edit needed.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_backfill_fiscal.py::TestFiscalRowProvenanceField -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/mfr_parser.py tests/test_backfill_fiscal.py
git commit -m "refactor(fiscal): rename FiscalRow.fy26_budget -> fy_budget (now per-FY)"
```

---

## Task 4: FY-aware `parse_one_mfr`

**Files:**
- Modify: `scripts/backfill_fiscal.py:212-215` (anchor selection in `parse_one_mfr`)
- Test: `tests/test_backfill_fiscal.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_backfill_fiscal.py`. Uses real FY25/FY24 token layouts from the experiment.

```python
class TestParseOneMfrPerFyAnchor:
    def test_fy25_report_uses_fy25_anchor(self, tmp_path):
        # March 2025 (FY25): borrow anchor 137500 -> 29987/44346;
        #                    nbr anchor 480000 -> 32245/255076.
        path = _make_mfr_pdf(
            tmp_path, month_title="March 2025",
            borrow_row="132395 155935 13399 124150 137500 29987 44346 85298",
            nbr_row="430000 410001 361452 31235 249572 480000 32245 255076 53.1",
        )
        pm = bf.parse_one_mfr(path, "url://mar2025")
        assert (pm.year, pm.month) == (2025, 3)
        assert pm.borrow_single == 29987.0 and pm.borrow_fytd == 44346.0
        assert pm.nbr_single == 32245.0 and pm.nbr_fytd == 255076.0

    def test_fy24_report_uses_fy24_anchor(self, tmp_path):
        # March 2024 (FY24): borrow anchor 132395 -> 13399/54508;
        #                    nbr anchor 430000 -> 31181/249402.
        path = _make_mfr_pdf(
            tmp_path, month_title="March 2024",
            borrow_row="106334 115425 8671 118025 132395 13399 54508 44346",
            nbr_row="370000 370000 319731 28867 222892 430000 31181 249402 58.0",
        )
        pm = bf.parse_one_mfr(path, "url://mar2024")
        assert pm.borrow_single == 13399.0 and pm.borrow_fytd == 54508.0
        assert pm.nbr_single == 31181.0 and pm.nbr_fytd == 249402.0

    def test_unknown_fiscal_year_raises(self, tmp_path):
        # FY19 not in the anchor table -> must raise (caller skips + logs),
        # never silently fall back to a wrong-year anchor.
        path = _make_mfr_pdf(
            tmp_path, month_title="March 2019",
            borrow_row="90000 90000 5000 40000 95000 8000 45000 50000",
            nbr_row="300000 300000 250000 20000 130000 320000 22000 140000 40.0",
        )
        with pytest.raises(Exception):
            bf.parse_one_mfr(path, "url://mar2019")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_backfill_fiscal.py::TestParseOneMfrPerFyAnchor -q`
Expected: FAIL — FY25/FY24 cases parse with the FY26 anchor (104000/499001 not in the row) → `MfrParseError`; unknown-FY case does not raise.

- [ ] **Step 3: Write minimal implementation** — replace the two anchor lines in `parse_one_mfr` (`scripts/backfill_fiscal.py:213-215`, currently using `FY26_BORROW_BUDGET_CRORE` / `FY26_NBR_BUDGET_CRORE`) with FY-derived lookups:

```python
def parse_one_mfr(pdf_path: str, pdf_url: str) -> ParsedMfr:
    year, month = mfr.parse_report_month(pdf_path)
    fy = fiscal_year_of(year, month)
    if fy not in FY_BORROW_BUDGET or fy not in FY_NBR_BUDGET:
        raise mfr.MfrParseError(
            f"no budget anchor for FY{fy % 100} ({year}-{month:02d}); "
            f"add it to FY_BORROW_BUDGET/FY_NBR_BUDGET after confirming the value"
        )
    b = mfr.parse_bank_borrowing(pdf_path, fy_budget_crore=FY_BORROW_BUDGET[fy])
    n = mfr.parse_nbr_revenue(pdf_path, fy_budget_crore=FY_NBR_BUDGET[fy])
```

(Leave the rest of `parse_one_mfr` — the July normalization and the `return ParsedMfr(...)` — unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_backfill_fiscal.py::TestParseOneMfrPerFyAnchor -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add scripts/backfill_fiscal.py
git commit -m "feat(fiscal): select MFR budget anchor by report fiscal year"
```

---

## Task 5: `self_check_failures` helper (DRY refactor)

**Files:**
- Modify: `scripts/backfill_fiscal.py:133-164` (`self_check_fytd`)
- Test: `tests/test_backfill_fiscal.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_backfill_fiscal.py`:

```python
class TestSelfCheckFailures:
    def _mk(self, y, m, b_single, b_fytd, n_single=0.0, n_fytd=0.0):
        return bf.ParsedMfr(y, m, "url", b_single, b_fytd, n_single, n_fytd)

    def test_returns_failing_month_keys(self):
        series = {
            (2025, 7): self._mk(2025, 7, 500.0, 500.0),
            (2025, 8): self._mk(2025, 8, 100.0, 2500.0),   # single 100 vs diff 2000
        }
        assert bf.self_check_failures(series, "borrow") == {(2025, 8)}

    def test_consistent_series_has_no_failures(self):
        series = {
            (2025, 7): self._mk(2025, 7, 2862.0, 2862.0),
            (2025, 8): self._mk(2025, 8, -6289.0, -3427.0),
        }
        assert bf.self_check_failures(series, "borrow") == set()

    def test_july_and_gap_months_never_fail(self):
        series = {
            (2025, 7): self._mk(2025, 7, 2862.0, 9999.0),   # July: uncheckable
            (2025, 10): self._mk(2025, 10, 5720.0, 7570.0),  # Sep missing: gap
        }
        assert bf.self_check_failures(series, "borrow") == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_backfill_fiscal.py::TestSelfCheckFailures -q`
Expected: FAIL — `AttributeError: ... has no attribute 'self_check_failures'`

- [ ] **Step 3: Write minimal implementation** — replace `self_check_fytd` (lines 133-164) with a shared core plus two thin wrappers (existing `self_check_fytd` string API preserved for the `TestSelfCheckFytd` tests):

```python
def _self_check_issues(series_by_month: dict[tuple[int, int], "ParsedMfr"],
                       which: str) -> list[tuple[tuple[int, int], str]]:
    """Shared core: for consecutive months within a fiscal year, published
    single-month should ~= (this FYTD - prior FYTD). Returns (key, message)
    for each month diverging by > SELF_CHECK_TOLERANCE. Skips July (FY first
    month) and gaps (no prior month present)."""
    issues: list[tuple[tuple[int, int], str]] = []
    keys = sorted(series_by_month)
    for (y, m) in keys:
        if m == 7:  # fiscal-year first month: single == FYTD, nothing to diff
            continue
        prev = (y, m - 1) if m > 1 else (y - 1, 12)
        if prev not in series_by_month:
            continue  # gap — cannot cross-check
        cur = series_by_month[(y, m)]
        pre = series_by_month[prev]
        single = cur.borrow_single if which == "borrow" else cur.nbr_single
        fytd_now = cur.borrow_fytd if which == "borrow" else cur.nbr_fytd
        fytd_pre = pre.borrow_fytd if which == "borrow" else pre.nbr_fytd
        implied = fytd_now - fytd_pre
        denom = abs(single) if abs(single) > 1e-9 else 1.0
        rel = abs(single - implied) / denom
        if rel > SELF_CHECK_TOLERANCE:
            issues.append((
                (y, m),
                f"{which} {y}-{m:02d}: published single={single:,.0f} vs "
                f"FYTD-diff={implied:,.0f} (rel {rel:.1%} > {SELF_CHECK_TOLERANCE:.0%})",
            ))
    return issues


def self_check_fytd(series_by_month: dict[tuple[int, int], "ParsedMfr"],
                    which: str) -> list[str]:
    """Human-readable FYTD-diff warnings (one per diverging month)."""
    return [msg for _key, msg in _self_check_issues(series_by_month, which)]


def self_check_failures(series_by_month: dict[tuple[int, int], "ParsedMfr"],
                        which: str) -> set[tuple[int, int]]:
    """The (year, month) keys that FAIL the FYTD-diff check — used as the
    backfill write gate. July/gap months are never failures (kept)."""
    return {key for key, _msg in _self_check_issues(series_by_month, which)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_backfill_fiscal.py::TestSelfCheckFailures tests/test_backfill_fiscal.py::TestSelfCheckFytd -q`
Expected: PASS (both the new failures tests and the pre-existing `self_check_fytd` string tests stay green)

- [ ] **Step 5: Commit**

```bash
git add scripts/backfill_fiscal.py
git commit -m "refactor(fiscal): add self_check_failures gate helper (DRY with self_check_fytd)"
```

---

## Task 6: Gate row-building + wire main()

**Files:**
- Modify: `scripts/backfill_fiscal.py` (add `build_history_rows`; rewire main() lines 368-380)
- Test: `tests/test_backfill_fiscal.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_backfill_fiscal.py`:

```python
class TestBuildHistoryRowsGate:
    def _mk(self, y, m, b_single, n_single):
        return bf.ParsedMfr(y, m, "url", b_single, 0.0, n_single, 0.0)

    def test_drops_only_the_failing_metric_month(self):
        parsed = {
            (2025, 8): self._mk(2025, 8, -6289.0, 26643.0),
            (2025, 9): self._mk(2025, 9, 1111.0, 27000.0),
        }
        rows = bf.build_history_rows(
            parsed, drop_borrow={(2025, 8)}, drop_nbr=set())
        keys = {(r["metric_id"], r["as_of"]) for r in rows}
        # Aug borrow dropped; Aug NBR + both Sep rows kept.
        assert (bf.METRIC_BORROW, "2025-08-31") not in keys
        assert (bf.METRIC_NBR, "2025-08-31") in keys
        assert (bf.METRIC_BORROW, "2025-09-30") in keys
        assert (bf.METRIC_NBR, "2025-09-30") in keys

    def test_no_drops_keeps_two_rows_per_month(self):
        parsed = {(2025, 9): self._mk(2025, 9, 1111.0, 27000.0)}
        rows = bf.build_history_rows(parsed)
        assert len(rows) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_backfill_fiscal.py::TestBuildHistoryRowsGate -q`
Expected: FAIL — `AttributeError: ... has no attribute 'build_history_rows'`

- [ ] **Step 3: Write minimal implementation** — add `build_history_rows` after `build_monthly_row` (~line 113):

```python
def build_history_rows(
    parsed: dict[tuple[int, int], "ParsedMfr"],
    *,
    drop_borrow: set[tuple[int, int]] = frozenset(),
    drop_nbr: set[tuple[int, int]] = frozenset(),
) -> list[dict]:
    """Build monthly borrow/NBR upsert rows, omitting any (metric, month) that
    failed the FYTD self-check. Months in neither drop set produce two rows."""
    rows: list[dict] = []
    for (y, m), pm in sorted(parsed.items()):
        if (y, m) not in drop_borrow:
            rows.append(build_monthly_row(METRIC_BORROW, y, m, pm.borrow_single))
        if (y, m) not in drop_nbr:
            rows.append(build_monthly_row(METRIC_NBR, y, m, pm.nbr_single))
    return rows
```

- [ ] **Step 4: Rewire main()** — replace the self-check + row-building block (`scripts/backfill_fiscal.py:368-380`, from `# FYTD-diff self-checks` through the `logger.info("prepared %d monthly...` line) with:

```python
    # FYTD-diff self-check is a HARD GATE: log every warning, then drop the
    # failing (metric, month) rows. July / gap-isolated months are never
    # failures (kept on parse-strength — they are the anchor points).
    drops: dict[str, set[tuple[int, int]]] = {}
    for which in ("borrow", "nbr"):
        for w in self_check_fytd(parsed, which):
            logger.warning("SELF-CHECK: %s", w)
        drops[which] = self_check_failures(parsed, which)
        for (y, m) in sorted(drops[which]):
            logger.warning("DROP %s %04d-%02d: failed FYTD self-check", which, y, m)

    history_rows = build_history_rows(
        parsed, drop_borrow=drops["borrow"], drop_nbr=drops["nbr"])

    logger.info("prepared %d monthly borrow/NBR rows from %d months "
                "(%d borrow + %d nbr dropped by self-check)",
                len(history_rows), len(parsed),
                len(drops["borrow"]), len(drops["nbr"]))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_backfill_fiscal.py -q`
Expected: PASS (full file; real-PDF tests may `skip`)

- [ ] **Step 6: Commit**

```bash
git add scripts/backfill_fiscal.py tests/test_backfill_fiscal.py
git commit -m "feat(fiscal): hard-gate backfill rows on FYTD self-check (drop+log failures)"
```

---

## Task 7: Full suite + lint green

**Files:** none (verification only)

- [ ] **Step 1: Run ruff**

Run: `.venv/bin/ruff check scripts/backfill_fiscal.py scripts/mfr_parser.py tests/test_backfill_fiscal.py`
Expected: no errors. If ruff flags an unused import or line length, fix minimally and re-run.

- [ ] **Step 2: Run the full test suite** (registry-coverage + all fiscal/parser tests must stay green)

Run: `.venv/bin/python -m pytest -q`
Expected: all pass (some real-PDF / network tests may `skip`); **no failures**.

- [ ] **Step 3: Commit any fixups**

```bash
git add -A && git commit -m "chore(fiscal): ruff/lint fixups for per-FY anchor backfill" || echo "nothing to commit"
```

---

## Task 8 (OPERATIONAL — interactive, with Adnan): full-archive dry-run + source FY23↓ anchors

> Not a code-subagent task. Executed in-session on the Mac (BD-side egress); **no DB writes**. Checkpoint with Adnan on the self-check output before Task 9.

- [ ] **Step 1: Build the links-file** from the already-saved month→URL map (`/tmp/mfr_links.json`, 75 reports 2019-07→2025-10): write all Oracle CDN PDF URLs (newest first) to `/tmp/mfr_backfill_urls.txt`, one per line.

- [ ] **Step 2: First dry-run (anchors FY26/FY25/FY24 only)**

Run:
```
.venv/bin/python scripts/backfill_fiscal.py --dry-run \
  --links-file /tmp/mfr_backfill_urls.txt --max-reports 75 --verbose
```
Expected: FY26/FY25/FY24 months parse + log; older months log `skipping ... no budget anchor for FYxx`. Note any `SELF-CHECK` / `DROP` lines.

- [ ] **Step 2a: Review with Adnan.** Confirm FY25/FY24 single-month values look sane and the self-check is clean (or understand each DROP). **Stop here for sign-off before sourcing older years.**

- [ ] **Step 3: Source FY23↓ anchors.** For each older fiscal year still skipped, read the current-FY *Budget* value off one of that year's PDFs (the prior-year column of the next year's report cross-confirms it), add `{fy: value}` to `FY_BORROW_BUDGET` / `FY_NBR_BUDGET` with a provenance comment, and re-run Step 2. Repeat until the dry-run stops cleanly parsing deeper (the natural depth floor). Commit the anchor additions:

```bash
git add scripts/backfill_fiscal.py
git commit -m "feat(fiscal): add FY23..FYxx budget anchors (confirmed via dry-run)"
```

- [ ] **Step 4: Final dry-run review.** Full self-check across the whole series clean (every kept month reconciles; every drop understood). Record the month count that will be written.

---

## Task 9 (OPERATIONAL — VPS write + verify)

> Prod write MUST run on ExonVPS (service-role key in `/etc/econdelta.env`; the harness blocks prod writes from the Mac). Idempotent upsert; existing FY26 rows re-confirmed, never destroyed.

- [ ] **Step 1: Open the PR** for the code change (Tasks 1-8) per repo style (EconDelta uses merge-commit). Get CI green.

- [ ] **Step 2: Run the real backfill on the VPS** (hand Adnan a ready script run via `!`, or `ssh exonhost`), after pulling the merged branch:
```
.venv/bin/python scripts/backfill_fiscal.py \
  --links-file /tmp/mfr_backfill_urls.txt --max-reports 75
```
Expected: `upsert ok: N rows -> metric_history_monthly`.

- [ ] **Step 3: Verify via anon REST** (from the Mac) that the new months landed with sane values + correct `source_as_of`:
```
curl -s "https://ssbliukchgibjcjohibi.supabase.co/rest/v1/metric_history_monthly?metric_id=eq.govt_bank_borrow_monthly_cr&select=as_of,value&order=as_of.asc" \
  -H "apikey: <anon>" -H "Authorization: Bearer <anon>"
```
Expected: count jumps from 4 to the dry-run month count; oldest `as_of` matches the depth floor; Oct 2025 still 5720.

- [ ] **Step 4: Update auto-memory + AGENT_LEARNINGS** with the outcome (newest depth reached, any dropped months, the per-FY anchor table as the canonical record).

---

## Self-Review

- **Spec coverage:** value-anchor kept (T3,T4); per-FY anchor table (T2); FY selection (T1,T4); self-check hard gate (T5,T6); dry-run review before write (T8); VPS write + verify (T9); idempotent/non-destructive (noted T6,T9); testing per FY incl. broken-row drop (T4,T6) + unknown-FY skip (T4). All spec sections map to a task.
- **Placeholder scan:** none — every code step shows complete code; FY23↓ anchors are a *defined operational step* (T8 Step 3) with a sourcing method, not a code placeholder.
- **Type consistency:** `fiscal_year_of(year,month)->int`, `FY_BORROW_BUDGET`/`FY_NBR_BUDGET: dict[int,float]`, `FiscalRow.fy_budget`, `self_check_failures(...)->set[tuple[int,int]]`, `build_history_rows(parsed,*,drop_borrow,drop_nbr)->list[dict]` — names/signatures used consistently across T1-T6 and the main() rewire.
