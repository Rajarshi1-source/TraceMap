"""Thin async PostgreSQL access for the platform's system of record.

psycopg 3, not asyncpg - for the same reason the microservices use it (F6):
asyncpg has no wheel for Python 3.14 and falls back to a source build that needs
gcc in the image. Adding a compiler to a runtime image to satisfy a build-time
need is the wrong trade, and it was the wrong trade the first time too.

Deliberately small. Two methods, `%s` placeholders, and a bounded pool.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)


def conninfo() -> str:
    return (
        f"host={os.environ.get('POSTGRES_HOST', 'postgres')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"user={os.environ.get('POSTGRES_USER', 'tracemap')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', 'change-me-locally')} "
        f"dbname={os.environ.get('POSTGRES_DB', 'tracemap')}"
    )


class PostgresClient:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, *, min_size: int = 1, max_size: int = 4) -> PostgresClient | None:
        """Returns None rather than raising.

        Losing the audit trail must not stop detection: alerts still fire, they
        are simply not recorded. A monitoring system that refuses to monitor
        because it cannot write its own history has its priorities backwards.
        """
        try:
            pool = AsyncConnectionPool(conninfo(), min_size=min_size,
                                       max_size=max_size, open=False)
            await pool.open(wait=True, timeout=10)
            return cls(pool)
        except Exception:  # noqa: BLE001
            log.warning("postgres unavailable; anomaly history disabled")
            return None

    async def close(self) -> None:
        await self.pool.close()

    async def execute(self, sql: str, params: tuple | None = None) -> None:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)

    async def execute_script(self, sql: str) -> None:
        """Run multi-statement DDL. autocommit so each statement stands alone."""
        async with self.pool.connection() as conn:
            await conn.set_autocommit(True)
            await conn.execute(sql)

    async def fetch(self, sql: str, params: tuple | None = None) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()
