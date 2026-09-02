# TraceMap ClickHouse — full DDL and production queries

Contents:
1. Databases
2. Raw spans table
3. Derived tables (trace_edges, trace_critical_path)
4. Incremental roll-up (AggregatingMergeTree + TDigest)
5. Refreshable reconciler
6. Deploy markers
7. The four production queries
8. Sizing and verification snippets

---

## 1. Databases

```sql
-- clickhouse/schema/01_databases.sql
CREATE DATABASE IF NOT EXISTS tracemap;
-- The `jaeger` database is created and owned by Jaeger v2's native backend.
-- We never write to it and never build views on it (alpha schema).
```

---

## 2. Raw spans table

```sql
-- clickhouse/schema/02_spans.sql
CREATE TABLE tracemap.spans
(
    timestamp           DateTime64(9)          CODEC(Delta, ZSTD(1)),
    trace_id            String                 CODEC(ZSTD(1)),
    span_id             String                 CODEC(ZSTD(1)),
    parent_span_id      String                 CODEC(ZSTD(1)),

    service_name        LowCardinality(String) CODEC(ZSTD(1)),
    operation_name      LowCardinality(String) CODEC(ZSTD(1)),
    span_kind           LowCardinality(String) CODEC(ZSTD(1)),

    start_time_unix_ns  UInt64                 CODEC(Delta, ZSTD(1)),
    end_time_unix_ns    UInt64                 CODEC(Delta, ZSTD(1)),
    duration_ms         Float64                CODEC(Gorilla, ZSTD(1)),

    status_code         LowCardinality(String) CODEC(ZSTD(1)),
    status_message      String                 CODEC(ZSTD(1)),

    http_method         LowCardinality(String) CODEC(ZSTD(1)),
    http_status_code    UInt16                 CODEC(ZSTD(1)),
    http_route          LowCardinality(String) CODEC(ZSTD(1)),

    db_system           LowCardinality(String) CODEC(ZSTD(1)),
    db_statement        String                 CODEC(ZSTD(1)),

    k8s_pod_name        LowCardinality(String) CODEC(ZSTD(1)),
    k8s_namespace       LowCardinality(String) CODEC(ZSTD(1)),
    service_version     LowCardinality(String) CODEC(ZSTD(1)),

    attributes          Map(String, String),
    resource_attributes Map(String, String),

    INDEX idx_trace_id trace_id       TYPE bloom_filter(0.01) GRANULARITY 4,
    INDEX idx_parent   parent_span_id TYPE bloom_filter(0.01) GRANULARITY 4
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (trace_id, start_time_unix_ns)
TTL toDateTime(timestamp) + INTERVAL 7 DAY
SETTINGS index_granularity = 8192;
```

`LowCardinality` on service/operation/status columns is not cosmetic — these are
dictionary-encoded, which is where a large share of the compression ratio comes
from. `Gorilla` on float durations exploits the fact that consecutive latency
values share most of their bits.

---

## 3. Derived tables

```sql
-- clickhouse/schema/03_derived_tables.sql
CREATE TABLE tracemap.trace_edges
(
    time_bucket       DateTime               CODEC(Delta, ZSTD(1)),
    trace_id          String                 CODEC(ZSTD(1)),
    source_service    LowCardinality(String) CODEC(ZSTD(1)),
    target_service    LowCardinality(String) CODEC(ZSTD(1)),
    source_operation  LowCardinality(String) CODEC(ZSTD(1)),
    target_operation  LowCardinality(String) CODEC(ZSTD(1)),
    child_span_id     String                 CODEC(ZSTD(1)),

    duration_ms       Float64 CODEC(Gorilla, ZSTD(1)),   -- wall clock
    critical_ms       Float64 CODEC(Gorilla, ZSTD(1)),   -- critical-path share
    is_error          UInt8,
    target_version    LowCardinality(String) CODEC(ZSTD(1))
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(time_bucket)
ORDER BY (source_service, target_service, time_bucket)
TTL time_bucket + INTERVAL 7 DAY;

CREATE TABLE tracemap.trace_critical_path
(
    time_bucket       DateTime CODEC(Delta, ZSTD(1)),
    trace_id          String   CODEC(ZSTD(1)),
    root_service      LowCardinality(String),
    root_operation    LowCardinality(String),
    total_duration_ms Float64  CODEC(Gorilla, ZSTD(1)),
    critical_path     Array(Tuple(String, String, Float64)),  -- (svc, op, ms)
    path_length       UInt16
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(time_bucket)
ORDER BY (root_service, time_bucket)
TTL time_bucket + INTERVAL 7 DAY;
```

