-- Platform metadata in PostgreSQL 18.
--
-- These live in Postgres, not ClickHouse or Redis, because of what they are:
-- small, relational, human-authored or audit-grade, and unacceptable to lose or
-- half-write. Baselines go in Redis because they are derived state rewritten
-- every cycle and rebuildable in one query. Choosing storage by durability
-- requirement rather than by habit is the whole point.

CREATE TABLE IF NOT EXISTS alert_rules (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          TEXT NOT NULL,
    rule_type     TEXT NOT NULL CHECK (rule_type IN
                  ('edge_p99_delta','edge_critical_delta','edge_error_rate')),
    src_service   TEXT,
    dst_service   TEXT,
    threshold_ms  NUMERIC,
    min_requests  INT  NOT NULL DEFAULT 100,
    sustained_n   INT  NOT NULL DEFAULT 3,
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS anomaly_events (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    detected_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    src_service       TEXT NOT NULL,
    dst_service       TEXT NOT NULL,
    metric            TEXT NOT NULL,        -- critical_p99 | wall_p99
    baseline_ms       NUMERIC NOT NULL,
    observed_ms       NUMERIC NOT NULL,
    delta_ms          NUMERIC NOT NULL,
    request_count     BIGINT  NOT NULL,
    sample_trace_ids  TEXT[]  NOT NULL DEFAULT '{}',
    -- Always written, including {"found": false}. "No deploy correlated" is a
    -- real result: it sends the investigation to load and dependencies instead
    -- of the changelog. A NULL here would be ambiguous between "we checked and
    -- found nothing" and "we never checked".
    correlated_deploy JSONB,
    resolved_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_anomaly_recent
    ON anomaly_events (detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_anomaly_edge
    ON anomaly_events (src_service, dst_service, detected_at DESC);

-- Every automated remediation, whoever triggered it. An action with no audit
-- trail is indistinguishable from an accident afterwards.
CREATE TABLE IF NOT EXISTS rollback_audit (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    anomaly_id   UUID REFERENCES anomaly_events(id),
    service      TEXT NOT NULL,
    to_revision  TEXT NOT NULL,
    actor        TEXT NOT NULL,             -- human handle, or 'automation'
    confidence   NUMERIC,
    executed     BOOLEAN NOT NULL DEFAULT FALSE,
    outcome      TEXT
);
