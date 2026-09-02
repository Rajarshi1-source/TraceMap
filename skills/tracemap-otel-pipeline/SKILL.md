---
name: tracemap-otel-pipeline
description: Build and maintain TraceMap's telemetry ingestion path — OpenTelemetry auto-instrumentation across Node.js 24 / Python 3.14 / Go 1.26, the two-tier Collector topology (agent DaemonSet plus gateway Deployment), tail sampling, the loadbalancing exporter, spanmetrics, persistent queues, and Jaeger v2.20 storage profiles. Use for ANY work on instrumentation, OTLP export, sampling policy, collector YAML, Jaeger configuration, or trace storage backends — even when the user only says "traces aren't showing up", "add tracing to this service", "tune sampling", or "wire up Jaeger". MANDATE - Jaeger v1 is END OF LIFE since 31 Dec 2025, so never generate v1 component YAML (jaeger-collector, jaeger-agent, jaeger-query as separate binaries); v2 is one binary on the OTel Collector framework configured by YAML roles. Never claim zero-code instrumentation for Go. Pair with tracemap-clickhouse-analytics for what happens to spans after export.
---

# TraceMap — OpenTelemetry Pipeline

This skill owns everything from "a request enters a service" to "a span is durably written to storage." Everything downstream of that belongs to `tracemap-clickhouse-analytics`.

## Version pins (verified 20 Aug 2026 — re-check before quoting)

| Component | Pin | Why it matters |
|---|---|---|
| OTel Collector contrib | **v0.158.x** | Need contrib for `tail_sampling`, `spanmetrics`, `clickhouse`, `loadbalancing` |
| Jaeger | **v2.20.x** | v1 reached EOL 31 Dec 2025; v2 is one binary on the Collector framework |
| Node.js | **24 LTS** | Node 20 went EOL April 2026 — never emit `node:20-alpine` |
| Python | **3.14** | |
| Go | **1.26** | |
| ClickHouse (trace store) | **26.3 LTS** | Jaeger native backend is alpha behind a feature gate |
| OpenSearch (fallback) | **3.x** | Jaeger recommends OpenSearch over Cassandra at large scale |

Elasticsearch 6 support was **removed** in Jaeger v2.20. If a user is on ES 6, that is a migration, not a config change.

## The three things that go wrong most often

### 1. Tail sampling silently misbehaves with more than one gateway replica

Tail sampling requires **every span of a trace to reach the same collector instance**. With two replicas and no trace-aware routing, each replica sees a partial trace and makes an arbitrary decision. Traces come out half-sampled and nobody notices until the data looks thin.

The fix is a two-tier topology. The agent tier does no sampling; it routes by trace ID into the sampling tier:

```yaml
# agent tier (DaemonSet) — no sampling here
exporters:
  loadbalancing:
    routing_key: traceID          # this line is the whole point
    protocol:
      otlp:
        tls: { insecure: true }
    resolver:
      k8s:
        service: otel-gateway.observability.svc.cluster.local
```

If the deployment genuinely runs a single gateway replica, it is fine to skip `loadbalancing` — but say so explicitly in a comment, because "we only have one replica" is a decision, and silently omitting it looks like an oversight.

### 2. Sampling before metric generation makes dashboards lie

`spanmetrics` derives RED metrics from spans. If it sits downstream of the sampling decision, every metric is scaled by the sampling rate and request-rate panels under-report. Generate metrics from the full stream, then sample for storage.

### 3. Tail sampler memory is the real capacity limit

Rough sizing: `avg_trace_size × traces_per_sec × decision_wait`. At 200 tps, 10s wait, 8KB traces that is ~16MB. At 5000 tps it is ~400MB and one traffic spike from an OOM kill.

Knobs worth knowing (they signal currency in 2026): the processor gained a `sampling_strategy` option in v0.149.0, and Pebble-backed disk buffering lets operators raise `decision_wait` and `num_traces` without OOM, trading CPU for RAM.

## Instrumentation per language

```bash
# Node.js 24 — true zero-code via the auto-instrumentation agent
NODE_OPTIONS="--require @opentelemetry/auto-instrumentations-node/register"
OTEL_SERVICE_NAME=gateway-svc
OTEL_RESOURCE_ATTRIBUTES="service.version=${GIT_SHA},deployment.environment=prod"
OTEL_EXPORTER_OTLP_ENDPOINT="http://$(NODE_IP):4317"
OTEL_EXPORTER_OTLP_PROTOCOL=grpc
OTEL_TRACES_SAMPLER=parentbased_always_on   # head sampling OFF — tail decides

# Python 3.14
opentelemetry-instrument --service_name payment-svc python -m uvicorn main:app

# Go 1.26 — NO runtime agent exists. Wrap handlers explicitly:
#   otelhttp.NewHandler(mux, "server")  /  otelgin.Middleware("product-svc")
# or instrument out-of-process with eBPF (OBI).
```

**Never write "zero code changes" as a blanket claim.** Node and Python get it via agents; Go is compiled with no runtime hook, so it needs middleware or eBPF. Overclaiming this is the fastest way to lose a reviewer's trust. Frame the limitation as the motivation for the eBPF path instead.

