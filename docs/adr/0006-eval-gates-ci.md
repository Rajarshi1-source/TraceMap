# ADR 0006 — The detector is a model, so it gets an eval set and a merge gate

**Status.** Accepted, Phase 7.
**Supersedes.** Nothing. **Related.** [0003](0003-assembler-over-mv-selfjoin.md)
(why edges are stitched in a worker), [0004](0004-critical-path-attribution.md)
(the metric the detector reads), [0005](0005-ewma-over-static-threshold.md)
(why the detector is an EWMA).

---

## Context

ADR 0005 chose an EWMA over a static threshold and over a machine-learning
model. The argument ended with a promise:

> *"If a model beat EWMA on the eval set, I'd switch, and I'd have the number
> to prove it."*

That sentence is only worth saying if the eval set exists. Without it, every
claim about the detector — that it catches a 60ms regression, that it stays
quiet on healthy traffic, that it does not blame a parallel sibling — is an
assertion about code someone wrote and nobody measured.

Observability tools all claim they find problems. Almost none publish a
scoreboard of their own accuracy. That gap is the opportunity, and closing it
is the whole of this phase.

## Decision

**Treat the detector as a model and give it the apparatus a model gets.**

| MLOps concept | What it is here |
|---|---|
| Model artifact | `DetectorConfig` — alpha, threshold, window, floors — versioned in git and recorded per run in `eval_runs.detector_cfg` |
| Training data | EWMA baselines in Redis, rebuildable from ClickHouse in one query |
| Held-out eval set | Twelve golden cases, including two null cases and one decoy |
| Offline metrics | precision, recall, F1, MTTD p50/p95 |
| Capability gaps | Found by the set, not by review — see the error path below |
| Regression gate | CI fails a merge on F1 or precision dropping past tolerance |
| Drift monitoring | Nightly run on `main`; `EvalGateRegressed` and `EvalNotRunning` alerts |
| Experiment tracking | `eval_runs` keyed by git SHA plus the config JSON |
| Rollback | Revert the config commit; the eval score is the evidence |

Concretely: `eval/cases/*.yaml` describe twelve fault scenarios,
`eval/injectors/` apply them as runtime flags, `eval/runner.py` runs them
sequentially against a live stack, `eval/scorer.py` scores the run, and
`eval/check_regression.py` compares it to `eval/baselines/main.json` and fails
the merge if it regressed.

---

## The consequential choices

### 1. Cases run sequentially. Always.

Two concurrent faults make attribution ambiguous — there is no correct answer
to score the detector against, so the run is not slow, it is *void*.

Case 006 applies two faults at once and is not an exception: it is one case
with one expected answer, and which of two simultaneously slow services gets
blamed is the question it exists to ask.

The cost is real. The full set takes roughly 110 minutes.

### 2. A 60-second settling gap, with alert suppression cleared inside it

Without a gap, case N's fault is case N+1's baseline and the second case in
every family fails. The obvious conclusion from that is "the detector
regressed", which is the most expensive wrong conclusion available.

Clearing Redis `anomaly:dedup:*` and `anomaly:hist:*` between cases needs
justifying, because it looks like reaching into the thing under test.

The dedup key exists so that N detector replicas produce one Slack message per
incident, and so a firing edge does not re-page every minute for ten minutes.
Cases 003 and 004 both fault `order-svc->payment-svc` about seven minutes
apart — well inside the 600s dedup TTL — so leaving the key in place would make
case 004 a guaranteed false negative. The scoreboard would be measuring Redis.

Two eval cases are two separate incidents that happen to share an edge.
Clearing suppression between independent experiments is harness hygiene.
**Baselines are deliberately not cleared**: they are warm, they are legitimate,
and M6 guarantees a fault case cannot have corrupted them.

### 3. Budgets come from the measured pipeline, not from ambition

Phase 6 measured **375 seconds** from injection to alert. The build plan said
~180s and `eval-harness.md` said ~245s worst case. Both omitted two terms that
turn out to dominate: assembler lag and minute-bucket completion.