Rows with `critical_ms = 0.0` are written deliberately — a parallel edge still
has traffic, latency and errors, and the UI needs the row to fade the edge out
rather than drop it from the graph.

---

## 4. Incremental roll-up

```sql
-- clickhouse/schema/04_materialized_views.sql
CREATE TABLE tracemap.service_edges_1m
(
    time_bucket      DateTime,
    source_service   LowCardinality(String),
    target_service   LowCardinality(String),
    request_count    SimpleAggregateFunction(sum, UInt64),
    error_count      SimpleAggregateFunction(sum, UInt64),
    wall_p50_state   AggregateFunction(quantileTDigest(0.50), Float64),
    wall_p95_state   AggregateFunction(quantileTDigest(0.95), Float64),
    wall_p99_state   AggregateFunction(quantileTDigest(0.99), Float64),
    crit_p50_state   AggregateFunction(quantileTDigest(0.50), Float64),
    crit_p95_state   AggregateFunction(quantileTDigest(0.95), Float64),
    crit_p99_state   AggregateFunction(quantileTDigest(0.99), Float64),
    crit_ms_sum      SimpleAggregateFunction(sum, Float64)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMMDD(time_bucket)
ORDER BY (source_service, target_service, time_bucket)
TTL time_bucket + INTERVAL 30 DAY;

CREATE MATERIALIZED VIEW tracemap.service_edges_1m_mv
TO tracemap.service_edges_1m AS
SELECT
    toStartOfMinute(time_bucket)            AS time_bucket,
    source_service,
    target_service,
    count()                                 AS request_count,
    countIf(is_error = 1)                   AS error_count,
    quantileTDigestState(0.50)(duration_ms) AS wall_p50_state,
    quantileTDigestState(0.95)(duration_ms) AS wall_p95_state,
    quantileTDigestState(0.99)(duration_ms) AS wall_p99_state,
    quantileTDigestState(0.50)(critical_ms) AS crit_p50_state,
    quantileTDigestState(0.95)(critical_ms) AS crit_p95_state,
    quantileTDigestState(0.99)(critical_ms) AS crit_p99_state,
    sum(critical_ms)                        AS crit_ms_sum
FROM tracemap.trace_edges          -- ONE table. No join. Batch-safe.
GROUP BY time_bucket, source_service, target_service;
```

> **Why the counters are `SimpleAggregateFunction(sum, UInt64)` and not plain
> `UInt64`.** `AggregatingMergeTree` merges rows sharing a sorting key, but it
> only *combines* `AggregateFunction` and `SimpleAggregateFunction` columns — a
> plain column keeps one arbitrary value and the rest are discarded. The MV
> fires once per insert block, so a single minute receives many rows with the
> same `(source_service, target_service, time_bucket)` key. Declared plain, the
> counters silently under-count.
>
> Measured on ClickHouse 26.3, three inserts into one minute:
>
> | Declaration | request_count | error_count |
> |---|---|---|
> | ground truth | 3 | 1 |
> | `UInt64` (plain) | **1** | **0** |
> | `SimpleAggregateFunction(sum, UInt64)` | 3 | 1 |
>
> Note the second row: the error count collapses to zero, so error-rate edge
> colouring would never fire. This is the same failure class as the
> materialized-view self-join — silent, load-dependent under-counting — one
> layer further down.

Also useful — a service-level RED roll-up, same shape, single table:

```sql
CREATE TABLE tracemap.service_red_1m
(
    time_bucket    DateTime,
    service_name   LowCardinality(String),
    request_count  SimpleAggregateFunction(sum, UInt64),
    error_count    SimpleAggregateFunction(sum, UInt64),
    p50_state      AggregateFunction(quantileTDigest(0.50), Float64),
    p95_state      AggregateFunction(quantileTDigest(0.95), Float64),
    p99_state      AggregateFunction(quantileTDigest(0.99), Float64)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMMDD(time_bucket)
ORDER BY (service_name, time_bucket)
TTL time_bucket + INTERVAL 30 DAY;

CREATE MATERIALIZED VIEW tracemap.service_red_1m_mv
TO tracemap.service_red_1m AS
SELECT
    toStartOfMinute(timestamp)              AS time_bucket,
    service_name,
    count()                                 AS request_count,
    countIf(status_code = 'ERROR')          AS error_count,
    quantileTDigestState(0.50)(duration_ms) AS p50_state,
    quantileTDigestState(0.95)(duration_ms) AS p95_state,
    quantileTDigestState(0.99)(duration_ms) AS p99_state
FROM tracemap.spans
WHERE span_kind = 'SERVER'
GROUP BY time_bucket, service_name;
```

---

## 5. Refreshable reconciler

```sql
-- clickhouse/schema/05_reconciler.sql
CREATE TABLE tracemap.service_edges_recon
(
    computed_at    DateTime,
    time_bucket    DateTime,
    source_service LowCardinality(String),
    target_service LowCardinality(String),
    request_count  UInt64
)
ENGINE = MergeTree
ORDER BY (time_bucket, source_service, target_service)
TTL computed_at + INTERVAL 2 DAY;

CREATE MATERIALIZED VIEW tracemap.service_edges_recon_mv
REFRESH EVERY 60 SECOND APPEND
TO tracemap.service_edges_recon AS
SELECT
    now()                        AS computed_at,
    toStartOfMinute(c.timestamp) AS time_bucket,
    p.service_name               AS source_service,
    c.service_name               AS target_service,
    count()                      AS request_count
FROM tracemap.spans AS c
INNER JOIN tracemap.spans AS p ON c.parent_span_id = p.span_id
WHERE c.timestamp >= now() - INTERVAL 5 MINUTE
  AND c.span_kind = 'SERVER'
  AND p.service_name != c.service_name
GROUP BY time_bucket, source_service, target_service;
```

The self-join is **correct here**: a refreshable MV runs the full query against
the whole table on a timer, it is not an insert trigger. Same SQL, different
execution model. Refreshable MVs have been production-ready since 24.10 and
`APPEND` mode adds rows instead of replacing the target.

Health check:

```sql
SELECT database, view, status, last_success_time, next_refresh_time
FROM system.view_refreshes
WHERE database = 'tracemap';
```

Divergence check (drive a Prometheus gauge from this):

```sql
SELECT
    sum(mv.request_count) AS mv_total,
    sum(rc.request_count) AS recon_total,
    abs(mv_total - recon_total) / nullIf(recon_total, 0) AS divergence
FROM tracemap.service_edges_1m AS mv
FULL OUTER JOIN (
    SELECT time_bucket, source_service, target_service, max(request_count) AS request_count
    FROM tracemap.service_edges_recon
    WHERE computed_at >= now() - INTERVAL 5 MINUTE
    GROUP BY time_bucket, source_service, target_service
) AS rc
USING (time_bucket, source_service, target_service)
WHERE mv.time_bucket >= now() - INTERVAL 5 MINUTE;
```

---

## 6. Deploy markers

```sql
-- clickhouse/schema/06_deploy_markers.sql
CREATE TABLE tracemap.deploy_markers
(
    deployed_at   DateTime,
    service_name  LowCardinality(String),
    revision      String,
    image_sha     String,
    git_sha       String,
    replicas      UInt16,
    trigger       LowCardinality(String)   -- rollout | scale | image-change
)
ENGINE = MergeTree
ORDER BY (service_name, deployed_at)
TTL deployed_at + INTERVAL 90 DAY;
```

