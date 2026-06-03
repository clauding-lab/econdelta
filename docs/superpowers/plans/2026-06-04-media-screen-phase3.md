# Media Screen — Phase 3 (Approve/Reject decision) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]` checkboxes.

**Goal:** The primitive that flips a `media_review` row to `approved`/`rejected` when you decide via Copotron — race-safe (only flips a still-`pending` row), records who/when, and is the canonical, tested decision path. Copotron (Hetzner) calls the equivalent Supabase PATCH; the wiring recipe is `docs/media-screen-copotron-wiring.md`.

**Architecture:** `utils/supabase_writer.decide_media_review(...)` does a conditional PATCH (`id=eq.N & status=eq.pending`) so a repeat/already-decided row is a no-op. `media_screen/decide.py` wraps it with a friendly result + a CLI. Approved rows are picked up by EconDelta's Phase-2 apply pass on the next aggregate run; rejected rows are discarded (no `metric_history` change).

**Tech Stack:** Python 3.11+, pytest (mock `requests.Session`), PostgREST. Builds on Phase 2.

**Spec:** `docs/superpowers/specs/2026-06-03-media-screen-bb-overrides-design.md` D3, §8.

---

## File structure (Phase 3)

| File | Responsibility |
|---|---|
| `utils/supabase_writer.py` (modify) | `decide_media_review(id, decision, actor=)` — conditional approve/reject PATCH |
| `media_screen/decide.py` (create) | `apply_decision(...)` result wrapper + `python -m media_screen.decide` CLI |

---

## Task 1: `decide_media_review` (conditional approve/reject PATCH)

**Files:**
- Modify: `utils/supabase_writer.py` (append `decide_media_review` + `_DECISION_STATUS`)
- Test: `tests/test_media_decide_io.py`

- [ ] **Step 1: Write the failing tests**

```python
from unittest.mock import MagicMock

import pytest
import requests

from utils.supabase_writer import decide_media_review


def _session(returned_rows, status=200):
    sess = MagicMock(spec=requests.Session)
    resp = MagicMock(); resp.status_code = status; resp.text = ""
    resp.json.return_value = returned_rows
    sess.patch.return_value = resp
    return sess


def test_approve_patches_pending_row_with_actor():
    sess = _session([{"id": 7}])
    n = decide_media_review(7, "approve", actor="discord:adnan",
                            url="https://x.supabase.co", service_key="sk", session=sess)
    assert n == 1
    url = sess.patch.call_args[0][0]
    body = sess.patch.call_args[1]["json"]
    assert "id=eq.7" in url and "status=eq.pending" in url      # race-safe: only flips pending
    assert body["status"] == "approved" and body["decided_by"] == "discord:adnan"
    assert "decided_at" in body


def test_reject_maps_to_rejected():
    sess = _session([{"id": 3}])
    decide_media_review(3, "reject", actor="cli", url="https://x.supabase.co",
                        service_key="sk", session=sess)
    assert sess.patch.call_args[1]["json"]["status"] == "rejected"


def test_already_decided_row_is_noop_returns_zero():
    sess = _session([])  # PATCH matched nothing (row not pending)
    n = decide_media_review(7, "approve", actor="cli", url="https://x.supabase.co",
                            service_key="sk", session=sess)
    assert n == 0


def test_unknown_decision_raises():
    with pytest.raises(ValueError):
        decide_media_review(7, "maybe", actor="cli", url="https://x.supabase.co",
                            service_key="sk", session=_session([]))
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_media_decide_io.py -q`
Expected: FAIL — `ImportError: cannot import name 'decide_media_review'`

- [ ] **Step 3: Append to `utils/supabase_writer.py`**

