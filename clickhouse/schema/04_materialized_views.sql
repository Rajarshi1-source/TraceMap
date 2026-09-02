-- 04_materialized_views.sql -- SINGLE-TABLE roll-ups only.
--
-- =========================================================================
-- TWO DEFECTS ARE FIXED HERE. BOTH WERE SILENT. READ BEFORE EDITING.
-- =========================================================================
--
-- (1) M2/M3 -- no self-join. A classic materialized view is an INSERT TRIGGER,
--     not a view: its SELECT sees only the rows in the current insert block,
--     never the whole table. Stitching parent to child with a self-join here
--     works in a test (where both spans land in one batch) and silently drops
--     edges in production (where a parent finishes after its child and the
--     collector flushes on size or time). No error, no warning -- the
--     dependency map just under-counts under load. Stitching lives in the
--     assembler; these views only roll up ONE table.
--
-- (2) F1 -- the counters below are SimpleAggregateFunction(sum, UInt64), NOT
--     plain UInt64. AggregatingMergeTree collapses rows sharing a sorting key
--     but only COMBINES aggregate-function columns; a plain column keeps one
--     arbitrary value and the rest are discarded. Since this view fires once
--     per insert block, a single minute receives many rows with the same
--     (source_service, target_service, time_bucket) key.
--
--     Measured on ClickHouse 26.3.28.5 -- three inserts of 10 into one key:
--
--         plain UInt64                        -> 10   (2 of 3 discarded)
--         SimpleAggregateFunction(sum,UInt64) -> 30   (correct)
--
--     The error counter collapsing is the dangerous half: error_rate is a ratio
--     of two independently mangled counters, so red edge colouring would never
--     fire and the map would look healthy while a service returned 5xx.
--
-- M4 -- AggregatingMergeTree, never SummingMergeTree. Summing merges numeric
-- columns by addition only; it cannot merge a TDigest sketch, so the "merge
-- percentiles across time windows" trick would silently produce nonsense.

CREATE TABLE IF NOT EXISTS tracemap.service_edges_1m
(
    time_bucket      DateTime,
    source_service   LowCardinality(String),
    target_service   LowCardinality(String),
    request_count    SimpleAggregateFunction(sum, UInt64),   -- F1
    error_count      SimpleAggregateFunction(sum, UInt64),   -- F1
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
ORDER BY (source_service, target_service, time_bucket);

CREATE MATERIALIZED VIEW IF NOT EXISTS tracemap.service_edges_1m_mv
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

-- Service-level RED roll-up. Same shape, same rules, same F1 fix.
CREATE TABLE IF NOT EXISTS tracemap.service_red_1m
(
    time_bucket    DateTime,
    service_name   LowCardinality(String),
    request_count  SimpleAggregateFunction(sum, UInt64),     -- F1
    error_count    SimpleAggregateFunction(sum, UInt64),     -- F1
    p50_state      AggregateFunction(quantileTDigest(0.50), Float64),
    p95_state      AggregateFunction(quantileTDigest(0.95), Float64),
    p99_state      AggregateFunction(quantileTDigest(0.99), Float64)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMMDD(time_bucket)
ORDER BY (service_name, time_bucket);

CREATE MATERIALIZED VIEW IF NOT EXISTS tracemap.service_red_1m_mv
TO tracemap.service_red_1m AS
SELECT
    toStartOfMinute(timestamp)              AS time_bucket,
    service_name,
    count()                                 AS request_count,
    countIf(status_code = 'ERROR')          AS error_count,
    quantileTDigestState(0.50)(duration_ms) AS p50_state,
    quantileTDigestState(0.95)(duration_ms) AS p95_state,
    quantileTDigestState(0.99)(duration_ms) AS p99_state
FROM tracemap.spans                -- ONE table. No join.
WHERE span_kind = 'SERVER'
GROUP BY time_bucket, service_name;
