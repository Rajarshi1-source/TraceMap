# TraceMap — Implementation Plan

**Executable build plan.** Derived from `IMPLEMENTATION_PLAN_REV3.md` (the design
authority) and the eight skills in `.claude/skills/` (the guardrails).

| Document | Answers | Authority over |
|---|---|---|
| `IMPLEMENTATION_PLAN_REV3.md` | *Why* — architecture, trade-offs, interview framing | Design decisions |
| `.claude/skills/*.skill` | *What must never be done* | Mandates, version pins |
| `schema-and-queries.md` · `eval-harness.md` · `starter-code.md` | *Exact artifacts* | DDL, golden set, scaffold |
| **This document** | *What to build, in what order, and how you know it is done* | Sequencing, exit gates |

Where this document and Rev 3 disagree, **this document wins** — the three
disagreements are in §2, and each one is a defect found by running the code, not
a matter of taste.

Audit performed 2026-09-02 against commit `abeef8d`.

---

## Contents

0. [Codebase audit — what exists today](#0-audit)
1. [Verification performed in this session](#1-verification)
2. [Findings that change the build](#2-findings)
3. [The mandate ledger — binding constraints](#3-mandates)
4. [Version pin ledger](#4-pins)
5. [Build phases](#5-phases)
6. [The project's own critical path](#6-critical-path)
7. [Risk register and pre-agreed cut lines](#7-risks)
8. [Decisions — resolved](#8-decisions)
9. [Definition of done](#9-done)

---

<a id="0-audit"></a>
## 0. Codebase audit — what exists today

I read every file in the repository before planning. Complete inventory:

| Path | Type | Lines | Role |
|---|---|---|---|
| `IMPLEMENTATION_PLAN_REV3.md` | Doc | 2,869 | Design authority, 29 sections |
| `starter-code.md` | Doc | 718 | Week 1–4 runnable scaffold |
| `schema-and-queries.md` | Doc | 391 | Full ClickHouse DDL + 4 queries |
| `eval-harness.md` | Doc | 344 | Golden set, runner, scorer, CI |
| `.claude/skills/*.skill` × 8 | Zip archives | — | `SKILL.md` + 3 embedded references |
| `.claude/skills/tracemap-skills-evals.json` | JSON | 56 | 3 triggering evals, 20 assertions |
| `.claude/settings.local.json` | JSON | 16 | Bash allowlist |

### The finding that shapes everything

**There is zero implementation code.** Not one service, schema file, config,
Dockerfile, test, or workflow exists. No `microservices/`, `platform/`,
`clickhouse/`, `eval/`, `dashboard/`, no `docker-compose.yml`, no `Makefile`.
Git history is two commits, both documentation.

That is good news, and it changes the plan's shape:

- **No migration, no legacy, no compatibility constraints.** Every mandate in §3
  can be honoured from the first commit instead of retrofitted.
- **The two mandates that are expensive to retrofit are free right now:**
  `service.version=${GIT_SHA}` on every span (Rev 3 §26 Q8 names backfilling it
  as the single thing the author would do differently) and the concurrent
  fan-out. Both land in Phase 1, before anything depends on them.
- **All 8 phases are greenfield.** Nothing in §5 is a modification.

### Documentation topology

The three reference documents exist **twice** — at the repo root and inside a
`.skill` archive. Compared by checksum:

```
IDENTICAL  schema-and-queries.md  <->  tracemap-clickhouse-analytics/references/
IDENTICAL  eval-harness.md        <->  tracemap-anomaly-eval/references/
IDENTICAL  starter-code.md        <->  tracemap-demo-services/references/
```

In sync today — and kept that way by hand during this session, which is exactly
the fragility that **§8 D4** now resolves: the root files become the single
source and the archives become generated artifacts.

---

<a id="1-verification"></a>
## 1. Verification performed in this session

I did not take the plan's correctness claims on trust. What I actually ran:

### 1a. The critical-path algorithm — 6/6 pass

Rev 3 §14c states the six tests "were executed against the implementation above
and all pass." I reproduced `critical_path.py` verbatim from §14b and ran all six
on **Python 3.14.6** — the exact runtime pin, not a nearby version.

```
PASS  test_critical_path_covers_the_root_span_exactly
PASS  test_orphan_root_survives_a_dropped_parent
PASS  test_parallel_fanout_blames_only_the_slowest_sibling
PASS  test_partial_overlap_excludes_the_overlapped_sibling
PASS  test_sequential_siblings_both_count
PASS  test_serial_chain_attributes_every_hop

6/6 passed
```

**Every constant in the flagship test is confirmed exactly:**
`order→payment = 720.0`, `order→product = 0.0`, `order→user = 0.0`,
`gateway→order = 735.0`. The parallel-sibling semantics hold — the zero-blame
edge is **present in the returned dict with value `0.0`**, not omitted. The
skill's warning (any test asserting `("a","b") not in edges` is wrong) is
correct, and the UI can rely on the row existing.

The algorithm in §14b is production-ready as written. **Copy it verbatim; do not
tidy it.** Three of the things you would be tempted to simplify — the
`kid.end_ns <= span.start_ns` guard, the `max(kid.start_ns, span.start_ns)`
cursor move, and the final `segments.reverse()` — are each load-bearing for at
least one of the six tests.

### 1b. Cycle safety — proven, not assumed

A worker that hangs on a malformed trace is worse than one that crashes, so I
checked whether a parent-pointer cycle can hang `_walk`.

**It cannot, and the reason is structural.** Every span has exactly one
`parent_span_id`, so a cycle forms a component containing no orphan. `_walk`
descends only through the `children` index, which is entered only from an orphan
root. A cycle is therefore unreachable from any start the algorithm can choose.
Empirically, a two-span cycle returns `[]` via the `if not roots` guard.

No cycle guard is needed. **Do not add one** — it would be dead code implying a
risk that does not exist.

### 1c. Recursion depth

With the interpreter limit lowered to 300, a 400-deep chain raises
`RecursionError` inside `compute_critical_path`, which the assembler already
catches. Depth 150 completes cleanly. At the default limit (1000) the failure
boundary is a chain of roughly 400 spans — far beyond anything a five-service
demo produces. The guard is correct; see F3 for the one line outside it.

### 1d. Schema declarations cross-checked

Confirmed by grep against both `IMPLEMENTATION_PLAN_REV3.md` and
`schema-and-queries.md` that `crit_ms_sum` is declared
`SimpleAggregateFunction(sum, Float64)` while `request_count` and `error_count`
in the same `CREATE TABLE` are plain `UInt64`. That inconsistency is F1.

---

<a id="2-findings"></a>
## 2. Findings that change the build

Four findings. F1 and F2 are material and must be fixed **before** the schema and
assembler are written — cheap now, expensive in week 6.

### F1 — 🔴 Roll-up counters are not aggregated on merge

`service_edges_1m` and `service_red_1m` are `AggregatingMergeTree` tables whose
`request_count` and `error_count` are plain `UInt64`:

```sql
ENGINE = AggregatingMergeTree
ORDER BY (source_service, target_service, time_bucket)
--
request_count    UInt64,                                 -- ⚠ plain column
error_count      UInt64,                                 -- ⚠ plain column
crit_ms_sum      SimpleAggregateFunction(sum, Float64)   -- ✅ correct
```

`AggregatingMergeTree` collapses rows sharing a sorting key and merges only
`AggregateFunction` / `SimpleAggregateFunction` columns. **A plain column keeps
one arbitrary value.** The incremental MV fires once per insert block, so a
single minute receives many rows with the identical sorting key
`(source_service, target_service, time_bucket)`. On merge, all but one
`request_count` is discarded.

The symptom is precisely the one Rev 3 §4 exists to prevent: no error, and a
dependency map that quietly under-counts under load. `Q1`'s `sum(request_count)`
sums whatever survived the merge, and `error_rate` — a ratio of two
independently mangled counters — is wrong in an unbounded direction. This is the
same defect class Rev 3 caught in Rev 2, one layer further down. That
`crit_ms_sum` is correct in the same statement shows the pattern was known and
simply not applied to the counters.

**Fix — both roll-up tables:**

```sql
request_count    SimpleAggregateFunction(sum, UInt64),
error_count      SimpleAggregateFunction(sum, UInt64),
```

**Verified 2 Sep 2026 on ClickHouse 26.3.** Three inserts into a single minute
for one edge, one of them an error, through the real MV into the real roll-up
table — run twice, once with each declaration:

| Declaration | request_count | error_count |
|---|---|---|
| ground truth (`trace_edges`) | 3 | 1 |
| `UInt64` (plain, as originally written) | **1** | **0** |
| `SimpleAggregateFunction(sum, UInt64)` | 3 | 1 |

The minimal reproduction is starker still — `plain=10, agg=20` from two identical
inserts of `10`.

**The error count collapsing to zero is the dangerous half**, and it is worse
than I first predicted. `error_rate` in Q1 is a ratio of two independently
mangled counters, so the red edge colouring would simply never fire. A demo
would show a healthy-looking green graph while payment-svc returned 5xx.

Status: **fixed in all three documents** — `schema-and-queries.md`,
`IMPLEMENTATION_PLAN_REV3.md` (with a visible correction note, since that file is
the one most likely to be copy-pasted from), and the
`tracemap-clickhouse-analytics` skill, which now carries the rule, the measured
table and a "what not to do" line.

### F2 — 🔴 Duplicate spans corrupt the assembler, not just percentiles

Rev 3 §20b and the ClickHouse skill both decide to **accept** duplicate spans
(ingestion is at-least-once) because "a duplicated span shifts a p99
imperceptibly." Sound for percentiles over `tracemap.spans`. **It does not hold
for the derived path**, and the assembler is on the derived path.

I reproduced it. A trace containing two spans sharing a `span_id`:

```
edges: {('gw', 'svc-b'): 85.0}     # the ('gw','svc-a') edge vanished entirely
```

Two distinct silent failures:

1. **`by_id = {s.span_id: s for s in spans}` collides.** The last duplicate wins,
   so a span's service, operation and timings can be silently replaced and blame
   moves to a different edge. Above, the `svc-a` edge disappears completely.
2. **The edge-row loop iterates `spans`, not `by_id`.** A duplicated span emits
   **two** rows into `trace_edges`, double-counting `request_count` for that edge
   and feeding its `duration_ms` into the TDigest twice.

**Fix — dedupe at the assembler's fetch boundary.** One clause, and it leaves the
"accept duplicates in raw storage" decision intact:

```sql
SELECT trace_id, span_id,
       argMax(parent_span_id,        timestamp) AS parent_span_id,
       argMax(service_name,          timestamp) AS service_name,
       argMax(operation_name,        timestamp) AS operation_name,
       argMax(start_time_unix_ns,    timestamp) AS start_time_unix_ns,
       argMax(end_time_unix_ns,      timestamp) AS end_time_unix_ns,
       argMax(status_code = 'ERROR', timestamp) AS is_error,
       argMax(service_version,       timestamp) AS service_version
FROM tracemap.spans
WHERE ...
GROUP BY trace_id, span_id          -- ← the fix
ORDER BY trace_id, start_time_unix_ns
```

This sharpens the interview answer rather than weakening it: *"I accept duplicate
spans in raw storage, because dedup costs merge CPU forever and moves a p99
imperceptibly. But I dedupe at the assembler boundary, because there a duplicate
doesn't nudge a percentile — it silently reassigns blame to the wrong edge."*
That is a more precise version of Rev 3 Q28.

### F3 — 🟡 One line sits outside the recursion guard

In the assembler (Rev 3 §14d), `compute_critical_path` is wrapped in
`try/except RecursionError`, but `attribute_to_edges` — which recurses in
`_depth()` at the same tree depth — is called outside it. In practice
`compute_critical_path` runs first and fails first, so this is nearly
unreachable. Cost of fixing: moving one line.

```python
try:
    segments = compute_critical_path(spans)
    if not segments:
        continue
    crit_by_edge = attribute_to_edges(spans, segments)   # ← inside the guard
except RecursionError:
    log.warning("trace %s too deep; skipping", trace_id)
    continue
```

### F4 — 🟡 Reference documents are duplicated

Three documents exist at the root *and* inside `.skill` archives, byte-identical
today. Editing either copy creates a silent fork, and the skill copy is the one
an agent loads. See §8 D4.

---

<a id="3-mandates"></a>
## 3. The mandate ledger — binding constraints

Every MANDATE and "what not to do" across the eight skills and Rev 3, in one
enforceable list. These are not preferences. Each row names the failure it
prevents, because a constraint without a consequence gets rationalised away at
2am in week 7.

| # | Constraint | Source | Failure it prevents |
|---|---|---|---|
| M1 | **The checkout fan-out must be genuinely concurrent** (`Promise.all`, never sequential `await`) | demo-services | Sequential fan-out puts every edge on the critical path; wall-clock and critical-path numbers become identical and **the flagship feature has nothing to demonstrate** |
| M2 | Never put a self-join in a **classic** materialized view on `spans` | clickhouse-analytics | MV `SELECT` sees one insert block; parent and child land in different batches and edges vanish silently |
| M3 | Roll-up MVs read **one table** only | clickhouse-analytics | Same as M2 |
| M4 | Use `AggregatingMergeTree` + `AggregateFunction`, never `SummingMergeTree`, for TDigest columns | clickhouse-analytics | Summing cannot merge sketches; percentile merging silently fails |
| M5 | **Parallel edges are written with `critical_ms = 0.0`, never omitted** | critical-path | The map must still draw an edge with real traffic and errors; a missing row makes it disappear instead of fade |
| M6 | Baseline updates **only on healthy observations** | anomaly-eval | Boiling-frog: folding anomalies into the EWMA makes slow degradation the new normal and blinds the detector |
| M7 | Detect on **critical-path p99**, wall clock secondary | anomaly-eval | Detecting on wall clock is the behaviour the project exists to improve on |
| M8 | Never tune `alpha` or thresholds without re-running the eval set | anomaly-eval | Changes justified by argument rather than measurement; the MLOps claim collapses |
| M9 | Never drop the null case to improve the scoreboard | anomaly-eval | The null case *is* the false-positive measurement |
| M10 | Never generate Jaeger **v1** YAML (`jaeger-agent`, `jaeger-collector` as separate binaries) | otel-pipeline | v1 is EOL since 31 Dec 2025; quoting it is disqualifying |
| M11 | Never claim **zero-code instrumentation for Go** | otel-pipeline | Go compiles with no runtime hook; a blanket claim is caught instantly by anyone who writes Go |
| M12 | `spanmetrics` generates **before** the sampling decision | otel-pipeline | RED metrics scaled by the sampling rate; dashboards lie |
| M13 | `loadbalancing` exporter with `routing_key: traceID` whenever gateway replicas > 1 | otel-pipeline | Partial traces reach each replica; sampling decisions become arbitrary |
| M14 | Keep the **probabilistic baseline** sampling policy | otel-pipeline | Storage holds only errors and slow traces; every downstream percentile and blame share is computed over a biased sample |
| M15 | `file_storage` persistent queue on every exporter | otel-pipeline | A collector restart during a storage blip drops exactly the spans that mattered |
| M16 | Never write to or build views on `jaeger.*` | otel-pipeline / clickhouse | The alpha schema changes underneath you |
| M17 | Never check dependencies in a **liveness** probe | platform-ops | One slow database fails liveness fleet-wide; a degradation becomes an outage |
| M18 | Never emit `node:20-alpine` or any EOL base image | platform-ops | Node 20 EOL April 2026 |
| M19 | Size hosts from **measured component floors (~7 GB)** | platform-ops | A 4 GB box OOMs |
| M20 | Auto-rollback ships **behind a flag defaulting to OFF**, with confidence floor 0.9, rate limit 1/service/hour, kill switch, audit trail | deploy-attribution | Remediating on a correlation signal turns one bad deploy into a rollback loop |
| M21 | Never assert causation in alert copy — "likely cause", with a confidence number | deploy-attribution | Correlation is not causation, and the alert must say so |
| M22 | Never trust a Slack button payload as a rollback target; validate server-side against `deploy_markers` | deploy-attribution | User input crossing a trust boundary into a cluster-mutating call |
| M23 | Faults are **runtime flags, never restarts**; default off; scoped; TTL-bounded; visible as `tracemap.fault.injected` | demo-services | A restart perturbs latency and measures itself; an invisible fault is indistinguishable from a real regression |
| M24 | `service.version = ${GIT_SHA}` on every span, from day one | otel-pipeline / deploy-attribution | Backfilling leaves a window of traces that can never be correlated to a release |
| M25 | Next.js **16.3**, App Router only; force graph in a client component; **persist node positions across refreshes** | nextjs-dashboard | 15 EOLs mid-project; a re-seeding layout makes a correct graph look broken |
| M26 | Never display stale data without the staleness banner | nextjs-dashboard | Silently stale numbers are worse than none |
| M27 | shadcn/ui components **copied into the repo**, never an npm dependency | nextjs-dashboard | — |
| M28 | Never strip parameters into `db.statement`; no PII or secrets in span attributes | demo-services / otel | Spans are retained for days and readable by anyone with dashboard access |
| M29 | Injectors' `stop()` must be **idempotent and called from `finally`** | anomaly-eval | A leaked fault poisons every subsequent case |
| M30 | Never add a sixth service to the **core** path | demo-services | Complexity in the traced app buys nothing the platform doesn't already show |

### The three that actually get violated

Most of the list is easy to obey once. Three are one careless refactor away and
deserve a CI check, not just a note:

- **M1** — a well-meaning "simplify the async code" commit turns `Promise.all`
  into sequential awaits and kills the demo with no test failing. Guard it: see
  Phase 1 exit criterion 5.
- **M6** — moving `_update_ewma` out of the non-breach branch during a refactor
  reads as harmless deduplication. Guard it with a unit test that feeds
  anomalous observations and asserts the baseline does not move.
- **M24** — trivially forgotten in one of five services, and only discovered in
  week 6 when attribution has a hole. Guard it: Phase 2 exit criterion 3 checks
  all five.

---

<a id="4-pins"></a>
## 4. Version pin ledger

Single source of truth. Verified upstream 2026-08-20 (Rev 3 §29); re-check
🔄 rows before quoting them in an interview.

| Component | Pin | Note |
|---|---|---|
| Jaeger | **v2.20.x** 🔄 | v1 EOL 31 Dec 2025. One binary, YAML roles |
| OTel Collector contrib | **v0.158.x** 🔄 | Needs contrib for `tail_sampling`, `spanmetrics`, `clickhouse`, `loadbalancing` |
| ClickHouse | **26.3 LTS** 🔄 | 25.8 LTS ended 29 Aug 2026 |
| OpenSearch (fallback) | **3.x** | Jaeger recommends over Cassandra at scale; ES 6 removed in v2.20 |
| Node.js | **24 LTS** | Node 20 EOL April 2026 |
| Python | **3.14** | Verified locally: 3.14.6 |
| Go | **1.26** | |
| PostgreSQL | **18** | |
| Redis | **8** | |
| Next.js / React | **16.3** / **19** 🔄 | 15 EOLs 21 Oct 2026 — inside the build window |
| D3 | **v7** | Force simulation |
| Prometheus / Grafana | **3.14** / **13.2** 🔄 | |
| k3s / Kubernetes | **1.36.x** 🔄 | 1.33 EOL; 1.34 EOLs 27 Oct 2026 |
| Helm | **3.x** | |
| Base images | `node:24-alpine`, `python:3.14-slim`, `golang:1.26-alpine` → `gcr.io/distroless/static-debian12:nonroot` | |
| Kafka (stretch only) | **4.x (KRaft)** | ZooKeeper *removed*, not merely optional |
| OBI / eBPF (D8) | **v0.10.x** 🔄 | Pre-1.0, breaking changes between minors — pin exactly |

### Tuning constants — one table, do not re-derive them per file

| Constant | Value | Where |
|---|---|---|
| Collector batch | 512 spans / 5s | agent tier |
| `decision_wait` | 10s | tail sampling |
| `num_traces` | 50,000 | tail sampling |
| Probabilistic baseline | 20% | tail sampling |
| `ASSEMBLE_INTERVAL_S` | 15 | assembler |
| `LATE_SPAN_GRACE_S` | 30 | assembler |
| Graph builder interval | 10s | worker + UI poll |
| `graph:current` TTL | 15s | Redis |
| Detector interval | 60s | worker |
| `alpha` | 0.2 | EWMA |
| `threshold_ms` | 50.0 | detector |
| `min_requests` | 100 | detector |
| `sustained_cycles` | 3 | detector |
| `warmup_cycles` | 5 | detector |
| `dedup_ttl_s` / `baseline_ttl_s` | 600 / 86,400 | Redis |
| `CORRELATION_WINDOW_S` | 900 | deploy attribution |
| Auto-rollback confidence floor | 0.9 | guarded rollback |
| Eval baseline | F1 0.85, precision 0.90, MTTD p95 180s | scorer |
| Eval tolerance | 0.03 (F1/precision), 1.2× (MTTD) | CI gate |
| Burn rate (page) | 14.4× — 2% of budget in 1h | Prometheus |
| MV/reconciler divergence alert | > 2% for 10m | Prometheus |

### The two derived budgets — quote these, do not invent them

**Detection budget (why `detect_within_s` is 180, not 60):**

```
collector batch                       5s
+ tail sampling decision_wait        10s
+ collector export batch              5s
+ assembler grace (30) + interval(15) 45s
+ detector interval 60 × sustained 3 180s
= ~245s worst case, ~120–180s typical
```

A case budget tighter than this measures your patience, not the detector.

**Staleness budget (span → pixel):** typical **~35–45s**, worst case **~105s**,
dominated by the sampling decision wait and the assembler grace window. State it
out loud; a named number beats hand-waving.

---

<a id="5-phases"></a>
## 5. Build phases

Eight phases on the Rev 3 §25 week grid, sequenced so **you always have a
demoable artifact**. Each phase lists the files it creates, binary exit criteria,
and the command that proves them.

---

### Phase 0 — Repo bootstrap *(day 1, before week 1)*

**Goal.** A repository skeleton where every mandate is already structurally
possible.

**Owning skills.** `tracemap-demo-services` (via `starter-code.md`),
`tracemap-platform-ops`.

**Files created**

```
tracemap/
├── .env.example            # from starter-code.md §2 — commit this, never .env
├── .gitignore              # .env, node_modules, __pycache__, .next, /data
├── Makefile                # demo, up, down, seed, schema, smoke, eval
├── docker-compose.yml      # profiles: core | full | eval
├── README.md               # skeleton; filled in Phase 8
├── LICENSE
└── docs/adr/               # empty; 6 ADRs land across phases
```

**Exit criteria**

1. `git rev-parse --short HEAD` flows into `.env` as `GIT_SHA` — the mechanism
   for M24 exists before any service does.
2. `docker compose config --profile core` parses with no error.
3. `make` with no target prints the help listing.

**Traps.** `.env.example` is committed; `.env` never is. Set `STORAGE_PROFILE=badger`
as the default so first boot needs zero infrastructure.

---

### Phase 1 — The traced application *(week 1)*

**Goal.** Five services exchanging real requests over docker-compose. **No
tracing yet** — this phase is about producing a call graph worth observing.

**Owning skills.** `tracemap-demo-services`.
**Reference.** `starter-code.md` §5–§11.

**Files created**

```
microservices/
├── gateway/      Node 24 / Express   POST /checkout, GET /products
├── order/        Node 24 / Express   POST /orders  ← THE CONCURRENT FAN-OUT
│   └── src/{routes/orders.js, clients.js, idempotency.js, db.js}
├── user/         Python 3.14 / FastAPI   GET /users/{id}
├── payment/      Python 3.14 / FastAPI   POST /charge  ← fault target
│   └── {main.py, faults.py}
├── product/      Go 1.26 / Gin       POST /stock/check  ← N+1 opportunity
└── schema.sql    PostgreSQL 18: users, products, orders, order_items, payments
load-testing/
├── normal-checkout-flow.js       70% checkout / 30% browse, diurnal ramp
└── induce-payment-slowdown.js
```

**Baseline latencies are load-bearing, not decoration.** `SVC_PAYMENT_BASE_MS=90`
must be the slowest and `SVC_PRODUCT_BASE_MS=45` a close second. That ordering is
what makes the wall-clock/critical-path toggle visibly disagree in Phase 5. If
you change them, the decoy eval case (Phase 7) must be re-validated.

**Exit criteria**

1. `POST /checkout` returns 2xx end-to-end through all five services.
2. k6 sustains 20 RPS with error rate < 1%.
3. `/healthz` returns 200 while the database is **down**; `/readyz` returns 503
   (M17, verified by actually stopping Postgres — not by reading the code).
4. Fault flags default to zero and are readable per request with no restart (M23).
5. **The fan-out is concurrent (M1).** The three child calls overlap in wall-clock
   time.

**Verify**

```bash
docker compose --profile core up -d --wait
curl -sf -X POST localhost:3000/checkout -H 'Content-Type: application/json' \
  -d '{"userId":"user-1","items":[{"sku":"SKU-1","qty":1}],"amount":1999}'
docker compose stop postgres && curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/healthz  # expect 200
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/readyz                                    # expect 503
docker compose start postgres
```

Concurrency cannot be verified from Jaeger yet, so assert it in code now and
re-assert it visually in Phase 2:

```js
// microservices/order/test/fanout.test.js — the M1 guard
test('fan-out is concurrent, not sequential', async () => {
  const t0 = Date.now();
  await createOrder({ userId: 'u1', items: [{ sku: 'SKU-1', qty: 1 }], amount: 100 });
  const elapsed = Date.now() - t0;
  // Sequential would be >= 30+45+90 = 165ms of downstream time.
  // Concurrent is bounded by the slowest leg (~90ms) plus overhead.
  expect(elapsed).toBeLessThan(150);
});
```

**Traps.** Sequential `await` (M1). Dependency checks in `/healthz` (M17).
Literal SQL parameters reaching span attributes later (M28) — strip them at the
DB layer now, while there is one place to do it.

---

### Phase 2 — Instrumentation and Jaeger *(week 2)*

**Goal.** One complete trace, five services, visible in the Jaeger UI.

**Owning skills.** `tracemap-otel-pipeline`.

**Files created**

```
otel/collector-config.yaml            # agent tier: memory_limiter, k8sattributes, batch
otel/collector-gateway-config.yaml    # gateway tier: tail_sampling, spanmetrics, exporters
jaeger/jaeger-badger.yaml             # STORAGE_PROFILE=badger  (this phase)
jaeger/jaeger-clickhouse.yaml         # STORAGE_PROFILE=clickhouse (wired in Phase 3)
jaeger/jaeger-opensearch.yaml         # STORAGE_PROFILE=opensearch (fallback)
docs/adr/0001-otel-over-vendor-sdks.md
docs/adr/0002-jaeger-storage-profiles.md
```

**Exit criteria**

1. One `POST /checkout` produces a single trace spanning all five services in
   the Jaeger UI at `:16686`.
2. **The three child spans of `order-svc` overlap in time** — the visual proof of M1.
3. **All five services** carry `service.version=${GIT_SHA}` (M24).
4. `spanmetrics` output appears in Prometheus with request rate matching actual
   load, not 20% of it (M12).
5. Tail sampling has ≥ 4 policies including the probabilistic baseline (M14).
6. Both exporters have `file_storage` sending queues (M15).

**Verify**

```bash
# 2 — the fan-out overlaps (the single most important check in this phase)
curl -s "http://localhost:16686/api/traces?service=order-svc&limit=1" \
  | jq -r '.data[0].spans[] | "\(.operationName) start=\(.startTime) dur=\(.duration)"'

# 3 — every service tagged, none missing
curl -s "http://localhost:16686/api/traces?service=gateway-svc&limit=1" \
  | jq -r '[.data[0].spans[].process.tags[] | select(.key=="service.version") | .value] | unique'
```

Staggered end-to-start spans in the first command mean the fan-out regressed —
**stop and fix it before Phase 3.** Everything downstream depends on it.

**Traps.** Jaeger v1 YAML (M10). `tail_sampling` in the agent tier — it would
decide on partial traces. Claiming zero-code for Go anywhere in the README (M11):
Go uses `otelgin.Middleware` explicitly, and the honest framing is that this
limitation is the motivation for the eBPF path.

---

### Phase 3 — ClickHouse analytics tier *(week 3)*

**Goal.** The same span queryable in Jaeger **and** in `tracemap.spans`, with
roll-ups that actually aggregate.

**Owning skills.** `tracemap-clickhouse-analytics`.
**Reference.** `schema-and-queries.md` — **with the F1 correction applied**.

**Files created**

```
clickhouse/schema/01_databases.sql
clickhouse/schema/02_spans.sql             # ORDER BY (trace_id, start_time_unix_ns)
clickhouse/schema/03_derived_tables.sql    # trace_edges, trace_critical_path
clickhouse/schema/04_materialized_views.sql  # ← APPLY F1 FIX HERE
clickhouse/schema/05_reconciler.sql        # refreshable MV, 60s, APPEND
clickhouse/schema/06_deploy_markers.sql
clickhouse/schema/07_retention.sql
docs/adr/0003-assembler-over-mv-selfjoin.md   # ⭐ the bug story
```

**Do this first, before writing any DDL:** run the F1 verification command from
§2 and record the result in `0003-assembler-over-mv-selfjoin.md`. If F1 holds,
declare both counter columns `SimpleAggregateFunction(sum, UInt64)`.

**Exit criteria**

1. All seven schema files apply cleanly to an **empty** ClickHouse.
2. The same trace is retrievable from Jaeger *and* from `tracemap.spans`.
3. `STORAGE_PROFILE=clickhouse` and `=badger` both boot clean from empty.
4. **The counter-aggregation check passes** — insert two blocks for the same
   minute and confirm `request_count` sums rather than keeping one value.
5. The reconciler MV shows `status = 'Scheduled'` in `system.view_refreshes`.
6. No materialized view contains a join on `spans` (M2, M3).

**Verify**

```sql
-- 4: the F1 regression check — run it as a test, not once by hand
SELECT source_service, target_service, sum(request_count) AS n
FROM tracemap.service_edges_1m
WHERE time_bucket >= now() - INTERVAL 5 MINUTE
GROUP BY source_service, target_service;
-- compare against the ground truth:
SELECT source_service, target_service, count() FROM tracemap.trace_edges
WHERE time_bucket >= now() - INTERVAL 5 MINUTE GROUP BY 1, 2;

-- 5: reconciler health
SELECT database, view, status, last_success_time FROM system.view_refreshes
WHERE database = 'tracemap';

-- 6: grep is the real test here
```

```bash
! grep -riE "JOIN" clickhouse/schema/04_materialized_views.sql && echo "M2/M3 OK"
```

**Traps.** `SummingMergeTree` for TDigest columns (M4). Building views on
`jaeger.*` (M16). Raising TTLs without recomputing disk from measured
bytes-per-span.

---

### Phase 4 — Critical path: the flagship *(week 4)* ⭐

**Goal.** `trace_edges.critical_ms` is `0.0` for a parallel sibling and non-zero
for payment. This is the thesis of the project appearing in a database row.

**Owning skills.** `tracemap-critical-path`.

**Build in this order — the test is the specification:**

1. `platform/tests/test_critical_path.py` — all six tests, verbatim.
2. `platform/src/core/critical_path.py` — verbatim from Rev 3 §14b (verified
   working in §1a; do not rewrite it).
3. `platform/workers/trace_assembler_worker.py` — **with the F2 and F3 fixes**.
4. `platform/workers/_runner.py` — shared supervision (`starter-code.md` §12).

**Files created**

```
platform/src/core/critical_path.py
platform/src/integrations/clickhouse_client.py
platform/workers/_runner.py               # never-die loop + heartbeat counter
platform/workers/trace_assembler_worker.py
platform/tests/test_critical_path.py      # the six tests
platform/tests/test_assembler_dedup.py    # ← new, guards F2
docs/adr/0004-critical-path-attribution.md
docs/LIMITATIONS.md                       # the four caveats, §14e
```

**Exit criteria**

1. All six critical-path tests pass on Python 3.14 (already proven achievable —
   §1a).
2. `trace_edges` contains a row with `critical_ms = 0.0` for the parallel sibling
   **and the row exists** (M5).
3. `trace_edges` contains a non-zero `critical_ms` for `order→payment`.
4. Edge count agrees with the reconciler within 2%.
5. **F2 guard:** a trace with a duplicated `span_id` produces exactly one edge row
   per real edge, and blame is unchanged.
6. The assembler emits a heartbeat metric every cycle.
7. `docs/LIMITATIONS.md` documents all four caveats — sampling bias, async spans,
   missing spans, clock skew.

**Verify**

```bash
python -m pytest platform/tests/test_critical_path.py -v   # expect 6 passed
```

```sql
-- 2 and 3 together: the moment worth celebrating
SELECT source_service, target_service,
       round(avg(duration_ms), 1) AS wall_ms,
       round(avg(critical_ms), 1) AS crit_ms
FROM tracemap.trace_edges
WHERE time_bucket >= now() - INTERVAL 5 MINUTE
GROUP BY 1, 2 ORDER BY crit_ms DESC;
-- expect order→payment with high crit_ms, order→product with crit_ms ≈ 0
-- and the order→product ROW PRESENT, not missing
```

**Traps.** Omitting zero-blame edges (M5) — the single easiest way to break the
UI's fade behaviour. "Tidying" the algorithm (§1a). Letting the worker die
silently — a dead assembler looks exactly like healthy quiet traffic.

> Exit criterion 2 is the one to celebrate. A `0.0` next to a 700ms wall-clock
> edge is the entire thesis showing up in a database row for the first time.

---

### Phase 5 — API and dashboard *(week 5)*

**Goal.** The map renders with real numbers, and the wall ⇄ critical toggle
visibly re-ranks edges.

**Owning skills.** `tracemap-nextjs-dashboard`, `tracemap-clickhouse-analytics`.

**Files created**

```
platform/src/main.py, api/, core/{query_engine,dependency_builder}.py, cache/
platform/workers/graph_builder_worker.py       # 10s, Redis single-flight lock
dashboard/src/app/{page.tsx, edge/[src]/[dst], traces/[id], anomalies, evals, cost}
dashboard/src/components/graph/DependencyGraph.tsx     # client component, D3
dashboard/src/components/graph/MetricModeToggle.tsx    # ⭐ the demo
```

**Exit criteria**

1. `GET /api/graph` returns in **< 100ms** cold, **< 5ms** warm (Redis hit).
2. The graph renders all five services with both metrics per edge.
3. **The toggle flips and product-svc visibly fades** while payment stays solid.
4. Node positions persist across the 10s refresh — no jump (M25).
5. The simulation freezes after convergence.
6. Empty state renders purposefully before any traffic, not a blank SVG.
7. The staleness banner appears when `stale: true` (M26).

**Verify**

```bash
curl -s -o /dev/null -w 'cold %{time_total}s\n' localhost:8000/api/graph
curl -s -o /dev/null -w 'warm %{time_total}s\n' localhost:8000/api/graph
docker compose stop clickhouse && curl -s localhost:8000/api/graph | jq '.stale'  # expect true
docker compose start clickhouse
```

**Traps.** Re-seeding the force layout every poll (M25) — the single most common
thing that makes a correct graph look amateurish. Rendering D3 in a server
component. Polling faster than the 10s graph-builder cadence. Installing
shadcn/ui as a dependency (M27).

---

### Phase 6 — Detector and deploy attribution *(week 6)*

**Goal.** Inject 60ms → a Slack alert inside the detection budget, with sample
trace links and a deploy verdict.

**Owning skills.** `tracemap-anomaly-eval`, `tracemap-deploy-attribution`.

**Files created**

```
platform/src/core/anomaly_detector.py       # 5 gates, EWMA
platform/src/core/deploy_attributor.py      # confidence decay
platform/src/core/alert_manager.py          # Slack blocks + SSE publish
platform/workers/anomaly_detector_worker.py # 60s
platform/workers/deploy_watcher_worker.py   # K8s watch + reconnect loop
platform/db/                                # alert_rules, anomaly_events
docs/adr/0005-ewma-over-static-threshold.md
```

**Exit criteria**

1. A 60ms injection produces an alert within the detection budget (~180s).
2. The alert leads with **critical-path** delta, wall clock second.
3. It carries 5 sample trace links, ordered by `critical_ms` **descending**.
4. It states either a correlated deploy with confidence, or explicitly
   "no deploy correlated" (M21) — never an omitted field.
5. **M6 guard:** feeding sustained anomalous observations does not move the
   baseline.
6. Detector heartbeat metric present; `absent_over_time` alert configured.
7. `AUTO_ROLLBACK_ENABLED` defaults to **false** (M20).

**Verify**

```python
# platform/tests/test_detector_baseline.py — the M6 guard, worth more than it looks
async def test_baseline_does_not_learn_from_anomalies():
    d = AnomalyDetector(redis)
    for _ in range(10):                      # warm on healthy traffic
        await d.evaluate(obs(critical_p99_ms=100.0, request_count=500))
    before = float(await redis.get("baseline:edge:a:b:cp99"))
    for _ in range(10):                      # sustained anomaly
        await d.evaluate(obs(critical_p99_ms=400.0, request_count=500))
    after = float(await redis.get("baseline:edge:a:b:cp99"))
    assert after == before, "boiling frog: baseline learned from an anomaly"
```

**Traps.** Moving `_update_ewma` out of the non-breach branch (M6). Detecting on
wall-clock p99 (M7). Ordering sample traces by `duration_ms` — the person who
clicks through then sees a trace where the edge was slow but parallel, and
nothing looks wrong. Treating a K8s scale event as a release.

---

### Phase 7 — Eval harness, CI gates, deploy *(week 7)* ⭐

**Goal.** `make eval` prints a scoreboard; CI blocks a deliberately broken
detector; a live HTTPS URL works.

**Owning skills.** `tracemap-anomaly-eval`, `tracemap-platform-ops`.
**Reference.** `eval-harness.md` (complete: 12 cases, runner, scorer, taxonomy).

**Files created**

```
eval/cases/001..012*.yaml     # incl. 001/002 null, 006 decoy, 011 deploy-correlated
eval/injectors/{base,latency,error,saturation,dep_kill,null}.py
eval/runner.py  eval/scorer.py  eval/check_regression.py
eval/baselines/main.json
charts/tracemap/              # Helm: probes, PDB, HPA, NetworkPolicy, RBAC
.github/workflows/{ci.yml, eval.yml, deploy.yml}
monitoring/prometheus-rules.yml  monitoring/grafana/
docs/adr/0006-eval-gates-ci.md
```

**Exit criteria**

1. All 12 golden cases run sequentially and produce one `eval_runs` row.
2. **Case 006 (decoy) passes** — the detector does not blame the parallel sibling.
   This is the case that proves D1 earns its place.
3. **Cases 001/002 (null) pass** — no alert on healthy traffic, including under a
   2× ramp.
4. Scores clear the gate: F1 ≥ 0.82, precision ≥ 0.87, MTTD p95 ≤ 216s.
5. **The gate is proven to fail.** Break the detector in a draft PR, screenshot
   the red check, keep the screenshot.
6. Storage-profile matrix (`clickhouse` + `badger`) both green in CI.
7. k6 SLO gate enforces p95 < 400ms and error rate < 1%.
8. Live HTTPS URL reachable from a phone; **write endpoints authenticated**.

**Verify**

```bash
make eval          # expect the scoreboard table, PASS
python -m eval.check_regression --baseline eval/baselines/main.json
```

**Traps.** Running cases in parallel — two concurrent faults make attribution
ambiguous. Skipping the 60s settling gap so case N's fault becomes case N+1's
baseline. A leaked injector (M29). Dropping the null case to improve the number
(M9). Budgets tighter than the ~245s pipeline reality.

> **If everything passes on the first run, suspect the harness before
> celebrating.** An eval suite that has never failed is evidence of an untested
> harness, not a working detector. Exit criterion 5 exists for exactly this
> reason.

---

### Phase 8 — Documentation and polish *(week 8)*

**Goal.** A stranger clones, runs one command, and sees a working graph.

**Files created**

```
README.md              # architecture diagram, one-command setup, screenshots,
                       # live URL, cost line, and the parallel-fanout test inline
docs/SCALING.md        # the ~7GB measured RAM floor table + "what breaks at 10×"
docs/CHAOS.md          # one real experiment with measured recovery time
docs/POSTMORTEM-001.md # ⭐ the MV bug, Google SRE format — highest-value file
docs/EVAL.md           # methodology + current scores
docs/COST.md           # the cost model
```

**Exit criteria**

1. `docker compose up` works from a clean clone **on a machine that is not
   yours** — test this literally.
2. `make demo` boots and pre-loads 60s of traffic; the dashboard opens populated.
3. All 6 ADRs written, including `0003-assembler-over-mv-selfjoin.md`.
4. 3-minute demo video linked from the README; screenshots committed.
5. Rev 3 §11 Flow 4 rehearsed to muscle memory.
6. Two stretch items shipped (recommend **D5 cost meter** + one of D4/D8).

**On POSTMORTEM-001.** Rev 3 frames it around the Rev 2 MV self-join bug. You
now have a second, first-hand candidate: **F1**, found by cross-checking column
declarations against engine semantics before writing a line of DDL. If F1 verifies,
write the postmortem about F1 — a bug *you* found in *your own* design document,
before it shipped, is a stronger story than one inherited from a prior revision,
because it demonstrates the review habit rather than the debugging habit.

---

<a id="6-critical-path"></a>
## 6. The project's own critical path

Fitting, given the subject. What genuinely blocks what:

```
P0 bootstrap
   └─► P1 services ──────────────────────────────┐
        │  (M1 concurrent fan-out)               │
        ▼                                        │
       P2 instrumentation ─────────────────┐     │
        │  (M24 service.version)           │     │
        ▼                                  │     │
       P3 ClickHouse ──► P4 CRITICAL PATH ─┴─────┴──► P5 dashboard
                              │                            │
                              ▼                            ▼
                         P6 detector ──────────────► P7 eval + deploy
                              │                            │
                              └──────────► P8 docs ◄───────┘
```

**The hard chain is P1 → P2 → P3 → P4.** Everything that makes this project
different lives downstream of Phase 4, and Phase 4 is worthless if M1 was
violated in Phase 1. That is why the fan-out check appears as an exit criterion
in **both** Phase 1 (in code) and Phase 2 (visually in Jaeger).

**What can run in parallel** — useful if you have uneven weeks:

- k6 scripts (P1) can be written any time after the endpoints are named.
- The Helm chart and Dockerfiles (P7) can be written during P5/P6 dead time.
- ADRs 0001–0004 should be written **in the phase that earns them**, not batched
  in week 8. An ADR written eight weeks later reads like one.
- `docs/LIMITATIONS.md` belongs in P4, while the caveats are fresh.

**What cannot be parallelised:** the 12 eval cases must run sequentially (§7),
and the detector cannot be tuned before P4 produces `critical_ms`.

---

<a id="7-risks"></a>
## 7. Risk register and pre-agreed cut lines

| # | Risk | Likelihood | Mitigation |
|---|---|---|---|
| R1 | Fan-out silently becomes sequential in a refactor | Medium | Unit test in P1 + visual check in P2; both are exit criteria |
| R2 | F1 real → counters wrong all the way to the demo | ~~Pending~~ **Confirmed & fixed** | DDL corrected in all three docs; Phase 3 exit criterion 4 keeps it fixed |
| R3 | Jaeger's alpha ClickHouse gate breaks on upgrade | Medium | Storage profiles + CI matrix; `STORAGE_PROFILE=opensearch` is one env var |
| R4 | Tail sampler OOM under load | Low at 20 RPS | `memory_limiter`; sizing formula in §4 |
| R5 | Host OOM on an undersized VPS | High if ignored | M19 — ~7 GB floor; CX32 or Oracle free tier |
| R6 | Eval suite never fails → false confidence | **High** | P7 exit criterion 5: break it deliberately |
| R7 | Week 7 time crunch | High | Cut lines below, decided now |
| R8 | Demo fails live | Certain, eventually | Pre-recorded video + committed screenshots |
| R9 | OBI (D8) breaks between minor releases | High | Pin exact tag; present as direction, not production |
| R10 | Auto-rollback loop | Low (off by default) | M20's five guards |

### Cut lines — decide these now, not at 2am in week 7

- **Behind at week 5** → drop the Go service; run 4 services in 2 languages.
  Nobody counts services. *(Cost: the M11 honesty story about Go gets weaker —
  keep the eBPF framing in the README anyway.)*
- **Behind at week 6** → drop deploy attribution (D3), keep the eval harness (D2).
  The harness is the rarer signal.
- **Behind at week 7** → run a 4-case smoke tier on PRs and the full 12 nightly.
  Say which tier produced any score you quote.
- **Never cut:** critical-path attribution (D1), the live URL, the README, the
  demo video. Those four *are* the project.

---

<a id="8-decisions"></a>
## 8. Decisions — resolved

All five are now decided. D1 was settled by running the check; the other four I
have called, with the reasoning, so nothing blocks the build. Reverse any of them
and only the noted consequence changes.

### D1 — Verify F1 before writing the schema? ✅ **Done**

Verified on ClickHouse 26.3. F1 is real, the fix is correct, and all three
documents plus the skill are rectified. See §2.

### D2 — Host: Hetzner CX32 or Oracle Always-Free?

**Decision: commit to Hetzner CX32** (4 vCPU / 8 GB / ~€7.05 per month). Oracle
stays documented as an opportunistic alternative, not the plan.

Three reasons, in order of weight:

1. **Oracle's free ARM tier is a capacity lottery**, and it is worst in the
   regions nearest this project. A demo that must work on interview day cannot
   depend on winning it.
2. **ARM means multi-arch for everything.** CI already plans `buildx`, but every
   base image, the ClickHouse build, and the OTel collector would each need an
   ARM path verified. That is real work bought with "free."
3. **€8/month is a better README line than €0** — it is the number that makes the
   cost-meter differentiator land ("this costs eight euros a month and tail
   sampling saves 68% of the storage").

⚑ **The consequence you must accept:** 8 GB against a measured ~7 GB floor is
only 1 GB of headroom. That is thin, so it comes with a binding condition —
**Prometheus and Grafana move to the `full` profile and are not resident during
the core demo.** That returns ~1.0 GB and makes the box comfortable rather than
marginal. If you later want them always-on, the honest answer is CX42 (16 GB,
~€14), not wishful thinking about the floor.

### D3 — Which two stretch differentiators?

**Decision: D5 (telemetry cost meter) + D6 (edge-level SLOs with multi-window
burn-rate alerting).** ⚠ *This changes my earlier recommendation of D5 + D8.*

What changed my mind: both D5 and D6 are almost entirely **assembly of things
Phase 3 and Phase 7 already build**, whereas D8 is net-new infrastructure with an
upstream dependency that warns of breaking changes between minor releases.

| | Marginal cost | Reuses | Risk |
|---|---|---|---|
| **D5** cost meter | 1–2 d | ClickHouse `system.parts`, collector metrics already scraped | Low |
| **D6** edge SLOs | 2 d | The Prometheus burn-rate rules already written in Phase 7 | Low |
| D8 eBPF/OBI | 2 d *nominal* | Nothing | **High** — pre-1.0, needs a privileged DaemonSet on the single k3s node |

D6 is also the better *story*: edge-level SLOs derived from **critical-path**
latency is a direct extension of the flagship, not a separate feature bolted on.
"I set an SLO on user-visible latency per dependency edge, not per service" is
something no OSS tool does, and it costs two days because the burn-rate machinery
already exists.

D8 is dropped to documented future work. **The Go honesty story does not need
it** — M11's framing ("Go compiles with no runtime hook, which is exactly why
eBPF is where the ecosystem is going") is a statement about the ecosystem, and it
is fully creditable without shipping a pre-1.0 DaemonSet. Week 8 has zero slack;
spending it debugging kernel privileges instead of recording the demo video would
be a bad trade.

### D4 — Resolve the duplicated reference docs?

**Decision: the three root documents are authoritative; `.claude/skills/*.skill`
become generated build artifacts.** ⚠ *This reverses my earlier recommendation of
making the skill copies authoritative.*

I changed my mind because I watched the failure mode happen during this session:
fixing F1 meant editing `schema-and-queries.md` at the root and then *remembering*
to copy it into the skill directory and repack the archive. An invariant that
depends on remembering is not an invariant.

The deeper reason is that **a zip cannot be reviewed.** A `.skill` archive does
not diff in a pull request, cannot be commented on line-by-line, and hides its
own drift. Source of truth belongs in text that a human reads and git can show.

Concretely:

```
skills/<name>/SKILL.md        ← source, diffable, reviewed
schema-and-queries.md         ← source, single copy
eval-harness.md               ← source, single copy
starter-code.md               ← source, single copy
        │  make skills  (copies references in, zips)
        ▼
.claude/skills/<name>.skill   ← build artifact, git-ignored or committed-but-generated
```

`make skills` rebuilds them; a CI step re-runs it and fails if the working tree
changes, which makes staleness impossible to merge. This is the same discipline
as a committed lockfile.

### D5 — Does the eval harness gate every PR, or only detector paths?

**Decision: a 4-case smoke tier on PRs that touch detector paths, and the full
twelve nightly on `main`.**

45 minutes on every PR trains you to route around CI, and a gate people avoid is
worse than no gate. But the earlier framing had the smoke tier as a *fallback if
behind* — that is backwards. Make it the default, because it is the right design
regardless of schedule.

The four smoke cases are chosen to cover each failure mode the gate exists to
catch, not simply the fastest four:

| Case | Covers |
|---|---|
| `001_null_baseline` | False positives — the measurement everyone forgets |
| `006_parallel_decoy` | The flagship; a wall-clock detector fails it by design |
| `003_payment_latency_60ms` | Latency sensitivity, the canonical brief case |
| `007_payment_errors_10pct` | The error path |

≈12 minutes, and it still fails a deliberately broken detector — which is what
Phase 7 exit criterion 5 has to demonstrate. **Any score you quote must name its
tier**; smoke-tier precision and full-set precision are different numbers and
conflating them is the kind of thing that gets caught.

---

<a id="9-done"></a>
## 9. Definition of done

The project is done when every box in Rev 3 §27 is ticked. These are the ones
that are load-bearing rather than administrative — if you are triaging, defend
these:

**Correctness**
- [ ] `test_parallel_fanout_blames_only_the_slowest_sibling` passes and is quoted in the README
- [ ] `trace_edges` has a `critical_ms = 0.0` **row** (present, not missing) for a parallel sibling
- [ ] No materialized view joins `spans`
- [ ] Roll-up counters verified to aggregate across insert blocks *(F1)*
- [ ] Assembler dedupes by `span_id` at the fetch boundary *(F2)*
- [ ] MV and reconciler agree within 2%

**The differentiators**
- [ ] Wall ⇄ critical toggle visibly re-ranks edges in under four seconds
- [ ] 12 golden cases including null and decoy
- [ ] CI gate proven to fail — screenshot kept
- [ ] Deploy markers on edge charts, correlation (or its absence) in alerts

**Operational honesty**
- [ ] Detector heartbeat alert (the silent-failure guard)
- [ ] Staleness banner when serving last-good data
- [ ] `docs/LIMITATIONS.md` with all four critical-path caveats
- [ ] `docs/SCALING.md` with the measured ~7 GB floor
- [ ] `POSTMORTEM-001.md` in Google SRE format
- [ ] Write endpoints authenticated on the public URL

**The demo**
- [ ] `make demo` opens on a populated graph, never an empty one
- [ ] 3-minute video recorded while things work
- [ ] Flow 4 rehearsed to muscle memory

---

## Appendix — what I verified vs. what I am asserting

Honesty about the provenance of each claim, so you know what to re-check.

| Claim | Status |
|---|---|
| Repo contains zero implementation code | ✅ Verified — full tree read |
| Six critical-path tests pass on Python 3.14.6 | ✅ Verified — executed |
| Flagship constants 720.0 / 0.0 / 0.0 / 735.0 | ✅ Verified — executed |
| Zero-blame edges are present with `0.0`, not omitted | ✅ Verified — executed |
| Parent-pointer cycles cannot hang the walk | ✅ Verified — reasoned + executed |
| Duplicate `span_id` corrupts edge attribution *(F2)* | ✅ Verified — reproduced |
| `attribute_to_edges` sits outside the recursion guard *(F3)* | ✅ Verified — read |
| Root docs and skill copies are byte-identical *(F4)* | ✅ Verified — checksummed |
| `request_count` is a plain column in an AggregatingMergeTree | ✅ Verified — grepped |
| Plain columns are not summed on merge *(F1's consequence)* | ✅ **Verified — reproduced on ClickHouse 26.3, both declarations** |
| All upstream version pins | ⚠️ Inherited from Rev 3 §29, checked 2026-08-20 — re-verify 🔄 rows |
