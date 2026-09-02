-- 06_deploy_markers.sql -- change intelligence (Phase 6).
--
-- Written by the Kubernetes deploy watcher. Tiny table, long retention: it is
-- the join target for "did a deploy cause this?", and 90 days of it costs
-- almost nothing while enabling long-range change analysis.
--
-- `trigger` distinguishes a rollout from a scale event. A replica change is
-- NOT a release, and correlating an anomaly to one would be a false lead that
-- looks authoritative.

CREATE TABLE IF NOT EXISTS tracemap.deploy_markers
(
    deployed_at   DateTime,
    service_name  LowCardinality(String),
    revision      String,
    image_sha     String,
    git_sha       String,          -- M24: joins a trace to the code that made it
    replicas      UInt16,
    trigger       LowCardinality(String)   -- rollout | scale | image-change
)
ENGINE = MergeTree
ORDER BY (service_name, deployed_at)
TTL deployed_at + INTERVAL 90 DAY;
