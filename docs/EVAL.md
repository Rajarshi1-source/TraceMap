# Evaluating the detector

The detector is a model, so it is evaluated like one: a held-out set of labelled
cases, a scoreboard, a committed baseline, and a merge gate that has been proven
to go red.

This document is the methodology. **The scores are not in it.** They live in
`eval-report.md` and `eval/baselines/main-<tier>.json`, written by a run, and
the reason they are not transcribed here is that a number copied by hand into a
document has no run attached to it and cannot be re-derived. Quote the artefact,
not the prose.

---

## 1. Why a detector needs an eval set at all

Anomaly detection invites a specific dishonesty. Every threshold change can be
justified in an argument — *this window is too twitchy, that alpha is too slow* —
and every such argument is unfalsifiable without a labelled set to run it
against. Six weeks of that produces a detector tuned to whatever incident was
most recent, and a confident story about why.

So the constraint (M8) is: **never tune alpha or a threshold without re-running
the set.** Not "prefer not to". The whole claim that this is engineering rather
than taste rests on it.

The second reason is subtler and is what most homegrown detectors get wrong:
without a null case you cannot measure false positives, and without measuring
false positives "it detects things" is not a result. Two of the twelve cases
inject nothing at all.

---

## 2. The golden set

Twelve cases, each a YAML file in `eval/cases/`. A case declares a fault to
inject, a load profile, the edge that should be blamed, a detection budget, and
— the important part — a list of edges that **must not** alert.

| # | Case | Family | Expected edge | Budget | Traps |
|---|---|---|---|---|---|
| 001 | `null_baseline` | null | *none* | 600s | — |
| 002 | `null_traffic_ramp` | null | *none* | 600s | — |
| 003 | `payment_latency_60ms` | latency | `order-svc→payment-svc` | 480s | 2 |
| 004 | `payment_latency_200ms` | latency | `order-svc→payment-svc` | 480s | 2 |
| 005 | `product_latency_800ms` | latency | `order-svc→product-svc` | 480s | 3 |
| 006 | `parallel_decoy` | **decoy** | `order-svc→payment-svc` | 480s | 2 |
| 007 | `payment_errors_10pct` | error | `order-svc→payment-svc` | 480s | 2 |
| 008 | `payment_errors_50pct` | error | `order-svc→payment-svc` | 480s | 2 |
| 009 | `user_cpu_saturation` | saturation | `order-svc→user-svc` | 480s | 2 |
| 010 | `payment_db_down` | dep_kill | `order-svc→payment-svc` | 480s | 2 |
| 011 | `deploy_correlated` | deploy | `order-svc→payment-svc` | 480s | 2 |
| 012 | `gateway_latency_cascade` | cascade | `gateway-svc→order-svc` | 480s | 3 |

### The two that carry the argument

**001 and 002 — the null cases.** Nothing is injected. 001 holds steady load;
002 ramps traffic, which is the thing that most reliably makes a naive detector
fire. A case passes by *staying silent* for the full 600 seconds. These are the
only measurement of false positives in the set, and they are the first cases a
struggling project is tempted to drop — which is why M9 exists and says not to.

**006 — the parallel decoy.** Two services are slowed inside the same concurrent
fan-out: `user-svc` by +100 ms and `payment-svc` by +150 ms. Both jump far past
the 50 ms threshold on wall clock. Only `payment-svc` is on the critical path,
because `user-svc` finishes ~160 ms earlier and `order-svc` was waiting on
payment the whole time.

A wall-clock detector alerts on both and books a false positive. A critical-path
detector alerts on payment alone.

**This is the case a wall-clock detector fails by design.** It is the entire
argument for the flagship feature, expressed as a number rather than a diagram.

> **The decoy is `user-svc`, not `product-svc`, and that correction is a
> measurement.** `product-svc` has an N+1 query path that fires on carts above a
> threshold, and roughly a fifth of generated carts cross it. On those traces
> product genuinely *is* the slowest leg and genuinely *does* carry blame:
> measured, +120 ms on product moved its critical p99 from 49.3 ms to 168.8 ms
> and tripped the trap at t+202s. A detector doing exactly the right thing would
> have scored a false positive. `user-svc` has a measured critical p99 of 0.0 —
> which is what a parallel sibling actually looks like.
>
> **And that measurement is host-dependent, which nobody knew until §9.** On a
> 2-vCPU contended GitHub runner `order-svc→user-svc` measures **56.6 ms**, not
> 0.0: the fan-out finish order genuinely differs under CPU contention. So this
> case is valid on a quiet machine and invalid on CI, and the guard that should
> have caught that was reading a hardcoded dict (**F42**). Whichever leg is used
> as the decoy, it has to be re-measured on the hardware the set runs on — this
> is the third time this application has moved its decoy.

