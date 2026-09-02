"""PostgreSQL access for user-svc.

psycopg 3 rather than asyncpg: asyncpg has no prebuilt wheel for Python 3.14 and
would need gcc in the image. psycopg[binary] ships wheels.

M28: parameterised statements only.
"""

from __future__ import annotations

import os

from psycopg_pool import AsyncConnectionPool

_pool: AsyncConnectionPool | None = None


def _conninfo() -> str:
    return (
        f"host={os.environ.get('POSTGRES_HOST', 'postgres')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"user={os.environ.get('POSTGRES_USER', 'tracemap')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', 'change-me-locally')} "
        f"dbname={os.environ.get('POSTGRES_DB', 'tracemap')}"
    )


async def connect() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(_conninfo(), min_size=1, max_size=10, open=False)
        await _pool.open(wait=True, timeout=10)
    return _pool


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def ping() -> bool:
    """Readiness only. Never called from /healthz - see M17."""
    pool = await connect()
    async with pool.connection(timeout=2) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1")
            row = await cur.fetchone()
            return bool(row and row[0] == 1)


async def get_user(handle: str) -> dict | None:
    pool = await connect()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, handle, name, email FROM users WHERE handle = %s",
                (handle,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return {"id": row[0], "handle": row[1], "name": row[2], "email": row[3]}