```
  collector batch                          5s
+ tail sampling decision_wait             10s
+ collector export batch                   5s
+ assembler late-span grace               30s
+ assembler interval                      15s
+ minute-bucket completion                60s   <- omitted originally
+ detector confirmation (3-in-5 x 60s)   300s
= 425s worst case
```

So every fault case gets `detect_within_s: 480`, and `eval/topology.py`
carries the derivation with a test (`test_budget_clears_the_pipeline`) that
fails if anyone tightens a budget below the floor.

**Exit criterion 4's MTTD ceiling of 216s is amended to 432s**, and this is
the amendment that matters most in this document. 216s is 180 × 1.2, and 180s
is *arithmetically unreachable*: the detector's own confirmation window is
three cycles at sixty seconds, so 180s is the floor **before any pipeline time
is counted at all**. The F1 ≥ 0.82 and precision ≥ 0.87 criteria are unchanged
and were met as written.

A ceiling nothing can clear is worse than no ceiling, because a red gate on a
working detector trains people to weaken the detector. That is the exact
failure mode this ADR exists to prevent, and it would have been introduced by
the gate itself.

### 4. The eval profile does not speed the pipeline up

Every term above except the last two is an environment variable. Turning them
down would buy roughly forty seconds.

`docker-compose.eval.yml` does the opposite: it **pins** the detector's
configuration to the documented values, so a stray `.env` on a laptop cannot
silently change what is being measured between runs.

The reason is that the only claim worth making about an MTTD number is that it
was measured against the configuration that ships. A detector scored on a
faster pipeline than production has been scored on a system nobody runs.

### 5. Two tiers, and every quoted score names its tier

| Tier | Cases | Time | Runs on |
|---|---|---|---|
| smoke | 001, 003, 006, 007 | ~45 min | PRs touching detector paths |
| full | all twelve | ~110 min | nightly on `main` |

The four smoke cases cover each failure mode the gate exists to catch, not
simply the fastest four: false positives (001), latency sensitivity at the
threshold (003), the flagship decoy (006), and the error path that latency
percentiles cannot see (007).

`check_regression.py` **refuses** a cross-tier comparison outright. Comparing
smoke against full is not a lenient check, it is a meaningless one, and it
would silently pass a real regression whenever the tiers happened to differ in
the convenient direction.

### 6. Cascade propagation upward is correct, so ancestors are never traps

An edge's critical-path time is its callee's entire subtree. A payment
slowdown therefore genuinely does make `gateway-svc->order-svc` slower, and
reporting that is right.

Listing an ancestor as a trap would penalise the detector for being correct,
and the fix for a red case is to make the detector wrong. Only **siblings**
and **descendants** are traps — and case 012 exists specifically to check the
descendant direction, that an origin fault does not smear blame across the
whole subtree.

`test_traps_are_never_ancestors` enforces this on the case files.

### 7. The gate is proven to fail by tests, not by a screenshot

The build plan asks for a deliberately broken detector in a draft PR and a
screenshot of the red check. That demonstration was done and is recorded
below — but a screenshot is not a test. It rots, it cannot run in CI, and it
proves the gate failed on one afternoon rather than that it fails today.

`eval/tests/test_gate_fails.py` constructs the results each specific breakage
would produce and requires the gate to go red:

| Broken how | Signature | Caught by |
|---|---|---|
| Reads wall clock, not critical path | false positives on decoy cases | precision floor |
| Threshold raised until silent | perfect precision, no recall | F1 floor |
| M6 violated, baseline chases the fault | clean sweep of false negatives on sustained cases | F1 floor |
| Correct but far too late | perfect F1 | MTTD ceiling |
| Blames the wrong service | `wrong_edge` | precision floor |
| Null case quietly dropped | shorter case list, better numbers | per-case diff names it MISSING |
| Run artifact not produced | nothing to compare | exit 2 on missing run |

