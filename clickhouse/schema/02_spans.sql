-- 02_spans.sql -- the landing table and TraceMap's own span table.
--
-- WHY THERE ARE TWO TABLES HERE
--
-- The OpenTelemetry ClickHouse exporter owns its wire schema: PascalCase
-- columns, `Duration` in nanoseconds, and -- the part that matters --
--
--     ORDER BY (ServiceName, SpanName, toDateTime(Timestamp))
--
-- That ordering suits "show me this service's spans". It is wrong for the
-- dominant read in this project, which is the trace assembler asking "give me
-- every span in THIS trace", thousands of times a minute. Under the exporter's
-- key those rows scatter across the part; under ours they are one contiguous
-- read. In ClickHouse the sorting key IS the primary index, so it has to match
-- the dominant access pattern.
--
-- Hence: `otel_spans` is a thin landing table matching the exporter byte for
-- byte, and `tracemap.spans` is ours, with our column names and our sorting
-- key, filled by a SINGLE-TABLE materialized view.
--
-- That view is single-table on purpose (M2/M3). It is a projection, not a
-- stitch: every row it reads is a row being inserted, so it cannot suffer the
-- batch-boundary bug a self-join would.
--
-- Cost: spans land twice. 07_retention.sql keeps `otel_spans` for 24h only, so
-- the steady-state overhead against a 7-day window is roughly 14%, not 100%.

