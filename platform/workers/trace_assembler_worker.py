"""trace-assembler: stitch parent->child edges and compute critical paths.

Runs every 15s. Reads traces that have gone quiet for LATE_SPAN_GRACE_S, and
emits BOTH derived tables in a single pass.

Doing both in one pass is the efficiency insight behind the whole design:
stitching edges and extracting the critical path need the same grouping - all
spans of one trace - so the flagship differentiator rides along on work the
pipeline has to do anyway.

This worker exists because stitching is a stateful, trace-scoped operation. A
materialized-view self-join would run per insert block and silently drop every
edge whose parent and child landed in different batches (ADR 0003).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta

from src.core.critical_path import Span, attribute_to_edges, compute_critical_path
from src.integrations.clickhouse_client import ClickHouseClient
from workers._runner import run_forever, serve_metrics

log = logging.getLogger(__name__)

ASSEMBLE_INTERVAL_S = int(os.environ.get("ASSEMBLE_INTERVAL_S", "15"))

# Wait this long past a trace's last span before assembling it. Too short and
# late spans arrive after the edges are written; too long and the map goes
# stale. 30s is the documented consistency budget.
LATE_SPAN_GRACE_S = int(os.environ.get("LATE_SPAN_GRACE_S", "30"))

LOOKBACK_MINUTES = int(os.environ.get("ASSEMBLE_LOOKBACK_MINUTES", "10"))

# ---------------------------------------------------------------------------
# Exactly-once processing, via a watermark on trace completion time.
#
# Without this the same trace is assembled by consecutive cycles: a trace whose
# last span lands at T is "complete" from T+grace onward, so every cycle in that
# range picks it up again. Measured before the fix: 24 of 40 distinct edges had
# been written twice.
#
# Duplicate edge rows are not cosmetic. trace_edges feeds an incremental MV, so
# every duplicate inflates request_count in service_edges_1m -- which would
# quietly undo the F1 fix that phase 3 exists to guarantee. A ReplacingMergeTree
# would not help either: its dedup happens at merge time, long after the MV has
# already fired on both inserts.
#
# The watermark is in-process. On restart it resets to now()-lookback, so a
# crash can reprocess up to LOOKBACK_MINUTES of traces once. That is a bounded,
# deliberate trade: persisting it would need another store on the write path,
# and Phase 7 has the eval harness to catch it if it ever matters.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# F2 - dedupe by span_id at the fetch boundary.
#
# Span ingestion is at-least-once and the project deliberately ACCEPTS duplicate
# spans in raw storage, because dedup costs merge CPU forever and a duplicate
# moves a p99 imperceptibly. That reasoning does not survive into the assembler:
#
#   * `by_id` is a dict keyed on span_id, so a duplicate silently REPLACES a
#     span's service and timings - blame moves to a different edge, and the
#     original edge can vanish from the output entirely.
#   * the edge loop iterates spans, so a duplicate emits TWO rows into
#     trace_edges, double-counting the edge and feeding the TDigest twice.
#
# argMax(..., timestamp) keeps the most recently written copy of each span.
# ---------------------------------------------------------------------------
FETCH_SQL = """
SELECT
    trace_id,
    span_id,
    argMax(parent_span_id,        timestamp) AS parent_span_id,
    argMax(service_name,          timestamp) AS service_name,
    argMax(operation_name,        timestamp) AS operation_name,
    argMax(start_time_unix_ns,    timestamp) AS start_time_unix_ns,
    argMax(end_time_unix_ns,      timestamp) AS end_time_unix_ns,
    argMax(status_code = 'ERROR', timestamp) AS is_error,
    argMax(service_version,       timestamp) AS service_version,
    argMax(span_kind,             timestamp) AS span_kind,
    -- toString, not formatDateTime: ClickHouse reads %M as the full month
    -- NAME (MySQL compatibility), so '%H:%M:%S' yields '10:September:30' and
    -- the value fails to parse on the way back in.
    toString(max(ingested_at)) AS last_seen
