---
name: tracemap-platform-ops
description: Own TraceMap's delivery and operations layer — multi-stage Dockerfiles on Node 24 / Python 3.14 / Go 1.26 distroless, the Helm chart with startup-liveness-readiness probes and PodDisruptionBudgets, k3s 1.36 deployment with TLS, docker-compose profiles for the one-command demo, GitHub Actions with k6 SLO and detector-eval gates, multi-window burn-rate alerting, platform self-monitoring, and the telemetry cost meter. Use for ANY work on Dockerfiles, Helm, Kubernetes manifests, probes, resource sizing, CI/CD, deployment strategy, SLOs, PromQL rules, Grafana dashboards, or observability cost — and when the user says "it OOMs", "one-command demo", "gate the pipeline", or "what does this cost". MANDATE - never emit node:20-alpine, EOL since April 2026; never check downstream dependencies in a liveness probe, since one slow database then restarts the whole fleet; and size hosts from measured component floors, because this stack needs roughly 7GB and OOMs on a 4GB box.
---

# TraceMap — Platform, Delivery and Operations

Everything that gets TraceMap running, deployed, gated and measured.

## Version pins (verified 20 Aug 2026)

| Component | Pin | Note |
|---|---|---|
| Kubernetes / k3s | **1.36.x** | 1.33 is EOL; 1.34 EOLs 27 Oct 2026 |
| Helm | **3.x** | |
| Node base image | **`node:24-alpine`** | Node 20 EOL April 2026 |
| Python base | **`python:3.14-slim`** | |
| Go build / final | **`golang:1.26-alpine`** then `gcr.io/distroless/static-debian12:nonroot` | ~12MB final image |
| Prometheus | **3.14** | |
| Grafana | **13.2** | |
| PostgreSQL | **18** | |
| Redis | **8** | |
| ClickHouse | **26.3 LTS** | |
| Registry | GHCR | Free for public repos |

## Sizing — do this before buying a host

This stack does not fit in 4GB. Measured component floors:

| Component | RAM floor |
|---|---|
| ClickHouse | 2.0 GB |
| Jaeger v2 (query + collector roles) | 0.5 GB |
| PostgreSQL 18 | 0.3 GB |
| Redis 8 | 0.2 GB |
| 5 microservices | 1.0 GB |
| OTel Collector (incl. tail-sampling buffer) | 0.5 GB |
| Prometheus + Grafana | 1.0 GB |
| Next.js + FastAPI + workers | 0.8 GB |
| k3s itself | 0.7 GB |
| **Total** | **~7 GB** |

Target Hetzner CX32 (4 vCPU / 8GB, ~EUR 7/mo) or Oracle Cloud Always-Free (4 ARM cores / 24GB, EUR 0 where capacity exists). Put this table in `docs/SCALING.md` — "I sized my deployment from measured component floors" is a far better answer than "I picked the cheapest box."

## Dockerfiles

```dockerfile
# Node.js 24 LTS — NOT node:20
FROM node:24-alpine AS deps
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev

FROM node:24-alpine AS runtime
WORKDIR /app
RUN addgroup -S app && adduser -S app -G app
COPY --from=deps --chown=app:app /app/node_modules ./node_modules
COPY --chown=app:app . .
USER app
ENV NODE_OPTIONS="--require @opentelemetry/auto-instrumentations-node/register"
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://localhost:3000/healthz || exit 1
CMD ["node", "server.js"]
```

```dockerfile
# Go 1.26 — distroless final stage, ~12MB
FROM golang:1.26-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -ldflags="-s -w -X main.gitSHA=${GIT_SHA}" -o /out/product ./cmd/product

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/product /product
USER nonroot:nonroot
EXPOSE 8080
ENTRYPOINT ["/product"]
```

Image-size talking point: Node ~130MB, Python-slim ~110MB, Go-distroless ~12MB. *"The Go service ships distroless — no shell, no package manager, minimal CVE surface. The Node one can't, because the OTel agent needs the runtime."*

Run as non-root everywhere. Inject `GIT_SHA` at build time so it can flow into `service.version`.

## Probes — the distinction that gets asked

