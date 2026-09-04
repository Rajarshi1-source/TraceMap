"""The production queries behind the dependency map.

Every one reads a pre-aggregated roll-up, never raw spans. `service_edges_1m`
stores TDigest *states*, so a 5-minute, 1-hour or 24-hour window is the same
query over a different range of rows - percentiles merge, they do not average.

Q1 ranks by crit_p99 rather than wall_p99 on purpose: the map should order edges
by what actually hurts users, not by what merely looks slow.
"""

from __future__ import annotations

from src.integrations.clickhouse_client import ClickHouseClient

# ---------------------------------------------------------------------------
# Q1 - the dependency graph, dual-metric. THE dashboard query.
# ---------------------------------------------------------------------------
Q_GRAPH = """
SELECT
    source_service AS source,
    target_service AS target,
    sum(request_count)                          AS requests,
    sum(error_count)                            AS errors,
    sum(error_count) / nullIf(sum(request_count), 0) AS error_rate,
    quantileTDigestMerge(0.50)(wall_p50_state)  AS wall_p50,
    quantileTDigestMerge(0.95)(wall_p95_state)  AS wall_p95,
    quantileTDigestMerge(0.99)(wall_p99_state)  AS wall_p99,
    quantileTDigestMerge(0.99)(crit_p99_state)  AS crit_p99,
    sum(crit_ms_sum)                            AS crit_total_ms,
    sum(crit_ms_sum) / nullIf(sum(request_count), 0) AS crit_mean_ms
FROM tracemap.service_edges_1m
WHERE time_bucket >= now() - INTERVAL {window} MINUTE
GROUP BY source, target
HAVING requests > 0
ORDER BY crit_p99 DESC
"""

# Service-level RED, for node size and colour.
Q_SERVICES = """
SELECT
    service_name,
    sum(request_count)                          AS requests,
    sum(error_count)                            AS errors,
    sum(error_count) / nullIf(sum(request_count), 0) AS error_rate,
    quantileTDigestMerge(0.99)(p99_state)       AS p99
FROM tracemap.service_red_1m
WHERE time_bucket >= now() - INTERVAL {window} MINUTE
GROUP BY service_name
"""

# ---------------------------------------------------------------------------
# Q2 - blame ranking: each edge's share of total user-visible latency.
# ---------------------------------------------------------------------------
Q_BLAME = """
SELECT
    source_service, target_service,
    sum(crit_ms_sum) AS total_critical_ms,
    sum(crit_ms_sum) / nullIf((SELECT sum(crit_ms_sum)
                               FROM tracemap.service_edges_1m
                               WHERE time_bucket >= now() - INTERVAL {window} MINUTE), 0)
        AS blame_share
FROM tracemap.service_edges_1m
WHERE time_bucket >= now() - INTERVAL {window} MINUTE
GROUP BY source_service, target_service
ORDER BY blame_share DESC
LIMIT 20
"""

# ---------------------------------------------------------------------------
# Q3 - sample slow traces for one edge.
#
# Ordered by critical_ms, NOT duration_ms. Order by wall clock and the alert
# link lands on a trace where the edge was slow but parallel - the person who
# clicks through sees nothing wrong and stops trusting the alert.
# ---------------------------------------------------------------------------
Q_SLOW_TRACES = """
SELECT trace_id, duration_ms, critical_ms, target_version, is_error
FROM tracemap.trace_edges
WHERE time_bucket >= now() - INTERVAL {window} MINUTE
  AND source_service = '{src}'
  AND target_service = '{dst}'
ORDER BY critical_ms DESC
LIMIT {limit}
"""

Q_EDGE_SERIES = """
SELECT
    toStartOfMinute(time_bucket)                AS ts,
    sum(request_count)                          AS requests,
    sum(error_count)                            AS errors,
    quantileTDigestMerge(0.99)(wall_p99_state)  AS wall_p99,
    quantileTDigestMerge(0.99)(crit_p99_state)  AS crit_p99,
    -- Total critical-path milliseconds this edge owns in the bucket. Divided
    -- by the bucket's total below, it is the edge's BLAME SHARE, and F37 is
    -- the reason the detector needs it: a p99 says how slow an edge was on its
    -- worst 1% of traces, and says nothing about whether the edge mattered.
    sum(crit_ms_sum)                            AS crit_ms_total
FROM tracemap.service_edges_1m
WHERE time_bucket >= now() - INTERVAL {window} MINUTE
  AND source_service = '{src}'
  AND target_service = '{dst}'
GROUP BY ts
ORDER BY ts
"""


# ---------------------------------------------------------------------------
# Detector view: ONE minute-bucket, not a rolling window.
#
# A rolling window is right for the dashboard and wrong for the detector. Over
# 5 minutes a p99 blends pre-fault and post-fault requests, so an injected step
# arrives as a RAMP - each cycle's delta lands under the threshold and the EWMA
# legitimately learns it, until the baseline has chased the fault into place
# without a single cycle ever breaching. Measured: a 60ms injection never fired.
#
# Narrowing the window to 1 minute does not fix it either, and measuring says
# why: the pipeline lag from trace to roll-up is ~54s, so `now() - 1 MINUTE`
# excludes the newest bucket entirely and the detector reads nan.
#
# A single minute-bucket is HOMOGENEOUS IN TIME - every request in it shares one
# fault state - so it cannot dilute. Taking the newest bucket that exists gives
# a clean step response and rides whatever the current lag happens to be,
# instead of encoding an assumption about it.
Q_LATEST_BUCKET = """
SELECT
    source_service AS source,
    target_service AS target,
    toString(time_bucket)                       AS bucket,
    sum(request_count)                          AS requests,
    sum(error_count)                            AS errors,
    sum(error_count) / nullIf(sum(request_count), 0) AS error_rate,
    quantileTDigestMerge(0.99)(wall_p99_state)  AS wall_p99,
    quantileTDigestMerge(0.99)(crit_p99_state)  AS crit_p99,
    -- Total critical-path milliseconds this edge owns in the bucket. Divided
    -- by the bucket's total below, it is the edge's BLAME SHARE, and F37 is
    -- the reason the detector needs it: a p99 says how slow an edge was on its
    -- worst 1% of traces, and says nothing about whether the edge mattered.
    sum(crit_ms_sum)                            AS crit_ms_total
FROM tracemap.service_edges_1m
WHERE time_bucket = (
    SELECT max(time_bucket) FROM tracemap.service_edges_1m
    WHERE time_bucket >= now() - INTERVAL 15 MINUTE
)
GROUP BY source, target, time_bucket
HAVING requests > 0
"""


