# TraceMap starter code — the runnable week 1–4 scaffold

Everything here is scoped to getting a **traced, fan-out-shaped application
running locally** so the platform has something interesting to observe. It is
deliberately not a copy of the other skills' material — where a file belongs to
another skill, this document points there instead of duplicating it, so there
is exactly one authoritative version of each artifact.

| You need | Go to |
|---|---|
| Collector YAML, Jaeger storage profiles | `tracemap-otel-pipeline` |
| ClickHouse DDL and the four queries | `tracemap-clickhouse-analytics` → `references/schema-and-queries.md` |
| Golden cases, runner, scorer, CI gate | `tracemap-anomaly-eval` → `references/eval-harness.md` |
| `critical_path.py` (full implementation + tests) | `tracemap-critical-path` |
| Dockerfiles, Helm, probes, CI | `tracemap-platform-ops` |

Contents:
1. Repo skeleton
2. `.env.example`
3. docker-compose (core profile)
4. Makefile
5. `order-svc` — the concurrent fan-out (Node.js 24)
6. `payment-svc` — the fault target (Python 3.14)
7. `product-svc` — the N+1 opportunity (Go 1.26)
8. `gateway-svc` — entry point and degradation
9. Fault-flag control plane
10. Application schema (PostgreSQL 18)
11. k6 load scripts
12. Wiring the platform workers
13. First-run checklist

---

## 1. Repo skeleton

```
tracemap/
├── microservices/
│   ├── gateway/           # Node.js 24 / Express
│   ├── order/             # Node.js 24 / Express  ← the fan-out
│   ├── user/              # Python 3.14 / FastAPI
│   ├── payment/           # Python 3.14 / FastAPI ← fault target
│   └── product/           # Go 1.26 / Gin
├── otel/                  # see tracemap-otel-pipeline
├── jaeger/                # three storage profiles
├── clickhouse/schema/     # see tracemap-clickhouse-analytics
├── platform/              # FastAPI analytics API + workers
├── eval/                  # see tracemap-anomaly-eval
├── dashboard/             # Next.js 16.3
├── load-testing/
├── docs/
├── docker-compose.yml
├── .env.example
├── Makefile
└── README.md
```

---

## 2. `.env.example`

Commit this; never commit `.env`.

```bash
# --- identity: this single line makes deploy attribution and trace-to-code work
GIT_SHA=dev-local

# --- OTel
OTEL_EXPORTER_OTLP_PROTOCOL=grpc
OTEL_TRACES_SAMPLER=parentbased_always_on   # tail sampling decides, not the SDK

# --- storage profile: clickhouse | opensearch | badger
STORAGE_PROFILE=badger                      # badger for first boot, zero infra

# --- datastores
POSTGRES_USER=tracemap
POSTGRES_PASSWORD=change-me-locally
POSTGRES_DB=tracemap
CLICKHOUSE_HOST=clickhouse
CLICKHOUSE_DB=tracemap
REDIS_URL=redis://redis:6379/0

# --- baseline service latencies (ms) — payment MUST be slowest, product close second
SVC_USER_BASE_MS=30
SVC_PRODUCT_BASE_MS=45
SVC_PAYMENT_BASE_MS=90

# --- fault injection: ALL default to zero/off
TRACEMAP_FAULT_DELAY_MS=0
TRACEMAP_FAULT_ERROR_RATE=0
TRACEMAP_FAULT_ENDPOINT=
TRACEMAP_FAULT_CPU_BURN_MS=0
TRACEMAP_FAULT_TTL_S=600                    # backstop: faults self-heal

# --- alerting (optional at first boot)
SLACK_WEBHOOK_URL=
```

`SVC_*_BASE_MS` are not decoration. Payment slowest and product a close second
is what makes the wall-clock/critical-path toggle visibly disagree. If you
change these, re-run the decoy eval case.

---

## 3. docker-compose (core profile)

