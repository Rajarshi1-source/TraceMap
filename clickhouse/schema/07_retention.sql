-- 07_retention.sql -- TTLs, applied last so every table exists.
--
-- Retention is set here rather than inline so the whole policy is readable in
-- one place, and so changing it is one reviewable diff.
--
-- TRAP: never raise a TTL without recomputing disk against the MEASURED
-- bytes-per-span. The figure to use comes from system.parts, not from a guess:
--
--   SELECT table,
--          formatReadableSize(sum(bytes_on_disk))              AS on_disk,
--          round(sum(bytes_on_disk) / nullIf(sum(rows), 0), 1) AS bytes_per_row,
--          round(sum(data_uncompressed_bytes) /
--                nullIf(sum(data_compressed_bytes), 0), 2)     AS compression
--   FROM system.parts WHERE database = (SELECT currentDatabase()) AND active
--   GROUP BY table ORDER BY sum(bytes_on_disk) DESC;

-- Landing table: 24h only. It exists to feed tracemap.spans, and keeping a
-- second 7-day copy of every span would double raw storage for no query.
ALTER TABLE tracemap.otel_spans
    MODIFY TTL toDateTime(Timestamp) + INTERVAL 24 HOUR;
ALTER TABLE tracemap.otel_spans_trace_id_ts
    MODIFY TTL toDateTime(Start) + INTERVAL 24 HOUR;

-- Raw spans dominate disk. 7 days is the drill-in window.
ALTER TABLE tracemap.spans
    MODIFY TTL toDateTime(timestamp) + INTERVAL 7 DAY;

-- Derived, and regenerable from spans while those still exist.
ALTER TABLE tracemap.trace_edges
    MODIFY TTL time_bucket + INTERVAL 7 DAY;
ALTER TABLE tracemap.trace_critical_path
    MODIFY TTL time_bucket + INTERVAL 7 DAY;

-- Roll-ups are roughly 1000x smaller than raw spans, so long retention here is
-- nearly free and is what makes month-over-month comparison possible.
ALTER TABLE tracemap.service_edges_1m
    MODIFY TTL time_bucket + INTERVAL 30 DAY;
ALTER TABLE tracemap.service_red_1m
    MODIFY TTL time_bucket + INTERVAL 30 DAY;
