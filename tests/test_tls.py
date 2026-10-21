"""Tests for fetchers.tls — host-scoped, chain-completing SSL contexts (R1).

Fully offline: these check the context is BUILT correctly and scoped narrowly. The
proof that it completes the live mof.gov.bd chain is a VPS step (BD egress is
firewalled from CI/this Mac).
"""
from __future__ import annotations

import ssl

from fetchers.tls import ssl_context_for


def test_mof_gov_bd_gets_custom_verifying_context():
    ctx = ssl_context_for("https://mof.gov.bd/site/page/debt-bulletin")
    assert ctx is not None
    # Verification must STAY ON — the fix completes the chain, never disables checks.
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_subdomain_of_mof_gets_custom_context():
    ctx = ssl_context_for("https://www.mof.gov.bd/some.pdf")
    assert ctx is not None
    # The subdomain path must be a VERIFYING context too — a regression that returned
    # a non-verifying context only for subdomains must not slip through.
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_vendored_intermediate_is_in_trust_store():
    ctx = ssl_context_for("https://mof.gov.bd/")
    common_names = [
        dict(pair for rdn in cert["subject"] for pair in rdn).get("commonName", "")
        for cert in ctx.get_ca_certs()
    ]
    assert any("R36" in cn for cn in common_names), (
        "vendored Sectigo R36 intermediate not loaded into the trust store"
    )


def test_other_hosts_use_default_context():
    assert ssl_context_for("https://www.bb.org.bd/some.pdf") is None
    assert ssl_context_for("https://thedocs.worldbank.org/some.xlsx") is None


def test_lookalike_host_is_not_treated_as_mof():
    # A host that merely embeds 'mof.gov.bd' as a non-suffix must NOT match.
    assert ssl_context_for("https://mof.gov.bd.evil.example/x") is None


def test_epb_export_index_gets_the_r36_completing_context():
    # epb.gov.bd serves only its leaf (issuer Sectigo DV R36; openssl verify code 21), so the
    # exports leg died with URLError on ExonVPS on 21 Oct 2026 and held the Brief. The
    # vendored R36 intermediate completes the chain (box check: HTTP 200 with it, URLError
    # without), and verification stays on.
    ctx = ssl_context_for("https://epb.gov.bd/views/epb-export-data/-/")
    assert ctx is not None
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    common_names = [
        dict(pair for rdn in cert["subject"] for pair in rdn).get("commonName", "")
        for cert in ctx.get_ca_certs()
    ]
    assert any("R36" in cn for cn in common_names)


def test_epb_workbook_store_and_lookalikes_keep_the_default_context():
    # The workbooks live on an object store that sends a complete chain; only epb.gov.bd
    # and its subdomains are widened.
    assert ssl_context_for(
        "https://objectstorage.ap-dcc-gazipur-1.oraclecloud15.com/n/x/b/office-epb/o/a.xlsx"
    ) is None
    assert ssl_context_for("https://epb.gov.bd.evil.example/x") is None
    assert ssl_context_for("https://notepb.gov.bd/x") is None
