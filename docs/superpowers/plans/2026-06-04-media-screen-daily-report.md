# Media-screen daily report to #thebrief — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the media screen post a report to #thebrief on every real run (including 0-candidate days, with "why skipped" reasons), and fix the approve/reject loop so `approve N` targets the real `media_review.id`.

**Architecture:** `classify` becomes a total function returning `Candidate | Skip`. `run_screen` collects both, dedups, inserts kept candidates to get their real ids, then builds an always-returned report via `format_report` and posts it to the #thebrief webhook (`MEDIA_SCREEN_WEBHOOK_URL`). `insert_media_review_rows` returns ids; `notify` gains an optional `webhook_url`.

**Tech Stack:** Python 3, pytest, PostgREST (Supabase), Discord webhooks. Run tests with `./.venv/bin/pytest` (or `pytest`) from the repo root; lint with `ruff check .`.

**Spec:** `docs/superpowers/specs/2026-06-04-media-screen-daily-report-design.md`

---

## File map

| File | Responsibility | Change |
|---|---|---|
| `media_screen/types.py` | shared frozen dataclasses | + `Skip` + `SKIP_REASONS` |
| `media_screen/filter.py` | classify a figure | return `Candidate \| Skip` (5 paths) |
| `media_screen/digest.py` | format the Discord report | replace `format_digest` → `format_report` |
| `scrapers/media_screen.py` | orchestration | collect skips, within-run dedup, insert→ids, always-post |
| `utils/supabase_writer.py` | DB writes | `insert_media_review_rows` returns ids |
| `utils/notifier.py` | Discord webhook | + optional `webhook_url` param |
| `.env.example`, `deploy/install.sh` | env scaffolding | + `MEDIA_SCREEN_WEBHOOK_URL` |
| `docs/media-screen-copotron-wiring.md` | wiring recipe | reference #thebrief + `approve <id>` |

`media_screen/dedup.py` is **unchanged** (stays a pure filter; the deduped set is computed in `run_screen`).

---

### Task 1: `Skip` dataclass + `SKIP_REASONS`

**Files:**
- Modify: `media_screen/types.py`
- Test: `tests/test_media_types.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_media_types.py`:
```python
from datetime import date

from media_screen.types import Skip, SKIP_REASONS


def test_skip_is_frozen_and_holds_reason():
    s = Skip("gross_npl_ratio", 32.26, date(2026, 3, 31), "matches-current-data")
    assert s.metric_id == "gross_npl_ratio" and s.reason == "matches-current-data"


def test_skip_reasons_are_the_five_known():
    assert SKIP_REASONS == frozenset({
        "out-of-range", "no-period", "matches-current-data",
        "older-period", "already-in-review",
    })
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/pytest tests/test_media_types.py -v`
Expected: FAIL with `ImportError: cannot import name 'Skip'`.

- [ ] **Step 3: Write minimal implementation**

In `media_screen/types.py`, after the `Candidate` class, append:
```python


@dataclass(frozen=True)
class Skip:
    """A tracked figure the screen saw but did NOT raise as a candidate, with why."""
    metric_id: str
    value: float
    period: date | None
    reason: str               # one of SKIP_REASONS


SKIP_REASONS = frozenset({
    "out-of-range",           # value outside the metric's valid_range (unit guard)
    "no-period",              # no explicit reporting period
    "matches-current-data",   # same period, value within tolerance of current
    "older-period",           # press period older than what we already have
    "already-in-review",      # a valid candidate already pending/rejected in media_review
})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/pytest tests/test_media_types.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add media_screen/types.py tests/test_media_types.py
git commit -m "feat(media-screen): add Skip dataclass + SKIP_REASONS"
```

---

### Task 2: `classify` returns `Candidate | Skip` (total function)

**Files:**
- Modify: `media_screen/filter.py`
- Test: `tests/test_media_filter.py:13-38`, `tests/test_media_precision.py:18-34` (migrate existing `is None` assertions)

- [ ] **Step 1: Rewrite the failing tests**

