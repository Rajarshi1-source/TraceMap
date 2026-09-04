"""HTTP surface for the dashboard."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/graph")
async def get_graph(request: Request, window: int = 5):
    """The dependency map. Three sources, tried in order, and none of them lie.

        cache      graph:current, under 60s old. The normal path, sub-2ms.
        live       a direct query. Covers a cold start, before the builder's
                   first interval has elapsed.
        last-good  graph:lastgood, up to an hour old, flagged degraded.

    F40, and the ordering is a measured correction rather than a design
    flourish. Chaos experiment 001 killed ClickHouse under live load; this
    endpoint served honest stale data for thirty seconds and then returned HTTP
    500 for the remaining hundred seconds of the outage, because `graph:current`
    had expired and the live fallback had nothing to talk to. The staleness
    banner is the feature that exists so degraded data beats no data (M26), and
    it could not survive the one scenario it was built for.

    Last-good is returned with HTTP 200, deliberately. M26 says never display
    stale data WITHOUT the banner -- not never display it. A 503 here would be
    more literal and less useful: the dashboard would render nothing, and an
    operator mid-incident would lose the last known shape of their system at
    the moment they most want it. So: 200, `stale: true`, `degraded: true`, and
    the real age in seconds.

    503 is reserved for having genuinely nothing -- no cache, no database, no
    last-good -- and it says which of the three failed.
    """
    cache = request.app.state.cache
    graph = await cache.get_graph()
    if graph is not None:
        graph["source"] = "cache"
        return graph

    try:
        graph = await request.app.state.engine.graph(window)
    except Exception as exc:
        # Deliberately broad: every way ClickHouse can be unreachable has the
        # same answer here, which is to fall back rather than 500. Enumerating
        # httpx's error tree would arrive at this same handler by a longer road.
        # (No BLE001 suppression needed -- `exc` is re-raised on the 503 path,
        # so ruff does not treat this as a blind catch.)
        log.warning("live graph query failed, falling back to last-good: %s", exc)
        graph = await cache.get_last_good()
        if graph is None:
            raise HTTPException(
                status_code=503,
                detail={"error": "no_graph_available",
                        "cache": "expired", "live": exc.__class__.__name__,
                        "lastGood": "absent",
                        "note": "analytics tier is down and nothing was cached "
                                "within the last hour"},
            ) from exc
        graph["source"] = "last-good"
        return graph

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


@router.get("/api/anomalies")
async def get_anomalies(request: Request, limit: int = 50):
    """Anomaly history from Postgres.

    Every row carries `correlated_deploy`, including the {"found": false} case -
    "no deploy correlated" is a real result that points the investigation away
    from the changelog, and omitting it would make the record ambiguous.
    """
    pg = getattr(request.app.state, "pg", None)
    if pg is None:
        return {"anomalies": [], "note": "postgres unavailable"}
    rows = await pg.fetch(
        """
        SELECT id, detected_at, src_service, dst_service, metric,
               baseline_ms, observed_ms, delta_ms, request_count,
               sample_trace_ids, correlated_deploy, resolved_at
        FROM anomaly_events
        ORDER BY detected_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    import json as _json
    return {"anomalies": [
        {
            "id": str(r["id"]),
            "detectedAt": r["detected_at"].isoformat(),
            "source": r["src_service"],
            "target": r["dst_service"],
            "metric": r["metric"],
            "baselineMs": float(r["baseline_ms"]),
            "observedMs": float(r["observed_ms"]),
            "deltaMs": float(r["delta_ms"]),
            "requestCount": int(r["request_count"]),
            "sampleTraceIds": list(r["sample_trace_ids"] or []),
            "deploy": (r["correlated_deploy"] if isinstance(r["correlated_deploy"], dict) else _json.loads(r["correlated_deploy"])) if r["correlated_deploy"] else {"found": False},
            "resolvedAt": r["resolved_at"].isoformat() if r["resolved_at"] else None,
        }
        for r in rows
    ]}


@router.get("/api/deploys")
async def get_deploys(request: Request, limit: int = 20):
    rows = await request.app.state.ch.query(
        "SELECT service_name, revision, git_sha, toString(deployed_at) AS deployed_at,"
        " trigger FROM tracemap.deploy_markers ORDER BY deployed_at DESC LIMIT {limit}",
        {"limit": limit},
    )
    return {"deploys": rows}


@router.get("/api/stream/alerts")
async def stream_alerts(request: Request):
    """Server-sent events over a Redis pub/sub channel.

    SSE rather than WebSockets: this is one-directional, it survives proxies
    that mangle upgrades, and the browser reconnects on its own. A WebSocket
    would be more machinery for no extra capability.
    """
    from src.core.alert_manager import ALERT_CHANNEL

    async def events():
        pubsub = request.app.state.cache.redis.pubsub()
        await pubsub.subscribe(ALERT_CHANNEL)
        try:
            # Comment frame first: it opens the stream immediately so a proxy
            # cannot sit on the response waiting for bytes that may be minutes away.
            yield ": connected\n\n"
            async for msg in pubsub.listen():
                if await request.is_disconnected():
                    break
                if msg.get("type") == "message":
                    yield f"data: {msg['data']}\n\n"
        finally:
            await pubsub.unsubscribe(ALERT_CHANNEL)
            await pubsub.aclose()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.get("/api/slo")
async def get_slo(request: Request):
    """D6 - error budgets on CRITICAL-PATH latency, per dependency edge.

    The one endpoint here that makes a claim no other tool makes: an edge can
    be 700ms of wall clock and burn zero budget, because those 700ms ran
    alongside a slower sibling and cost the user nothing. Each row carries
    `wallClockBad` next to `sli` so the contrast is visible rather than
    asserted.
    """
    return await request.app.state.slo.evaluate()


@router.get("/api/cost")
async def get_cost(request: Request):
    """D5 - the monthly bill, from ClickHouse's own accounting.

    Every figure is labelled by how it was obtained: bytes per row are measured
    from system.parts, steady state is projected, prices are configuration. A
    cost model that does not say which of its numbers were observed is a
    spreadsheet with extra steps.
    """
    return await request.app.state.cost.report()


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
