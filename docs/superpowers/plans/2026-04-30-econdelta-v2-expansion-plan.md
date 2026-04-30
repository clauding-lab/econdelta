# EconDelta v2 — Bangladesh Indicators Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship ~36 new Bangladesh economic indicators (45 total) into `latest.json` via a hybrid deterministic + Sonnet-4.6-via-Claude-Max parser pipeline, with one-time first-extraction validation on ExonVPS.

**Architecture:** Three-stage pipeline (`fetch_all.py` → `parse_all.py` → `aggregate_latest.py`). Stage 1 downloads PDFs/HTML to disk-cached artifacts with sha256 dedup. Stage 2 runs deterministic parsers per indicator with Sonnet-4.6 sanity-check + LLM fallback (mirrors `the-brief/brief/claude/max_client.py`). Stage 3 emits dual-shape `latest.json` (new domain groups + legacy keys for The Brief). All driven by `config/sources-v3.json` registry.

**Tech Stack:** Python 3.12, pydantic 2.x, pdfplumber (new), playwright-stealth 2.x (existing), `claude -p` headless CLI for Sonnet 4.6, pytest, systemd timers on ExonVPS.

**Reference design spec:** `docs/superpowers/specs/2026-04-30-econdelta-v2-expansion-design.md`

---

## File Structure

**New top-level packages (alongside existing `scrapers/`, `utils/`):**
- `fetchers/` — Stage 1 (HTML + PDF fetching with sha256-cached artifacts)
- `parsers/` — Stage 2 strategies (deterministic per-pattern parsers)
- `claude_max/` — `claude -p` subprocess wrapper + prompt templates + LLM-side validators

**New top-level scripts:**
- `fetch_all.py` — Stage 1 entry: walks sources-v3, fetches due indicators
- `parse_all.py` — Stage 2 entry: parses all fetched artifacts, emits per-indicator snapshots
- `scripts/build_sources_v3.py` — one-shot v2 → v3 migration

**New config:**
- `config/sources-v3.json` — executable registry of ~45 indicators

**Modified files:**
- `aggregate_latest.py` — registry-driven, dual-shape output, freshness summary
- `pyproject.toml` — add `pdfplumber`, `pdfminer.six`; extend coverage `source` list
- `requirements.txt` + `requirements-dev.txt` — add new deps
- `tests/test_aggregator.py` — cover new shape + dual-emit

**New deploy units:**
- `deploy/econdelta-fetch.{service,timer}` — daily 00:00 UTC
- `deploy/econdelta-parse.{service,timer}` — daily 00:15 UTC
- `deploy/install.sh` — picks up new units automatically (existing pattern)

**Existing files left untouched:**
- `scrapers/bb_forex.py`, `dse_market.py`, `commodity_prices.py` — keep producing snapshots in their current locations and shapes. The aggregator reads both old and new layouts.
- `deploy/econdelta-{forex,dse,commodity,aggregate}.{service,timer}` — keep current schedules.

---

## Phase 1 — Scaffolding

### Task 1: Build `config/sources-v3.json` from v2

**Files:**
- Create: `scripts/build_sources_v3.py`
- Create: `config/sources-v3.json`
- Test: `tests/test_build_sources_v3.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_build_sources_v3.py
"""Tests the one-shot v2 → v3 migration script.

The script must:
- Preserve every indicator id from v2
- Default cadence/url from v2
- Inject domain, value_type, valid_range, anomaly_threshold from a hand-curated map
- Skip indicators whose primary.type is "news" with no alternate URL (none exist today)
"""
import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_build_sources_v3_preserves_all_v2_ids(tmp_path):
    out = tmp_path / "sources-v3.json"
    subprocess.run(
        ["python", str(REPO / "scripts" / "build_sources_v3.py"),
         "--in", str(REPO / "config" / "sources-v2.json"),
         "--out", str(out)],
        check=True,
    )
    v2 = json.loads((REPO / "config" / "sources-v2.json").read_text())
    v3 = json.loads(out.read_text())

    v2_ids = {i["id"] for i in v2["indicators"]}
    v3_ids = {i["id"] for i in v3["indicators"]}
    assert v2_ids == v3_ids, f"id mismatch: missing={v2_ids - v3_ids}, extra={v3_ids - v2_ids}"

    # spot check: every entry has the new required fields
    for ind in v3["indicators"]:
        assert ind["domain"] in {
            "forex_and_reserves", "money_market", "monetary_aggregates",
            "inflation", "government_finance", "external_sector",
            "commodities", "equities", "macro",
        }, f"{ind['id']} has bad domain {ind['domain']}"
        assert ind["parse"]["value_type"] in {
            "percent", "amount_bdt_crore", "amount_usd_bn", "ratio", "count", "rate"
        }
        assert isinstance(ind["parse"]["valid_range"], list) and len(ind["parse"]["valid_range"]) == 2
        assert isinstance(ind["anomaly_threshold"], (int, float))
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Projects/clauding-lab/econdelta
.venv/bin/pytest tests/test_build_sources_v3.py -v
```

Expected: FAIL with `FileNotFoundError: scripts/build_sources_v3.py`.

- [ ] **Step 3: Write the migration script**

```python
# scripts/build_sources_v3.py
"""One-shot v2 -> v3 source registry migration.

Reads config/sources-v2.json (human-curated backlog). Emits config/sources-v3.json
(executable registry consumed by fetch_all.py and parse_all.py).

Each v3 indicator gets:
  - domain (one of 9 buckets)
  - parse.deterministic (registered parser name)
  - parse.value_type, parse.valid_range
  - parse.llm_prompt (filename in claude_max/prompts/)
  - anomaly_threshold (% change ceiling)

Non-deterministic v2 fields (primary/alternate/fallback urls + tasks) are
preserved verbatim so the human-readable instructions remain available to
the LLM at parse time.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

# Hand-curated metadata: id -> v3 augmentation
# Values were inferred from sources-v2.json by domain expert review.
META: dict[str, dict[str, Any]] = {
    "policy_rate_slf_sdf": {
        "domain": "money_market",
        "deterministic": "html_footer_ticker",
        "value_type": "percent",
        "valid_range": [0.5, 25.0],
        "llm_prompt": "html_footer_ticker.txt",
        "anomaly_threshold": 1.0,
    },
    "call_money_rate": {
        "domain": "money_market",
        "deterministic": "html_call_money",
        "value_type": "percent",
        "valid_range": [0.0, 25.0],
        "llm_prompt": "html_call_money.txt",
        "anomaly_threshold": 2.0,
    },
    "usd_bdt_exchange_rate": {
        "domain": "forex_and_reserves",
        "deterministic": "html_footer_ticker",
        "value_type": "rate",
        "valid_range": [80.0, 200.0],
        "llm_prompt": "html_footer_ticker.txt",
        "anomaly_threshold": 0.02,
    },
    # ... entries for every v2 id; full table in this file
}

# Default fallbacks for indicators not in META (so the script doesn't error
# on a missing entry; first-extraction will surface any gaps via valid_range
# violations).
DEFAULT_META = {
    "domain": "macro",
    "deterministic": "pdf_component",
    "value_type": "amount_bdt_crore",
    "valid_range": [0.0, 1_000_000_000.0],
    "llm_prompt": "pdf_component.txt",
    "anomaly_threshold": 0.10,
}


def _augment(v2_entry: dict[str, Any]) -> dict[str, Any]:
    meta = META.get(v2_entry["id"], DEFAULT_META)
    return {
        "id": v2_entry["id"],
        "name": v2_entry["name"],
        "domain": meta["domain"],
        "cadence": v2_entry["cadence"],
        "fetch": _fetch_block(v2_entry),
        "parse": {
            "deterministic": meta["deterministic"],
            "llm_prompt": meta["llm_prompt"],
            "value_type": meta["value_type"],
            "valid_range": meta["valid_range"],
        },
        "anomaly_threshold": meta["anomaly_threshold"],
        "alternate": v2_entry.get("alternate"),
        "fallback": v2_entry.get("fallback"),
    }


def _fetch_block(v2_entry: dict[str, Any]) -> dict[str, Any]:
    primary = v2_entry["primary"]
    block: dict[str, Any] = {"type": primary.get("type", "html")}
    if "url" in primary:
        block["url"] = primary["url"]
    if "task" in primary:
        block["task"] = primary["task"]
    if primary.get("type") == "pdf" and primary.get("url", "").endswith(("/3/11", "/5/27", "/3/58")):
        block["discover"] = "latest_pdf_link"
    return block


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--in", dest="in_path", required=True)
    p.add_argument("--out", dest="out_path", required=True)
    args = p.parse_args()

    v2 = json.loads(Path(args.in_path).read_text())
    v3 = {
        "version": "3.0",
        "generated_from": v2["version"],
        "indicators": [_augment(e) for e in v2["indicators"]],
    }
    Path(args.out_path).write_text(json.dumps(v3, indent=2))
    print(f"Wrote {len(v3['indicators'])} indicators to {args.out_path}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Hand-curate the META table**

Open `scripts/build_sources_v3.py` and fill the `META` dict with one entry per v2 indicator (~45 entries). Use this as a checklist — copy from `config/sources-v2.json` ids:

```text
point_to_point_inflation, general_inflation, food_inflation, non_food_inflation
budget_opex_of_the_fy_vs_utilization, budget_adpex_of_the_fy_vs_utilization
tax_revenue, non_tax_revenue, total_revenue_budget_vs_actual
tax_gdp_ratio, rev_gdp_ratio
foreign_borrowing_for_budget_deficit, domestic_borrowing_for_bidget_deficit
bank_borrowing_for_deficit_financing, non_bank_borrowing_for_deficit_financing
treasury_bill_outstanding, treasury_bond_outstabnding
bill_bond_rates, policy_rate_slf_sdf, call_money_rate
usd_bdt_exchange_rate, fx_buy_sale_from_market
gsec_auction, gsec_mautiry
fx_reserve_gross_and_bpm6
private_sector_credit, deposits_of_the_system, currency_outside_bank
broad_money, reserve_money, deposits_held_with_bb_crr
money_multiplier, excess_liquid_asset_total_minimum, nsc_outstanding
monthly_export, fy_export, categorywise_export
monthly_import, monthly_import_lc_opening, monthly_import_lc_settlement
fy_import_lc, categorywise_fy_import_breakdown
monthly_remittance, fy_remittance, remittance_by_country
bop_summary, interbank_repo_data, gdp
```

For each, decide:
- `domain` — one of: `forex_and_reserves`, `money_market`, `monetary_aggregates`, `inflation`, `government_finance`, `external_sector`, `commodities`, `equities`, `macro`
- `deterministic` parser:
  - `html_footer_ticker` — bb.org.bd footer ticker (USD/BDT, policy rate, SLF, SDF)
  - `html_table_row` — gsom.bb.org.bd table page totals
  - `html_call_money` — multi-tenor table
  - `pdf_component` — labeled "Component N" entries in BB Monthly Economic Indicators
  - `pdf_table_row` — "Page N, table M, row K"
  - `pdf_table_total` — "last total of the page"
- `value_type` — `percent`, `amount_bdt_crore`, `amount_usd_bn`, `ratio`, `count`, `rate`
- `valid_range` — sane bounds for sanity validation
- `llm_prompt` — filename matching the parser strategy
- `anomaly_threshold` — fraction (0.05 = 5%)

- [ ] **Step 5: Run the script and verify**

```bash
.venv/bin/python scripts/build_sources_v3.py --in config/sources-v2.json --out config/sources-v3.json
```

Expected stdout: `Wrote 45 indicators to config/sources-v3.json` (or whatever the v2 count is).

```bash
.venv/bin/pytest tests/test_build_sources_v3.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/build_sources_v3.py config/sources-v3.json tests/test_build_sources_v3.py
git commit -m "feat(config): add sources-v3.json registry (executable backlog of 45 indicators)"
```

---

### Task 2: Add new dependencies + package skeletons

**Files:**
- Modify: `pyproject.toml:10-16` (dependencies block)
- Modify: `pyproject.toml:30-32` (coverage source)
- Modify: `requirements.txt`
- Modify: `requirements-dev.txt`
- Create: `fetchers/__init__.py`
- Create: `parsers/__init__.py`
- Create: `claude_max/__init__.py`
- Create: `claude_max/prompts/.gitkeep`

- [ ] **Step 1: Update `pyproject.toml`**

Modify lines 10-16 to:

```toml
dependencies = [
    "requests>=2.32,<3",
    "beautifulsoup4>=4.12,<5",
    "pydantic>=2.8,<3",
    "yfinance>=0.2.40,<1",
    "python-dateutil>=2.9,<3",
    "pdfplumber>=0.11,<0.13",
    "playwright>=1.49,<2",
    "playwright-stealth>=2.0,<3",
]
```

Modify lines 30-32 to:

```toml
[tool.coverage.run]
source = ["utils", "scrapers", "fetchers", "parsers", "claude_max"]
omit = ["tests/*"]
```

- [ ] **Step 2: Update `requirements.txt`**

Add lines (preserve existing pins):
```
pdfplumber>=0.11,<0.13
playwright>=1.49,<2
playwright-stealth>=2.0,<3
```

- [ ] **Step 3: Create empty package init files**

```bash
touch fetchers/__init__.py parsers/__init__.py claude_max/__init__.py
mkdir -p claude_max/prompts
touch claude_max/prompts/.gitkeep
```

- [ ] **Step 4: Install new deps locally**

```bash
.venv/bin/pip install -e . -r requirements-dev.txt
```

Expected: pdfplumber, pdfminer.six (transitive), pillow (transitive) installed. `pip show pdfplumber` should show version 0.11+.

- [ ] **Step 5: Run existing test suite (regression check)**

```bash
.venv/bin/pytest --no-cov -q
```

Expected: 122 tests pass, no new failures.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml requirements.txt fetchers/ parsers/ claude_max/
git commit -m "build: add pdfplumber + scaffold fetchers/parsers/claude_max packages"
```

