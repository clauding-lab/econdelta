# Aggregate Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the daily aggregate resilient to a single bad indicator — a cumulative-monotonicity guard stops impossible FYTD drops at the source, a granular Opus reject quarantines flagged fields instead of freezing the whole snapshot, and the NBR extraction prompt is hardened so Opus 4.8 reads the FYTD-cumulative figure correctly.

**Architecture:** All three changes live in the aggregate/parse layer. The guard and granular-reject extend existing machinery in `aggregate_latest.py` (`_load_last_good_snapshot`, the `_build_v3_blocks` per-indicator loop, the `main()` opus-review reject branch). The prompt change is config + `pdf_component.txt`. Defense-in-depth: guard catches it pre-review, granular-reject catches anything that reaches the review.

**Tech Stack:** Python 3.11, pytest. Tests mock the `claude` subprocess (wiring only). `ECONDELTA_SKIP_SUPABASE=1` autouse fixture in aggregate tests.

**Spec:** `docs/superpowers/specs/2026-05-31-nbr-guard-granular-reject-design.md`

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `aggregate_latest.py` | aggregation + safety gates | + `_fiscal_year`, `_prior_good_snapshot`, `_is_cumulative_regression`, `_quarantine_flagged`; guard hook in `_build_v3_blocks`; granular-reject in `main()` |
| `config/sources-v3.json` | source registry | `"cumulative": true` on FYTD ids; hardened `tax_revenue` task |
| `claude_max/prompts/pdf_component.txt` | LLM extraction prompt | general FYTD/cumulative rule |
| `tests/test_aggregate_cumulative_guard.py` | guard unit tests | new |
| `tests/test_aggregate_granular_reject.py` | granular-reject unit tests | new |

Constants to add near the top of `aggregate_latest.py` (after existing module constants):
```python
# Cumulative-figure guard: a fiscal-year-to-date total can only rise within a FY.
CUMULATIVE_DROP_TOLERANCE = 0.05   # >5% same-FY drop ⇒ implausible
FISCAL_YEAR_START_MONTH = 7        # Bangladesh FY = July–June
# Granular Opus reject: quarantine up to this many flagged fields; more ⇒ hard reject.
MAX_QUARANTINE_FIELDS = 5
```

---

## Task 1: Fiscal-year + cumulative-regression helpers

**Files:**
- Modify: `aggregate_latest.py` (add helpers near `_load_last_good_snapshot`, ~L235)
- Test: `tests/test_aggregate_cumulative_guard.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the cumulative-monotonicity guard in aggregate_latest.py."""
from __future__ import annotations

from datetime import date

import pytest


@pytest.fixture(autouse=True)
def skip_supabase(monkeypatch):
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "1")
    yield


class TestFiscalYear:
    def test_july_starts_new_fy(self):
        from aggregate_latest import _fiscal_year
        assert _fiscal_year(date(2026, 7, 1)) == 2026
        assert _fiscal_year(date(2026, 6, 30)) == 2025
        assert _fiscal_year(date(2026, 1, 15)) == 2025


class TestCumulativeRegression:
    def test_same_fy_big_drop_is_regression(self):
        from aggregate_latest import _is_cumulative_regression
        # 287862 -> 33522 within FY2025 (both before next July)
        assert _is_cumulative_regression(33522.0, 287862.59, date(2026, 5, 31), date(2026, 5, 30)) is True

    def test_same_fy_small_dip_within_tolerance_is_ok(self):
        from aggregate_latest import _is_cumulative_regression
        # 2% downward revision — allowed
        assert _is_cumulative_regression(282000.0, 287862.59, date(2026, 5, 31), date(2026, 5, 30)) is False

    def test_rise_is_never_a_regression(self):
        from aggregate_latest import _is_cumulative_regression
        assert _is_cumulative_regression(300000.0, 287862.59, date(2026, 5, 31), date(2026, 5, 30)) is False

    def test_july_reset_across_fy_is_allowed(self):
        from aggregate_latest import _is_cumulative_regression
        # prior good from FY2025 (June), today early FY2026 (July) — legitimate reset
        assert _is_cumulative_regression(20000.0, 287862.59, date(2026, 7, 5), date(2026, 6, 28)) is False

    def test_non_numeric_today_is_not_regression(self):
        from aggregate_latest import _is_cumulative_regression
        assert _is_cumulative_regression(None, 287862.59, date(2026, 5, 31), date(2026, 5, 30)) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_aggregate_cumulative_guard.py -v`
