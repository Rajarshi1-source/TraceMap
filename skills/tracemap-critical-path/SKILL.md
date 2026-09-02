---
name: tracemap-critical-path
description: Implement and maintain TraceMap's flagship differentiator — critical-path latency attribution. Covers the span-tree backward walk, per-edge blame attribution, the trace-assembler worker that stitches parent-child edges and computes critical paths in a single pass, the six-test invariant suite, and the four documented limitations (sampling bias, async spans, missing spans, clock skew). Use for ANY work touching critical path, blame attribution, latency contribution, span-tree traversal, the trace assembler, or trace_edges and trace_critical_path population — and also when the user says "which service is actually slow", "the map blames the wrong service", "parallel calls look red", or "attribute latency per hop". MANDATE - wall-clock duration is NOT contribution; work that overlaps the critical path contributes zero user-visible latency however slow it looks, and parallel edges are written with critical_ms of 0.0 rather than omitted. Pair with tracemap-clickhouse-analytics for the tables this writes.
---

# TraceMap — Critical-Path Latency Attribution

This is the feature that makes the project better than SigNoz, Coroot, Kiali or Tempo's service graph. Those all answer *which edge is slow*. This answers *which edge is slow **and on the critical path***, which is the question that determines whether fixing it helps a user.

## The idea in one example

`order-svc` fans out to three services concurrently:

```
order-svc            |------------------------------------- 730ms --|
  |- user-svc        |-- 40ms --|
  |- product-svc     |-------------------- 700ms ---------------|
  |- payment-svc     |---------------------- 720ms ----------------|
```

A conventional map paints both `order->product` (700ms) and `order->payment` (720ms) red. But making product-svc infinitely fast saves the user **zero milliseconds** — payment finishes last and defines the response time. Critical-path attribution reports `order->payment = 720ms` and `order->product = 0ms`, and the ambiguity disappears.

## The algorithm

Walk the span tree **backwards** from the root's end time. At each level take the child that finished last among those ending before the current cursor. The gap between that child's end and the cursor is the parent's own work. Recurse into the child, then move the cursor to the child's start. Any sibling ending after the new cursor overlapped it — parallel work — and is skipped.

Complexity is `O(n log n)`, dominated by sorting children by end time.

```python
# platform/src/core/critical_path.py
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class Span:
    span_id: str
    parent_span_id: str
    service: str
    operation: str
    start_ns: int
    end_ns: int
    is_error: bool = False
    version: str = ""

    @property
    def duration_ms(self) -> float:
        return (self.end_ns - self.start_ns) / 1_000_000


@dataclass(slots=True, frozen=True)
class Segment:
    """A contiguous slice of the critical path owned by exactly one span."""
    span_id: str
    service: str
    operation: str
    start_ns: int
    end_ns: int

    @property
    def ms(self) -> float:
        return (self.end_ns - self.start_ns) / 1_000_000


def compute_critical_path(spans: list[Span]) -> list[Segment]:
    if not spans:
        return []

    by_id: dict[str, Span] = {s.span_id: s for s in spans}
    children: dict[str, list[Span]] = defaultdict(list)
    for s in spans:
        if s.parent_span_id and s.parent_span_id in by_id:
            children[s.parent_span_id].append(s)

    # Orphan-tolerant: sampling or loss can remove the true root. A partial
    # answer beats a blank map, so take the earliest orphan.
    roots = [s for s in spans
             if not s.parent_span_id or s.parent_span_id not in by_id]
    if not roots:
        return []                       # cycle or corrupt trace: refuse to guess
    root = min(roots, key=lambda s: s.start_ns)

    segments: list[Segment] = []
    _walk(root, root.end_ns, children, segments)
    segments.reverse()                  # produced backwards; return chronological
    return segments


def _walk(span: Span, cursor_ns: int,
          children: dict[str, list[Span]], out: list[Segment]) -> None:
    kids = sorted(children.get(span.span_id, []),
                  key=lambda c: c.end_ns, reverse=True)

    for kid in kids:
        # Anything overlapping the already-attributed region ran in parallel
        # and is NOT on the critical path.
        if kid.end_ns > cursor_ns or kid.end_ns <= span.start_ns:
            continue

        if cursor_ns > kid.end_ns:      # gap = the parent's own work
            out.append(Segment(span.span_id, span.service, span.operation,
                               kid.end_ns, cursor_ns))

        _walk(kid, kid.end_ns, children, out)
        cursor_ns = max(kid.start_ns, span.start_ns)

        if cursor_ns <= span.start_ns:
            return

    if cursor_ns > span.start_ns:
        out.append(Segment(span.span_id, span.service, span.operation,
                           span.start_ns, cursor_ns))


def attribute_to_edges(spans: list[Span],
                       segments: list[Segment]) -> dict[tuple[str, str], float]:
    """An edge's contribution is the critical-path time spent anywhere inside
    the callee's subtree — its own work plus everything it waited on."""
    by_id = {s.span_id: s for s in spans}

    own_ms: dict[str, float] = defaultdict(float)
    for seg in segments:
        own_ms[seg.span_id] += seg.ms

    depth: dict[str, int] = {}

    def _depth(sid: str) -> int:
        if sid in depth:
            return depth[sid]
        span = by_id[sid]
        d = 0 if span.parent_span_id not in by_id else _depth(span.parent_span_id) + 1
        depth[sid] = d
        return d

    subtree_ms: dict[str, float] = dict(own_ms)
    for sid in sorted(by_id, key=_depth, reverse=True):
        span = by_id[sid]
        if span.parent_span_id in by_id:
            subtree_ms[span.parent_span_id] = (
                subtree_ms.get(span.parent_span_id, 0.0) + subtree_ms.get(sid, 0.0))

    edges: dict[tuple[str, str], float] = defaultdict(float)
    for sid, span in by_id.items():
        parent = by_id.get(span.parent_span_id)
        if parent and parent.service != span.service:
            edges[(parent.service, span.service)] += subtree_ms.get(sid, 0.0)
    return dict(edges)
```

