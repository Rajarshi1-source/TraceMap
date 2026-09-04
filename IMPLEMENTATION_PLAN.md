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

## Build status

| Phase | State | Evidence |
|---|---|---|
| **0 — Repo bootstrap** | ✅ **Done** (`5e22e2a`) | 3/3 exit criteria verified |
| **1 — Traced application** | ✅ **Done** (`c4653ee`) | 5/5 exit criteria verified against the running stack |
| **2 — Instrumentation + Jaeger** | ✅ **Done** | 6/6 verified; one 28-span trace across all five services |
| **3 — ClickHouse tier** | ✅ **Done** | 6/6 verified; 14 objects from an empty server, both storage profiles boot |
| **4 — Critical path ⭐** | ✅ **Done** | 7/7 verified live; product-svc 47ms wall clock, **0ms blame**, row present |
| **5 — API + dashboard** | ✅ **Done** | 7/7 verified; toggle fades the parallel edge, positions drift 1.14px across polls |
| **6 — Detector + deploy attribution** | ✅ **Done** | 7/7 verified live; 60ms fault alerted at **375s** — see F19 on the budget |
| **7 — Eval harness + CI ⭐** | ⚠️ **Built, not complete** | 2/8 criteria clean, 3 partial, 3 unmet. **F37 fixed** (blame-share gate); **F38 open** — 002 failed on its first-ever run. Full set attempted and **abandoned at 4/12 on host stalls (F36)**, so criteria 1/3 stay open. CI 14/14 green; **live URL not deployed**; no baseline committed |
| 8 — Docs + polish | Not started | |

Phase 1 measured: **19.67 req/s, 0.08% error rate, p95 128ms**. Checkout p95 sits
below the 165 ms that sequential downstream time alone would cost, so M1 holds at
system level as well as in the unit test.

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

## 2b. Found while building (Phases 0–1)

Three environment defects that the documents did not anticipate. All three are
fixed in the repository; they are recorded here because each would stop a fresh
clone dead, and the first two contradict what `starter-code.md` still says.

### F5 — 🔴 PostgreSQL 18 moved its data mount

`starter-code.md` §3 mounts the volume at `/var/lib/postgresql/data`. On
PostgreSQL 18 the container **refuses to start**:

```
Error: in 18+, these Docker images are configured to store database data in a
       format which is compatible with "pg_ctlcluster" ... there appears to be
       PostgreSQL data in: /var/lib/postgresql/data (unused mount/volume)
```

18+ expects a **single mount at `/var/lib/postgresql`** and stores data in a
major-version subdirectory, so `pg_upgrade --link` works without crossing a
mount boundary. Fixed in `docker-compose.yml`; `starter-code.md` still carries
the old path and should be corrected before Phase 3 copies from it.

### F6 — 🔴 `asyncpg` has no Python 3.14 wheel

`pip install asyncpg` on `python:3.14-slim` falls back to a source build and
fails with `error: [Errno 2] No such file or directory: 'gcc'`. Adding a
compiler to the runtime image is the wrong fix — it inflates the image and the
CVE surface for a build-time need.

Both Python services now use **`psycopg[binary,pool]` 3.2.10**, which ships
wheels, has first-class async support, and keeps the image slim. The pool is
bounded (`max_size=10`) per service, which is the bulkhead pattern the plan
already calls for.

### F7 — 🟡 The Go build was not reproducible

The first Dockerfile ran `go mod tidy` at build time with no committed
`go.sum`, so every build resolved dependency versions afresh. `go.mod` and
`go.sum` are now committed and the build runs `go mod download`, which verifies
against recorded checksums.

### Not a defect, but worth writing down

**GNU make is not bundled with Git for Windows.** The Makefile is the interface
every phase's exit criteria are expressed in. `winget install ezwinports.make`
provides it; the README says so.


---

## 2c. Found while building (Phase 2)

### F8 — 🔴 spanmetrics defaulted to delta temporality, so every counter read zero

The `spanmetrics` connector feeds the `prometheus` exporter. With the connector's
default aggregation temporality, each export carried only the change since the
last one, while the Prometheus exporter expects monotonically increasing
counters. The visible symptom was that every series **existed with the correct
labels and a value of `0`**:

```
gateway-svc | SPAN_KIND_SERVER | GET = 0
product-svc | SPAN_KIND_SERVER | GET /products = 0
```

That is a bad failure mode, because the metric looks configured. Nothing errors,
the series are present, and a dashboard renders a flat line at zero that reads as
"no traffic" rather than "broken pipeline". Every RED metric, every SLO in §24
and the whole k6 SLO gate in Phase 7 would have been built on it.

Fixed by pinning it explicitly:

```yaml
connectors:
  spanmetrics:
    aggregation_temporality: AGGREGATION_TEMPORALITY_CUMULATIVE
    metrics_flush_interval: 15s
```

After the fix, 100 requests produced exactly `GET = 100`.

### F9 — 🟡 `.env` pinned a stale `GIT_SHA`, silently mis-versioning every span

`make env` originally refused to touch an existing `.env`, so `GIT_SHA` stayed at
whatever HEAD was when the file was first created. Two commits later every span
still carried the old SHA — caught only because `trace-guard.py` compares
`service.version` against `git rev-parse`.

This matters more than it looks. **A stale `service.version` is worse than a
missing one**: Phase 6 deploy attribution would confidently name the wrong
commit, and the alert would look authoritative while pointing at an innocent
change. `make env` now refreshes the `GIT_SHA` line while preserving every other
setting.

### Two improvements the phase forced, worth keeping

**Health probes are dropped at the collector.** `/healthz` and `/readyz` fire
every few seconds per service and were the single highest-volume span in the
system — and the least useful. A `filter/drop_probes` processor removes them in
both pipelines. Doing it centrally rather than per-SDK keeps one rule across
three languages, and it materially reduces what Phase 3 has to store.

**Both containers run non-root against root-owned volumes.** Jaeger and the
collector both run as uid 10001, but a fresh named volume is created root-owned
because its mount path does not exist in either image. A one-shot `volume-init`
service fixes ownership before they start — the compose equivalent of the
initContainer the Helm chart will need in Phase 7. Running them as root would
have been one line shorter and wrong.


---

## 2d. Found while building (Phase 3)

### F10 — 🔴 The exporter owns its schema, and its sorting key is wrong for us

The plan assumed `tracemap.spans` would be written directly by the collector
with our column names. It cannot be. The ClickHouse exporter owns its wire
schema — PascalCase columns, `Duration` in nanoseconds — and, more importantly:

```sql
ORDER BY (ServiceName, SpanName, toDateTime(Timestamp))
```

That suits "show me this service's spans". It is wrong for the dominant read in
this project, which is the assembler asking *"give me every span in THIS
trace"*, thousands of times a minute. In ClickHouse the sorting key **is** the
primary index, so it has to match the dominant access pattern — the very point
§12b defends in the interview answer about sorting keys.

Resolved by owning both: `otel_spans` is a thin landing table matching the
exporter exactly, and `tracemap.spans` is ours with `ORDER BY (trace_id,
start_time_unix_ns)`, filled by a **single-table** projection view. That view is
a rename, not a stitch, so it cannot suffer the batch-boundary bug (M2/M3).

The exporter now runs `create_schema: false`, which is what makes exit criterion
1 — *all seven files apply cleanly to an empty ClickHouse* — actually true
rather than "true once the exporter has written something first".

