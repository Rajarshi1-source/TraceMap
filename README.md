# TraceMap

A dependency map that tells you which service to actually go fix.

Every service map answers *which edge is slow*. In a parallel fan-out that is
the wrong question — a 700 ms call running alongside a 720 ms sibling
contributes **zero** milliseconds of user-visible latency, but Tempo's service
graph, SigNoz, Coroot and Kiali will all paint it red and send you to debug the
wrong service. TraceMap computes the **critical path** through every trace and
attributes user-visible milliseconds per edge, so the map is actionable rather
than merely informative.

```
order-svc            |------------------------------------- 730ms --|
  |- user-svc        |-- 40ms --|                             0.0 ms blame
  |- product-svc     |-------------------- 700ms ---------|   0.0 ms blame
  |- payment-svc     |---------------------- 720ms ---------| 720.0 ms blame
```

Making `product-svc` infinitely fast would save the user nothing.

> **Status: Phase 1 of 8.** The traced application runs; tracing lands in
> Phase 2. See [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) for the build
> order, exit criteria and the four defects found during the design audit.

---

## Quick start

```bash
make env      # write .env from .env.example, with GIT_SHA filled in
make up       # boot the core profile
make smoke    # end-to-end check through all five services
```

`make` on its own lists every target.

## What runs today (Phase 1)

Five services in three languages, exchanging real requests over docker-compose.
Three languages is deliberate: it proves OpenTelemetry is not a one-language
trick, and it forces confronting that Go has no runtime auto-instrumentation
agent.

| Service | Language | Role | Baseline |
|---|---|---|---|
| `gateway-svc` | Node.js 24 / Express | Entry point, graceful degradation | 3–8 ms own work |
| `order-svc` | Node.js 24 / Express | **The concurrent fan-out** | 10–20 ms own work |
| `user-svc` | Python 3.14 / FastAPI | Profile lookup | 30 ms |
| `product-svc` | Go 1.26 / Gin | Stock check, deliberate N+1 | 45 ms |
| `payment-svc` | Python 3.14 / FastAPI | Charge — slowest by design | 90 ms |

```
client ──► gateway-svc ──► order-svc
                               │
                 ┌─────────────┼─────────────┐   ← CONCURRENT. This is the point.
                 ▼             ▼             ▼
             user-svc     product-svc   payment-svc
                 └─────────────┼─────────────┘
                               ▼
                        PostgreSQL 18
```

**The fan-out must stay concurrent.** If `order-svc` ever awaits its three
downstream calls in sequence, every edge lands on the critical path, wall-clock
and critical-path numbers become identical, and the project's flagship feature
has nothing to demonstrate. It is one careless refactor away, so it is asserted
by a test:

```bash
make fanout-guard
```

## Architecture (target)

```
5 services ──► OTel Collector (agent → gateway, tail sampling)
                   ├─► Jaeger v2.20  (search, waterfall UI)
                   └─► ClickHouse 26.3  tracemap.spans
                             │
                     trace-assembler ── stitch edges + critical path, one pass
                             ▼
             trace_edges · trace_critical_path
                             ▼
              service_edges_1m  (incremental MV, TDigest)
              …_recon           (refreshable MV, correctness guard)
                             ▼
              FastAPI analytics API ──► Next.js dependency map
```

## Documentation

| Document | What it is |
|---|---|
| [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) | The executable build plan — phases, exit criteria, mandate ledger |
| [`IMPLEMENTATION_PLAN_REV3.md`](IMPLEMENTATION_PLAN_REV3.md) | Design authority — architecture and trade-offs |
| [`schema-and-queries.md`](schema-and-queries.md) | ClickHouse DDL and the four production queries |
| [`eval-harness.md`](eval-harness.md) | The golden set, runner and CI quality gate |
| [`starter-code.md`](starter-code.md) | Week 1–4 scaffold |
| `skills/` | Project rules, packed to `.claude/skills/*.skill` by `make skills` |

`.claude/skills/*.skill` are **build artifacts**. Edit `skills/<name>/SKILL.md`
and run `make skills`; `make skills-check` fails if they are stale.

## Licence

MIT — see [LICENSE](LICENSE).
