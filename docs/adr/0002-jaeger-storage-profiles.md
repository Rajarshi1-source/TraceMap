# ADR 0002 — Jaeger storage as a profile, not a hardcoded backend

- **Status:** Accepted
- **Date:** 2026-09-02 (Phase 2)
- **Deciders:** Rajarshi Basu

## Context

Jaeger v2.18 added native ClickHouse storage, and v2.20 is the current release.
ClickHouse is the backend this project wants: one server serves both trace
search and the analytics tier, and Jaeger's own benchmark reports roughly 8.6×
compression on 10 million spans.

The problem is that **ClickHouse support is alpha, behind a feature gate.** An
alpha backend can change its schema, change its configuration surface, or break
on a minor upgrade.

There is also a floor to consider: a laptop or a CI runner should be able to
boot the stack without standing up ClickHouse at all.

## Decision

**The storage backend is a profile selected by one environment variable.**

```
STORAGE_PROFILE=badger       laptop and CI - embedded, zero infrastructure
STORAGE_PROFILE=clickhouse   default in Phase 3 onward - the alpha backend
STORAGE_PROFILE=opensearch   fallback if the gate breaks on an upgrade
```

Three files under `jaeger/`, one variable in `.env`, and compose mounts the
matching config. **Badger is the default**, so a first boot needs nothing and a
ClickHouse problem can never be mistaken for an instrumentation problem — which
is precisely how Phase 2 was verified before ClickHouse existed.

Jaeger owns the `jaeger` database. TraceMap's analytics live in a separate
`tracemap` database on the same server. **We never write to Jaeger's tables and
never build materialized views on them**, because an alpha schema is free to
change underneath us. That separation is what keeps the analytics tier immune to
a Jaeger upgrade.

Elasticsearch is deliberately absent: v2.20 removed ES 6 support entirely, and
Jaeger now recommends OpenSearch over Cassandra at large scale.

## Consequences

**Good.** Depending on an alpha feature becomes defensible, because the failure
has a rehearsed answer rather than a hopeful one. Phase 7's CI matrix boots two
profiles, which turns "I have a fallback" into "my fallback is tested" — and an
untested fallback is worth less than none, because it invites false confidence.

**Bad.** Three config files to keep in step. They share the same pipeline and
differ only in the `jaeger_storage.backends` block, so drift is visible, but it
is still three files.

**Verified in Phase 2.** Only the badger profile is exercised so far: it boots
clean, serves the UI on :16686, and stores a complete five-service trace. The
ClickHouse and OpenSearch profiles are written but **not yet run** — claiming
otherwise would be exactly the untested-fallback mistake this ADR exists to
avoid. ClickHouse is exercised in Phase 3; both are matrixed in CI in Phase 7.
