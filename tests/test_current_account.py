"""End-to-end tests for the current_account_balance metric (cab-memo-2026-08-05.md).

Two independent, stacked defects made the deterministic parser 100% dead for
this metric in production:

1. `html_table_row`'s `col=2` instruction is a hardcoded POSITION. BB's real
   Balance of Payments table orders columns prior-FY, current-FY-revised,
   current-FY-provisional (left to right) — NOT chronologically — so
   `col=2` always read last year's number (-778), never the live one (-301).
2. `html_table_row` has no unit-conversion step. BB states the table in USD
   million; the config declares `amount_usd_bn`. Even the right cell would
   have been 1000x off.

Both defects meant `validate_value` rejected the deterministic result on
EVERY run, so `hybrid.parse_one` silently fell through to the LLM extract
path — whose prompt contained a self-contradiction ("col=2" vs "most recent
period") that a fresh Sonnet call resolved differently run to run, producing
the observed -0.301 / -0.778 alternation in Supabase `metric_history`.

The fix (this PR): a dedicated parser (`parsers/bb_bop_row.py`) that selects
the column by HEADER TEXT (fiscal year + month window), never by position,
and converts units by reading the page's own "In million US$" label. The LLM
extraction path is removed for this metric entirely (`llm_prompt` key gone
from config) — deterministic is now the sole writer.

These tests run the REAL captured BB page (`tests/fixtures/bb_bop_2026-08.html`,
fetched verbatim from bb.org.bd — see PR body for provenance) through
`parsers.hybrid.parse_one`, not just the parser in isolation, because a
parser-only test cannot catch a `validate_value` rejection — which is
precisely the failure mode that hid in production for two months while the
existing (inverted-semantics) synthetic fixture stayed green.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import parsers.bb_bop_row  # noqa: F401  triggers @register
from claude_max.validators import validate_value
from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.bb_bop_row import _detect_unit_divisor, _score_column_header, _select_column
from parsers.hybrid import parse_one
from parsers.registry import REGISTRY, get_parser

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG = _REPO_ROOT / "config" / "sources-v3.json"
_REAL_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "bb_bop_2026-08.html"

# The real published number this metric should resolve to (memo section 1):
# BB, Balance of Payments (Monthly), Current Account Balance, FY2025-26
# July-May (Provisional) column = -US$301 million = -0.301 bn. Corroborated
# independently by TBS and New Age reporting the same figure the same week.
_EXPECTED_VALUE_BN = -0.301
_PRIOR_YEAR_VALUE_BN = -0.778  # FY2024-25 Jul-May — the bug's wrong answer
_JUL_APR_VALUE_BN = -1.229  # FY2025-26 Jul-Apr (col 3) — also not the target
_OVERALL_BALANCE_VALUES_BN = {-1.151, 3.741, 4.019}  # landmine 19a: wrong ROW
_OTHER_ST_LOANS_COLLISION_BN = -1.073  # same numeric value as an unrelated row


@pytest.fixture(scope="module")
def indicator() -> dict:
    cfg = json.loads(_CONFIG.read_text())
    matches = [i for i in cfg["indicators"] if i["id"] == "current_account_balance"]
    assert matches, "current_account_balance missing from sources-v3.json"
    return matches[0]


def _real_artifact() -> FetchResult:
    assert _REAL_FIXTURE.exists(), f"missing real fixture {_REAL_FIXTURE}"
    return FetchResult(
        indicator_id="current_account_balance",
        artifact_path=_REAL_FIXTURE,
        artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.bb.org.bd/en/index.php/econdata/bop",
        sha256="x" * 64,
        cache_hit=False,
    )


def _plausible_sanity():
    return type("R", (), {"parsed": {"plausible": True, "reason": "ok"}, "raw_text": ""})()


# ---------------------------------------------------------------------------
# End-to-end: parse_one against the REAL captured BB page.
# ---------------------------------------------------------------------------


def test_parse_one_resolves_current_provisional_column_on_real_page(indicator):
    """The core S1/D2 guarantee: parse_one on the real BB fixture returns
    -0.301 bn with provenance=deterministic — the deterministic parser is no
    longer dead code for this metric."""
    with patch("parsers.hybrid._sanity_check", return_value=_plausible_sanity()):
        snapshot = parse_one(_real_artifact(), indicator, history=[])
    assert snapshot["value"] == pytest.approx(_EXPECTED_VALUE_BN)
    assert snapshot["_provenance"] == "deterministic"
    assert snapshot["_parse_strategy"] == "bb_bop_row"


def test_parse_one_does_not_select_the_prior_year_column(indicator):
    """Regression guard for the exact bug that shipped: col=2 read the
    PRIOR fiscal year (-778), not the current one."""
    with patch("parsers.hybrid._sanity_check", return_value=_plausible_sanity()):
        snapshot = parse_one(_real_artifact(), indicator, history=[])
    assert snapshot["value"] != pytest.approx(_PRIOR_YEAR_VALUE_BN)
    assert snapshot["value"] != pytest.approx(_JUL_APR_VALUE_BN)


def test_parse_one_does_not_select_overall_balance_row(indicator):
    """Landmine 19a guard: Current Account Balance and Overall Balance are
    different rows of the same table. None of Overall Balance's three
    period values should ever come out of this indicator."""
    with patch("parsers.hybrid._sanity_check", return_value=_plausible_sanity()):
        snapshot = parse_one(_real_artifact(), indicator, history=[])
    assert snapshot["value"] not in _OVERALL_BALANCE_VALUES_BN


def test_parser_does_not_collide_with_other_short_term_loans_row(indicator):
    """The live table incidentally contains -1073 twice in spirit: as the
    observed -1.073 alternation value (a stale FY25 Jul-Apr reading from an
    earlier vintage) AND as 'Other short term loans (net)' col 4 on the
    CURRENT page. Row selection is by exact label match, not by scanning for
    a plausible-looking number, so this collision cannot bite."""
    parser = get_parser("bb_bop_row")
    result = parser.parse(_real_artifact(), instruction="row=Current Account Balance")
    assert result.value != pytest.approx(_OTHER_ST_LOANS_COLLISION_BN)
    # Sanity: the collision row really does hold -1073 on this real page,
    # under a DIFFERENT row label — proving the guard is real, not vacuous.
    other = parser.parse(_real_artifact(), instruction="row=Other short term loans (net)")
    assert other.value == pytest.approx(_OTHER_ST_LOANS_COLLISION_BN)


def test_validate_value_passes_on_the_converted_value(indicator):
    """The exact assertion the old suite never made: validate_value must
    ACCEPT the parser's output. The shipped bug wasn't a wrong number in
    isolation — it was a number that failed this call on every run, silently
    routing every write through the LLM instead."""
    parser = get_parser("bb_bop_row")
    result = parser.parse(_real_artifact(), instruction=indicator["fetch"]["task"])
    validate_value(
        value=result.value,
        value_type=indicator["parse"]["value_type"],
        valid_range=tuple(indicator["parse"]["valid_range"]),
    )  # must not raise
    assert result.value == pytest.approx(_EXPECTED_VALUE_BN)


def test_raises_when_current_account_row_absent(tmp_path: Path):
    """If the Current Account row is missing, the parser must raise
    ParseError (so hybrid falls through to the terminal fallback), never
    silently return a wrong row. Uses minimal non-BB content purely to
    exercise the error path — not presented as real BB output."""
    html = """
    <html><body><table>
      <tr><th>Items</th><th>2024-25RJuly-May</th><th>2025-26PJuly-May</th></tr>
      <tr><td>Overall Balance</td><td>-1151</td><td>4019</td></tr>
    </table></body></html>
    """
    p = tmp_path / "cab_missing_row_test.html"
    p.write_text(html)
    artifact = FetchResult(
        indicator_id="current_account_balance", artifact_path=p, artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.bb.org.bd/en/index.php/econdata/bop",
        sha256="x" * 64, cache_hit=False,
    )
    parser = get_parser("bb_bop_row")
    with pytest.raises(ParseError):
        parser.parse(artifact, instruction="row=Current Account Balance")


# ---------------------------------------------------------------------------
# Column-selection unit tests — header-text semantics, not position.
# ---------------------------------------------------------------------------


def test_score_column_header_ranks_current_provisional_highest():
    """2025-26P July-May (current, 11mo) must outrank both 2025-26R
    July-Apr (current FY but only 10mo) and 2024-25R July-May (prior FY,
    11mo) — fiscal year wins first, month-window breaks ties."""
    prior = _score_column_header("2024-25RJuly-May")
    current_revised = _score_column_header("2025-26RJuly-Apr")
    current_provisional = _score_column_header("2025-26PJuly-May")
    assert current_provisional > current_revised > prior


def test_score_column_header_rejects_non_fiscal_columns():
    """A '% Changes' delta column carries no fiscal-year/month pattern and
    must never be selectable."""
    assert _score_column_header("% Changes4 over 2") is None
    assert _score_column_header("Items") is None


def test_select_column_picks_index_not_position_four():
    """Regression guard against reintroducing a NEW hardcoded index (e.g.
    'always pick col 4') — the memo explicitly warns the yearly BoP page has
    4 columns where the monthly page has 5, so a hardcoded index would break
    on that page. Column selection must derive from header text alone."""
    monthly_headers = [
        "Items", "2024-25RJuly-May", "2025-26RJuly-Apr", "2025-26PJuly-May", "% Changes4 over 2",
    ]
    assert _select_column(monthly_headers) == 3  # 0-based: the provisional column

    # A differently-shaped header row (fewer columns, different order) must
    # still resolve to whichever column is the most-recent FY / longest span
    # — not to a hardcoded index that happened to work for the 5-column page.
    reordered_headers = ["Items", "2025-26PJuly-May", "2024-25RJuly-May"]
    assert _select_column(reordered_headers) == 1


def test_select_column_raises_when_no_fiscal_year_header_present():
    with pytest.raises(ParseError):
        _select_column(["Items", "% Changes", "Notes"])


def test_detect_unit_divisor_reads_million_from_real_page():
    raw_html = _REAL_FIXTURE.read_text()
    assert _detect_unit_divisor(raw_html) == 1000.0


def test_detect_unit_divisor_reads_billion_when_stated():
    assert _detect_unit_divisor("<p>In billion US$</p>") == 1.0


def test_detect_unit_divisor_raises_without_a_unit_label():
    with pytest.raises(ParseError):
        _detect_unit_divisor("<p>no unit here</p>")


# ---------------------------------------------------------------------------
# Hold-last-good terminal fallback — both flag states of a missing parse.
# ---------------------------------------------------------------------------


def _broken_artifact(tmp_path: Path) -> FetchResult:
    """Artifact whose content cannot produce a Current Account Balance
    reading by any path — used only to exercise the terminal-fallback code,
    not presented as a real BB capture (we did not observe a Radware
    challenge page on this metric's endpoint from this machine; see PR body
    for the real fetch attempt and its outcome)."""
    p = tmp_path / "cab_broken_artifact_test.html"
    p.write_text("<html><body>Service Unavailable</body></html>")
    return FetchResult(
        indicator_id="current_account_balance", artifact_path=p, artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.bb.org.bd/en/index.php/econdata/bop",
        sha256="x" * 64, cache_hit=False,
    )


def test_holds_last_good_value_when_parse_fails_and_last_good_available(indicator, tmp_path):
    """Flag state 1: deterministic fails, no LLM configured, but a real
    last-good snapshot exists — the metric must republish that real number,
    never a synthesised 0.0."""
    last_good = {
        "indicator_id": "current_account_balance",
        "value": -0.301,
        "value_type": "amount_usd_bn",
        "scraped_at": "2026-08-04T08:00:59+00:00",
        "source_url": "https://www.bb.org.bd/en/index.php/econdata/bop",
        "_provenance": "deterministic",
        "_parse_strategy": "bb_bop_row",
        "_stale_from": "2026-08-04",
    }
    snapshot = parse_one(_broken_artifact(tmp_path), indicator, history=[], last_good=last_good)
    assert snapshot["value"] == pytest.approx(-0.301)
    assert snapshot["_provenance"] == "stale_fallback"
    assert snapshot["value"] != 0.0


def test_falls_back_to_needs_review_zero_when_parse_fails_and_no_last_good(indicator, tmp_path):
    """Flag state 2: deterministic fails, no LLM configured, AND no
    last-good snapshot exists (e.g. first-ever run) — there is nothing safe
    to hold, so the original needs_review/0.0 sentinel is still correct
    (it flags for review rather than fabricating a plausible-looking number
    from nothing)."""
    snapshot = parse_one(_broken_artifact(tmp_path), indicator, history=[], last_good=None)
    assert snapshot["value"] == 0.0
    assert snapshot["_provenance"] == "needs_review"
    assert snapshot["_parse_strategy"] == "extract_failed"


# ---------------------------------------------------------------------------
# Config entry — no LLM path, no hardcoded column index, correct parser.
# ---------------------------------------------------------------------------


def test_config_no_longer_declares_an_llm_prompt(indicator):
    """D2 bundle step 2/4: the LLM extraction path is removed for this
    metric — deterministic is the sole writer. (A generic sanity-check call
    still runs; that is a plausibility flag, not a second writer.)"""
    assert "llm_prompt" not in indicator["parse"]


def test_config_task_does_not_hardcode_a_column_index(indicator):
    """Regression guard against reintroducing 'col=N' — column selection
    must come from header text, never a position, per the memo bundle."""
    task = indicator["fetch"]["task"]
    assert "col=" not in task.lower()


def test_config_unit_matches_bop_summary_usd_billion(indicator):
    """Must be USD billion with the same [-20, 20] range as bop_summary —
    a USD-mn unit would store a value 1000x off and break cross-reads."""
    assert indicator["parse"]["value_type"] == "amount_usd_bn"
    assert indicator["parse"]["valid_range"] == [-20.0, 20.0]


def test_config_range_admits_the_real_current_value(indicator):
    lo, hi = indicator["parse"]["valid_range"]
    assert lo <= _EXPECTED_VALUE_BN <= hi


def test_config_domain_and_cadence(indicator):
    assert indicator["domain"] == "macro"
    assert indicator["cadence"] == "monthly"


def test_config_task_names_the_current_account_row_not_overall(indicator):
    task = indicator["fetch"]["task"].lower()
    assert "current account" in task
    assert "overall balance" in task  # explicit do-NOT-pick guard


def test_config_uses_the_dedicated_bb_bop_row_parser(indicator):
    """S1 fix: html_table_row (positional col=N, no unit conversion) is
    replaced by the dedicated header-matching parser."""
    name = indicator["parse"]["deterministic"]
    assert name == "bb_bop_row"
    assert name in REGISTRY


def test_alternate_pdf_task_names_current_account(indicator):
    """The PDF alternate must NOT be bop_summary's bare 'Go to page 31' — it
    must name the Current Account row explicitly."""
    alt_task = indicator["alternate"]["task"].lower()
    assert "current account" in alt_task
    assert "page 31" in alt_task
