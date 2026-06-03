# Media Screen — Phase 1 (Detection + Discord ping) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A daily ExonVPS job that screens the BD press for numbers on BB indicators, surfaces only material, period-pinned mismatches into a new `media_review` queue, and pings Adnan one Discord digest. **Detection only — no apply, no approval handling** (those are Phases 2 & 3).

**Architecture:** A new `media_screen` package of small pure-logic modules (filter, catalog, extract, digest) plus an orchestration scraper `scrapers/media_screen.py`. It reuses existing infra: `claude_max.run_max` (extraction), `utils/supabase_reader` (current parsed values), `utils/supabase_writer` (insert queue rows), `utils/notifier.notify` (Discord). EconDelta remains the only writer of `metric_history` — Phase 1 writes only the new `media_review` table.

**Tech Stack:** Python 3.11+ (ruff, line-length 100), pytest, PostgREST (Supabase), the `claude` Max CLI. Tests mock `requests.Session` and patch `run_max` — no live network/LLM in CI.

**Spec:** `docs/superpowers/specs/2026-06-03-media-screen-bb-overrides-design.md` (decisions D1–D6).

---

## File structure (Phase 1)

| File | Responsibility |
|---|---|
| `supabase/migrations/0010_media_review.sql` | The `media_review` queue table + RLS (applied via SQL editor) |
| `media_screen/__init__.py` | Package marker |
| `media_screen/types.py` | `Candidate`, `Extracted`, `MetricSpec` dataclasses (shared, frozen) |
| `media_screen/filter.py` | The flap-killer: classify a parsed-vs-press pair into a `Candidate` or `None` |
| `media_screen/catalog.py` | BB metric catalog (id, press names, tolerance) from `config/sources-v3.json` + overlay |
| `media_screen/extract.py` | LLM extraction of `(indicator, value, period, quote)` from article text |
| `media_screen/digest.py` | Format the Discord digest from a list of candidates |
| `media_screen/dedup.py` | Drop candidates matching open/recently-rejected `media_review` rows |
| `scrapers/media_screen.py` | Orchestration `main()` + `wrap_run` entry point + `--dry-run` |
| `utils/supabase_writer.py` (modify) | Add `insert_media_review_rows(...)` |
| `utils/supabase_reader.py` (modify) | Add `get_open_media_review(...)` |
| `deploy/econdelta-media-screen.{service,timer}` | Daily timer (sign-off gated) |

---

## Task 1: `media_review` migration

**Files:**
- Create: `supabase/migrations/0010_media_review.sql`

