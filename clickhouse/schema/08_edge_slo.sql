-- 08_edge_slo.sql -- D6: service-level objectives on CRITICAL-PATH latency,
-- per dependency edge.
--
-- WHY THIS IS NOT THE SAME AS AN ORDINARY LATENCY SLO. Every SLO tool in the
-- ecosystem measures an edge by its wall-clock duration. In a parallel fan-out
-- that is the wrong measurement twice over:
--
--   * a slow sibling that is NOT on the critical path costs the user nothing,
--     so burning error budget for it pages someone for a non-problem;
--   * the edge that IS on the critical path is diluted by every request where
--     it was not, so the number understates the real user impact.
--
-- Objectives here are stated against `critical_ms` -- the milliseconds this
-- edge actually contributed to user-visible latency. An edge can be 700 ms of
-- wall clock and burn zero budget, which is exactly the assertion the whole
-- project exists to make. No OSS tool does this, and this table is the
-- cheapest possible way to do it.
--
-- WHY BUCKET COUNTS AND NOT A QUANTILE. A latency SLO needs good events over
-- total events, because that is what an error budget is denominated in. A
-- TDigest gives percentiles, and "p99 <= 100 ms" is a threshold on a summary
-- statistic, not a budget you can burn down. So this table stores a small
-- fixed histogram, the same shape Prometheus uses, for the same reason: any
-- objective that lands on a bucket bound can be evaluated after the fact,
-- per edge, without re-defining the view and without a backfill.
--
-- The bounds are deliberately coarse. Seven columns cover the useful range,
-- and a bound is cheap to add later while a bound removed is a hole in the
-- history.
--
-- M3 -- this reads ONE table. `trace_edges` and nothing else. A roll-up MV
-- that joins sees a single insert block and silently loses rows, which is the
-- entire lesson of ADR 0003.
--
-- F1 -- every counter is SimpleAggregateFunction(sum, UInt64), never a plain
-- UInt64. A plain column in an AggregatingMergeTree is NOT summed on merge;
-- it keeps an arbitrary row's value. That defect is what Postmortem 001 is
-- about, and this table is the first one written after it, so it is the first
-- place the fix had to be applied from the start rather than retrofitted.

CREATE TABLE IF NOT EXISTS tracemap.edge_slo_1m
(
    time_bucket        DateTime,
    source_service     LowCardinality(String),
    target_service     LowCardinality(String),

    total_count        SimpleAggregateFunction(sum, UInt64),
    error_count        SimpleAggregateFunction(sum, UInt64),

    -- Cumulative "critical_ms <= bound" counts. Cumulative rather than
    -- disjoint so a ratio is one division and never a running sum, and so a
    -- consumer cannot accidentally read a bucket as an exclusive band.
    crit_le_25_count   SimpleAggregateFunction(sum, UInt64),
    crit_le_50_count   SimpleAggregateFunction(sum, UInt64),
    crit_le_100_count  SimpleAggregateFunction(sum, UInt64),
    crit_le_250_count  SimpleAggregateFunction(sum, UInt64),
    crit_le_500_count  SimpleAggregateFunction(sum, UInt64),
    crit_le_1000_count SimpleAggregateFunction(sum, UInt64),

    -- Wall clock at one bound, kept purely so the SLO page can show the
    -- contrast that justifies the design: same edge, same window, budget
    -- burnt under each measure. It is evidence, not an objective.
    wall_le_250_count  SimpleAggregateFunction(sum, UInt64)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMMDD(time_bucket)
ORDER BY (source_service, target_service, time_bucket);

CREATE MATERIALIZED VIEW IF NOT EXISTS tracemap.edge_slo_1m_mv
TO tracemap.edge_slo_1m AS
SELECT
    toStartOfMinute(time_bucket)          AS time_bucket,
    source_service,
    target_service,
    count()                               AS total_count,
    countIf(is_error = 1)                 AS error_count,
    countIf(critical_ms <=   25.0)        AS crit_le_25_count,
    countIf(critical_ms <=   50.0)        AS crit_le_50_count,
    countIf(critical_ms <=  100.0)        AS crit_le_100_count,
    countIf(critical_ms <=  250.0)        AS crit_le_250_count,
    countIf(critical_ms <=  500.0)        AS crit_le_500_count,
    countIf(critical_ms <= 1000.0)        AS crit_le_1000_count,
    countIf(duration_ms <=  250.0)        AS wall_le_250_count
FROM tracemap.trace_edges
GROUP BY time_bucket, source_service, target_service;

-- 30 days, matching the SLO window. An error budget stated over 30 days and
-- computed over 7 is not an error budget, it is a rolling week wearing a
-- month's label -- and it reads as healthy on day 8 of a bad month.
ALTER TABLE tracemap.edge_slo_1m
    MODIFY TTL time_bucket + INTERVAL 30 DAY;