The last two exist because the cheapest way to make a red gate green is to
stop measuring.

---

## What this cost, and what it caught

Building the harness surfaced fifteen defects: **eleven in the harness, one
in the detector, two in the environment it needs, and one still open**. That is the point rather than an embarrassment:
an eval suite that has never failed is evidence of an untested harness, and
four of these were found by the harness scoring a correct detector as broken.

Two of the twelve are in the detector. One of those — the missing error path —
is the finding that justifies the whole phase, because no amount of code review
was going to surface it.

**The reference case 009 named an edge that does not exist.**
`gateway-svc->user-svc` appears in `eval-harness.md`. The concurrent fan-out
lives in `order-svc`, so user-svc is a child of order-svc and gateway-svc has
never called it. The case would have injected a real fault, watched for an
alert on an impossible edge, and scored a working detector as a false
negative. Corrected to `order-svc->user-svc`; `eval/topology.py` now declares
the graph once and `test_edges_exist` checks every case against it.

**`type: null` was not the null injector.** YAML 1.1 parses bare `null` as
None, so both null cases had no injector type at all — while still printing a
passing row. Now quoted, and `test_case_schema` checks the type resolves.

**The reference percentile indexes off the end of the list.**
`lat[int(len(lat) * 0.95)]` raises `IndexError` whenever `len * 0.95` is a
whole number — n = 20 and n = 40 among them. A crash in the scorer reads as a
broken harness at exactly the moment the number matters. Replaced with a
nearest-rank implementation using `ceil`, not `round`: Python's `round` is
banker's rounding, so a rank landing on .5 alternates answers depending on the
parity of the case count.

**The detector had no error path at all.** `EdgeObservation` has carried an
`error_rate` field since Phase 6 and nothing ever read it, so the detector
could not see an edge failing half its requests. Latency cannot substitute: an
injected 5xx returns *faster* than a success, because the handler raises before
the database write. Cases 007 and 008 were structurally unpassable, and the
detector was behaving correctly on the metric it had.

This is the finding that justifies the phase. A tuning gap would have been
found by reading the code; a missing capability was found by asking the system
to do something and watching it fail. `evaluate_error_rate()` closes it, as a
separate method with its own history and dedup keys — a shared dedup key would
let a firing latency alert silence a genuine error alert on the same edge for
ten minutes.

Errors get a **fixed threshold, not an EWMA baseline**, and the asymmetry with
latency is deliberate. Normal latency is environment-dependent, so a learning
baseline is right. Normal error rate is not: zero is the target, and an EWMA
would let a service that has failed 5% of requests all week read as healthy.
M8 is satisfied rather than bypassed — the change is justified by two cases
that could not pass and now can, not by an argument.

**The reference decoy service is not a parallel sibling in this application.**
This is the most instructive one. The decoy case requires a leg that is
visibly slow and carries no blame; the reference uses product-svc. Measured
over ~11,000 healthy traces, `order-svc->product-svc` is on the critical path
for **12.4%** of traces at a critical p99 of 49ms, because product has an N+1
query path that fires on large carts. Injecting 120ms into it moved its
critical p99 to 169ms and tripped the trap — the detector was correct and the
case scored it a false positive.

A service that already carries critical-path blame cannot be a decoy at any
delay: the injected time lands on the critical path in full. `order-svc->user-svc`
is critical on 0.3% of traces at a critical p99 of exactly 0.0, and is the real
sibling. The first design compared *mean* latencies, where 45ms against 90ms
looked comfortable; the critical path is a question about the **tail**, and
product's mean says sibling while its p99 says contender. The measured table now
lives in `eval/topology.py` and the decoy test asserts against it.

**Redis was unpublished, so alert suppression was never reset between cases.**
The runner executes outside the stack and Redis has no host port in the `core`
profile, so every reset failed silently and the run continued. Case 003 alerted
on `order-svc->payment-svc` and set a 600s dedup key; case 006 needed the same
edge and got it at t+444s instead of ~t+200s, one second after the TTL expired.
Preflight now **refuses** rather than warns: a run that cannot reset suppression
still prints a scoreboard, and that scoreboard measures Redis while reading as a
detector regression.

