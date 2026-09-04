# What this costs to run

**€7.05 a month.** One Hetzner CX32 — 4 vCPU, 8 GB RAM, 80 GB NVMe — and nothing
else.

That is the headline, and it is also the least interesting thing in this
document. The interesting finding is *where the money goes*, because it is not
where people assume, and it changes which lever you reach for when the bill
grows.

> **The bill is the box.** At this scale the marginal cost of storing a span is
> so far below the fixed cost of the host that retention is not the lever. If
> you want to spend less, change the sampling rate. If you want to keep more
> history, keep it — you are already paying for the disk.

Every figure below is either measured, derived, or configuration, and each one
says which. `GET /api/cost` returns the same breakdown computed live against
your own database.

---

## 1. Where the numbers come from

| | Source | Kind |
|---|---|---|
| Bytes on disk, per table | ClickHouse `system.parts` | **measured** |
| Bytes per row, compression ratio | `system.parts` | **measured** |
| Span ingest rate | `count()` over `tracemap.spans`, last hour | **measured** |
| Steady-state disk | each table's own growth, scaled to its TTL | derived |
| Host and volume price | D2, env-overridable | configuration |

The ingest rate is counted from the same database as the byte totals, and that
is deliberate. A rate scraped from the collector divided by a size read from
ClickHouse is two clocks, and the ratio is wrong in a way nobody notices — it
looks plausible in every direction.

**A young database cannot report steady state.** Nothing here has run for
thirty days, let alone ninety, so `/api/cost` reports the age of the data
alongside every projection and refuses to let a four-hour-old database imply a
monthly figure without saying so. Bytes per row is real from the first merge;
the euros are an extrapolation and are labelled as one.

```bash
curl -s localhost:8090/api/cost | jq
```

---

## 2. The model

```
monthly = host                                  # fixed, €7.05
        + max(0, projected_GiB - 80) × €/GiB    # only the overflow
```

Charging for the first 80 GB would be charging twice — the CX32 includes it.
Getting this wrong is the single easiest way to produce a cost model that looks
authoritative and is inflated by an order of magnitude.

### Steady state, per table, from each table's own growth

```
projected(T) = current_bytes(T) × retention_days / min(age_days, retention_days)
```

Read plainly: this table put down X bytes in N days, so at a TTL of R days it
holds X·R/N. Once a table is older than its own retention the factor is 1 and
the projection collapses to its current size — correct, because it is already at
steady state.

**Why not scale everything by the span rate.** Because the tables do not share
an axis:

| Table | Scales with | Retention |
|---|---|---|
| `otel_spans` (landing) | span volume | 1 day |
| `spans` | span volume × sample rate | 7 days |
| `trace_edges`, `trace_critical_path` | **traces**, not spans | 7 days |
| `service_edges_1m`, `service_red_1m`, `edge_slo_1m` | **distinct edges × minutes** | 30 days |
| `service_edges_recon` | as above | 2 days |
| `deploy_markers` | **deploys** | 90 days |

The first version of this model classified tables by name and billed everything
that was not a roll-up as span traffic. Running it produced **`deploy_markers`
at 3,477 GiB** — ninety days of a table that gains a row per release, priced as
ninety days of span volume — and turned a €7.05 bill into €191. `trace_critical_path`
was overstated the same way: one row per trace, projected as one row per span.

Any model that has to guess which axis a table scales on will be wrong about a
table someone adds later. Measuring each table's own growth needs no taxonomy
and cannot be wrong about a table it has never seen. `test_cost_meter.py` pins
the regression.

---

## 3. Measured, on this repository

Taken from a live stack, 4 Sep 2026: sixteen containers, 36 hours of history,
6.9 million spans retained.

| Table | Rows | **Bytes/row** | Compression |
|---|---|---|---|
| `spans` | 6,906,413 | **56.33** | **19.9×** |
| `otel_spans` | 6,897,577 | 49.46 | 13.8× |
| `trace_critical_path` | 243,649 | 311.26 | 4.3× |
| `trace_edges` | 906,176 | 35.06 | 2.5× |
| `service_edges_1m` | 1,364 | 5,022 | 2.1× |

**Fifty-six bytes on disk per span, at nearly twenty-fold compression.** That is
the number the whole model rests on, and it is what the per-column codecs buy:
`Delta` on timestamps, `Gorilla` on float durations, `ZSTD` on strings, chosen
per column rather than globally because a timestamp and a latency compress well
under different schemes.

The roll-ups look expensive per row — 5 KB — and are irrelevant in aggregate:
1,364 rows against 6.9 million. That is the topology bound made visible.

### The resulting bill

| | |
|---|---|
| Projected steady-state disk | **2.71 GiB** |
| Disk included with the box | 80 GB |
| Storage overflow | **0** |
| **Total monthly** | **€7.05** |
| Share of the bill that is the box | **100%** |
| Cost per million spans | €0.0058 |

## 4. Why storage is not the lever — the arithmetic

The raw span table is the only thing on this disk that scales with traffic. So:
how much traffic before it fills the 80 GB the box already includes?

```
spans_per_day_at_breakeven = 80 GB / (56.33 bytes × 7 days)
                           ≈ 203 million spans/day
```

The stack above was measured at **40.8 million spans/day** — and that is not the
quiet demo profile, it is the eval overlay's load generator running hard. **Five
times that traffic, sustained, before one euro of storage appears on the bill.**

