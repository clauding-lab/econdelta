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

Scope is intentionally narrow (``_HOST_INTERMEDIATES``) so every other source keeps
the stock default context. Add a host here only after confirming — on a host with
egress to it — that it actually serves an incomplete chain; over-applying a custom
CA bundle is a silent way to mask a genuinely bad cert elsewhere.

Vendored intermediate:
  fetchers/ca/sectigo_r36.pem — "Sectigo Public Server Authentication CA DV R36",
  issued by Root R46 (already in certifi), valid through 2036-03-21. When it
  eventually rotates, mof.gov.bd fetches will fail again with CERTIFICATE_VERIFY_FAILED
  until the new intermediate is re-vendored here.
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
