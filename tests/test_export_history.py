"""E2.4 — off-box export of irreplaceable history."""

from __future__ import annotations

import json

import pytest

from scripts.export_history import (
    ExportError,
    export_history,
    is_rescrapable_daily,
    paginate_table,
)

_MONTHLY = [
    {"metric_id": "cpi_headline_monthly", "as_of": "2026-06-01", "value": 9.1},
    {"metric_id": "fiscal_bank_borrow_monthly", "as_of": "2026-05-01", "value": 2862},
]
_DAILY_TABLE = [
    {"metric_id": "money_multiplier", "as_of": "2026-05-31", "value": 5.37, "source": "EconDelta"},
    {"metric_id": "dsex", "as_of": "2026-07-08", "value": 5804.0, "source": "EconDelta"},
    {"metric_id": "dse_close_GP", "as_of": "2026-07-08", "value": 320.0, "source": "DSE"},
]


def _fetcher(table):
    return {"metric_history_monthly": _MONTHLY, "metric_history": _DAILY_TABLE}.get(table, [])


def test_export_writes_both_tables_with_manifest(tmp_path):
    out = export_history(tmp_path, fetcher=_fetcher)
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["manifest"] == {
        "metric_history_monthly": 2,
        "metric_history": 3,
        "metric_definitions": 0,
        "metric_definitions_monthly": 0,
        "auction_results": 0,
        "media_review": 0,
    }
    assert payload["tier"] == "all"
    assert len(payload["tables"]["metric_history"]) == 3
    assert out.name.startswith("econdelta_history_export_")


def test_irreplaceable_only_drops_rescrapable_daily(tmp_path):
    out = export_history(tmp_path, fetcher=_fetcher, irreplaceable_only=True)
    payload = json.loads(out.read_text())
    kept = {r["metric_id"] for r in payload["tables"]["metric_history"]}
    assert kept == {"money_multiplier"}  # dsex + dse_close_GP dropped
    assert payload["manifest"]["metric_history_monthly"] == 2  # monthly untouched
    assert payload["tier"] == "irreplaceable_only"


def test_is_rescrapable_daily_predicate():
    daily_cfg = frozenset({"call_money_rate"})
    assert is_rescrapable_daily("dsex", daily_cfg) is True
    assert is_rescrapable_daily("dse_close_XYZ", daily_cfg) is True
    assert is_rescrapable_daily("call_money_rate", daily_cfg) is True
    assert is_rescrapable_daily("money_multiplier", daily_cfg) is False
    assert is_rescrapable_daily("cpi_headline_monthly", daily_cfg) is False


def test_mid_write_crash_preserves_prior_good_backup(tmp_path, monkeypatch):
    """Review fix (#86 HIGH): the filename is date-based, so a same-day re-run
    targets the SAME path. A crash mid-write must NOT destroy the previous good
    backup — the atomic .tmp -> os.replace pattern guarantees the good file is
    only ever swapped for a fully-written one."""
    from datetime import datetime, timezone

    import scripts.export_history as eh

    stamp = datetime(2026, 7, 9, 7, 15, tzinfo=timezone.utc)
    good_path = tmp_path / "econdelta_history_export_2026-07-09.json"
    good_payload = {"exported_at": "earlier", "tables": {"metric_history": []}}
    good_path.write_text(json.dumps(good_payload))

    # Crash while producing the re-run's bytes (the mid-write class: the .tmp
    # is partially/never written; out_path must not be touched).
    def _boom(*a, **k):
        raise RuntimeError("simulated mid-write crash")

    monkeypatch.setattr(eh.json, "dumps", _boom)

    with pytest.raises(RuntimeError):
        eh.export_history(tmp_path, fetcher=_fetcher, now=stamp)

    # The prior good backup survives, byte-identical and parseable...
    assert json.loads(good_path.read_text()) == good_payload
    # ...and no partial .tmp is left behind.
    assert not (tmp_path / "econdelta_history_export_2026-07-09.json.tmp").exists()


