"""End-to-end deterministic-parse tests for config-conversion batch 2.

Issue #113 / fix-all plan Steps 11-13, batch 2 of 3: `bop_summary`'s
deterministic path was dead the same way `current_account_balance`'s was
before PR #111 — `html_table_row`'s `task="Entire Table"` instruction
contains no `row=`/`col=` tokens, so `_parse_instruction` raises `ParseError`
on every real run and `hybrid.parse_one` always fell through to the LLM.

PR #111 built `parsers/bb_bop_row.py` for exactly this table (header-text
column selection, same-`<table>`-scoped header/data/unit resolution — see
that module's docstring) but only wired it to `current_account_balance`.
This file converts `bop_summary` to the SAME parser, `row=Overall Balance`
(a different row of the identical table) — the fix issue #113 called
"nearly free" once #111 landed.

The fixture (`tests/fixtures/bb_bop_2026-08.html`) is the SAME real,
verbatim-captured BB BoP page `test_current_account.py` already uses (see
that file's docstring for capture provenance). Overall Balance's three
period columns are `-1151 | 3741 | 4019` (in USD million); the current
fiscal-year PROVISIONAL column (the same column current_account_balance
selects, per PR #111's header-scoring) is the LAST one, `4019` (`4.019` bn).

**Arithmetic cross-check** (not just "the parser returns some number in
range" — the number is independently provable from the fixture's own other
rows): BB's own BoP identity for the current-period column is

    Current Account Balance (-301) + Capital account (366)
    + Financial account (4161) + Errors and omissions (-207) = 4019

which reconciles EXACTLY against Overall Balance's own printed 4019 for that
column (`test_overall_balance_reconciles_against_bop_identity` pins this).

**Parity vs production**: `metric_history.bop_summary` (queried via the
anon key, 2026-08-05) has held `4.019` for 8 consecutive days
(2026-07-29..2026-08-05) via the LLM path currently in place — an exact
match to this deterministic value. No published figure changes; this
conversion is parity-clean, not a HOLD MERGE case (contrast with
`non_bank_borrowing_for_deficit_financing` in batch 1, which WAS a real
figure change).
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import parsers.bb_bop_row  # noqa: F401 -- registers
from claude_max.validators import validate_value
from fetchers.base import FetchResult
from parsers.hybrid import parse_one
from parsers.registry import get_parser

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "bb_bop_2026-08.html"
CONFIG_PATH = REPO_ROOT / "config" / "sources-v3.json"

# Verified via `shasum -a 256 tests/fixtures/bb_bop_2026-08.html`.
FIXTURE_SHA256 = "e2ddc03669e5161ba811eeb31f5f8dd01b953c871c2ab7709dd7ee39821ca898"

# The real published number this metric resolves to (Overall Balance,
# current FY provisional column, per the fixture's own table — see docstring).
_EXPECTED_VALUE_BN = 4.019
_EXPECTED_SOURCE_AS_OF = date(2026, 5, 31)
# landmine 19a's guard, mirrored: bop_summary must never resolve to any of
# Current Account Balance's three columns.
_CURRENT_ACCOUNT_VALUES_BN = {-0.778, -1.229, -0.301}


def _load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def _indicator(indicator_id: str) -> dict:
    cfg = _load_config()
    for ind in cfg["indicators"]:
        if ind["id"] == indicator_id:
            return ind
    raise KeyError(indicator_id)


def _artifact() -> FetchResult:
    assert FIXTURE.exists(), f"missing real fixture {FIXTURE}"
    return FetchResult(
        indicator_id="bop_summary",
        artifact_path=FIXTURE,
        artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.bb.org.bd/en/index.php/econdata/bop",
        sha256=FIXTURE_SHA256,
        cache_hit=False,
    )


def _plausible_sanity():
    return type("R", (), {"parsed": {"plausible": True, "reason": "ok"}, "raw_text": ""})()


@pytest.fixture(scope="module")
def indicator() -> dict:
    return _indicator("bop_summary")


def test_fixture_sha256_matches_captured_sidecar():
    """Guards against silently swapping the fixture for a hand-edited file."""
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256


# ---------------------------------------------------------------------------
# Core conversion: real config task -> real registered parser -> real fixture.
# ---------------------------------------------------------------------------


def test_bop_summary_resolves_overall_balance_current_provisional_column(indicator):
    parser = get_parser(indicator["parse"]["deterministic"])
    result = parser.parse(_artifact(), indicator["fetch"]["task"])
    assert result.value == pytest.approx(_EXPECTED_VALUE_BN)
    assert result.source_as_of == _EXPECTED_SOURCE_AS_OF
    assert result._parse_strategy == "bb_bop_row"


def test_bop_summary_does_not_select_current_account_balance_row(indicator):
    """Landmine 19a guard, mirrored: Overall Balance and Current Account
    Balance are different rows of the same table. None of Current Account
    Balance's three period values should ever come out of this indicator."""
    parser = get_parser(indicator["parse"]["deterministic"])
    result = parser.parse(_artifact(), indicator["fetch"]["task"])
    assert result.value not in _CURRENT_ACCOUNT_VALUES_BN


