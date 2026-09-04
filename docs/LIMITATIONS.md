# Limitations of critical-path attribution

Naming the limits of your own idea is a stronger signal than the idea. These are
the conditions under which TraceMap's flagship number is wrong, how each is
mitigated, and which ones remain open.

Volunteer these before being asked.

---

## 1. Sampling skews attribution

**The problem.** Blame shares are computed over whatever traces were kept. Tail
sampling deliberately keeps errors and slow traces, so a naive blame share is
measured over a sample that is biased *toward* the very traces where one edge
dominates. The number would overstate how often that edge is the bottleneck.

**Mitigation.** The tail sampler keeps a `baseline-probabilistic` policy at 20%
alongside the error and latency policies, specifically so an unbiased slice
exists (M14). Measured in Phase 2: 100 browse requests produced 100 spanmetrics
counts and 26 stored traces, which is that policy working.

**Still open.** The API does not yet compute blame shares *only* from the
unbiased slice, nor scale by adjusted counts. Until it does, quote per-edge
`critical_ms` (which is per-trace and unaffected) rather than
"share of total latency" (which is not).

---

## 2. Async work breaks the model

**The problem.** A fire-and-forget span — a queue publish, a background write —
can outlive the request that spawned it. It is not on the user's critical path,
so the algorithm attributes it 0ms. That is *correct for latency* and
*misleading for correctness*: the work matters, it just does not make the user
wait.

**Mitigation — implemented.** `split_async()` in the assembler removes
`span.kind = PRODUCER` spans *and their subtrees* from the critical-path
computation. The subtree goes too, because that subtree **is** the async work.

Why exclude something the walk would score 0.0 anyway: a deliberate zero and an
accidental zero look identical in the database, and only one of them is a
statement about the system. If the walk ever changes such that a long async span
*would* land on the path, this keeps the answer correct rather than lucky.

The edge is still written with `critical_ms = 0.0` (M5) — the call really
happened, and the map should fade it, not delete it.

**Still open.** The demo application has no async fan-out, so this runs only
against the unit test `test_async_producer_work_is_excluded_explicitly_not_silently_zeroed`,
not against live traffic. Tested, not yet exercised in production.

---

## 3. Missing spans distort the path

**The problem.** A dropped intermediate span makes a grandchild look like a
direct child, and the walk attributes time to the wrong hop. A dropped root
leaves the trace with no obvious starting point.

**Mitigation.** Root selection is orphan-tolerant: the earliest orphan is used
when the true root is absent, because a partial answer beats a blank map. This
is covered by `test_orphan_root_survives_a_dropped_parent`.

**Still open.** Deeper gaps need trace-completeness scoring — comparing observed
span count against an expected shape per route — so that a badly incomplete
trace is *excluded* rather than silently mis-attributed. Not built.

---

## 4. Clock skew across nodes

**The problem.** The maths assumes comparable timestamps. Spans from different
hosts carry different clocks, and a few milliseconds of skew is normal. At short
durations that noise is a meaningful fraction of the measurement.

**Mitigation — implemented.** `SKEW_FLOOR_MS = 2.0` in the assembler floors any
contribution below the threshold to a clean `0.0`. Reporting "0.4 ms of blame"
claims a precision the timestamps do not have. Segments also remain contiguous
and sum exactly to the root duration
(`test_critical_path_covers_the_root_span_exactly`), so gross skew shows up as a
broken invariant rather than a plausible wrong number.

**Still open.** Skew itself is not yet *measured* — there is no panel showing
drift between hosts, so the floor is a mitigation without an instrument. On a
single-node deployment there is no cross-host skew to see, which is precisely
why this must stay written down before the stack runs on more than one machine.

---

## 5. Malformed nesting — found in production, not theory

**The problem.** The walk assumes a child span lies inside its parent's window.
Real instrumentation violates this. Express closes its `request handler` span a
tick *after* the HTTP server span that contains it:

```
order-svc POST (SERVER)                [ 5.0 .. 122.6]
  order-svc request handler /orders    [ 6.0 .. 122.8]   <-- 0.2 ms late
```

`_walk` skips any child ending after the cursor, because that is precisely how
it recognises parallel work. So a child overrunning its parent by a fraction of
a millisecond takes its **entire subtree** with it. Measured on the real demo
trace before the fix:

| edge | wall clock | attributed |
|---|---|---|
| order-svc → payment-svc | 98.8 ms | **0.0 ms** |
| order-svc → product-svc | 46.8 ms | 0.0 ms |
| order-svc → user-svc | 34.3 ms | 0.0 ms |

Every downstream edge scored zero. Nothing errored. The flagship number just
quietly became meaningless on the one trace the project exists to explain.

**Mitigation.** `normalize_spans()` in the assembler clamps every span inside
its parent's window before the walk runs. This is normalisation of malformed
input, **not** a change to the algorithm — `critical_path.py` stays verbatim and
its six tests stay green. A span that outlives its own parent is not a fact
about the system; it is an artifact of two instrumentation layers rounding
differently. After the fix, the same trace attributes 98.8 ms to payment and
0.0 ms to the parallel siblings.