> DDL applies via the Supabase SQL editor (Adnan's login) — there is no programmatic DDL path. This task writes the SQL; Adnan runs it; verify via an anon `SELECT`. No pytest.

- [ ] **Step 1: Write the migration**

```sql
-- 0010_media_review.sql — queue + decision record for the daily media screen.
-- Candidates land here as 'pending'; Copotron (Phase 3) flips status; the
-- aggregate (Phase 2) consumes 'approved' rows. Phase 1 only inserts 'pending'.
CREATE TABLE IF NOT EXISTS public.media_review (
    id             bigserial    PRIMARY KEY,
    detected_at    timestamptz  NOT NULL DEFAULT now(),
    metric_id      text         NOT NULL,
    parsed_value   numeric,
    parsed_as_of   date,
    press_value    numeric      NOT NULL,
    press_as_of    date         NOT NULL,
    kind           text         NOT NULL CHECK (kind IN ('fresher_period','same_period_conflict')),
    source_outlet  text,
    source_url     text         NOT NULL,
    source_quote   text,
    confidence     text,
    status         text         NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','approved','rejected','applied','superseded')),
    decided_at     timestamptz,
    decided_by     text,
    applied_at     timestamptz
);

CREATE INDEX IF NOT EXISTS media_review_status_idx ON public.media_review (status);
CREATE INDEX IF NOT EXISTS media_review_metric_idx ON public.media_review (metric_id, press_as_of);

ALTER TABLE public.media_review ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies
    WHERE schemaname='public' AND tablename='media_review' AND policyname='service_role_all') THEN
    CREATE POLICY service_role_all ON public.media_review
      FOR ALL TO service_role USING (true) WITH CHECK (true);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies
    WHERE schemaname='public' AND tablename='media_review' AND policyname='anon_read') THEN
    CREATE POLICY anon_read ON public.media_review FOR SELECT TO anon USING (true);
  END IF;
END $$;
```

- [ ] **Step 2: Hand to Adnan + verify**

Give Adnan the SQL + the SQL-editor link. After he runs it, verify the table exists:
Run: `curl -s "$SUPABASE_URL/rest/v1/media_review?select=id&limit=1" -H "apikey: $ANON_KEY" -H "Authorization: Bearer $ANON_KEY"`
Expected: `[]` (empty array, HTTP 200) — table exists, anon read works.

- [ ] **Step 3: Commit**

```bash
git add supabase/migrations/0010_media_review.sql
git commit -m "feat(media-screen): media_review queue table migration"
```

---

## Task 2: Shared types

**Files:**
- Create: `media_screen/__init__.py` (empty)
- Create: `media_screen/types.py`

- [ ] **Step 1: Write the types**

```python
"""Shared, frozen dataclasses for the media screen."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class MetricSpec:
    metric_id: str            # EconDelta indicator id (alias propagation carries it to brief keys)
    press_names: tuple[str, ...]   # how the press refers to it ("NPL", "default loans", ...)
    tolerance: float          # absolute diff in the metric's unit below which press==parsed


@dataclass(frozen=True)
class Extracted:
    indicator_hint: str       # the press_name the extractor matched
    value: float
    period: date | None       # the reporting period the article states (None if absent)
    quote: str                # the exact sentence
    source_url: str
    source_outlet: str


@dataclass(frozen=True)
class Candidate:
    metric_id: str
    parsed_value: float | None
    parsed_as_of: date | None
    press_value: float
    press_as_of: date
    kind: str                 # 'fresher_period' | 'same_period_conflict'
    source_outlet: str
    source_url: str
    source_quote: str
    confidence: str
```

- [ ] **Step 2: Commit**

```bash
git add media_screen/__init__.py media_screen/types.py
git commit -m "feat(media-screen): shared dataclasses"
```

---

## Task 3: Strict filter + kind derivation (the flap-killer)

**Files:**
- Create: `media_screen/filter.py`
- Test: `tests/test_media_filter.py`

- [ ] **Step 1: Write the failing tests**

```python
from datetime import date

from media_screen.filter import classify
from media_screen.types import Extracted

P_AS_OF = date(2025, 9, 30)


def _ex(value, period):
    return Extracted("NPL", value, period, "quote", "http://x", "tbsnews")


def test_no_period_is_discarded():
    """Strict rule: an undated press number is never a candidate (the old flap)."""
    assert classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(32.26, None), tolerance=0.05) is None


def test_fresher_period_is_a_candidate():
    c = classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(32.26, date(2026, 3, 31)), tolerance=0.05)
    assert c is not None and c.kind == "fresher_period"
    assert c.press_value == 32.26 and c.press_as_of == date(2026, 3, 31)


def test_same_period_material_diff_is_a_conflict():
    c = classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(35.50, P_AS_OF), tolerance=0.05)
    assert c is not None and c.kind == "same_period_conflict"


def test_same_period_within_tolerance_is_not_raised():
    """35.73 vs 35.75 (rounding) at the same period is the same number — no ping."""
    assert classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(35.75, P_AS_OF), tolerance=0.05) is None


def test_no_parsed_value_with_dated_press_is_fresher():
    """Metric never parsed yet, press has a dated value → surface it as fresher_period."""
    c = classify("x", None, None, _ex(10.0, date(2026, 1, 31)), tolerance=0.05)
    assert c is not None and c.kind == "fresher_period"
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_media_filter.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'media_screen.filter'`

- [ ] **Step 3: Implement**

```python
"""Classify a parsed-vs-press pair into a review Candidate, or None.

The strict filter (spec D4): a candidate is emitted only when the press period
is explicit AND the value differs from the parsed value beyond a per-metric
rounding tolerance. This is the flap-killer — undated numbers are discarded.
"""
from __future__ import annotations

from datetime import date

from media_screen.types import Candidate, Extracted


def classify(
    metric_id: str,
    parsed_value: float | None,
    parsed_as_of: date | None,
    ex: Extracted,
    *,
    tolerance: float,
) -> Candidate | None:
    # Rule 1: period MUST be explicit.
    if ex.period is None:
        return None

    # Rule 2 + kind derivation.
    if parsed_as_of is None or ex.period > parsed_as_of:
        kind = "fresher_period"
    elif ex.period == parsed_as_of:
        if parsed_value is not None and abs(ex.value - parsed_value) <= tolerance:
            return None  # same period, within rounding → same number
        kind = "same_period_conflict"
    else:
        return None  # press period is OLDER than what we have → not interesting

    return Candidate(
        metric_id=metric_id,
        parsed_value=parsed_value,
        parsed_as_of=parsed_as_of,
        press_value=ex.value,
        press_as_of=ex.period,
        kind=kind,
        source_outlet=ex.source_outlet,
        source_url=ex.source_url,
        source_quote=ex.quote,
        confidence=f"press={ex.value} @ {ex.period} vs parsed={parsed_value} @ {parsed_as_of}",
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_media_filter.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add media_screen/filter.py tests/test_media_filter.py
git commit -m "feat(media-screen): strict period+materiality filter"
```

---

## Task 4: BB metric catalog

**Files:**
- Create: `media_screen/catalog.py`
- Test: `tests/test_media_catalog.py`

- [ ] **Step 1: Write the failing tests**

```python
from media_screen.catalog import load_catalog
from media_screen.types import MetricSpec


def test_catalog_includes_npl_with_press_names():
    specs = load_catalog()
    npl = next(s for s in specs if s.metric_id == "gross_npl_ratio")
    assert isinstance(npl, MetricSpec)
    assert any("npl" in n.lower() for n in npl.press_names)
    assert npl.tolerance > 0


def test_catalog_only_bb_sourced_metrics():
    """Every spec maps to a real BB indicator id from the config."""
    specs = load_catalog()
    assert len(specs) >= 5
    assert all(s.metric_id and s.press_names for s in specs)
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_media_catalog.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'media_screen.catalog'`

- [ ] **Step 3: Implement**

```python
"""The BB metric catalog the screen matches press numbers against.

Spec D2 says screen "everything", but a press number is only actionable if we
know which metric_id it maps to and a sensible rounding tolerance. This overlay
seeds the headline figures the press actually prints; extend it as real
candidate volume reveals more. metric_id is the EconDelta indicator id — alias
propagation (PR #65) carries an approved override to the brief keys.
"""
from __future__ import annotations

from media_screen.types import MetricSpec

# (metric_id, press_names, tolerance-in-unit)
_CATALOG: tuple[MetricSpec, ...] = (
    MetricSpec("gross_npl_ratio", ("NPL ratio", "non-performing loan", "default loan"), 0.05),
    MetricSpec("banking_sector_crar", ("CAR", "CRAR", "capital adequacy"), 0.05),
    MetricSpec("fx_reserve_gross_and_bpm6", ("gross reserves", "forex reserves", "foreign exchange reserves"), 0.05),
    MetricSpec("point_to_point_inflation", ("inflation", "point-to-point inflation", "CPI"), 0.05),
    MetricSpec("private_sector_credit_yoy_pct", ("private sector credit growth", "credit growth"), 0.05),
)


def load_catalog() -> list[MetricSpec]:
    """Return the BB metrics the screen covers, with press aliases + tolerances."""
    return list(_CATALOG)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_media_catalog.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add media_screen/catalog.py tests/test_media_catalog.py
git commit -m "feat(media-screen): BB metric catalog with press aliases + tolerances"
```

---

## Task 5: LLM extraction

**Files:**
- Create: `media_screen/extract.py`
- Test: `tests/test_media_extract.py`

- [ ] **Step 1: Write the failing tests** (patch the LLM, like `tests/test_hybrid.py`)

```python
from datetime import date
from unittest.mock import patch

from media_screen.extract import extract_numbers
from media_screen.types import MetricSpec

SPECS = [MetricSpec("gross_npl_ratio", ("NPL ratio", "default loan"), 0.05)]


def test_extracts_value_and_period():
    fake = type("R", (), {"parsed": {"findings": [
        {"press_name": "NPL ratio", "value": 32.26, "period": "2026-03-31",
         "quote": "NPLs were 32.26% as of end-March 2026."}
    ]}, "raw_text": ""})()
    with patch("media_screen.extract.run_max", return_value=fake):
        out = extract_numbers("article text", specs=SPECS,
                              source_url="http://x", source_outlet="tbsnews")
    assert len(out) == 1
    assert out[0].value == 32.26 and out[0].period == date(2026, 3, 31)
    assert out[0].indicator_hint == "NPL ratio"


def test_undated_finding_keeps_period_none():
    fake = type("R", (), {"parsed": {"findings": [
        {"press_name": "NPL ratio", "value": 32.26, "period": None, "quote": "NPLs rose."}
    ]}, "raw_text": ""})()
    with patch("media_screen.extract.run_max", return_value=fake):
        out = extract_numbers("t", specs=SPECS, source_url="http://x", source_outlet="tbs")
    assert out[0].period is None  # downstream filter discards it


def test_llm_error_returns_empty(caplog):
    from claude_max.max_client import MaxCallError
    with patch("media_screen.extract.run_max", side_effect=MaxCallError("boom")):
        out = extract_numbers("t", specs=SPECS, source_url="http://x", source_outlet="tbs")
    assert out == []  # screen fails safe — no candidates
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_media_extract.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'media_screen.extract'`

- [ ] **Step 3: Implement**

```python
"""Extract (indicator, value, period, quote) triples from press article text.

Uses the Max CLI. Best-effort: any LLM/parse error yields [] so the screen
never breaks (spec §9). The prompt forces an explicit period or null — the
downstream strict filter discards null-period findings.
"""
from __future__ import annotations

import logging
from datetime import date

from claude_max.max_client import MaxCallError, run_max
from media_screen.types import Extracted, MetricSpec

logger = logging.getLogger("media_extract")

_PROMPT = """You extract Bangladesh-economy figures from a news article for a banking desk.

For EACH of these indicators, if the article states a number for it, return one finding:
{names}

Rules:
- "period" MUST be the explicit reporting date the article gives (ISO YYYY-MM-DD,
  using the last day of the stated month/quarter). If the article does not state a
  clear period for the number, set "period" to null. NEVER guess a period.
- "value" is the bare number (percent as a number, e.g. 32.26).
- "quote" is the exact sentence containing the number.
Return JSON ONLY: {{"findings": [{{"press_name": "...", "value": 0.0, "period": "YYYY-MM-DD"|null, "quote": "..."}}]}}

ARTICLE:
{text}
"""


def _parse_period(raw) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except (ValueError, TypeError):
        return None


def extract_numbers(
    text: str, *, specs: list[MetricSpec], source_url: str, source_outlet: str,
) -> list[Extracted]:
    names = "\n".join(f"- {n}" for s in specs for n in s.press_names)
    prompt = _PROMPT.format(names=names, text=text[:20000])
    try:
        result = run_max(prompt=prompt, effort="high")
    except MaxCallError as e:
        logger.warning("media extract LLM failed for %s: %s", source_url, e)
        return []
    findings = (result.parsed or {}).get("findings") or []
    out: list[Extracted] = []
    for f in findings:
        try:
            value = float(f["value"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append(Extracted(
            indicator_hint=str(f.get("press_name", "")),
            value=value,
            period=_parse_period(f.get("period")),
            quote=str(f.get("quote", "")),
            source_url=source_url,
            source_outlet=source_outlet,
        ))
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_media_extract.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add media_screen/extract.py tests/test_media_extract.py
git commit -m "feat(media-screen): LLM extraction of dated press figures"
```

---

## Task 6: Digest formatter

**Files:**
- Create: `media_screen/digest.py`
- Test: `tests/test_media_digest.py`

- [ ] **Step 1: Write the failing tests**

```python
from datetime import date

from media_screen.digest import format_digest
from media_screen.types import Candidate


def _cand():
    return Candidate("gross_npl_ratio", 35.73, date(2025, 9, 30), 32.26, date(2026, 3, 31),
                     "fresher_period", "tbsnews", "http://x", "NPLs 32.26% end-March 2026", "c")


def test_digest_empty_returns_none():
    assert format_digest([]) is None


def test_digest_lists_each_candidate():
    title, message, fields = format_digest([_cand()])
    assert "1" in title
    assert "gross_npl_ratio" in message and "32.26" in message and "2026-03-31" in message
    assert "reply" in message.lower()  # tells Adnan how to act (approve N / reject N)
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_media_digest.py -q`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
"""Format a Discord digest from review candidates for utils.notifier.notify()."""
from __future__ import annotations

from media_screen.types import Candidate


def format_digest(candidates: list[Candidate]) -> tuple[str, str, dict] | None:
    """Return (title, message, fields) for notify(), or None if nothing to report."""
    if not candidates:
        return None
    n = len(candidates)
    title = f"Media screen: {n} candidate{'s' if n != 1 else ''} for review"
    lines = [
        f"**{i+1}. {c.metric_id}** [{c.kind}] — press **{c.press_value}** @ {c.press_as_of} "
        f"vs parsed {c.parsed_value} @ {c.parsed_as_of}\n_{c.source_quote}_ <{c.source_url}>"
        for i, c in enumerate(candidates)
    ]
    message = (
        "\n\n".join(lines)
        + "\n\nReply `approve N` or `reject N` (N = the number above)."
    )
    fields = {c.metric_id: f"{c.press_value} @ {c.press_as_of}" for c in candidates[:10]}
    return title, message, fields
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_media_digest.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add media_screen/digest.py tests/test_media_digest.py
git commit -m "feat(media-screen): Discord digest formatter"
```

---

## Task 7: Dedup + queue writer/reader

**Files:**
- Modify: `utils/supabase_writer.py` (add `insert_media_review_rows`)
- Modify: `utils/supabase_reader.py` (add `get_open_media_review`)
- Create: `media_screen/dedup.py`
- Test: `tests/test_media_review_io.py`

- [ ] **Step 1: Write the failing tests**

```python
from datetime import date
from unittest.mock import MagicMock

import requests

from media_screen.dedup import drop_already_open
from media_screen.types import Candidate
from utils.supabase_writer import insert_media_review_rows


def _cand(metric="gross_npl_ratio", as_of=date(2026, 3, 31)):
    return Candidate(metric, 35.73, date(2025, 9, 30), 32.26, as_of,
                     "fresher_period", "tbs", "http://x", "q", "c")


def test_dedup_drops_candidate_matching_open_row():
    open_rows = [{"metric_id": "gross_npl_ratio", "press_as_of": "2026-03-31", "status": "pending"}]
    assert drop_already_open([_cand()], open_rows) == []


def test_dedup_keeps_new_candidate():
    open_rows = [{"metric_id": "gross_npl_ratio", "press_as_of": "2025-12-31", "status": "pending"}]
    assert len(drop_already_open([_cand()], open_rows)) == 1


def test_insert_posts_pending_rows():
    sess = MagicMock(spec=requests.Session)
    resp = MagicMock(); resp.status_code = 201; resp.text = ""
    sess.post.return_value = resp
    n = insert_media_review_rows([_cand()], url="https://x.supabase.co",
                                 service_key="sk", session=sess)
    assert n == 1
    body = sess.post.call_args[1]["json"][0]
    assert body["metric_id"] == "gross_npl_ratio" and body["status"] == "pending"
    assert body["press_as_of"] == "2026-03-31" and body["kind"] == "fresher_period"
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_media_review_io.py -q`
Expected: FAIL — import errors (`insert_media_review_rows`, `drop_already_open` not defined)

- [ ] **Step 3a: Implement `media_screen/dedup.py`**

```python
"""Drop candidates that already have an open (pending) or just-rejected row, so
the same article doesn't re-ping day after day."""
from __future__ import annotations

from media_screen.types import Candidate


def drop_already_open(candidates: list[Candidate], open_rows: list[dict]) -> list[Candidate]:
    seen = {(r["metric_id"], str(r["press_as_of"])[:10]) for r in open_rows}
    return [c for c in candidates if (c.metric_id, c.press_as_of.isoformat()) not in seen]
```

- [ ] **Step 3b: Add `insert_media_review_rows` to `utils/supabase_writer.py`**

Append (reuses `_resolve_credentials`, `SupabaseWriteError`, `_DEFAULT_TIMEOUT`):

```python
def insert_media_review_rows(candidates, *, url=None, service_key=None,
                             timeout=_DEFAULT_TIMEOUT, session=None) -> int:
    """Insert review Candidates as status='pending' rows into media_review.

    Returns count inserted (0 if empty). Raises SupabaseWriteError on non-2xx.
    """
    if not candidates:
        return 0
    base_url, key = _resolve_credentials(url, service_key)
    rows = [{
        "metric_id": c.metric_id,
        "parsed_value": c.parsed_value,
        "parsed_as_of": c.parsed_as_of.isoformat() if c.parsed_as_of else None,
        "press_value": c.press_value,
        "press_as_of": c.press_as_of.isoformat(),
        "kind": c.kind,
        "source_outlet": c.source_outlet,
        "source_url": c.source_url,
        "source_quote": c.source_quote,
        "confidence": c.confidence,
        "status": "pending",
    } for c in candidates]
    endpoint = f"{base_url}/rest/v1/media_review"
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Content-Type": "application/json", "Prefer": "return=minimal"}
    sess = session or requests.Session()
    try:
        resp = sess.post(endpoint, json=rows, headers=headers, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise SupabaseWriteError(f"media_review insert network error: {e}") from e
    if resp.status_code not in (200, 201, 204):
        raise SupabaseWriteError(f"media_review insert HTTP {resp.status_code}: {resp.text[:200]}")
    return len(rows)
```

- [ ] **Step 3c: Add `get_open_media_review` to `utils/supabase_reader.py`**

Append (reuses `_get`):

```python
def get_open_media_review(*, url: str | None = None, key: str | None = None,
                          session: "requests.Session | None" = None) -> list[dict[str, Any]]:
    """Rows still pending or recently rejected — used to dedup new candidates."""
    return _get(
        "media_review?select=metric_id,press_as_of,status&status=in.(pending,rejected)",
        url=url, key=key, session=session,
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_media_review_io.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add media_screen/dedup.py utils/supabase_writer.py utils/supabase_reader.py tests/test_media_review_io.py
git commit -m "feat(media-screen): media_review insert/read + dedup"
```

---

## Task 8: Orchestration scraper + dry-run

**Files:**
- Create: `scrapers/media_screen.py`
- Test: `tests/test_media_screen.py`

- [ ] **Step 1: Write the failing test** (mock fetch + extract + IO; assert candidates reach the writer)

```python
from datetime import date
from unittest.mock import patch

from media_screen.types import Candidate, Extracted


def _ex():
    return Extracted("NPL ratio", 32.26, date(2026, 3, 31), "q", "http://x", "tbs")


def test_run_screen_inserts_filtered_candidates(monkeypatch):
    import scrapers.media_screen as ms
    monkeypatch.setattr(ms, "_collect_articles", lambda specs: [("text", "http://x", "tbs")])
    monkeypatch.setattr(ms, "extract_numbers", lambda *a, **k: [_ex()])
    # parsed value older + different → fresher_period candidate
    monkeypatch.setattr(ms, "_parsed_for", lambda mid: (35.73, date(2025, 9, 30)))
    monkeypatch.setattr(ms, "get_open_media_review", lambda **k: [])
    captured = {}
    monkeypatch.setattr(ms, "insert_media_review_rows",
                        lambda cands, **k: captured.setdefault("c", cands) or len(cands))
    monkeypatch.setattr(ms, "notify", lambda *a, **k: True)
    rc = ms.run_screen(dry_run=False)
    assert rc == 0
    assert len(captured["c"]) == 1 and captured["c"][0].kind == "fresher_period"


def test_dry_run_does_not_insert(monkeypatch):
    import scrapers.media_screen as ms
    monkeypatch.setattr(ms, "_collect_articles", lambda specs: [("text", "http://x", "tbs")])
    monkeypatch.setattr(ms, "extract_numbers", lambda *a, **k: [_ex()])
    monkeypatch.setattr(ms, "_parsed_for", lambda mid: (35.73, date(2025, 9, 30)))
    monkeypatch.setattr(ms, "get_open_media_review", lambda **k: [])
    called = {"insert": False}
    monkeypatch.setattr(ms, "insert_media_review_rows",
                        lambda *a, **k: called.update(insert=True))
    monkeypatch.setattr(ms, "notify", lambda *a, **k: True)
    rc = ms.run_screen(dry_run=True)
    assert rc == 0 and called["insert"] is False
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_media_screen.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'scrapers.media_screen'`

- [ ] **Step 3: Implement**

```python
"""Daily media screen — detection only (Phase 1).

Collects press articles, extracts dated figures via the Max CLI, compares each
to the currently-parsed value, applies the strict filter, dedups against open
review rows, inserts survivors as 'pending', and pings one Discord digest.
Writes ONLY media_review — never metric_history (Phases 2/3 handle apply).
"""
from __future__ import annotations

import argparse
import logging
import sys

from media_screen.catalog import load_catalog
from media_screen.dedup import drop_already_open
from media_screen.digest import format_digest
from media_screen.extract import extract_numbers
from media_screen.filter import classify
from utils.notifier import notify
from utils.supabase_reader import SupabaseReadError, get_metric_history, get_open_media_review
from utils.supabase_writer import insert_media_review_rows

logger = logging.getLogger("media_screen")

# Press outlets to sweep. (Phase 1 seed; extend in config later.)
_OUTLETS = ("thedailystar.net", "tbsnews.net", "dhakatribune.com")


def _collect_articles(specs):
    """Return [(text, url, outlet)] for the day's relevant press articles.

    Reuses fetchers/news_article_discovery + the HTML fetcher. Network-bound, so
    monkeypatched in tests. Best-effort: a fetch failure for one outlet is logged
    and skipped (the screen must never crash on a bad source).
    """
    # Implementation reuses fetchers.news_article_discovery.discover_latest_article_link
    # + the existing HTML fetcher per outlet; omitted here for brevity in the test
    # path (covered by monkeypatch). Build it against the real fetchers in this step.
    raise NotImplementedError  # replaced by the real fetch loop during implementation


def _parsed_for(metric_id: str):
    """Return (value, as_of) of the current get_latest, or (None, None)."""
    try:
        rows = get_metric_history(metric_id, days=1)
    except SupabaseReadError as e:
        logger.warning("could not read parsed value for %s: %s", metric_id, e)
        return None, None
    if not rows:
        return None, None
    from datetime import date as _date
    return float(rows[0]["value"]), _date.fromisoformat(str(rows[0]["as_of"])[:10])


def run_screen(*, dry_run: bool) -> int:
    specs = load_catalog()
    by_name = {n.lower(): s for s in specs for n in s.press_names}
    candidates = []
    for text, url, outlet in _collect_articles(specs):
        for ex in extract_numbers(text, specs=specs, source_url=url, source_outlet=outlet):
            spec = by_name.get(ex.indicator_hint.lower())
            if spec is None:
                continue
            parsed_value, parsed_as_of = _parsed_for(spec.metric_id)
            c = classify(spec.metric_id, parsed_value, parsed_as_of, ex, tolerance=spec.tolerance)
            if c is not None:
                candidates.append(c)

    candidates = drop_already_open(candidates, get_open_media_review())
    digest = format_digest(candidates)

    if dry_run:
        for c in candidates:
            print(f"[DRY-RUN] {c.metric_id} {c.kind} press={c.press_value}@{c.press_as_of}")
        logger.info("dry-run: %d candidate(s), no insert/notify", len(candidates))
        return 0

    if candidates:
        insert_media_review_rows(candidates)
    if digest is not None:
        notify("warning", digest[0], digest[1], fields=digest[2])
    logger.info("media screen: %d candidate(s) inserted", len(candidates))
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return run_screen(dry_run=args.dry_run)


if __name__ == "__main__":
    from utils.supabase_writer import wrap_run
    sys.exit(wrap_run("media_screen", "econdelta-media-screen.service", main))
```

> **Implementation note for Step 3:** flesh out `_collect_articles` against the real
> `fetchers.news_article_discovery` + HTML fetcher (one discovery + fetch per outlet,
> wrapped in try/except → skip-and-log). The two tests monkeypatch it, so they pass
> without network; add a separate live `--dry-run` smoke run on the VPS before the timer.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_media_screen.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Full suite + lint**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/ruff check .`
Expected: all pass, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add scrapers/media_screen.py tests/test_media_screen.py
git commit -m "feat(media-screen): orchestration scraper + dry-run (detection only)"
```

---

## Task 9: Deploy timer (SIGN-OFF GATED — do not enable without Adnan)

**Files:**
- Create: `deploy/econdelta-media-screen.service`
- Create: `deploy/econdelta-media-screen.timer`

> Per VISION.md, `deploy/` changes need sign-off. Per AGENTS landmine 5, a new timer file is glob-copied but NOT enabled unless added to `install.sh`'s hardcoded enable-loop. **Stop at a live `--dry-run` for Adnan's sign-off before enabling the prod timer** (the F4 pattern).

- [ ] **Step 1: Write the unit files** (model on `deploy/econdelta-dse-dayend.{service,timer}`; `EnvironmentFile=/etc/econdelta.env`, `ExecStart=.../.venv/bin/python -m scrapers.media_screen`, `OnCalendar` evening BDT e.g. `15:30 UTC` = 21:30 BDT, after the press publishes).

- [ ] **Step 2: Live dry-run on the VPS for sign-off**

Run on the box: `set -a; . /etc/econdelta.env; set +a; ./.venv/bin/python -m scrapers.media_screen --dry-run`
Expected: prints any candidates; **no** Supabase insert; **no** Discord ping. Adnan reviews the candidate quality before the timer is enabled.

- [ ] **Step 3: Commit (units only; enabling is a separate signed-off step)**

```bash
git add deploy/econdelta-media-screen.service deploy/econdelta-media-screen.timer
git commit -m "feat(media-screen): systemd units (timer disabled pending sign-off)"
```

---

## Self-Review (run against the spec)

**Spec coverage:** D1 (both kinds) → Task 3 `classify` derives both. D2 (all metrics) → Task 4 catalog (seeded headline set, extensible) + extract-then-match. D3 (reply-to-Copotron) → Phase 3 (out of scope; digest tells Adnan the syntax). D4 (strict) → Task 3 period-required + tolerance, Task 7 dedup. D5 (EconDelta-only writer) → Phase 1 writes only `media_review`. D6 (supersede) → Phases 2 (apply). Detection-only scope honored — no apply in Phase 1.

**Placeholder scan:** the only deliberate "build it here" is `_collect_articles` (network glue), flagged with an explicit implementation note + a live dry-run gate; its consumers are fully tested via monkeypatch. No TBD/TODO elsewhere.

**Type consistency:** `Candidate`/`Extracted`/`MetricSpec` defined in Task 2, used identically in Tasks 3–8. `classify(...)` signature matches its call in Task 8. `insert_media_review_rows(candidates,...)` / `get_open_media_review()` / `drop_already_open(...)` signatures match their Task-8 calls.

**Out of scope (Phases 2 & 3, separate plans):** the apply path in `aggregate_latest` + supersede release predicate; the Copotron `approve N`/`reject N` handler.