-- ---------------------------------------------------------------- landing ---
-- Must stay compatible with what the exporter writes. The exporter runs with
-- create_schema: false so this file is the single source of truth and the whole
-- schema applies cleanly to an empty server.
CREATE TABLE IF NOT EXISTS tracemap.otel_spans
(
    `Timestamp`          DateTime64(9) CODEC(Delta(8), ZSTD(1)),
    `TraceId`            String CODEC(ZSTD(1)),
    `SpanId`             String CODEC(ZSTD(1)),
    `ParentSpanId`       String CODEC(ZSTD(1)),
    `TraceState`         String CODEC(ZSTD(1)),
    `SpanName`           LowCardinality(String) CODEC(ZSTD(1)),
    `SpanKind`           LowCardinality(String) CODEC(ZSTD(1)),
    `ServiceName`        LowCardinality(String) CODEC(ZSTD(1)),
    `ResourceAttributes` Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    `ScopeName`          String CODEC(ZSTD(1)),
    `ScopeVersion`       String CODEC(ZSTD(1)),
    `SpanAttributes`     Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    `Duration`           UInt64 CODEC(ZSTD(1)),
    `StatusCode`         LowCardinality(String) CODEC(ZSTD(1)),
    `StatusMessage`      String CODEC(ZSTD(1)),
    `Events.Timestamp`   Array(DateTime64(9)) CODEC(ZSTD(1)),
    `Events.Name`        Array(LowCardinality(String)) CODEC(ZSTD(1)),
    `Events.Attributes`  Array(Map(LowCardinality(String), String)) CODEC(ZSTD(1)),
    `Links.TraceId`      Array(String) CODEC(ZSTD(1)),
    `Links.SpanId`       Array(String) CODEC(ZSTD(1)),
    `Links.TraceState`   Array(String) CODEC(ZSTD(1)),
    `Links.Attributes`   Array(Map(LowCardinality(String), String)) CODEC(ZSTD(1)),
    INDEX idx_trace_id        TraceId                       TYPE bloom_filter(0.001) GRANULARITY 1,
    INDEX idx_res_attr_key    mapKeys(ResourceAttributes)   TYPE bloom_filter(0.01)  GRANULARITY 1,
    INDEX idx_res_attr_value  mapValues(ResourceAttributes) TYPE bloom_filter(0.01)  GRANULARITY 1,
    INDEX idx_span_attr_key   mapKeys(SpanAttributes)       TYPE bloom_filter(0.01)  GRANULARITY 1,
    INDEX idx_span_attr_value mapValues(SpanAttributes)     TYPE bloom_filter(0.01)  GRANULARITY 1,
    INDEX idx_duration        Duration                      TYPE minmax              GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toDate(Timestamp)
ORDER BY (ServiceName, SpanName, toDateTime(Timestamp))
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;

-- The exporter also maintains this trace-id index and its feeding view. Both
-- are single-table. Owning the DDL keeps a cold start deterministic.
CREATE TABLE IF NOT EXISTS tracemap.otel_spans_trace_id_ts
(
    `TraceId` String CODEC(ZSTD(1)),
    `Start`   DateTime CODEC(Delta(4), ZSTD(1)),
    `End`     DateTime CODEC(Delta(4), ZSTD(1)),
    INDEX idx_trace_id TraceId TYPE bloom_filter(0.01) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toDate(Start)
ORDER BY (TraceId, Start)
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;

CREATE MATERIALIZED VIEW IF NOT EXISTS tracemap.otel_spans_trace_id_ts_mv
TO tracemap.otel_spans_trace_id_ts AS
SELECT
    TraceId,
    min(Timestamp) AS Start,
    max(Timestamp) AS End
FROM tracemap.otel_spans
WHERE TraceId != ''
GROUP BY TraceId;

-- ------------------------------------------------------- TraceMap's table ---
CREATE TABLE IF NOT EXISTS tracemap.spans
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

    -- When the row ARRIVED, not when the span happened. The assembler needs to
    -- ask "have any new spans for this trace turned up in the last 30s?", and
    -- span timestamps cannot answer that: by the time a span is written it is
    -- already ~20s old (collector batch + tail-sampling decision_wait +
    -- export). Grace-checking on span time therefore declares a trace complete
    -- while a second batch is still in flight, and the assembler computes a
    -- critical path over half a tree.
    ingested_at         DateTime DEFAULT now() CODEC(Delta, ZSTD(1)),

    INDEX idx_trace_id trace_id       TYPE bloom_filter(0.01) GRANULARITY 4,
    INDEX idx_parent   parent_span_id TYPE bloom_filter(0.01) GRANULARITY 4
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (trace_id, start_time_unix_ns)
SETTINGS index_granularity = 8192;

-- ------------------------------------------------------------ projection ---
-- Attribute keys are read with a fallback because OpenTelemetry renamed several
-- of them between semantic-convention versions, and the three SDKs here are not
-- all on the same one. Reading only the newer name silently yields empty
-- strings for some services -- a gap that looks like missing instrumentation
-- rather than a naming mismatch.
CREATE MATERIALIZED VIEW IF NOT EXISTS tracemap.spans_mv TO tracemap.spans AS
SELECT
    Timestamp                                   AS timestamp,
    TraceId                                     AS trace_id,
    SpanId                                      AS span_id,
    ParentSpanId                                AS parent_span_id,

    ServiceName                                 AS service_name,
    SpanName                                    AS operation_name,
    upper(SpanKind)                             AS span_kind,

    toUnixTimestamp64Nano(Timestamp)            AS start_time_unix_ns,
    toUnixTimestamp64Nano(Timestamp) + Duration AS end_time_unix_ns,
    Duration / 1000000.0                        AS duration_ms,

    upper(StatusCode)                           AS status_code,
    StatusMessage                               AS status_message,

    if(SpanAttributes['http.request.method'] != '',
       SpanAttributes['http.request.method'],
       SpanAttributes['http.method'])           AS http_method,
    toUInt16OrZero(if(SpanAttributes['http.response.status_code'] != '',
                      SpanAttributes['http.response.status_code'],
                      SpanAttributes['http.status_code']))
                                                AS http_status_code,
    SpanAttributes['http.route']                AS http_route,

    if(SpanAttributes['db.system.name'] != '',
       SpanAttributes['db.system.name'],
       SpanAttributes['db.system'])             AS db_system,
    -- M28: whatever the SDK put here is already parameterised. Never build this
    -- column by concatenating values in.
    if(SpanAttributes['db.query.text'] != '',
       SpanAttributes['db.query.text'],
       SpanAttributes['db.statement'])          AS db_statement,

    ResourceAttributes['k8s.pod.name']          AS k8s_pod_name,
    ResourceAttributes['k8s.namespace.name']    AS k8s_namespace,
    ResourceAttributes['service.version']       AS service_version,

    CAST(SpanAttributes, 'Map(String, String)')     AS attributes,
    CAST(ResourceAttributes, 'Map(String, String)') AS resource_attributes
FROM tracemap.otel_spans;
