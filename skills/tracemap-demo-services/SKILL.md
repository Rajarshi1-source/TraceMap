---
name: tracemap-demo-services
description: Build and maintain TraceMap's five traced microservices — gateway and order on Node.js 24 Express, user and payment on Python 3.14 FastAPI, product on Go 1.26 Gin — plus the checkout fan-out that produces interesting traces, the runtime fault-injection flags the eval harness drives, and the resilience patterns (timeouts, retry with jitter, circuit breaker, bulkhead, idempotency keys, saga compensation, graceful degradation). Use for ANY work on the traced application itself - service endpoints, the call graph, fault injection, k6 load scripts, resilience behaviour, health and readiness endpoints, or the app database schema - and when the user says "add a service", "inject a slowdown", or "the fan-out isn't parallel". MANDATE - the checkout fan-out MUST be genuinely concurrent, because a sequential fan-out cannot demonstrate critical-path attribution and the project's flagship feature becomes undemonstrable. Pair with tracemap-otel-pipeline for instrumentation.
---

# TraceMap — The Five Traced Services

This is the application being observed, not the observability platform. Its job is to produce **interesting, realistic traces** — and above all, a call graph where wall-clock latency and critical-path latency genuinely disagree.

## The call graph

```
client -> gateway-svc (Node 24 / Express)
             |
             v
          order-svc (Node 24 / Express)
             |
   +---------+---------+  <-- CONCURRENT fan-out, this is the whole point
   v         v         v
user-svc  product-svc  payment-svc
(Py 3.14) (Go 1.26)    (Py 3.14)
   |         |            |
   +---------+------------+
             v
        PostgreSQL 18
```

| Service | Language | Role | Typical latency |
|---|---|---|---|
| `gateway-svc` | Node.js 24 / Express | Entry point, request shaping, header auth | 3-8ms own work |
| `order-svc` | Node.js 24 / Express | Orchestrates the fan-out, writes the order | 10-20ms own work |
| `user-svc` | Python 3.14 / FastAPI | Profile lookup, cheap | 20-40ms |
| `product-svc` | Go 1.26 / Gin | Stock check, moderate | 30-60ms |
| `payment-svc` | Python 3.14 / FastAPI | Charge, slowest by design | 60-120ms |

Three languages is deliberate: it proves OpenTelemetry is not a one-language trick, and it forces you to confront that Go has no runtime auto-instrumentation agent.

## The non-negotiable: the fan-out must be concurrent

If `order-svc` awaits each downstream call in sequence, every edge sits on the critical path, wall-clock and critical-path numbers are identical, and the project's flagship feature has nothing to show. The parallel fan-out is not a performance optimisation here — it is the demo.

```js
// order-svc — CORRECT: genuinely concurrent
const [user, stock, charge] = await Promise.all([
  userClient.getUser(userId),
  productClient.checkStock(cartItems),
  paymentClient.charge(userId, amount),
]);
```

```js
// WRONG for this project: sequential await makes every edge critical
const user   = await userClient.getUser(userId);
const stock  = await productClient.checkStock(cartItems);
const charge = await paymentClient.charge(userId, amount);
```

Verify it after any refactor: pull one trace and confirm the three child spans **overlap in time**. If they are staggered end-to-start, the fan-out regressed and the decoy eval case will start failing for a reason that has nothing to do with the detector.

Tune the baseline latencies so `payment-svc` is the slowest and `product-svc` is a close second. That ordering is what makes the toggle demo land: product looks guilty in wall-clock mode and is exonerated in critical-path mode.

## Endpoints

| Service | Endpoint | Produces |
|---|---|---|
| gateway | `POST /checkout` | The root span; the demo path |
| gateway | `GET /products` | Cacheable read path, exercises degradation |
| order | `POST /orders` | Fan-out parent; Postgres write span |
| user | `GET /users/{id}` | Simple DB read |
| product | `POST /stock/check` | Batch DB read, N+1 opportunity |
| payment | `POST /charge` | Slowest hop; the fault-injection target |
| all | `GET /healthz` | Process liveness only — no dependency checks |
| all | `GET /readyz` | Dependency readiness (DB reachable, pool healthy) |

Let `product-svc` do a deliberate **N+1 query** under some cart sizes. It gives the Jaeger waterfall something genuinely diagnosable — twelve sibling DB spans instead of one — and makes the drill-in demo land: *"I clicked the edge, opened the trace, and saw twelve identical queries where there should have been one."* That is the postmortem artifact writing itself.

## Fault injection flags

The eval harness drives faults through **runtime-readable configuration**, never a restart. A pod restart perturbs latency on its own and confounds the measurement it is supposed to produce.