Expected: FAIL — `ImportError: cannot import name '_fiscal_year'`

- [ ] **Step 3: Write minimal implementation**

Add to `aggregate_latest.py` (after `_load_last_good_snapshot`, before `_compute_reserve_utilisation`):

```python
def _fiscal_year(d: date) -> int:
    """Bangladesh fiscal year (July–June). Returns the FY-start calendar year."""
    return d.year if d.month >= FISCAL_YEAR_START_MONTH else d.year - 1


def _is_cumulative_regression(
    today_value: object,
    prior_value: object,
    today_date: date,
    prior_date: date,
) -> bool:
    """True if a cumulative (FYTD) figure dropped implausibly within the same FY.

    A cumulative fiscal-year-to-date total can only rise within a fiscal year.
    A drop beyond CUMULATIVE_DROP_TOLERANCE in the SAME fiscal year is a parse
    error. A drop across the July FY boundary is the legitimate annual reset.
    """
    if not isinstance(today_value, (int, float)) or isinstance(today_value, bool):
        return False
    if not isinstance(prior_value, (int, float)) or isinstance(prior_value, bool):
        return False
    if prior_value <= 0:
        return False
    if _fiscal_year(today_date) != _fiscal_year(prior_date):
        return False  # FY reset — drop is legitimate
    return today_value < prior_value * (1 - CUMULATIVE_DROP_TOLERANCE)
```

