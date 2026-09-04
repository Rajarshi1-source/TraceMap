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

## 9. The first full-set run — and it failed

Recorded here rather than quietly re-run until it passed, because a harness
whose failures are not published is a harness nobody should believe.

**Run [33848444520](https://github.com/Rajarshi1-source/TraceMap/actions/runs/33848444520),
tier `full`, 12 cases, `8308c692`, on CI hardware.**

| metric | value | gate | |
|---|---|---|---|
| precision | **0.583** | ≥ 0.870 | ✗ |
| recall | **1.000** | — | |
| f1 | **0.737** | ≥ 0.820 | ✗ |
| mttd p50 | 122 s | — | |
| mttd p95 | **213 s** | ≤ 432 s | ✓ |

7 true positives, 5 false positives, **0 false negatives**.

### What the shape of it says

**Recall is 1.000 and precision is 0.583.** The detector misses nothing and
over-alerts. That is a much better failure than the reverse — a detector that
misses incidents is useless, one that over-alerts is noisy — but it is still a
failure, and the gate is right to say so.

**MTTD p95 of 213 s clears the 432 s ceiling with room to spare**, which is the
first time that number has been measurable at all: detection latency is measured
against `time.monotonic()`, which keeps advancing while a process is descheduled,
so a local run on a host that stalls a worker (F36: 471 s, measured) inflates
every latency in the set. This is what dedicated CI hardware buys.

### The five failures, and their single cause

| Case | Outcome | Blamed |
|---|---|---|
| `null_traffic_ramp` | false positive | `gateway-svc→order-svc` — nothing was injected |
| `parallel_decoy` | false positive | got `order-svc→payment-svc` **correctly**, and *also* alerted on the decoy `order-svc→user-svc` |
| `user_cpu_saturation` | wrong edge | `gateway-svc→order-svc`, expected `order-svc→user-svc` |
| `payment_db_down` | wrong edge | `gateway-svc→order-svc`, expected `order-svc→payment-svc` |
| `gateway_latency_cascade` | false positive | got `gateway-svc→order-svc` **correctly**, and *also* blamed `order-svc→payment-svc` |

**`gateway-svc→order-svc` appears in three of the five**, and it is worth being
precise about *why*, because the three are not all the same thing.

That edge absorbs `order-svc`'s entire subtree by design — it is 735.0 in the
specification test, because the gateway really did wait 735 ms on order. It also
owns roughly 52% of the critical path in steady state. So it is genuinely the
biggest edge in the system, and *any* downstream degradation raises it too.

**One of the three is a known open defect, not a new finding.**
`null_traffic_ramp` is **F38**, recorded open in `IMPLEMENTATION_PLAN.md` with
the failure already reproduced: the detector cannot distinguish a latency rise
caused by a regression from one caused by load. Offered load doubles, queueing
raises p99, nothing is wrong, and every existing gate correctly declines to
fire — the request-count floor is a *minimum*, the ramp *is* sustained, and the
blame-share gate agrees the edge really is the bottleneck. F38 is deliberately
unfixed because the fix needs a second EWMA over throughput and a threshold, and
picking that threshold without an eval run to justify it is M8. The run this
document reports is the beginning of that number.

**Two are a different gap: the parent alerted instead of the child.** In
`user_cpu_saturation` and `payment_db_down` the culprit edge was real and
identifiable, and what surfaced was the parent that contains it. When a parent's
critical-path increase is fully explained by a child's in the same window it is
the same finding one level up, not an independent one — and when a single alert
is taken as "the answer", it points at the gateway instead of the culprit.
Nothing currently suppresses that.

**Two are co-alerts.** In `parallel_decoy` and `gateway_latency_cascade` the
right edge *was* found — the decoy case correctly named `order-svc→payment-svc`
— and a second alert on a trap edge cost the case anyway. Under the scoring
rules that is correct: an extra alert on an innocent edge costs the same on-call
hour whether or not a correct alert accompanied it.

So: **one known open defect, one structural gap, and a scoring rule doing its
job.** Not five unrelated bugs.

### What was done about it

Not a threshold change. Tuning to clear a gate is exactly what M8 forbids, and
"precision was low so we lowered the floor" is indistinguishable from having no
gate.

Instead, **F41** — a genuine correctness defect found separately while the run
was in flight. The assembler was filling per-call rows from a map keyed by
service pair, so a retry or the saga's compensation gave every call on an edge
the pair total. Measured on live data: 4.83% of `order-svc→payment-svc` rows
carried `critical_ms > duration_ms`, which is definitionally impossible. Since
`crit_p99` is what the detector alerts on and the map ranks by, that inflation
plausibly contributes to several of the failures above — including the decoy.

The set is being re-run against the fix. **Whatever it reports will be recorded
here, passing or not.** A baseline will be committed from a green run and from
nothing else.