Replace the body of `tests/test_media_filter.py` (keep the top imports, add `Skip`):
```python
from datetime import date

from media_screen.filter import classify
from media_screen.types import Extracted, Skip, Candidate

P_AS_OF = date(2025, 9, 30)


def _ex(value, period):
    return Extracted("NPL", value, period, "quote", "http://x", "tbsnews")


def test_no_period_is_skipped_with_reason():
    r = classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(32.26, None), tolerance=0.05)
    assert isinstance(r, Skip) and r.reason == "no-period"


def test_fresher_period_is_a_candidate():
    c = classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(32.26, date(2026, 3, 31)), tolerance=0.05)
    assert isinstance(c, Candidate) and c.kind == "fresher_period"
    assert c.press_value == 32.26 and c.press_as_of == date(2026, 3, 31)


def test_same_period_material_diff_is_a_conflict():
    c = classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(35.50, P_AS_OF), tolerance=0.05)
    assert isinstance(c, Candidate) and c.kind == "same_period_conflict"


def test_same_period_within_tolerance_is_matches_current():
    r = classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(35.75, P_AS_OF), tolerance=0.05)
    assert isinstance(r, Skip) and r.reason == "matches-current-data"


def test_older_period_is_skipped_with_reason():
    r = classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(34.0, date(2025, 6, 30)), tolerance=0.05)
    assert isinstance(r, Skip) and r.reason == "older-period"


def test_no_parsed_value_with_dated_press_is_fresher():
    c = classify("x", None, None, _ex(10.0, date(2026, 1, 31)), tolerance=0.05)
    assert isinstance(c, Candidate) and c.kind == "fresher_period"
```

Replace the three out-of-range assertions in `tests/test_media_precision.py` (lines 18-34) to assert `Skip`:
```python
def test_amount_mislabelled_as_ratio_is_rejected():
    r = classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(588704.0),
                 tolerance=0.05, valid_range=(0.0, 50.0))
    assert isinstance(r, Skip) and r.reason == "out-of-range"


def test_in_range_ratio_still_classifies():
    c = classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(32.26),
                 tolerance=0.05, valid_range=(0.0, 50.0))
    assert isinstance(c, Candidate) and c.kind == "fresher_period" and c.press_value == 32.26


def test_range_guard_runs_before_period_check():
    """An out-of-range value is rejected (reason=out-of-range) even with period=None,
    proving the range guard fires before the period check."""
    r = classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(416482.0, period=None),
                 tolerance=0.05, valid_range=(0.0, 50.0))
    assert isinstance(r, Skip) and r.reason == "out-of-range"


def test_default_range_is_permissive():
    c = classify("x", None, None, _ex(588704.0), tolerance=0.05)
    assert isinstance(c, Candidate)
```

Add the imports at the top of `tests/test_media_precision.py`:
```python
from media_screen.types import Extracted, Skip, Candidate
```
(replacing the existing `from media_screen.types import Extracted`)

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_media_filter.py tests/test_media_precision.py -v`
Expected: FAIL (classify still returns `None`; `isinstance(None, Skip)` is False).

- [ ] **Step 3: Rewrite `classify`**

Replace `media_screen/filter.py` entirely:
```python
"""Classify a parsed-vs-press pair into a review Candidate or a Skip(reason).

classify is a TOTAL function: every tracked figure returns either a Candidate
(needs approval) or a Skip with the reason it was dropped. No `return None`.
"""
from __future__ import annotations

from datetime import date

from media_screen.types import Candidate, Extracted, Skip


