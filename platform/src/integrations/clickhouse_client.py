"""Minimal async ClickHouse client over the HTTP interface.

Deliberately small. The workers need three things - run a query, insert rows,
and not fall over when ClickHouse is briefly unavailable - and a thin client
keeps the failure modes obvious. JSONEachRow is used in both directions because
it round-trips Map and Array columns without a bespoke encoder.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)


class ClickHouseError(RuntimeError):
    pass


class ClickHouseClient:
    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        database: str | None = None,
        user: str | None = None,
        password: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base = f"http://{host or os.environ.get('CLICKHOUSE_HOST', 'clickhouse')}:{port or int(os.environ.get('CLICKHOUSE_PORT', '8123'))}"
        self.database = database or os.environ.get("CLICKHOUSE_DB", "tracemap")
        self.params = {"database": self.database}
        # Credentials go in headers, not the query string: a URL is the thing
        # that ends up in logs, traces and error messages.
        self.headers = {
            "X-ClickHouse-User": user or os.environ.get("CLICKHOUSE_USER", "tracemap"),
            "X-ClickHouse-Key": password or os.environ.get("CLICKHOUSE_PASSWORD", ""),
        }
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(self, sql: str, content: bytes | None = None) -> str:
        params = dict(self.params)
        if content is not None:
            params["query"] = sql
            body: bytes = content
        else:
            body = sql.encode()
        r = await self._client.post(self.base, params=params, content=body, headers=self.headers)
        if r.status_code != 200:
            raise ClickHouseError(f"{r.status_code}: {r.text[:400]}")
        return r.text

    @staticmethod
    def _bind(sql: str, params: dict[str, Any] | None) -> str:
        """Substitute {name} placeholders.

        Deliberately NOT %-formatting: ClickHouse SQL is full of literal percent
        signs (formatDateTime('%Y-%m-%d'), LIKE patterns), and `sql % params`
        chokes on them with "unsupported format character". Nor str.format(),
        which would trip over JSON braces. Only the keys we were given are
        replaced, and nothing else in the string is touched.

        Values here are always internal constants - intervals, limits, a
        watermark this process computed. Never user input and never span
        content, so this is not a SQL-injection surface.
        """
        if not params:
            return sql
        for k, v in params.items():
            sql = sql.replace("{" + k + "}", str(v))
        return sql

    async def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        """Run a SELECT and return rows as dicts."""
        sql = self._bind(sql, params)
        text = await self._post(sql.strip().rstrip(";") + "\nFORMAT JSONEachRow")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        await self._post(self._bind(sql, params))

    async def insert(self, table: str, rows: list[dict]) -> None:
        """Insert rows using JSONEachRow."""
        if not rows:
            return
        payload = "\n".join(json.dumps(r, default=str) for r in rows).encode()
        await self._post(f"INSERT INTO {table} FORMAT JSONEachRow", content=payload)

    async def ping(self) -> bool:
        try:
            r = await self._client.get(f"{self.base}/ping")
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False