`service.version=${GIT_SHA}` is not optional garnish — deploy attribution and trace-to-code both key off it. Set it on day one; backfilling it means a stretch of traces that cannot be correlated to a release.

Point services at the **node-local** agent via the downward API, never at a cluster-wide service:

```yaml
env:
  - name: NODE_IP
    valueFrom:
      fieldRef: { fieldPath: status.hostIP }
  - name: OTEL_EXPORTER_OTLP_ENDPOINT
    value: "http://$(NODE_IP):4317"
```

## Gateway tier config (the shape to reproduce)

```yaml
processors:
  memory_limiter:
    check_interval: 1s
    limit_percentage: 75
    spike_limit_percentage: 20

  tail_sampling:
    decision_wait: 10s
    num_traces: 50000
    expected_new_traces_per_sec: 200
    policies:
      - name: keep-all-errors
        type: status_code
        status_code: { status_codes: [ERROR] }
      - name: keep-slow-traces
        type: latency
        latency: { threshold_ms: 500 }
      - name: keep-checkout-flow          # the demo path, always sampled
        type: string_attribute
        string_attribute:
          key: http.route
          values: ["/checkout"]
      - name: baseline-probabilistic      # keeps the sample unbiased
        type: probabilistic
        probabilistic: { sampling_percentage: 20 }

connectors:
  spanmetrics:
    histogram:
      explicit: { buckets: [5ms, 10ms, 25ms, 50ms, 100ms, 250ms, 500ms, 1s, 2s] }
    dimensions:
      - name: http.route
      - name: service.version

exporters:
  otlp/jaeger:
    endpoint: jaeger-collector.observability.svc:4317
    tls: { insecure: true }
    sending_queue: { enabled: true, storage: file_storage }
    retry_on_failure: { enabled: true, max_elapsed_time: 300s }
  clickhouse:
    endpoint: tcp://clickhouse.observability.svc:9000
    database: tracemap
    traces_table_name: spans
    ttl: 168h
    sending_queue: { enabled: true, storage: file_storage }
    retry_on_failure: { enabled: true }

extensions:
  file_storage:
    directory: /var/lib/otelcol/queue    # survives restarts — no span loss
```

Always attach `file_storage` to the sending queues. Without it, a collector restart during a storage blip drops everything in flight, and that is exactly when the data matters most.

Keep the **probabilistic baseline policy**. Without it, storage contains only errors and slow traces, and any percentile or blame-share computed downstream is measured over a biased sample.

## Jaeger v2 storage profiles

Native ClickHouse storage landed in Jaeger v2.18 and is **alpha, behind a feature gate**. Depending on an alpha feature is acceptable; depending on it with no fallback is not. Model the backend as a profile, not a hardcoded choice:

```
STORAGE_PROFILE=clickhouse   # default — Jaeger writes to the `jaeger` database
STORAGE_PROFILE=opensearch   # fallback if the gate breaks on upgrade
STORAGE_PROFILE=badger       # laptop and CI — embedded, zero infra
```

Three YAML files, one env var, and at least two profiles exercised in CI. Jaeger owns the `jaeger` database; TraceMap's own analytics live in a separate `tracemap` database on the same server. Never write to Jaeger's tables and never build materialized views on top of them — the alpha schema is free to change underneath.

## Self-monitoring

Set `service.telemetry.metrics.level: detailed` and alert on:

- `otelcol_processor_dropped_spans` rate above zero for 5m
- exporter queue depth trending up (storage is refusing writes)
- `otelcol_receiver_refused_spans` (backpressure reaching the apps)

The platform should emit its own OTLP into its own pipeline. "The dependency map contains the dependency map" is both a good demo moment and genuinely useful when the assembler gets slow.

## What not to do

- Do not generate Jaeger v1 YAML or reference `jaeger-agent` — it was removed from distributions.
- Do not put `tail_sampling` in the agent tier; it will make decisions on partial traces.
- Do not enable head sampling in the SDK alongside tail sampling unless the interaction is deliberate and documented.
- Do not record `db.statement` with literal parameter values, or full auth headers. Spans are retained for days and readable by anyone with dashboard access.
- Do not set `decision_wait` above ~30s without recomputing the memory budget.

## Interview soundbites

- *"Tail sampling needs every span of a trace in one collector, so the agent tier load-balances by trace ID into the sampling tier. Skip that and you sample partial traces without knowing it."*
- *"I generate span metrics before the sampling decision. If you derive RED metrics from sampled traces, your request-rate panel is off by the sampling factor and nobody notices."*
- *"I depend on an alpha ClickHouse backend, so I designed for it to fail — the storage backend is a profile, one env var swaps to OpenSearch or Badger, and CI runs two of them."*

## Pairing

- `tracemap-clickhouse-analytics` — schema and queries for exported spans
- `tracemap-critical-path` — the assembler that consumes `tracemap.spans`
- `tracemap-platform-ops` — Helm, probes, CI gates for this pipeline
