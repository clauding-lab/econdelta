"""Tests pdf_fetcher against an in-process HTTP server serving a fixture PDF."""
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from fetchers.pdf_fetcher import fetch_pdf

_TINY_PDF = (
    b"%PDF-1.1\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
    b"0000000052 00000 n \n0000000099 00000 n \ntrailer<</Size 4/Root 1 0 R>>\n"
    b"startxref\n151\n%%EOF\n"
)


@pytest.fixture
def pdf_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(_TINY_PDF)))
            self.end_headers()
            self.wfile.write(_TINY_PDF)

        def log_message(self, *a, **kw):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/file.pdf"
    server.shutdown()


def test_fetch_pdf_caches_with_sidecar(pdf_server, tmp_path: Path):
    fr = fetch_pdf(
        url=pdf_server,
        indicator_id="bb_mei",
        snapshot_dir=tmp_path,
        as_of_month="2026-04",
    )
    assert fr.artifact_path.exists()
    assert fr.artifact_path.suffix == ".pdf"
    meta = fr.artifact_path.with_suffix(".meta.json")
    assert meta.exists()
    assert fr.sha256 == hashlib.sha256(_TINY_PDF).hexdigest()
    assert fr.cache_hit is False


def test_fetch_pdf_detects_cache_hit(pdf_server, tmp_path: Path):
    fetch_pdf(url=pdf_server, indicator_id="bb_mei", snapshot_dir=tmp_path, as_of_month="2026-04")
    fr2 = fetch_pdf(url=pdf_server, indicator_id="bb_mei", snapshot_dir=tmp_path, as_of_month="2026-04")
    assert fr2.cache_hit is True


def test_fetch_pdf_handles_url_with_spaces(pdf_server, tmp_path: Path):
    """BB publishes some FSAR PDFs with literal spaces in the filename
    ('qfsar (july-september 2025).pdf'). urllib.request.urlopen rejects
    these — the fetcher must percent-encode the path before the request.
    The on-disk filename keeps the original spaces (decoded)."""
    # Substitute a spaced path on the existing local server.
    base = pdf_server.rsplit("/", 1)[0]
    spaced_url = f"{base}/qfsar (july-september 2025).pdf"
    fr = fetch_pdf(
        url=spaced_url,
        indicator_id="bb_fsar",
        snapshot_dir=tmp_path,
        as_of_month="2026-05",
    )
    assert fr.artifact_path.exists()
    # On-disk name preserves the original spaces (not percent-encoded).
    assert "july-september 2025" in fr.artifact_path.name
