# ADR 0004 — Attribute latency by critical path, not wall clock

- **Status:** Accepted
- **Date:** 2026-09-03 (Phase 4)
- **Deciders:** Rajarshi Basu

## Context

Every dependency map — Grafana Tempo's `servicegraph`, SigNoz, Coroot, Kiali —
annotates each edge with wall-clock latency: p50, p95, p99 of how long that call
took. That is a solved problem, shipped free in four tools.

It also answers the wrong question when calls run concurrently.

In the checkout fan-out, `order-svc` calls three services at once. Measured on
the running system:

| edge | wall clock |
|---|---|
| order-svc → payment-svc | 98.8 ms |
| order-svc → product-svc | 46.8 ms |
| order-svc → user-svc | 34.3 ms |

A conventional map paints payment and product as the two worst edges and sends
you to look at both. But making `product-svc` **infinitely fast** would save the
user **zero milliseconds**: payment finishes last and defines the response time.
Optimising product is wasted work that a wall-clock map actively recommends.

## Decision

**Compute the critical path through every trace and attribute user-visible
milliseconds per edge.**

Walk the span tree backwards from the root's end time. At each level take the
child that finished last among those ending before the current cursor; the gap
between that child's end and the cursor is the parent's own work. Recurse, move
the cursor to the child's start, and skip any sibling ending after the new
cursor — that sibling ran in parallel and is not on the path.

Same fan-out, attributed:

| edge | wall clock | critical path | blame |
|---|---|---|---|
| order-svc → payment-svc | 98.8 ms | **98.8 ms** | 100% |
| order-svc → product-svc | 46.8 ms | **0.0 ms** | 0% |
| order-svc → user-svc | 34.3 ms | **0.0 ms** | 0% |

One number, and the ambiguity is gone.

The assembler emits `trace_edges` and `trace_critical_path` **in a single pass**,
because stitching edges and extracting the critical path need the same grouping —
all spans of one trace. The flagship feature therefore costs almost nothing
extra: it rides along on work the pipeline must do anyway to avoid the
materialized-view trap in ADR 0003.

## Three decisions inside the decision

**Zero-blame edges are written, never omitted (M5).** A parallel sibling has
real traffic, a real error rate and real wall-clock latency. What it lacks is
blame. Writing the `0.0` row is what lets the map *fade* that edge rather than
delete it — and deleting it would be a lie, because the call genuinely happened.
Guarded by `test_zero_blame_edges_are_written_not_omitted`.

**A parent edge absorbs its whole subtree.** `gateway → order` is 118.2 ms, not
order's own 15 ms of work. The gateway really did wait 118 ms on order. Blame is
about what the caller experienced, not about who burned CPU.

**The algorithm is copied verbatim and is not "tidied".** Three constructs look
redundant and are each load-bearing for a test: the
`kid.end_ns <= span.start_ns` guard, the `max(kid.start_ns, span.start_ns)`
cursor move, and the final `segments.reverse()`. Simplifying any of them yields
code that still runs, still returns plausible numbers, and is wrong. The file
says so at the top.

## What Phase 4 actually cost, in defects

Building it surfaced four bugs that the design did not anticipate. All were
silent — no exception, no error log, just wrong numbers:

1. **The same trace was assembled by consecutive cycles.** 24 of 40 distinct
   edges were written twice. Duplicate rows inflate `request_count` in the
   incremental MV, which would have quietly undone the F1 fix Phase 3 exists to
   guarantee. Fixed with a completion-time watermark. A `ReplacingMergeTree`
   would *not* have helped: its dedup happens at merge time, long after the MV
   has fired on both inserts.

2. **Grace was measured on span time, not ingestion time.** A span is already
   ~20 s old when it lands (collector batch + `decision_wait` + export), so a
   30 s grace on span time declares a trace complete while a later batch is
   still in flight. Fixed by adding `ingested_at DEFAULT now()` and asking the
   question that actually matters: *has any new span for this trace arrived
   recently?*

3. **A child span outliving its parent erased its entire subtree.** See
   `docs/LIMITATIONS.md` §5. This is the one that mattered: on the real demo
   trace, payment, product and user all scored 0.0 because Express closes its
   handler span 0.2 ms after the HTTP span. Fixed by normalising input, not by
   touching the algorithm.

4. **`formatDateTime('%M')` returns the month name, not minutes.** Produced
   `19:September:24`. Fixed with `toString()`.

Defects 1–3 share the shape this project keeps running into: *silent, correct-looking,
wrong.* None would have been caught by a passing test suite over synthetic data —
all three needed the thing actually running against real traces.

## Consequences

**Good.** The map becomes actionable rather than merely informative. The claim
is narrow and defensible against a principal engineer: *"every service map tells
you which edge is slow; mine tells you which edge is slow and on the critical
path."* It is verified end to end, and the reconciler independently agrees with
the assembler at **0% divergence**.

**Bad.** A stateful worker to supervise, a watermark that resets on restart
(bounded reprocessing of up to `ASSEMBLE_LOOKBACK_MINUTES`), and a normalisation
step that must stay a no-op on well-formed traces — asserted by its own test.

**The honest limits** are in `docs/LIMITATIONS.md`, and two of the five remain
open. Naming them is what makes the cases where the number *is* right worth
believing.
