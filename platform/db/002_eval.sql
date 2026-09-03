-- Eval harness system of record (Phase 7).
--
-- The scoreboard lives in Postgres for the same reason the alert rules do: it
-- is small, relational, audit-grade, and losing it silently would be worse
-- than not having it. A score you cannot reproduce or attribute is a claim,
-- not a measurement.
--
-- `detector_cfg` is the column that turns this from a log into experiment
-- tracking. Any change in F1 is attributable to a config change or it is
-- drift, and without the config recorded alongside the score there is no way
-- to tell which -- which is exactly the question "did your tuning help?"
-- is asking.

CREATE TABLE IF NOT EXISTS eval_runs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    git_sha       TEXT NOT NULL,
    -- Smoke-tier and full-set scores are different numbers over different
    -- case mixes. Storing the tier is what stops them being quoted as if they
    -- were comparable (D5).
    tier          TEXT NOT NULL DEFAULT 'full'
                  CHECK (tier IN ('full', 'smoke')),
    detector_cfg  JSONB NOT NULL DEFAULT '{}'::jsonb,   -- the model artifact
    precision     NUMERIC,
    recall        NUMERIC,
    f1            NUMERIC,
    mttd_p50_s    NUMERIC,
    mttd_p95_s    NUMERIC,
    passed        BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_eval_runs_recent
    ON eval_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_eval_runs_sha
    ON eval_runs (git_sha, started_at DESC);

CREATE TABLE IF NOT EXISTS eval_cases (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id         UUID REFERENCES eval_runs(id) ON DELETE CASCADE,
    case_name      TEXT NOT NULL,
    expected_edge  TEXT NOT NULL,
    detected_edge  TEXT,
    detected_at_s  NUMERIC,
    -- Four outcomes, and the constraint is deliberate: a fifth one invented at
    -- 2am to make a run look better would have to be argued for in a migration
    -- rather than slipped into a dict.
    --
    -- `wrong_edge` is separate from `false_positive` only so the failure stays
    -- legible afterwards. Both count against precision, because an alert
    -- pointing at the wrong service costs the same on-call hour as one
    -- pointing at an innocent one.
    outcome        TEXT NOT NULL CHECK (outcome IN
                   ('true_positive','false_negative','false_positive','wrong_edge')),
    detail         TEXT
);

CREATE INDEX IF NOT EXISTS idx_eval_cases_run
    ON eval_cases (run_id);
-- Per-case history across runs: "when did case 006 start failing" is the
-- question you actually ask, and it is a scan without this.
CREATE INDEX IF NOT EXISTS idx_eval_cases_name
    ON eval_cases (case_name, id);