---

## 3. Scoring

Four outcomes per case:

| Outcome | Meaning |
|---|---|
| `true_positive` | the correct edge, inside budget — or a null case that stayed silent |
| `false_negative` | a fault was present and never surfaced in time |
| `false_positive` | a trap edge alerted; something innocent got blamed |
| `wrong_edge` | something alerted, but neither the expected edge nor a trap |

`wrong_edge` counts against precision alongside `false_positive`. An alert
pointing at the wrong service costs the same on-call hour as one pointing at an
innocent service; the distinction is kept only so the failure is legible
afterwards.

From those: precision, recall, F1, and MTTD at p50/p95.

**MTTD is computed over detected cases only.** A null case that correctly stays
silent contributes no detection latency, and averaging a zero into it would make
silence look fast. Every surface that reports MTTD carries that caveat inline —
`/api/eval`, the markdown report and the Prometheus help text — because the
number is quotable and the caveat is not optional.

### The absolute floor, and why the MTTD ceiling is 360s

```
f1         >= 0.85 - 0.03
precision  >= 0.90 - 0.03
mttd p95   <= 360s x 1.2
```

The 360-second ceiling is a correction, not a concession. The reference harness
carried 180s, and **180s is arithmetically unreachable**: the detector's own
confirmation window is three cycles at sixty seconds, so 180s is the floor
*before* any pipeline time is counted, and roughly 125s of collector batching,
tail-sampling decision wait, assembler grace and minute-bucket completion sits
in front of it. Phase 6 measured 375s end to end against a documented ~245s
(F19).

A ceiling nothing can clear is worse than no ceiling: it goes red on a working
detector, and the natural response to a red gate is to weaken the thing being
gated. 360s is the pipeline worst case (425s, `eval/topology.py`) less the margin
the 1.2× tolerance already provides.

### Two gates that fail for different reasons

`scorer.py` asks *is this detector good enough in absolute terms.*
`check_regression.py` asks *is it worse than main.* A detector that was always
mediocre passes the second and fails the first; one that dropped from excellent
to merely good fails the second and passes the first. Both run.

---

## 4. Two tiers, and why the PR gate is not the full set

| Tier | Cases | Runtime | Runs on |
|---|---|---|---|
| `smoke` | 4 | ~45 min | PRs touching detector paths |
| `full` | 12 | ~110 min | nightly on `main`, and `workflow_dispatch` |

The smoke four are chosen to cover each failure mode the gate exists to catch,
not simply the fastest four: **001** null (false positives), **003** latency
sensitivity at the threshold, **006** the decoy, **007** the error path, which
latency percentiles cannot see.

Ninety minutes on every pull request trains people to route around CI, and a
gate people avoid is worse than no gate. This was decided as the right design
(D5) rather than adopted as a fallback when the schedule slipped.

> **Any score quoted anywhere must name its tier.** Smoke and full precision are
> computed over different case mixes and are different numbers. A score without
> its tier is not a score.

---

## 5. The baseline is committed, and nothing updates it automatically

`eval/baselines/main-full.json` and `main-smoke.json` are files in git.

A gate whose threshold lives outside version control is not a gate. Committing
the baseline means a change to what "acceptable" means arrives as a diff, gets
reviewed, and can be pointed at afterwards when someone asks why the number
moved.

The nightly run *reports* drift; it does not rewrite the file. A baseline that
updates itself can only ratchet in whichever direction the detector happens to
be drifting, so slow degradation becomes the new normal — the same boiling-frog
failure M6 guards against in the detector, one level up, and easier to introduce
here because "keep the baseline current" sounds like hygiene.

A missing baseline is a **hard failure** (exit 2), not a pass. The cheapest way
to turn a red gate green is to stop measuring, and "no baseline yet" is exactly
what that looks like from the outside.

---

## 6. The gate has been proven to fail

A quality gate nobody has watched go red is a decoration. `make eval-gate-guard`
breaks the detector deliberately and asserts the gate catches it; the offline
tests in `eval/tests/` do the same against the scorer without booting anything.

The same discipline applies to the harness's own guards. `eval/tests/test_ci_ports.py`
was written after a dispatched run died in preflight, and it was checked red
against the exact defect before being committed — not assumed to work because it
passed.

---

## 7. How to run it

```bash
make eval-up          # boot the stack, then wait ~6 minutes for warmup
make eval             # full tier, ~110 min
make eval-smoke       # 4-case tier, ~45 min
make eval-case CASE=006
make eval-check       # compare the last run to its tier's committed baseline
```

