# Media Screen — Phase 2 (Apply + Supersede) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]` checkboxes.

**Goal:** Make EconDelta's `aggregate` apply **approved** `media_review` rows to `metric_history` at the press's reporting period, and automatically retire each override once BB's own pipeline has fresher data for that metric (spec D6).

**Architecture:** A new apply pass `_apply_media_overrides(...)` runs in `aggregate_latest.main()` **after** the normal `upsert_metric_history` (so an approved press value wins until superseded). EconDelta remains the sole `metric_history` writer. The override write reuses `_apply_brief_aliases` so it reaches the brief keys the SPA reads (PR #65 mechanism).

**Tech Stack:** Python 3.11+, pytest (mock `requests.Session`), PostgREST. Builds on Phase 1's `media_review` table + `media_screen` package.

**Spec:** `docs/superpowers/specs/2026-06-03-media-screen-bb-overrides-design.md` §5, §7, D6.

---

## File structure (Phase 2)

| File | Responsibility |
|---|---|
| `utils/supabase_reader.py` (modify) | `get_active_media_review()` — approved/applied overrides |
| `utils/supabase_writer.py` (modify) | `set_media_review_status(id, status, applied=)` — flip a row's status |
| `media_screen/supersede.py` (create) | `is_superseded(...)` — pure rule for retire vs re-assert |
| `aggregate_latest.py` (modify) | `_apply_media_overrides(...)` + call it after the normal upsert |

---

## Task 1: Override reader + status updater

**Files:**
- Modify: `utils/supabase_reader.py` (append `get_active_media_review`)
- Modify: `utils/supabase_writer.py` (append `set_media_review_status`)
- Test: `tests/test_media_review_status_io.py`

- [ ] **Step 1: Write the failing tests**

```python
from unittest.mock import MagicMock

import requests

from utils.supabase_reader import get_active_media_review
from utils.supabase_writer import set_media_review_status


def _read_session(rows):
    sess = MagicMock(spec=requests.Session)
    resp = MagicMock(); resp.status_code = 200; resp.json.return_value = rows
    sess.get.return_value = resp
    return sess


def test_get_active_filters_to_approved_and_applied():
    sess = _read_session([{"id": 1, "metric_id": "gross_npl_ratio", "status": "approved"}])
    out = get_active_media_review(url="https://x.supabase.co", key="sk", session=sess)
    assert out and out[0]["metric_id"] == "gross_npl_ratio"
    called = sess.get.call_args[0][0]
    assert "status=in.(approved,applied)" in called


def test_set_status_patches_row():
    sess = MagicMock(spec=requests.Session)
    resp = MagicMock(); resp.status_code = 204; resp.text = ""
    sess.patch.return_value = resp
    set_media_review_status(7, "applied", applied=True,
                            url="https://x.supabase.co", service_key="sk", session=sess)
    url = sess.patch.call_args[0][0]
    body = sess.patch.call_args[1]["json"]
    assert "id=eq.7" in url and body["status"] == "applied" and "applied_at" in body


def test_set_status_without_applied_omits_timestamp():
    sess = MagicMock(spec=requests.Session)
    resp = MagicMock(); resp.status_code = 204; resp.text = ""
    sess.patch.return_value = resp
    set_media_review_status(3, "superseded", url="https://x.supabase.co",
                            service_key="sk", session=sess)
    assert sess.patch.call_args[1]["json"] == {"status": "superseded"}
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_media_review_status_io.py -q`
Expected: FAIL — import errors.

- [ ] **Step 3a: Append to `utils/supabase_reader.py`**

```python
def get_active_media_review(*, url: str | None = None, key: str | None = None,
                            session: "requests.Session | None" = None) -> list[dict[str, Any]]:
    """Approved or already-applied media overrides — the apply pass re-asserts
    these each aggregate run and checks whether BB's pipeline has superseded them."""
    return _get(
        "media_review?select=id,metric_id,parsed_value,parsed_as_of,press_value,"
        "press_as_of,kind,source_outlet,status&status=in.(approved,applied)",
        url=url, key=key, session=session,
    )
```

- [ ] **Step 3b: Append to `utils/supabase_writer.py`**

```python
def set_media_review_status(review_id, status, *, applied: bool = False,
                            url=None, service_key=None, timeout=_DEFAULT_TIMEOUT, session=None) -> None:
    """PATCH one media_review row's status (+ applied_at when applied=True).
    Raises SupabaseWriteError on non-2xx."""
    base_url, key = _resolve_credentials(url, service_key)
    payload: dict = {"status": status}
    if applied:
        from datetime import datetime, timezone
        payload["applied_at"] = datetime.now(timezone.utc).isoformat()
    endpoint = f"{base_url}/rest/v1/media_review?id=eq.{int(review_id)}"
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Content-Type": "application/json", "Prefer": "return=minimal"}
    sess = session or requests.Session()
    try:
        resp = sess.patch(endpoint, json=payload, headers=headers, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise SupabaseWriteError(f"media_review status patch network error: {e}") from e
    if resp.status_code not in (200, 204):
        raise SupabaseWriteError(f"media_review status patch HTTP {resp.status_code}: {resp.text[:200]}")
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_media_review_status_io.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add utils/supabase_reader.py utils/supabase_writer.py tests/test_media_review_status_io.py
git commit -m "feat(media-screen): active-override reader + status updater"
```

---

## Task 2: Supersede decision (pure)

**Files:**
- Create: `media_screen/supersede.py`
- Test: `tests/test_media_supersede.py`

- [ ] **Step 1: Write the failing tests**

```python
from datetime import date

from media_screen.supersede import is_superseded

PRESS = date(2026, 3, 31)


def test_fresher_not_superseded_while_bb_lags():
    assert is_superseded(kind="fresher_period", press_as_of=PRESS, parsed_baseline=35.73,
                         automated_value=35.73, automated_as_of=date(2025, 9, 30)) is False


def test_fresher_superseded_when_bb_reaches_period():
    assert is_superseded(kind="fresher_period", press_as_of=PRESS, parsed_baseline=35.73,
                         automated_value=31.0, automated_as_of=date(2026, 3, 31)) is True


def test_same_period_held_while_bb_value_unchanged():
    assert is_superseded(kind="same_period_conflict", press_as_of=PRESS, parsed_baseline=35.73,
                         automated_value=35.73, automated_as_of=PRESS) is False


def test_same_period_superseded_when_bb_revises():
    assert is_superseded(kind="same_period_conflict", press_as_of=PRESS, parsed_baseline=35.73,
                         automated_value=34.10, automated_as_of=PRESS) is True


def test_no_automated_data_means_not_superseded():
    assert is_superseded(kind="fresher_period", press_as_of=PRESS, parsed_baseline=None,
                         automated_value=None, automated_as_of=None) is False
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_media_supersede.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
"""When does an approved media override yield to BB's own pipeline?

Spec D6: any later BB parse — including the SAME period — supersedes the
human-approved press value. The override is a temporary bridge:
  - fresher_period: BB has caught up once its parsed source_as_of reaches (or
    passes) the period the press front-ran.
  - same_period_conflict: BB has genuinely revised once its parsed value for
    that period moves off the baseline that was current at approval. The daily
    re-emission of the IDENTICAL figure is not a revision (that would flap).
"""
from __future__ import annotations

from datetime import date


def is_superseded(
    *,
    kind: str,
    press_as_of: date,
    parsed_baseline: float | None,
    automated_value: float | None,
    automated_as_of: date | None,
    epsilon: float = 1e-9,
) -> bool:
    if kind == "fresher_period":
        return automated_as_of is not None and automated_as_of >= press_as_of
    if kind == "same_period_conflict":
        if automated_value is None or parsed_baseline is None:
            return False
        return abs(automated_value - parsed_baseline) > epsilon
    return False
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_media_supersede.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add media_screen/supersede.py tests/test_media_supersede.py
git commit -m "feat(media-screen): supersede decision (bridge until BB catches up)"
```

---

## Task 3: Apply pass in aggregate

**Files:**
- Modify: `aggregate_latest.py` (add `_apply_media_overrides` + call it after the upsert at ~line 1016)
- Test: `tests/test_media_apply.py`

- [ ] **Step 1: Write the failing tests** (mock reader/writer/status — no live Supabase)

```python
from datetime import date
from unittest.mock import MagicMock

import aggregate_latest as agg


def _override(kind, press_as_of, status="approved", metric="gross_npl_ratio",
              press_value=32.26, parsed_value=35.73):
    return {"id": 9, "metric_id": metric, "parsed_value": parsed_value, "parsed_as_of": "2025-09-30",
            "press_value": press_value, "press_as_of": press_as_of.isoformat(), "kind": kind,
            "source_outlet": "tbsnews", "status": status}


def test_fresher_override_is_written_at_press_period():
    writer, set_status = MagicMock(), MagicMock()
    reader = MagicMock(return_value=[_override("fresher_period", date(2026, 3, 31))])
    # automated pipeline still on the old quarter → NOT superseded
    agg._apply_media_overrides({"gross_npl_ratio": 35.73}, {"gross_npl_ratio": date(2025, 9, 30)},
                               writer=writer, reader=reader, set_status=set_status)
    kwargs = writer.call_args[1]
    assert kwargs["as_of"] == date(2026, 3, 31)
    assert kwargs["source"].startswith("media-approved")
    assert kwargs["data"]["gross_npl_ratio"] == 32.26
    assert "banking_npl_pct" in kwargs["data"]   # alias propagation reached the brief key
    set_status.assert_called_once()
    assert set_status.call_args[0][1] == "applied"


def test_fresher_override_superseded_when_bb_catches_up():
    writer, set_status = MagicMock(), MagicMock()
    reader = MagicMock(return_value=[_override("fresher_period", date(2026, 3, 31), status="applied")])
    # automated pipeline now ON the press period → superseded
    agg._apply_media_overrides({"gross_npl_ratio": 31.0}, {"gross_npl_ratio": date(2026, 3, 31)},
                               writer=writer, reader=reader, set_status=set_status)
    writer.assert_not_called()
    assert set_status.call_args[0][1] == "superseded"


def test_same_period_held_then_superseded_on_revision():
    writer, set_status = MagicMock(), MagicMock()
    held = MagicMock(return_value=[_override("same_period_conflict", date(2025, 9, 30))])
    agg._apply_media_overrides({"gross_npl_ratio": 35.73}, {"gross_npl_ratio": date(2025, 9, 30)},
                               writer=writer, reader=held, set_status=set_status)
    writer.assert_called_once()  # held: press value written
    writer.reset_mock(); set_status.reset_mock()
    revised = MagicMock(return_value=[_override("same_period_conflict", date(2025, 9, 30), status="applied")])
    agg._apply_media_overrides({"gross_npl_ratio": 34.10}, {"gross_npl_ratio": date(2025, 9, 30)},
                               writer=writer, reader=revised, set_status=set_status)
    writer.assert_not_called()
    assert set_status.call_args[0][1] == "superseded"
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_media_apply.py -q`
Expected: FAIL — `AttributeError: module 'aggregate_latest' has no attribute '_apply_media_overrides'`

- [ ] **Step 3a: Add the function to `aggregate_latest.py`**

Add near `_build_source_as_of_map` (imports at top of file: `from media_screen.supersede import is_superseded`; the reader/writer funcs are already importable from `utils`):

```python
def _apply_media_overrides(
    data, source_as_of_map, *,
    writer=None, reader=None, set_status=None,
):
    """Re-assert approved media overrides into metric_history AFTER the normal
    upsert, so a human-approved press value wins until BB's pipeline supersedes
    it (spec D6). EconDelta stays the sole writer; the override write reuses
    _apply_brief_aliases so it reaches the brief keys. Best-effort."""
    from datetime import date as _date

    from media_screen.supersede import is_superseded
    from utils.supabase_reader import get_active_media_review
    from utils.supabase_writer import (
        SupabaseWriteError, set_media_review_status, upsert_metric_history,
    )
    writer = writer or upsert_metric_history
    reader = reader or get_active_media_review
    set_status = set_status or set_media_review_status

    try:
        rows = reader()
    except Exception as e:  # noqa: BLE001 — overrides must never break aggregate
        logger.warning("media overrides: could not read active rows: %s", e)
        return

    for r in rows:
        mid = r["metric_id"]
        press_as_of = _date.fromisoformat(str(r["press_as_of"])[:10])
        automated_value = data.get(mid)
        automated_value = float(automated_value) if isinstance(automated_value, (int, float)) else None
        parsed_baseline = float(r["parsed_value"]) if r.get("parsed_value") is not None else None
        if is_superseded(kind=r["kind"], press_as_of=press_as_of, parsed_baseline=parsed_baseline,
                         automated_value=automated_value, automated_as_of=source_as_of_map.get(mid)):
            set_status(r["id"], "superseded")
            logger.info("media override %s (%s @ %s) superseded by BB", r["id"], mid, press_as_of)
            continue
        override_data = {mid: float(r["press_value"])}
        _apply_brief_aliases(override_data)
        try:
            writer(data=override_data, as_of=press_as_of,
                   source=f"media-approved:{r.get('source_outlet') or 'press'}")
        except SupabaseWriteError as e:
            logger.warning("media override write failed for %s: %s", mid, e)
            continue
        if r["status"] == "approved":
            set_status(r["id"], "applied", applied=True)
        logger.info("media override applied: %s = %s @ %s", mid, r["press_value"], press_as_of)
```

- [ ] **Step 3b: Call it in `main()` after the normal upsert**

In the Supabase-write block (right after the `upsert_metric_history(...)` + its log line, inside the same `if not ECONDELTA_SKIP_SUPABASE`-style guard), add:

```python
            _apply_media_overrides(data, source_as_of_map)
```

- [ ] **Step 4: Run to verify pass + full suite + ruff**

Run: `.venv/bin/python -m pytest tests/test_media_apply.py -q && .venv/bin/python -m pytest tests/ -q && .venv/bin/ruff check .`
Expected: all PASS, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add aggregate_latest.py tests/test_media_apply.py
git commit -m "feat(media-screen): apply approved overrides + supersede in aggregate"
```

---

## Self-Review (against spec)

**Spec coverage:** §7 apply-at-press-period → Task 3 writer `as_of=press_as_of`, `source='media-approved:<outlet>'`. Alias reach → `_apply_brief_aliases(override_data)`. D6 supersede (later period OR same-period revision) → Task 2 `is_superseded` + Task 3 retire branch. EconDelta sole writer → override goes through `upsert_metric_history`. Best-effort/fail-safe → broad except on the reader, per-row except on the writer.

**Type consistency:** `is_superseded(kind, press_as_of, parsed_baseline, automated_value, automated_as_of)` matches its Task-3 call. `get_active_media_review()` / `set_media_review_status(id, status, applied=)` match. `_apply_media_overrides(data, source_as_of_map)` matches the main() call.

**Out of scope:** Phase 3 (Copotron flips `approved`/`rejected`; this phase only consumes `approved` and emits `applied`/`superseded`).