FROM tracemap.spans
WHERE timestamp >= now() - INTERVAL {lookback} MINUTE
  AND trace_id IN (
      SELECT trace_id
      FROM tracemap.spans
      WHERE timestamp >= now() - INTERVAL {lookback} MINUTE
      GROUP BY trace_id
      -- Grace is measured on INGESTION time: "no new span for this trace has
      -- arrived in `grace` seconds". Measuring it on span time declares a trace
      -- complete while a later batch is still in flight, and the assembler then
      -- attributes a critical path over a partial tree -- which shows up as
      -- edges with 0.0 blame that should have had some.
      HAVING max(ingested_at) <  now() - INTERVAL {grace} SECOND
         AND max(ingested_at) >  parseDateTime64BestEffort('{watermark}')
  )
GROUP BY trace_id, span_id
ORDER BY trace_id, start_time_unix_ns
"""


def rows_to_traces(rows: list[dict]) -> dict[str, list[Span]]:
    """Group fetched rows into per-trace span lists.

    Second line of defence for F2: the SQL already collapses duplicates, but a
    dict keyed on span_id here means a future change to that query cannot
    quietly reintroduce double-counting. The invariant is cheap to hold and
    expensive to lose.
    """
    by_trace: dict[str, dict[str, Span]] = {}
    for r in rows:
        span = Span(
            span_id=r["span_id"],
            parent_span_id=r.get("parent_span_id") or "",
            service=r["service_name"],
            operation=r["operation_name"],
            start_ns=int(r["start_time_unix_ns"]),
            end_ns=int(r["end_time_unix_ns"]),
            is_error=bool(int(r.get("is_error") or 0)),
            version=r.get("service_version") or "",
        )
        by_trace.setdefault(r["trace_id"], {})[span.span_id] = span
    return {tid: list(spans.values()) for tid, spans in by_trace.items()}


def normalize_spans(spans: list[Span]) -> list[Span]:
    """Clamp every span inside its parent's window.

    Real traces violate the "a child cannot outlive its parent" invariant that
    the critical-path walk assumes. Express closes its `request handler` span a
    tick AFTER the HTTP server span that contains it, so the child ends ~0.2ms
    late. Observed on the actual demo path:

        order-svc POST (SERVER)              [ 5.0 .. 122.6]
          order-svc request handler /orders  [ 6.0 .. 122.8]   <-- 0.2ms late

    _walk skips any child ending after the cursor, because that is how it
    identifies parallel work. A child that overruns its parent therefore takes
    its ENTIRE SUBTREE with it - here that meant payment, product and user all
    silently scored 0.0 blame, on the one trace the whole project is about.
    Nothing errors; the numbers just quietly become wrong.

    This is normalisation of malformed input, NOT a change to the algorithm.
    critical_path.py stays verbatim and its six tests stay green; the fix
    belongs here because the defect is in the data, and a span that outlives its
    own parent is not a fact about the system - it is an artifact of how two
    instrumentation layers round their clocks.
    """
    by_id = {s.span_id: s for s in spans}

    depth: dict[str, int] = {}

    def _depth(sid: str, seen: frozenset[str] = frozenset()) -> int:
        if sid in depth:
            return depth[sid]
        span = by_id[sid]
        parent = span.parent_span_id
        d = 0 if parent not in by_id or parent in seen else _depth(parent, seen | {sid}) + 1
        depth[sid] = d
        return d

    fixed: dict[str, Span] = {}
    clamped = 0
    for sid in sorted(by_id, key=_depth):      # parents settle before children
        span = by_id[sid]
        parent = fixed.get(span.parent_span_id)
        if parent is None:
            fixed[sid] = span
            continue
        start = max(span.start_ns, parent.start_ns)
        end = min(span.end_ns, parent.end_ns)
        if end < start:                        # degenerate; collapse to a point
            end = start
        if start != span.start_ns or end != span.end_ns:
            clamped += 1
            span = Span(span.span_id, span.parent_span_id, span.service,
                        span.operation, start, end, span.is_error, span.version)
        fixed[sid] = span

    if clamped:
        log.debug("clamped %d span(s) inside their parent window", clamped)
    return list(fixed.values())


def assemble_trace(trace_id: str, spans: list[Span]) -> tuple[list[dict], list[dict]]:
    """Turn one trace into its edge rows and its critical-path row."""
    # F3 - attribute_to_edges recurses in _depth() at the same tree depth, so it
    # belongs INSIDE the guard, not after it. A trace deep enough to blow the
    # stack must skip cleanly rather than kill the cycle.
    spans = normalize_spans(spans)
    try:
        segments = compute_critical_path(spans)
        if not segments:
            return [], []
        crit_by_edge = attribute_to_edges(spans, segments)
    except RecursionError:
        log.warning("trace %s too deep; skipping", trace_id)
        return [], []

    by_id = {s.span_id: s for s in spans}
    bucket = min(s.start_ns for s in spans) // 1_000_000_000

    edge_rows: list[dict] = []
    for s in spans:
        parent = by_id.get(s.parent_span_id)
        if not parent or parent.service == s.service:
            continue
        edge_rows.append(
            {
                "time_bucket": bucket,
                "trace_id": trace_id,
                "source_service": parent.service,
                "target_service": s.service,
                "source_operation": parent.operation,
                "target_operation": s.operation,
                "child_span_id": s.span_id,
                "duration_ms": s.duration_ms,
                # M5: a parallel sibling gets 0.0 and the row is STILL written.
                # The map needs it to fade the edge out rather than drop it.
                "critical_ms": crit_by_edge.get((parent.service, s.service), 0.0),
                "is_error": int(s.is_error),
                "target_version": s.version,
            }
        )

    root = segments[0]
    path_rows = [
        {
            "time_bucket": bucket,
            "trace_id": trace_id,
            "root_service": root.service,
            "root_operation": root.operation,
            "total_duration_ms": sum(seg.ms for seg in segments),
            "critical_path": [[s.service, s.operation, s.ms] for s in segments],
            "path_length": len(segments),
        }
    ]
    return edge_rows, path_rows


class Watermark:
    """Highest trace-completion time already assembled."""

    def __init__(self) -> None:
        self.value: str | None = None

    def sql(self) -> str:
        if self.value:
            return self.value
        return (
            datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=LOOKBACK_MINUTES)
        ).strftime("%Y-%m-%d %H:%M:%S")

    def advance(self, to: str) -> None:
        if self.value is None or to > self.value:
            self.value = to


async def assemble_once(ch: ClickHouseClient, wm: Watermark) -> int:
    """One assembly cycle. Returns the number of traces assembled."""
    rows = await ch.query(
        FETCH_SQL,
        {
            "grace": LATE_SPAN_GRACE_S,
            "lookback": LOOKBACK_MINUTES,
            "watermark": wm.sql(),
        },
    )
    traces = rows_to_traces(rows)
    if not traces:
        return 0

    # Advance past everything just read, so the next cycle cannot see it again.
    newest = max(r["last_seen"] for r in rows)
    wm.advance(newest)

    edge_rows: list[dict] = []
    path_rows: list[dict] = []
    for trace_id, spans in traces.items():
        e, p = assemble_trace(trace_id, spans)
        edge_rows.extend(e)
        path_rows.extend(p)

    if edge_rows:
        await ch.insert("tracemap.trace_edges", edge_rows)
    if path_rows:
        await ch.insert("tracemap.trace_critical_path", path_rows)

    log.info(
        "assembled %d traces -> %d edges, %d paths",
        len(traces), len(edge_rows), len(path_rows),
    )
    return len(traces)


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format='{"level":"%(levelname)s","worker":"trace-assembler","msg":"%(message)s"}',
    )
    # httpx logs every request at INFO, and our credentials ride in the query
    # string, so the password would land in container logs on every cycle.
    # Logs are read by more people than the database is.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    serve_metrics(int(os.environ.get("METRICS_PORT", "9100")))

    ch = ClickHouseClient()
    for _ in range(60):
        if await ch.ping():
            break
        log.info("waiting for clickhouse")
        await asyncio.sleep(2)

    wm = Watermark()
    await run_forever(
        "trace_assembler",
        lambda: assemble_once(ch, wm),
        ASSEMBLE_INTERVAL_S,
    )


if __name__ == "__main__":
    asyncio.run(main())
