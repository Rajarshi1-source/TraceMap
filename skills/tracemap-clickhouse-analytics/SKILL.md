---
name: tracemap-clickhouse-analytics
description: Design and maintain TraceMap's ClickHouse 26.3 analytics tier — the tracemap database, raw spans table, derived trace_edges and trace_critical_path tables, AggregatingMergeTree roll-ups with TDigest sketches, the refreshable reconciler, TTL retention, and the four dashboard queries. Use for ANY ClickHouse work in this project - schema design, sorting keys, materialized views, percentile aggregation, slow queries, retention, deduplication, or storage sizing - and also when the user says "the graph is missing edges", "aggregation is slow", "add a rollup", or "why is my materialized view empty". MANDATE - a classic ClickHouse materialized view is an INSERT TRIGGER over a single insert block, never a view over a table, so it must never contain a self-join on the spans table; parent-child stitching belongs in the assembler, and cross-block correctness is guarded by a refreshable materialized view. Read references/schema-and-queries.md for the full DDL. Pair with tracemap-critical-path for the writer side.
---

# TraceMap — ClickHouse Analytics Tier

One ClickHouse server, two databases:

- `jaeger.*` — owned by Jaeger v2's native backend. **Read-only to us.** Never build on it; the schema is alpha.
- `tracemap.*` — ours. Raw spans, derived tables, roll-ups, deploy markers.

Yes, spans are stored twice. At ~8.6x compression and demo-scale traffic that is a rounding error, and it buys schema independence from an alpha backend. State the cost out loud rather than hiding it.

## The defect this skill exists to prevent

The single most damaging bug in this project's history was an edge-aggregation materialized view written like this:

```sql
-- BROKEN. Do not reproduce this shape.
CREATE MATERIALIZED VIEW tracemap.service_edges_1m ... AS
SELECT ...
FROM tracemap.spans AS child
INNER JOIN tracemap.spans AS parent
    ON child.parent_span_id = parent.span_id
```

A classic MV fires on INSERT and its `SELECT` sees **only the rows in the current insert block** — not the table. Parent and child spans usually arrive in different batches, because a parent span finishes after its children and the collector flushes on size or time. So the join matches nothing and the edge is dropped. No error, no warning; the dependency map just quietly under-counts and every percentile is computed over a biased subset of edges.

Three rules follow, and they are worth internalising rather than memorising:

1. **MV `FROM` sees one block.** Joins inside an MV read the *current state* of the joined table at insert time — fine for small dimension lookups, wrong for stitching a stream against itself.
2. **Stitching is trace-scoped and stateful.** It belongs in a worker that can see a whole trace, not in an insert trigger.
3. **Anything that must be correct deserves an independent recomputation.** Hence the reconciler below.

## The corrected pipeline

```
OTel Collector ──► tracemap.spans            (raw landing table, TTL 7d)
                          │
                          ▼
              trace-assembler worker          (stateful, trace-scoped)
                          │
          ┌───────────────┴───────────────┐
          ▼                               ▼
  tracemap.trace_edges          tracemap.trace_critical_path
          │
          ▼  (incremental MV — SINGLE TABLE, no join)
  tracemap.service_edges_1m      AggregatingMergeTree + TDigest states
          │
          ▼  (refreshable MV every 60s, independent full query)
  tracemap.service_edges_recon   correctness guard; alert on >2% divergence
```

The assembler owns correctness; the incremental MV owns speed; the refreshable MV owns proof. Each layer has one job.

## Schema decisions to be able to defend

**Sorting key on raw spans is `(trace_id, start_time_unix_ns)`.** The dominant read on this table is "give me every span in this trace" — that is what the assembler does thousands of times a minute. In ClickHouse the sorting key *is* the primary index, so it should match the dominant access pattern. Per-service scans are served by the derived tables instead. "Why did you choose that sorting key?" is the most common ClickHouse interview question; this is the answer.

**Roll-ups use `AggregatingMergeTree` with `AggregateFunction` columns, not `SummingMergeTree`.** Summing merges numeric columns by addition only — it cannot merge TDigest sketches, so a `SummingMergeTree` roll-up accumulates unmerged sketch rows and the "merge percentiles across buckets" trick silently fails. Pair `quantileTDigestState` on write with `quantileTDigestMerge` on read.

**Plain counter columns in an `AggregatingMergeTree` silently under-count.** The
engine merges rows sharing a sorting key, but it only combines
`AggregateFunction` and `SimpleAggregateFunction` columns — a plain column keeps
one arbitrary value and the rest are thrown away. Because a materialized view
fires once per insert block, one minute receives many rows with the same
`(source_service, target_service, time_bucket)` key, so `request_count UInt64`
loses almost all of them. Declare every roll-up counter as
`SimpleAggregateFunction(sum, UInt64)`.

