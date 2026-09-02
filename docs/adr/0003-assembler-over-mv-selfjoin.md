# ADR 0003 — Stitch spans in a stateful assembler, not a materialized-view self-join

- **Status:** Accepted
- **Date:** 2026-09-02 (Phase 3)
- **Deciders:** Rajarshi Basu

## Context

The dependency map needs one row per parent→child service hop. The obvious way
to get it is to join the span table to itself inside a materialized view:

```sql
-- BROKEN. Do not reproduce this shape.
CREATE MATERIALIZED VIEW tracemap.service_edges_1m ... AS
SELECT ...
FROM tracemap.spans AS child
INNER JOIN tracemap.spans AS parent
    ON child.parent_span_id = parent.span_id
```

It reads correctly, and it passes a test.

**A classic ClickHouse materialized view is an insert trigger, not a view.** Its
`SELECT` runs against only the rows in the *current insert block* — the `FROM`
clause never sees the table. So:

- parent and child in the **same** collector batch → the join matches, edge recorded
- parent and child in **different** batches → **the join matches nothing and the
  edge is silently dropped**

The second case is the common one, because a parent span finishes *after* its
children and the collector flushes on size or time. There is no error and no
warning. The map just under-counts, and the percentiles are computed over a
biased subset of edges.

The tell is that it is **load-dependent**: at low traffic a whole trace fits in
one batch and everything looks fine; under load the batches split and edges
start vanishing. "Why does the graph miss edges under load?" is a question with
no good answer if this is the design.

## Decision

**Stitching is a stateful, trace-scoped operation and belongs in a worker that
can see a whole trace.** Three layers, each with exactly one job:

| Layer | Owns | Mechanism |
|---|---|---|
| `trace-assembler` (Phase 4) | **correctness** | reads completed traces, emits `trace_edges` + `trace_critical_path` in one pass |
| `service_edges_1m_mv` | **speed** | incremental MV, **single table**, TDigest states |
| `service_edges_recon_mv` | **proof** | refreshable MV, full query every 60s, independent recomputation |

The assembler doing both outputs in one pass is the efficiency insight: edge
stitching and critical-path extraction need the *same* grouping — all spans of
one trace — so the flagship differentiator rides along on work the pipeline must
do anyway.

**The reconciler's self-join is correct**, and the distinction is the whole
point: a `REFRESH EVERY 60 SECOND` view runs the full query against the whole
table on a timer. It is not an insert trigger. Same SQL, completely different
execution model. A Prometheus alert on the delta between the fast path and the
reconciler means that if this class of bug ever returns, it arrives as a page
rather than as a surprise during a demo.

## The second defect, found in this project's own schema

Auditing the fix surfaced a defect of the same family one layer down.
`service_edges_1m` is an `AggregatingMergeTree`, and its counters were declared
plain `UInt64`:

```sql
request_count    UInt64,          -- wrong
error_count      UInt64,          -- wrong
crit_ms_sum      SimpleAggregateFunction(sum, Float64)   -- already correct
```

`AggregatingMergeTree` collapses rows sharing a sorting key but only *combines*
`AggregateFunction` and `SimpleAggregateFunction` columns. A plain column keeps
one arbitrary value. Since the MV fires once per insert block, a single minute
receives many rows with the same key — so the counters silently under-count.

**Measured on ClickHouse 26.3.28.5**, three inserts of `10` into one key:

| Declaration | result |
|---|---|
| ground truth | 30 |
| `UInt64` (plain) | **10** |
| `SimpleAggregateFunction(sum, UInt64)` | 30 |

Verified again end-to-end through the real view, three separate insert blocks
into one minute:

| source | request_count | error_count |
|---|---|---|
| ground truth (`trace_edges`) | 3 | 1 |
| roll-up (`service_edges_1m`) | 3 | 1 |

**The error counter is the dangerous half.** `error_rate` in Q1 is a ratio of
two independently mangled counters, so red edge colouring would never fire — the
map would look healthy while a service returned 5xx. That `crit_ms_sum` was
already declared correctly in the same statement shows the pattern was known and
simply not applied to the counters.

Both defects share a shape worth naming: **silent, load-dependent under-count
with no error surface.** That is why `scripts/schema-guard.py` asserts on the
DDL and runs in CI, rather than relying on anyone remembering.

## Consequences

**Good.** Correctness, speed and proof are separable and independently
testable. The reconciler independently reproduced the correct four edges at 8
requests each before the assembler existed at all.

**Bad.** More moving parts than a single view: a worker to supervise, a refresh
to monitor via `system.view_refreshes`, and a divergence alert to maintain.

**The interview answer this buys.** *"A ClickHouse materialized view is an
insert trigger over one block, not a view over a table. My first schema got that
wrong and silently dropped edges whenever a parent and child landed in different
batches. I moved stitching into a stateful assembler, kept the MVs single-table,
and then added a refreshable view that recomputes the same numbers independently
with an alert on the delta — so if I get it wrong again, I find out from a page
rather than from a demo."*