## Semantics that trip people up

**Parallel edges are kept with `0.0`, not dropped.** `attribute_to_edges` returns a key for every cross-service edge, including zero-blame ones. The map still needs to draw an edge that has real traffic, real wall-clock latency and a real error rate — it just has no blame. A zero row is what lets the UI fade it out instead of making it disappear. Any test asserting `("a","b") not in edges` for a parallel sibling is wrong; assert `== 0.0`.

**A parent edge absorbs its whole subtree.** In the fan-out example `gateway->order` is 735ms: order's own 15ms of work plus payment's 720ms. That is intended — the gateway really did wait 735ms on order.

**Segments are contiguous and non-overlapping**, so they sum exactly to the root duration. That invariant is the cheapest possible regression test on the walk.

## Test suite

These six tests were executed against the implementation above and all pass. Reproduce them verbatim.

| Test | Asserts |
|---|---|
| `test_parallel_fanout_blames_only_the_slowest_sibling` | payment 720.0, product 0.0, user 0.0, gateway->order 735.0 |
| `test_critical_path_covers_the_root_span_exactly` | segment sum equals root duration (the walk invariant) |
| `test_serial_chain_attributes_every_hop` | gateway->order 280.0, order->payment 260.0 |
| `test_sequential_siblings_both_count` | non-overlapping siblings both get ~140.0 |
| `test_partial_overlap_excludes_the_overlapped_sibling` | b 190.0, a 0.0 |
| `test_orphan_root_survives_a_dropped_parent` | order->payment 720.0 with the root missing |

```python
MS = 1_000_000

def test_parallel_fanout_blames_only_the_slowest_sibling():
    spans = [
        Span("root", "",     "gateway-svc", "POST /checkout", 0,       750 * MS),
        Span("ord",  "root", "order-svc",   "createOrder",    5  * MS, 740 * MS),
        Span("usr",  "ord",  "user-svc",    "getUser",        10 * MS, 50  * MS),
        Span("prd",  "ord",  "product-svc", "checkStock",     10 * MS, 710 * MS),
        Span("pay",  "ord",  "payment-svc", "charge",         10 * MS, 730 * MS),
    ]
    edges = attribute_to_edges(spans, compute_critical_path(spans))
    assert edges[("order-svc", "payment-svc")] == 720.0
    assert edges[("order-svc", "product-svc")] == 0.0   # 700ms wall clock, 0ms blame
    assert edges[("order-svc", "user-svc")]    == 0.0
    assert edges[("gateway-svc", "order-svc")] == 735.0
```

Write the parallel-fanout test **before** the worker. It is the specification, and it belongs in the README — a reader who sees one test in the repo should see that one.

## The assembler worker

Stitching parent-to-child edges and computing critical paths need the same grouping — all spans of one trace. Doing both in one pass is why the flagship feature costs almost nothing extra: the grouping is work the pipeline must do anyway to avoid the materialized-view join trap.