def test_bop_summary_value_passes_its_own_valid_range(indicator):
    parser = get_parser(indicator["parse"]["deterministic"])
    result = parser.parse(_artifact(), indicator["fetch"]["task"])
    validate_value(
        value=result.value,
        value_type=indicator["parse"]["value_type"],
        valid_range=tuple(indicator["parse"]["valid_range"]),
    )  # raises InvalidValueError on failure -- the assertion IS "does not raise"


def test_overall_balance_reconciles_against_bop_identity():
    """Independent arithmetic cross-check against the fixture's OWN other
    rows (not just internal test-file consistency): Overall Balance must
    equal Current Account + Capital Account + Financial Account + Errors and
    Omissions for the SAME (current-period) column. All four numbers are
    read from the real page via the same parser, not hand-copied."""
    parser = get_parser("bb_bop_row")
    cab = parser.parse(_artifact(), "row=Current Account Balance").value
    capital = parser.parse(_artifact(), "row=Capital account").value
    financial = parser.parse(_artifact(), "row=Financial account").value
    errors_omissions = parser.parse(_artifact(), "row=Errors and omissions").value
    overall = parser.parse(_artifact(), "row=Overall Balance").value
    assert overall == pytest.approx(cab + capital + financial + errors_omissions, abs=0.001)
    # Pin the literal expected value too, so a uniform column-shift bug that
    # still satisfies the identity (unlikely, but batch-1's M1 finding
    # showed self-consistency alone isn't proof) can't sneak past silently.
    assert overall == pytest.approx(_EXPECTED_VALUE_BN)


# ---------------------------------------------------------------------------
# Full hybrid.parse_one path (not just the parser in isolation) -- the same
# rigor test_current_account.py applies, since a parser-only test cannot
# catch a validate_value rejection.
# ---------------------------------------------------------------------------


def test_parse_one_resolves_bop_summary_as_deterministic(indicator):
    with patch("parsers.hybrid._sanity_check", return_value=_plausible_sanity()):
        snapshot = parse_one(_artifact(), indicator, history=[])
    assert snapshot["value"] == pytest.approx(_EXPECTED_VALUE_BN)
    assert snapshot["_provenance"] == "deterministic"
    assert snapshot["_parse_strategy"] == "bb_bop_row"
    assert snapshot["source_as_of"] == _EXPECTED_SOURCE_AS_OF.isoformat()


# ---------------------------------------------------------------------------
# Config-shape guards.
# ---------------------------------------------------------------------------


def test_config_no_longer_declares_an_llm_prompt(indicator):
    """Deterministic is now the sole writer for this metric -- mirrors
    current_account_balance's PR #111 config shape."""
    assert "llm_prompt" not in indicator["parse"]


def test_config_uses_the_dedicated_bb_bop_row_parser(indicator):
    assert indicator["parse"]["deterministic"] == "bb_bop_row"


def test_config_task_names_the_overall_balance_row_not_current_account(indicator):
    task = indicator["fetch"]["task"]
    assert task.startswith("row=Overall Balance")
    assert "current account" not in task.lower().split(" -- ", 1)[0]


def test_config_range_and_domain_unchanged(indicator):
    assert tuple(indicator["parse"]["valid_range"]) == (-20.0, 20.0)
    assert indicator["parse"]["value_type"] == "amount_usd_bn"
    assert indicator["domain"] == "external_sector"
    assert indicator["cadence"] == "monthly"