Guarded by `test_child_outliving_its_parent_does_not_erase_the_subtree`, and by
a companion test asserting the clamp is a no-op on well-formed traces — so it
cannot start quietly rewriting good data.

**Still open.** Nothing. This one is fixed and tested.

---

## 6. The eval score measures this detector on this application

Added in Phase 7, when the harness that produces the accuracy numbers started
existing. It is a limitation of the *evidence*, not of the algorithm, and it is
the one most likely to be over-claimed.

**The problem.** F1, precision and MTTD come from twelve hand-written cases
against one synthetic five-service topology, driven by one k6 script, on one
host. That is a held-out set in the sense that the detector was not tuned on
individual cases — but it is not a sample of anything. It generalises to this
application and no further.

Three specific ways the number is narrower than it sounds:

- **The topology has one shape.** Every case is a synchronous fan-out from
  order-svc. Nothing measures the detector on a queue, a batch job, or a
  service mesh with retries.
- **The traffic has one mode.** A mild diurnal ramp and a 2x step. No weekly
  seasonality, no multi-tenant mix, no traffic that is legitimately bimodal
  for business reasons.
- **The null case needs a quiet host.** Measured directly: a run on a laptop
  that was concurrently building images produced a **+1020ms** excursion on
  `gateway-svc->order-svc` and a false positive with nothing injected. The
  detector was right — the checkout really was slow. On a contended host the
  null case measures contention, and the result looks exactly like a precision
  regression, which invites tuning the detector to fix a problem that is not
  in it.

**Mitigation.** Every reported score names its tier and its environment. The
nightly run uses a dedicated CI runner rather than a shared one. The case files
carry their own invariant tests, so a case that has quietly stopped measuring
what it claims fails before it produces a number.

**Still open.** A second application. Until one exists, twelve cases against
one topology is a regression suite that behaves like a held-out set, and the
honest framing is that rather than "my detector has an F1 of 0.9".

---

## 7. A parent edge absorbs its children, and nothing suppresses the echo

The attribution is correct here and the *presentation* is not, which makes this
the subtlest entry in the document.

`gateway-svc→order-svc` is credited with `order-svc`'s entire subtree — 735.0 ms
in the specification test, because the gateway really did wait 735 ms on order.
That is the right answer to "how much did this dependency cost the user". It also
means **any** downstream degradation raises the parent too, and in steady state
that edge owns roughly 52% of the critical path on this topology.

So when `payment-svc` degrades, two edges legitimately move: the one that broke
and the one containing it. Both are true. Only one is actionable.

**Measured, not theorised.** In the first full twelve-case eval run,
`gateway-svc→order-svc` was the blamed edge in three of five failures. Two of
those — `user_cpu_saturation` and `payment_db_down` — were scored `wrong_edge`
because the parent surfaced *instead of* the child that explains it. A single
alert taken as "the answer" points at the gateway rather than the culprit.

**Why it is not simply a bug.** Suppressing parents unconditionally would be
wrong: `gateway_latency_cascade` is a case where the gateway edge genuinely *is*
the finding, and the correct answer is the parent. The distinction is whether the
parent's increase is *fully explained* by a child's in the same window — which is
a real comparison to implement, not a flag to set.

**Open.** The fix is parent suppression conditioned on that comparison, and
choosing its threshold without an eval run to justify it is M8. The case that
measures it exists and now runs; the number comes first.

Related but distinct: **F38** covers a parent edge that is slow for a legitimate
reason (offered load rising), which is a different question with a different
answer — normalising against throughput rather than against a child.

---

## What this means for the claim

The defensible statement is narrow:

> *For synchronous request-response traces, on a single host, with the sampling
> policies documented in `otel/collector-gateway-config.yaml`, an edge's
> `critical_ms` is the number of user-visible milliseconds that edge
> contributed.*

Everything outside that sentence — async work, multi-host skew, heavily sampled
blame shares, badly incomplete traces — is either mitigated above or listed as
open. The number is not universally true, and saying so is what makes the cases
where it *is* true worth believing.

### Status

| # | Caveat | State |
|---|---|---|
| 1 | Sampling skews attribution | Mitigated; unbiased-slice maths not built |
| 2 | Async work | **Implemented** and unit-tested; not exercised live |
| 3 | Missing spans | Orphan-tolerant root; completeness scoring not built |
| 4 | Clock skew | **Implemented** (2 ms floor); skew not yet measured |
| 5 | Malformed nesting | **Fixed and tested**, both directions |
| 6 | Eval scope | Measured; one topology, one traffic mode, quiet-host requirement documented |
| 7 | Parent edges echo their children | **Open.** Measured: 3 of 5 failures in the first full eval run. Attribution correct, presentation not |
