"""MEI selectors against the REAL October 2026 edition, as pdfplumber extracts it.

Why this file exists: from 20 Oct to 25 Nov 2026 every crore/USD MEI metric
failed in production because the parser looked for "BDT in crore" while
Bangladesh Bank prints "(Crore Taka)" / "(Million USD)", and the money-and-
credit table splits row labels across cells (``['B.', 'Deposits', ...]``).
The older tests used hand-written tables that carried our own wording, so
they passed throughout. These tests read the registry's own selectors and
the bank's own extracted text (``tests/fixtures/mei_2026_10_tables.json``),
so a selector or caption mismatch fails here, not silently in production.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from parsers.base import ParseError
from parsers.mei_observation import _associated_unit_caption, _select_observation

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "tests/fixtures/mei_2026_10_tables.json").read_text())


def _registry_selector(metric_id: str) -> dict:
    registry = json.loads((ROOT / "config/sources-v3.json").read_text())
    items = registry if isinstance(registry, list) else (
        registry.get("indicators") or registry.get("sources")
    )
    (item,) = [i for i in items if i["id"] == metric_id]
    assert item["parse"]["deterministic"] == "mei_observation"
    return json.loads(item["fetch"]["task"])


def _select(metric_id: str):
    selector = _registry_selector(metric_id)
    section = FIXTURE["sections"][selector["table"]]
    caption = _associated_unit_caption(section["page_text"], selector["table"])
    return _select_observation(section["table"], selector=selector, caption=caption)


@pytest.mark.parametrize(
    ("metric_id", "value", "as_of", "status"),
    [
        # Money and credit (page 2 of the edition): "September 2026P" column.
        ("deposits_of_the_system", 2_163_071.0, date(2026, 9, 30), "provisional"),
        ("currency_outside_bank", 311_066.0, date(2026, 9, 30), "provisional"),
        # Reserve money (page 3).
        ("deposits_held_with_bb_crr", 101_114.0, date(2026, 9, 30), "provisional"),
        ("money_multiplier", 5.49, date(2026, 9, 30), "provisional"),
        # Import LCs (section 13.1): the October row, FY27 columns.
        ("monthly_import_lc_opening", 7_660.0, date(2026, 10, 31), "provisional"),
        ("monthly_import_lc_settlement", 7_058.0, date(2026, 10, 31), "provisional"),
    ],
)
def test_registry_selector_reads_the_real_october_edition(metric_id, value, as_of, status):
    result = _select(metric_id)
    assert (result.value, result.source_as_of) == (value, as_of)
    assert result.release_status == status


def test_deposits_is_the_total_not_a_component():
    # Demand (221419) + Time (1941652) = the "B. Deposits" total.
    assert _select("deposits_of_the_system").value == 221_419 + 1_941_652


def test_crore_caption_never_proves_usd_million():
    section = FIXTURE["sections"]["money_credit"]
    with pytest.raises(ParseError, match="unit does not prove"):
        _select_observation(section["table"], selector={
            "table": "money_credit", "row": "B. Deposits",
            "column": "latest_month", "unit": "USD million",
        })


def test_split_label_cell_does_not_swallow_values():
    from parsers.mei_observation import _row_label

    assert _row_label(["B.", "Deposits", None, "1878170", "1915263"]) == "b. deposits"
    assert _row_label(["", "", None, "(+6.69)", "(+6.45)"]) == ""
    assert _row_label(["B. Deposits Held with BB*", "86482"]) == "b. deposits held with bb*"


def test_wrapped_label_tail_does_not_claim_the_next_row():
    # June 2026 edition layout: labels wrap onto a second row.
    from parsers.mei_observation import _row_index

    table = [
        ["A. Currency outside", "290436.50"],
        ["banks", "(-0.51)"],
        ["B. Deposits of the", "1742797.50"],
        ["banking system", "(+9.25)"],
    ]
    assert _row_index(table, "B. Deposits")[0] == 2
    assert _row_index(table, "A. Currency outside banks")[0] == 0


def test_money_credit_selector_never_reads_the_reserve_money_table():
    # "B. Deposits" is a prefix of reserve money's "B. Deposits Held with BB*"
    # (101114); without a table-identity check that value would be published
    # as deposits of the banking system if both tables shared a page.
    selector = _registry_selector("deposits_of_the_system")
    section = FIXTURE["sections"]["reserve_money"]
    with pytest.raises(ParseError, match="is not 'money_credit'"):
        _select_observation(section["table"], selector=selector, caption="(Crore Taka)")
