"""S6 — ways_means_usage_cr (Ways & Means Advances usage; BB overdraft to govt).

After the R4 fix this metric uses the ``pdf_table_latest`` parser (scan-by-row-label
across all pages, take the latest absolute Tk-crore value), mirroring the working
``broad_money`` / ``reserve_money`` siblings — NOT the brittle ``pdf_table_row``,
which hard-locked an absolute page/table/col index AND mangled the multi-word row
label on its ``=`` split (``row=Ways and Means`` collapsed to ``row='Ways'`` because
the bare tokens ``and``/``Means`` were dropped).

USAGE-ONLY by design: there is NO published monthly limit/ceiling cell, so this
metric carries no 'vs limit' denominator.

The exact LIVE row label ('Ways and Means Advances' vs 'WMA') and the on-page layout
are VPS-deferred (BD egress firewalls this Mac). These tests prove the config wires
the robust parser and that the parser resolves the WMA usage level from MEI-shaped
text using the config's own quoted instruction.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

CONFIG = Path(__file__).parent.parent / "config" / "sources-v3.json"

# CEIC-sourced reference prints (Tk crore): Nov-2025 = 120,000; Oct-2025 = 90,924.
WMA_USAGE_CR = 120000.0

# MEI government-finance block as pdfplumber would extract it: the WMA usage level
# sits in the absolute Tk-crore columns, followed by a smaller pct-change column.
MEI_GOVT_FINANCE = """
Government Finance (Tk crore)               Jun'25      Sep'25      Oct'25   chg%
Bank Borrowing (net)                       198000.00   205400.00   210000.00  6.06
Ways and Means Advances                     85000.00    90924.00   120000.00 41.31
Non-bank Borrowing (net)                    91000.00    93500.00    95000.00  4.40
""".strip()


@pytest.fixture(scope="module")
def mod():
    """pdfplumber is lazy-imported inside the parser, so module import is
    dependency-free — the pure ``_find_latest_in_text`` is what we exercise."""
    return importlib.import_module("parsers.pdf_table_latest")


@pytest.fixture(scope="module")
def ways_means_cfg() -> dict:
    data = json.loads(CONFIG.read_text())
    inds = data["indicators"] if isinstance(data, dict) and "indicators" in data else data
    return next(i for i in inds if i["id"] == "ways_means_usage_cr")


def test_config_uses_scan_by_label_parser(ways_means_cfg):
    """R4 intent: the metric must be wired to the scan-by-label parser, not the
    brittle absolute-index pdf_table_row. A regression back to pdf_table_row fails."""
    assert ways_means_cfg["parse"]["deterministic"] == "pdf_table_latest"
    assert ways_means_cfg["parse"]["llm_prompt"] == "pdf_component.txt"


def test_config_task_parses_to_full_label(mod, ways_means_cfg):
    """The quotes are load-bearing: pdf_table_latest needs row="<label>". The config
    task must parse to the FULL label, not the 'Ways' fragment the old '=' split gave."""
    label, min_value = mod._parse_instruction(ways_means_cfg["fetch"]["task"])
    assert label == "Ways and Means"
    assert label != "Ways"  # the exact regression the old pdf_table_row produced
    assert min_value == 1000.0


def test_alternate_task_uses_same_grammar(mod, ways_means_cfg):
    """The alternate (MoF Debt Bulletin) task must use the same robust grammar so a
    fallback fetch does not re-trip the old '=' split bug."""
    label, min_value = mod._parse_instruction(ways_means_cfg["alternate"]["task"])
    assert label == "Ways and Means"
    assert min_value == 1000.0


def test_extracts_wma_usage_level(mod):
    """Scan-by-label resolves the WMA usage level (Tk crore), skipping the pct column."""
    v = mod._find_latest_in_text(MEI_GOVT_FINANCE, "Ways and Means", min_value=1000.0)
    assert v == WMA_USAGE_CR


def test_usage_sits_inside_config_band(mod, ways_means_cfg):
    v = mod._find_latest_in_text(MEI_GOVT_FINANCE, "Ways and Means", min_value=1000.0)
    lo, hi = ways_means_cfg["parse"]["valid_range"]
    assert lo <= v <= hi


def test_min_filter_excludes_pct_change_column(mod):
    """Without min the trailing pct-change (41.31) would win; min=1000 keeps the level."""
    assert mod._find_latest_in_text(MEI_GOVT_FINANCE, "Ways and Means", 0.0) == 41.31
    assert (
        mod._find_latest_in_text(MEI_GOVT_FINANCE, "Ways and Means", 1000.0)
        == WMA_USAGE_CR
    )