```yaml
# docker-compose.yml  (core services only; see tracemap-platform-ops for full/eval)
services:
  postgres:
    image: postgres:18-alpine
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./microservices/schema.sql:/docker-entrypoint-initdb.d/01-schema.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER}"]
      interval: 5s
      retries: 10
    profiles: [core, full, eval]

  redis:
    image: redis:8-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      retries: 10
    profiles: [core, full, eval]

  clickhouse:
    image: clickhouse/clickhouse-server:26.3-alpine
    ulimits:
      nofile: { soft: 262144, hard: 262144 }
    volumes:
      - chdata:/var/lib/clickhouse
      - ./clickhouse/schema:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost:8123/ping"]
      interval: 5s
      retries: 20
    profiles: [core, full, eval]

  jaeger:
    image: jaegertracing/jaeger:2.20.0
    command: ["--config", "/jaeger/config.yaml"]
    volumes:
      - ./jaeger/jaeger-${STORAGE_PROFILE}.yaml:/jaeger/config.yaml:ro
    ports:
      - "16686:16686"       # UI
    depends_on:
      clickhouse: { condition: service_healthy }
    profiles: [core, full, eval]

  otel-collector:
    image: otel/opentelemetry-collector-contrib:0.158.0
    command: ["--config", "/etc/otel/collector-gateway-config.yaml"]
    volumes:
      - ./otel:/etc/otel:ro
      - otelqueue:/var/lib/otelcol/queue      # persistent sending queue
    depends_on:
      jaeger: { condition: service_started }
      clickhouse: { condition: service_healthy }
    profiles: [core, full, eval]

  gateway-svc:
    build: ./microservices/gateway
    environment:
      OTEL_SERVICE_NAME: gateway-svc
      OTEL_RESOURCE_ATTRIBUTES: "service.version=${GIT_SHA}"
      OTEL_EXPORTER_OTLP_ENDPOINT: "http://otel-collector:4317"
      ORDER_URL: "http://order-svc:3001"
      PRODUCT_URL: "http://product-svc:8080"
    ports: ["3000:3000"]
    depends_on: [otel-collector, order-svc]
    profiles: [core, full, eval]

  # order-svc, user-svc, product-svc, payment-svc follow the same shape:
  #   OTEL_SERVICE_NAME, OTEL_RESOURCE_ATTRIBUTES with service.version,
  #   OTEL_EXPORTER_OTLP_ENDPOINT, downstream URLs, TRACEMAP_FAULT_* passthrough.

volumes:
  pgdata:
  chdata:
  otelqueue:
```

In compose everything points at the single collector. In Kubernetes services
point at the **node-local** agent via the downward API instead — see
`tracemap-otel-pipeline`.

---

## 4. Makefile

```makefile
.PHONY: demo up down logs seed eval schema smoke

demo: up seed          ## boot core and pre-load traffic — use this before a screen-share
	@echo "Dashboard  http://localhost:3001"
	@echo "Jaeger UI  http://localhost:16686"

up:
	docker compose --profile core up -d --wait

seed:                  ## 60s of load so the graph is populated on first look
	docker run --rm --network host -v $(PWD)/load-testing:/scripts \
	  grafana/k6 run --duration 60s --vus 5 /scripts/normal-checkout-flow.js

down:
	docker compose --profile core down -v

schema:
	docker compose exec -T clickhouse clickhouse-client --multiquery \
	  < clickhouse/schema/02_spans.sql

smoke:
	./scripts/smoke-test.sh

eval:
	python -m eval.runner --git-sha $$(git rev-parse --short HEAD)
```

`make demo` exists because a dashboard that opens on an empty graph is a demo
you have already lost. Never screen-share a cold boot.

---

## 5. `order-svc` — the concurrent fan-out (Node.js 24)

The single most important file in the starter. If this fan-out becomes
sequential, critical-path attribution has nothing to show.

