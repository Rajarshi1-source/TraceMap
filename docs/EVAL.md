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

## 9. Two full-set runs, both red — and what they actually measured

Recorded here rather than quietly re-run until they passed, because a harness
whose failures are not published is a harness nobody should believe.

| | Run 1 · [33848444520](https://github.com/Rajarshi1-source/TraceMap/actions/runs/33848444520) | Run 2 · [33860377967](https://github.com/Rajarshi1-source/TraceMap/actions/runs/33860377967) | gate |
|---|---|---|---|
| commit | `8308c692` | `26a3cb629` (F41 fixed) | |
| precision | 0.583 | **0.500** | ≥ 0.870 ✗ |
| recall | 1.000 | **1.000** | — |
| f1 | 0.737 | **0.667** | ≥ 0.820 ✗ |
| mttd p50 | 122 s | 183 s | — |
| mttd p95 | 213 s | **214 s** | ≤ 432 s ✓ |
| outcome | 7 tp / 5 fp / 0 fn | 6 tp / 6 fp / 0 fn | **FAIL** |

**Recall is 1.000 in both runs and there is not a single false negative across
twenty-four case executions.** The detector misses nothing. It over-alerts —
a much better failure than the reverse, and still a failure.

**MTTD p95 is stable at 213 s and 214 s against a 432 s ceiling.** This is the
number a developer machine cannot produce: detection latency is measured against
`time.monotonic()`, which keeps advancing while a process is descheduled, so a
host that stalls a worker (F36: 471 s, measured) inflates every latency in the
set. Two independent runs landing within one second of each other is what
dedicated hardware buys.

### Exactly one case moved between runs

| Case | Run 1 | Run 2 |
|---|---|---|
| `payment_latency_60ms` | true positive | **false positive** |

It still found the right edge — `order-svc→payment-svc` — and lost the case to
an *additional* alert on a trap. Every other case kept its outcome. **F41 did
not measurably change the score**, and with one run either side it would be
dishonest to claim it helped or hurt; the failure taxonomy is identical in both.

### What the failures actually are — and two of them are the harness

Six failures in run 2, in three groups.

**Group A — two cases scored against a trap that was not a trap. This is F42.**

The topology guard printed this on quiet traffic, *before a single case ran*:

```
crit p99 order-svc->payment-svc  148.0 (recorded 157.0)  OK
crit p99 order-svc->product-svc   49.0 (recorded  49.3)  OK
crit p99 order-svc->user-svc      56.6 (recorded   0.0)  DRIFT
at least one leg is still a true parallel sibling         OK
```

`order-svc→user-svc` — the decoy, the leg whose whole purpose is carrying **no**
blame — was measured at **56.6 ms of critical path against a declared 0.0**. On a
2-vCPU contended GitHub runner the fan-out finish order genuinely differs from a
quiet developer machine, and `user-svc` lands on the critical path often enough
to matter.

So when the detector alerted on `order-svc→user-svc` at +339 ms in
`payment_latency_60ms` and in `parallel_decoy`, **it was right**, and the
scoreboard recorded it as a false positive twice.

Worse: `parallel_decoy` is the flagship case. On this hardware it was not
testing what it claims to test.

The guard *detected* this and did not fail, because its only hard assertion
about decoy validity was reading a hardcoded dict rather than the measurement —
a check that could not fail. That is F42, now fixed and verified by reproducing
the condition. **The full-tier scoreboard from a GitHub runner therefore carries
an asterisk until the decoy is re-measured on that hardware.**

**Group B — three cases where the parent edge alerted.** `null_traffic_ramp`,
`user_cpu_saturation` and `payment_db_down` all surfaced
`gateway-svc→order-svc`. That edge absorbs `order-svc`'s entire subtree by
design — 735.0 in the specification test, because the gateway really did wait
735 ms on order — and owns roughly 52% of the critical path in steady state.

`null_traffic_ramp` is **F38**, already recorded open with the failure already
reproduced: the detector cannot distinguish a latency rise caused by a
regression from one caused by load. Every existing gate correctly declines to
fire, and the fix needs a second EWMA over throughput plus a threshold — which
cannot be chosen without an eval run, because that is M8.

The other two are `docs/LIMITATIONS.md` caveat 7: nothing suppresses a parent
whose rise is fully explained by a child's. Note that suppressing parents
unconditionally would be wrong — `gateway_latency_cascade` is a case where the
parent genuinely *is* the finding.

**Group C — one co-alert.** `gateway_latency_cascade` found the right edge and
also alerted on `order-svc→payment-svc`. Under the scoring rules that is
correctly charged: an extra alert on an innocent edge costs the same on-call
hour whether or not a correct alert accompanied it.

### So what does the 0.500 mean?

Honestly: **less than it looks, and it is still not a pass.**

Two of six failures are the harness being wrong about the host. Of the
remaining four, one is a defect logged open before either run and three are one
structural gap. Nothing in either run suggests the detector is missing
incidents — recall is perfect twice over.

But the number stands as measured. It is not adjusted for the harness defect,
because adjusting a score for a fault you found in your own measurement
apparatus is exactly how a scoreboard stops meaning anything. The fix is to
repair the harness and re-measure, in that order.

### What is not being done about it

**No threshold has been moved.** Tuning to clear a gate is what M8 forbids, and
"precision was low, so we lowered the floor" is indistinguishable from having no
gate at all. `eval/baselines/main-full.json` remains **absent**, and
`check_regression.py` continues to exit 2, which is the designed behaviour for
"not yet measured". A baseline will be committed from a green run and from
nothing else.

### The order of work this implies

1. **F42 is fixed** — the decoy invariant is measured and hard, so a run whose
   traps are invalid now stops instead of producing a scoreboard.
2. **Re-measure `CRIT_P99_BASELINE_MS` on the runner** and choose the decoy from
   that measurement. It must be a measurement, not a choice — this is the third
   time this application has moved its decoy (F30, F37, F42).
3. **Then** re-run the set, and only then is a precision figure worth arguing
   about.
4. Parent suppression and F38 come after that, each justified by its own run.