`/healthz` answers **"should I be restarted?"** and must check only that the process is alive. `/readyz` answers **"should I get traffic?"** and may check dependencies.

Putting a dependency check in a liveness probe is the most common Kubernetes mistake in interviews: one slow database makes every pod fail liveness, Kubernetes restarts them all, and a degradation becomes an outage. Use a **startup probe** for slow starters so liveness does not kill a booting pod.

```yaml
resources:
  requests: { cpu: 50m,  memory: 96Mi }    # requests drive scheduling
  limits:   { cpu: 300m, memory: 256Mi }
startupProbe:
  httpGet: { path: /healthz, port: 8000 }
  failureThreshold: 20
  periodSeconds: 3
livenessProbe:
  httpGet: { path: /healthz, port: 8000 }   # process only — no dependencies
  periodSeconds: 10
readinessProbe:
  httpGet: { path: /readyz, port: 8000 }    # dependencies checked here
  periodSeconds: 5
```

Also ship: `PodDisruptionBudget` with `minAvailable: 1` per service, an HPA on the analytics API at 70% CPU, a `NetworkPolicy` restricting ClickHouse to the collector and analytics API, and StatefulSets with PVCs for ClickHouse, Postgres and Redis. Label every pod with `git-sha` so the deploy watcher can record it.

## docker-compose — the one-command demo

Use profiles so a laptop can run a subset:

```bash
docker compose --profile core up      # services + tracing + dashboard
docker compose --profile full up      # + Prometheus, Grafana, eval harness
docker compose --profile eval up      # CI: core + injectors + runner
```

Add a `make demo` target that boots `core`, waits for health, and runs 60 seconds of k6 before opening the browser. **A demo that opens on an empty graph is a demo you have lost** — the reviewer's first impression is a blank canvas and no amount of explanation recovers it.

## CI/CD gates

| Stage | Gate | Blocks merge |
|---|---|---|
| Lint + typecheck | ruff, mypy strict, eslint, tsc | yes |
| Unit tests | pytest, vitest, go test — **including critical-path tests** | yes |
| Build + scan | buildx multi-arch to GHCR, Trivy | yes on HIGH/CRITICAL |
| Migrations | ClickHouse + Postgres apply to a clean DB | yes |
| Integration | kind cluster, compose `eval` profile, smoke test | yes |
| **k6 SLO gate** | p95 < 400ms, error rate < 1% at 50 RPS | yes |
| **Detector eval gate** | F1 within 0.03 of baseline, MTTD p95 within 1.2x | yes on detector paths |
| **Storage-profile matrix** | boots with `clickhouse` **and** `badger` | yes |
| Deploy | Helm upgrade to k3s on tag | – |

```yaml
  k6-slo-gate:
    steps:
      - run: docker compose --profile core up -d --wait
      - run: k6 run --out json=result.json load-testing/normal-checkout-flow.js
      - name: Enforce SLO
        run: |
          P95=$(jq '.metrics.http_req_duration.values["p(95)"]' result.json)
          ERR=$(jq '.metrics.http_req_failed.values.rate' result.json)
          echo "p95=${P95}ms error_rate=${ERR}"
          awk -v p="$P95" -v e="$ERR" 'BEGIN{exit !(p<400 && e<0.01)}' \
            || { echo "::error::SLO gate failed"; exit 1; }

  storage-profile-matrix:
    strategy:
      matrix:
        profile: [clickhouse, badger]
    steps:
      - run: STORAGE_PROFILE=${{ matrix.profile }} docker compose up -d --wait
      - run: ./scripts/smoke-test.sh
```

The storage-profile matrix is what turns "I have a fallback" into "my fallback is tested." Claiming an untested fallback is worse than having none.

## Deployment strategy

| Strategy | Use | Honest note |
|---|---|---|
| Rolling update | Default | `maxSurge: 1, maxUnavailable: 0`; only safe if readiness probes are correct |
| Blue-green | Dashboard swap demo | Doubles resources briefly — a real constraint on an 8GB box |
| Canary | Discuss for payment-svc | Can be gated on **critical-path p99** using this project's own signal |

The canary point is the best closing line in a system-design round: *"my platform already measures per-edge critical-path latency, so it can act as the analysis provider for a progressive rollout — promote only if the canary's critical path stays within 10% of stable."*

