# ADR 0008 — Error budgets are denominated in critical-path milliseconds

- **Status:** Accepted
- **Date:** 2026-09-04 (Phase 8, stretch item D6)
- **Deciders:** Rajarshi Basu

## Context

Phase 7 shipped a checkout SLO with a multi-window multi-burn-rate policy: 14.4×
over a 1-hour window AND a 5-minute window, paging at two minutes. That watches
the product. It says *checkout is breaking its promise* and nothing about which
of the four dependency edges is breaking it.

The obvious next step is an SLO per edge. Every APM product offers one, and all
of them measure the edge by its **wall-clock duration**.

In a parallel fan-out that measurement is wrong twice over, and this application
was built to make that concrete. `order-svc` calls `user-svc`, `product-svc` and
`payment-svc` concurrently. `product-svc` runs about 700 ms; `payment-svc` about
720 ms. The user waits 720 ms.

- **False alarms.** A wall-clock SLO on `order-svc→product-svc` with any
  objective under 700 ms is in permanent violation. It pages someone to go
  optimise a service whose entire 700 ms is free — making it infinitely fast
  would save the user nothing. After the third such page nobody reads the alert,
  and the next one that matters is ignored with it.
- **Understated impact.** `payment-svc` is on the critical path on most requests
  but not all. Its wall-clock number averages the requests where it mattered
  together with the requests where it did not, so its budget burn understates
  what users actually experienced.

The system already computes the right number. `trace_edges.critical_ms` is the
milliseconds each edge contributed to user-visible latency, written on every
edge including `0.0` for parallel siblings (M5). It was being used to rank the
dependency map and to drive the detector, and not to state a promise.

## Decision

**Edge-level objectives are stated against `critical_ms`, never wall clock.**

An edge can be slow and satisfy its SLO. An edge can be fast and violate it.
That inversion is the point, not a side effect.

Three supporting choices:

**A fixed latency histogram, not a TDigest.** An error budget is denominated in
good events over total events. `service_edges_1m` already stores TDigest
*states*, and a TDigest gives percentiles — `p99 <= 100ms` is a threshold on a
summary statistic, not a budget anyone can burn down. So `edge_slo_1m` stores
cumulative `countIf(critical_ms <= bound)` at six fixed bounds (25/50/100/250/
500/1000 ms), the same shape Prometheus histograms use and for the same reason:
any objective landing on a bound can be evaluated after the fact, per edge,
without redefining the view and without a backfill.

**A new table, not a widened one.** `edge_slo_1m` reads `trace_edges` — one
table, no join (M3) — and lives beside `service_edges_1m` rather than adding
seven columns to it. The flagship roll-up is left untouched, and the SLO
machinery is independently droppable.

**The wall-clock counterfactual is exported and never alerted on.** The table
carries one wall-clock bound at 250 ms purely so `/api/slo` and
`tracemap_edge_wallclock_bad_ratio` can show, on the same edge over the same
window, the budget a wall-clock objective *would* have burnt. It is evidence for
the design. The moment someone writes an alert against that series, this whole
decision has been undone.

Every counter is `SimpleAggregateFunction(sum, UInt64)`. This is the first table
written after Postmortem 001, and the first where F1's fix was applied from the
start rather than retrofitted — a plain `UInt64` in an `AggregatingMergeTree` is
not summed on merge, it keeps an arbitrary row's value, and the resulting SLI
would be silently wrong in the direction of looking healthy.

The burn-rate policy is expressed in `monitoring/prometheus-rules.yml` beside the
checkout SLO it was modelled on. One policy, applied at two altitudes.

## Alternatives considered

**Reuse `service_edges_1m` with a quantile threshold.** Zero new storage, and it
is what most teams do. Rejected because it produces an alert, not a budget:
there is nothing to burn down, no "2% consumed in an hour", and no way to say
whether a month has been spent. It would also have meant calling a latency
threshold an SLO, which is the specific vocabulary abuse this project should not
be committing.

**A per-edge objectives table joined into the materialised view.** Would allow
arbitrary objectives instead of six fixed bounds. Rejected outright: an MV that
joins sees a single insert block and silently loses rows. That is M2/M3 and the
entire lesson of ADR 0003.

**Objectives in YAML.** Rejected for four entries. A frozen dataclass is
diffable, type-checked and reviewable, and validates its own bound at import
rather than at query time — an objective naming a bucket that was never
materialised fails at startup rather than silently never firing. If the
catalogue outgrows a page, move it because it grew.

**Error-rate SLOs per edge instead of latency.** Cheaper — `error_count` was
already there. Not rejected so much as insufficient: the checkout SLO already
covers errors, and errors are the part every tool gets right. Latency
attribution is the part that is wrong everywhere else.

## Consequences

**Good.** The project's central claim is now expressed in the vocabulary
operations teams actually use to make decisions. `product-svc` sits near zero
burn while running 700 ms, and the number it *would* have burnt is visible next
to it — an argument that can be graphed rather than explained. Case 006 doubles
as the proof, and `test_edge_slo.py` pins the inversion so a refactor cannot
quietly turn this back into a wall-clock alert.

**Bad.** Objectives must land on one of six bounds. An objective of 137 ms is
not expressible without a schema change and a period where history has no
counter for it. This is a real constraint and it is enforced loudly rather than
rounded silently.

`edge_slo_1m` is created by `08_edge_slo.sql`, which `docker-entrypoint-initdb.d`
applies only on a **fresh** ClickHouse volume. An existing deployment needs the
file applied by hand. `/api/slo` degrades to a note naming the file rather than
returning a 500 — an SLO page that errors reads as a broken platform, one that
says the roll-up is absent points at the fix.

**Neutral.** Storage is negligible: bounded by distinct edges × minutes, which
the topology bounds, not by traffic. `docs/COST.md` covers why that class of
table is effectively free here.