```python
# platform/workers/trace_assembler_worker.py  (shape)
ASSEMBLE_INTERVAL_S = 15
LATE_SPAN_GRACE_S   = 30    # wait past a trace's last span before assembling

async def assemble_once(ch) -> int:
    rows = await ch.query(FETCH_COMPLETED_TRACES_SQL, {"grace": LATE_SPAN_GRACE_S})

    traces: dict[str, list[Span]] = {}
    for r in rows:
        traces.setdefault(r["trace_id"], []).append(Span(...))

    edge_rows, path_rows = [], []
    for trace_id, spans in traces.items():
        try:
            segments = compute_critical_path(spans)
            if not segments:
                continue
            # attribute_to_edges recurses in _depth() at the same tree depth,
            # so it belongs INSIDE the guard, not after it.
            crit_by_edge = attribute_to_edges(spans, segments)
        except RecursionError:
            log.warning("trace %s too deep; skipping", trace_id)
            continue

        by_id = {s.span_id: s for s in spans}
        bucket = min(s.start_ns for s in spans) // 1_000_000_000

        for s in spans:
            parent = by_id.get(s.parent_span_id)
            if not parent or parent.service == s.service:
                continue
            edge_rows.append({
                "time_bucket": bucket, "trace_id": trace_id,
                "source_service": parent.service, "target_service": s.service,
                "source_operation": parent.operation, "target_operation": s.operation,
                "child_span_id": s.span_id,
                "duration_ms": s.duration_ms,
                "critical_ms": crit_by_edge.get((parent.service, s.service), 0.0),
                "is_error": int(s.is_error), "target_version": s.version,
            })

        root = segments[0]
        path_rows.append({
            "time_bucket": bucket, "trace_id": trace_id,
            "root_service": root.service, "root_operation": root.operation,
            "total_duration_ms": sum(seg.ms for seg in segments),
            "critical_path": [(s.service, s.operation, s.ms) for s in segments],
            "path_length": len(segments),
        })

    if edge_rows:
        await ch.insert("tracemap.trace_edges", edge_rows)
    if path_rows:
        await ch.insert("tracemap.trace_critical_path", path_rows)
    return len(traces)
```

**Dedupe by `span_id` in the fetch query.** Span ingestion is at-least-once, and
the project deliberately accepts duplicates in raw storage because they move a
p99 imperceptibly. That reasoning does not survive into the assembler. A
duplicated `span_id` collides in `by_id`, so the last copy wins and a span's
service and timings can be silently replaced — blame moves to a different edge,
and the original edge can disappear from the output entirely. Worse, the edge
loop iterates `spans` rather than `by_id`, so a duplicate emits two rows into
`trace_edges`, double-counting the edge and feeding its duration into the TDigest
twice. Collapse duplicates at the boundary:

```sql
SELECT trace_id, span_id,
       argMax(parent_span_id,        timestamp) AS parent_span_id,
       argMax(service_name,          timestamp) AS service_name,
       argMax(operation_name,        timestamp) AS operation_name,
       argMax(start_time_unix_ns,    timestamp) AS start_time_unix_ns,
       argMax(end_time_unix_ns,      timestamp) AS end_time_unix_ns,
       argMax(status_code = 'ERROR', timestamp) AS is_error,
       argMax(service_version,       timestamp) AS service_version
FROM tracemap.spans
WHERE ...
GROUP BY trace_id, span_id          -- the fix
ORDER BY trace_id, start_time_unix_ns
```

Operational rules for this worker:

- **Never let it die.** Wrap the cycle in a broad `except` that logs and continues; a dead assembler means an empty map with no error anywhere.
- **Emit a heartbeat metric every cycle** and alert on its absence. A silent assembler looks exactly like healthy quiet traffic.
- **Guard recursion depth.** Deep traces can blow the Python stack; skip and log rather than crash the cycle.
- **Alert on assembler lag** above ~120s. Lag means the map is stale in a way the freshness banner will not catch.
- **Scale by trace-ID hash** if one instance falls behind — the work partitions cleanly because every trace is independent.

## The four limitations — volunteer these before being asked

1. **Sampling skews attribution.** If tail sampling keeps mostly slow traces, blame shares are computed over a biased sample. Keep a probabilistic baseline policy and compute shares from the unbiased slice, or scale by adjusted counts.
2. **Async work breaks the model.** A fire-and-forget span outliving its parent is not on the request's critical path but may still matter. Detect `span.kind = PRODUCER` and exclude it explicitly rather than letting it be silently zeroed.
3. **Missing spans distort the path.** A dropped intermediate makes a grandchild look like a child. Orphan-tolerant root selection covers the common case; deeper gaps need trace-completeness scoring, which is future work.
4. **Clock skew across nodes.** The maths assumes comparable timestamps. A few milliseconds of skew between pods is normal and adds noise at short durations — floor contributions below ~2ms to zero and monitor skew.

Naming the limits of your own idea is a stronger signal than the idea. Keep these in `docs/LIMITATIONS.md` and rehearse them.

## Interview soundbites

- *"Every service map tells you which edge is slow. Mine tells you which edge is slow and on the critical path — a 700ms call running parallel to a 720ms sibling contributes zero user-visible latency, and every other map paints it red."*
- *"The algorithm is simple: walk the span tree backwards from the root's end and skip anything overlapping what you already attributed. The hard part is knowing when it lies — biased sampling, async spans, missing spans, clock skew. I documented all four and mitigated three."*
- *"Stitching edges and computing the critical path need the same grouping, so I do both in one pass. The differentiator rides along on work the pipeline had to do anyway."*

## Pairing

- `tracemap-clickhouse-analytics` — the tables this writes and the roll-ups above them
- `tracemap-anomaly-eval` — detection runs on critical-path p99, not wall clock
- `tracemap-nextjs-dashboard` — the wall-clock vs critical-path toggle