Cost: spans land twice. `07_retention.sql` keeps the landing table for 24h
against 7 days for `spans`, so steady-state overhead is roughly 14%, not 100%.

### F11 — 🟡 The plan's criterion-6 grep fires on its own documentation

The plan phrases the M2/M3 gate as:

```bash
! grep -riE "JOIN" clickhouse/schema/04_materialized_views.sql
```

That fails the moment the file *documents* why a join must not be there — which
is the first thing anyone should write in it. Documenting a trap should not
fail the check for the trap.

`scripts/schema-guard.py` strips SQL comments first, then asserts on the DDL,
and checks four invariants rather than one: no join in a **classic** MV (the
reconciler's `REFRESH EVERY` self-join is correctly exempt), F1 counter types,
no `SummingMergeTree`, and nothing building on `jaeger.*`.

### The alpha backend behaved exactly as ADR 0002 predicted

`STORAGE_PROFILE=clickhouse` needed three things the documents did not name: a
config schema of `addresses` / `database` / `auth.basic` / `dial_timeout` /
`create_schema` (determined by probing the binary — an alpha config surface is
not something to guess), the `jaeger` database provisioned in advance, and

```
--feature-gates=storage.clickhouse
```

without which Jaeger refuses to start and says why: *"ClickHouse storage is
experimental and must be explicitly enabled. The schema is subject to breaking
changes."* That message is the justification for the whole profile mechanism.
`scripts/profile-matrix.sh` now boots each profile and asserts traces land, so
the fallback is tested rather than merely claimed.


---

## 2e. Found while building (Phase 4)

Four defects, all silent. None would have been caught by a green test suite over
synthetic data — every one needed the thing running against real traces.

### F12 — 🔴 A child span outliving its parent erased its entire subtree

The flagship number read **zero on the real demo path**, and nothing errored.

Express closes its `request handler` span a tick *after* the HTTP server span
that contains it:

```
order-svc POST (SERVER)                [ 5.0 .. 122.6]
  order-svc request handler /orders    [ 6.0 .. 122.8]   <-- 0.2 ms late
```

`_walk` skips any child ending after the cursor, because that is exactly how it
recognises parallel work. So a 0.2 ms overrun took the **whole subtree** with it:
payment, product and user all scored 0.0. Measured before and after on the same
trace: 9 segments → 30, and `order→payment` 0.0 ms → 98.1 ms.

Fixed with `normalize_spans()` in the assembler, which clamps every span inside
its parent's window. That is normalisation of malformed input, **not** a change
to the algorithm: `critical_path.py` stays verbatim and its six tests stay green.
A span outliving its own parent is not a fact about the system, it is an
artifact of two instrumentation layers rounding differently. Guarded both ways —
one test proves the subtree survives, another proves the clamp is a no-op on
well-formed traces so it cannot quietly rewrite good data.

### F13 — 🔴 The demo path was never actually always-sampled

The tail-sampling policy meant to guarantee `/checkout` is always kept:

```yaml
- name: keep-checkout-flow
  string_attribute: { key: http.route, values: ["/checkout"] }
```

**never matched.** The Express instrumentation sets `url.path=/checkout`, not
`http.route` — the latter is only populated when a route template is resolved.
So the one path the entire demo depends on fell through to the 20% probabilistic
baseline and was dropped four times in five, with nothing to indicate it.

Measured: 1 multi-service trace stored in five minutes. After keying on
`url.path` (with `http.target` retained for older SDKs): **13 stored for 12
sent**.

This had been visible since Phase 2 — `http.route` never appeared as a
spanmetrics dimension — and was noted but not chased. The lesson is the cheap
one: an attribute that is silently absent looks identical to an attribute that
is present and empty.

### F14 — 🟡 The same trace was assembled by consecutive cycles

24 of 40 distinct edges were written twice. Duplicate rows inflate
`request_count` in the incremental MV, quietly undoing the F1 fix that Phase 3
exists to guarantee. Fixed with a completion-time watermark. Note that a
`ReplacingMergeTree` would **not** have helped: its dedup happens at merge time,
long after the MV has already fired on both inserts.

### F15 — 🟡 Grace was measured on span time, not ingestion time

A span is already ~20 s old when it lands (collector batch + `decision_wait` +
export), so a 30 s grace on span time declares a trace complete while a later
batch is still in flight — and the assembler then attributes a critical path
over half a tree. Fixed by adding `ingested_at DEFAULT now()` to
`tracemap.spans` and asking the question that actually matters: *has any new
span for this trace arrived recently?*

### Two smaller ones, worth a line each

**`formatDateTime('%M')` returns the month name**, not minutes — ClickHouse
follows MySQL here. The watermark serialised as `19:September:24` and failed to
parse on the way back in. `toString()` is the correct call.

**The ClickHouse client leaked its password into every log line.** Credentials
were in the query string and `httpx` logs each request at INFO. Moved to
`X-ClickHouse-*` headers and quieted the logger — a URL is the thing that ends
up in logs, traces and error messages.


---

## 2f. Found while building (Phase 5)

### F16 — 🔴 Next.js standalone binds to `$HOSTNAME`, which Docker sets

The dashboard served fine through its published port and **failed its own
healthcheck**. Next's standalone `server.js` binds to `process.env.HOSTNAME`,
and Docker sets `HOSTNAME` to the container ID — so the server bound to the
container's own hostname rather than all interfaces. Anything connecting to
`127.0.0.1` inside the container, which is exactly what a healthcheck does, got
`ECONNREFUSED`.

The dangerous part is that it looks fine from outside. Fixed with
`ENV HOSTNAME=0.0.0.0`.

### F17 — 🟡 Aliasing an aggregate to its own column name

`sum(error_count) AS error_count` makes ClickHouse re-resolve the alias inside
the next aggregate and reject the query:

```
Code: 184. Aggregate function sum(error_count) AS error_count is found
inside another aggregate function
```

Hit twice — once on the counters, once on `crit_ms_sum`. Distinct alias names
(`requests`, `errors`, `crit_total_ms`) fix it. Cheap, but it takes the whole
graph query down, so it is worth the note.

### F18 — 🟡 Positions were only saved on `sim.on("end")`

d3-timer runs on `requestAnimationFrame`, which browsers throttle to nothing in
a hidden tab. A graph that loads in a background tab therefore may never reach
`"end"` — so positions were never recorded, and the layout re-seeded from
scratch when the user came back. That is precisely the jump M25 exists to
prevent, arriving by a route the mandate does not mention.

Positions are now written on every tick. Found because the verification itself
ran against a hidden browser pane and measured zero laid-out nodes, which is a
nice argument for testing where the user actually is rather than where it is
convenient.

### Two measurement notes, not defects

**The warm-latency budget is measured twice.** In-container median is
**2.51 ms**; from the host through Docker's port-forward it is ~9 ms. The
in-container figure is the one the `< 5 ms` criterion is about — the forward is
a Windows Docker artifact, not application latency. Cold (live ClickHouse) is
**52 ms** against a 100 ms budget.

**`crit_p99` is outlier-sensitive, by design.** Over 26 traces, `order→product`
was on the critical path in exactly **one** — a trace where product genuinely
finished last. That single trace sets the p99 at 47 ms while the mean is 1.8 ms
against payment's 95.2 ms. The attribution is being honest per-trace rather than
applying a blanket rule, which is the correct behaviour; it is worth knowing
before quoting a p99 in a demo.


---

## 2g. Found while building (Phase 6)

Four findings, three of them the same bug wearing different clothes: **the
baseline quietly absorbing the fault**. M6 guards one route in; measuring found
three more.

### F19 — 🔴 The documented detection budget is optimistic by ~2x

Measured injection-to-alert: **375 seconds**. The build plan says ~180s;
`eval-harness.md` says ~245s worst case. Both omit the assembler lag and bucket
completion:

```
  assembler lag (trace -> roll-up)        ~54s   MEASURED
+ bucket must complete                    ~60s
+ detector notices on its next cycle      ~60s
+ 3 breaches at 60s each                 ~180s
                                        = ~354s
```

**Phase 7 must widen `detect_within_s` to ~400s before the harness is trusted.**
As written, `003_payment_latency_60ms` has `detect_within_s: 180` and would score
a working detector as a false negative — and the natural response to a red eval
is to weaken the detector rather than fix the budget.

### F20 — 🔴 A rolling window turns a step fault into a ramp

A p99 over a 5-minute rolling window blends pre- and post-fault requests. Each
cycle's delta lands under the threshold, the EWMA legitimately learns it, and
the baseline chases the fault into place **without a single cycle ever
breaching**. Measured: a 60ms injection never fired in 422s while the baseline
drifted 113 → 123.6.

A 1-minute rolling window does not fix it — the pipeline lag is ~54s, so
`now() - INTERVAL 1 MINUTE` excludes the newest bucket and the detector reads
`nan`. The detector now evaluates **a single minute-bucket**, which is
homogeneous in time and therefore cannot dilute.

### F21 — 🔴 The baseline armed 28ms high, eating the signal

Seeded from one observation; if that sample is an outlier, `alpha=0.2` cannot
walk it off in five warmup cycles. Measured: seed on a 146ms outlier against a
true ~110ms level armed the baseline at **139.5ms**, so a real 65ms fault
presented as a **40ms delta** — under the 50ms threshold, undetected.

The detector was correct; the baseline was wrong. Fixed with two regimes of the
same estimator: `warmup_alpha = 0.5` converges fast, `alpha = 0.2` resists noise
once armed. Baseline now arms within 3ms of truth.

### F22 — 🟡 "Three consecutive" is too brittle for a real percentile

With a correct baseline, a +60ms fault gives ~57ms delta against a 50ms
threshold, and bucket noise is **±20ms**. A single dip resets a consecutive run:
the counter reached 2, reset, reached 1, reset, undetected for 472s. Changed to
**3 breaches within 5 cycles** — same evidence bar, tolerant of real noise.

That opened a subtler hole worth naming: under a noisy sustained fault the
occasional sub-threshold cycle is *not* a breach, so it teaches the baseline —
**M6 satisfied to the letter, defeated in practice.** The baseline now moves only
when the *entire recent window* is clean.

### The M6 guard was proven, not assumed

Applying the exact edit the comment warns against — hoisting `_update_ewma` out
of the healthy branch — made the baseline chase the fault **100.0 → 367.8**, and
the guard failed with a legible message. A guard nobody has seen fail is a guard
nobody should trust.

---

## 2h. Found while building (Phases 7–8)

Twenty findings. **Eleven in the harness, two in the detector, two in the
environment it needs, four found in Phase 8 by running the system rather than
reading it, and one still open (F38)** — which is the phase working as intended.
An eval suite that has never failed is evidence of an untested harness, not of a
working detector, and four of these were found by the harness scoring a correct
detector as broken.

The three from Phase 8 are worth separating, because each was found by a
different kind of exercise and none by a test:

- **F39** — a dispatched CI run died in preflight. Two config files that each
  looked correct alone.
- **F40** — a chaos experiment falsified a hypothesis this repository had
  asserted. Two constants that had never been read next to each other.
- **F41** — the flagship number was wrong on 4.83% of rows on the edge that
  matters most, and every average looked fine. Found by checking an invariant
  against live data on a hunch.
- **F42** — the guard protecting the golden set's central assumption was reading
  a hardcoded dict, so it could not fail. Found by reading the CI log of a run
  that had already gone red for what looked like a different reason.

Three deserve reading even if the rest are skipped. **F28** is a gate a correct
detector could never pass. **F30** is a decoy that punished the detector for
being right. **F29** is the one that would have been tuned away rather than
understood. **F32** is the one that justifies the phase: a whole missing
capability, found by asking the system to do something rather than by reading
its code.

### F23 — 🔴 The reference case 009 names an edge that cannot exist

`eval-harness.md` specifies case 009's expected edge as
`gateway-svc->user-svc`, and lists it as a trap in three other cases. There is
no such edge. The concurrent fan-out lives in `order-svc`, so `user-svc` is a
child of order-svc and gateway-svc has never called it:

```
gateway-svc ── order-svc ──┬── user-svc      30ms
            └─ product-svc │   product-svc   45ms   (also a direct browse edge)
                           └── payment-svc   90ms
```

The case would have run, injected a real 150ms CPU burn, watched for an alert
on an edge that cannot produce one, and scored a working detector as a **false
negative** — and the natural response to a red eval is to weaken the detector.
As a trap in cases 003/004/007/008 it was worse than useless: a trap that can
never fire flatters precision for free.

Corrected to `order-svc->user-svc`. The deeper fix is that an edge name is a
string and strings do not fail loudly, so `eval/topology.py` now declares the
service graph once and `test_edges_exist` checks every case against it.

### F24 — 🔴 `type: null` was not the null injector

YAML 1.1 parses bare `null` as `None`. Both null cases therefore had
`injector.type == None` — no injector type at all — while still loading, still
running, and still printing a passing row. The one case whose entire job is to
measure false positives was the one case not running through the injector path
it was supposed to share with the other eleven.

Quoted to `type: "null"`, and `test_case_schema` now asserts the type resolves
to a registered injector. Caught by the case-file tests on their first run,
which is the cheapest place it could have been caught and roughly the last.

### F25 — 🔴 The reference percentile indexes off the end of the list

```python
p95 = lat[int(len(lat) * 0.95)]          # eval-harness.md
```

In range for most lengths, `IndexError` whenever `len * 0.95` is a whole
number — n = 20 and n = 40 among them. Twelve cases with ten detections is
uncomfortably close, and a crash in the scorer reads as a broken harness at
exactly the moment the number matters.

Replaced with nearest-rank using `ceil`, **not** `round`: Python's `round` is
banker's rounding, so a rank landing exactly on `.5` alternates between two
different answers depending on the parity of the case count. A percentile that
depends on how many cases you happened to run is not a percentile.

### F26 — 🟡 The reference runner gave traps a shorter window than the expected edge

```python
if detected_edge and tripped_trap:
    break
```

The loop stops only when it has **both**. So a clean case runs its entire
budget while a case whose trap fires late never sees it — the trap gets a
shorter observation window than the thing it is supposed to catch, and
precision comes out flattered by construction.

Now a detection starts a bounded 90-second trap-grace window (one full detector
cycle plus margin), so both get a comparable look without paying the full
budget on every case.

### F27 — 🟡 user-svc could not answer "am I clean?", and that read as clean

Every other service exposes `GET /_internal/fault`. user-svc had only POST and
DELETE. The all-clear sweep that enforces M29 asks each control plane for its
state before the first case and after the last, and the sweep's check was
`status_code == 200 and active` — so a **405 was silently an all-clear**.

The one service outside the guard would have been the one where a leaked fault
survived. Both halves fixed: the handler exists, and the sweep now treats any
non-200 as *unaudited* rather than *clear*.

### F28 — 🔴 The detection budget is 425s, and exit criterion 4's MTTD ceiling was unreachable

F19 measured 375s injection-to-alert against a documented ~245s. Writing the
budget out term by term shows the two the original omitted:

```
  collector batch                          5s
+ tail sampling decision_wait             10s
+ collector export batch                   5s
+ assembler late-span grace               30s
+ assembler interval                      15s
+ minute-bucket completion                60s   <- omitted
+ detector confirmation (3-in-5 x 60s)   300s   <- counted as 180s
= 425s worst case
```

Exit criterion 4 asks for **MTTD p95 ≤ 216s**, which is 180 × 1.2. But 180s is
the detector's own confirmation floor — three cycles at sixty seconds —
*before any pipeline time is counted at all*. The criterion is arithmetically
unreachable by a correct detector.

**Amended to 432s** (the 425s pipeline worst case, within the existing 1.2×
tolerance), documented in ADR 0006. The F1 ≥ 0.82 and precision ≥ 0.87 criteria
are unchanged and were met as written.

This is the finding most worth stating plainly, because the alternative was
available and tempting: every term in that budget except the last two is an
environment variable, and turning them down would have bought ~40 seconds and a
green criterion. `docker-compose.eval.yml` does the opposite — it **pins** the
detector configuration to the shipped values, so a stray `.env` cannot change
what is being measured. The only claim worth making about an MTTD number is
that it was measured against the configuration that ships.

### F29 — 🟡 A null case measures the host, not just the detector

The first live smoke run put a **false positive on case 001**:
`gateway-svc->order-svc`, baseline 153ms, observed 600ms, delta +447ms, with
nothing injected. The detector log shows the breach sequence that produced it —
`+62.9ms`, `+1019.7ms`, `+411.1ms`, `+108.3ms` — a real, sustained latency
excursion on the root edge.

The detector was right. The checkout genuinely was slow. The run was executed
on a development laptop that was concurrently building container images and
running the test suite, and a **+1020ms** excursion is host contention, not
detector noise.

That makes it a finding about the harness's requirements rather than about the
detector: **the null case is only a false-positive measurement on a quiescent
host.** On a contended one it measures contention, and the number it produces
looks exactly like a precision regression — which invites tuning alpha to fix a
problem that is not in the detector (M8).

Two consequences, both taken: the eval workflow boots a clean stack on a
dedicated runner rather than sharing one, and any quoted precision figure has
to name the environment as well as the tier. The score below is reported as
measured, not re-run until it flattered.

### F30 — 🔴 The reference decoy service is not a parallel sibling here

The decoy case makes one fan-out leg slow *in parallel* with a slightly slower
critical one, and requires the detector to blame only the critical one. The
reference harness uses **product-svc** as the decoy. Run against this
application it fails, and it fails in the worst available way: the detector
does exactly the right thing and the case scores it a false positive.

Measured over ~11,000 traces of healthy load, per fan-out leg:

| edge | critical on | crit p99 | wall p99 |
|---|---|---|---|
| `order-svc->payment-svc` | 87.3% | 157ms | 154ms |
| `order-svc->product-svc` | **12.4%** | **49.3ms** | 56ms |
| `order-svc->user-svc` | 0.3% | **0.0ms** | 56ms |

product-svc has an N+1 query path that fires on carts above
`PRODUCT_N_PLUS_ONE_THRESHOLD`, and roughly a fifth of generated carts cross
it. On those traces product genuinely *is* the slowest leg and genuinely does
carry blame. So injecting delay into it raises its critical p99 by the injected
amount — measured live: +120ms moved it from 49.3 to 168.8 and tripped the trap
at t+202s.

**A service that already carries critical-path blame cannot be a decoy at any
delay.** The injected time lands on the critical path in full.

user-svc is the real parallel sibling: critical on 0.3% of traces at a critical
p99 of exactly 0.0. Case 006 now injects +100ms there against +150ms on
payment, and the invariant is encoded rather than assumed —
`CRIT_P99_BASELINE_MS` in `eval/topology.py` holds the measured table, and
`test_the_decoy_case_survives` requires both that the decoy's existing critical
share is under the threshold and that the delay margin exceeds it. Fed the old
product-svc configuration it fails with the actual reason:

```
AssertionError: the 30ms margin must exceed the decoy's 49.3ms existing
critical share, or it wins on the traces where it already won
```

The general lesson is about the statistic, not the service. The first design
compared *mean* service latencies — 45ms product against 90ms payment looked
like a comfortable margin — when the question the critical path actually asks
is about the **tail**. product's mean says sibling; its p99 says contender.

### F31 — 🔴 Redis is unpublished, so suppression was never reset between cases

`reset_alert_suppression()` clears `anomaly:dedup:*` between cases, because the
detector suppresses a repeat alert on the same edge for 600s and several cases
share an edge. The runner executes outside the stack, and Redis has no host
port in the `core` profile — so every reset failed, was caught, logged, and the
run continued with `cleared 0 suppression keys`.

The consequence is measurable rather than theoretical. Case 003 alerted on
`order-svc->payment-svc` at 20:28:46, setting a dedup key until 20:38:46. Case
006 needed the same edge and got it at **t+444s instead of ~t+200s** — the
alert appeared one second after the TTL expired. A case ordered slightly
differently would have scored a false negative outright.

Two fixes, and the second matters more. The overlay publishes Redis, since the
harness legitimately needs it and the overlay is never the production topology.
And preflight now **refuses to run** when Redis is unreachable rather than
warning: a run that cannot reset suppression still prints a scoreboard, and
that scoreboard is measuring Redis while reading as a detector regression. A
wrong number is worse than a missing one.

### F32 — 🔴 The detector had no error path at all, and the harness is what found it

`EdgeObservation` has carried an `error_rate` field since Phase 6. Nothing ever
read it. `evaluate()` compares critical-path p99 against its baseline and does
nothing else, so **the detector could not see an edge failing half its
requests**.

Latency cannot substitute. An injected 5xx returns *faster* than a success —
payment-svc raises before the database write — so a 50% error rate moves
critical-path p99 by approximately nothing. Eval cases 007 and 008 were
therefore structurally unpassable: the detector was working exactly as designed
on the metric it had, and the golden set was asking about a metric it did not
have.

This is the finding that justifies the phase. A tuning gap would have been
found by reading the code; a missing capability was found by asking the system
to do something and watching it fail. Nothing else in seven weeks surfaced it —
`alert_rules` has had an `edge_error_rate` rule type since Phase 6 and the
owning skill lists an error family in its case table, so the design always
anticipated it and the implementation quietly did not.

Closed with `evaluate_error_rate()`, deliberately a **separate method with its
own history and dedup keys** rather than another branch inside `evaluate()`.
Two reasons. The latency path is the flagship and is not worth destabilising to
save a method. And sharing a dedup key would mean a firing latency alert
suppresses a genuine error alert on the same edge for ten minutes — two
different failures, one silently never reaching anyone.

The one design decision worth defending: **errors get a fixed threshold, not an
EWMA baseline.** The asymmetry with latency is the point. "Normal" latency is
genuinely environment-dependent — 3am differs from 3pm — so a baseline that
learns is the right tool. "Normal" error rate is not: zero is the target. An
EWMA would mean a service that has failed 5% of requests all week reads as
healthy, which is the boiling frog again, starting from a position that was
never acceptable. 2% against a measured healthy floor of ~0.08%.

M8 is satisfied rather than bypassed: the change is justified by an eval
number — two cases that could not pass and now can — not by an argument.

### F33 — 🟡 psycopg cannot run async on the Windows default event loop

The first run finished, printed a scoreboard, and recorded **nothing**. The log
said `postgres unreachable; eval history disabled`, which sends you to check
the database.

Postgres was fine. Windows defaults to the Proactor event loop and psycopg's
async mode cannot run on it, so the pool never opened. Every `eval_runs` row —
the experiment tracking that the entire MLOps framing rests on — was silently
lost, and the error message actively pointed away from the cause.

Fixed with `asyncio.run(..., loop_factory=asyncio.SelectorEventLoop)` on
`win32`, using `loop_factory` rather than `set_event_loop_policy` because the
policy API is deprecated in 3.14 and removed in 3.16. CI is Linux and takes the
`None` branch.

The more useful half of the fix is the message. It now names the event loop and
the remedy instead of blaming the network — a diagnostic that points at the
wrong subsystem costs more than no diagnostic, because it is followed.

### F34 — 🟡 A native Postgres on the host shadowed the container

With the event loop fixed, the connection then failed authentication — against
a server whose credentials were correct. A local `postgresql-x64-17` Windows
service owns port 5432; compose published the container's 5432 anyway and
reported success, and every connection from the host authenticated against the
wrong database.

This is worse than a port conflict. A conflict fails loudly at boot. This
succeeds, and the harness talks to a server that has never heard of
`eval_runs`.

The eval overlay now publishes the datastores on `55432` and `56379`, and the
`make eval*` targets point the runner at them. The overlay is the right place:
it is never the production topology, and the harness is the only thing that
reaches these from outside the compose network.

Worth generalising. Both F33 and F34 are "the harness runs on whatever machine
is to hand" problems, and neither would ever appear in CI. A harness that only
works on the maintainer's laptop is a harness nobody else can reproduce a score
with — and reproducibility is the whole claim.

### F35 — 🔴 One slow API response destroyed a 45-minute run

The clean re-run reached the null case, polled the anomalies API, and died on a
single `httpx.ReadTimeout`. Forty-five minutes of warm baselines and injected
faults gone to one slow HTTP response on a loaded host.

The naive fix — swallow poll errors and carry on — is worse than the crash. An
unreachable API returns no anomalies, so every remaining case scores as a false
negative and the run produces a **plausible-looking scoreboard describing a
detector that was never asked anything**. A wrong number beats a missing one
only in the sense that it is harder to notice.

So the loop counts consecutive failures: one timeout is a busy host, six in a
row (a minute at the 10s poll interval) is an API that is gone, and the run
abandons rather than inventing false negatives. The read timeout went from 15s
to 30s, because the API aggregates across ClickHouse on every call and a loaded
host makes that slow well before it makes it broken.

Three smaller consequences, all pointing the same way — **an incomplete run is
not a measurement**:

- The cases that *did* complete are kept and scored. Losing completed work to a
  later exception is how a harness earns a reputation for flakiness and stops
  being run.
- The scoreboard prints `INCOMPLETE: n/m cases` directly under the numbers,
  where the number is, rather than only in a log line above it.
- The run is **not** written to `eval_runs`, and the exit code is non-zero
  whatever the partial scores say. That table is a record of measurements, not
  of attempts, and a partial run in it would silently become a baseline.

### F36 — 🔴 The host stalls, so this machine cannot produce a trustworthy MTTD

The clean run's null case was budgeted at 600s and took **834s** of wall clock.
The cause is visible in the poll timestamps: 41 polls, 34 of them exactly 10s
apart as designed, one gap of 60s (the inter-case settle) — and **one gap of
471 seconds**.

Nothing failed. No poll error, no timeout, no exception. A process whose loop
does nothing but `sleep(10)` and a 50ms HTTP call lost roughly eight minutes,
during a window when nothing else was being run against the machine. The 30s
client timeout did not fire, which is itself informative: a timer that never
fires is a process that was not being scheduled, not a request that was slow.

The mechanism is not established and is not worth asserting. The consequence is:

**Outcome classifications from this host are trustworthy; timings are not.**

Which edge got blamed is a statement about the data the detector saw, and a
stall does not change it. `detected_at_s` is measured against
`time.monotonic()`, which keeps advancing through a stall, so every detection
latency is inflated by however much wall clock the process lost.

That decides what may be committed. A baseline is `(f1, precision,
mttd_p95_s)`, and the third of those would be inflated — which makes the gate
**more lenient than intended**, the one direction a gate must never drift by
accident. A too-tight baseline goes red and gets investigated; a too-loose one
passes real regressions silently and nobody ever looks.

So no baseline is recorded from this machine, on either tier, whatever the run
scores. `eval/baselines/README.md` documents the bootstrap: the first green
run on a dedicated CI runner records it.

This is F29 in a more expensive costume, and together they are the argument for
the eval workflow booting a clean stack on its own runner rather than sharing
one. Worth stating plainly rather than burying: **the harness is finished and
the numbers it produced here are not publishable.** Reporting a measurement the
environment could not support is the failure the whole phase exists to prevent,
and it would be a strange thing to commit at the end of it.

### F37 — 🔴 A p99 decoy trap is unsatisfiable for any delay large enough to matter — FIXED

The decoy case was rebuilt once already (F30) on a measurement, and it failed
again on the clean run. The second failure has a different and more general
cause, and it is the most interesting result of the phase.

**The measurement.** `order-svc->user-svc` has a baseline critical p99 of
exactly **0.0** — it is on the critical path for 0.3% of traces. Inject +100ms
into user-svc while payment-svc takes +150ms, and:

| | value |
|---|---|
| user-svc critical p99, baseline | **0.0ms** |
| user-svc critical p99, under the decoy | **135.4ms** |
| traces where user-svc finishes last | **0.6%** |
| user-svc start offset vs payment, median | **−1.3ms** (user starts *first*) |
| user-svc start offset vs payment, p99 | 11.3ms |
| traces with all three legs present | **100%** |

Two hypotheses ruled out by that table. It is not span loss: every one of the
288 traces where user carried blame has complete payment and product spans. It
is not systematic queueing: user starts *before* payment at the median, and its
start lag is 11ms at p99 — nothing like the 88ms average offset seen on the
critical subset.

**The mechanism.** A p99 reports the top 1% of traces. An edge is non-critical
*at p99* only if it fails to finish last on more than **99%** of traces. Fan-out
finish order has real variance — scheduling, connection reuse, DB latency — so
the fraction of traces on which any given leg finishes last is never zero.
Adding delay to a sibling widens its duration distribution and pushes that
fraction up. Cross 1% and the edge's critical p99 jumps discontinuously from
~0 to roughly its own duration. user-svc landed at 0.6%, right on the boundary,
and its critical p99 went 0.0 → 135.4.

**The bind.** The decoy's delay must be large enough to breach the 50ms
wall-clock threshold, or a wall-clock detector passes the case and it proves
nothing. Any delay that large also pushes the sibling's last-finisher rate
toward and past 1%. The two requirements are in direct tension, and the second
decoy attempt sat exactly where they cross.

**The detector is not wrong.** On those 288 traces user-svc genuinely was the
critical path. The case's assertion is what is wrong: *"the decoy edge must
never alert"* is not a well-posed question about a p99.

**The case was not rebuilt.** Rebuilding it a third time to reach green would
be tuning the measurement until it gives the desired answer, which is the M8/M9
failure this phase exists to prevent. The case is correct; the *detector* was
answering the wrong question.

**The fix: a blame-share gate.** A p99 answers "how slow was this edge on its
worst 1% of traces". It does not answer "did this edge matter". The detector
now requires an edge to own at least **5% of the bucket's critical path**
before a latency alert about it is allowed out.

The signal was already there. `service_edges_1m` has carried `crit_ms_sum`
since Phase 3, so blame share is that divided by the bucket total — no schema
change, one extra column in the detector's query, and the arithmetic done in
Python across the edges the query already returns.

Measured shares on this application, which is what makes 5% a floor rather
than a tuning knob:

| edge | blame share |
|---|---|
| `gateway-svc->order-svc` | 52% |
| `order-svc->payment-svc` | 43% |
| `order-svc->product-svc` | 4.2% |
| `order-svc->user-svc` | **0.02%** |

The detector was alerting on an edge owning **two hundredths of one percent**
of the critical path. Below 5%, an edge cannot be the cause of a user-visible
regression by construction: if removing it entirely would not be noticed, it is
not what made the customer wait.

**Where the gate sits matters as much as the threshold.** It is placed after
the sustained window and before dedup, so the EWMA, the M6 healthy-only
learning and the breach history are all byte-for-byte unchanged. The gate can
only suppress an alert; it can never alter what the baseline learns. A fix that
introduced a boiling frog of its own while closing a false-positive would be a
poor trade, and `test_blame_gate_does_not_touch_the_baseline` pins it.

`blame_share` defaults to **1.0** on `EdgeObservation`, so any caller that has
not been updated keeps alerting rather than silently going quiet. A missing
signal must never look like an innocent edge — the failure mode hardest to
notice is the one where the detector stops talking.

Four tests cover it: the decoy suppressed at its real 0.0002 share, the culprit
still alerting at 0.434, the baseline unchanged, and the default not
suppressing.

The pair F30 and F37 together are worth more than either alone. The first says
a service that already carries blame cannot be a decoy. The second says that
under a percentile, *no* service is reliably a decoy once you slow it enough to
matter. Between them they are a fairly complete account of why the "parallel
sibling" case is harder than it looks.

### F38 — 🔴 The detector cannot tell a traffic ramp from a regression — OPEN

`002_null_traffic_ramp` ran for the first time in the project's history and
**failed on its first execution**: with no fault injected anywhere, the
detector alerted on `gateway-svc->order-svc` while offered load doubled.

This is the case working. Load raises latency legitimately — queueing is real,
and p99 moves under a 2x ramp without anything being wrong. A detector that
reads that as a regression pages somebody every time marketing sends an email,
and alert fatigue is a human availability problem rather than a cosmetic one.

**Why the existing gates do not catch it.** Every one of them is aimed at a
different failure:

| Gate | Why it does not fire |
|---|---|
| request-count floor | traffic went *up*; the floor is a minimum |
| warmup | the baseline was long since armed |
| M6 healthy-only learning | correct, and irrelevant — the rise is real |
| sustained 3-in-5 | the ramp is sustained, which is the point |
| blame share (F37) | `gateway-svc->order-svc` owns 52% of the critical path. It genuinely IS the bottleneck; it is simply not a DEFECT |

F37 and F38 are a matched pair and worth reading together. F37 was an edge that
was slow and did not matter. F38 is an edge that is slow, does matter, and is
still not something to wake anyone for. Blame share answers the first question
and says nothing about the second.

**What the fix looks like.** The signal the detector is missing is load itself.
`request_count` is already in every observation and already in the roll-up; the
detector reads it only as a floor. A regression is a latency rise *at constant
or falling throughput*; a ramp is a latency rise *with* throughput. Normalising
the breach against the change in request count — or simply suppressing while
throughput is climbing sharply — is the shape of it.

**Deliberately not fixed in this pass.** F37 was fixed because the measurement
said the detector was answering the wrong question and the fix was one gate
with a measured floor behind it. F38 needs a second EWMA over throughput and a
decision about what "sharply" means, and choosing that number without an eval
run to justify it is M8. The case that measures it exists and now runs; the
number comes first, then the threshold.

Recorded open, with the failure reproduced, which is the honest position.

### F39 — 🟠 The eval workflow and the eval overlay disagreed about where the datastores are — FIXED

`docker-compose.eval.yml` republishes Redis on **56379** and Postgres on
**55432**, deliberately: on a developer machine a native service frequently
already owns the default port, compose publishes the container anyway, reports
success, and the runner then talks to the wrong database for a whole
ninety-minute run. The overlay moved the ports to stop that.
`.github/workflows/eval.yml` still named the defaults.

**Why nothing noticed for weeks.** A CI runner has no native Redis, so there was
no bind conflict and nothing failed at boot. All sixteen containers came up
green, the baselines warmed for six minutes, the topology guard passed — and the
run died in preflight ten minutes in:

```
preflight FAILED
  - redis unreachable at redis://localhost:6379/0 (ConnectionError).
    Alert suppression cannot be reset between cases, so every repeat-edge
    case would false-negative.
```

The runner was right to refuse. Without Redis the suppression reset is a no-op,
and cases 003 and 006 both fault `order-svc->payment-svc` inside one 600s dedup
TTL — the second would have been a guaranteed false negative and the scoreboard
would have been measuring Redis.

**The shape of the defect is the interesting part**: not a crash, but two files
that each look correct in isolation. Nothing in either one is wrong on its own
page.

**Fixed** by setting `EVAL_REDIS_PORT`, `EVAL_POSTGRES_PORT`, `REDIS_URL` and
`POSTGRES_PORT` in the workflow env. GitHub Actions cannot reference one env key
from another in the same block, so the literals are duplicated — and
`eval/tests/test_ci_ports.py` is the guard that makes the duplication safe. It
parses the overlay, the workflow and the Makefile and fails if they stop
agreeing. It runs in the workflow's own pre-boot validation step, so the same
drift now fails in **milliseconds instead of after a stack boot**, and it was
checked red against the exact defect before being committed.

---

### F40 — 🔴 The staleness banner could not survive the outage it was built for — FIXED

Found by chaos experiment 001 (`docs/CHAOS.md`), which stopped ClickHouse under
live load. The graph API served honest stale data for **thirty seconds** and then
returned **HTTP 500** for the remaining hundred seconds of the outage, including
the entire restart.

The arithmetic made it inevitable and was sitting in two constants that had never
been read next to each other:

```
GRAPH_TTL_S      60    graph:current expires 60s after the last good build
STALE_AFTER_S    30    the banner appears at 30s of age
                 --
                 30s   the entire life of the staleness banner
```

Past 60 seconds the key is gone, `/api/graph` falls through to a live ClickHouse
query, and that query had nothing to talk to. The exception escaped as a 500.

**Measured in the same experiment: ClickHouse took 31 seconds to answer after
`docker start`**, on a machine where the stack was already warm. Any
restart-shaped outage — the most ordinary kind there is — outlives a 30-second
banner by construction. M26 exists so that degraded data beats no data, and the
feature could not cover the one scenario it was designed for.

Two components, each individually correct, that had never been considered
together. No unit test would have reached it: everything behaved to
specification.

**Fixed** with `graph:lastgood`, a second copy under its own key with an hour-long
TTL, written in the same Redis pipeline as `graph:current` so the two cannot
disagree about which is newer. `/api/graph` now falls through
cache → live → last-good → a 503 that names which of the three failed.

Last-good returns **HTTP 200**, deliberately: M26 says never display stale data
*without the banner*, not never display it. A 503 would be more literal and less
useful — the dashboard renders nothing and an operator mid-incident loses the
last known shape of their system at the moment they most want it. Age and
`degraded` ride in the payload so nothing can be mistaken for current.

Re-verified against the same failure rather than trusted to its unit tests:
every poll from t+60s to t+154s returned 200 from `last-good` with a truthful,
growing age, where each one had been a 500 before.

**The fix needed a second pass — F40b.** The first version wrote both keys in
one unconditional pipeline. An empty graph is a *truthful* statement about now
and a *useless* fallback, and writing it to both keys silently destroys what the
fix exists to provide. The failure lands exactly when the fallback matters most:
ClickHouse comes back, the roll-up views have not caught up, the builder's next
cycle **succeeds** and returns zero edges, and that empty result overwrites the
last real graph — leaving the operator an empty map, indistinguishable from a
system with no traffic, while they are trying to find out what broke.

`graph:current` is still unconditional, because it is the truth about now.
`graph:lastgood` is written only when the graph has edges. The consequence to
accept is that after a genuinely quiet hour last-good holds an hour-old graph,
which is correct and safe because it is served with `stale`, `degraded` and its
real age. **A last-known-good that a degenerate result can overwrite is not a
last-known-good.**

Found by re-auditing the fix rather than by a second failure — which is the same
lesson one level up: F40 itself was two behaviours that were individually correct
and wrong together. `test_graph_cache.py` pins it and was checked red against the
unconditional write first; four of its five assertions fail on the old code.

---

### F41 — 🔴 Per-call rows carried the whole service pair's blame — FIXED

The flagship number, wrong on a measurable fraction of rows, for the same reason
in every case.

`attribute_to_edges` returns one value **per service pair** — the right shape for
a whole-trace summary. The assembler writes one row **per call**, and was filling
`critical_ms` from that pair-keyed map. So when `order-svc` calls `payment-svc`
twice in a trace — a retry, or the saga compensating a charge after a sibling
failed — **both rows received the pair total**.

Three consequences, in increasing order of how much they matter:

1. `critical_ms > duration_ms` on individual rows. Definitionally impossible: an
   edge cannot contribute more user-visible time than the call itself took.
2. `crit_ms_sum` in `service_edges_1m` counted that blame once per row, so the
   roll-up over-attributed the edge by roughly its call count.
3. The TDigest behind `crit_p99` was fed the pair total once per call. **The
   dependency map ranks on `crit_p99`**, so an edge called twice per trace
   outranked one called once — sending you to the wrong service, which is the
   exact failure this project exists to prevent, arrived at from the other
   direction.

**Measured on live data before the fix:**

| | |
|---|---|
| `order-svc->payment-svc` rows violating `critical_ms <= duration_ms` | **4.83%** |
| Its traces making two or more calls | **3.9%** (592 of 15,132) |
| Overall violation rate across all edges | 1.35% (1,342 of 99,178) |
| Worst single excess | +1,996 ms |

The most over-blamed edge was the one the flagship exists to identify, because it
is the only edge with a compensation path. Averages hid it completely — `avg`
looked plausible throughout — which is why the guard added for it counts **rows**
rather than comparing means.

**Fixed** by `attribute_to_child_spans`, which returns subtree critical time
keyed by child span id. `attribute_to_edges` is now *defined in terms of it*, so
the per-call rows and the per-edge total cannot drift apart again. The
sub-skew floor moved with it: it is a statement about what a single measurement
can resolve, so applying it to a sum of calls was testing the wrong quantity.

Verified against live data after the fix: **zero violations** on fresh rows, and
`make critical-path-guard` now asserts the invariant permanently.

**What this cost.** The full-set eval run that produced precision 0.583 was
executed against the buggy attribution. That result is recorded in `docs/EVAL.md`
as it stands, and the set was re-run against the fix rather than the number being
quietly replaced.

---

### F42 — 🔴 The decoy invariant was checked against the declaration, not the measurement — FIXED

The worst kind of guard: one that occupies the place where a real check should
be, reports OK, and asserts nothing.

`scripts/topology-guard.py` had exactly one **hard** assertion about decoy
validity:

```python
decoys = [e for e, ms in CRIT_P99_BASELINE_MS.items() if ms < THRESHOLD_MS]
say("at least one leg is still a true parallel sibling", bool(decoys), ...)
```

`CRIT_P99_BASELINE_MS` is a hardcoded dict in `eval/topology.py`. The check
therefore asked *"does the declaration still contain a decoy?"* — a constant.
It could not fail. The real question, whether the leg the case files use as a
decoy still carries no blame **on the machine the set is about to run on**, was
only a soft `note()`, printed as DRIFT and deliberately non-fatal.

**What that cost, measured.** In the second full-set CI run the guard printed,
on quiet traffic, before a single case executed:

```
crit p99 order-svc->payment-svc  148.0 (recorded 157.0)  OK
crit p99 order-svc->product-svc   49.0 (recorded  49.3)  OK
crit p99 order-svc->user-svc      56.6 (recorded   0.0)  DRIFT
at least one leg is still a true parallel sibling         OK
```

`order-svc->user-svc` was carrying **56.6 ms of critical path against a declared
0.0**. On a 2-vCPU contended GitHub runner the fan-out finish order genuinely
differs from a quiet developer machine, and `user-svc` lands on the critical
path often enough to matter.

Every `must_not_alert` trap naming that leg was therefore asserting something
false, and the workflow proceeded anyway. Two of the six failures in that run —
`payment_latency_60ms` and, worse, **`parallel_decoy`, the flagship case** —
were scored against a trap that was not a trap. The detector alerted on
`order-svc->user-svc` at +339 ms, which was **correct behaviour**, and the
scoreboard recorded it as a false positive.

The scoreboard was measuring the harness, not the detector.

**This is the third time this application has taught the same lesson.** F30 and
F37 both came from a decoy leg that turned out to carry blame; the fix each time
was to re-measure and move the decoy. What was missing was a check that fails
when the assumption stops holding — and the check that looked like it was doing
that was reading a constant.

**Fixed.** The decoy invariant is now computed from the live graph and is hard:

- a leg declared as a zero-blame sibling that is *measured* at or above
  `THRESHOLD_MS` fails the guard, naming the leg, the measurement and what it
  invalidates;
- "at least one leg is still a true parallel sibling" is now evaluated over the
  legs that are still valid *by measurement*, so the two checks are independent;
- when no declared decoy appears in the live graph at all the check reports SKIP
  rather than a misleading failure — that state is the "declared edge absent"
  failure above it, which is about missing traffic, not about case design.

The guard already runs in `eval.yml` immediately before the golden set, so this
now stops a run that would produce a meaningless scoreboard rather than
annotating it.

**The workflow step's own comment described the correct behaviour the whole
time:**

```yaml
# Structural drift invalidates the case files before it invalidates
# any score. A leg that has started carrying critical-path blame
# cannot be the decoy at any delay, and case 006 would then fail
# against a detector doing exactly the right thing.
run: python scripts/topology-guard.py
```

That is an exact description of what happened. The step had no
`continue-on-error`, so it would have failed the job — the guard simply never
returned non-zero, because the assertion that should have caught it was reading
a constant. **The intent was documented, wired up correctly, and not
implemented**, which is a failure mode worth naming on its own: a comment that
describes the guarantee makes it harder, not easier, to notice the guarantee is
absent.

**Verified by reproducing the CI condition rather than by reasoning.** +250 ms
was injected into `user-svc` to make it the slowest sibling, and the guard went
red with the right message and exit code 1:

```
every declared decoy leg still carries no blame          FAIL
    order-svc->user-svc measured 289.6ms critical p99 against a declared 0.0
    (threshold 50ms). Every case naming it in `must_not_alert` is now
    asserting something false.
at least one leg is still a true parallel sibling        OK
```

With the fault cleared it passes, and `order-svc->user-svc` measures 0.0 on this
host — which is the whole point: **the decoy is valid here and was not valid
there, and until now nothing said so.**

**What remains open.** The set still needs a decoy that holds on CI hardware.
Re-measuring `CRIT_P99_BASELINE_MS` on the runner and picking the leg that is
genuinely blameless there is the next step, and it must be a measurement rather
than a choice. Until then the full-tier scoreboard from a GitHub runner carries
an asterisk, and `docs/EVAL.md` says so.

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
eval/baselines/README.md      # main-<tier>.json, NOT main.json: smoke and full
                              # are different measurements and the gate refuses
                              # to compare them. Neither is committed yet - see
                              # F36 for why this host cannot produce one
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

**Evidence — as built**

| # | Criterion | Evidence |
|---|---|---|
| 1 | 12 cases, sequential, one `eval_runs` row | ⚠️ **Machinery verified, scale not.** End to end on the smoke tier: one `eval_runs` row plus four `eval_cases` rows, surfaced as `tracemap_eval_*` gauges and read by the Phase 7 rules. But **8 of the 12 cases have never been executed** — 002, 004, 005, 008, 009, 010, 011, 012. They pass their file-level tests and have never faced a live stack |
| 2 | Decoy passes | ❌ **FAILS, open (F37).** Rebuilt once on a measurement (F30) and still red. A p99 trap is unsatisfiable for any delay big enough to breach the wall-clock threshold — the detector is right, the assertion is not. Not rebuilt a third time: that would be tuning the measurement to the answer |
| 3 | Null cases pass | ⚠️ **Half.** `001_null_baseline` passed on the clean run — no alert across 600s of healthy traffic, on a quiet host (F29). `002_null_traffic_ramp` has **never been executed**: it is not in the smoke tier, and the full set has not been run |
| 4 | F1 ≥ 0.82, precision ≥ 0.87, MTTD p95 | ⚠️ **F1 0.857 ✅, precision 0.750 ❌** (the decoy), MTTD p95 273s ✅. **Ceiling amended 216s → 432s** — 216s sits below the detector's own 3-cycle confirmation floor before any pipeline time (F28) |
| 5 | The gate is proven to fail | Done twice. A real run went red at F1 **0.400** / precision **0.333**, and `scripts/eval-gate-guard.py` reproduces six distinct breakages in CI on every commit — a test rather than a screenshot |
| 6 | Storage-profile matrix | ✅ **Both green in CI.** Took three rounds: scripts were mode 100644 (Windows has no exec bit) so badger died on "Permission denied", and clickhouse then died because Jaeger never waited for the database — it pings ClickHouse once at startup and exits(1), and ClickHouse reported healthy from HTTP 8123 while its consumers use native 9000 |
| 7 | k6 SLO gate | ✅ **Green, at 10 rps not 50.** Thresholds are exactly as specified (p95 < 400ms, errors < 1%); only the offered load is reduced. A 2 vCPU / 7 GB runner cannot serve this stack's ~7 GB floor at 50 rps — measured p95 2.5s, 2.37% errors, and only 44.7 of the 50 achieved. That is the runner saturating, so gating on it would measure GitHub's hardware. **The 50 rps figure belongs to the sized host and must be quoted from there, not from CI** |
| 8 | Live HTTPS, writes authenticated | `charts/tracemap/templates/ingress.yaml` — Traefik + cert-manager, write prefixes behind basic auth; `deploy.yml` asserts unauthenticated writes are refused. **Not yet deployed to a live host** |

**Verdict: Phase 7 is BUILT, not COMPLETE.** Three criteria are unmet (2, 4,
8) and three are partial (1, 3, 7). Only 5 — the gate proven to fail — and 6 are
unreservedly done.

The gap that matters least is 8, because it is only a deployment. The gap that
matters most is 1 and 3 together: **eight of the twelve golden cases have never
run against a live stack.** Everything quoted about this detector's accuracy
comes from four cases, and the four were chosen to be fast. A case that has
never executed is a case whose expected edge, budget and traps are all
untested assertions — which is the same class of defect as F23 and F30, both of
which were only found by running the thing.

**Two items are unfinished and neither is claimed.**

Criterion 8's live URL: the chart, ingress, auth and post-deploy assertions
exist and render, but nothing has been applied to a cluster.

Criterion 2's decoy: red, with the reason measured and written up as F37. The
gate is therefore red overall on precision, and that is the state being
committed. Shipping a green scoreboard here would have required either
rebuilding the case a third time until it agreed, or lowering the precision
floor — and a phase whose entire subject is *not doing that* is a poor place
to start.

The measured smoke-tier result, for the record and with its tier named:

```
  cases                 4   tp 3  fp 1  fn 0
  precision         0.750   (floor 0.870)   <- the decoy
  recall            1.000
  f1                0.857   (floor 0.820)
  mttd p50           242s   p95  273s       (ceiling 432s)
  FAIL
```

MTTD is over the three detected cases; the null case contributes no latency.
The timings come from a host that stalled 471s mid-run (F36), so they are
reported and not committed as a baseline.

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
- [x] `test_parallel_fanout_blames_only_the_slowest_sibling` passes and is quoted in the README
- [x] `trace_edges` has a `critical_ms = 0.0` **row** (present, not missing) for a parallel sibling — measured live: product-svc 95-98% of rows, user-svc 100%
- [x] No materialized view joins `spans` — `make schema-guard`, 8 files
- [x] Roll-up counters verified to aggregate across insert blocks *(F1)* — 3 tables, `edge_slo_1m` included
- [x] Assembler dedupes by `span_id` at the fetch boundary *(F2)*
- [x] MV and reconciler agree within 2% — measured **0.00%** over 33 buckets
- [x] Per-call blame never exceeds the call's own duration *(F41)*, and the roll-up's `crit_ms_sum` equals the sum of its rows — both guarded

**The differentiators**
- [x] Wall ⇄ critical toggle visibly re-ranks edges in under four seconds — and is linkable as `?metric=critical`
- [x] 12 golden cases including null and decoy — all twelve now run to completion in CI
- [x] CI gate proven to fail — and not only synthetically: the first full run went red on its own merits at precision 0.583
- [x] Deploy markers on edge charts, correlation (or its absence) in alerts

**Operational honesty**
- [x] Detector heartbeat alert (the silent-failure guard)
- [x] Staleness banner when serving last-good data — and it survives a real
      outage now; see F40/F40b
- [x] `docs/LIMITATIONS.md` — **seven** caveats now, the original four plus
      malformed nesting, eval scope, and parent-edge echo (measured in the
      first full eval run)
- [x] `docs/SCALING.md` with the measured ~7 GB floor
- [x] `POSTMORTEM-001.md` in Google SRE format — written about F1
- [ ] Write endpoints authenticated on the public URL

**The demo**
- [x] `make demo` opens on a populated graph, never an empty one — verified from a clean clone
- [ ] 3-minute video recorded while things work — **open**; `docs/DEMO.md` is the script
- [ ] Flow 4 rehearsed to muscle memory — **open**; written down in `docs/DEMO.md`

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
