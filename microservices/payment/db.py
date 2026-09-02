"""PostgreSQL access for payment-svc.

psycopg 3 rather than asyncpg: asyncpg has no prebuilt wheel for Python 3.14 and
falls back to a source build that needs gcc in the image. psycopg[binary] ships
wheels, so the runtime image stays slim and no compiler is installed.

M28: every statement is parameterised and the text that may later reach a span
is the parameterised form, never the interpolated one. Spans live for days and
are readable by anyone with dashboard access.
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
        # Bulkhead: bounded pool per service, so one slow dependency cannot
        # starve the others of connections.
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


async def insert_payment(status: str) -> str:
    """Record a charge, so the call produces a real DB span.

    order_id is NULL: order-svc owns the order row and writes its own payments
    row inside its transaction.
    """
    pool = await connect()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO payments (order_id, status) VALUES (NULL, %s) RETURNING id",
                (status,),
            )
            row = await cur.fetchone()
            return str(row[0])


async def mark_refunded(charge_id: str) -> bool:
    pool = await connect()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE payments SET status = 'refunded' WHERE id = %s::uuid",
                (charge_id,),
            )
            return cur.rowcount == 1
