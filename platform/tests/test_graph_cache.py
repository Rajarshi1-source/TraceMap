"""F40, second pass -- what may and may not overwrite the last-known-good graph.

The first version of the F40 fix wrote `graph:current` and `graph:lastgood` in
the same unconditional pipeline. That is wrong in one specific and very badly
timed way: an empty graph is a truthful statement about *now*, and a useless
fallback.

The failure it produces is worst exactly when the fallback matters most.
ClickHouse comes back after an outage, the roll-up views have not caught up, the
builder's next cycle succeeds and returns zero edges -- and that empty result
overwrites the last real graph the operator had. They are then shown an empty
map, which is indistinguishable from a system with no traffic, while trying to
find out what broke.

A last-known-good that a degenerate result can overwrite is not a
last-known-good.
"""

from __future__ import annotations

import asyncio
import json

from src.cache.redis_cache import (
    GRAPH_KEY,
    GRAPH_LAST_GOOD_KEY,
    GRAPH_TTL_S,
    LAST_GOOD_TTL_S,
    Cache,
)


class FakePipeline:
    def __init__(self, store: dict, ttls: dict):
        self.store, self.ttls, self.ops = store, ttls, []

    def set(self, key, value, ex=None):
        self.ops.append((key, value, ex))

    async def execute(self):
        for key, value, ex in self.ops:
            self.store[key] = value
            self.ttls[key] = ex
        self.ops.clear()


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}

    def pipeline(self):
        return FakePipeline(self.store, self.ttls)

    async def get(self, key):
        return self.store.get(key)


def make_cache() -> Cache:
    """A Cache with its Redis swapped out. __init__ builds a real client from a
    URL, so the attribute is replaced rather than the constructor mocked."""
    c = Cache.__new__(Cache)
    c.redis = FakeRedis()
    return c


def graph(n_edges: int) -> dict:
    return {
        "nodes": [{"id": "order-svc"}] if n_edges else [],
        "edges": [{"source": "order-svc", "target": f"svc-{i}"}
                  for i in range(n_edges)],
    }


def test_a_healthy_graph_writes_both_copies():
    c = make_cache()
    asyncio.run(c.put_graph(graph(5)))

    assert GRAPH_KEY in c.redis.store
    assert GRAPH_LAST_GOOD_KEY in c.redis.store
    assert c.redis.ttls[GRAPH_KEY] == GRAPH_TTL_S
    assert c.redis.ttls[GRAPH_LAST_GOOD_KEY] == LAST_GOOD_TTL_S


def test_an_empty_graph_updates_current_but_never_last_good():
    """The assertion the first version of the fix would have failed."""
    c = make_cache()
    asyncio.run(c.put_graph(graph(5)))
    good = c.redis.store[GRAPH_LAST_GOOD_KEY]

    asyncio.run(c.put_graph(graph(0)))

    # current tells the truth about now...
    assert json.loads(c.redis.store[GRAPH_KEY])["edges"] == []
    # ...and the fallback is untouched.
    assert c.redis.store[GRAPH_LAST_GOOD_KEY] == good
    assert json.loads(c.redis.store[GRAPH_LAST_GOOD_KEY])["edges"]


def test_the_recovery_sequence_that_motivated_this():
    """ClickHouse returns, the roll-ups lag, the builder succeeds with nothing.

    Three cycles: a healthy graph, then two empty ones as the views catch up.
    The operator must still be able to see the last real shape of the system.
    """
    c = make_cache()
    asyncio.run(c.put_graph(graph(5)))
    for _ in range(2):
        asyncio.run(c.put_graph(graph(0)))

    recovered = asyncio.run(c.get_last_good())
    assert recovered is not None
    assert len(recovered["edges"]) == 5
    assert recovered["stale"] is True
    assert recovered["degraded"] is True


def test_last_good_keeps_its_own_age_not_the_empty_write_s():
    """The age must describe the data being served, not the last write attempt.

    An hour-old graph labelled an hour old beats an empty one labelled nothing;
    that only holds if the label is honest.
    """
    c = make_cache()
    asyncio.run(c.put_graph(graph(3)))
    stored_at = json.loads(c.redis.store[GRAPH_LAST_GOOD_KEY])["computedAt"]

    asyncio.run(c.put_graph(graph(0)))

    assert json.loads(c.redis.store[GRAPH_LAST_GOOD_KEY])["computedAt"] == stored_at
    # And current moved on.
    assert json.loads(c.redis.store[GRAPH_KEY])["computedAt"] > stored_at


def test_a_graph_that_has_never_been_good_yields_no_fallback():
    """On a cold start with no traffic there is nothing to fall back to, and
    the API is expected to answer 503 rather than invent an empty graph."""
    c = make_cache()
    asyncio.run(c.put_graph(graph(0)))

    assert asyncio.run(c.get_last_good()) is None