```js
// microservices/order/src/routes/orders.js
import express from 'express';
import { userClient, productClient, paymentClient } from '../clients.js';
import { withIdempotency } from '../idempotency.js';
import { db } from '../db.js';

export const router = express.Router();

router.post('/orders', async (req, res, next) => {
  const { userId, items, amount } = req.body;

  try {
    const result = await withIdempotency(`${userId}:${hashCart(items)}`, async () => {
      // CONCURRENT fan-out. Promise.all, never sequential await.
      // Sequential here makes every edge critical and kills the demo.
      const [user, stock, charge] = await Promise.all([
        userClient.getUser(userId),
        productClient.checkStock(items),
        paymentClient.charge(userId, amount),
      ]);

      if (!stock.available) {
        await paymentClient.refund(charge.id);   // saga compensation
        return { status: 409, body: { error: 'out_of_stock' } };
      }

      const order = await db.createOrder({ userId, items, amount, chargeId: charge.id });
      return { status: 201, body: { orderId: order.id, user: user.name } };
    });

    res.status(result.status).json(result.body);
  } catch (err) {
    next(err);
  }
});
```

```js
// microservices/order/src/clients.js — timeouts, retry with jitter, breaker
import CircuitBreaker from 'opossum';
import { setTimeout as sleep } from 'node:timers/promises';

const TIMEOUTS = { user: 2000, product: 2000, payment: 500 };

async function callWithRetry(fn, { attempts = 3 } = {}) {
  let lastErr;
  for (let i = 0; i < attempts; i++) {
    try {
      return await fn();
    } catch (err) {
      lastErr = err;
      if (!isRetryable(err) || i === attempts - 1) throw err;
      // exponential backoff WITH jitter — without jitter, retries synchronise
      // across pods and you get a thundering herd on an already-sick service
      const base = 100 * 2 ** i;
      await sleep(base / 2 + Math.random() * base);
    }
  }
  throw lastErr;
}

const paymentBreaker = new CircuitBreaker(
  (userId, amount) => callWithRetry(() => post('/charge', { userId, amount },
                                               TIMEOUTS.payment)),
  { errorThresholdPercentage: 50, resetTimeout: 30_000, rollingCountTimeout: 60_000 },
);

// Breaker transitions belong in the trace — a waterfall showing the breaker
// opening mid-incident is a strong artifact.
import { trace } from '@opentelemetry/api';
for (const ev of ['open', 'halfOpen', 'close']) {
  paymentBreaker.on(ev, () =>
    trace.getActiveSpan()?.addEvent('circuit_breaker', { state: ev, target: 'payment-svc' }));
}

export const paymentClient = {
  charge: (userId, amount) => paymentBreaker.fire(userId, amount),
  refund: (chargeId) => post('/refund', { chargeId }, TIMEOUTS.payment),
};
```

**Verify concurrency after any refactor.** Pull one trace and confirm the three
child spans overlap in time:

```bash
curl -s "http://localhost:16686/api/traces?service=order-svc&limit=1" \
  | jq -r '.data[0].spans[] | "\(.operationName) start=\(.startTime) dur=\(.duration)"'
```

Staggered end-to-start means the fan-out regressed. The decoy eval case will
start failing for a reason that has nothing to do with the detector.

---

## 6. `payment-svc` — the fault target (Python 3.14)

```python
# microservices/payment/main.py
import asyncio, os, random
from fastapi import FastAPI, HTTPException
from opentelemetry import trace

from faults import fault_config          # see section 9

app = FastAPI()
tracer = trace.get_tracer(__name__)
BASE_MS = int(os.environ.get("SVC_PAYMENT_BASE_MS", "90"))


@app.post("/charge")
async def charge(body: dict):
    cfg = fault_config.for_endpoint("/charge")
    span = trace.get_current_span()

    # Make injected faults VISIBLE in telemetry. An invisible fault is
    # indistinguishable from a real regression, and you will eventually spend
    # an hour debugging your own injector.
    if cfg.delay_ms or cfg.error_rate:
        span.set_attribute("tracemap.fault.injected", True)
        span.set_attribute("tracemap.fault.delay_ms", cfg.delay_ms)

    await asyncio.sleep((BASE_MS + cfg.delay_ms) / 1000)

    if cfg.error_rate and random.random() < cfg.error_rate:
        raise HTTPException(status_code=502, detail="payment_provider_error")

    async with db.transaction():          # produces a real DB span
        charge_id = await db.insert_payment(body["userId"], body["amount"])
    return {"id": charge_id, "status": "captured"}


@app.get("/healthz")                       # process liveness ONLY
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")                        # dependencies checked HERE
async def readyz():
    if not await db.ping():
        raise HTTPException(status_code=503, detail="db_unreachable")
    return {"status": "ready"}
```