(Ensure `from datetime import date` is imported — `datetime` is already imported; add `date` to that import if absent.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_aggregate_cumulative_guard.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add aggregate_latest.py tests/test_aggregate_cumulative_guard.py
git commit -m "feat(aggregate): cumulative-regression + fiscal-year helpers"
```

---

## Task 2: Prior-good-snapshot lookup (excludes today)

**Files:**
- Modify: `aggregate_latest.py` (add helper after `_load_last_good_snapshot`)
- Test: `tests/test_aggregate_cumulative_guard.py` (extend)

The existing `_load_last_good_snapshot` would return *today's* snapshot (the regression isn't `_is_bad_snapshot`), so the guard needs the most-recent good snapshot **strictly before today**.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_aggregate_cumulative_guard.py`:

```python
class TestPriorGoodSnapshot:
    def test_returns_most_recent_good_before_today(self, tmp_path, monkeypatch):
        import aggregate_latest
        d = tmp_path / "tax_revenue"
        d.mkdir()
        (d / "2026-05-30.json").write_text(
            '{"value": 287862.59, "scraped_at": "2026-05-30T05:00:00+00:00", "_provenance": "llm_extracted"}'
        )
        (d / "2026-05-31.json").write_text(
            '{"value": 33522.0, "scraped_at": "2026-05-31T05:00:00+00:00", "_provenance": "llm_extracted"}'
        )
        monkeypatch.setattr(aggregate_latest, "DATA_DIR", tmp_path)
        snap = aggregate_latest._prior_good_snapshot("tax_revenue", date(2026, 5, 31))
        assert snap is not None
        assert snap["value"] == 287862.59

    def test_returns_none_when_no_prior(self, tmp_path, monkeypatch):
        import aggregate_latest
        d = tmp_path / "tax_revenue"
        d.mkdir()
        (d / "2026-05-31.json").write_text(
            '{"value": 33522.0, "scraped_at": "2026-05-31T05:00:00+00:00", "_provenance": "llm_extracted"}'
        )
        monkeypatch.setattr(aggregate_latest, "DATA_DIR", tmp_path)
        assert aggregate_latest._prior_good_snapshot("tax_revenue", date(2026, 5, 31)) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_aggregate_cumulative_guard.py::TestPriorGoodSnapshot -v`
Expected: FAIL — `AttributeError: ... has no attribute '_prior_good_snapshot'`

- [ ] **Step 3: Write minimal implementation**

Add to `aggregate_latest.py` (right after `_load_last_good_snapshot`):

```python
def _prior_good_snapshot(indicator_id: str, today: date) -> dict | None:
    """Most-recent good snapshot strictly BEFORE `today` (by scraped_at date).

    Unlike _load_last_good_snapshot, this excludes today's own snapshot — the
    cumulative guard must compare today's value against a genuinely prior value.
    """
    d = DATA_DIR / indicator_id
    if not d.exists():
        return None
    for path in sorted(d.glob("*.json"), reverse=True):
        try:
            blob = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if _is_bad_snapshot(blob):
            continue
        try:
            scraped = datetime.fromisoformat(
                blob["scraped_at"].replace("Z", "+00:00")
            ).date()
        except (KeyError, ValueError):
            continue
        if scraped < today:
            return blob
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_aggregate_cumulative_guard.py::TestPriorGoodSnapshot -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add aggregate_latest.py tests/test_aggregate_cumulative_guard.py
git commit -m "feat(aggregate): _prior_good_snapshot lookup excluding today"
```

---

## Task 3: Wire the guard into `_build_v3_blocks` + config flag

**Files:**
- Modify: `aggregate_latest.py` (`_build_v3_blocks` per-indicator loop, ~L330–345)
- Modify: `config/sources-v3.json` (`"cumulative": true` on FYTD ids)
- Test: `tests/test_aggregate_cumulative_guard.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_aggregate_cumulative_guard.py`:

```python
class TestGuardInBuildBlocks:
    def _write(self, root, ind_id, dates_values):
        d = root / ind_id
        d.mkdir()
        for ds, val in dates_values:
            (d / f"{ds}.json").write_text(
                f'{{"value": {val}, "scraped_at": "{ds}T05:00:00+00:00", '
                f'"_provenance": "llm_extracted", "change_pct": null}}'
            )

    def test_cumulative_regression_uses_prior_good(self, tmp_path, monkeypatch):
        import aggregate_latest
        from datetime import datetime, timezone
        self._write(tmp_path, "tax_revenue", [("2026-05-30", 287862.59), ("2026-05-31", 33522.0)])
        monkeypatch.setattr(aggregate_latest, "DATA_DIR", tmp_path)
        monkeypatch.setattr(
            aggregate_latest, "_load_v3_registry",
            lambda: [{"id": "tax_revenue", "domain": "government_finance",
                      "cadence": "monthly", "cumulative": True}],
        )
        data_additions, _, _, _ = aggregate_latest._build_v3_blocks(
            datetime(2026, 5, 31, 6, 0, tzinfo=timezone.utc)
        )
        # guard replaced the regressed 33522 with the prior-good 287862.59
        assert data_additions["tax_revenue"] == 287862.59

    def test_non_cumulative_drop_is_untouched(self, tmp_path, monkeypatch):
        import aggregate_latest
        from datetime import datetime, timezone
        self._write(tmp_path, "some_level", [("2026-05-30", 100.0), ("2026-05-31", 10.0)])
        monkeypatch.setattr(aggregate_latest, "DATA_DIR", tmp_path)
        monkeypatch.setattr(
            aggregate_latest, "_load_v3_registry",
            lambda: [{"id": "some_level", "domain": "macro", "cadence": "daily"}],
        )
        data_additions, _, _, _ = aggregate_latest._build_v3_blocks(
            datetime(2026, 5, 31, 6, 0, tzinfo=timezone.utc)
        )
        assert data_additions["some_level"] == 10.0  # no guard for non-cumulative
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_aggregate_cumulative_guard.py::TestGuardInBuildBlocks -v`
Expected: FAIL — `assert 33522.0 == 287862.59`

- [ ] **Step 3: Write minimal implementation**

In `aggregate_latest.py`, in the `_build_v3_blocks` per-indicator loop, **after** the `if _is_bad_snapshot(snapshot):` block (after the line `snapshot = historical`, ~L344) and **before** `fresh = _is_fresh(...)` (~L346), insert:

```python
        # Cumulative-monotonicity guard: a FYTD/cumulative total can't fall within
        # a fiscal year. If it did (parser/LLM mis-read), fall back to the prior
        # good value, marked stale — see docs/.../nbr-guard-granular-reject.
        elif ind.get("cumulative"):
            prior = _prior_good_snapshot(indicator_id, now.date())
            if prior is not None:
                try:
                    prior_date = datetime.fromisoformat(
                        prior["scraped_at"].replace("Z", "+00:00")
                    ).date()
                except (KeyError, ValueError):
                    prior_date = None
                if prior_date is not None and _is_cumulative_regression(
                    snapshot.get("value"), prior.get("value"), now.date(), prior_date
                ):
                    logger.error(
                        "cumulative regression for %s: today=%s < prior-good=%s (same FY) "
                        "— stale-fallback to %s",
                        indicator_id, snapshot.get("value"), prior.get("value"),
                        prior.get("scraped_at", "?"),
                    )
                    indicators_failed += 1
                    prior = {**prior, "_provenance": "stale_fallback",
                             "_stale_from": prior.get("scraped_at")}
                    snapshot = prior
```

**Note:** this is an `elif` on the existing `if _is_bad_snapshot(snapshot):`. A bad snapshot already gets stale-fallback; a *good* snapshot of a cumulative indicator gets the regression check. Confirm the `if`/`elif` indentation aligns with the existing block.

Then add `"cumulative": true` in `config/sources-v3.json` to: `tax_revenue`, `nbr_vat_collected_cr`, `nbr_it_collected_cr`, `nbr_customs_collected_cr`, and any `*_fytd*` / FYTD fiscal id (search `"FYTD"` in names). Example edit for `tax_revenue` (add the key alongside `cadence`):
```json
      "cadence": "monthly",
      "cumulative": true,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_aggregate_cumulative_guard.py -v`
Expected: PASS (all)

- [ ] **Step 5: Validate config JSON + commit**

```bash
.venv/bin/python -c "import json; json.load(open('config/sources-v3.json')); print('config OK')"
git add aggregate_latest.py config/sources-v3.json tests/test_aggregate_cumulative_guard.py
git commit -m "feat(aggregate): cumulative-monotonicity guard + config flags"
```

---

## Task 4: Granular Opus reject — quarantine helper

**Files:**
- Modify: `aggregate_latest.py` (add `_quarantine_flagged` near the other helpers)
- Test: `tests/test_aggregate_granular_reject.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""Tests for granular Opus-reject quarantine in aggregate_latest.py."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def skip_supabase(monkeypatch):
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "1")
    yield


class TestQuarantineFlagged:
    def _history(self):
        # newest-last list of archived `.data` dicts (matches load_history shape)
        return [
            {"data": {"nbr_fytd_collected_cr": 287862.59, "usd_bdt_mid": 121.0}},
            {"data": {"nbr_fytd_collected_cr": 287862.59, "usd_bdt_mid": 121.5}},
        ]

    def test_quarantines_mappable_flagged_field_from_history(self):
        from aggregate_latest import _quarantine_flagged
        data = {"nbr_fytd_collected_cr": 33522.0, "usd_bdt_mid": 121.6}
        cleaned, quarantined, hard_reject = _quarantine_flagged(
            data, ["nbr_fytd_collected_cr"], self._history()
        )
        assert hard_reject is False
        assert "nbr_fytd_collected_cr" in quarantined
        assert cleaned["nbr_fytd_collected_cr"] == 287862.59  # last-good from history
        assert cleaned["usd_bdt_mid"] == 121.6                 # untouched

    def test_unmappable_field_forces_hard_reject(self):
        from aggregate_latest import _quarantine_flagged
        data = {"nbr_fytd_collected_cr": 33522.0}
        cleaned, quarantined, hard_reject = _quarantine_flagged(
            data, ["totally_unknown_metric"], self._history()
        )
        assert hard_reject is True

    def test_too_many_flagged_forces_hard_reject(self):
        from aggregate_latest import _quarantine_flagged
        data = {f"m{i}": float(i) for i in range(10)}
        flagged = [f"m{i}" for i in range(6)]  # > MAX_QUARANTINE_FIELDS (5)
        _, _, hard_reject = _quarantine_flagged(data, flagged, [{"data": data}])
        assert hard_reject is True

    def test_no_history_value_drops_the_field(self):
        from aggregate_latest import _quarantine_flagged
        data = {"nbr_fytd_collected_cr": 33522.0, "usd_bdt_mid": 121.6}
        cleaned, quarantined, hard_reject = _quarantine_flagged(
            data, ["nbr_fytd_collected_cr"], [{"data": {"usd_bdt_mid": 121.0}}]
        )
        assert hard_reject is False
        assert "nbr_fytd_collected_cr" not in cleaned  # dropped, no last-good
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_aggregate_granular_reject.py -v`
Expected: FAIL — `ImportError: cannot import name '_quarantine_flagged'`

- [ ] **Step 3: Write minimal implementation**

Add to `aggregate_latest.py` (near the other helpers):

```python
def _quarantine_flagged(
    data: dict[str, Any],
    flagged_ids: list[str],
    history: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str], bool]:
    """Quarantine Opus-flagged fields instead of rejecting the whole snapshot.

    Returns (cleaned_data, quarantined_ids, hard_reject).
    hard_reject is True when the verdict is untrustworthy or too broad:
      * any flagged id is not present in `data`, or
      * more than MAX_QUARANTINE_FIELDS ids are flagged.
    Otherwise each flagged id is replaced with its most-recent good value from
    `history` (newest-last list of archived `.data` dicts); if no historical
    value exists, the field is dropped.
    """
    present = [fid for fid in flagged_ids if fid in data]
    if len(present) != len(flagged_ids):
        return data, [], True   # unmappable flagged id ⇒ don't trust the verdict
    if len(present) > MAX_QUARANTINE_FIELDS:
        return data, [], True   # too broadly broken to publish

    cleaned = dict(data)
    quarantined: list[str] = []
    for fid in present:
        last_good = None
        for snap in reversed(history):  # newest-last ⇒ reversed = newest-first
            v = (snap.get("data") or {}).get(fid)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                last_good = v
                break
        if last_good is not None:
            cleaned[fid] = last_good
        else:
            cleaned.pop(fid, None)
        quarantined.append(fid)
    return cleaned, quarantined, False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_aggregate_granular_reject.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add aggregate_latest.py tests/test_aggregate_granular_reject.py
git commit -m "feat(aggregate): _quarantine_flagged helper for granular reject"
```

---

## Task 5: Wire granular reject into `main()`

**Files:**
- Modify: `aggregate_latest.py` (`main()` opus-review reject branch, ~L640–654)

The current branch (after `elif status == "reject":`) logs + notifies + `return 1`. Replace the unconditional `return 1` with quarantine-or-hard-reject. Note the bundle is built at ~L609 from `data`; on quarantine we mutate `data` and must **rebuild** the bundle before `write_latest`.

- [ ] **Step 1: Write the implementation**

In `main()`, restructure the reject branch. Replace (the block at ~L640–654):

```python
            elif status == "reject":
                missing = verdict.get("missing", [])
                anomalies = verdict.get("anomalies", [])
                logger.error(
                    "opus review REJECTED: %s | missing=%s | anomalies=%d",
                    reason, missing[:5], len(anomalies),
                )
                notify(...)  # existing
                return 1
```

with:

```python
            elif status == "reject":
                missing = verdict.get("missing", []) or []
                anomalies = verdict.get("anomalies", []) or []
                flagged = [a.get("indicator") for a in anomalies if a.get("indicator")]
                flagged = list({*flagged, *missing})
                cleaned, quarantined, hard_reject = _quarantine_flagged(data, flagged, history)
                if hard_reject:
                    logger.error(
                        "opus review REJECTED (hard): %s | missing=%s | anomalies=%d "
                        "(unmappable or >%d fields) — keeping yesterday's latest.json",
                        reason, missing[:5], len(anomalies), MAX_QUARANTINE_FIELDS,
                    )
                    notify(
                        "warn",
                        "EconDelta Opus review rejected today's data",
                        f"reason: {reason}\nmissing: {missing[:5]}\nanomalies: {len(anomalies)}\n"
                        f"keeping yesterday's latest.json — retry timers will re-run.",
                    )
                    return 1
                # Granular path: quarantine the flagged fields, publish the rest.
                logger.warning(
                    "opus review reject → quarantined %d field(s): %s | reason: %s",
                    len(quarantined), quarantined, reason,
                )
                notify(
                    "warn",
                    "EconDelta published with fields quarantined",
                    f"reason: {reason}\nquarantined: {quarantined}\n"
                    f"these fields use last-good values; the rest published fresh.",
                )
                data = cleaned
                bundle = LatestBundle(
                    schema_version="3.0",
                    updated_at=now,
                    sources_status=sources_status,
                    data=data,
                    domains=domains,
                    freshness=freshness,
                    alerts=alerts,
                )
```

(Control then falls through to the existing `write_latest(bundle)` at ~L658.)

**Confirm:** `history` is in scope here (it is — `history = load_history(ARCHIVE_DIR, days=5)` at ~L631). `domains`, `freshness`, `alerts`, `sources_status`, `now` are all in scope from the earlier bundle build.

- [ ] **Step 2: Verify the full aggregate test suite + targeted reject test**

Run: `.venv/bin/python -m pytest tests/test_aggregate_definitions.py tests/test_aggregate_cumulative_guard.py tests/test_aggregate_granular_reject.py -v`
Expected: PASS

- [ ] **Step 3: Smoke-check the module imports + main() is wired**

Run: `.venv/bin/python -c "import aggregate_latest; print('import OK')"`
Expected: `import OK`

- [ ] **Step 4: Commit**

```bash
git add aggregate_latest.py
git commit -m "feat(aggregate): granular Opus reject — quarantine flagged fields, publish rest"
```

---

## Task 6: Harden the NBR extraction prompt (keep Opus 4.8)

**Files:**
- Modify: `config/sources-v3.json` (`tax_revenue.fetch.task`)
- Modify: `claude_max/prompts/pdf_component.txt`

- [ ] **Step 1: Tighten the `tax_revenue` task instruction**

In `config/sources-v3.json`, `tax_revenue.fetch.task`, replace:
```json
        "task": "Go to latest available PDF, Component 9",
```
with:
```json
        "task": "Go to the latest available PDF, Component 9 (NBR tax revenue). Return the CUMULATIVE fiscal-year-to-date (July to the latest reported month) NBR tax revenue total in BDT crore — the running cumulative total, which mid-year is typically 200,000+ crore. NEVER return a single month's collection or a target/shortfall figure.",
```

- [ ] **Step 2: Add a general FYTD rule to `pdf_component.txt`**

In `claude_max/prompts/pdf_component.txt`, in the `RULES:` block, add:
```
- If the instruction asks for a fiscal-year-to-date (FYTD) or CUMULATIVE figure, return the running cumulative total, NEVER a single month's value. A cumulative FYTD figure only grows during a fiscal year.
```

- [ ] **Step 3: Validate JSON + commit**

```bash
.venv/bin/python -c "import json; json.load(open('config/sources-v3.json')); print('config OK')"
git add config/sources-v3.json claude_max/prompts/pdf_component.txt
git commit -m "fix(nbr): harden tax_revenue FYTD extraction prompt (cumulative, not monthly)"
```

- [ ] **Step 4: Live validation against the saved PDF (manual, requires box read access)**

This proves the hardened prompt actually fixes 4.8's mis-read (the risk accepted in the spec). On the box (read-only inputs; do not write Supabase):
```bash
# fetch the saved 24-May PDF text + run the hardened extraction with Opus 4.8,
# confirm it returns ~287,862 (not 33,522).
```
Expected: value ≈ 287,862. If it still returns a monthly figure, the guard (Task 3) keeps NBR safely stale — escalate the model decision (revert hybrid parser to 4.6). **Document the observed value in the PR.**

---

## Task 7: Final verification

- [ ] **Step 1: Full test suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass (existing + new). The opus_review/aggregate tests mock the `claude` subprocess.

- [ ] **Step 2: Lint**

Run: `.venv/bin/ruff check aggregate_latest.py tests/test_aggregate_cumulative_guard.py tests/test_aggregate_granular_reject.py`
Expected: no errors.

- [ ] **Step 3: Confirm spec coverage**

Re-read the spec; confirm Fix 1 (granular reject, Tasks 4–5), Fix 2 (guard, Tasks 1–3), Fix 3 (prompt, Task 6) all land. Follow-ups (deterministic Component-9, `parse.service` label) are intentionally out of scope.

---

## Notes / landmines
- The guard runs **pre-flatten** on the source id (`tax_revenue`); the granular-reject runs **post-flatten** on the aliased id (`nbr_fytd_collected_cr`) using `history`. Don't conflate the two layers.
- `_load_last_good_snapshot` (existing) returns today's snapshot for a non-bad value — that's why the guard uses the new `_prior_good_snapshot` (excludes today).
- Aggregate tests mock the `claude` subprocess — they prove wiring, not real Opus output. Task 6 Step 4 is the only real-output check.
