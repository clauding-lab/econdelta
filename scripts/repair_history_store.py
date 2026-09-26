"""Explicit-target transports for reviewed history repair; no default credentials.

The Docker transport is restricted to an isolated, networkless rehearsal. REST
requires writers to stay paused: GET+PATCH is not a cross-request transaction.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from scripts.repair_observation_history import KEYS, Operation, RepairConflict, Row, RowKey, same


def _identity(table: str, key: RowKey) -> None:
    if table not in KEYS or set(key) != set(KEYS[table]):
        raise RepairConflict("unsupported table/key")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RepairConflict("database redirect refused")


class RestStore:
    """Supabase project pinned by exact HTTPS hostname; keys only from env."""

    def __init__(self, target: str) -> None:
        self.target = target
        project = target.removeprefix("supabase:")
        if not re.fullmatch(r"[a-z]{20}", project):
            raise RepairConflict("invalid Supabase project reference")
        self.url = f"https://{project}.supabase.co"
        if os.environ.get("SUPABASE_URL", "").rstrip("/") != self.url:
            raise RepairConflict("SUPABASE_URL does not match explicit target")
        self.key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get(
            "SUPABASE_SERVICE_KEY"
        )
        if not self.key:
            raise RepairConflict("approved service key must come from environment")
        self.opener = urllib.request.build_opener(_NoRedirect())

    def _request(self, method: str, table: str, key: RowKey, body: Row | None = None) -> list[Row]:
        _identity(table, key)
        params = [(k, f"eq.{v}") for k, v in key.items()] if method != "POST" else []
        params.append(("select", "*"))
        url = f"{self.url}/rest/v1/{table}?{urllib.parse.urlencode(params)}"
        data = json.dumps(body, allow_nan=False).encode() if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "apikey": self.key,
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
        )
        try:
            with self.opener.open(request, timeout=60) as response:
                rows = json.load(response)
        except urllib.error.HTTPError as exc:
            # Never echo headers, credentials or arbitrary server body.
            raise RepairConflict(f"{method} {table} failed HTTP {exc.code}") from None
        except urllib.error.URLError:
            raise RepairConflict(
                f"{method} {table} transport failed; retain intent receipt"
            ) from None
        if not isinstance(rows, list) or len(rows) > 1:
            raise RepairConflict("exact-key response is not zero/one rows")
        return rows

    def get(self, table: str, key: RowKey) -> Row | None:
        rows = self._request("GET", table, key)
        return rows[0] if rows else None

    def change(self, op: Operation) -> None:
        if not same(self.get(op["table"], op["key"]), op["before"]):
            raise RepairConflict("row changed immediately before REST mutation")
        method = "DELETE" if op["after"] is None else ("POST" if op["before"] is None else "PATCH")
        rows = self._request(method, op["table"], op["key"], op["after"])
        expected = op["before"] if method == "DELETE" else op["after"]
        if len(rows) != 1 or not same(rows[0], expected):
            raise RepairConflict("mutation representation mismatch; retain intent receipt")


class DockerStore:
    """Existing isolated PostgreSQL only; local docker exec through Unix socket."""

    def __init__(self, target: str) -> None:
        self.target = target
        match = re.fullmatch(r"docker:([a-zA-Z0-9_-]+)/([a-zA-Z0-9_]+)", target)
        if not match:
            raise RepairConflict("explicit docker:container/database target required")
        self.container, self.database = match.groups()
        result = subprocess.run(
            ["docker", "inspect", self.container], capture_output=True, text=True, check=True
        )
        inspection = json.loads(result.stdout)[0]
        if inspection["HostConfig"]["NetworkMode"] != "none" or inspection["HostConfig"].get(
            "PortBindings"
        ):
            raise RepairConflict("rehearsal container must have no network or published ports")
        if self._sql("SHOW listen_addresses;").strip():
            raise RepairConflict("rehearsal database must listen only on Unix socket")

    def _sql(self, sql: str) -> str:
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                self.container,
                "psql",
                "-U",
                "postgres",
                "-d",
                self.database,
                "-X",
                "-q",
                "-t",
                "-A",
                "-v",
                "ON_ERROR_STOP=1",
            ],
            input=sql,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise RepairConflict(f"isolated database rejected operation: {result.stderr[:500]}")
        return result.stdout

    @staticmethod
    def _json(value: Row | RowKey) -> str:
        encoded = json.dumps(value, allow_nan=False).encode().hex()
        return f"convert_from(decode('{encoded}', 'hex'), 'UTF8')::jsonb"

    def _where(self, table: str, key: RowKey) -> str:
        _identity(table, key)
        record = f"jsonb_populate_record(NULL::public.{table}, {self._json(key)})"
        return " AND ".join(f"t.{k} = ({record}).{k}" for k in KEYS[table])

    def get(self, table: str, key: RowKey) -> Row | None:
        where = self._where(table, key)
        rows = json.loads(
            self._sql(
                f"SELECT coalesce(jsonb_agg(to_jsonb(t)), '[]') FROM public.{table} t WHERE {where};"
            )
        )
        if len(rows) > 1:
            raise RepairConflict("nonunique exact key")
        return rows[0] if rows else None

    def change(self, op: Operation) -> None:
        table, key = op["table"], op["key"]
        where = self._where(table, key)
        before, after = op["before"], op["after"]
        expected = (
            "NULL::jsonb"
            if before is None
            else f"to_jsonb(jsonb_populate_record(NULL::public.{table}, {self._json(before)}))"
        )
        if after is None:
            mutation = f"DELETE FROM public.{table} t WHERE {where};"
        else:
            if any(not re.fullmatch(r"[a-z_][a-z0-9_]*", k) for k in after):
                raise RepairConflict("invalid column identifier")
            record = (
                f"SELECT * FROM jsonb_populate_record(NULL::public.{table}, {self._json(after)})"
            )
            columns = ", ".join(after)
            # Explicit SELECT column order, not physical table order.
            record = f"SELECT {columns} FROM ({record}) r"
            mutation = (
                f"INSERT INTO public.{table} ({columns}) {record};"
                if before is None
                else f"UPDATE public.{table} t SET ({columns}) = ({record}) WHERE {where};"
            )
        self._sql(f"""BEGIN;
LOCK TABLE public.{table} IN SHARE ROW EXCLUSIVE MODE;
DO $repair$ DECLARE actual jsonb; BEGIN
  SELECT to_jsonb(t) INTO actual FROM public.{table} t WHERE {where};
  IF actual IS DISTINCT FROM {expected} THEN RAISE EXCEPTION 'before-image changed'; END IF;
  {mutation}
END $repair$;
COMMIT;""")


def open_store(target: str) -> RestStore | DockerStore:
    """Reject unknown target schemes instead of guessing a production endpoint."""
    if target.startswith("supabase:"):
        return RestStore(target)
    if target.startswith("docker:"):
        return DockerStore(target)
    raise RepairConflict(
        "target must explicitly name supabase:project or docker:container/database"
    )