The `/healthz` and `/readyz` split is not boilerplate. Check the database in
liveness and one slow query restarts every pod — a degradation becomes an
outage.

---

## 7. `product-svc` — the N+1 opportunity (Go 1.26)

Go has no runtime auto-instrumentation agent, so wrap handlers explicitly:

```go
// microservices/product/cmd/product/main.go
package main

import (
    "github.com/gin-gonic/gin"
    "go.opentelemetry.io/contrib/instrumentation/github.com/gin-gonic/gin/otelgin"
)

var gitSHA string // set via -ldflags at build time

func main() {
    r := gin.New()
    r.Use(otelgin.Middleware("product-svc"))   // explicit — no agent exists for Go
    r.POST("/stock/check", checkStock)
    r.GET("/healthz", func(c *gin.Context) { c.JSON(200, gin.H{"status": "ok"}) })
    r.GET("/readyz", readyz)
    _ = r.Run(":8080")
}

func checkStock(c *gin.Context) {
    var req StockRequest
    if err := c.ShouldBindJSON(&req); err != nil {
        c.JSON(400, gin.H{"error": "bad_request"}); return
    }

    ctx := c.Request.Context()

    // Deliberate N+1 above a cart-size threshold. This gives the Jaeger
    // waterfall something genuinely diagnosable — twelve identical queries
    // where there should have been one — which is the drill-in demo AND the
    // raw material for POSTMORTEM-001.
    if len(req.Items) > nPlusOneThreshold {
        avail := true
        for _, item := range req.Items {
            ok, err := repo.CheckOne(ctx, item.SKU)   // one span per item
            if err != nil { c.JSON(500, gin.H{"error": "db"}); return }
            avail = avail && ok
        }
        c.JSON(200, gin.H{"available": avail}); return
    }

    avail, err := repo.CheckBatch(ctx, req.SKUs())    // the good path, one span
    if err != nil { c.JSON(500, gin.H{"error": "db"}); return }
    c.JSON(200, gin.H{"available": avail})
}
```

---

## 8. `gateway-svc` — entry point and degradation

```js
// microservices/gateway/src/routes/products.js — graceful degradation
router.get('/products', async (req, res) => {
  try {
    const catalog = await productClient.list();
    await cache.set('catalog', catalog, { ttl: 300 });
    res.json(catalog);
  } catch (err) {
    const stale = await cache.get('catalog');
    if (stale) {
      // Serving stale beats serving nothing — and say so in a header
      res.set('X-Stale', 'true').json(stale);
      trace.getActiveSpan()?.addEvent('degraded', { reason: 'product_svc_unavailable' });
      return;
    }
    res.status(503).json({ error: 'catalog_unavailable' });
  }
});
```

---

## 9. Fault-flag control plane

Faults are read **per request** from mutable config. Never restart to apply a
fault: a restart perturbs latency on its own, so restarting to inject a latency
fault measures the restart.