def test_paginate_raises_without_credentials(monkeypatch):
    for var in (
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_SERVICE_KEY",
        "SUPABASE_ANON_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ExportError):
        paginate_table("metric_history_monthly")


def test_server_cap_does_not_truncate_and_actual_page_length_advances():
    from urllib.parse import parse_qs, urlparse

    class Session:
        offsets = []

        def get(self, url, **kwargs):
            offset = int(parse_qs(urlparse(url).query)["offset"][0])
            self.offsets.append(offset)
            page = _DAILY_TABLE[offset : offset + 1]
            return type("Response", (), {"status_code": 200, "json": lambda _: page})()

    session = Session()
    assert (
        paginate_table("metric_history", url="https://test.supabase.co", key="k", session=session)
        == _DAILY_TABLE
    )
    assert session.offsets == [0, 1, 2, 3]


def test_repeated_page_refused():
    class Session:
        def get(self, url, **kwargs):
            return type("Response", (), {"status_code": 200, "json": lambda _: _DAILY_TABLE[:1]})()

    with pytest.raises(ExportError, match="duplicate|progress"):
        paginate_table("metric_history", url="https://test.supabase.co", key="k", session=Session())


# --- Round 4 (owner decision D2, 2 Oct 2026): the split-layout repair snapshot -------------
_PROJECT_URL = "https://ssbliukchgibjcjohibi.supabase.co"
_SERVICE_KEY = "sb_secret_synthetic-service-key"  # SYNTHETIC opaque secret-key shape
_NBR_ROWS = [  # SYNTHETIC rows in metric_history's shape
    {"metric_id": "tax_revenue", "as_of": "2026-09-25", "value": 415473.0, "source": "EconDelta",
     "provenance": None, "ingested_at": "2026-09-25T21:18:08.437914+00:00"},
    {"metric_id": "fiscal_nbr_collected_trn", "as_of": "2026-09-25", "value": 4.15,
     "source": "EconDelta", "provenance": None, "ingested_at": "2026-09-25T21:18:08.437914+00:00"},
]


def _service_env(monkeypatch, *, service: bool = True) -> None:
    for var in ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_KEY", "SUPABASE_ANON_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SUPABASE_URL", _PROJECT_URL)
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-only")
    if service:
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", _SERVICE_KEY)


def test_repair_snapshot_writes_the_split_layout_the_round4_generator_verifies(
    tmp_path, monkeypatch
):
    import hashlib

    from scripts.export_history import export_repair_snapshot

    _service_env(monkeypatch)
    calls = []

    def fetch(table, key):
        calls.append((table, key))
        return _NBR_ROWS

    out = tmp_path / "round4-recapture"
    export_repair_snapshot(out, fetcher=fetch)

    manifest = json.loads((out / "manifest.json").read_text())
    raw = (out / "metric_history.json").read_bytes()
    assert calls == [("metric_history", _SERVICE_KEY)]  # the service key, never the anon key
    assert json.loads(raw) == _NBR_ROWS
    assert set(manifest) == {
        "target_project", "started_at", "non_transactional", "key_role", "tables"
    }
    assert manifest["target_project"] == "ssbliukchgibjcjohibi"
    assert manifest["non_transactional"] is True and manifest["key_role"] == "service"
    assert manifest["tables"] == {
        "metric_history": {
            "rows": 2,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "key": ["metric_id", "as_of"],
        }
    }
    assert sorted(p.name for p in out.iterdir()) == ["manifest.json", "metric_history.json"]
    from scripts.nbr_round4_candidates import load_snapshot

    snapshot = load_snapshot(out, "recapture R4")  # the round-4 generator accepts the layout
    assert snapshot.table_sha256 == hashlib.sha256(raw).hexdigest() and snapshot.rows == _NBR_ROWS


def test_repair_snapshot_refuses_an_existing_directory(tmp_path, monkeypatch):
    from scripts.export_history import export_repair_snapshot, main

    _service_env(monkeypatch)
    out = tmp_path / "round4-night1"
    out.mkdir()
    (out / "keep.txt").write_text("earlier evidence")

    with pytest.raises(ExportError, match="already exists"):
        export_repair_snapshot(out, fetcher=lambda table, key: _NBR_ROWS)
    assert main(["--repair-snapshot", str(out), "--table", "metric_history"]) == 1
    assert sorted(p.name for p in out.iterdir()) == ["keep.txt"]


def test_repair_snapshot_refuses_without_a_service_key(tmp_path, monkeypatch):
    from scripts.export_history import export_repair_snapshot

    _service_env(monkeypatch, service=False)  # no service variable at all
    # The anon slot holds a value that WOULD pass the key-shape check, so only the missing
    # service variable can explain the refusal (an anon fallback would accept it).
    monkeypatch.setenv("SUPABASE_ANON_KEY", "sb_secret_synthetic-but-in-the-anon-slot")
    out = tmp_path / "round4-n2"
    fetched = []

    with pytest.raises(ExportError, match="needs SUPABASE_SERVICE_ROLE_KEY or SUPABASE_SERVICE_KEY"):
        export_repair_snapshot(out, fetcher=lambda table, key: fetched.append(table) or _NBR_ROWS)
    assert fetched == [] and not out.exists()


def test_repair_snapshot_accepts_the_service_key_alias_variable(tmp_path, monkeypatch):
    from scripts.export_history import export_repair_snapshot

    _service_env(monkeypatch, service=False)
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", _SERVICE_KEY)  # the design's named alias
    calls = []
    out = tmp_path / "round4-recapture"

    export_repair_snapshot(out, fetcher=lambda table, key: calls.append(key) or _NBR_ROWS)

    assert calls == [_SERVICE_KEY]
    assert json.loads((out / "manifest.json").read_text())["key_role"] == "service"


def test_repair_snapshot_writes_no_directory_when_the_read_fails(tmp_path, monkeypatch):
    from scripts.export_history import export_repair_snapshot

    _service_env(monkeypatch)
    out = tmp_path / "round4-recapture"

    def fetch(table, key):
        raise ExportError("SYNTHETIC: PostgREST read failed mid-way")

    with pytest.raises(ExportError, match="read failed"):
        export_repair_snapshot(out, fetcher=fetch)
    assert not out.exists()  # a retry can reuse the same evidence path


def _jwt(payload: dict) -> str:
    """An UNSIGNED SYNTHETIC JWT: only the payload's role matters to the exporter."""
    import base64

    def part(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(payload)}.synthetic-signature"


@pytest.mark.parametrize(
    ("key", "refused"),
    [
        pytest.param(_jwt({"role": "anon", "ref": "ssbliukchgibjcjohibi"}), True, id="anon-jwt"),
        pytest.param(_jwt({"ref": "ssbliukchgibjcjohibi"}), True, id="jwt-without-role"),
        pytest.param("opaque-not-a-secret-key", True, id="opaque-non-secret"),
        pytest.param("sb_publishable_synthetic", True, id="publishable-key"),
        pytest.param(_jwt({"role": "service_role", "ref": "ssbliukchgibjcjohibi"}), False,
                     id="service-role-jwt"),
        pytest.param(_SERVICE_KEY, False, id="sb-secret-key"),
    ],
)
def test_repair_snapshot_accepts_only_a_key_that_is_a_service_key(tmp_path, monkeypatch, key, refused):
    """An anon JWT stored under the service variable name would read RLS-trimmed rows silently."""
    from scripts.export_history import export_repair_snapshot

    _service_env(monkeypatch, service=False)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", key)
    out = tmp_path / "round4-recapture"
    fetched = []

    def fetch(table, k):
        fetched.append(table)
        return _NBR_ROWS

    if refused:
        with pytest.raises(ExportError, match="service") as caught:
            export_repair_snapshot(out, fetcher=fetch)
        assert key not in str(caught.value)  # the key is never echoed
        assert fetched == [] and not out.exists()
    else:
        export_repair_snapshot(out, fetcher=fetch)
        assert fetched == ["metric_history"]
        assert json.loads((out / "manifest.json").read_text())["key_role"] == "service"


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://ssbliukchgibjcjohibi.attacker.example", id="other-domain"),
        pytest.param("https://sub.ssbliukchgibjcjohibi.supabase.co", id="extra-subdomain"),
    ],
)
def test_repair_snapshot_refuses_a_non_supabase_url(tmp_path, monkeypatch, url):
    from scripts.export_history import export_repair_snapshot

    _service_env(monkeypatch)
    monkeypatch.setenv("SUPABASE_URL", url)
    out = tmp_path / "round4-recapture"

    with pytest.raises(ExportError, match="SUPABASE_URL must be https://<project>.supabase.co"):
        export_repair_snapshot(out, fetcher=lambda table, key: _NBR_ROWS)
    assert not out.exists()


