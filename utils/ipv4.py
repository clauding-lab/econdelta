"""Scoped IPv4-only DNS resolution for a single outbound fetch.

Why this exists: a few hosts EconDelta fetches from — ``thedocs.worldbank.org``
(World Bank Pink Sheet) and ``www.imf.org`` (IMF Financial Position + DataMapper)
— have their IPv6 (AAAA) addresses **blackholed** from the ExonVPS Dhaka box. A
default dual-stack client tries the dead IPv6 address first and stalls per address
until timeout (~25s each; with several AAAA records that is 60-120s, long enough
for systemd to kill the one-shot scraper service before the fetch ever completes).
Forcing IPv4-only resolution skips the dead addresses and the fetch returns fast.
(Verified on the box 2026-06-01: ``curl -6`` to both hosts times out at 25s;
``curl -4`` succeeds in ~1.5s.)

Why a context manager and not a bare assignment: urllib3 decides the getaddrinfo
family in ``allowed_gai_family()`` by reading its module-global ``HAS_IPV6`` flag.
Flipping it to ``False`` makes every resolution AF_INET-only with zero per-call
plumbing — but it is **process-global**, so it MUST be scoped to the single fetch
and restored afterwards. Otherwise it bleeds into later calls in the same process
(notably the Supabase upsert), which is a real foot-gun this guard removes by
construction: the restore runs in a ``finally`` so even a fetch exception can't
leave the flag stuck.

Note: Supabase itself is IPv4-only (no AAAA record), so the bleed never actually
broke the upsert — but a process-global side effect that is never undone is a
latent bug, and scoping it is the correct, durable shape.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import urllib3.util.connection as _u3conn


@contextlib.contextmanager
def force_ipv4_only() -> Iterator[None]:
    """Within the ``with`` block, resolve hostnames to IPv4 (A) addresses only.

    Saves and restores urllib3's module-global ``HAS_IPV6`` so the override is
    confined to the block and cannot bleed into later requests in the process.
    """
    previous = _u3conn.HAS_IPV6
    _u3conn.HAS_IPV6 = False
    try:
        yield
    finally:
        _u3conn.HAS_IPV6 = previous