```python
# microservices/payment/faults.py  (mirror this in the Node services)
import os, time
from dataclasses import dataclass


@dataclass(frozen=True)
class Fault:
    delay_ms: int = 0
    error_rate: float = 0.0
    cpu_burn_ms: int = 0


class FaultConfig:
    """Runtime-mutable fault state with a TTL backstop.

    The TTL matters: if the eval runner crashes mid-case, the fault expires on
    its own instead of poisoning every subsequent measurement.
    """

    def __init__(self) -> None:
        self._fault = Fault()
        self._endpoint = os.environ.get("TRACEMAP_FAULT_ENDPOINT", "")
        self._expires_at = 0.0
        self._load_from_env()

    def _load_from_env(self) -> None:
        delay = int(os.environ.get("TRACEMAP_FAULT_DELAY_MS", "0"))
        rate = float(os.environ.get("TRACEMAP_FAULT_ERROR_RATE", "0"))
        if delay or rate:
            self.set(Fault(delay, rate), self._endpoint,
                     ttl_s=int(os.environ.get("TRACEMAP_FAULT_TTL_S", "600")))

    def set(self, fault: Fault, endpoint: str, ttl_s: int) -> None:
        self._fault, self._endpoint = fault, endpoint
        self._expires_at = time.monotonic() + ttl_s

    def clear(self) -> None:                    # idempotent — called from finally
        self._fault, self._expires_at = Fault(), 0.0

    def for_endpoint(self, path: str) -> Fault:
        if time.monotonic() > self._expires_at:
            return Fault()                      # expired: heal automatically
        if self._endpoint and self._endpoint != path:
            return Fault()                      # scoped to one endpoint
        return self._fault


fault_config = FaultConfig()
```

Expose a control endpoint the eval injectors drive. Bind it to the internal
network only — it must never be reachable from the public ingress:

```python
@app.post("/_internal/fault")
async def set_fault(body: dict):
    fault_config.set(Fault(body.get("delay_ms", 0), body.get("error_rate", 0.0)),
                     body.get("endpoint", ""), body.get("ttl_s", 600))
    return {"ok": True}


@app.delete("/_internal/fault")
async def clear_fault():
    fault_config.clear()
    return {"ok": True}
```

---

## 10. Application schema (PostgreSQL 18)

Small on purpose — this is a trace generator, not an e-commerce product. Real
foreign keys and a real transaction, because a DB span with a rollback is a
more interesting trace than one without.

```sql
-- microservices/schema.sql
CREATE TABLE users (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT NOT NULL,
    email      TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE products (
    sku        TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    price_cents INT NOT NULL CHECK (price_cents >= 0),
    stock      INT NOT NULL CHECK (stock >= 0)
);

CREATE TABLE orders (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id),
    amount_cents INT  NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('pending','paid','failed','refunded')),
    idem_key     TEXT UNIQUE,          -- retries must not double-charge
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE order_items (
    order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    sku      TEXT NOT NULL REFERENCES products(sku),
    qty      INT  NOT NULL CHECK (qty > 0),
    PRIMARY KEY (order_id, sku)
);

CREATE TABLE payments (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id   UUID REFERENCES orders(id),
    status     TEXT NOT NULL CHECK (status IN ('captured','refunded','failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON orders (user_id, created_at DESC);
```

Strip parameters from `db.statement` before it reaches a span. Spans are
retained for days and readable by anyone with dashboard access.

---

## 11. k6 load scripts

```js
// load-testing/normal-checkout-flow.js
import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  scenarios: {
    // Mild diurnal variation. A detector tuned against perfectly flat traffic
    // produces false positives the moment real traffic breathes.
    baseline: {
      executor: 'ramping-arrival-rate',
      startRate: 8, timeUnit: '1s',
      preAllocatedVUs: 20, maxVUs: 60,
      stages: [
        { target: 12, duration: '5m' },
        { target: 20, duration: '5m' },
        { target: 12, duration: '5m' },
        { target: 8,  duration: '5m' },
      ],
    },
  },
  thresholds: {                       // these double as the CI SLO gate
    http_req_duration: ['p(95)<400'],
    http_req_failed:   ['rate<0.01'],
  },
};

const BASE = __ENV.BASE_URL || 'http://localhost:3000';

export default function () {
  if (Math.random() < 0.3) {          // 30% browse
    check(http.get(`${BASE}/products`), { 'browse 200': (r) => r.status === 200 });
    sleep(0.5 + Math.random());
    return;
  }
  const payload = JSON.stringify({    // 70% checkout
    userId: `user-${Math.ceil(Math.random() * 50)}`,
    items: [{ sku: `SKU-${Math.ceil(Math.random() * 20)}`, qty: 1 }],
    amount: 1999,
  });
  const res = http.post(`${BASE}/checkout`, payload,
                        { headers: { 'Content-Type': 'application/json' } });
  check(res, { 'checkout 2xx': (r) => r.status >= 200 && r.status < 300 });
  sleep(0.5 + Math.random());
}
```

