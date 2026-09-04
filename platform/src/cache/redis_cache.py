"""Redis: graph cache, single-flight lock, and the staleness signal.

Three jobs, and the third is the one people skip.

The graph is recomputed by a worker every 10s and read from cache by the API in
under 2ms. When the analytics tier is degraded the cache keeps serving the last
good graph - and the payload says so, because silently stale numbers are worse
than none. An operator who cannot tell fresh from stale will trust the wrong
one at exactly the wrong moment.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import redis.asyncio as aioredis

GRAPH_KEY = "graph:current"
GRAPH_LAST_GOOD_KEY = "graph:lastgood"
GRAPH_LOCK = "graph:lock"

# Longer than the builder's interval so a single slow cycle does not empty the
# cache, short enough that genuinely stale data is flagged quickly.
GRAPH_TTL_S = int(os.environ.get("GRAPH_CACHE_TTL_S", "60"))
LOCK_TTL_S = int(os.environ.get("GRAPH_LOCK_TTL_S", "10"))

# Past this age the payload is marked stale and the UI shows a banner.
STALE_AFTER_S = int(os.environ.get("GRAPH_STALE_AFTER_S", "30"))

# F40. The second copy, and the reason it exists is measured rather than
# imagined: chaos experiment 001 killed ClickHouse under live load and the
# graph API returned HTTP 500 sixty-eight seconds later.
#
# The arithmetic is unforgiving. `graph:current` lives GRAPH_TTL_S = 60s past
# the last successful build and the banner appears at STALE_AFTER_S = 30s, so
# the staleness banner -- the feature that exists precisely so degraded data
# beats no data (M26) -- is visible for exactly THIRTY SECONDS. ClickHouse
# alone took 31s to answer after `docker start` in that same experiment, so
# any restart-shaped outage outlives the banner by construction. The one
# scenario the design was for is the one it could not survive.
#
# So last-good is kept for an hour under its own key. It is never served as
# fresh: it carries its real age, `stale` is true, and `degraded` says the
# live path was tried and failed. An hour is a judgement -- long enough to
# cover a database restart or a node reschedule, short enough that nobody
# mistakes it for a working system.
LAST_GOOD_TTL_S = int(os.environ.get("GRAPH_LAST_GOOD_TTL_S", "3600"))


class Cache:
    def __init__(self, url: str | None = None) -> None:
        self.redis = aioredis.from_url(
            url or os.environ.get("REDIS_URL", "redis://redis:6379/0"),
            decode_responses=True,
        )

    async def close(self) -> None:
        await self.redis.aclose()

    async def ping(self) -> bool:
        try:
            return bool(await self.redis.ping())
        except Exception:  # noqa: BLE001
            return False

    # ---- graph ------------------------------------------------------------
    async def put_graph(self, graph: dict[str, Any]) -> None:
        """Write both copies in one round trip.

        Same payload, two TTLs. A pipeline rather than two awaits so a builder
        cycle cannot be interrupted between them and leave last-good newer than
        current, which would make the age arithmetic read backwards.
        """
        graph["computedAt"] = time.time()
        blob = json.dumps(graph)
        pipe = self.redis.pipeline()
        pipe.set(GRAPH_KEY, blob, ex=GRAPH_TTL_S)
        pipe.set(GRAPH_LAST_GOOD_KEY, blob, ex=LAST_GOOD_TTL_S)
        await pipe.execute()

    def _hydrate(self, raw: str, *, degraded: bool = False) -> dict[str, Any]:
        graph = json.loads(raw)
        age = time.time() - graph.get("computedAt", 0)
        graph["ageSeconds"] = round(age, 1)
        graph["stale"] = age > STALE_AFTER_S
        if degraded:
            # Last-good is ALWAYS flagged stale regardless of age. Reaching it
            # means the live path was tried and failed, and a graph served
            # because the analytics tier is down is stale in the sense that
            # matters even if the clock disagrees.
            graph["stale"] = True
            graph["degraded"] = True
        return graph

    async def get_graph(self) -> dict[str, Any] | None:
        raw = await self.redis.get(GRAPH_KEY)
        return self._hydrate(raw) if raw else None

    async def get_last_good(self) -> dict[str, Any] | None:
        """The hour-long copy. Only read when the live query has already failed."""
        raw = await self.redis.get(GRAPH_LAST_GOOD_KEY)
        return self._hydrate(raw, degraded=True) if raw else None

    # ---- single-flight ----------------------------------------------------
    async def acquire_lock(self) -> bool:
        """SET NX EX - atomic, so exactly one replica recomputes.

        Without it every replica rebuilds the graph on the same cadence and they
        all hammer ClickHouse together. The lock TTL is shorter than the build
        interval so a crashed holder cannot wedge the cache.
        """
        return bool(await self.redis.set(GRAPH_LOCK, "1", nx=True, ex=LOCK_TTL_S))

    async def release_lock(self) -> None:
        await self.redis.delete(GRAPH_LOCK)
