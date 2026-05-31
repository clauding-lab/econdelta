"""Unit tests for the current_account_balance metric (S1).

current_account_balance row-selects the Current Account Balance line out of
BB's Balance of Payments table — the SAME source as bop_summary, whose single
persisted scalar means Overall Balance, NOT Current Account (landmine 19a).
These tests assert the parser extracts the Current Account number and NOT the
Overall Balance, and that the config entry stays unit-consistent with
bop_summary (USD billion, range [-20, 20], negatives valid for a deficit).

It reuses the existing html_table_row deterministic parser, so no new parser
is registered; the instruction is `row=Current Account col=<n>` (substring
match on the first cell). The LLM fallback prompt current_account_balance.txt
is the robust real path on the live BB page (verified on ExonVPS Dhaka — BB
firewalls non-BD IPs, so the live fetch is deferred off this Mac).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import parsers.html_table_row  # noqa: F401  triggers @register
from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.registry import REGISTRY, get_parser

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG = _REPO_ROOT / "config" / "sources-v3.json"
_PROMPT = _REPO_ROOT / "claude_max" / "prompts" / "current_account_balance.txt"

# Representative shape of BB's BoP summary table. The Current Account Balance
# row (a deficit, negative) is DISTINCT from the Overall Balance row — the
# whole point of S1 is row-selecting the former, not the latter.
_BOP_HTML = """
<html><body>
<h1>Balance of Payments (in million USD)</h1>
<table>
  <tr><th>Item</th><th>FY25</th><th>FY24</th></tr>
  <tr><td>Trade Balance</td><td>-18,500</td><td>-22,400</td></tr>
  <tr><td>Current Account Balance</td><td>-6,500</td><td>-6,800</td></tr>
  <tr><td>Capital Account</td><td>150</td><td>200</td></tr>
  <tr><td>Financial Account</td><td>4,200</td><td>5,100</td></tr>
  <tr><td>Overall Balance</td><td>3,400</td><td>-1,200</td></tr>
</table>
</body></html>
"""


@pytest.fixture
def bop_artifact(tmp_path: Path) -> FetchResult:
    p = tmp_path / "page.html"
    p.write_text(_BOP_HTML)
    return FetchResult(
        indicator_id="current_account_balance",
        artifact_path=p,
        artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.bb.org.bd/en/index.php/econdata/bop",
        sha256="x" * 64,
        cache_hit=False,
    )


# ---------------------------------------------------------------------------
# Row selection — the core S1 guarantee: Current Account, NOT Overall Balance.
# ---------------------------------------------------------------------------


def test_extracts_current_account_row_not_overall_balance(bop_artifact):
    """The deterministic parser must pick the Current Account Balance row
    (-6,500), never the Overall Balance row (3,400)."""
    parser = get_parser("html_table_row")
    result = parser.parse(bop_artifact, instruction="row=Current Account col=2")
    assert result.value == -6500.0
    # Guard against accidentally matching the Overall Balance line (landmine 19a).
    assert result.value != 3400.0


def test_current_account_value_is_negative_for_a_deficit(bop_artifact):
    """Bangladesh runs a current-account deficit, so a negative value must
    survive parsing (a sign-stripping parser would be wrong)."""
    parser = get_parser("html_table_row")
    result = parser.parse(bop_artifact, instruction="row=Current Account col=2")
    assert result.value < 0


def test_raises_when_current_account_row_absent(tmp_path: Path):
    """If the Current Account row is missing, the deterministic parser must
    raise ParseError (so hybrid falls through to the LLM fallback), never
    silently return a wrong row."""
    html = """
    <html><body><table>
      <tr><th>Item</th><th>FY25</th></tr>
      <tr><td>Overall Balance</td><td>3,400</td></tr>
    </table></body></html>
    """
    p = tmp_path / "page.html"
    p.write_text(html)
    artifact = FetchResult(
        indicator_id="current_account_balance",
        artifact_path=p,
        artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.bb.org.bd/en/index.php/econdata/bop",
        sha256="x" * 64,
        cache_hit=False,
    )
    parser = get_parser("html_table_row")
    with pytest.raises(ParseError):
        parser.parse(artifact, instruction="row=Current Account col=2")


# ---------------------------------------------------------------------------
# Config entry — stays unit-consistent with bop_summary (USD billion, range,
# domain/cadence), and points at a registered deterministic parser.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def indicator() -> dict:
    cfg = json.loads(_CONFIG.read_text())
    matches = [i for i in cfg["indicators"] if i["id"] == "current_account_balance"]
    assert matches, "current_account_balance missing from sources-v3.json"
    return matches[0]


def test_config_unit_matches_bop_summary_usd_billion(indicator):
    """Must be USD billion with the same [-20, 20] range as bop_summary —
    a USD-mn unit would store a value 1000x off and break cross-reads."""
    assert indicator["parse"]["value_type"] == "amount_usd_bn"
    assert indicator["parse"]["valid_range"] == [-20.0, 20.0]


def test_config_range_admits_a_typical_bd_deficit(indicator):
    """BD's current-account deficit (~ -6 to -7 bn) must sit inside the range."""
    lo, hi = indicator["parse"]["valid_range"]
    assert lo <= -6.5 <= hi


def test_config_domain_and_cadence(indicator):
    assert indicator["domain"] == "macro"
    assert indicator["cadence"] == "monthly"


def test_config_task_names_the_current_account_row_not_overall(indicator):
    """The fetch task must explicitly target the Current Account row and warn
    off the Overall Balance row (the semantic trap)."""
    task = indicator["fetch"]["task"].lower()
    assert "current account" in task
    assert "overall balance" in task  # the explicit "NOT the Overall Balance" guard


def test_config_uses_registered_deterministic_parser(indicator):
    """Reuses html_table_row — which is registered via parse_all's import
    block — so no new import line is needed (landmine A)."""
    name = indicator["parse"]["deterministic"]
    assert name == "html_table_row"
    assert name in REGISTRY


def test_alternate_pdf_task_names_current_account(indicator):
    """The PDF alternate must NOT be bop_summary's bare 'Go to page 31' — it
    must name the Current Account row explicitly."""
    alt_task = indicator["alternate"]["task"].lower()
    assert "current account" in alt_task
    assert "page 31" in alt_task


# ---------------------------------------------------------------------------
# Prompt file — exists and is .format()-safe with the hybrid placeholders.
# ---------------------------------------------------------------------------


def test_llm_prompt_exists_and_formats():
    """The fallback prompt must render with the hybrid's HTML placeholders and
    must instruct the model to pick the Current Account row, allow negatives,
    and use USD billion."""
    assert _PROMPT.exists(), f"missing prompt {_PROMPT}"
    template = _PROMPT.read_text()
    rendered = template.format(
        indicator_name="Current Account Balance",
        instruction="row=Current Account col=2",
        value_type="amount_usd_bn",
        valid_range=[-20.0, 20.0],
        html_text="<table>...</table>",
    )
    low = rendered.lower()
    assert "current account balance" in low
    assert "overall balance" in low  # explicit do-NOT-pick guard
    assert "negative" in low  # deficit sign allowed
    assert "billion" in low
    # The strict-JSON contract survives .format() (doubled braces -> single).
    assert '{"value":' in rendered