def _patched_reader(monkeypatch) -> list:
    import scripts.export_history as export_module

    calls = []

    def paginate(table, *, url=None, key=None, **kwargs):
        calls.append((table, url, key))
        return _NBR_ROWS

    monkeypatch.setattr(export_module, "paginate_table", paginate)
    return calls


def test_repair_snapshot_cli_refuses_a_key_flag(tmp_path, monkeypatch):
    from scripts.export_history import main

    _service_env(monkeypatch)
    calls = _patched_reader(monkeypatch)
    out = tmp_path / "round4-recapture"

    code = main(["--repair-snapshot", str(out), "--key", "anon-key-value"])

    assert code == 1 and calls == [] and not out.exists()


def test_repair_snapshot_cli_writes_the_layout_with_the_environment_service_key(
    tmp_path, monkeypatch
):
    from scripts.export_history import main
    from scripts.nbr_round4_candidates import load_snapshot

    _service_env(monkeypatch)
    calls = _patched_reader(monkeypatch)
    out = tmp_path / "round4-recapture"

    code = main(["--repair-snapshot", str(out), "--table", "metric_history"])

    assert code == 0
    assert calls == [("metric_history", None, _SERVICE_KEY)]
    assert sorted(p.name for p in out.iterdir()) == ["manifest.json", "metric_history.json"]
    assert load_snapshot(out, "recapture R4").rows == _NBR_ROWS