```
TRACEMAP_FAULT_DELAY_MS=60        # added latency on the target endpoint
TRACEMAP_FAULT_ERROR_RATE=0.10    # fraction of requests returning 5xx
TRACEMAP_FAULT_ENDPOINT=/charge   # scope the fault
TRACEMAP_FAULT_CPU_BURN_MS=0      # saturation simulation
```

Read these per request from a small config object that a control endpoint or ConfigMap watch can mutate. Requirements:

- **Default to zero** — faults must never be on by accident.
- **Scoped to an endpoint**, so a case can target `/charge` without slowing every path.
- **Bounded by a duration** as a backstop, so a crashed eval runner self-heals.
- **Visible in a span attribute** (`tracemap.fault.injected=true`) so an operator reading a trace is not misled by a fault they forgot about.

That last one matters more than it looks: an injected fault that is invisible in the telemetry is indistinguishable from a real regression, and you will eventually debug your own injector for an hour.

## Resilience patterns to implement

Implement these in the application, not the platform — they are what makes the traces interesting and they are standard interview material.

| Pattern | Where | Setting |
|---|---|---|
| Timeouts | Every outbound call | 2s default, 500ms on payment |
| Retry + exponential backoff + **jitter** | Transient failures | 3 attempts max; jitter prevents synchronised retry storms |
| Circuit breaker | order -> payment | Open at 50% errors over 1 min, half-open probe after 30s |
| Bulkhead | Per-downstream connection pools | One slow dependency cannot starve the others |
| Idempotency key | `POST /checkout` | Key on `(user_id, cart_hash)`; retries must not double-charge |
| Saga with compensation | order -> payment -> inventory | Compensating action when a later step fails |
| Graceful degradation | product-svc unavailable | Serve cached catalog with `X-Stale: true` |

Circuit-breaker state transitions should be recorded as span events. A trace showing the breaker opening mid-incident is a strong artifact, and it makes the resilience work visible rather than merely claimed.

## Application data model (PostgreSQL 18)

Keep it small — this is a trace generator, not an e-commerce product. `users`, `products` with a stock count, `orders`, `order_items`, `payments`. Real foreign keys and a real transaction around order creation, because a DB span with a rollback is a more interesting trace than one without.

Never log or attach full SQL with literal parameter values to spans. Strip parameters from `db.statement`; spans are retained for days and readable by anyone with dashboard access.

## Load generation

k6 drives 5-20 RPS continuously. Two scripts:

- `normal-checkout-flow.js` — the baseline mix, roughly 70% checkout, 30% browse, with think time. Constant load with no variation makes EWMA baselines unrealistically tight and the detector looks better than it is.
- `induce-payment-slowdown.js` — the manual chaos demo used in the screen-share.

Add mild diurnal variation to the baseline script. A detector tuned against perfectly flat traffic will produce false positives the moment real traffic breathes.

## Starter code

`references/starter-code.md` holds the runnable week 1-4 scaffold: repo skeleton, `.env.example`, docker-compose core profile, Makefile with `make demo`, the concurrent fan-out in `order-svc`, the fault-target `payment-svc`, the N+1 `product-svc`, the fault-flag control plane, the application schema, both k6 scripts, shared worker supervision, and a 13-step first-run checklist. Read it when bootstrapping the repo or when a service skeleton is needed — it points at the other skills rather than duplicating their artifacts, so there is one authoritative copy of each.

## What not to do

- Do not make the fan-out sequential.
- Do not require a restart to apply a fault.
- Do not check dependencies in `/healthz`.
- Do not let injected faults default to on, or leave them invisible in spans.
- Do not add a sixth service to the core path — complexity in the traced app buys nothing the platform does not already demonstrate. (An uninstrumented sixth service for the eBPF demo lives outside the core path.)
- Do not put real secrets or PII in span attributes.

## Interview soundbites

- *"The fan-out is genuinely concurrent, and that's deliberate — with a sequential fan-out every edge is on the critical path and there's nothing to show. Payment is the slowest by design and product is a close second, so the toggle exonerates product live."*
- *"Faults are runtime flags, not restarts. A restart changes latency on its own, so restarting to inject a latency fault measures the restart."*
- *"The product service does an N+1 under larger carts on purpose. It gives the waterfall something genuinely diagnosable — twelve identical queries where there should have been one."*

## Pairing

- `tracemap-otel-pipeline` — instrumenting these services
- `tracemap-anomaly-eval` — the harness that drives the fault flags
- `tracemap-platform-ops` — Dockerfiles, probes and Helm for these services
