"""F40 -- what /api/graph does when ClickHouse is gone.

Written after chaos experiment 001, not before it. The endpoint had a fallback
chain that looked complete on the page and had a hole in the middle:

    t+0s    ClickHouse killed under live load
    t+36s   graph:current ages past 30s, banner appears -- correct
    t+68s   graph:current expires at its 60s TTL, the live fallback has nothing
            to talk to, and the endpoint returns HTTP 500
    t+131s  ClickHouse restarted; 31s of database boot before it answers

The staleness banner was visible for thirty seconds of a two-minute outage. The
database's own restart takes longer than that, so the banner could never have
covered a real restart -- the exact scenario it was designed for.

These tests pin the corrected chain. The one that would have caught the
original bug is `test_expired_cache_and_dead_clickhouse_serves_last_good`.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import HTTPException

from src.api.routes import get_graph


class FakeCache:
    """Two keys with independent lifetimes, like the real one."""

    def __init__(self, current=None, last_good=None):
        self._current = current
        self._last_good = last_good

    async def get_graph(self):
        return self._current

    async def get_last_good(self):
        return self._last_good


class FakeEngine:
    def __init__(self, result=None, error: Exception | None = None):
        self.result, self.error = result, error
        self.calls = 0

    async def graph(self, window: int):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class FakeRequest:
    def __init__(self, cache, engine):
        self.app = type("A", (), {"state": type("S", (), {
            "cache": cache, "engine": engine})()})()


def payload(age_s: float, *, degraded=False) -> dict:
    return {
        "edges": [{"source": "order-svc", "target": "payment-svc"}],
        "services": [],
        "computedAt": time.time() - age_s,
        "ageSeconds": round(age_s, 1),
        "stale": age_s > 30 or degraded,
        **({"degraded": True} if degraded else {}),
    }


def call(cache, engine):
    return asyncio.run(get_graph(FakeRequest(cache, engine), window=5))


DEAD = ConnectionError("clickhouse: connection refused")


# ---------------------------------------------------------------------------
# The normal paths, so the fix cannot be shown to work by breaking them
# ---------------------------------------------------------------------------

def test_fresh_cache_is_served_without_touching_clickhouse():
    engine = FakeEngine(result={"edges": []})
    out = call(FakeCache(current=payload(4.0)), engine)

    assert out["source"] == "cache"
    assert out["stale"] is False
    assert engine.calls == 0, "the cached path must not query the database"


def test_cold_start_falls_through_to_a_live_query():
    """No cache yet: the builder has not finished its first interval."""
    engine = FakeEngine(result={"edges": [], "services": []})
    out = call(FakeCache(current=None, last_good=None), engine)

    assert out["source"] == "live"
    assert out["stale"] is False
    assert engine.calls == 1


# ---------------------------------------------------------------------------
# The hole the chaos experiment found
# ---------------------------------------------------------------------------

def test_expired_cache_and_dead_clickhouse_serves_last_good():
    """The exact state at t+68s of chaos experiment 001.

    Before the fix this raised out of the endpoint and became an HTTP 500 for
    the remaining hundred seconds of the outage.
    """
    cache = FakeCache(current=None, last_good=payload(95.0, degraded=True))
    out = call(cache, FakeEngine(error=DEAD))

    assert out["source"] == "last-good"
    assert out["edges"], "an empty graph is not a fallback, it is a lie"


def test_last_good_is_never_presented_as_current():
    """Serving it is only defensible because it announces what it is."""
    cache = FakeCache(current=None, last_good=payload(95.0, degraded=True))
    out = call(cache, FakeEngine(error=DEAD))

    assert out["stale"] is True
    assert out["degraded"] is True
    assert out["ageSeconds"] >= 90


def test_nothing_at_all_is_503_and_says_which_layer_failed():
    """503, not 500, and not an empty 200.

    An empty 200 is the worst of the three: the dashboard renders a graph with
    no edges, which is indistinguishable from a system with no traffic.
    """
    with pytest.raises(HTTPException) as e:
        call(FakeCache(current=None, last_good=None), FakeEngine(error=DEAD))

    assert e.value.status_code == 503
    detail = e.value.detail
    assert detail["error"] == "no_graph_available"
    assert detail["live"] == "ConnectionError"
    assert detail["lastGood"] == "absent"


def test_a_live_query_failure_does_not_escape_as_a_500():
    """The general contract, stated once.

    Any exception reaching the client as a 500 from this endpoint is a
    regression: the dashboard cannot distinguish a 500 from a crashed API, so
    it renders the same generic failure for a degraded database as for a dead
    platform.
    """
    for exc in (ConnectionError("refused"), TimeoutError("timed out"),
                RuntimeError("Code: 210. DB::NetException")):
        cache = FakeCache(current=None, last_good=payload(120.0, degraded=True))
        out = call(cache, FakeEngine(error=exc))
        assert out["source"] == "last-good", f"{exc!r} escaped the fallback"


# ---------------------------------------------------------------------------
# The arithmetic that made the bug inevitable
# ---------------------------------------------------------------------------

def test_last_good_outlives_a_database_restart():
    """The measurement that justifies the TTL, not a round number.

    ClickHouse took 31s to answer after `docker start` in chaos experiment 001,
    on a machine with the whole stack already warm. graph:current gives 60s
    total and flags stale at 30, so the banner covered 30s of a 131s outage.
    Last-good has to be longer than a restart by a wide margin or the fix is
    cosmetic.
    """
    from src.cache.redis_cache import (
        GRAPH_TTL_S,
        LAST_GOOD_TTL_S,
        STALE_AFTER_S,
    )

    measured_clickhouse_restart_s = 31.0

    # The original window, reproduced so its size is visible in a test rather
    # than argued about in a comment.
    banner_window_s = GRAPH_TTL_S - STALE_AFTER_S
    assert banner_window_s < measured_clickhouse_restart_s, (
        "graph:current now outlives a database restart on its own -- re-check "
        "whether last-good is still earning its place"
    )

    assert 10 * measured_clickhouse_restart_s < LAST_GOOD_TTL_S
