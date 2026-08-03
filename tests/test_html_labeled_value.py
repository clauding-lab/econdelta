"""Unit tests for the div-pseudo-table label/value parser.

The fixture is a trimmed capture of Bangladesh Bank's homepage taken
2026-08-03, keeping all three rate panels — POLICY RATES, RESERVE RATIOS and
INTER-BANK EXCHANGE RATE. All three are `div.display_table`, so the fixture is
also the scoping test.

Context: `policy_rate_repo` served a stale 10.00% for days after BB cut to
9.50% on 2026-07-30, because it was read from a monthly statistical bulletin
that cannot carry an intra-month MPC decision. These tests pin the homepage
panel — which is live — and, just as importantly, pin the parser's refusal to
guess when the panel changes shape.
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest

import parsers.html_labeled_value  # noqa: F401 — registers
from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.registry import get_parser

FIXTURE = Path(__file__).parent / "fixtures" / "bb_homepage_policy_rates.html"

POLICY_PANEL = "div.policy"


def _artifact(tmp_path: Path, html: str) -> FetchResult:
    p = tmp_path / "bb_home.html"
    p.write_text(html, encoding="utf-8")
    return FetchResult(
        indicator_id="policy_rate_repo",
        artifact_path=p,
        artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.bb.org.bd/en/index.php",
        sha256="x" * 64,
        cache_hit=False,
    )


@pytest.fixture
def bb_home(tmp_path: Path) -> FetchResult:
    return _artifact(tmp_path, FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def parser():
    return get_parser("html_labeled_value")


# --------------------------------------------------------------------------
# The three rates EconDelta actually publishes
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label,expected",
    [
        ("Policy Rate (Repo Rate)", 9.50),
        ("SLF Rate", 11.00),
        ("SDF Rate", 7.50),
        ("Bank Rate", 4.00),
    ],
)
def test_reads_each_policy_rate(parser, bb_home, label, expected):
    result = parser.parse(bb_home, f"panel={POLICY_PANEL} label={label}")
    assert result.value == pytest.approx(expected)
    assert result._parse_strategy == "html_labeled_value"


def test_repo_rate_is_not_the_pre_cut_value(parser, bb_home):
    """Regression pin: 10.00 is what the old PDF source kept returning."""
    result = parser.parse(bb_home, f"panel={POLICY_PANEL} label=Policy Rate (Repo Rate)")
    assert result.value != 10.0


# --------------------------------------------------------------------------
# Scoping — three panels on the page share the `display_table` class
# --------------------------------------------------------------------------

def test_panel_scopes_the_search(parser, bb_home):
    """`USD` lives in the exchange panel, so it must NOT resolve under policy."""
    with pytest.raises(ParseError, match="not found"):
        parser.parse(bb_home, f"panel={POLICY_PANEL} label=USD")


def test_same_parser_reads_a_different_panel(parser, bb_home):
    result = parser.parse(bb_home, "panel=div.exchange label=USD")
    assert result.value == pytest.approx(123.82)


def test_missing_panel_raises(parser, bb_home):
    with pytest.raises(ParseError, match="panel"):
        parser.parse(bb_home, "panel=div.no_such_panel label=SLF Rate")


# --------------------------------------------------------------------------
# Fail loudly, never guess — the whole point of this parser
# --------------------------------------------------------------------------

def test_renamed_label_raises_instead_of_matching_a_neighbour(parser, tmp_path):
    """If BB renames the repo row, we must break — not silently serve SLF."""
    html = FIXTURE.read_text(encoding="utf-8").replace(
        "Policy Rate (Repo Rate)", "Repo Rate (Policy)"
    )
    with pytest.raises(ParseError, match="not found"):
        parser.parse(_artifact(tmp_path, html), f"panel={POLICY_PANEL} label=Policy Rate (Repo Rate)")


def test_partial_label_does_not_match(parser, bb_home):
    """Substring matching is what lets a parser lock onto the wrong row."""
    with pytest.raises(ParseError, match="not found"):
        parser.parse(bb_home, f"panel={POLICY_PANEL} label=Policy Rate")


def test_error_lists_the_labels_it_did_see(parser, bb_home):
    """A rename should tell the operator what BB is calling the row now."""
    with pytest.raises(ParseError) as exc:
        parser.parse(bb_home, f"panel={POLICY_PANEL} label=Policy Rate")
    assert "slf rate" in str(exc.value)


def test_non_numeric_value_cell_raises(parser, tmp_path):
    html = FIXTURE.read_text(encoding="utf-8").replace("9.50%", "n/a")
    with pytest.raises(ParseError, match="not a bare number"):
        parser.parse(_artifact(tmp_path, html), f"panel={POLICY_PANEL} label=Policy Rate (Repo Rate)")


def test_range_value_cell_raises(parser, tmp_path):
    """A corridor printed as '9.50% - 11.00%' is a structure change, not a value."""
    html = FIXTURE.read_text(encoding="utf-8").replace("9.50%", "9.50% - 11.00%")
    with pytest.raises(ParseError, match="not a bare number"):
        parser.parse(_artifact(tmp_path, html), f"panel={POLICY_PANEL} label=Policy Rate (Repo Rate)")


@pytest.mark.parametrize(
    "instruction",
    [
        "label=SLF Rate",                    # no panel
        "panel=div.policy",                  # no label
        "row=SLF Rate col=2",                # html_table_row's syntax
        "",
    ],
)
def test_malformed_instruction_raises(parser, bb_home, instruction):
    with pytest.raises(ParseError, match="instruction must be"):
        parser.parse(bb_home, instruction)


# --------------------------------------------------------------------------
# Normalisation and formatting tolerance
# --------------------------------------------------------------------------

def test_label_match_tolerates_whitespace_and_case(parser, bb_home):
    result = parser.parse(bb_home, f"panel={POLICY_PANEL} label=  policy   rate (REPO rate)  ")
    assert result.value == pytest.approx(9.50)


def test_trailing_colon_on_the_page_label_is_ignored(parser, tmp_path):
    html = FIXTURE.read_text(encoding="utf-8").replace("SLF Rate", "SLF Rate:")
    result = parser.parse(_artifact(tmp_path, html), f"panel={POLICY_PANEL} label=SLF Rate")
    assert result.value == pytest.approx(11.00)


def test_thousands_separator_is_stripped(parser, tmp_path):
    html = FIXTURE.read_text(encoding="utf-8").replace("9.50%", "1,234.56")
    result = parser.parse(_artifact(tmp_path, html), f"panel={POLICY_PANEL} label=Policy Rate (Repo Rate)")
    assert result.value == pytest.approx(1234.56)


def test_negative_value_is_read(parser, tmp_path):
    html = FIXTURE.read_text(encoding="utf-8").replace("9.50%", "-2.64%")
    result = parser.parse(_artifact(tmp_path, html), f"panel={POLICY_PANEL} label=Policy Rate (Repo Rate)")
    assert result.value == pytest.approx(-2.64)


def test_blank_cell_between_label_and_value_is_skipped(parser, tmp_path):
    html = FIXTURE.read_text(encoding="utf-8").replace(
        "<div>\n    9.50%", "<div></div>\n   <div>\n    9.50%", 1
    )
    result = parser.parse(_artifact(tmp_path, html), f"panel={POLICY_PANEL} label=Policy Rate (Repo Rate)")
    assert result.value == pytest.approx(9.50)


# --------------------------------------------------------------------------
# Registration + the deliberate absence of source_as_of
# --------------------------------------------------------------------------

def test_parser_is_registered():
    from parsers.registry import REGISTRY

    assert "html_labeled_value" in REGISTRY


def test_no_source_as_of_is_claimed(parser, bb_home):
    """The panel's 'Last update: 15.02.2026' stamp is stale even when the
    values are current, so the parser must NOT date the value from it."""
    result = parser.parse(bb_home, f"panel={POLICY_PANEL} label=Policy Rate (Repo Rate)")
    assert result.source_as_of is None
    assert not hasattr(parser, "recover_source_as_of")
