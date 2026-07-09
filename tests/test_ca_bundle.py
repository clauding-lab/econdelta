"""Tests for utils.ca_bundle — the additive certifi + intermediates CA bundle (E1.2).

DSE's servers send an incomplete TLS chain (leaf only, missing the Sectigo R36
intermediate). The intermediate is vendored at fetchers/ca/sectigo_r36.pem — the
ONE canonical cert location, shared with fetchers/tls.py's host-scoped urllib
path — and merged with certifi so HttpClient verifies DSE without ever disabling
verification. These tests pin that the merge is additive, loadable, actually
wired into HttpClient, and that it reads the SAME vendored file fetchers/tls.py
uses (one rotation point — no second cert copy may reappear).
"""
from __future__ import annotations

import ssl
from pathlib import Path

import certifi

from utils.ca_bundle import _CERTS_DIR, combined_ca_bundle

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DSE_INTERMEDIATE = _REPO_ROOT / "fetchers" / "ca" / "sectigo_r36.pem"


def _pem_body(path: Path) -> str:
    """Return the certificate block(s), ignoring the leading comment lines."""
    text = path.read_text()
    return text[text.index("-----BEGIN CERTIFICATE-----"):].strip()


def test_combined_bundle_includes_the_dse_intermediate():
    """The Sectigo R36 intermediate's PEM must be present in the merged bundle —
    that is the whole point: it lets requests build DSE's chain to a certifi root."""
    bundle = Path(combined_ca_bundle()).read_text()
    assert _pem_body(_DSE_INTERMEDIATE) in bundle


def test_combined_bundle_is_a_superset_of_certifi():
    """Additive only — every certifi root is retained; the bundle is strictly larger."""
    bundle = Path(combined_ca_bundle()).read_text()
    certifi_text = Path(certifi.where()).read_text()
    assert certifi_text in bundle
    assert len(bundle) > len(certifi_text)


def test_combined_bundle_is_a_loadable_ca_store():
    """It must parse as a real CA file — ssl raises if any PEM block is malformed."""
    ctx = ssl.create_default_context(cafile=combined_ca_bundle())
    assert ctx.cert_store_stats()["x509_ca"] > 0


def test_http_client_verifies_against_the_combined_bundle():
    """HttpClient must point session.verify at the combined bundle (a real path),
    never leave it at the certifi-only default — otherwise DSE breaks again."""
    from utils.http_client import HttpClient

    client = HttpClient()
    assert client._session.verify == combined_ca_bundle()
    assert Path(client._session.verify).exists()


def test_single_vendored_cert_location_shared_with_fetchers_tls():
    """Consolidation guard (review MEDIUM on PR #78): utils/ca_bundle and
    fetchers/tls must read the SAME vendored directory, and no second cert dir
    (the old certs/) may reappear — two copies of one intermediate means two
    rotation points and silent drift when the CA rotates."""
    import fetchers.tls as ftls

    assert _CERTS_DIR == ftls._CA_DIR, "ca_bundle and fetchers.tls must share one cert dir"
    assert _DSE_INTERMEDIATE.exists(), "the vendored Sectigo R36 intermediate is missing"
    assert not (_REPO_ROOT / "certs").exists(), (
        "a second cert dir (certs/) reappeared — vendor intermediates ONLY in fetchers/ca/"
    )
