"""End-to-end deterministic-parse tests for config-conversion batch 1.

Fix-all plan Steps 11-13 (2026-08-05): 27/67 config.sources-v3.json
extractions were LLM-only — their `fetch.task` didn't match the grammar
their declared deterministic parser requires, so `hybrid.parse_one` always
raised ParseError and fell through to the LLM. Batch 1 converts the 9
EASIEST of those 27 to real deterministic grammar.

The fixture (`tests/_pdfs/bb_mei_2026_june.pdf`) is a VERBATIM capture of
Bangladesh Bank's "Major Economic Indicators: Monthly Update (June 2026)"
PDF, fetched live from bb.org.bd on 2026-08-05 (sha256
30f593863230aaa744d61652f8c8a11f198a06541bfcbf5b4fb7a81a82354b8f — matches
the `.meta.json` sidecar recorded at fetch time). 8 of the 9 batch-1 metrics
share this single document (3 tables + 1 narrative sentence); it is NOT
hand-written.

Every test below reads the indicator's `fetch.task` straight out of
config/sources-v3.json (not a hardcoded copy) and feeds it to the real
registered parser via `get_parser(...).parse(...)` — the same call
`parsers/hybrid.py:parse_one` makes for the deterministic-first attempt.
This is "the real parse entry point" short of the live Sonnet sanity-check
call in `hybrid.parse_one` (which needs a real `claude` CLI invocation and is
therefore exercised manually against this fixture, not in the automated
suite — see the PR body for that run's output).

Expected values are pinned against the real Supabase `metric_history` row
Bangladesh Bank's own LLM-extraction path most recently wrote for the same
June-2026 edition (as_of=2026-06-30) — see the PR body's parity table.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import parsers.pdf_component  # noqa: F401 -- registers
import parsers.pdf_table_latest  # noqa: F401 -- registers
import parsers.pdf_table_row  # noqa: F401 -- registers
from fetchers.base import FetchResult
from parsers.registry import get_parser

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "_pdfs" / "bb_mei_2026_june.pdf"
CONFIG_PATH = REPO_ROOT / "config" / "sources-v3.json"

# Verbatim capture, verified via `shasum -a 256` at commit time.
FIXTURE_SHA256 = "30f593863230aaa744d61652f8c8a11f198a06541bfcbf5b4fb7a81a82354b8f"


def _load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def _indicator(indicator_id: str) -> dict:
    cfg = _load_config()
    for ind in cfg["indicators"]:
        if ind["id"] == indicator_id:
            return ind
    raise KeyError(indicator_id)


def _artifact(indicator_id: str) -> FetchResult:
    return FetchResult(
        indicator_id=indicator_id,
        artifact_path=FIXTURE,
        artifact_type="pdf",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.bb.org.bd//pub/monthly/selectedecooind/2026_june.pdf",
        sha256=FIXTURE_SHA256,
        cache_hit=False,
    )


def test_fixture_sha256_matches_captured_sidecar():
    """Guards against silently swapping the fixture for a hand-edited file."""
    import hashlib
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256


# ---------------------------------------------------------------------------
# The 9 batch-1 conversions, each: real config task -> real registered parser
# -> real fixture -> expected value pinned against production's last LLM read
# for the SAME June-2026 edition (as_of=2026-06-30).
# ---------------------------------------------------------------------------

BATCH1_CASES = [
    # (indicator_id, expected_value, expected_source_as_of)
    ("money_multiplier", 4.92, None),
    ("currency_outside_bank", 349374.0, None),
    ("deposits_of_the_system", 2041692.7, None),
    ("deposits_held_with_bb_crr", 115326.7, None),
    ("bank_borrowing_for_deficit_financing", 94158.9, date(2026, 6, 30)),
    ("non_bank_borrowing_for_deficit_financing", -567.67, date(2026, 6, 30)),
    ("domestic_borrowing_for_budget_deficit", 93591.23, date(2026, 6, 30)),
    ("foreign_borrowing_for_budget_deficit", 21944.28, date(2026, 6, 30)),
    ("point_to_point_inflation", 9.16, date(2026, 6, 30)),
]


@pytest.mark.parametrize("indicator_id,expected_value,expected_as_of", BATCH1_CASES)
def test_batch1_metric_parses_deterministically(indicator_id, expected_value, expected_as_of):
    ind = _indicator(indicator_id)
    parser = get_parser(ind["parse"]["deterministic"])
    result = parser.parse(_artifact(indicator_id), ind["fetch"]["task"])
    assert result.value == pytest.approx(expected_value)
    if expected_as_of is not None:
        assert result.source_as_of == expected_as_of


@pytest.mark.parametrize("indicator_id,expected_value,_", BATCH1_CASES)
def test_batch1_metric_value_passes_its_own_valid_range(indicator_id, expected_value, _):
    """Guards the non_bank_borrowing valid_range widening (was [0, 200000],
    which would reject its real, legitimately-negative -567.67 reading)."""
    from claude_max.validators import validate_value

    ind = _indicator(indicator_id)
    validate_value(
        value=expected_value,
        value_type=ind["parse"]["value_type"],
        valid_range=tuple(ind["parse"]["valid_range"]),
    )  # raises InvalidValueError on failure — the assertion IS "does not raise"


def test_non_bank_borrowing_can_go_negative_by_design():
    """Net non-bank borrowing is a flow (new borrowing minus repayment) and can
    legitimately be negative in a net-repayment month — this is what actually
    happened in the June-2026 edition (banking + non-bank must sum to the
    report's own stated domestic total: 94158.90 + (-567.67) == 93591.23,
    matching the report's Executive Summary prose verbatim)."""
    ind = _indicator("non_bank_borrowing_for_deficit_financing")
    lo, _hi = ind["parse"]["valid_range"]
    assert lo < 0.0


def test_deficit_financing_row_arithmetic_reconciles_against_report_prose():
    """Independent cross-check: the 4 deficit-financing metrics come from 4
    different columns of the SAME table row. bank + non_bank must equal
    domestic exactly, per the table's own '4 = 2+3' column formula (and per
    the report's Executive Summary: 'total net domestic borrowing ... was
    BDT 93591.23 crore, which included net borrowing of BDT 94158.90 crore
    from the banking system')."""
    bank = _indicator("bank_borrowing_for_deficit_financing")
    non_bank = _indicator("non_bank_borrowing_for_deficit_financing")
    domestic = _indicator("domestic_borrowing_for_budget_deficit")

    bank_v = get_parser("pdf_table_row").parse(_artifact("x"), bank["fetch"]["task"]).value
    non_bank_v = get_parser("pdf_table_row").parse(_artifact("x"), non_bank["fetch"]["task"]).value
    domestic_v = get_parser("pdf_table_row").parse(_artifact("x"), domestic["fetch"]["task"]).value

    assert bank_v + non_bank_v == pytest.approx(domestic_v)


def test_batch1_ids_are_registered_with_a_real_parser_in_config():
    """Companion to tests/test_parser_registry_coverage.py: pin that batch 1's
    `deterministic` field actually changed to a real, registered parser (not
    left as a stale/mismatched name)."""
    expected_parser_by_id = {
        "money_multiplier": "pdf_table_latest",
        "currency_outside_bank": "pdf_table_latest",
        "deposits_of_the_system": "pdf_table_latest",
        "deposits_held_with_bb_crr": "pdf_table_latest",
        "bank_borrowing_for_deficit_financing": "pdf_table_row",
        "non_bank_borrowing_for_deficit_financing": "pdf_table_row",
        "domestic_borrowing_for_budget_deficit": "pdf_table_row",
        "foreign_borrowing_for_budget_deficit": "pdf_table_row",
        "point_to_point_inflation": "pdf_component",
    }
    for indicator_id, expected_parser in expected_parser_by_id.items():
        assert _indicator(indicator_id)["parse"]["deterministic"] == expected_parser