> **A caveat the meter raises about its own numbers.** At the time of that
> reading the last-hour ingest rate was 8.9× the database's lifetime average,
> because the load generator had recently started. The projection scales each
> table from its observed growth, so it *understates* steady state by roughly
> that factor while the average catches up. `/api/cost` says so in its `caveat`
> field rather than leaving a reader to notice. Even 8.9× of 2.71 GiB is 24 GiB
> — still inside the 80.

So the shape of the cost curve is a step function, not a slope:

```
€7.05  ├────────────────────────────────────┐
       │   everything up to ~203M spans/day │
       │        costs exactly the same      └──── then €0.05/GB, gently
       └─────────────────────────────────────────────────────────────►
              40.8M measured ▲          203M ▲ breakeven
```

Two consequences, and they are the practical output of this whole document:

1. **Tuning TTLs down to save money is wasted work here.** You are inside the
   step. Shortening the 7-day span retention buys nothing and costs you the
   drill-in window that makes the dependency map useful.
2. **If the bill does grow, the sampling rate is what moved it.** Turning off
   tail sampling multiplies raw storage roughly fivefold for traces nobody
   reads. The 20% baseline policy is a cost decision as much as a storage one —
   and it is the *only* knob that changes the answer at this scale.

`docs/SCALING.md` covers the other side of the same coin: what breaks first is
CPU on the fan-out service, not disk and not memory.

---

## 5. What the €7.05 buys, and what it does not

**Included:** five instrumented services, an OpenTelemetry collector with tail
sampling, Jaeger v2, ClickHouse, PostgreSQL, Redis, the four platform workers,
the analytics API and the Next.js dashboard. Sixteen containers, a measured
3.12 GiB working set under live load.

**Not resident:** Prometheus and Grafana. They live in the `full` profile and
cost about 1.0 GB, which is most of the headroom between the 3.12 GiB working
set and the ~7 GB provisioning floor. Eight gigabytes against a seven-gigabyte
floor is one gigabyte of slack, and spending it on dashboards during a demo is
how the OOM killer takes ClickHouse.

If you want them always-on, the honest answer is a CX42 at about €14 — not
optimism about the floor.

**Not included and not modelled:** egress, backups, and a second host. This is a
single-node deployment and the cost model says so rather than quietly assuming
a resilience story it does not have. See `docs/LIMITATIONS.md`.

---

## 6. Against a vendor

The comparison worth making is not a price, because prices move and a number
quoted here will be stale before anyone reads it. It is the **billing shape**.

Hosted APM is priced per host and per gigabyte ingested. That has two effects
that this deployment does not have:

- **Retention is a purchase decision.** Keeping thirty days instead of seven is
  a line item, so the retention window gets set by procurement rather than by
  what an investigation needs.
- **Sampling becomes a cost control rather than a signal-quality decision.**
  When ingest is the billed unit, the pressure is always to send less — and the
  traces that get dropped are the ordinary ones, which are exactly the baseline
  the anomalous ones have to be compared against.

Here, sampling is chosen for signal quality (M14: keep 20% of ordinary traces
*because* every downstream percentile is computed over that sample, and a
sample biased toward errors and slow traces makes every percentile a lie) and
retention is chosen for how far back an investigation reaches. Neither is chosen
by the bill, because at this scale the bill does not notice.

**That is the actual argument for self-hosting this, and it is not "it is
cheaper".** It is that the knobs stay attached to the questions they should be
attached to.

At what scale does that flip? When storage crosses the step — around 5× this
traffic on the assumptions above — the arithmetic starts to matter and the
answer becomes a real comparison rather than a rounding error. The honest
sentence is that this document describes a €7 deployment and does not pretend to
have modelled a €7,000 one.

---

## 7. Watching it

Prometheus scrapes the cost meter alongside everything else:

| Series | |
|---|---|
| `tracemap_storage_on_disk_bytes{table}` | measured, per table |
| `tracemap_storage_bytes_per_row{table}` | measured; the model's foundation |
| `tracemap_storage_compression_ratio{table}` | measured |
| `tracemap_spans_per_day` | measured ingest rate |
| `tracemap_cost_monthly_eur` | projected |
| `tracemap_cost_per_million_spans_eur` | projected |
| `tracemap_cost_fixed_share` | how much of the bill is the box |

Two alerts, and deliberately **no alert on the euro figure**. It contains a
projection, projections drift, and paging someone because a forecast moved is
how a cost dashboard gets muted within a week.

- **`StorageGrowthOffProjection`** — bytes per span is 50% above its weekly
  average. This is the one number the whole model rests on. If it moves that
  far, either the per-column codecs stopped applying or a high-cardinality
  attribute started riding along on every span, and every retention decision
  made from the old figure is now wrong.
- **`DiskProjectedToFill`** — ClickHouse is using more than 70% of the host
  disk. Measured, not forecast, and it ends with the database refusing writes.

---

## 8. Re-deriving this

Prices are the most volatile facts in this repository. The constants are
env-overridable for that reason, and the discipline is the same as the version
pins: **re-check before quoting one out loud.**

```bash
COST_HOST_MONTHLY_EUR=14.00 \
COST_HOST_DISK_GB=160 \
docker compose up -d analytics-api
```

The TTL trap note in `clickhouse/schema/07_retention.sql` says never to raise a
retention without recomputing disk against the *measured* bytes-per-span. That
query is now one HTTP call away, which was most of the point of building this.
