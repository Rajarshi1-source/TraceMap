"""Postgres persistence for eval runs.

Deliberately separate from `platform/src/integrations/postgres_client.py`: the
harness is a client of the platform, not part of it, and importing platform
internals would make the eval suite unable to score a platform it cannot import
-- which is the wrong coupling for the thing whose job is to judge it.

Like the platform client, `connect()` returns None rather than raising. A run
that cannot record its scoreboard should still print it; losing the history is
bad, losing the measurement as well is worse.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("eval.store")

SCHEMA = Path(__file__).resolve().parents[1] / "platform" / "db" / "002_eval.sql"

INSERT_RUN = """
INSERT INTO eval_runs
    (id, git_sha, tier, detector_cfg, precision, recall, f1,
     mttd_p50_s, mttd_p95_s, passed)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

INSERT_CASE = """
INSERT INTO eval_cases
    (run_id, case_name, expected_edge, detected_edge, detected_at_s,
     outcome, detail)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""


def conninfo() -> str:
    return (
        f"host={os.environ.get('POSTGRES_HOST', 'localhost')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"user={os.environ.get('POSTGRES_USER', 'tracemap')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', 'change-me-locally')} "
        f"dbname={os.environ.get('POSTGRES_DB', 'tracemap')}"
    )


def _num(v: float | None) -> float | None:
    """NUMERIC cannot hold infinity, and a run that detected nothing has an
    infinite MTTD. NULL is the honest encoding: not slow, unmeasured."""
    if v is None or v == float("inf"):
        return None
    return float(v)


class EvalStore:
    def __init__(self, pool) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls) -> EvalStore | None:
        try:
            from psycopg_pool import AsyncConnectionPool
        except ImportError:
            log.warning("psycopg not installed; eval history disabled")
            return None
        import asyncio
        import sys

        try:
            pool = AsyncConnectionPool(conninfo(), min_size=1, max_size=2,
                                       open=False)
            await pool.open(wait=True, timeout=10)
        except Exception as exc:  # noqa: BLE001 - a run that cannot record its
            # scoreboard should still print it. Losing the history is bad;
            # losing the measurement as well is worse.
            #
            # Name the actual cause. "postgres unreachable" sent an hour to the
            # database when the real problem was the event loop.
            if (sys.platform == "win32"
                    and type(asyncio.get_running_loop()).__name__
                    == "ProactorEventLoop"):
                log.warning(
                    "eval history disabled: psycopg cannot run async on the "
                    "Windows Proactor event loop. Run through eval.runner, "
                    "which selects a compatible loop.")
            else:
                log.warning("postgres unreachable (%s); eval history disabled",
                            exc.__class__.__name__)
            return None
        store = cls(pool)
        await store.apply_schema()
        return store

    async def apply_schema(self) -> None:
        """Idempotent DDL, applied by whoever gets there first.

        The harness applies it rather than relying on the detector worker
        having booted, because `make eval` against a partially-started stack is
        a normal thing to do and failing on a missing table teaches nothing.
        """
        try:
            async with self.pool.connection() as conn:
                await conn.set_autocommit(True)
                await conn.execute(SCHEMA.read_text(encoding="utf-8"))
        except Exception:
            log.exception("could not apply the eval schema")

    async def close(self) -> None:
        await self.pool.close()

    async def insert_run(self, run_id: str, git_sha: str, tier: str,
                         detector_cfg: dict, scores, results: list[dict]) -> None:
        """One run row and its case rows, in one transaction.

        Atomic on purpose: a scoreboard whose per-case detail is half written
        is a scoreboard nobody can audit, and the whole reason these rows exist
        is so a number can be taken apart afterwards.
        """
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(INSERT_RUN, (
                run_id, git_sha or "local", tier,
                json.dumps(detector_cfg),
                _num(scores.precision), _num(scores.recall), _num(scores.f1),
                _num(scores.mttd_p50_s), _num(scores.mttd_p95_s),
                scores.passed,
            ))
            for r in results:
                await cur.execute(INSERT_CASE, (
                    run_id, r["case_name"], r["expected_edge"],
                    r.get("detected_edge"), _num(r.get("detected_at_s")),
                    r["outcome"], r.get("detail") or None,
                ))

    async def latest_runs(self, tier: str = "full", limit: int = 20) -> list[dict]:
        from psycopg.rows import dict_row

        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, started_at, git_sha, tier, detector_cfg,"
                " precision, recall, f1, mttd_p50_s, mttd_p95_s, passed"
                " FROM eval_runs WHERE tier = %s"
                " ORDER BY started_at DESC LIMIT %s",
                (tier, limit),
            )
            return await cur.fetchall()