---

## 7. The four production queries

```sql
-- Q1: Dependency graph, dual-metric (THE dashboard query)
SELECT
    source_service AS source,
    target_service AS target,
    sum(request_count)                         AS request_count,
    sum(error_count)                           AS error_count,
    sum(error_count) / sum(request_count)      AS error_rate,
    quantileTDigestMerge(0.50)(wall_p50_state) AS wall_p50,
    quantileTDigestMerge(0.95)(wall_p95_state) AS wall_p95,
    quantileTDigestMerge(0.99)(wall_p99_state) AS wall_p99,
    quantileTDigestMerge(0.99)(crit_p99_state) AS crit_p99,
    sum(crit_ms_sum) / sum(request_count)      AS crit_mean_ms
FROM tracemap.service_edges_1m
WHERE time_bucket >= now() - INTERVAL 5 MINUTE
GROUP BY source, target
HAVING request_count > 10
ORDER BY crit_p99 DESC;
```

```sql
-- Q2: Blame ranking — share of total user-visible latency per edge
SELECT
    source_service, target_service,
    sum(crit_ms_sum) AS total_critical_ms,
    sum(crit_ms_sum) / (SELECT sum(crit_ms_sum)
                        FROM tracemap.service_edges_1m
                        WHERE time_bucket >= now() - INTERVAL 5 MINUTE) AS blame_share
FROM tracemap.service_edges_1m
WHERE time_bucket >= now() - INTERVAL 5 MINUTE
GROUP BY source_service, target_service
ORDER BY blame_share DESC
LIMIT 20;
```

```sql
-- Q3: Sample slow traces for one edge (alert links + drill-in)
SELECT trace_id, duration_ms, critical_ms, target_version
FROM tracemap.trace_edges
WHERE time_bucket >= now() - INTERVAL 5 MINUTE
  AND source_service = {src:String}
  AND target_service = {dst:String}
ORDER BY critical_ms DESC
LIMIT 5;
```

```sql
-- Q4: Deploy correlation for an anomaly window
SELECT service_name, revision, git_sha, deployed_at,
       dateDiff('second', deployed_at, {anomaly_start:DateTime}) AS lag_seconds
FROM tracemap.deploy_markers
WHERE deployed_at BETWEEN {anomaly_start:DateTime} - INTERVAL 15 MINUTE
                      AND {anomaly_start:DateTime}
ORDER BY deployed_at DESC;
```

Order Q3 by `critical_ms`, not `duration_ms` — otherwise alert links point at
traces where the edge was slow but parallel, and the person who clicks through
sees nothing wrong.

---

## 8. Sizing and verification

```sql
-- Bytes per span, compression ratio — feeds the cost model
SELECT
    table,
    formatReadableSize(sum(bytes_on_disk))                AS on_disk,
    sum(rows)                                             AS rows,
    round(sum(bytes_on_disk) / nullIf(sum(rows), 0), 1)   AS bytes_per_row,
    round(sum(data_uncompressed_bytes) /
          nullIf(sum(data_compressed_bytes), 0), 2)       AS compression_ratio
FROM system.parts
WHERE database = 'tracemap' AND active
GROUP BY table
ORDER BY sum(bytes_on_disk) DESC;
```

```sql
-- Part count per table — runaway small parts means insert batches are too small
SELECT table, count() AS parts
FROM system.parts
WHERE database = 'tracemap' AND active
GROUP BY table ORDER BY parts DESC;
```

```sql
-- Query latency check for the dashboard path
SELECT query_duration_ms, read_rows, formatReadableSize(read_bytes) AS read
FROM system.query_log
WHERE type = 'QueryFinish'
  AND query LIKE '%service_edges_1m%'
  AND event_time >= now() - INTERVAL 1 HOUR
ORDER BY query_duration_ms DESC
LIMIT 10;
```

Target: Q1 under 100ms with a day of data. If it is slower, check part count
first, sorting key second, and only then consider adding a projection.