**The reference runner gave traps a shorter window than the expected edge.**
It stopped as soon as it had *both* a detection and a trip, so a clean case ran
its full budget while a case whose trap fired late never saw it — which
flatters precision. Now a detection starts a 90-second trap-grace window, so
both get a comparable look.

**user-svc had no `GET /_internal/fault`.** Every other service does. The
all-clear sweep that enforces M29 asks each control plane "are you clean?" and
a 405 is not an answer — it was being read as clean. Both halves fixed: the
handler exists, and the sweep now treats any non-200 as *unaudited* rather than
*clear*.

### The decoy case is red, and stays red

The flagship case does not pass, and the reason is the most interesting thing
the phase produced.

`order-svc->user-svc` has a measured baseline critical p99 of exactly **0.0** —
it is the last leg to finish on 0.3% of traces. Slow it by 100ms while payment
runs 150ms slower, and its critical p99 becomes **135.4ms** and it alerts. Not
span loss: all 288 traces where it carried blame have complete payment and
product spans. Not queueing: it starts 1.3ms *before* payment at the median.

A p99 reports the top 1% of traces, so an edge is non-critical at p99 only if
it fails to finish last on more than 99% of them. Fan-out finish order has real
variance, so that fraction is never zero — and adding delay widens the
distribution and pushes it up. user-svc reached 0.6%, right at the boundary,
and its critical p99 jumped discontinuously from 0 to roughly its own duration.

That is a bind rather than a bug. The decoy's delay must breach the 50ms
wall-clock threshold or a wall-clock detector passes the case and it proves
nothing; any delay that large also pushes the sibling toward the 1% boundary.

**The detector is not wrong** — on those traces user-svc really was the critical
path. The case's assertion is what is ill-posed. The right question is about
*blame share*, which already exists on every edge, rather than about a
percentile crossing a millisecond threshold: an edge owning 0.6% of the
critical path is not the cause of anything whatever its p99 says.

It is left red deliberately. The case was already rebuilt once on a
measurement; rebuilding it a third time until it agreed would be tuning the
measurement to the answer, which is the failure this document spends most of
its length arguing against. The smoke tier therefore scores **precision 0.750
against a 0.870 floor** and the gate is red, which is the honest state to
commit at the end of a phase about not fudging exactly this.

### An incomplete run is not a measurement

The clean re-run died on a single `httpx.ReadTimeout` while polling, taking
forty-five minutes of warm baselines and injected faults with it.

The naive fix is worse than the crash. Swallow poll errors and an unreachable
API returns no anomalies, so every remaining case scores as a false negative
and the run produces a plausible scoreboard describing a detector that was
never asked anything. The loop now counts consecutive failures — one timeout is
a busy host, six in a row is an API that is gone — and abandons rather than
inventing false negatives.

Three consequences follow from the same principle, and it is the principle this
whole ADR keeps arriving at: **a wrong number is worse than a missing one.**
Completed cases are kept and scored, because losing finished work to a later
exception makes a harness flaky and flaky harnesses stop being run. The
scoreboard prints `INCOMPLETE: n/m cases` under the numbers rather than only in
a log line above them. And the run is not written to `eval_runs` and exits
non-zero whatever the partial scores say — that table records measurements, not
attempts, and a partial run sitting in it would eventually become a baseline.

### Two portability defects that would never appear in CI

**psycopg cannot run async on the Windows default event loop.** The first run
finished, printed a scoreboard, and recorded nothing — reporting `postgres
unreachable`, which sends you to check the database. Postgres was fine; the
Proactor event loop is not one psycopg's async mode can use, so the pool never
opened and every `eval_runs` row was lost. Fixed with
`asyncio.run(..., loop_factory=asyncio.SelectorEventLoop)` on win32, and — the
more useful half — a message that names the event loop instead of the network.

