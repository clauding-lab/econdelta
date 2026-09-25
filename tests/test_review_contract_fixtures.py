"""Keep the reviewed source documents attached to the dated cross-project contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "tests/fixtures/contracts/brief-observations-v1.json"


def test_review_contract_references_the_exact_pdf_pages() -> None:
    contract = json.loads(CONTRACT.read_text())
    assert contract["contract_version"] == 1
    assert len({case["case_id"] for case in contract["cases"]}) == len(contract["cases"])

    page_counts: dict[str, int] = {}
    for evidence_id, evidence in contract["evidence"].items():
        document = ROOT / evidence["fixture"]
        assert hashlib.sha256(document.read_bytes()).hexdigest() == evidence["sha256"]
        metadata = json.loads(document.with_suffix(".meta.json").read_text())
        assert metadata["sha256"] == evidence["sha256"]
        with pdfplumber.open(document) as pdf:
            page_counts[evidence_id] = len(pdf.pages)
        assert page_counts[evidence_id] == metadata["page_count"]

    for case in contract["cases"]:
        assert case["run_date_bdt"] == "2026-09-25"
        assert case["expected_outputs"]
        for observation in case["source_observations"]:
            assert 1 <= observation["page_pdf"] <= page_counts[observation["evidence_id"]]
            assert observation["row"] and observation["column"]
            assert observation["observation_period"]
