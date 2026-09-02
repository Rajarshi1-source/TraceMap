"""HTTP surface for the dashboard."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/graph")
async def get_graph(request: Request, window: int = 5):
    """The dependency map. Served from Redis; falls back to a live query.

    The cached path is the normal one and is a sub-2ms hit. The fallback exists
    so a cold start still renders instead of showing an empty canvas while the
    builder waits out its first interval.
    """
    cache = request.app.state.cache
    graph = await cache.get_graph()
    if graph is not None:
        graph["source"] = "cache"
        return graph

    engine = request.app.state.engine
    graph = await engine.graph(window)
    graph.update({"computedAt": None, "ageSeconds": 0.0,
                  "stale": False, "source": "live"})
    return graph


@router.get("/api/blame")
async def get_blame(request: Request, window: int = 5):
    return {"window": window, "edges": await request.app.state.engine.blame(window)}


@router.get("/api/edges/{src}/{dst}")
async def get_edge(request: Request, src: str, dst: str, window: int = 60):
    engine = request.app.state.engine
    series = await engine.edge_series(src, dst, window)
    traces = await engine.slow_traces(src, dst, min(window, 15))
    if not series and not traces:
        raise HTTPException(status_code=404, detail="edge_not_found")
    return {"source": src, "target": dst, "series": series, "slowTraces": traces}


@router.get("/healthz")
async def healthz():
    """Process liveness ONLY - no dependency checks (M17)."""
    return {"status": "ok", "service": "analytics-api"}


@router.get("/readyz")
async def readyz(request: Request):
    """Dependencies checked HERE."""
    ch_ok = await request.app.state.ch.ping()
    redis_ok = await request.app.state.cache.ping()
    if not (ch_ok and redis_ok):
        raise HTTPException(
            status_code=503,
            detail={"clickhouse": ch_ok, "redis": redis_ok},
        )
    return {"status": "ready"}