---

### Task 3: Port `max_client.py` from the-brief

**Files:**
- Create: `claude_max/max_client.py`
- Test: `tests/test_max_client.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_max_client.py
"""Tests for the claude-p subprocess wrapper.

The wrapper invokes `claude -p` via subprocess and parses the JSON envelope
the CLI returns. We mock subprocess.run so tests don't actually call Claude.
"""
import json
import subprocess
from unittest.mock import patch

import pytest

from claude_max.max_client import MaxCallError, run_max


def _fake_cli_output(result_text: str = '{"value": 42}') -> str:
    return json.dumps({
        "result": result_text,
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "total_cost_usd": 0.0,
    })


def test_run_max_parses_clean_json_result():
    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout=_fake_cli_output(), stderr="")
    with patch("subprocess.run", return_value=fake):
        r = run_max(prompt="hi")
    assert r.parsed == {"value": 42}
    assert r.tokens["input"] == 100


def test_run_max_strips_markdown_fences():
    fake = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=_fake_cli_output('```json\n{"value": 7}\n```'),
        stderr="",
    )
    with patch("subprocess.run", return_value=fake):
        r = run_max(prompt="hi")
    assert r.parsed == {"value": 7}


def test_run_max_raises_on_nonzero_exit():
    fake = subprocess.CompletedProcess(args=[], returncode=2, stdout="", stderr="boom")
    with patch("subprocess.run", return_value=fake):
        with pytest.raises(MaxCallError, match="exited 2"):
            run_max(prompt="hi")


def test_run_max_raises_on_timeout():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1)):
        with pytest.raises(MaxCallError, match="timed out"):
            run_max(prompt="hi", timeout_s=1)


def test_run_max_uses_sonnet_default():
    captured: dict = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=_fake_cli_output(), stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        run_max(prompt="hi")
    assert "--model" in captured["argv"]
    assert "claude-sonnet-4-6" in captured["argv"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_max_client.py -v
```

Expected: FAIL with `ModuleNotFoundError: claude_max.max_client`.

- [ ] **Step 3: Write the implementation**

Copy the structure from `~/Projects/clauding-lab/the-brief/brief/claude/max_client.py` and adapt the default model to Sonnet 4.6:

```python
# claude_max/max_client.py
"""Subprocess wrapper around the `claude -p` Max CLI.

Mirrors brief/claude/max_client.py from the-brief. Adapted for EconDelta:
  - Default model: claude-sonnet-4-6 (the-brief defaults to opus)
  - Default effort: medium (the-brief uses high for headline curation)
  - One retry on transient quota errors

No Anthropic API calls. Auth is via the OS user's ~/.claude/.credentials.json
(Max OAuth), injected by the CLI itself — we pass no tokens.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n(.*?)\n```$", re.DOTALL)


def _strip_markdown_fences(text: str) -> str:
    stripped = text.strip()
    m = _FENCE_RE.match(stripped)
    if m:
        return m.group(1)
    return stripped if stripped != text else text


class MaxCallError(RuntimeError):
    """Raised when the CLI fails, times out, or returns non-JSON."""


@dataclass(frozen=True)
class MaxCallResult:
    raw_text: str
    parsed: Any | None
    usage: dict[str, Any]
    total_cost_usd: float | None
    duration_s: float = 0.0
    tokens: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0})


