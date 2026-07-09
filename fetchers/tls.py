"""Host-scoped TLS contexts for the urllib-based fetchers.

Some government sources — confirmed for ``mof.gov.bd`` — serve an INCOMPLETE
certificate chain: they present only the leaf and omit the Sectigo intermediate.
Neither ``urllib`` nor ``requests`` chases the AIA "CA Issuers" URL to fetch the
missing link, so the default trust store cannot build a path to the root and
verification fails with ``CERTIFICATE_VERIFY_FAILED`` — even though the root IS
trusted. (``www.bb.org.bd`` works only because it sends a complete chain.)

The fix completes the chain WITHOUT weakening verification (never ``verify=False``
on a financial-data source): for the affected hosts we build an ``SSLContext`` that
trusts the standard roots (certifi) AND is pre-loaded with the missing intermediate
vendored under ``fetchers/ca/``. OpenSSL then uses the vendored intermediate to
bridge leaf → root.

Scope here is narrow (``_HOST_INTERMEDIATES``) so the urllib fetchers keep the
stock default context for every other source. Add a host here only after
confirming — on a host with egress to it — that it actually serves an incomplete
chain. NOTE (2026-07-09, E1.2): the ``requests`` path takes a different route —
``utils/ca_bundle.combined_ca_bundle`` merges certifi with EVERY PEM in
``fetchers/ca/`` and ``utils/http_client.HttpClient`` verifies against that
globally. That is safe because an intermediate is only a chain-building link: a
leaf must still chain to a certifi-trusted ROOT and match the hostname, so an
additive intermediate cannot mask a genuinely bad cert (the original caution
above applies to adding ROOTS or swapping the whole bundle, not to additive
intermediates). The DSE break proved the same intermediate is needed by multiple
unrelated hosts (mof.gov.bd, www.dse.com.bd, dsebd.org), which host-scoping
handles poorly for requests-based scrapers.

Vendored intermediates (``fetchers/ca/`` is the ONE canonical location — never
create a second cert dir; both this module and ``utils/ca_bundle.py`` read it,
one file = one rotation point):
  fetchers/ca/sectigo_r36.pem — "Sectigo Public Server Authentication CA DV R36",
  issued by Root R46 (already in certifi), valid through 2036-03-21. Completes the
  chain for mof.gov.bd (urllib path, host-scoped here) AND www.dse.com.bd /
  dsebd.org (requests path, via utils/ca_bundle). When it eventually rotates,
  those fetches fail again with CERTIFICATE_VERIFY_FAILED until the new
  intermediate is re-vendored here (fetch it from the leaf's AIA "CA Issuers"
  URL — see AGENTS.md landmine 33).
"""
from __future__ import annotations

import ssl
from pathlib import Path
from urllib.parse import urlparse

_CA_DIR = Path(__file__).resolve().parent / "ca"

# host-suffix -> vendored intermediate PEM that completes that host's chain.
_HOST_INTERMEDIATES: dict[str, str] = {
    "mof.gov.bd": "sectigo_r36.pem",
}


def ssl_context_for(url: str) -> ssl.SSLContext | None:
    """Return a custom verifying ``SSLContext`` for hosts known to serve an
    incomplete chain (matched on exact host or any subdomain), else ``None`` so the
    caller falls back to urllib's stock default. Never disables verification."""
    host = (urlparse(url).hostname or "").lower()
    pem = next(
        (
            name
            for suffix, name in _HOST_INTERMEDIATES.items()
            if host == suffix or host.endswith("." + suffix)
        ),
        None,
    )
    if pem is None:
        return None
    import certifi  # transitive dep (via requests); lazy to keep import cheap

    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.load_verify_locations(cafile=str(_CA_DIR / pem))
    return ctx