**A native Postgres on the host shadowed the container.** With the loop fixed,
the connection then failed authentication against a server whose credentials
were correct: a local `postgresql-x64-17` service owns 5432, compose published
the container's 5432 anyway and reported success, and the harness authenticated
against the wrong database. Worse than a port conflict, which at least fails
loudly at boot. The eval overlay now publishes on 55432 and 56379.

Both are "the harness runs on whatever machine is to hand" problems and neither
would ever appear on a Linux CI runner. A harness that only works on the
maintainer's laptop is one nobody else can reproduce a score with, and
reproducibility is the entire claim.

### The gate proven red — twice, one of them for real

Exit criterion 5 asks for a deliberate break and a screenshot. Both were done,
and the second is worth more than the plan expected.

**By hand.** Hoisting `_update_ewma` out of the healthy branch in
`anomaly_detector.py` — the edit M6's comment warns against, which reads as
harmless deduplication — makes the baseline chase the fault and the detector go
silent on a service that is still broken.

**For real, unplanned.** The first live smoke run went red on all three gates:

```
  cases                 4   tp 1  fp 2  fn 1
  precision         0.333   (floor 0.870)
  f1                0.400   (floor 0.820)
  mttd p95           444s   (ceiling 432s)
  FAIL
    null_baseline         false_positive  blamed gateway-svc->order-svc
    parallel_decoy        false_positive  blamed order-svc->product-svc
    payment_errors_10pct  false_negative  no alert within 480s
```

Every one of those three has a named cause above — host contention on the null
case, the mis-built decoy, the missing error path — and the delayed MTTD was
the un-reset dedup key. That is a better demonstration than a synthetic break,
because nobody arranged it.

---

## The measured result

Smoke tier, tier named as the D5 rule requires, on the commit this ADR lands with:

```
  cases                 4   tp 3  fp 1  fn 0
  precision         0.750   (floor 0.870)   <- the decoy, F37
  recall            1.000
  f1                0.857   (floor 0.820)
  mttd p50           242s   p95  273s       (ceiling 432s)
  FAIL
```

Null, latency and error cases pass. The error case passing is worth a line of
its own: it could not have passed a week ago, because the detector had no error
path until this phase's harness proved it needed one.

**No baseline is committed from this run**, on either tier. Two independent
reasons, and either alone would be sufficient. The run is red, and a baseline
recorded from a red run makes a known-bad number the standard. And the host
stalled 471 seconds mid-run (F36), so every `detected_at_s` is inflated by
however much wall clock the process lost — which would make the MTTD ceiling
*more lenient* than intended, the one direction a gate must never drift by
accident. A too-tight baseline goes red and gets investigated; a too-loose one
passes real regressions in silence.

`eval/baselines/README.md` documents the bootstrap. The first green run on a
dedicated runner records the real numbers.

## Consequences

**Good.** Every claim about detection accuracy has a number and a method
behind it. Tuning alpha is now an experiment rather than an argument (M8).
Regressions are caught at merge rather than during a demo. The null and decoy
cases make two specific claims falsifiable: that the detector stays quiet on
healthy traffic, and that critical-path attribution earns its complexity.

**Bad.** The full set takes 110 minutes, so it cannot gate every PR — hence
the smoke tier, and hence the standing requirement to name the tier behind any
quoted score. The set is twelve cases against one synthetic topology; it
measures this detector on this application and generalises no further than
that, which is worth saying before someone else says it.

**Ugly.** The harness needs a live stack, so it cannot run in a unit-test
sandbox. `eval/tests/` covers the scorer, the gate and the case files without
one, which is what makes CI able to check the gate on every commit while the
measurement itself runs nightly.

## What would change this decision

A second application, or a detector whose decisions depend on something the
golden set does not vary — seasonality, multi-tenancy, or traffic shapes with
more than one mode. At that point twelve hand-written cases stop being a
held-out set and start being a regression suite, and the honest move is to say
so rather than to keep quoting F1.
