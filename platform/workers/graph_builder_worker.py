"""graph-builder: recompute the dependency map every 10s into Redis.

The API never queries ClickHouse on the request path. This worker does it on a
fixed cadence and writes one JSON blob; the API returns that blob. Read latency
becomes a Redis GET regardless of how expensive the underlying aggregation is,
and the dashboard's 10s poll costs a cache read rather than a fan-out of
percentile merges.

The single-flight lock is what makes that safe with more than one replica. Every
replica wakes on the same interval; without the lock they would all recompute
simultaneously and hammer ClickHouse in lockstep - a self-inflicted thundering
herd, on a schedule.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from prometheus_client import Gauge

from src.cache.redis_cache import Cache
from src.core.query_engine import QueryEngine
from src.integrations.clickhouse_client import ClickHouseClient
from workers._runner import run_forever, serve_metrics

# Backs the GraphStaleness alert. The dashboard shows a staleness banner when
# it knows the data is old (M26); this catches the case the banner cannot --
# the API cheerfully serving a cached graph that nothing has refreshed. Set on
# a successful build only, so a failing builder shows as an ageing timestamp
# rather than as a fresh one.
GRAPH_BUILT = Gauge(
    "tracemap_graph_built_timestamp_seconds",
    "Unix time the dependency map was last rebuilt",
)

log = logging.getLogger(__name__)

BUILD_INTERVAL_S = int(os.environ.get("GRAPH_BUILD_INTERVAL_S", "10"))
GRAPH_WINDOW_MIN = int(os.environ.get("GRAPH_WINDOW_MIN", "5"))


async def build_once(engine: QueryEngine, cache: Cache) -> int:
    if not await cache.acquire_lock():
        # Another replica is already on it. Skipping is the correct outcome,
        # not a failure - the cache will be fresh either way.
        log.debug("graph build skipped: lock held")
        return 0
    try:
        graph = await engine.graph(GRAPH_WINDOW_MIN)
        graph["windowMinutes"] = GRAPH_WINDOW_MIN
        await cache.put_graph(graph)
        GRAPH_BUILT.set(time.time())
        log.info("graph rebuilt: %d nodes, %d edges",
                 len(graph["nodes"]), len(graph["edges"]))
        return len(graph["edges"])
    finally:
        # Always release, even on failure. A lock left held by a crashed build
        # would freeze the map until its TTL expired.
        await cache.release_lock()


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format='{"level":"%(levelname)s","worker":"graph-builder","msg":"%(message)s"}',
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    serve_metrics(int(os.environ.get("METRICS_PORT", "9101")))

    ch = ClickHouseClient()
    cache = Cache()
    engine = QueryEngine(ch)

    for _ in range(60):
        if await ch.ping() and await cache.ping():
            break
        log.info("waiting for clickhouse and redis")
        await asyncio.sleep(2)

    await run_forever("graph_builder", lambda: build_once(engine, cache), BUILD_INTERVAL_S)


if __name__ == "__main__":
    asyncio.run(main())
