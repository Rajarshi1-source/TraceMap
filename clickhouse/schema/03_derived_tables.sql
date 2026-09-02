-- 03_derived_tables.sql -- written by the trace-assembler worker (Phase 4).
--
-- Stitching a parent span to its child is a STATEFUL, TRACE-SCOPED operation.
-- It belongs in a worker that can hold a whole trace in memory, not in an
-- insert trigger that only ever sees one block. That is the entire lesson of
-- ADR 0003, and these two tables are its output.
--
-- The assembler produces both in a single pass, because stitching edges and
-- computing the critical path need the same grouping -- all spans of one trace.
-- The flagship differentiator therefore costs almost nothing extra: it rides
-- along on work the pipeline has to do anyway.

CREATE TABLE IF NOT EXISTS tracemap.trace_edges
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
ORDER BY (source_service, target_service, time_bucket);

-- M5: rows with critical_ms = 0.0 are written DELIBERATELY and must never be
-- filtered out. A parallel edge still has real traffic, real wall-clock latency
-- and a real error rate -- what it does not have is blame. The zero row is what
-- lets the dependency map fade that edge out instead of making it vanish, and
-- vanishing would be a lie: the call really did happen.

CREATE TABLE IF NOT EXISTS tracemap.trace_critical_path
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
ORDER BY (root_service, time_bucket);