Measured on ClickHouse 26.3 — three inserts into a single minute:

| Declaration | request_count | error_count |
|---|---|---|
| ground truth | 3 | 1 |
| `UInt64` (plain) | **1** | **0** |
| `SimpleAggregateFunction(sum, UInt64)` | 3 | 1 |

The error count collapsing to zero is the dangerous part: error-rate edge
colouring would never fire. This is the same failure class as the MV self-join —
silent, load-dependent under-counting — one layer down. Note that a plain
`UInt64` counter is still correct on the *reconciler* table, which is an ordinary
`MergeTree` in `APPEND` mode, not an aggregating one.

**TDigest states, not stored percentiles.** A TDigest is a small mergeable sketch of a distribution. Storing one per minute means 5-minute, 1-hour and 24-hour windows are all one fast query over the same rows. You cannot average percentiles; you *can* merge sketches. This is the trick that keeps the dashboard under 100ms.

**Both metrics on every edge.** Every edge row carries `duration_ms` (wall clock) and `critical_ms` (critical-path contribution). Parallel edges are written with `critical_ms = 0.0` — **kept, not omitted** — because the map must still draw an edge that has real traffic and real errors but no blame. A zero row is what lets the UI fade it out instead of making it vanish.

## Retention

| Table | TTL | Rationale |
|---|---|---|
| `spans` | 7 days | Raw span volume dominates disk |
| `trace_edges` | 7 days | Derived; regenerable from spans while they exist |
| `trace_critical_path` | 7 days | Same |
| `service_edges_1m` | 30 days | Roll-ups are ~1000x smaller — long retention is nearly free |
| `service_edges_recon` | 2 days | Only needed for the divergence check |
| `deploy_markers` | 90 days | Tiny, and useful for long-range change analysis |

## Duplicate spans

Ingestion is at-least-once — a collector retry can insert the same span twice. Two options: `ReplacingMergeTree` keyed on `(trace_id, span_id)`, or accept the duplicates. **Measure before choosing.** At demo scale a duplicated span shifts a p99 imperceptibly, while dedup costs merge CPU forever, so the default is to accept and document the decision. Also know that ClickHouse dedup is asynchronous — it happens at merge time, and `FINAL` is what forces correctness now at query cost. That follow-up gets asked a lot.

## Monitoring this tier

- `system.view_refreshes` — alert if the reconciler's `status != 'Scheduled'` or `last_success_time` is older than 5 minutes.
- Divergence between `service_edges_1m` and `service_edges_recon` above 2% for 10 minutes — this is the guard for the whole bug class above.
- `system.parts` count per table — runaway small parts means insert batching is too small.
- Insert rate and disk growth per partition, feeding the cost model.

## Query patterns

Full DDL and the four production queries live in `references/schema-and-queries.md`. Read it before writing new SQL against these tables. The four that matter:

1. **Dependency graph, dual-metric** — powers the map; ranks by `crit_p99` because that is what actually hurts users.
2. **Blame ranking** — each edge's share of total critical-path milliseconds.
3. **Sample slow traces for an edge** — ordered by `critical_ms`, not wall clock, so alert links point at traces that were genuinely on the critical path.
4. **Deploy correlation** — deploy markers within the anomaly window.

## What not to do

- Do not put a self-join in a classic materialized view.
- Do not use `SummingMergeTree` for TDigest columns.
- Do not declare roll-up counters as plain `UInt64` in an `AggregatingMergeTree`; they will not be summed on merge.
- Do not store precomputed p99 numbers you cannot re-aggregate across time windows.
- Do not build views on `jaeger.*` tables.
- Do not omit zero-blame edges from `trace_edges`.
- Do not add `FINAL` to dashboard queries by reflex — measure whether duplicates actually matter first.
- Do not raise TTLs without recomputing disk against the measured bytes-per-span.

## Interview soundbites

- *"A ClickHouse materialized view is an insert trigger over one block, not a view over a table. My first schema got that wrong and silently dropped edges whenever a parent and child landed in different batches."*
- *"I keep an incremental MV for speed and a refreshable MV that recomputes the same numbers on a timer, with an alert on the delta. If I get it wrong again I find out from a page, not from a demo."*
- *"You can't average percentiles, but you can merge TDigest sketches — that's why any time window is one query instead of a rescan."*
- *"Anything in an AggregatingMergeTree that isn't an aggregate-function column keeps one arbitrary value on merge. My request counters were plain UInt64 — three inserts in a minute rolled up as one, and the error count went to zero, so the edge would never have turned red."*

## Pairing

- `tracemap-critical-path` — the assembler that writes these derived tables
- `tracemap-otel-pipeline` — how spans arrive
- `tracemap-anomaly-eval` — the detector reading these roll-ups