```js
// load-testing/induce-payment-slowdown.js — the manual screen-share demo
import http from 'k6/http';
export const options = { vus: 1, iterations: 1 };

export default function () {
  http.post('http://payment-svc:8000/_internal/fault',
    JSON.stringify({ delay_ms: 60, endpoint: '/charge', ttl_s: 300 }),
    { headers: { 'Content-Type': 'application/json' } });
  console.log('injected +60ms into payment-svc /charge; expires in 300s');
}
```

---

## 12. Wiring the platform workers

Four workers, all following the same never-die shape. The authoritative logic
lives in the skills named beside each one.

```python
# platform/workers/_runner.py — shared supervision for every worker
import asyncio, logging
from prometheus_client import Counter

log = logging.getLogger(__name__)
CYCLES = Counter("tracemap_worker_cycle_total", "Worker cycles", ["worker"])


async def run_forever(name: str, cycle, interval_s: float) -> None:
    """A worker that dies quietly is the worst failure in this system:
    everything looks fine and nothing is being watched. So: never exit, and
    emit a heartbeat every cycle so absence is alertable."""
    while True:
        try:
            await cycle()
            CYCLES.labels(worker=name).inc()
        except Exception:
            log.exception("%s cycle failed", name)     # log, never re-raise
        await asyncio.sleep(interval_s)
```

| Worker | Interval | Logic lives in |
|---|---|---|
| `trace_assembler_worker` | 15s | `tracemap-critical-path` |
| `graph_builder_worker` | 10s | `tracemap-clickhouse-analytics` (Q1/Q2) |
| `anomaly_detector_worker` | 60s | `tracemap-anomaly-eval` |
| `deploy_watcher_worker` | watch stream | `tracemap-deploy-attribution` |

Alert on `absent_over_time(tracemap_worker_cycle_total[5m])` per worker. That
single rule covers the silent-failure mode for all four.

---

## 13. First-run checklist

Work down this list; each step is independently verifiable, so a failure tells
you exactly which layer broke.

1. `cp .env.example .env` and set `GIT_SHA=$(git rev-parse --short HEAD)`.
2. `STORAGE_PROFILE=badger make up` — Badger first, so a ClickHouse problem
   cannot be confused with an instrumentation problem.
3. `curl -X POST localhost:3000/checkout -d '{...}'` returns 2xx.
4. Jaeger UI at :16686 shows one complete trace spanning all five services.
5. **Confirm the fan-out overlaps** using the `jq` command in section 5. If the
   three child spans are staggered, stop and fix it now — everything downstream
   depends on it.
6. Switch to `STORAGE_PROFILE=clickhouse`, apply the schema, confirm the same
   span appears in both Jaeger and `tracemap.spans`.
7. Start the assembler; confirm `trace_edges` has a row with `critical_ms = 0.0`
   for a parallel sibling and a non-zero value for payment.
8. `make demo` and open the dashboard — the graph should be populated, not empty.
9. Inject the 60ms fault; confirm an anomaly appears within ~3 detector cycles.
10. Only now start writing eval cases.

Step 7 is the one to celebrate. A zero next to a 700ms wall-clock edge is the
entire thesis of the project showing up in a database row for the first time.