Offline, in milliseconds, without a stack:

```bash
make eval-tests       # case files, scorer, the gate, and the CI port guard
```

### The warmup is not optional

Five detector cycles plus pipeline lag — about six minutes. Skip it and the
warmup gate suppresses every case, which scores as twelve false negatives and
looks exactly like a broken detector. It is failure mode #1 in the harness
table, and the runner's preflight refuses to start without it.

---

## 8. Where the scores actually are

| Artefact | What it holds |
|---|---|
| `eval-report.md` | the last run's scoreboard, per case |
| `eval-run.json` | the same run, machine-readable, with the detector config |
| `eval/baselines/main-full.json` | the committed 12-case baseline |
| `eval/baselines/main-smoke.json` | the committed 4-case baseline |
| `GET /api/eval?tier=full` | the latest run from Postgres, with history |
| `tracemap_eval_*` | Prometheus gauges; `EvalGateRegressed` alerts on them |

`/api/eval` returns `detectorCfg` alongside every score, deliberately. A score
without the configuration that produced it is not reproducible, and the
difference between *"we tuned alpha and F1 improved"* and *"F1 improved"* is the
entire claim.

### On this repository's own numbers

The full-set baseline is recorded by a green `full` run on `main` and committed
in the same change that explains why it moved. **No such run has happened yet**,
so `eval/baselines/main-full.json` is absent and `check_regression.py` exits 2 —
the designed behaviour for "not yet measured".
A local run on a developer machine is **not** a substitute for that. Detection
latency is measured against `time.monotonic()`, which keeps advancing while the
process is descheduled; a host that stalls a worker for seven minutes (F36,
measured) inflates every MTTD in the run. Outcome classifications mostly survive
such a stall, because the runner queries anomalies *since case start* from
Postgres and still sees what fired during it — but the latency figures do not,
and MTTD p95 is one third of the baseline. That is why the full set is run on
dedicated CI hardware and not locally.

---

## 9. Three full-set runs, all red — and what they actually measured

Recorded here rather than quietly re-run until they passed, because a harness
whose failures are not published is a harness nobody should believe.

