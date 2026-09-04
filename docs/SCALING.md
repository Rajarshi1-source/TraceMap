# Scaling: what this costs to run, and what breaks first

Two numbers get quoted about this stack and they are not the same number.
Confusing them is how a host gets undersized, so this document separates them
and says which is measured.

| | Value | Kind |
|---|---|---|
| Working set, core profile at 20 rps | **3.12 GiB** | measured, `docker stats` under live load |
| Provisioning floor | **~7 GB** | derived, and what you buy a host against |

The second is not padding on the first. It is a different question.

---

## 1. The measured working set

Taken from a running stack under the eval harness's own load — sixteen
containers, 20 requests per second through the full checkout path, ClickHouse
actively ingesting spans. Not idle, not synthetic.

| Component | RSS | Share |
|---|---|---|
| ClickHouse | 1194 MiB | 37% |
| Jaeger v2 (query + collector roles) | 561 MiB | 18% |
| OTel Collector | 399 MiB | 12% |
| order-svc | 279 MiB | 9% |
| gateway-svc | 125 MiB | 4% |
| PostgreSQL 18 | 100 MiB | 3% |
| payment-svc, product-svc, user-svc | 132 MiB | 4% |
| trace-assembler | 46 MiB | 1% |
| dashboard (Next.js standalone) | 32 MiB | 1% |
| analytics-api | 24 MiB | <1% |
| Redis 8 | 19 MiB | <1% |
| anomaly-detector, deploy-watcher, graph-builder | 56 MiB | 2% |
| **Total** | **3195 MiB ≈ 3.12 GiB** | |

Three things worth noticing.

**ClickHouse is 37% of the stack and everything else is rounding.** It is the
only component whose memory is worth tuning, and the only one that grows with
retention rather than with traffic.

**The four platform workers cost 56 MiB between them.** The part of this
project that does the interesting work — stitching traces, computing critical
paths, running the detector — is the cheapest part of the bill. That is a
consequence of doing the work in ClickHouse and keeping the workers as
plumbing.

**The Next.js dashboard is 32 MiB** because it ships `output: standalone`,
which bundles only the dependencies actually traced rather than the whole
`node_modules` tree.

## 2. Why the provisioning floor is ~7 GB anyway

You do not buy a host for the working set. You buy it for the worst hour.

| Addition | Cost | Why it is not in the 3.12 GiB |
|---|---|---|
| Prometheus + Grafana | ~1.0 GB | `full` profile only — D2 deliberately keeps them out of `core` |
| ClickHouse headroom | ~1.0 GB | mark cache and query memory grow with retention and dashboard use; 1.2 GiB is a young database |
| Tail-sampling buffer | ~0.3 GB | `num_traces: 50_000` in flight is the collector's worst case, not its resting state |
| k3s itself | ~0.7 GB | not present under docker compose at all |
| OS, page cache, headroom | ~0.9 GB | |

3.12 + 3.9 ≈ **7 GB**, which is where M19's number comes from.

**The consequence, stated plainly.** A 4 GB box fits the working set and dies
the first time ClickHouse merges a large part or Prometheus is switched on.
That failure presents as a tracing outage rather than as a memory problem,
because the OOM killer takes ClickHouse first and everything downstream then
reports empty rather than broken.

Target is **Hetzner CX32** — 4 vCPU / 8 GB, ~€7.05/month (D2). Eight gigabytes
against a seven-gigabyte floor is one gigabyte of headroom, and that is why
Prometheus and Grafana live in the `full` profile and are not resident during
the demo. If you want them always-on, the honest answer is CX42 at ~€14, not
optimism about the floor.

---

## 3. What breaks first at 10× — measured, not guessed

The interesting answer is that it is **not memory**.

Under the same 20 rps sample:

| Component | CPU | Memory |
|---|---|---|
| order-svc | **69–81%** | 279 MiB |
| ClickHouse | 40–86% | 1194 MiB |
| gateway-svc | **30–90%** | 125 MiB |

The two Node services are pinned near a full core while their memory is
irrelevant. At 200 rps they saturate long before anything runs out of RAM.

**Why order-svc and not the database.** order-svc owns the concurrent fan-out
(M1): every checkout is three outbound HTTP calls, three sets of OpenTelemetry
spans, and a saga with compensation logic. It is the busiest process in the
system by construction, and it is the one the flagship feature exists to
measure.

So the breaking order at 10×, in the order it actually happens:

1. **order-svc CPU** — the fan-out. Horizontally scalable; it is stateless.
   Add replicas and a load balancer.
2. **gateway-svc CPU** — same shape, same fix.
3. **The OTel Collector's tail-sampling buffer** — `num_traces` is a count, not
   a byte budget, so 10× the traces is 10× the buffer. `memory_limiter` sheds
   before it OOMs, but shedding means dropped spans, which is a silent blind
   spot (see the `SpanIngestionLoss` alert).
4. **ClickHouse part count** — many small inserts create many small parts,
   merges fall behind, and queries slow down long before anything errors. The
   fix is larger batches, not a bigger box.
5. **Memory**, eventually and least urgently.

**What does NOT scale by adding replicas**, and this is the part worth being
honest about: the four platform workers are singletons on purpose. Two trace
assemblers stitch the same trace twice and write duplicate edges, corrupting
every percentile downstream (F2). Sharding by trace ID would work and is not
worth the complexity at this volume. That is a decision, not an omission — but
it does mean the assembler is a genuine ceiling, and at 10× it is the next
thing to design rather than the next thing to scale.

---

## 4. Storage, which grows with time rather than load

Spans dominate and everything else is noise. The levers, in order of effect:

- **Tail sampling.** The single biggest one. The probabilistic baseline policy
  (M14) keeps 20% of ordinary traces alongside all errors and slow ones. Turn
  it off and storage grows fivefold for traces nobody reads.
- **TTL.** `spans` is retained short, `deploy_markers` for 90 days — the
  markers are tiny and are the join target for every "did a deploy cause this"
  question, so long retention there costs almost nothing and enables long-range
  analysis.
- **Codecs.** `Delta` on timestamps, `Gorilla` on float durations, `ZSTD` on
  strings. Chosen per column rather than globally, because a timestamp and a
  latency compress well under different schemes.

`docs/COST.md` turns these into a monthly figure.

---

## 5. Status

| Claim | Basis |
|---|---|
| 3.12 GiB working set | **Measured** — 16 containers, 20 rps, live ingest |
| ~7 GB provisioning floor | Derived from the measurement plus components not resident in `core` |
| order-svc is the first CPU ceiling | **Measured** — 69–81% at 20 rps |
| Assembler is a singleton ceiling | Reasoned from F2; not load-tested to failure |
| 10× behaviour | **Extrapolated.** Not tested at 200 rps — say so before quoting it |

The last row matters. Everything above 20 rps in this document is an
extrapolation from a measured baseline, not a measurement. The honest sentence
is *"I measured 3.12 GiB and 70% CPU on the fan-out at 20 rps, and here is what
I expect to break first"* — not *"it handles 200 rps"*, which nobody here has
seen.