Public URL: Cloudflare A record to the k3s node, Traefik ingress (bundled with k3s), cert-manager with Let's Encrypt. **Authenticate write endpoints.** Do not put an unauthenticated write API on the public internet and then link it on a resume.

## SLOs and burn-rate alerting

SLOs for the traced app: checkout availability 99.5%, latency p95 under 400ms, **critical-path p99 under 600ms**.

SLOs for the platform itself — this is the part that reads as SRE thinking:

| SLI | Target | Why |
|---|---|---|
| Span ingestion completeness | >= 99.9% stored | Dropped spans are silent blind spots |
| Graph freshness | 95% of renders under 60s stale | The consistency budget |
| Detector MTTD | 95% of injected faults under 432s | Measured continuously; 180s is below the detector's own 3-cycle confirmation floor before any pipeline time |
| Detector precision | >= 90% | Alert fatigue is a human availability problem |
| MV/reconciler agreement | within 2% | Guards the materialized-view bug class |

```yaml
- alert: CheckoutErrorBudgetFastBurn
  expr: |
    job:checkout_error_ratio:5m > (14.4 * 0.005)
    and
    (sum(rate(traces_span_metrics_calls_total{http_route="/checkout",status_code="STATUS_CODE_ERROR"}[1h]))
     / sum(rate(traces_span_metrics_calls_total{http_route="/checkout"}[1h]))) > (14.4 * 0.005)
  for: 2m
  labels: { severity: page }

- alert: DetectorHeartbeatMissing
  expr: absent_over_time(tracemap_detector_cycle_total[5m])
  for: 3m
  labels: { severity: page }
  annotations: { summary: "Detector worker silent — alerting is BLIND" }

- alert: ReconcilerDivergence
  expr: abs(tracemap_edges_mv_total - tracemap_edges_recon_total)
        / tracemap_edges_recon_total > 0.02
  for: 10m
  labels: { severity: ticket }
```

**Why two windows:** the long window (1h) confirms a real budget burn; the short window (5m) confirms it is still happening now, so the alert resolves on recovery instead of paging for an hour afterwards.

Five dashboards: golden signals, dependency health, platform self-monitoring (collector accepted/refused/dropped spans, exporter queue depth, ClickHouse insert rate and part count, assembler lag, `system.view_refreshes`), detector scoreboard, and cost.

## Cost meter

A `/cost` page answering what observability costs and what changes it:

| Metric | Source |
|---|---|
| Spans/sec accepted, dropped | `otelcol_receiver_accepted_spans`, `otelcol_processor_dropped_spans` |
| Bytes per span (compressed) | ClickHouse `system.parts` / row count |
| Storage growth per day | `sum(bytes_on_disk)` by partition |
| Projected monthly cost | growth x cost per GB |
| **Savings from sampling** | `(1 - sampled/accepted) x unsampled projection` |

One or two days of work for a sentence no other candidate says: *"my tracing stack costs about eight euros a month and tail sampling saves 68% of the storage I'd otherwise pay for."* SREs are judged on cost; show that you know it.

## What not to do

- Do not use `node:20-alpine` or any EOL base image.
- Do not check dependencies in a liveness probe.
- Do not omit resource requests — they drive scheduling, not just limits.
- Do not deploy to a 4GB host and be surprised by OOM kills.
- Do not expose write endpoints without auth.
- Do not claim an untested fallback profile.
- Do not skip `make demo` traffic seeding before a screen-share.

## Interview soundbites

- *"Liveness answers 'should I be restarted', readiness answers 'should I get traffic'. Check a database in liveness and one slow query restarts your whole fleet."*
- *"I sized the host from measured component floors — about 7GB — rather than picking the cheapest VPS and hoping."*
- *"CI has two gates most projects don't: a k6 SLO gate and a detector-eval gate. I broke the detector in a draft PR to prove the second one actually fails."*

## Pairing

- `tracemap-otel-pipeline` — collector deployment topology
- `tracemap-anomaly-eval` — the eval gate this pipeline runs
- `tracemap-nextjs-dashboard` — the surface being deployed