| | Run 1 | Run 2 | Run 3 | gate |
|---|---|---|---|---|
| run | [33848444520](https://github.com/Rajarshi1-source/TraceMap/actions/runs/33848444520) | [33860377967](https://github.com/Rajarshi1-source/TraceMap/actions/runs/33860377967) | [33870357809](https://github.com/Rajarshi1-source/TraceMap/actions/runs/33870357809) | |
| commit | `8308c692` | `26a3cb629` | `0fe7226e9` | |
| **traps valid?** | unknown | **no** (F42) | **yes** | |
| precision | 0.583 | 0.500 | **0.500** | ≥ 0.870 ✗ |
| recall | 1.000 | 1.000 | **1.000** | — |
| f1 | 0.737 | 0.667 | **0.667** | ≥ 0.820 ✗ |
| mttd p50 | 122 s | 183 s | 179 s | — |
| mttd p95 | 213 s | 214 s | 282 s | ≤ 432 s ✓ |
| | 7 tp / 5 fp | 6 tp / 6 fp | 6 tp / 6 fp | **FAIL** |

**Recall is 1.000 in all three runs. Zero false negatives across thirty-six case
executions.** The detector misses nothing. It over-alerts.

**MTTD p95 clears its ceiling in all three.** This is the number a developer
machine cannot produce: detection latency is measured against `time.monotonic()`,
which keeps advancing while a process is descheduled, so a host that stalls a
worker (F36: 471 s, measured) inflates every latency in the set.

### Run 3 is the one that counts

Runs 1 and 2 were scored against a decoy leg that was measurably carrying blame
— **F42**, a guard whose only hard assertion read a hardcoded dict and therefore
could not fail. Run 2's topology snapshot, taken on quiet traffic before any
fault:

```
crit p99 order-svc->user-svc  56.6 (recorded 0.0)  DRIFT     <- the decoy
```

Run 3 ran with the fixed guard, and it passed on measurement rather than on a
constant:

```
crit p99 order-svc->payment-svc  289.1 (recorded 157.0)  DRIFT
crit p99 order-svc->product-svc   48.3 (recorded  49.3)  OK
crit p99 order-svc->user-svc       0.0 (recorded   0.0)  OK
every declared decoy leg still carries no blame           OK
```

**The decoy measured exactly 0.0. The traps were provably valid. Precision was
still 0.500.**

That settles the question F42 raised: the score is *not* an artefact of the
harness. The detector genuinely over-alerts on this hardware.

### The failure is one edge, and it is stable

| Case | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| `null_baseline` | tp | tp | **fp** → `gateway-svc→order-svc` |
| `null_traffic_ramp` | fp | fp | **fp** → `gateway-svc→order-svc` |
| `user_cpu_saturation` | wrong | wrong | **wrong** → `gateway-svc→order-svc` |
| `payment_db_down` | wrong | wrong | **wrong** → `gateway-svc→order-svc` |
| `gateway_latency_cascade` | fp | fp | **fp** → co-alert |
| `parallel_decoy` | fp | fp | **fp** → co-alert |
| the other six | tp | tp | tp |

**`gateway-svc→order-svc` is the blamed edge in four of run 3's six failures,
including *both* null cases** — and `null_baseline` holds steady load with
nothing injected at all. That is not F38 (a traffic *ramp*); it fires at rest.

### Why — and the fix is not a bigger number

Compare the same edges, same code, two hosts:

| edge | this laptop | CI runner (run 3) |
|---|---|---|
| `order-svc→payment-svc` | ~148 ms | **289.1 ms** |
| `order-svc→product-svc` | ~48 ms | 48.3 ms |
| `order-svc→user-svc` | 0.0 ms | 0.0 ms |

The runner is roughly twice as slow on the critical path and much noisier.
`gateway-svc→order-svc` absorbs `order-svc`'s entire subtree by design — 735.0
in the specification test, because the gateway really did wait 735 ms on order —
so it carries both **the largest baseline and the largest absolute jitter** of
any edge in the graph.

The detector's breach test is a **fixed absolute threshold**:

```python
threshold_ms: float = 50.0    # the brief's ">50ms" requirement
breached = delta > cfg.threshold_ms
```

Fifty milliseconds is a large signal on an edge whose baseline is 30 ms. On an
edge whose baseline is ~290 ms and swinging with runner contention, it sits
**inside the noise**. The edge with the biggest numbers breaches most easily,
which is the opposite of what anyone wants.

This is **F43**, and the fix is not to raise 50 to 80. That would clear these
cases by making the detector deafer everywhere, and picking the number without a
run to justify it is exactly M8. The fix is for the breach test to scale with
the edge's own baseline or its observed variability, so an edge is judged
against itself rather than against a constant borrowed from the brief.

### The three remaining failures, briefly

- **`null_traffic_ramp`** is **F38**, logged open before any of these runs: the
  detector cannot tell a latency rise caused by load from one caused by a
  regression. Every existing gate correctly declines to fire.
- **`user_cpu_saturation` / `payment_db_down`** are `docs/LIMITATIONS.md`
  caveat 7 — nothing suppresses a parent whose rise is fully explained by a
  child's. Suppressing parents unconditionally would be wrong:
  `gateway_latency_cascade` is a case where the parent genuinely *is* the
  finding.
- **`parallel_decoy` / `gateway_latency_cascade`** found the right edge and lost
  the case to a second alert on a trap. The scoring is correct to charge for
  that: an extra alert on an innocent edge costs the same on-call hour whether
  or not a correct one accompanied it.

All three are the same shape as F43 — an edge alerting when it should not — and
it is plausible that a baseline-relative threshold moves several of them at
once. Plausible is not measured, which is the whole point of this document.

### What is not being done about it

**No threshold has been moved.** Tuning to clear a gate is what M8 forbids, and
"precision was low, so we raised the threshold" is indistinguishable from having
no gate. `eval/baselines/main-full.json` remains **absent** and
`check_regression.py` continues to exit 2 — the designed behaviour for "not yet
measured". A baseline will be committed from a green run and from nothing else.

**The score is not adjusted for F42 either.** Runs 1 and 2 stand as recorded,
labelled with the reason they cannot be trusted. Adjusting a score for a fault
found in your own measurement apparatus is how a scoreboard stops meaning
anything.

### What this run bought

Three things that did not exist before it:

1. **A trustworthy measurement.** Run 3 is the first scoreboard whose traps were
   verified valid *before* the cases ran. Precision 0.500 is now a fact about
   the detector rather than a fact about the harness.
2. **A specific, testable hypothesis** — F43, with a mechanism and a measurement
   behind it, rather than "precision is low".
3. **A harness that can no longer hide this.** The guard fails on an invalid
   decoy, and every future `eval-run.json` records `topology_at_start`,
   `traps_valid` and `invalid_traps` alongside its scores, so a number and the
   reason it cannot be trusted travel together.

The next run is worth doing only after F43 is addressed. Running the set again
unchanged would produce 0.500 a third time and teach nothing.