```python
_DECISION_STATUS = {"approve": "approved", "reject": "rejected"}


def decide_media_review(review_id, decision, *, actor, url=None, service_key=None,
                        timeout=_DEFAULT_TIMEOUT, session=None) -> int:
    """Flip a PENDING media_review row to approved/rejected (the Phase 3 decision).

    Conditional on status='pending', so a repeat or an already-decided row is a
    no-op (returns 0). Records decided_by + decided_at. Returns rows updated (0/1).
    Raises ValueError on an unknown decision; SupabaseWriteError on non-2xx.
    """
    status = _DECISION_STATUS.get(decision)
    if status is None:
        raise ValueError(f"decision must be 'approve' or 'reject', got {decision!r}")
    base_url, key = _resolve_credentials(url, service_key)
    from datetime import datetime, timezone
    payload = {
        "status": status,
        "decided_by": actor,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }
    endpoint = f"{base_url}/rest/v1/media_review?id=eq.{int(review_id)}&status=eq.pending"
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Content-Type": "application/json", "Prefer": "return=representation"}
    sess = session or requests.Session()
    try:
        resp = sess.patch(endpoint, json=payload, headers=headers, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise SupabaseWriteError(f"media_review decide network error: {e}") from e
    if resp.status_code not in (200, 204):
        raise SupabaseWriteError(f"media_review decide HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return len(resp.json())
    except Exception:  # noqa: BLE001 — return=minimal or empty body → treat as 0
        return 0
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_media_decide_io.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add utils/supabase_writer.py tests/test_media_decide_io.py
git commit -m "feat(media-screen): conditional approve/reject decision writer"
```

---

## Task 2: `media_screen/decide.py` — result wrapper + CLI

**Files:**
- Create: `media_screen/decide.py`
- Test: `tests/test_media_decide.py`

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from media_screen.decide import apply_decision


def test_apply_decision_success():
    res = apply_decision(7, "approve", actor="discord:adnan", decider=lambda *a, **k: 1)
    assert res["ok"] is True and "approve" in res["message"] and "7" in res["message"]


def test_apply_decision_noop_when_not_pending():
    res = apply_decision(7, "approve", actor="cli", decider=lambda *a, **k: 0)
    assert res["ok"] is False and "not pending" in res["message"].lower()


def test_apply_decision_propagates_bad_decision():
    def bad(*a, **k):
        raise ValueError("decision must be 'approve' or 'reject'")
    with pytest.raises(ValueError):
        apply_decision(7, "maybe", actor="cli", decider=bad)
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_media_decide.py -q`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
"""Apply an approve/reject decision to a media_review row, + a manual CLI.

Copotron (Hetzner) normally performs the equivalent Supabase PATCH directly
(see docs/media-screen-copotron-wiring.md). This module is the canonical,
tested decision path and a CLI for the ExonVPS box:
    python -m media_screen.decide approve 7 --actor discord:adnan
Approved rows are applied by EconDelta's aggregate (Phase 2); rejected rows are
discarded (no metric_history change).
"""
from __future__ import annotations

import argparse
import sys

from utils.supabase_writer import decide_media_review


def apply_decision(review_id, decision, *, actor, decider=decide_media_review) -> dict:
    """Flip the row and return a friendly result. ok=False if it wasn't pending."""
    updated = decider(review_id, decision, actor=actor)
    if updated:
        return {"ok": True, "review_id": int(review_id),
                "message": f"media_review {review_id} → {decision}d by {actor}"}
    return {"ok": False, "review_id": int(review_id),
            "message": f"media_review {review_id} not pending (already decided or not found) — no change"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("decision", choices=["approve", "reject"])
    ap.add_argument("review_id", type=int)
    ap.add_argument("--actor", default="cli")
    a = ap.parse_args()
    result = apply_decision(a.review_id, a.decision, actor=a.actor)
    print(result["message"])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run to verify pass + full suite + ruff**

Run: `.venv/bin/python -m pytest tests/test_media_decide.py -q && .venv/bin/python -m pytest tests/ -q && .venv/bin/ruff check .`
Expected: all PASS, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add media_screen/decide.py tests/test_media_decide.py
git commit -m "feat(media-screen): apply_decision wrapper + CLI"
```

---

## Self-Review (against spec)

**Spec coverage:** D3 (approve/reject via Copotron) → `decide_media_review` flips status; the wiring recipe (`docs/media-screen-copotron-wiring.md`, authored alongside) tells Copotron the exact PATCH. §8 flow → approved consumed by Phase 2 apply; rejected discarded (no write). Race-safe / idempotent → conditional `status=eq.pending` PATCH, no-op returns 0.

**Type consistency:** `decide_media_review(review_id, decision, *, actor, ...)` matches `apply_decision`'s call (`decider`). CLI `choices=["approve","reject"]` matches `_DECISION_STATUS` keys.

**Out of scope:** Copotron's Discord message parsing + the PATCH it runs live on Hetzner (documented in the recipe, applied by the owner — not EconDelta code).