def _f(v, default: float = 0.0) -> float:
    return default if v is None else float(v)


class QueryEngine:
    def __init__(self, ch: ClickHouseClient) -> None:
        self.ch = ch

    async def graph(self, window_min: int = 5) -> dict:
        """Both metrics for every edge, plus per-service RED."""
        edges_raw = await self.ch.query(Q_GRAPH, {"window": window_min})
        svc_raw = await self.ch.query(Q_SERVICES, {"window": window_min})

        total_crit = sum(_f(e.get("crit_total_ms")) for e in edges_raw) or 1.0

        edges = [
            {
                "source": e["source"],
                "target": e["target"],
                "requestCount": int(e["requests"]),
                "errorRate": round(_f(e.get("error_rate")), 4),
                "wallP50": round(_f(e.get("wall_p50")), 2),
                "wallP95": round(_f(e.get("wall_p95")), 2),
                "wallP99": round(_f(e.get("wall_p99")), 2),
                "critP99": round(_f(e.get("crit_p99")), 2),
                "critMeanMs": round(_f(e.get("crit_mean_ms")), 2),
                # 0..1, drives edge opacity in critical-path mode. This is the
                # channel that makes a parallel edge fade instead of shouting.
                "blameShare": round(_f(e.get("crit_total_ms")) / total_crit, 4),
            }
            for e in edges_raw
        ]

        by_svc = {s["service_name"]: s for s in svc_raw}
        names = {e["source"] for e in edges} | {e["target"] for e in edges} | set(by_svc)
        nodes = [
            {
                "service": n,
                "requestCount": int(by_svc.get(n, {}).get("requests") or 0),
                "errorRate": round(_f((by_svc.get(n) or {}).get("error_rate")), 4),
                "p99": round(_f((by_svc.get(n) or {}).get("p99")), 2),
                "deployedRecently": False,   # wired in Phase 6
            }
            for n in sorted(names)
        ]
        return {"nodes": nodes, "edges": edges}

    async def latest_bucket_edges(self) -> list[dict]:
        """Per-edge metrics for the newest minute-bucket. Detector input."""
        rows = await self.ch.query(Q_LATEST_BUCKET)

        # Blame share is a property of the BUCKET, not of a row, so it is
        # computed here across the edges the query returned rather than in SQL.
        # Same single-table read either way; this keeps the window function out
        # of a query the detector runs every cycle.
        total_crit = sum(_f(r.get("crit_ms_total")) for r in rows)

        return [
            {
                "source": r["source"],
                "target": r["target"],
                "bucket": r["bucket"],
                "requestCount": int(r["requests"]),
                "errorRate": round(_f(r.get("error_rate")), 4),
                "wallP99": round(_f(r.get("wall_p99")), 2),
                "critP99": round(_f(r.get("crit_p99")), 2),
                # 0.0 when the bucket carried no critical time at all, which is
                # the honest answer: with nothing on the critical path, no edge
                # owns a share of it.
                "blameShare": round(
                    _f(r.get("crit_ms_total")) / total_crit, 4
                ) if total_crit > 0 else 0.0,
            }
            for r in rows
        ]

    async def blame(self, window_min: int = 5) -> list[dict]:
        rows = await self.ch.query(Q_BLAME, {"window": window_min})
        return [
            {
                "source": r["source_service"],
                "target": r["target_service"],
                "totalCriticalMs": round(_f(r.get("total_critical_ms")), 2),
                "blameShare": round(_f(r.get("blame_share")), 4),
            }
            for r in rows
        ]

    async def slow_traces(self, src: str, dst: str, window_min: int = 5, limit: int = 5) -> list[dict]:
        rows = await self.ch.query(
            Q_SLOW_TRACES,
            {"window": window_min, "src": src, "dst": dst, "limit": limit},
        )
        return [
            {
                "traceId": r["trace_id"],
                "durationMs": round(_f(r.get("duration_ms")), 2),
                "criticalMs": round(_f(r.get("critical_ms")), 2),
                "targetVersion": r.get("target_version") or "",
                "isError": bool(int(r.get("is_error") or 0)),
            }
            for r in rows
        ]

    async def edge_series(self, src: str, dst: str, window_min: int = 60) -> list[dict]:
        rows = await self.ch.query(
            Q_EDGE_SERIES, {"window": window_min, "src": src, "dst": dst}
        )
        return [
            {
                "ts": r["ts"],
                "requestCount": int(r["requests"]),
                "errorCount": int(r["errors"]),
                "wallP99": round(_f(r.get("wall_p99")), 2),
                "critP99": round(_f(r.get("crit_p99")), 2),
            }
            for r in rows
        ]