def run_max(
    *,
    prompt: str,
    model: str = "claude-sonnet-4-6",
    timeout_s: int = 300,
    claude_binary: str | None = None,
    effort: str = "medium",
) -> MaxCallResult:
    if claude_binary is None:
        claude_binary = os.environ.get("CLAUDE_BINARY", "claude")
    argv = [
        claude_binary, "-p", prompt,
        "--model", model,
        "--output-format", "json",
        "--no-session-persistence",
        "--tools", "",
        "--permission-mode", "bypassPermissions",
        "--effort", effort,
    ]
    _t0 = time.monotonic()
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as e:
        raise MaxCallError(f"Claude CLI timed out after {timeout_s}s") from e
    except FileNotFoundError as e:
        raise MaxCallError(f"Claude CLI binary not found: {claude_binary}") from e

    if cp.returncode != 0:
        raise MaxCallError(f"Claude CLI exited {cp.returncode}: {cp.stderr.strip()[:500]}")

    try:
        outer = json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        raise MaxCallError(f"Claude CLI stdout is not JSON: {e}") from e

    raw_text = outer.get("result", "")
    if not isinstance(raw_text, str):
        raise MaxCallError("Claude CLI returned non-string result field")

    parsed: Any | None
    try:
        parsed = json.loads(_strip_markdown_fences(raw_text))
    except json.JSONDecodeError:
        parsed = None

    duration = time.monotonic() - _t0
    usage = outer.get("usage") or {}
    return MaxCallResult(
        raw_text=raw_text,
        parsed=parsed,
        usage=usage,
        total_cost_usd=outer.get("total_cost_usd"),
        duration_s=duration,
        tokens={
            "input": int(usage.get("input_tokens") or 0),
            "output": int(usage.get("output_tokens") or 0),
        },
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_max_client.py -v
```

Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add claude_max/max_client.py tests/test_max_client.py
git commit -m "feat(claude_max): port max_client.py from the-brief, default to Sonnet 4.6"
```

---

## Phase 2 — Validators + base types

### Task 4: `claude_max/validators.py` — value sanity helpers

**Files:**
- Create: `claude_max/validators.py`
- Test: `tests/test_validators.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_validators.py
import pytest
from claude_max.validators import (
    InvalidValueError,
    validate_value,
    values_match,
)


def test_validate_value_passes_for_in_range_percent():
    validate_value(value=10.0, value_type="percent", valid_range=(0.5, 25.0))  # no raise


def test_validate_value_rejects_out_of_range():
    with pytest.raises(InvalidValueError, match="out of range"):
        validate_value(value=99.0, value_type="percent", valid_range=(0.5, 25.0))


def test_validate_value_rejects_wrong_type():
    with pytest.raises(InvalidValueError, match="expected number"):
        validate_value(value="abc", value_type="percent", valid_range=(0.5, 25.0))  # type: ignore[arg-type]


def test_values_match_floats_within_relative_tolerance():
    assert values_match(100.0, 100.4, value_type="percent")
    assert values_match(100.0, 100.6, value_type="percent") is False


def test_values_match_int_strict_equality():
    assert values_match(5, 5, value_type="count")
    assert values_match(5, 6, value_type="count") is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_validators.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
# claude_max/validators.py
"""Value sanity validators used by the hybrid parser.

A value is "valid" if it matches its declared `value_type` and falls inside
its `valid_range`. Two values "match within tolerance" if they're equal
(strict for ints / counts) or within 0.5% relative diff (floats).
"""
from __future__ import annotations

from typing import Final

from typing import Literal

ValueType = Literal["percent", "amount_bdt_crore", "amount_usd_bn", "ratio", "count", "rate"]

_FLOAT_RELATIVE_TOLERANCE: Final[float] = 0.005  # 0.5%


class InvalidValueError(ValueError):
    """Raised when a value fails type or range validation."""


def validate_value(*, value: object, value_type: ValueType, valid_range: tuple[float, float]) -> None:
    if value_type == "count":
        if not isinstance(value, int):
            raise InvalidValueError(f"expected int for {value_type}, got {type(value).__name__}")
    else:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise InvalidValueError(f"expected number for {value_type}, got {type(value).__name__}")

    lo, hi = valid_range
    if not (lo <= float(value) <= hi):
        raise InvalidValueError(f"value {value} out of range [{lo}, {hi}] for {value_type}")


def values_match(a: float | int, b: float | int, *, value_type: ValueType) -> bool:
    if value_type == "count":
        return a == b
    if a == 0 and b == 0:
        return True
    denominator = max(abs(a), abs(b))
    return abs(a - b) / denominator <= _FLOAT_RELATIVE_TOLERANCE
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_validators.py -v
```

Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add claude_max/validators.py tests/test_validators.py
git commit -m "feat(claude_max): add value sanity validators (range + tolerance)"
```

---

### Task 5: `fetchers/base.py` and `parsers/base.py` — shared types

**Files:**
- Create: `fetchers/base.py`
- Create: `parsers/base.py`
- Test: `tests/test_base_types.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_base_types.py
from datetime import datetime, timezone
from pathlib import Path

from fetchers.base import FetchError, FetchResult
from parsers.base import ParseError, ParseResult


def test_fetch_result_is_frozen_dataclass(tmp_path: Path):
    artifact = tmp_path / "x.pdf"
    artifact.write_bytes(b"hi")
    fr = FetchResult(
        indicator_id="x",
        artifact_path=artifact,
        artifact_type="pdf",
        fetched_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
        source_url="https://example.com",
        sha256="ab" * 32,
        cache_hit=False,
    )
    assert fr.indicator_id == "x"


def test_parse_result_carries_provenance():
    pr = ParseResult(value=10.0, _provenance="deterministic", _parse_strategy="html_footer_ticker")
    assert pr.value == 10.0
    assert pr._provenance == "deterministic"


def test_fetch_error_is_runtime_error():
    assert issubclass(FetchError, RuntimeError)
    assert issubclass(ParseError, RuntimeError)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_base_types.py -v
```

Expected: FAIL.

- [ ] **Step 3: Write `fetchers/base.py`**

```python
# fetchers/base.py
"""Shared types for Stage 1 (fetch).

A FetchResult points at a cached artifact on disk. The artifact is
replayable — Stage 2 reads it without touching the network.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal


class FetchError(RuntimeError):
    """Network failure, bot challenge, or discovery returned nothing."""


@dataclass(frozen=True)
class FetchResult:
    indicator_id: str
    artifact_path: Path
    artifact_type: Literal["pdf", "html"]
    fetched_at: datetime
    source_url: str
    sha256: str
    cache_hit: bool
```

- [ ] **Step 4: Write `parsers/base.py`**

```python
# parsers/base.py
"""Shared types for Stage 2 (parse).

ParseResult carries the extracted value plus provenance metadata that
flows into the final per-indicator snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Provenance = Literal["deterministic", "llm_extracted", "llm_corrected", "needs_review"]


class ParseError(RuntimeError):
    """Deterministic parser couldn't extract a value (caught -> LLM fallback)."""


@dataclass(frozen=True)
class ParseResult:
    value: float | int | str
    _provenance: Provenance = "deterministic"
    _parse_strategy: str = ""
    sanity_note: str | None = None
```

- [ ] **Step 5: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_base_types.py -v
```

Expected: 3 tests pass.

- [ ] **Step 6: Commit**

```bash
git add fetchers/base.py parsers/base.py tests/test_base_types.py
git commit -m "feat: add FetchResult and ParseResult dataclasses + error types"
```

---

## Phase 3 — Fetchers

### Task 6: `fetchers/html_fetcher.py` — playwright-stealth wrapper

**Files:**
- Create: `fetchers/html_fetcher.py`
- Test: `tests/test_html_fetcher.py`
- Reference: `scrapers/bb_forex.py` (existing playwright code; we extract a reusable helper, do not modify)

- [ ] **Step 1: Read the existing playwright pattern**

```bash
.venv/bin/python -c "import scrapers.bb_forex; print(scrapers.bb_forex.__file__)"
```

Open it and identify the playwright launch/context/page block. The new helper exposes one function: `fetch_html(url, indicator_id, snapshot_dir) -> FetchResult` that wraps the same browser launch behavior.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_html_fetcher.py
"""Integration test for fetch_html.

We launch a real Playwright Chromium against a tiny local HTML file served
via file:// URL, so no network is touched. The test verifies the helper:
- writes the rendered HTML to snapshot_dir
- returns a FetchResult with sha256 set
- detects cache_hit on a 2nd call with unchanged content
"""
from pathlib import Path

import pytest

from fetchers.html_fetcher import fetch_html


@pytest.fixture
def fixture_page(tmp_path: Path) -> str:
    p = tmp_path / "page.html"
    p.write_text("<html><body><table id='rates'><tr><td>USD/BDT</td><td>122.5</td></tr></table></body></html>")
    return p.as_uri()


def test_fetch_html_writes_snapshot_and_returns_result(fixture_page, tmp_path):
    snap_dir = tmp_path / "snaps"
    fr = fetch_html(url=fixture_page, indicator_id="usd_bdt_test", snapshot_dir=snap_dir)
    assert fr.indicator_id == "usd_bdt_test"
    assert fr.artifact_type == "html"
    assert fr.artifact_path.exists()
    assert "USD/BDT" in fr.artifact_path.read_text()
    assert len(fr.sha256) == 64
    assert fr.cache_hit is False


def test_fetch_html_detects_cache_hit_on_unchanged_content(fixture_page, tmp_path):
    snap_dir = tmp_path / "snaps"
    fetch_html(url=fixture_page, indicator_id="usd_bdt_test", snapshot_dir=snap_dir)
    fr2 = fetch_html(url=fixture_page, indicator_id="usd_bdt_test", snapshot_dir=snap_dir)
    assert fr2.cache_hit is True
```

- [ ] **Step 3: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_html_fetcher.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 4: Write the implementation**

```python
# fetchers/html_fetcher.py
"""Playwright-stealth wrapper for HTML fetching.

Reuses the stealth context pattern from scrapers/bb_forex.py. Persists
rendered HTML to data/_html/<indicator_id>/<YYYY-MM-DD>.html and detects
content-unchanged via sha256 across runs.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from fetchers.base import FetchError, FetchResult


def fetch_html(*, url: str, indicator_id: str, snapshot_dir: Path) -> FetchResult:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = snapshot_dir / f"{today}.html"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context()
            Stealth().apply_stealth_sync(ctx)
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            html = page.content()
            browser.close()
    except Exception as e:
        raise FetchError(f"playwright fetch failed for {url}: {e}") from e

    sha = hashlib.sha256(html.encode("utf-8")).hexdigest()
    cache_hit = out_path.exists() and (
        hashlib.sha256(out_path.read_bytes()).hexdigest() == sha
    )
    if not cache_hit:
        out_path.write_text(html)
    return FetchResult(
        indicator_id=indicator_id,
        artifact_path=out_path,
        artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url=url,
        sha256=sha,
        cache_hit=cache_hit,
    )
```

- [ ] **Step 5: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_html_fetcher.py -v
```

Expected: 2 tests pass. (Requires playwright chromium installed locally — `playwright install chromium` if not.)

- [ ] **Step 6: Commit**

```bash
git add fetchers/html_fetcher.py tests/test_html_fetcher.py
git commit -m "feat(fetchers): add playwright-stealth html_fetcher with sha256 cache"
```

---

### Task 7: `fetchers/pdf_fetcher.py` — download + cache + dedupe

**Files:**
- Create: `fetchers/pdf_fetcher.py`
- Test: `tests/test_pdf_fetcher.py`
- Test fixture: `tests/fixtures/sample.pdf` (any small PDF — generate via reportlab in test setup)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pdf_fetcher.py
"""Tests pdf_fetcher against an in-process HTTP server serving a fixture PDF.

We don't hit BB. The test verifies:
- HTTP fetch writes file under snapshot_dir/_pdfs/<indicator>/<YYYY-MM>/
- A meta.json sidecar records source_url + sha256 + page_count
- Re-fetch with unchanged bytes returns cache_hit=True without rewriting
"""
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from fetchers.pdf_fetcher import fetch_pdf

# Smallest valid 1-page PDF, hand-rolled so the test has no fixture dep.
_TINY_PDF = (
    b"%PDF-1.1\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
    b"0000000052 00000 n \n0000000099 00000 n \ntrailer<</Size 4/Root 1 0 R>>\n"
    b"startxref\n151\n%%EOF\n"
)


@pytest.fixture
def pdf_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(_TINY_PDF)))
            self.end_headers()
            self.wfile.write(_TINY_PDF)
        def log_message(self, *a, **kw):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/file.pdf"
    server.shutdown()


def test_fetch_pdf_caches_with_sidecar(pdf_server, tmp_path: Path):
    fr = fetch_pdf(
        url=pdf_server,
        indicator_id="bb_mei",
        snapshot_dir=tmp_path,
        as_of_month="2026-04",
    )
    assert fr.artifact_path.exists()
    assert fr.artifact_path.suffix == ".pdf"
    meta = fr.artifact_path.with_suffix(".meta.json")
    assert meta.exists()
    assert fr.sha256 == hashlib.sha256(_TINY_PDF).hexdigest()
    assert fr.cache_hit is False


def test_fetch_pdf_detects_cache_hit(pdf_server, tmp_path: Path):
    fetch_pdf(url=pdf_server, indicator_id="bb_mei", snapshot_dir=tmp_path, as_of_month="2026-04")
    fr2 = fetch_pdf(url=pdf_server, indicator_id="bb_mei", snapshot_dir=tmp_path, as_of_month="2026-04")
    assert fr2.cache_hit is True
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_pdf_fetcher.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
# fetchers/pdf_fetcher.py
"""HTTP fetcher for PDFs with sha256 dedup.

Writes <snapshot_dir>/_pdfs/<indicator_id>/<as_of_month>/<source-derived-name>.pdf
plus a sibling .meta.json sidecar (source_url, fetched_at, sha256, page_count, byte_size).
On re-fetch, if computed sha256 matches the existing file, skips the write and
returns FetchResult.cache_hit=True.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import pdfplumber

from fetchers.base import FetchError, FetchResult


def _derive_filename(url: str) -> str:
    parsed = urlparse(url)
    name = Path(parsed.path).name or "download.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def fetch_pdf(*, url: str, indicator_id: str, snapshot_dir: Path, as_of_month: str) -> FetchResult:
    """Download a PDF from `url` and cache it under snapshot_dir."""
    out_dir = snapshot_dir / "_pdfs" / indicator_id / as_of_month
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / _derive_filename(url)

    try:
        req = Request(url, headers={"User-Agent": "EconDelta/3.0"})
        with urlopen(req, timeout=60) as resp:
            body = resp.read()
    except Exception as e:
        raise FetchError(f"PDF download failed for {url}: {e}") from e

    sha = hashlib.sha256(body).hexdigest()
    cache_hit = out_path.exists() and hashlib.sha256(out_path.read_bytes()).hexdigest() == sha
    if not cache_hit:
        out_path.write_bytes(body)
        page_count = _safe_page_count(out_path)
        sidecar = {
            "source_url": url,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "sha256": sha,
            "page_count": page_count,
            "byte_size": len(body),
        }
        out_path.with_suffix(".meta.json").write_text(json.dumps(sidecar, indent=2))

    return FetchResult(
        indicator_id=indicator_id,
        artifact_path=out_path,
        artifact_type="pdf",
        fetched_at=datetime.now(timezone.utc),
        source_url=url,
        sha256=sha,
        cache_hit=cache_hit,
    )


def _safe_page_count(path: Path) -> int:
    try:
        with pdfplumber.open(path) as pdf:
            return len(pdf.pages)
    except Exception:
        return 0
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_pdf_fetcher.py -v
```

Expected: 2 tests pass.

- [ ] **Step 5: Commit**

```bash
git add fetchers/pdf_fetcher.py tests/test_pdf_fetcher.py
git commit -m "feat(fetchers): add pdf_fetcher with sha256 dedup + meta sidecar"
```

---

### Task 8: `fetchers/pdf_discovery.py` — find latest PDF on index page

**Files:**
- Create: `fetchers/pdf_discovery.py`
- Test: `tests/test_pdf_discovery.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pdf_discovery.py
"""Tests the BB-publication-page link discovery helper.

The fixture HTML mimics the structure of bb.org.bd publication pages: a
month-grouped table of <a href="...pdf"> links. The helper picks the most
recent month present.
"""
import pytest

from fetchers.pdf_discovery import discover_latest_pdf_link

_FIXTURE_HTML = """
<html><body>
<table>
  <tr><td>March 2026</td><td><a href="/files/pub/3-11/march-2026.pdf">Download</a></td></tr>
  <tr><td>February 2026</td><td><a href="/files/pub/3-11/feb-2026.pdf">Download</a></td></tr>
  <tr><td>April 2026</td><td><a href="/files/pub/3-11/april-2026.pdf">Download</a></td></tr>
</table>
</body></html>
"""


def test_discover_latest_pdf_picks_most_recent_month():
    base = "https://www.bb.org.bd"
    link = discover_latest_pdf_link(html=_FIXTURE_HTML, base_url=base)
    assert link == f"{base}/files/pub/3-11/april-2026.pdf"


def test_discover_latest_pdf_raises_on_empty():
    with pytest.raises(ValueError, match="no.*pdf"):
        discover_latest_pdf_link(html="<html></html>", base_url="https://example.com")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_pdf_discovery.py -v
```

Expected: FAIL.

- [ ] **Step 3: Write the implementation**

```python
# fetchers/pdf_discovery.py
"""Find the most recent PDF link on a Bangladesh Bank publication index page.

BB publication pages list "Month YYYY -> [link]" rows. We find every <a>
whose href ends with .pdf, infer a (year, month) from surrounding text via
a small regex, and return the absolute URL of the most-recent one.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}
_MONTH_RE = re.compile(
    r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{4})",
    re.IGNORECASE,
)


def discover_latest_pdf_link(*, html: str, base_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[tuple[tuple[int, int], str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.lower().endswith(".pdf"):
            continue
        # Look at the surrounding row text (parent <tr> if present, else <a> text)
        row = a.find_parent("tr") or a
        text = row.get_text(" ", strip=True).lower()
        m = _MONTH_RE.search(text)
        if not m:
            continue
        month_num = _MONTHS[m.group(1).lower()]
        year = int(m.group(2))
        candidates.append(((year, month_num), urljoin(base_url, href)))
    if not candidates:
        raise ValueError("no PDF links with month/year context found")
    candidates.sort(reverse=True)
    return candidates[0][1]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_pdf_discovery.py -v
```

Expected: 2 tests pass.

- [ ] **Step 5: Commit**

```bash
git add fetchers/pdf_discovery.py tests/test_pdf_discovery.py
git commit -m "feat(fetchers): add pdf_discovery helper for BB publication index pages"
```

---

### Task 9: `fetch_all.py` entry point

**Files:**
- Create: `fetch_all.py`
- Test: `tests/test_fetch_all.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fetch_all.py
"""Tests fetch_all entry point with mocked fetchers."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import fetch_all


def _registry_with_two_indicators() -> dict:
    return {
        "version": "3.0",
        "indicators": [
            {
                "id": "policy_rate",
                "cadence": "daily",
                "fetch": {"type": "html", "url": "https://www.bb.org.bd/en/"},
                "domain": "money_market",
            },
            {
                "id": "broad_money",
                "cadence": "monthly",
                "fetch": {
                    "type": "pdf",
                    "url": "https://www.bb.org.bd/en/index.php/publication/publictn/5/27",
                    "discover": "latest_pdf_link",
                    "task": "Component 11a",
                },
                "domain": "monetary_aggregates",
            },
        ],
    }


def test_fetch_all_dispatches_html_and_pdf(tmp_path: Path):
    cfg = tmp_path / "sources-v3.json"
    cfg.write_text(json.dumps(_registry_with_two_indicators()))

    with patch("fetch_all.fetch_html") as html_mock, patch("fetch_all.fetch_pdf") as pdf_mock:
        html_mock.return_value = MagicMock(cache_hit=False, indicator_id="policy_rate")
        pdf_mock.return_value = MagicMock(cache_hit=False, indicator_id="broad_money")
        with patch("fetch_all.discover_latest_pdf_link", return_value="https://example.com/x.pdf"), \
             patch("fetch_all._download_index_html", return_value="<html></html>"):
            results = fetch_all.run(config_path=cfg, data_root=tmp_path / "data")

    assert len(results) == 2
    html_mock.assert_called_once()
    pdf_mock.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_fetch_all.py -v
```

Expected: FAIL.

- [ ] **Step 3: Write the implementation**

```python
# fetch_all.py
"""Stage 1 entry point: walk sources-v3.json, fetch every due indicator,
write artifacts under data/_pdfs/ and data/_html/.

Usage:
    python fetch_all.py [--dry-run] [--config config/sources-v3.json] [--only INDICATOR_ID]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from fetchers.base import FetchError, FetchResult
from fetchers.html_fetcher import fetch_html
from fetchers.pdf_discovery import discover_latest_pdf_link
from fetchers.pdf_fetcher import fetch_pdf

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "sources-v3.json"
DEFAULT_DATA_ROOT = REPO_ROOT / "data"

logger = logging.getLogger("fetch_all")


def _download_index_html(url: str) -> str:
    req = Request(url, headers={"User-Agent": "EconDelta/3.0"})
    with urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


def _fetch_one(indicator: dict, data_root: Path) -> FetchResult | None:
    fetch_block = indicator["fetch"]
    indicator_id = indicator["id"]
    if fetch_block["type"] == "html":
        return fetch_html(
            url=fetch_block["url"],
            indicator_id=indicator_id,
            snapshot_dir=data_root / "_html" / indicator_id,
        )
    if fetch_block["type"] == "pdf":
        url = fetch_block["url"]
        if fetch_block.get("discover") == "latest_pdf_link":
            html = _download_index_html(url)
            url = discover_latest_pdf_link(html=html, base_url=url)
        as_of_month = datetime.now(timezone.utc).strftime("%Y-%m")
        return fetch_pdf(
            url=url,
            indicator_id=indicator_id,
            snapshot_dir=data_root,
            as_of_month=as_of_month,
        )
    logger.warning("unsupported fetch.type=%s for %s", fetch_block.get("type"), indicator_id)
    return None


def run(*, config_path: Path, data_root: Path, only: str | None = None, dry_run: bool = False) -> list[FetchResult]:
    cfg = json.loads(config_path.read_text())
    results: list[FetchResult] = []
    for ind in cfg["indicators"]:
        if only and ind["id"] != only:
            continue
        if dry_run:
            logger.info("[dry-run] would fetch %s (%s)", ind["id"], ind["fetch"]["type"])
            continue
        try:
            r = _fetch_one(ind, data_root)
        except FetchError as e:
            logger.error("fetch_failed: %s — %s", ind["id"], e)
            continue
        if r:
            results.append(r)
            logger.info("fetched %s sha=%s cache_hit=%s", r.indicator_id, r.sha256[:8], r.cache_hit)
    return results


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    p.add_argument("--only", type=str, default=None)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    results = run(config_path=args.config, data_root=args.data_root, only=args.only, dry_run=args.dry_run)
    cache_hits = sum(1 for r in results if r.cache_hit)
    print(f"Fetched: {len(results)} · Cache hits: {cache_hits} · Failed: see log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_fetch_all.py -v
```

Expected: 1 test passes.

- [ ] **Step 5: Commit**

```bash
git add fetch_all.py tests/test_fetch_all.py
git commit -m "feat: add fetch_all.py Stage 1 entry point"
```

---

## Phase 4 — Parsers

### Task 10: `parsers/registry.py` — strategy registration

**Files:**
- Create: `parsers/registry.py`
- Test: `tests/test_parser_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_parser_registry.py
import pytest

from parsers.base import ParseResult
from parsers.registry import REGISTRY, get_parser, register


def test_register_adds_to_registry():
    @register("dummy_test_parser")
    class _D:
        def parse(self, artifact, instruction):  # pragma: no cover - dummy
            return ParseResult(value=1.0)

    assert "dummy_test_parser" in REGISTRY
    p = get_parser("dummy_test_parser")
    assert p is not None


def test_get_parser_unknown_raises():
    with pytest.raises(KeyError):
        get_parser("nonexistent_parser")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_parser_registry.py -v
```

Expected: FAIL.

- [ ] **Step 3: Write the implementation**

```python
# parsers/registry.py
"""Parser strategy registry.

Decorator pattern: @register("name") attaches an instance to REGISTRY. The
hybrid orchestrator looks up parsers by the `parse.deterministic` field in
sources-v3.json.
"""
from __future__ import annotations

from typing import Protocol

from fetchers.base import FetchResult
from parsers.base import ParseResult


class Parser(Protocol):
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult: ...


REGISTRY: dict[str, Parser] = {}


def register(name: str):
    def decorator(cls: type) -> type:
        REGISTRY[name] = cls()
        return cls
    return decorator


def get_parser(name: str) -> Parser:
    if name not in REGISTRY:
        raise KeyError(f"no parser registered for {name!r}; have: {sorted(REGISTRY)}")
    return REGISTRY[name]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_parser_registry.py -v
```

Expected: 2 tests pass.

- [ ] **Step 5: Commit**

```bash
git add parsers/registry.py tests/test_parser_registry.py
git commit -m "feat(parsers): add parser strategy registry"
```

---

### Task 11: `parsers/html_footer_ticker.py`

**Files:**
- Create: `parsers/html_footer_ticker.py`
- Test: `tests/test_html_footer_ticker.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_html_footer_ticker.py
from datetime import datetime, timezone
from pathlib import Path

import pytest

import parsers.html_footer_ticker  # noqa: F401 — registers
from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.registry import get_parser

_HTML = """
<html><body>
<div class="ticker">USD/BDT 122.50 EUR/BDT 132.10 Policy Rate 10.00% SLF 11.50% SDF 8.50%</div>
</body></html>
"""


@pytest.fixture
def fixture_artifact(tmp_path: Path) -> FetchResult:
    p = tmp_path / "page.html"
    p.write_text(_HTML)
    return FetchResult(
        indicator_id="policy_rate_slf_sdf",
        artifact_path=p,
        artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.bb.org.bd/en/",
        sha256="x" * 64,
        cache_hit=False,
    )


def test_extracts_policy_rate(fixture_artifact):
    p = get_parser("html_footer_ticker")
    r = p.parse(fixture_artifact, instruction="Policy Rate")
    assert r.value == 10.00
    assert r._parse_strategy == "html_footer_ticker"


def test_raises_on_label_not_found(fixture_artifact):
    p = get_parser("html_footer_ticker")
    with pytest.raises(ParseError, match="not found"):
        p.parse(fixture_artifact, instruction="Nonexistent Indicator")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_html_footer_ticker.py -v
```

Expected: FAIL.

- [ ] **Step 3: Write the implementation**

```python
# parsers/html_footer_ticker.py
"""Parser for the bb.org.bd footer ticker (policy rate, SLF, SDF, USD/BDT).

The instruction names a label (e.g. "Policy Rate"). We find it in the rendered
HTML text and grab the numeric token immediately after.
"""
from __future__ import annotations

import re

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register


@register("html_footer_ticker")
class HtmlFooterTickerParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        text = artifact.artifact_path.read_text()
        # Strip HTML tags crudely — the ticker text is concatenated in <div class="ticker">.
        plain = re.sub(r"<[^>]+>", " ", text)
        # Match: <label>(any whitespace)(number)(optional %)
        pattern = re.escape(instruction) + r"\s*([0-9]+(?:\.[0-9]+)?)\s*%?"
        m = re.search(pattern, plain, re.IGNORECASE)
        if not m:
            raise ParseError(f"label {instruction!r} not found in HTML")
        return ParseResult(value=float(m.group(1)), _parse_strategy="html_footer_ticker")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_html_footer_ticker.py -v
```

Expected: 2 tests pass.

- [ ] **Step 5: Commit**

```bash
git add parsers/html_footer_ticker.py tests/test_html_footer_ticker.py
git commit -m "feat(parsers): add html_footer_ticker for bb.org.bd ticker indicators"
```

---

### Task 12: `parsers/html_table_row.py`

**Files:**
- Create: `parsers/html_table_row.py`
- Test: `tests/test_html_table_row.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_html_table_row.py
from datetime import datetime, timezone
from pathlib import Path

import pytest

import parsers.html_table_row  # registers
from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.registry import get_parser

_HTML = """
<html><body>
<h1>Marketable T-Bills Outstanding</h1>
<table>
  <tr><th>Tenor</th><th>Outstanding (BDT crore)</th></tr>
  <tr><td>91-day</td><td>50,000</td></tr>
  <tr><td>182-day</td><td>30,000</td></tr>
  <tr><td>364-day</td><td>20,000</td></tr>
  <tr><td><b>Total</b></td><td><b>100,000</b></td></tr>
</table>
</body></html>
"""


@pytest.fixture
def fixture_artifact(tmp_path: Path) -> FetchResult:
    p = tmp_path / "page.html"
    p.write_text(_HTML)
    return FetchResult(
        indicator_id="treasury_bill_outstanding",
        artifact_path=p,
        artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://gsom.bb.org.bd/mtm-bill.php",
        sha256="x" * 64,
        cache_hit=False,
    )


def test_extracts_table_total_row(fixture_artifact):
    p = get_parser("html_table_row")
    # instruction format: "row=Total col=2"
    r = p.parse(fixture_artifact, instruction="row=Total col=2")
    assert r.value == 100_000.0


def test_raises_on_row_not_found(fixture_artifact):
    p = get_parser("html_table_row")
    with pytest.raises(ParseError):
        p.parse(fixture_artifact, instruction="row=Nope col=2")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_html_table_row.py -v
```

Expected: FAIL.

- [ ] **Step 3: Write the implementation**

```python
# parsers/html_table_row.py
"""Parser for HTML pages with a single relevant table.

Instruction syntax: "row=<label> col=<1-based index>". Finds the row whose
first cell text contains <label> and extracts the number at <col>.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register


def _parse_instruction(instruction: str) -> tuple[str, int]:
    parts = dict(p.split("=", 1) for p in instruction.split() if "=" in p)
    if "row" not in parts or "col" not in parts:
        raise ParseError(f"instruction must be 'row=<label> col=<int>', got {instruction!r}")
    return parts["row"], int(parts["col"])


def _to_number(text: str) -> float:
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    if not cleaned:
        raise ParseError(f"no number in cell text {text!r}")
    return float(cleaned)


@register("html_table_row")
class HtmlTableRowParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        row_label, col = _parse_instruction(instruction)
        soup = BeautifulSoup(artifact.artifact_path.read_text(), "html.parser")
        for tr in soup.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            first = cells[0].get_text(strip=True)
            if row_label.lower() in first.lower():
                if len(cells) <= col:
                    raise ParseError(f"row {row_label!r} has only {len(cells)} cells, need col {col}")
                return ParseResult(
                    value=_to_number(cells[col - 1].get_text(strip=True)) if col >= 1 else _to_number(cells[col].get_text(strip=True)),
                    _parse_strategy="html_table_row",
                )
        raise ParseError(f"row matching {row_label!r} not found")
```

Note: there's an off-by-one risk with col indexing. Use `col - 1` consistently for 1-based callers. Adjust the test fixture accordingly if needed during implementation.

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_html_table_row.py -v
```

Expected: 2 tests pass.

- [ ] **Step 5: Commit**

```bash
git add parsers/html_table_row.py tests/test_html_table_row.py
git commit -m "feat(parsers): add html_table_row for gsom.bb.org.bd-style pages"
```

---

### Task 13: `parsers/html_call_money.py`

**Files:**
- Create: `parsers/html_call_money.py`
- Test: `tests/test_html_call_money.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_html_call_money.py
from datetime import datetime, timezone
from pathlib import Path
import pytest

import parsers.html_call_money  # registers
from fetchers.base import FetchResult
from parsers.registry import get_parser

_HTML = """
<table>
<tr><th>Tenor</th><th>Rate (%)</th></tr>
<tr><td>1D</td><td>9.50</td></tr>
<tr><td>7D</td><td>9.75</td></tr>
<tr><td>14D</td><td>10.10</td></tr>
<tr><td>90D</td><td>10.50</td></tr>
</table>
"""


@pytest.fixture
def artifact(tmp_path: Path):
    p = tmp_path / "p.html"; p.write_text(_HTML)
    return FetchResult(indicator_id="call_money_rate", artifact_path=p, artifact_type="html",
                       fetched_at=datetime.now(timezone.utc), source_url="x", sha256="x"*64, cache_hit=False)


def test_extracts_all_tenors_as_dict(artifact):
    p = get_parser("html_call_money")
    r = p.parse(artifact, instruction="all_tenors")
    assert r.value == {"1D": 9.50, "7D": 9.75, "14D": 10.10, "90D": 10.50}
```

- [ ] **Step 2-4: Implement, run, verify**

```python
# parsers/html_call_money.py
"""Extracts the four call-money tenors (1D/7D/14D/90D) as a dict from
bb.org.bd's call money market page."""
from __future__ import annotations

from bs4 import BeautifulSoup

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register

_TENORS = {"1D", "7D", "14D", "90D"}


@register("html_call_money")
class HtmlCallMoneyParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        soup = BeautifulSoup(artifact.artifact_path.read_text(), "html.parser")
        out: dict[str, float] = {}
        for tr in soup.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) >= 2 and cells[0] in _TENORS:
                try:
                    out[cells[0]] = float(cells[1])
                except ValueError:
                    continue
        if len(out) < 4:
            raise ParseError(f"expected 4 tenors, got {sorted(out)}")
        return ParseResult(value=out, _parse_strategy="html_call_money")
```

```bash
.venv/bin/pytest tests/test_html_call_money.py -v
```

- [ ] **Step 5: Commit**

```bash
git add parsers/html_call_money.py tests/test_html_call_money.py
git commit -m "feat(parsers): add html_call_money multi-tenor extractor"
```

---

### Task 14: `parsers/pdf_component.py`

**Files:**
- Create: `parsers/pdf_component.py`
- Test: `tests/test_pdf_component.py`
- Test fixture: a 1-page PDF generated in the test setup

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pdf_component.py
from datetime import datetime, timezone
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

import parsers.pdf_component  # registers
from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.registry import get_parser


@pytest.fixture
def pdf_artifact(tmp_path: Path) -> FetchResult:
    pdf_path = tmp_path / "test.pdf"
    c = canvas.Canvas(str(pdf_path))
    c.drawString(100, 800, "Component 11a Broad Money: 1900000")
    c.drawString(100, 780, "Component 12c Private Sector Credit: 1500000")
    c.showPage()
    c.save()
    return FetchResult(
        indicator_id="broad_money", artifact_path=pdf_path, artifact_type="pdf",
        fetched_at=datetime.now(timezone.utc), source_url="x", sha256="x"*64, cache_hit=False,
    )


def test_extracts_component_value(pdf_artifact):
    p = get_parser("pdf_component")
    r = p.parse(pdf_artifact, instruction="Component 11a")
    assert r.value == 1_900_000.0


def test_raises_when_component_missing(pdf_artifact):
    p = get_parser("pdf_component")
    with pytest.raises(ParseError):
        p.parse(pdf_artifact, instruction="Component 99z")
```

Add `reportlab>=4.0,<5` to `requirements-dev.txt` (test-only dep for synthesizing PDFs).

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pip install "reportlab>=4.0,<5"
.venv/bin/pytest tests/test_pdf_component.py -v
```

Expected: FAIL.

- [ ] **Step 3: Write the implementation**

```python
# parsers/pdf_component.py
"""Parser for "Component <ID>" labeled values in BB Monthly Economic Indicators PDFs.

These PDFs prefix each indicator with "Component 11a", "Component 12c", etc.
We extract the page text via pdfplumber and grep for the label.
"""
from __future__ import annotations

import re

import pdfplumber

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register


@register("pdf_component")
class PdfComponentParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        with pdfplumber.open(artifact.artifact_path) as pdf:
            full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        pattern = re.escape(instruction) + r"[^\d\-]*([\-]?[0-9][0-9,\.]*)"
        m = re.search(pattern, full_text, re.IGNORECASE)
        if not m:
            raise ParseError(f"component {instruction!r} not found in PDF")
        cleaned = m.group(1).replace(",", "")
        return ParseResult(value=float(cleaned), _parse_strategy="pdf_component")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_pdf_component.py -v
```

Expected: 2 tests pass.

- [ ] **Step 5: Commit**

```bash
git add parsers/pdf_component.py tests/test_pdf_component.py requirements-dev.txt
git commit -m "feat(parsers): add pdf_component parser for BB MEI labeled values"
```

---

### Task 15: `parsers/pdf_table_row.py`

**Files:**
- Create: `parsers/pdf_table_row.py`
- Test: `tests/test_pdf_table_row.py`

- [ ] **Step 1-5: Same TDD pattern as Task 14**

Test extracts a value from a known row of a known table on a known page (use pdfplumber's `pages[N].extract_tables()`).

```python
# parsers/pdf_table_row.py
"""Extract a value from a specific (page, table_index, row_label) triple in a PDF.

Instruction syntax: "page=<N> table=<K> row=<label> col=<1-based int>".
Uses pdfplumber's extract_tables() — the same library that ships first-class
table extraction. Falls through to ParseError if the table isn't grid-shaped.
"""
from __future__ import annotations

import pdfplumber

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register
from parsers.html_table_row import _to_number  # reuse number cleaner


def _parse_instruction(instruction: str) -> dict:
    out = {}
    for token in instruction.split():
        if "=" in token:
            k, v = token.split("=", 1)
            out[k] = v
    for k in ("page", "table", "row", "col"):
        if k not in out:
            raise ParseError(f"instruction missing {k}: {instruction!r}")
    return out


@register("pdf_table_row")
class PdfTableRowParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        ins = _parse_instruction(instruction)
        page_idx = int(ins["page"]) - 1
        tbl_idx = int(ins["table"]) - 1
        row_label = ins["row"]
        col = int(ins["col"]) - 1
        with pdfplumber.open(artifact.artifact_path) as pdf:
            if page_idx >= len(pdf.pages):
                raise ParseError(f"page {ins['page']} > {len(pdf.pages)} pages")
            tables = pdf.pages[page_idx].extract_tables()
        if tbl_idx >= len(tables):
            raise ParseError(f"table {ins['table']} > {len(tables)} on page {ins['page']}")
        for row in tables[tbl_idx]:
            if row and row[0] and row_label.lower() in str(row[0]).lower():
                if col >= len(row):
                    raise ParseError(f"row has {len(row)} cols, need {ins['col']}")
                cell = row[col]
                if cell is None:
                    raise ParseError(f"cell at col {col} is empty")
                return ParseResult(value=_to_number(str(cell)), _parse_strategy="pdf_table_row")
        raise ParseError(f"row {row_label!r} not found in page {ins['page']} table {ins['table']}")
```

Test:

```python
# tests/test_pdf_table_row.py
from datetime import datetime, timezone
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table

import parsers.pdf_table_row  # registers
from fetchers.base import FetchResult
from parsers.registry import get_parser


@pytest.fixture
def pdf_artifact(tmp_path: Path) -> FetchResult:
    pdf_path = tmp_path / "table.pdf"
    doc = SimpleDocTemplate(str(pdf_path), pagesize=letter)
    table = Table([["Tenor", "Outstanding"], ["91-day", "50000"], ["Total", "100000"]])
    doc.build([table])
    return FetchResult(indicator_id="x", artifact_path=pdf_path, artifact_type="pdf",
                       fetched_at=datetime.now(timezone.utc), source_url="x", sha256="x"*64, cache_hit=False)


def test_extracts_total_row(pdf_artifact):
    p = get_parser("pdf_table_row")
    r = p.parse(pdf_artifact, instruction="page=1 table=1 row=Total col=2")
    assert r.value == 100_000.0
```

```bash
.venv/bin/pytest tests/test_pdf_table_row.py -v
```

```bash
git add parsers/pdf_table_row.py tests/test_pdf_table_row.py
git commit -m "feat(parsers): add pdf_table_row for page+table+row indexed extraction"
```

---

### Task 16: `parsers/pdf_table_total.py`

**Files:**
- Create: `parsers/pdf_table_total.py`
- Test: `tests/test_pdf_table_total.py`

- [ ] Same TDD pattern. Implementation finds the largest table on a given page and returns the bottom-right cell ("last total of the page").

```python
# parsers/pdf_table_total.py
"""Extract the bottom-right number from the last table on a page.

Instruction syntax: "page=<N>" or empty (defaults to page 1). Used when the
v2 instruction is "Last total of the page".
"""
from __future__ import annotations

import pdfplumber

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.html_table_row import _to_number
from parsers.registry import register


@register("pdf_table_total")
class PdfTableTotalParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        page_idx = 0
        for tok in instruction.split():
            if tok.startswith("page="):
                page_idx = int(tok.split("=", 1)[1]) - 1
        with pdfplumber.open(artifact.artifact_path) as pdf:
            if page_idx >= len(pdf.pages):
                raise ParseError(f"page {page_idx + 1} out of range")
            tables = pdf.pages[page_idx].extract_tables()
        if not tables:
            raise ParseError(f"no tables on page {page_idx + 1}")
        last_table = tables[-1]
        last_row = last_table[-1]
        # Find the rightmost non-None cell with a number
        for cell in reversed(last_row):
            if cell is None:
                continue
            try:
                return ParseResult(value=_to_number(str(cell)), _parse_strategy="pdf_table_total")
            except ParseError:
                continue
        raise ParseError("no numeric cell in last row of last table")
```

Test mirrors Task 15's. Commit:
```bash
git add parsers/pdf_table_total.py tests/test_pdf_table_total.py
git commit -m "feat(parsers): add pdf_table_total for 'last total of page' instructions"
```

---

## Phase 5 — Hybrid orchestrator + Stage 2 entry

### Task 17: Prompt templates for Sonnet

**Files:**
- Create: `claude_max/prompts/sanity_check.txt`
- Create: `claude_max/prompts/html_footer_ticker.txt`
- Create: `claude_max/prompts/pdf_component.txt`
- Create: `claude_max/prompts/pdf_table_row.txt`
- Create: `claude_max/prompts/pdf_table_total.txt`
- Create: `claude_max/prompts/html_table_row.txt`
- Create: `claude_max/prompts/html_call_money.txt`

- [ ] **Step 1: Write `sanity_check.txt`**

```text
You are validating an extracted economic indicator.

Indicator: {indicator_name}
Domain: {domain}
Cadence: {cadence}
Extracted value: {value}
Value type: {value_type}
Valid range: {valid_range}
Last 3 known values: {history}

Is this value plausible given the indicator's typical scale, recent history,
and value_type? Reply ONLY with strict JSON of the form:

{"plausible": true|false, "reason": "<1 sentence>"}
```

- [ ] **Step 2: Write `pdf_component.txt`**

```text
You are extracting an economic indicator from a Bangladesh Bank PDF.

Indicator: {indicator_name}
Instruction: {instruction}
Value type: {value_type}
Valid range: {valid_range}

PDF page text follows between <<<PDF_TEXT>>> markers. Find the value the
instruction points at. Numbers may use commas (1,234,567 = 1234567). Strip
commas and return a single number.

<<<PDF_TEXT>>>
{pdf_text}
<<<END>>>

Reply ONLY with strict JSON of the form:

{"value": <number>, "reason": "<1 sentence on where you found it>"}
```

- [ ] **Step 3: Write the other 5 prompt files** with analogous structure (instruction-aware text body, strict JSON output spec).

- [ ] **Step 4: Verify they're loadable**

```bash
ls claude_max/prompts/
.venv/bin/python -c "from pathlib import Path; assert all((Path('claude_max/prompts') / n).exists() for n in ['sanity_check.txt','html_footer_ticker.txt','pdf_component.txt','pdf_table_row.txt','pdf_table_total.txt','html_table_row.txt','html_call_money.txt'])"
```

- [ ] **Step 5: Commit**

```bash
git add claude_max/prompts/
git commit -m "feat(claude_max): add prompt templates for parser strategies + sanity check"
```

---

### Task 18: Hybrid `parse_one` orchestrator

**Files:**
- Create: `parsers/hybrid.py`
- Test: `tests/test_hybrid.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hybrid.py
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import parsers.html_footer_ticker  # registers
from fetchers.base import FetchResult
from parsers.hybrid import parse_one


def _ticker_artifact(tmp_path):
    p = tmp_path / "x.html"
    p.write_text("<html><body>Policy Rate 10.0%</body></html>")
    return FetchResult(indicator_id="policy_rate_slf_sdf", artifact_path=p, artifact_type="html",
                       fetched_at=datetime.now(timezone.utc), source_url="x", sha256="x"*64, cache_hit=False)


def test_deterministic_path_emits_value_when_sonnet_agrees(tmp_path):
    indicator = {
        "id": "policy_rate_slf_sdf", "name": "Policy Rate", "domain": "money_market",
        "cadence": "daily",
        "fetch": {"task": "Policy Rate"},
        "parse": {"deterministic": "html_footer_ticker", "value_type": "percent",
                  "valid_range": [0.5, 25.0], "llm_prompt": "html_footer_ticker.txt"},
    }
    fake_sanity = type("R", (), {"parsed": {"plausible": True, "reason": "ok"}, "raw_text": ""})()
    with patch("parsers.hybrid._sanity_check", return_value=fake_sanity):
        snapshot = parse_one(_ticker_artifact(tmp_path), indicator, history=[])
    assert snapshot["value"] == 10.0
    assert snapshot["_provenance"] == "deterministic"


def test_falls_back_to_llm_when_deterministic_raises(tmp_path):
    indicator = {
        "id": "x", "name": "X", "domain": "money_market", "cadence": "daily",
        "fetch": {"task": "Nonexistent"},
        "parse": {"deterministic": "html_footer_ticker", "value_type": "percent",
                  "valid_range": [0.0, 100.0], "llm_prompt": "html_footer_ticker.txt"},
    }
    fake_extract = type("R", (), {"parsed": {"value": 7.0}, "raw_text": ""})()
    with patch("parsers.hybrid._llm_extract", return_value=fake_extract):
        snapshot = parse_one(_ticker_artifact(tmp_path), indicator, history=[])
    assert snapshot["value"] == 7.0
    assert snapshot["_provenance"] == "llm_extracted"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_hybrid.py -v
```

Expected: FAIL.

- [ ] **Step 3: Write the implementation**

```python
# parsers/hybrid.py
"""Hybrid orchestrator: deterministic-first with Sonnet 4.6 sanity-check + fallback.

Flow:
  1. Run registry parser → V_det
     ├─ Validate value_type + range
     │   ├─ ok → sanity-check via Sonnet
     │   │   ├─ plausible → emit V_det as authoritative
     │   │   └─ implausible → LLM extract → V_llm; if matches V_det, emit V_det;
     │   │                    else flag needs_review and emit both
     │   └─ fail → LLM extract path
     └─ raises → LLM extract path
  2. LLM extract → V_llm; validate; emit with provenance="llm_extracted"
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_max.max_client import MaxCallError, run_max
from claude_max.validators import InvalidValueError, validate_value, values_match
from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import get_parser

logger = logging.getLogger("hybrid")
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "claude_max" / "prompts"


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text()


def _sanity_check(*, indicator: dict, value: float, history: list[float]) -> Any:
    template = _load_prompt("sanity_check.txt")
    prompt = template.format(
        indicator_name=indicator["name"],
        domain=indicator["domain"],
        cadence=indicator["cadence"],
        value=value,
        value_type=indicator["parse"]["value_type"],
        valid_range=indicator["parse"]["valid_range"],
        history=history or "(none)",
    )
    return run_max(prompt=prompt)


def _llm_extract(*, indicator: dict, artifact: FetchResult) -> Any:
    template = _load_prompt(indicator["parse"]["llm_prompt"])
    if artifact.artifact_type == "pdf":
        import pdfplumber
        with pdfplumber.open(artifact.artifact_path) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    else:
        text = artifact.artifact_path.read_text()
    prompt = template.format(
        indicator_name=indicator["name"],
        instruction=indicator["fetch"].get("task", ""),
        value_type=indicator["parse"]["value_type"],
        valid_range=indicator["parse"]["valid_range"],
        pdf_text=text[:6000],  # truncate to ~6k chars to keep within model context
    )
    return run_max(prompt=prompt)


def _build_snapshot(
    *, indicator: dict, artifact: FetchResult, value: float | int | str,
    provenance: str, parse_strategy: str, sanity_note: str | None = None,
    previous_value: float | None = None, change_pct: float | None = None,
) -> dict:
    return {
        "indicator_id": indicator["id"],
        "name": indicator["name"],
        "domain": indicator["domain"],
        "cadence": indicator["cadence"],
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "source_url": artifact.source_url,
        "value": value,
        "value_type": indicator["parse"]["value_type"],
        "previous_value": previous_value,
        "change_pct": change_pct,
        "_provenance": provenance,
        "_artifact_sha256": artifact.sha256,
        "_parse_strategy": parse_strategy,
        "sanity_note": sanity_note,
    }


def parse_one(artifact: FetchResult, indicator: dict, history: list[float]) -> dict:
    parse_block = indicator["parse"]
    instruction = indicator["fetch"].get("task", "")
    value_type = parse_block["value_type"]
    valid_range = tuple(parse_block["valid_range"])

    # 1. Try deterministic
    parser = get_parser(parse_block["deterministic"])
    v_det: float | int | str | None = None
    try:
        det_result: ParseResult = parser.parse(artifact, instruction)
        validate_value(value=det_result.value, value_type=value_type, valid_range=valid_range)
        v_det = det_result.value
    except (ParseError, InvalidValueError) as e:
        logger.info("deterministic parse failed for %s: %s", indicator["id"], e)

    # 2. Sanity-check via Sonnet (if det succeeded)
    if v_det is not None:
        try:
            sanity = _sanity_check(indicator=indicator, value=float(v_det), history=history)
            plausible = bool((sanity.parsed or {}).get("plausible", True))
            note = (sanity.parsed or {}).get("reason")
        except MaxCallError as e:
            logger.warning("sanity-check failed for %s: %s — emitting deterministic anyway", indicator["id"], e)
            return _build_snapshot(indicator=indicator, artifact=artifact, value=v_det,
                                   provenance="deterministic", parse_strategy=parse_block["deterministic"])

        if plausible:
            return _build_snapshot(indicator=indicator, artifact=artifact, value=v_det,
                                   provenance="deterministic", parse_strategy=parse_block["deterministic"],
                                   sanity_note=note)
        # Disagreement: cross-check with extract
        try:
            extract = _llm_extract(indicator=indicator, artifact=artifact)
            v_llm = (extract.parsed or {}).get("value")
            if v_llm is not None and values_match(float(v_det), float(v_llm), value_type=value_type):
                return _build_snapshot(indicator=indicator, artifact=artifact, value=v_det,
                                       provenance="deterministic", parse_strategy=parse_block["deterministic"],
                                       sanity_note=f"sanity flagged but extract agreed; {note}")
            return _build_snapshot(indicator=indicator, artifact=artifact, value=v_det,
                                   provenance="needs_review", parse_strategy=parse_block["deterministic"],
                                   sanity_note=f"det={v_det} llm={v_llm} note={note}")
        except MaxCallError as e:
            logger.warning("llm_extract failed for %s: %s", indicator["id"], e)
            return _build_snapshot(indicator=indicator, artifact=artifact, value=v_det,
                                   provenance="needs_review", parse_strategy=parse_block["deterministic"],
                                   sanity_note=f"sanity flagged, extract errored: {e}")

    # 3. LLM extract path (deterministic failed)
    try:
        extract = _llm_extract(indicator=indicator, artifact=artifact)
        v_llm = (extract.parsed or {}).get("value")
        if v_llm is None:
            raise MaxCallError(f"llm extract returned no value: {extract.raw_text[:200]}")
        validate_value(value=float(v_llm), value_type=value_type, valid_range=valid_range)
        return _build_snapshot(indicator=indicator, artifact=artifact, value=float(v_llm),
                               provenance="llm_extracted", parse_strategy=parse_block["deterministic"])
    except (MaxCallError, InvalidValueError) as e:
        logger.error("extract_failed for %s: %s", indicator["id"], e)
        return _build_snapshot(indicator=indicator, artifact=artifact, value=0.0,
                               provenance="needs_review", parse_strategy="extract_failed",
                               sanity_note=str(e))
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_hybrid.py -v
```

Expected: 2 tests pass.

- [ ] **Step 5: Commit**

```bash
git add parsers/hybrid.py tests/test_hybrid.py
git commit -m "feat(parsers): add hybrid orchestrator (deterministic + Sonnet sanity-check + fallback)"
```

---

### Task 19: `parse_all.py` Stage 2 entry point

**Files:**
- Create: `parse_all.py`
- Test: `tests/test_parse_all.py`

- [ ] **Step 1: Write the failing test** (mock `parse_one`, verify it walks the registry, writes per-indicator snapshots, emits summary).

```python
# tests/test_parse_all.py
import json
from pathlib import Path
from unittest.mock import patch

import parse_all


def test_parse_all_writes_per_indicator_snapshots(tmp_path: Path):
    cfg = tmp_path / "sources-v3.json"
    cfg.write_text(json.dumps({
        "version": "3.0",
        "indicators": [
            {"id": "x", "name": "X", "domain": "money_market", "cadence": "daily",
             "fetch": {"type": "html", "url": "https://example.com", "task": "x"},
             "parse": {"deterministic": "html_footer_ticker", "value_type": "percent",
                       "valid_range": [0, 100], "llm_prompt": "html_footer_ticker.txt"}},
        ],
    }))
    fake_artifact = type("FR", (), {
        "indicator_id": "x", "artifact_path": Path(""), "artifact_type": "html",
        "fetched_at": None, "source_url": "x", "sha256": "y"*64, "cache_hit": False,
    })()
    fake_snapshot = {"indicator_id": "x", "value": 10.0, "_provenance": "deterministic"}
    with patch("parse_all._load_artifact_for", return_value=fake_artifact), \
         patch("parse_all.parse_one", return_value=fake_snapshot):
        results = parse_all.run(config_path=cfg, data_root=tmp_path / "data")
    out = (tmp_path / "data" / "x").glob("*.json")
    assert any(out)
    assert results[0]["value"] == 10.0
```

- [ ] **Step 3: Write the implementation**

```python
# parse_all.py
"""Stage 2 entry point: walk sources-v3.json, parse each fetched artifact,
emit per-indicator snapshots."""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fetchers.base import FetchResult
from parsers.hybrid import parse_one

# Auto-import all parser modules so they register
import parsers.html_footer_ticker  # noqa: F401
import parsers.html_table_row  # noqa: F401
import parsers.html_call_money  # noqa: F401
import parsers.pdf_component  # noqa: F401
import parsers.pdf_table_row  # noqa: F401
import parsers.pdf_table_total  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "sources-v3.json"
DEFAULT_DATA_ROOT = REPO_ROOT / "data"

logger = logging.getLogger("parse_all")


def _load_artifact_for(indicator: dict, data_root: Path) -> FetchResult | None:
    """Find the most recent fetched artifact for an indicator.

    HTML: data/_html/<id>/<latest>.html
    PDF:  data/_pdfs/<id>/<latest_yyyy_mm>/<latest>.pdf (one PDF per month dir)
    """
    indicator_id = indicator["id"]
    if indicator["fetch"]["type"] == "html":
        d = data_root / "_html" / indicator_id
        if not d.exists():
            return None
        candidates = sorted(d.glob("*.html"), reverse=True)
        if not candidates:
            return None
        artifact_path = candidates[0]
    else:
        d = data_root / "_pdfs" / indicator_id
        if not d.exists():
            return None
        month_dirs = sorted([p for p in d.iterdir() if p.is_dir()], reverse=True)
        if not month_dirs:
            return None
        pdfs = list(month_dirs[0].glob("*.pdf"))
        if not pdfs:
            return None
        artifact_path = pdfs[0]
    return FetchResult(
        indicator_id=indicator_id,
        artifact_path=artifact_path,
        artifact_type=indicator["fetch"]["type"],
        fetched_at=datetime.fromtimestamp(artifact_path.stat().st_mtime, tz=timezone.utc),
        source_url=indicator["fetch"]["url"],
        sha256="0" * 64,  # TODO: read from sidecar if present
        cache_hit=False,
    )


def _emit_snapshot(snapshot: dict, data_root: Path) -> Path:
    out_dir = data_root / snapshot["indicator_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = out_dir / f"{today}.json"
    path.write_text(json.dumps(snapshot, indent=2, default=str))
    return path


def _load_history(indicator_id: str, data_root: Path, n: int = 3) -> list[float]:
    d = data_root / indicator_id
    if not d.exists():
        return []
    paths = sorted(d.glob("*.json"), reverse=True)[1 : n + 1]  # skip today
    out: list[float] = []
    for p in paths:
        try:
            v = json.loads(p.read_text()).get("value")
            if isinstance(v, (int, float)):
                out.append(float(v))
        except json.JSONDecodeError:
            continue
    return out


def run(*, config_path: Path, data_root: Path, only: str | None = None) -> list[dict]:
    cfg = json.loads(config_path.read_text())
    snapshots: list[dict] = []
    for ind in cfg["indicators"]:
        if only and ind["id"] != only:
            continue
        artifact = _load_artifact_for(ind, data_root)
        if artifact is None:
            logger.warning("no artifact for %s — skipping", ind["id"])
            continue
        history = _load_history(ind["id"], data_root)
        try:
            snapshot = parse_one(artifact, ind, history=history)
        except Exception as e:
            logger.error("parse_one raised for %s: %s", ind["id"], e)
            continue
        _emit_snapshot(snapshot, data_root)
        snapshots.append(snapshot)
        logger.info("parsed %s value=%s provenance=%s", ind["id"], snapshot.get("value"), snapshot.get("_provenance"))
    return snapshots


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    p.add_argument("--only", type=str, default=None)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    snapshots = run(config_path=args.config, data_root=args.data_root, only=args.only)
    by_prov: dict[str, int] = {}
    for s in snapshots:
        by_prov[s.get("_provenance", "unknown")] = by_prov.get(s.get("_provenance", "unknown"), 0) + 1
    print(f"Parsed: {len(snapshots)} ({', '.join(f'{k}:{v}' for k, v in by_prov.items())})")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
```

- [ ] **Step 4-5: Run, commit**

```bash
.venv/bin/pytest tests/test_parse_all.py -v
git add parse_all.py tests/test_parse_all.py
git commit -m "feat: add parse_all.py Stage 2 entry point"
```

---

## Phase 6 — Aggregator changes

### Task 20: Refactor `aggregate_latest.py` to be registry-driven + dual-shape

**Files:**
- Modify: `aggregate_latest.py`
- Modify: `tests/test_aggregator.py`

- [ ] **Step 1: Write the failing test for the new dual-shape output**

Append to `tests/test_aggregator.py`:

```python
def test_dual_shape_includes_legacy_and_domains_keys(tmp_path: Path):
    """v3 latest.json must include both legacy keys (bb_forex, dse_market,
    commodity_prices) AND domain-grouped keys."""
    # Setup: write a sources-v3.json with one money_market indicator.
    # Setup: write a snapshot for that indicator + the existing 3 MVP scrapers.
    # Run aggregate_latest.run(...).
    # Assert latest.json["domains"]["money_market"]["policy_rate_slf_sdf"] exists.
    # Assert latest.json["bb_forex"] still exists with old shape.
    # ... (full fixture assembly omitted here for plan brevity; copy patterns
    # from existing test_aggregator.py)
    pass
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_aggregator.py -v
```

Expected: the new test fails (no domains key in output).

- [ ] **Step 3: Modify `aggregate_latest.py`**

The diff is large. Key changes:
1. Add `SOURCES_V3_PATH = REPO_ROOT / "config" / "sources-v3.json"`.
2. Add `_load_v3_indicators()` that reads the registry.
3. Add `_load_v3_snapshot(indicator_id)` that reads the latest JSON in `data/<indicator_id>/`.
4. Add `_compute_freshness(snapshots)` that builds the freshness summary block per cadence:

```python
STALE_BY_CADENCE = {
    "daily": 24.0,        # hours
    "weekly": 8 * 24.0,
    "monthly": 35 * 24.0,
    "quarterly": 100 * 24.0,
    "fy": 400 * 24.0,
}
```

5. Modify `build_bundle()` to:
   - Keep emitting the 3 legacy top-level keys (`bb_forex`, `dse_market`, `commodity_prices`) — DO NOT remove.
   - Add a top-level `domains` dict grouping all v3 indicators by domain.
   - Add a top-level `freshness` summary.
   - Add a top-level `alerts` list (anomaly hits).
   - Bump `schema_version` to `"3.0"`.

6. Update `LatestBundle` Pydantic schema in `utils/schema.py` to allow the new fields (or add a new model `LatestBundleV3`).

The test from Step 1 anchors the contract.

- [ ] **Step 4: Run all aggregator tests**

```bash
.venv/bin/pytest tests/test_aggregator.py -v
```

Expected: all tests pass (existing + new dual-shape).

- [ ] **Step 5: Commit**

```bash
git add aggregate_latest.py tests/test_aggregator.py utils/schema.py
git commit -m "feat(aggregator): registry-driven, dual-shape latest.json with freshness + alerts"
```

---

### Task 21: The Brief read-path regression test

**Files:**
- Modify: `tests/test_aggregator.py`

- [ ] **Step 1: Add a test that reads the new latest.json via the SAME paths The Brief uses**

```python
def test_the_brief_read_paths_still_work(tmp_path: Path):
    """The Brief reads latest.json["bb_forex"]["usd_bdt_buy"] etc. v3
    aggregator must not break those exact key paths."""
    # ... setup MVP snapshots, run aggregator ...
    bundle = json.loads((tmp_path / "data" / "latest.json").read_text())
    # The Brief's exact read paths (cite source — search for `latest.json[` in the-brief)
    assert "bb_forex" in bundle
    assert "usd_bdt_buy" in bundle["bb_forex"]
    assert "dsex" in bundle["dse_market"]
    assert "brent_crude" in bundle["commodity_prices"]
```

- [ ] **Step 2-5: Run, fix anything broken, commit**

```bash
.venv/bin/pytest tests/test_aggregator.py::test_the_brief_read_paths_still_work -v
git add tests/test_aggregator.py
git commit -m "test(aggregator): regression-test The Brief's exact read paths"
```

---

## Phase 7 — Deployment units

### Task 22: New systemd units `econdelta-fetch` and `econdelta-parse`

**Files:**
- Create: `deploy/econdelta-fetch.service`
- Create: `deploy/econdelta-fetch.timer`
- Create: `deploy/econdelta-parse.service`
- Create: `deploy/econdelta-parse.timer`

- [ ] **Step 1: Write the unit files**

```ini
# deploy/econdelta-fetch.service
[Unit]
Description=EconDelta Stage 1 — fetch all (HTML + PDF)
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=adnan
Group=adnan
WorkingDirectory=/home/adnan/econdelta
EnvironmentFile=/etc/econdelta.env
Environment="CLAUDE_BINARY=/home/adnan/.npm-global/bin/claude"
ExecStart=/home/adnan/econdelta/.venv/bin/python /home/adnan/econdelta/fetch_all.py
StandardOutput=append:/home/adnan/econdelta/logs/fetch-systemd.log
StandardError=append:/home/adnan/econdelta/logs/fetch-systemd.log
TimeoutStartSec=900
```

```ini
# deploy/econdelta-fetch.timer
[Unit]
Description=Daily EconDelta Stage 1 fetch

[Timer]
OnCalendar=*-*-* 00:00:00 UTC
Persistent=true
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
```

```ini
# deploy/econdelta-parse.service
[Unit]
Description=EconDelta Stage 2 — parse all
Wants=econdelta-fetch.service
After=econdelta-fetch.service

[Service]
Type=oneshot
User=adnan
Group=adnan
WorkingDirectory=/home/adnan/econdelta
EnvironmentFile=/etc/econdelta.env
Environment="CLAUDE_BINARY=/home/adnan/.npm-global/bin/claude"
ExecStart=/home/adnan/econdelta/.venv/bin/python /home/adnan/econdelta/parse_all.py
StandardOutput=append:/home/adnan/econdelta/logs/parse-systemd.log
StandardError=append:/home/adnan/econdelta/logs/parse-systemd.log
TimeoutStartSec=1800
```

```ini
# deploy/econdelta-parse.timer
[Unit]
Description=Daily EconDelta Stage 2 parse

[Timer]
OnCalendar=*-*-* 00:15:00 UTC
Persistent=true
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
```

Note: paths use `/home/adnan` here. `deploy/install.sh` already has the existing pattern that sed-substitutes for `/home/adnan.local/` on ExonVPS. Verify on Step 4.

- [ ] **Step 2: Verify the existing install.sh handles the new units**

```bash
cat ~/Projects/clauding-lab/econdelta/deploy/install.sh
```

If it explicitly lists units (rather than globbing), add the two new ones to its list.

- [ ] **Step 3: Validate the units pass `systemd-analyze verify` locally** *(only if you have systemd; skip on Mac)*

```bash
# Skipped on macOS; verify on ExonVPS during Task 25.
```

- [ ] **Step 4: Commit**

```bash
git add deploy/econdelta-fetch.service deploy/econdelta-fetch.timer deploy/econdelta-parse.service deploy/econdelta-parse.timer
[ -f deploy/install.sh ] && git add deploy/install.sh
git commit -m "feat(deploy): add econdelta-fetch + econdelta-parse systemd units"
```

---

### Task 23: Push branch + open PR

**Files:**
- (no files modified)

- [ ] **Step 1: Push to origin**

```bash
cd ~/Projects/clauding-lab/econdelta
git push -u origin main
```

(If you want a feature branch, create one before push: `git checkout -b feat/v3-expansion && git push -u origin feat/v3-expansion`. Recommended given the scope.)

- [ ] **Step 2: Open a PR (if branch-based)**

```bash
gh pr create --title "feat: EconDelta v2 — 45-indicator expansion with hybrid parser" --body "$(cat <<'EOF'
## Summary
- 36 new Bangladesh economic indicators on top of the 9 MVP indicators
- Three-stage pipeline: fetch_all.py → parse_all.py → aggregate_latest.py
- Hybrid parser: deterministic-first with Sonnet 4.6 sanity-check + LLM fallback (mirrors the-brief/brief/claude/max_client.py)
- Dual-shape latest.json: legacy keys preserved for The Brief; new domains/freshness/alerts blocks added

## Test plan
- [ ] All existing tests pass (122)
- [ ] New tests pass: build_sources_v3, max_client, validators, fetchers, parsers, hybrid, parse_all, aggregator dual-shape
- [ ] First-extraction validation completes on ExonVPS (Tasks 24-30)
- [ ] The Brief read paths regression passes
EOF
)"
```

- [ ] **Step 3: Commit (no changes — placeholder)**

(no commit needed)

---

## Phase 8 — First-extraction on ExonVPS

### Task 24: Verify Sonnet auth on ExonVPS — **GATE**

**Files:** none

- [ ] **Step 1: SSH and confirm `claude` binary**

```bash
ssh adnan.local@103.187.23.22 'which claude || echo MISSING'
```

Expected: a path (likely `/usr/local/bin/claude` or `~/.npm-global/bin/claude`). If MISSING → install with `npm install -g @anthropic-ai/claude-code` first.

- [ ] **Step 2: Confirm credentials file exists**

```bash
ssh adnan.local@103.187.23.22 'ls -la ~/.claude/.credentials.json 2>/dev/null || echo MISSING'
```

Expected: a file owned by `adnan.local`, mode 600. If MISSING → **STOP**. This is the Claude Max device-count question. Tell the user.

- [ ] **Step 3: Test a smoke `claude -p`**

```bash
ssh adnan.local@103.187.23.22 'claude -p "Reply with strict JSON: {\"ok\": true}" --model claude-sonnet-4-6 --output-format json --no-session-persistence --tools "" --permission-mode bypassPermissions --effort medium'
```

Expected: stdout is valid JSON with `result` field containing `{"ok": true}`. If this fails with rate-limit / device-limit / not-authenticated → **STOP** and surface the error to the user. Do not proceed with first-extraction.

- [ ] **Step 4: Update memory with VPS-side claude binary path**

Edit `~/.claude/projects/-Users-adnanrashid/memory/reference_econdelta_vps.md` to add:
```
- ExonVPS claude binary: <path from step 1>
- ExonVPS Claude Max auth: confirmed working YYYY-MM-DD
```

- [ ] **Step 5: Commit (none — environment verification)**

(no commit; no file changed in repo)

---

### Task 25: Deploy + dry-run Stage 1 on ExonVPS

**Files:** none on Mac; deploy on VPS

- [ ] **Step 1: Pull latest code on ExonVPS**

```bash
ssh adnan.local@103.187.23.22 'cd ~/econdelta && git pull origin main'
```

- [ ] **Step 2: Install new deps**

```bash
ssh adnan.local@103.187.23.22 'cd ~/econdelta && .venv/bin/pip install -e . -r requirements-dev.txt'
```

Expected: pdfplumber installs.

- [ ] **Step 3: Install new systemd units**

```bash
ssh root@103.187.23.22 'cd /home/adnan.local/econdelta/deploy && bash install.sh'
```

Expected: 2 new timers (`econdelta-fetch`, `econdelta-parse`) appear in `systemctl list-timers`.

- [ ] **Step 4: Stage 1 dry-run**

```bash
ssh adnan.local@103.187.23.22 'cd ~/econdelta && .venv/bin/python fetch_all.py --dry-run'
```

Expected: prints "would fetch <id> (<type>)" for each indicator. No errors. ~45 lines.

- [ ] **Step 5: Stage 1 real run**

```bash
ssh adnan.local@103.187.23.22 'cd ~/econdelta && .venv/bin/python fetch_all.py 2>&1 | tee logs/fetch-first-run.log'
```

Expected: each indicator logged. Final summary like `Fetched: 38 · Cache hits: 0 · Failed: see log`.

- [ ] **Step 6: Verify artifact tree**

```bash
ssh adnan.local@103.187.23.22 'cd ~/econdelta && find data/_pdfs data/_html -type f | wc -l && du -sh data/_pdfs data/_html'
```

Expected: ~30+ PDFs, ~7+ HTML files. Total ~50-200MB depending on PDF sizes.

- [ ] **Step 7: Commit (no changes — environment-only)**

(no commit)

---

### Task 26: Stage 2 dry-run, real run, hand-validate

**Files:** none on Mac; verification on VPS

- [ ] **Step 1: Stage 2 dry-run**

```bash
ssh adnan.local@103.187.23.22 'cd ~/econdelta && .venv/bin/python parse_all.py --only=policy_rate_slf_sdf 2>&1 | head -30'
```

Expected: parses one indicator. Snapshot lands in `data/policy_rate_slf_sdf/<today>.json`. Walk through it:

```bash
ssh adnan.local@103.187.23.22 'cat ~/econdelta/data/policy_rate_slf_sdf/$(date -u +%Y-%m-%d).json'
```

Expected JSON has: `value`, `_provenance` ("deterministic" hopefully), `_artifact_sha256`, `sanity_note`.

- [ ] **Step 2: Full Stage 2 run**

```bash
ssh adnan.local@103.187.23.22 'cd ~/econdelta && time .venv/bin/python parse_all.py 2>&1 | tee logs/parse-first-run.log'
```

Expected: ~2-5 minutes wall-clock (mostly Sonnet calls). Final summary like `Parsed: 38 (deterministic:25, llm_extracted:8, needs_review:3, extract_failed:2)`.

- [ ] **Step 3: Hand-validate 5 random indicators**

```bash
ssh adnan.local@103.187.23.22 'ls -t ~/econdelta/data/*/$(date -u +%Y-%m-%d).json | shuf | head -5 | xargs -I {} cat {}'
```

For each, eyeball:
- Does `value` look right for the indicator (cross-check with the source URL)?
- Is `_provenance` deterministic where you'd expect, llm_extracted where deterministic struggles, needs_review where it disagreed?
- Are `valid_range` violations absent?

- [ ] **Step 4: Triage `needs_review` and `extract_failed` indicators**

```bash
ssh adnan.local@103.187.23.22 'grep -l "needs_review\|extract_failed" ~/econdelta/data/*/$(date -u +%Y-%m-%d).json'
```

For each: investigate, fix `valid_range` or `anomaly_threshold` in `config/sources-v3.json`, commit + push, re-run Stage 2 (Stage 1 cache held).

- [ ] **Step 5: Commit any sources-v3.json tunings**

```bash
cd ~/Projects/clauding-lab/econdelta
git add config/sources-v3.json
git commit -m "tune(sources-v3): tighten valid_range/threshold for first-extraction findings"
git push
ssh adnan.local@103.187.23.22 'cd ~/econdelta && git pull && .venv/bin/python parse_all.py 2>&1 | tail'
```

---

### Task 27: Stage 3 — aggregate and verify

**Files:** none

- [ ] **Step 1: Run aggregator**

```bash
ssh adnan.local@103.187.23.22 'cd ~/econdelta && .venv/bin/python aggregate_latest.py'
```

Expected: writes `data/latest.json`. No errors.

- [ ] **Step 2: Inspect dual-shape**

```bash
ssh adnan.local@103.187.23.22 'cat ~/econdelta/data/latest.json | jq "keys"'
```

Expected output (subset): `["alerts", "bb_forex", "commodity_prices", "domains", "dse_market", "freshness", "generated_at", "schema_version", "sources_status"]`

```bash
ssh adnan.local@103.187.23.22 'cat ~/econdelta/data/latest.json | jq ".domains | keys"'
```

Expected: 9 domains.

```bash
ssh adnan.local@103.187.23.22 'cat ~/econdelta/data/latest.json | jq ".freshness"'
```

Expected: `indicators_total: 45`, `indicators_fresh: <some N>`, by_cadence breakdown.

- [ ] **Step 3: Verify The Brief read path**

```bash
ssh adnan.local@103.187.23.22 'cat ~/econdelta/data/latest.json | jq ".bb_forex.usd_bdt_buy"'
```

Expected: a number. If null/missing → aggregator regressed; investigate before going further.

- [ ] **Step 4: Trigger HetznerVPS pull and verify Brief still works**

```bash
ssh adnan@135.181.43.68 '~/bin/pull-econdelta-latest.sh && cat ~/econdelta/data/latest.json | jq ".schema_version, .bb_forex.usd_bdt_buy, .domains.money_market | keys"'
```

Expected: schema_version "3.0", USD/BDT a number, money_market keys list.

- [ ] **Step 5: Commit (no changes; verification only)**

(no commit)

---

### Task 28: Enable systemd timers

**Files:** none

- [ ] **Step 1: Enable + start the new timers**

```bash
ssh root@103.187.23.22 'systemctl enable --now econdelta-fetch.timer econdelta-parse.timer'
```

- [ ] **Step 2: Verify they're queued**

```bash
ssh root@103.187.23.22 'systemctl list-timers econdelta-* --all'
```

Expected: 6 timers — the existing 4 (forex, dse, commodity, aggregate) plus the 2 new (fetch, parse). Check the next-fire times look right.

- [ ] **Step 3: Memory update**

```bash
# Edit ~/.claude/projects/-Users-adnanrashid/memory/project_econdelta.md
# Add: "v3 expansion landed YYYY-MM-DD: 45 indicators, 6 timers, hybrid parser via Sonnet 4.6 over Claude Max."
```

- [ ] **Step 4: Final commit** *(optional, if any sources-v3 tunings happened)*

Already done in Task 26.

---

### Task 29: Soak watch (first 24h)

**Files:** none

- [ ] **Step 1: Watch the next scheduled fetch**

The next `econdelta-fetch.timer` fire is the morning after deploy at 06:00 BDT (= 00:00 UTC). Set a reminder. Check:

```bash
ssh adnan.local@103.187.23.22 'systemctl status econdelta-fetch.service && tail -50 ~/econdelta/logs/fetch-systemd.log'
```

- [ ] **Step 2: Watch the next scheduled parse**

```bash
ssh adnan.local@103.187.23.22 'systemctl status econdelta-parse.service && tail -50 ~/econdelta/logs/parse-systemd.log'
```

- [ ] **Step 3: Verify aggregate ran**

```bash
ssh adnan.local@103.187.23.22 'cat ~/econdelta/data/latest.json | jq ".generated_at, .freshness.indicators_fresh"'
```

- [ ] **Step 4: Verify HetznerVPS mirror updated**

```bash
ssh adnan@135.181.43.68 'cat ~/econdelta/data/latest.json | jq ".generated_at"'
```

Should be within ~10 min of the ExonVPS `generated_at`.

- [ ] **Step 5: Watch Discord `#econdelta-alerts` channel**

Expected: silence (no alerts) ⇒ success. If alerts fire, triage:
- Anomaly with real movement → tune threshold
- `extract_failed` → fix parser or tune valid_range
- `needs_review` → eyeball, decide

- [ ] **Step 6: Final memory + session save**

```bash
# Edit memory:
# - project_econdelta.md → mark v3 live
# - reference_econdelta_vps.md → add new commands
# - feedback_subagent_model.md → add Sonnet-4.6-via-Max for parser sanity-check
```

Commit any docs/memory updates.

---

## Self-review

**Spec coverage:**
- §3 architecture → Tasks 6-9, 18-19 (3 stages implemented)
- §4 source registry → Task 1 (build_sources_v3.py)
- §5 fetch infra → Tasks 5-9
- §6 parse infra + LLM hybrid → Tasks 3, 4, 10-19
- §7 schema/aggregator dual-shape → Tasks 20-21
- §8 scheduling → Task 22 systemd units, Task 28 enable
- §9 first-extraction → Tasks 24-29
- §10 failure modes → Tasks 24 (gate) and 26 (triage)
- §11 risks → Task 24 (Claude Max device count gate)
- §12 testing → every task has TDD steps
- §13 out-of-scope → respected (no Brief migration, no news indicators automated, no global threshold retune, legacy keys retained)

**Placeholder scan:** Task 20 ("diff is large; key changes:") describes the modification rather than showing every line. The aggregator is a 460-line file and an inline rewrite would balloon the plan. The change is well-bounded by the test in Step 1; the implementer is expected to read the existing aggregator and make the changes named. Acceptable for a plan; not a placeholder for behavior.

**Type consistency:**
- `FetchResult` fields used in fetchers and parsers — match.
- `ParseResult` `_provenance` literal values — match between `parsers/base.py` and `parse_one`.
- `value_type` literals — match between `claude_max/validators.py` and `parsers/base.py` use sites.
- `register("name")` decorator name vs `parse.deterministic` field name in sources-v3.json — match.

**Scope check:** This is one coherent spec/plan. ~30 tasks but tightly coupled (they all ship together for any meaningful delivery). Not split.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-30-econdelta-v2-expansion-plan.md`.

Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