def classify(
    metric_id: str,
    parsed_value: float | None,
    parsed_as_of: date | None,
    ex: Extracted,
    *,
    tolerance: float,
    valid_range: tuple[float, float] = (float("-inf"), float("inf")),
) -> Candidate | Skip:
    # Rule 0: value must be plausible for this metric's unit (the unit guard).
    lo, hi = valid_range
    if not (lo <= ex.value <= hi):
        return Skip(metric_id, ex.value, ex.period, "out-of-range")

    # Rule 1: period MUST be explicit.
    if ex.period is None:
        return Skip(metric_id, ex.value, None, "no-period")

    # Rule 2 + kind derivation.
    if parsed_as_of is None or ex.period > parsed_as_of:
        kind = "fresher_period"
    elif ex.period == parsed_as_of:
        if parsed_value is not None and abs(ex.value - parsed_value) <= tolerance:
            return Skip(metric_id, ex.value, ex.period, "matches-current-data")
        kind = "same_period_conflict"
    else:
        return Skip(metric_id, ex.value, ex.period, "older-period")

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

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_media_filter.py tests/test_media_precision.py -v`
Expected: PASS (all). The range-before-period test confirms `out-of-range` wins over `no-period`.

- [ ] **Step 5: Commit**

```bash
git add media_screen/filter.py tests/test_media_filter.py tests/test_media_precision.py
git commit -m "feat(media-screen): classify returns Candidate|Skip (5 reasons, total fn)"
```

---

### Task 3: `insert_media_review_rows` returns the inserted ids

**Files:**
- Modify: `utils/supabase_writer.py:542-574`
- Test: `tests/test_media_review_io.py:26-37` (migrate)

- [ ] **Step 1: Rewrite the failing test**

Replace `test_insert_posts_pending_rows` in `tests/test_media_review_io.py`:
```python
def test_insert_returns_inserted_ids():
    sess = MagicMock(spec=requests.Session)
    resp = MagicMock()
    resp.status_code = 201
    resp.text = ""
    resp.json.return_value = [{"id": 42}]
    sess.post.return_value = resp
    ids = insert_media_review_rows([_cand()], url="https://x.supabase.co",
                                   service_key="sk", session=sess)
    assert ids == [42]
    body = sess.post.call_args[1]["json"][0]
    assert body["metric_id"] == "gross_npl_ratio" and body["status"] == "pending"
    assert sess.post.call_args[1]["headers"]["Prefer"] == "return=representation"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/pytest tests/test_media_review_io.py::test_insert_returns_inserted_ids -v`
Expected: FAIL (`insert_media_review_rows` returns `1`, not `[42]`).

- [ ] **Step 3: Modify the implementation**

In `utils/supabase_writer.py`, change the signature/docstring/return of `insert_media_review_rows`. Replace the `endpoint`/`headers`/return tail (current lines 564-574):
```python
    endpoint = f"{base_url}/rest/v1/media_review?select=id"
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Content-Type": "application/json", "Prefer": "return=representation"}
    sess = session or requests.Session()
    try:
        resp = sess.post(endpoint, json=rows, headers=headers, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise SupabaseWriteError(f"media_review insert network error: {e}") from e
    if resp.status_code not in (200, 201, 204):
        raise SupabaseWriteError(f"media_review insert HTTP {resp.status_code}: {resp.text[:200]}")
    return [row["id"] for row in resp.json()]
```
And change line 543 return annotation `-> int:` to `-> list[int]:`, line 549 `return 0` to `return []`, and the docstring "Returns count inserted" to "Returns the inserted rows' ids (PostgREST preserves array order)."

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_media_review_io.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add utils/supabase_writer.py tests/test_media_review_io.py
git commit -m "feat(media-screen): insert_media_review_rows returns inserted ids (fixes approve N=id)"
```

---

### Task 4: `notify` gains optional `webhook_url`

**Files:**
- Modify: `utils/notifier.py:29-71`
- Test: `tests/test_notifier_webhook.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_notifier_webhook.py`:
```python
from unittest.mock import MagicMock, patch

import utils.notifier as notifier


def _clear():
    notifier._recent_alerts.clear()


def test_webhook_url_param_overrides_env(monkeypatch):
    _clear()
    monkeypatch.delenv("ECONDELTA_DRY_RUN", raising=False)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://ops/webhook")
    with patch.object(notifier.requests, "post") as post:
        post.return_value = MagicMock(status_code=204, raise_for_status=lambda: None)
        ok = notifier.notify("info", "t", "m", webhook_url="https://brief/webhook")
    assert ok is True
    assert post.call_args.args[0] == "https://brief/webhook"


def test_none_webhook_url_falls_back_to_env(monkeypatch):
    _clear()
    monkeypatch.delenv("ECONDELTA_DRY_RUN", raising=False)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://ops/webhook")
    with patch.object(notifier.requests, "post") as post:
        post.return_value = MagicMock(status_code=204, raise_for_status=lambda: None)
        notifier.notify("info", "t2", "m", webhook_url=None)
    assert post.call_args.args[0] == "https://ops/webhook"


def test_empty_webhook_url_is_treated_as_unset(monkeypatch):
    _clear()
    monkeypatch.delenv("ECONDELTA_DRY_RUN", raising=False)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://ops/webhook")
    with patch.object(notifier.requests, "post") as post:
        post.return_value = MagicMock(status_code=204, raise_for_status=lambda: None)
        notifier.notify("info", "t3", "m", webhook_url="   ")
    assert post.call_args.args[0] == "https://ops/webhook"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/pytest tests/test_notifier_webhook.py -v`
Expected: FAIL (`notify() got an unexpected keyword argument 'webhook_url'`).

- [ ] **Step 3: Modify `notify`**

In `utils/notifier.py`, change the signature (line 29-34) to add the kwarg:
```python
def notify(
    level: Literal["info", "warning", "error"],
    title: str,
    message: str,
    fields: dict | None = None,
    *,
    webhook_url: str | None = None,
) -> bool:
```
Then replace the webhook resolution (current line 66):
```python
    url = (webhook_url or "").strip() or os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        logger.warning(
            "no webhook URL configured — skipping alert (%s: %s)", level, title
        )
        return False
```
…and change the later `requests.post(webhook_url, ...)` (line 88) to `requests.post(url, ...)`. (The old local name `webhook_url` for the env value is now the param; use `url` for the resolved value.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_notifier_webhook.py tests/test_notifier.py -v`
Expected: PASS (new + existing notifier tests unaffected — the kwarg is additive).

- [ ] **Step 5: Commit**

```bash
git add utils/notifier.py tests/test_notifier_webhook.py
git commit -m "feat(notifier): optional webhook_url param (empty treated as unset)"
```

---

### Task 5: `format_report` — always-returned report with skip reasons

**Files:**
- Modify: `media_screen/digest.py` (replace `format_digest` with `format_report`)
- Test: `tests/test_media_digest.py` (rewrite)

- [ ] **Step 1: Rewrite the failing tests**

Replace `tests/test_media_digest.py` entirely:
```python
from datetime import date

from media_screen.digest import format_report
from media_screen.types import Candidate, Skip


def _cand():
    return Candidate("gross_npl_ratio", 35.73, date(2025, 9, 30), 32.26, date(2026, 3, 31),
                     "fresher_period", "tbsnews", "http://x", "NPLs 32.26% end-March 2026", "c")


def test_zero_articles_message():
    title, message, fields = format_report([], [], 0, 0)
    assert "0 articles" in title and "all sources failed" in message.lower()


def test_no_tracked_figures_heartbeat():
    title, message, fields = format_report([], [], 6, 6)
    assert "no change" in title.lower()
    assert "no tracked figures" in message.lower()
    assert "Checked 12 articles (6 TBS, 6 Daily Star)" in message


def test_heartbeat_renders_skip_reason_text():
    skip = Skip("gross_npl_ratio", 32.26, date(2026, 3, 31), "matches-current-data")
    title, message, fields = format_report([], [skip], 6, 6)
    assert "no change" in title.lower()
    assert "matches current data" in message
    assert "gross_npl_ratio" in message and "32.26" in message
    assert "nothing needs approval" in message.lower()


def test_candidate_uses_real_id_and_approve_reject():
    title, message, fields = format_report([(42, _cand())], [], 5, 6)
    assert "1 needs approval" in title
    assert "#42" in message and "32.26" in message
    assert "approve 42" in message and "reject 42" in message
    assert fields == {"gross_npl_ratio": "32.26 @ 2026-03-31"}


def test_dry_run_candidate_has_no_id_or_approve_line():
    title, message, fields = format_report([(None, _cand())], [], 5, 6)
    assert "dry-run" in message.lower()
    assert "approve" not in message.lower()


def test_skip_cap_truncates_with_footer():
    skips = [Skip(f"m{i}", float(i), date(2026, 3, 31), "matches-current-data")
             for i in range(20)]
    title, message, fields = format_report([], skips, 6, 6)
    assert "…and 8 more" in message
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_media_digest.py -v`
Expected: FAIL with `ImportError: cannot import name 'format_report'`.

- [ ] **Step 3: Replace `media_screen/digest.py`**

```python
"""Format the always-on Discord report (candidates + skips) for utils.notifier.notify().

format_report ALWAYS returns (title, message, fields) — never None — so every run
posts. Candidates are numbered by their REAL media_review.id (None on dry-run).
"""
from __future__ import annotations

from datetime import date

from media_screen.types import Candidate, Skip

# Healthy/expected reasons first, then the eyeball bucket.
_REASON_ORDER = ["already-in-review", "matches-current-data", "older-period",
                 "out-of-range", "no-period"]
_REASON_PHRASE = {
    "already-in-review": "already in review queue",
    "matches-current-data": "matches current data",
    "older-period": "older period than current",
    "out-of-range": "value out of range",
    "no-period": "no explicit period",
}
_MAX_SKIP_LINES = 12


def _period_str(p: date | None) -> str:
    return p.isoformat() if p is not None else "(no period)"


def format_report(
    candidates_with_ids: list[tuple[int | None, Candidate]],
    skips: list[Skip],
    n_tbs: int,
    n_ds: int,
) -> tuple[str, str, dict]:
    n_articles = n_tbs + n_ds
    n_cand = len(candidates_with_ids)

    if n_articles == 0:
        return ("Media screen — 0 articles",
                "Collected 0 articles — all sources failed. No screen this run.", {})

    header = f"Checked {n_articles} articles ({n_tbs} TBS, {n_ds} Daily Star)."

    cand_lines = []
    for rid, c in candidates_with_ids:
        tag = f"#{rid}" if rid is not None else "(dry-run — not queued)"
        parsed = c.parsed_as_of.isoformat() if c.parsed_as_of else "—"
        line = (f"**{tag} {c.metric_id}** [{c.kind}] — press **{c.press_value}** @ "
                f"{c.press_as_of.isoformat()} vs current {c.parsed_value} @ {parsed}\n"
                f"_{c.source_quote}_ <{c.source_url}>")
        if rid is not None:
            line += f"\nReply: approve {rid} · reject {rid}"
        cand_lines.append(line)

    ordered = sorted(
        skips,
        key=lambda s: (_REASON_ORDER.index(s.reason) if s.reason in _REASON_ORDER else 99,
                       s.metric_id),
    )
    skip_lines = [
        f"• {s.metric_id} — {s.value} @ {_period_str(s.period)} → "
        f"{_REASON_PHRASE.get(s.reason, s.reason)}, skipped"
        for s in ordered
    ]
    overflow = ""
    if len(skip_lines) > _MAX_SKIP_LINES:
        overflow = f"\n…and {len(skip_lines) - _MAX_SKIP_LINES} more"
        skip_lines = skip_lines[:_MAX_SKIP_LINES]

    if n_cand > 0:
        title = f"Media screen — {n_cand} needs approval"
        body = [header, ""] + cand_lines
        if skip_lines:
            body += ["", "Also seen (skipped):"] + skip_lines
            body[-1] += overflow
    elif skip_lines:
        title = "Media screen — no change"
        body = [header] + skip_lines
        body[-1] += overflow
        body += ["", "✅ Nothing needs approval."]
    else:
        title = "Media screen — no change"
        body = [header, "No tracked figures in today's articles. No change needed."]

    message = "\n".join(body)
    if len(message) > 3900:  # keep under Discord's 4096 embed-description limit
        message = message[:3900] + "\n…(truncated)"
    fields = {c.metric_id: f"{c.press_value} @ {c.press_as_of.isoformat()}"
              for _, c in candidates_with_ids[:10]}
    return title, message, fields
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_media_digest.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add media_screen/digest.py tests/test_media_digest.py
git commit -m "feat(media-screen): format_report (always-on report, real ids, skip reasons)"
```

---

### Task 6: `run_screen` — collect skips, dedup, insert→ids, always-post

**Files:**
- Modify: `scrapers/media_screen.py` (imports, `run_screen`, add 2 dedup helpers)
- Test: `tests/test_media_screen.py` (migrate + add behavior tests)

- [ ] **Step 1: Write/rewrite the failing tests**

Append these to `tests/test_media_screen.py` (and the existing tests are updated to mock notify with a recorder where they assert routing). Add at top: `from media_screen.types import Skip`. Add:
```python
def _setup_one_candidate(monkeypatch, ms, open_rows=None):
    monkeypatch.setattr(ms, "_collect_articles",
                        lambda specs: [("text", "http://x", "tbsnews")])
    monkeypatch.setattr(ms, "extract_numbers", lambda *a, **k: [_ex()])
    monkeypatch.setattr(ms, "_parsed_for", lambda mid: (35.73, date(2025, 9, 30)))
    monkeypatch.setattr(ms, "get_open_media_review", lambda **k: open_rows or [])


def test_zero_candidates_still_posts_heartbeat(monkeypatch):
    """Goal 1: a 0-candidate live run posts exactly one report. Mutation: re-gating
    the post behind a None/empty check must turn this red."""
    import scrapers.media_screen as ms
    monkeypatch.setenv("MEDIA_SCREEN_WEBHOOK_URL", "https://brief/wh")
    monkeypatch.setattr(ms, "_collect_articles",
                        lambda specs: [("t", "http://x", "tbsnews")])
    monkeypatch.setattr(ms, "extract_numbers", lambda *a, **k: [])  # no figures
    monkeypatch.setattr(ms, "_parsed_for", lambda mid: (None, None))
    monkeypatch.setattr(ms, "get_open_media_review", lambda **k: [])
    calls = []
    monkeypatch.setattr(ms, "notify",
                        lambda level, title, message, **k: calls.append((level, title, message, k)))
    rc = ms.run_screen(dry_run=False)
    assert rc == 0
    assert len(calls) == 1
    level, title, message, kw = calls[0]
    assert level == "info" and "no change" in title.lower()
    assert kw.get("webhook_url") == "https://brief/wh"


def test_report_routes_to_media_screen_webhook(monkeypatch):
    """Goal 4: the report carries webhook_url=MEDIA_SCREEN_WEBHOOK_URL.
    Mutation: dropping webhook_url= must fail this."""
    import scrapers.media_screen as ms
    monkeypatch.setenv("MEDIA_SCREEN_WEBHOOK_URL", "https://brief/wh")
    _setup_one_candidate(monkeypatch, ms)
    monkeypatch.setattr(ms, "insert_media_review_rows", lambda cands, **k: [42])
    calls = []
    monkeypatch.setattr(ms, "notify",
                        lambda level, title, message, **k: calls.append((level, k)))
    rc = ms.run_screen(dry_run=False)
    assert rc == 0
    level, kw = calls[-1]
    assert level == "warning" and kw.get("webhook_url") == "https://brief/wh"


def test_unset_webhook_skips_post_no_ops_fallback(monkeypatch):
    """An unset MEDIA_SCREEN_WEBHOOK_URL must NOT route the report to the ops channel."""
    import scrapers.media_screen as ms
    monkeypatch.delenv("MEDIA_SCREEN_WEBHOOK_URL", raising=False)
    _setup_one_candidate(monkeypatch, ms)
    monkeypatch.setattr(ms, "insert_media_review_rows", lambda cands, **k: [42])
    calls = []
    monkeypatch.setattr(ms, "notify", lambda *a, **k: calls.append((a, k)))
    rc = ms.run_screen(dry_run=False)
    assert rc == 0 and calls == []  # no report posted anywhere


def test_dry_run_prints_report_does_not_notify(monkeypatch, capsys):
    import scrapers.media_screen as ms
    monkeypatch.setenv("MEDIA_SCREEN_WEBHOOK_URL", "https://brief/wh")
    _setup_one_candidate(monkeypatch, ms)
    notified = []
    monkeypatch.setattr(ms, "notify", lambda *a, **k: notified.append(a))
    rc = ms.run_screen(dry_run=True)
    assert rc == 0 and notified == []
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "needs approval" in out


def test_already_open_candidate_reported_as_skip(monkeypatch):
    """An open-row match: not inserted AND shown in the report as already-in-review."""
    import scrapers.media_screen as ms
    monkeypatch.setenv("MEDIA_SCREEN_WEBHOOK_URL", "https://brief/wh")
    _setup_one_candidate(monkeypatch, ms,
                         open_rows=[{"metric_id": "gross_npl_ratio", "press_as_of": "2026-03-31"}])
    inserted = {"called": False}
    monkeypatch.setattr(ms, "insert_media_review_rows",
                        lambda *a, **k: inserted.update(called=True) or [])
    calls = []
    monkeypatch.setattr(ms, "notify",
                        lambda level, title, message, **k: calls.append(message))
    rc = ms.run_screen(dry_run=False)
    assert rc == 0 and inserted["called"] is False
    assert "already in review queue" in calls[-1]
```

Also update the existing `test_run_screen_inserts_filtered_candidates` (it stubs `insert_media_review_rows` to return `len(cands)` — change to return a list of ids):
```python
    monkeypatch.setattr(ms, "insert_media_review_rows",
                        lambda cands, **k: captured.setdefault("c", cands) or [1])
    monkeypatch.setenv("MEDIA_SCREEN_WEBHOOK_URL", "https://brief/wh")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_media_screen.py -v`
Expected: FAIL (run_screen doesn't yet collect skips / pass webhook_url / build a report at 0 candidates).

- [ ] **Step 3: Rework `run_screen` + add dedup helpers**

In `scrapers/media_screen.py`: ensure `import os` is present near the top; change the import `from media_screen.digest import format_digest` → `from media_screen.digest import format_report`; add `from media_screen.types import Candidate, Skip`. Add two helpers above `run_screen`:
```python
def _dedup_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Collapse the same (metric_id, press_as_of) seen in multiple articles;
    prefer the one carrying a source_quote."""
    best: dict[tuple, Candidate] = {}
    for c in candidates:
        key = (c.metric_id, c.press_as_of)
        cur = best.get(key)
        if cur is None or (c.source_quote and not cur.source_quote):
            best[key] = c
    return list(best.values())


def _dedup_skips(skips: list[Skip]) -> list[Skip]:
    """Collapse duplicate (metric_id, period, reason) skips."""
    best: dict[tuple, Skip] = {}
    for s in skips:
        best.setdefault((s.metric_id, s.period, s.reason), s)
    return list(best.values())
```
Replace the body of `run_screen` (lines 149-194) with:
```python
def run_screen(*, dry_run: bool, urls=None) -> int:
    specs = load_catalog()
    by_name = {n.lower(): s for s in specs for n in s.press_names}
    articles = _articles_from_urls(urls) if urls else _collect_articles(specs)
    n_tbs = sum(1 for _, _, outlet in articles if outlet == "tbsnews")
    n_ds = sum(1 for _, _, outlet in articles if outlet == "thedailystar")

    candidates: list[Candidate] = []
    skips: list[Skip] = []
    for text, url, outlet in articles:
        for ex in extract_numbers(text, specs=specs, source_url=url, source_outlet=outlet):
            spec = by_name.get(ex.indicator_hint.lower())
            if spec is None:
                continue
            parsed_value, parsed_as_of = _parsed_for(spec.metric_id)
            result = classify(spec.metric_id, parsed_value, parsed_as_of, ex,
                              tolerance=spec.tolerance, valid_range=spec.valid_range)
            if isinstance(result, Candidate):
                candidates.append(result)
            else:
                skips.append(result)

    candidates = _dedup_candidates(candidates)
    skips = _dedup_skips(skips)

    # Dedup candidates against open review rows; the dropped ones become skips.
    try:
        open_rows = get_open_media_review()
    except SupabaseReadError as e:
        logger.exception("media screen: could not read open review rows")
        notify("error", "media screen failed", f"open-review read failed: {e}")
        return 1
    kept = drop_already_open(candidates, open_rows)
    for c in candidates:
        if c not in kept:
            skips.append(Skip(c.metric_id, c.press_value, c.press_as_of, "already-in-review"))
    candidates = kept

    if dry_run:
        title, message, _ = format_report([(None, c) for c in candidates], skips, n_tbs, n_ds)
        print(f"[DRY-RUN] {title}\n{message}")
        logger.info("dry-run: %d candidate(s), %d skip(s), no insert/notify",
                    len(candidates), len(skips))
        return 0

    ids: list[int] = []
    if candidates:
        try:
            ids = insert_media_review_rows(candidates)
        except SupabaseWriteError as e:
            logger.exception("media screen: insert into media_review failed")
            notify("error", "media screen failed", f"media_review insert failed: {e}")
            return 1

    title, message, fields = format_report(list(zip(ids, candidates)), skips, n_tbs, n_ds)
    level = "warning" if candidates else "info"
    webhook = os.environ.get("MEDIA_SCREEN_WEBHOOK_URL", "").strip()
    if webhook:
        notify(level, title, message, fields=fields, webhook_url=webhook)
    else:
        logger.warning("MEDIA_SCREEN_WEBHOOK_URL not set — skipping #thebrief report")
    logger.info("media screen: %d candidate(s) inserted, %d skip(s)", len(candidates), len(skips))
    return 0
```

- [ ] **Step 4: Run the full media-screen suite**

Run: `./.venv/bin/pytest tests/test_media_screen.py tests/test_media_digest.py tests/test_media_filter.py tests/test_media_precision.py tests/test_media_review_io.py tests/test_media_types.py tests/test_notifier_webhook.py -v`
Expected: PASS (all). Then run the whole suite: `./.venv/bin/pytest -q` → green; `ruff check .` → clean.

- [ ] **Step 5: Commit**

```bash
git add scrapers/media_screen.py tests/test_media_screen.py
git commit -m "feat(media-screen): always-post report, within-run dedup, real-id approve loop"
```

---

### Task 7: env scaffolding for `MEDIA_SCREEN_WEBHOOK_URL`

**Files:**
- Modify: `.env.example`, `deploy/install.sh`

- [ ] **Step 1: Add to `.env.example`**

After the `DISCORD_WEBHOOK_URL=` line in `./.env.example`, add:
```bash
# Discord webhook for the #thebrief channel — the media screen posts its daily
# report here (Copotron watches #thebrief for `approve <id>` / `reject <id>`).
MEDIA_SCREEN_WEBHOOK_URL=
```

- [ ] **Step 2: Add to the install heredoc**

In `deploy/install.sh`, inside the `cat > "$ENV_FILE" <<EOF` block, on the line after `DISCORD_WEBHOOK_URL=...`, add:
```bash
MEDIA_SCREEN_WEBHOOK_URL=${MEDIA_SCREEN_WEBHOOK_URL:-}
```

- [ ] **Step 3: Verify the script still parses**

Run: `bash -n deploy/install.sh`
Expected: no output (syntax OK).

- [ ] **Step 4: Commit**

```bash
git add .env.example deploy/install.sh
git commit -m "chore(deploy): scaffold MEDIA_SCREEN_WEBHOOK_URL (#thebrief)"
```

---

### Task 8: Rollout (manual prod, Copotron-first ordering) + wiring doc

> Not code — a deploy checklist. Do these IN ORDER. Adnan provides the #thebrief webhook URL.

- [ ] **Step 1: Repoint Copotron to #thebrief FIRST**

Update the Hetzner `~/CLAUDE.md` block (the "Media-screen approve/reject" section) to say **#thebrief** instead of "the media-screen channel", and confirm Copotron has access to #thebrief. Then verify the loop still works from the new channel:
```bash
ssh hetzner 'ssh exon-media "approve 999"'   # expect: "not pending … no change" (safe no-op)
```

- [ ] **Step 2: Set the webhook on the box**

```bash
ssh exonhost "grep -q MEDIA_SCREEN_WEBHOOK_URL /etc/econdelta.env || echo 'MEDIA_SCREEN_WEBHOOK_URL=<the #thebrief webhook>' | sudo tee -a /etc/econdelta.env"
```
(Adnan pastes the real webhook URL.)

- [ ] **Step 3: Deploy the code + smoke test**

Pull on the box, then a dry-run that prints the report (no post):
```bash
ssh exonhost 'cd /home/adnan-local/econdelta && git pull && set -a && . /etc/econdelta.env && set +a && ./.venv/bin/python -m scrapers.media_screen --dry-run'
```
Expected: a printed report (heartbeat or candidates), no Discord post.

- [ ] **Step 4: Update the wiring doc**

Edit `docs/media-screen-copotron-wiring.md`: change the digest channel to #thebrief and the instruction to `approve <id>` / `reject <id>` (the real `media_review.id`) — keep ONE instruction format across `digest.py`, the spec, and this doc. Commit:
```bash
git add docs/media-screen-copotron-wiring.md
git commit -m "docs(media-screen): wiring doc → #thebrief + approve <id> (real id)"
```

---

## Notes for the implementer

- **Order matters:** Tasks 1-6 are code (TDD, commit each). Task 7 is config. Task 8 is manual prod and must run Copotron-repoint **before** the webhook is set, so reports never land in a channel Copotron isn't watching.
- **PostgREST array order:** `insert_media_review_rows` assumes `return=representation` yields ids in input order (standard). The within-run dedup runs before insert, so `zip(ids, candidates)` lines up.
- **Don't** re-introduce a `if report is not None` guard around the notify call — `format_report` is total; the always-post behavior is the whole point (a mutation test in Task 6 guards this).
