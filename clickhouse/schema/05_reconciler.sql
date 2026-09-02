-- 05_reconciler.sql -- the correctness guard.
--
-- The incremental view in 04 owns SPEED. This owns PROOF.
--
-- A REFRESHABLE materialized view runs the FULL query against the WHOLE table
-- on a timer. It is not an insert trigger. That is why the self-join below is
-- correct here and would be a defect in 04: same SQL, completely different
-- execution model. Being able to explain that distinction is what separates
-- understanding ClickHouse from having copied a snippet.
--
-- Refreshable MVs have been production-ready since 24.10, APPEND adds rows
-- rather than replacing the target, and progress is observable in
-- system.view_refreshes.
--
-- The point is independence: if the fast path is ever wrong again, a Prometheus
-- alert on the delta between the two tells us -- from a page, not from a demo.

CREATE TABLE IF NOT EXISTS tracemap.service_edges_recon
(
    computed_at    DateTime,
    time_bucket    DateTime,
    source_service LowCardinality(String),
    target_service LowCardinality(String),
    request_count  UInt64          -- plain UInt64 is CORRECT here: this is an
                                   -- ordinary MergeTree in APPEND mode, not an
                                   -- AggregatingMergeTree, so nothing is
                                   -- collapsed on merge and nothing is lost.
)
ENGINE = MergeTree
ORDER BY (time_bucket, source_service, target_service)
TTL computed_at + INTERVAL 2 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS tracemap.service_edges_recon_mv
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
