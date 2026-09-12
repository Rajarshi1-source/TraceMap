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

## 9. Five full-set runs, all red — and what each one bought

Recorded here rather than quietly re-run until they passed, because a harness
whose failures are not published is a harness nobody should believe. Two of these
runs measured the harness rather than the detector, one measured a defect the fix
itself introduced, and the last one found that the *runner* cannot sustain the
detector's confirmation window at all. That sequence is the useful part.

| | Run 1 | Run 2 | Run 3 | Run 4 | Run 14 | gate |
|---|---|---|---|---|---|---|
| run | [3384…4520](https://github.com/Rajarshi1-source/TraceMap/actions/runs/33848444520) | [3386…7967](https://github.com/Rajarshi1-source/TraceMap/actions/runs/33860377967) | [3387…7809](https://github.com/Rajarshi1-source/TraceMap/actions/runs/33870357809) | [3388…7039](https://github.com/Rajarshi1-source/TraceMap/actions/runs/33887207039) | [3468…0528](https://github.com/Rajarshi1-source/TraceMap/actions/runs/34680830528) | |
| detector | flat 50 ms | flat 50 ms | flat 50 ms | 4× MAD, ungated | **4× MAD, gated** | |
| **traps valid?** | unknown | **no** (F42) | yes | yes | yes | |
| **premises valid?** | unchecked | unchecked | unchecked | unchecked | **1 excluded** (F44) | |
| precision | 0.583 | 0.500 | 0.500 | 0.714 | **0.625** | ≥ 0.870 ✗ |
| recall | 1.000 | 1.000 | 1.000 | 0.500 | **0.625** | — |
| f1 | 0.737 | 0.667 | 0.667 | 0.588 | **0.625** | ≥ 0.820 ✗ |
| mttd p95 | 213 s | 214 s | 282 s | 214 s | **213 s** | ≤ 432 s ✓ |
| | 7 tp / 5 fp | 6 tp / 6 fp | 6 tp / 6 fp | 5 tp / 2 fp / 5 fn | 5 tp / 3 fp / 3 fn | **FAIL** |

**MTTD p95 clears its ceiling in every run.** This is the number a developer
machine cannot produce: detection latency is measured against `time.monotonic()`,
which keeps advancing while a process is descheduled, so a host that stalls a
worker (F36: 471 s, measured) inflates every latency in the set.

### Runs 1–3: the harness, then the detector

Runs 1 and 2 were scored against a decoy leg that was measurably carrying blame
— **F42**, a guard whose only hard assertion read a hardcoded dict and therefore
could not fail. Run 2's pre-fault snapshot:

```
crit p99 order-svc->user-svc  56.6 (recorded 0.0)  DRIFT     <- the decoy
```

Run 3 ran with the fixed guard, which asserted validity on measurement:

```
crit p99 order-svc->user-svc   0.0 (recorded 0.0)  OK
every declared decoy leg still carries no blame     OK
```

**The traps were provably valid and precision was still 0.500.** That settled it:
the score was a fact about the detector, and fixing the harness is what made the
detector defect legible. `gateway-svc→order-svc` was the blamed edge in four of
six failures, *including both null cases* — and `null_baseline` injects nothing
and holds steady load.

### Run 4: the fix worked, and then a second defect ate the gain

**F43.** The breach test was a flat 50 ms for every edge, which makes the
*noisiest* edge the twitchiest — precisely backwards. `gateway-svc→order-svc`
absorbs order-svc's whole subtree, so it carries the largest baseline and the
largest jitter in the graph. Scaling by *baseline* is arithmetically impossible
here: suppressing gateway's measured +234 ms of noise needs k > ~0.52, while
keeping payment's +109 ms detection needs k < 0.377. Magnitude does not separate
signal from noise. **Variability does**, so the bar became
`max(50 ms, 4 × MAD)` per edge.

**It did what it was built to do.** Both null cases passed for the first time in
the project's history — including `null_traffic_ramp`, which is **F38**, logged
open as needing its own throughput signal.

**And recall halved.** Five false negatives, three of them cases that had
detected cleanly in every previous run. The detector's own log says why:

```
edge gateway-svc->order-svc breach 1/3 in last 5 (+1788.3ms)
```

A delta of 1788 ms reaching one breach in five is not a threshold that is too
high. **It is a threshold that is moving.**

This is **F43b**, and it was introduced by the fix's own second pass. Dispersion
had been gated on the all-clean window — the same guard the level uses — and that
gate was removed on the reasoning that the sample is always below the bar, so the
estimate could not run away. Under a *sustained fault* that reasoning fails: the
sub-threshold cycles are not noise, they are the fault's own lower excursions.
Learning them raises MAD, which raises the bar, which makes more cycles
sub-threshold. The bar climbs after the fault.

That is exactly the failure M6 exists to prevent, one derivative up, and harder
to see because nothing about the *level* looks wrong.

Reverted. And the test that should have caught it —
`test_dispersion_does_not_learn_from_a_breaching_window` — fed only
pure-breaching observations, so its guard held trivially and the realistic
oscillating case was never exercised. Its replacement drives the real mechanism
and was checked red first, at which point the bar climbs to 477 ms while chasing
a 600 ms fault.

### What each run actually bought

1. **Run 1** — the first complete twelve-case execution, and the first MTTD p95
   the project could quote.
2. **Run 2** — F42. The scoreboard was measuring the harness, and nothing in the
   artifact said so. Every `eval-run.json` now records `topology_at_start`,
   `traps_valid` and `invalid_traps`, so a number and the reason it cannot be
   trusted travel together.
3. **Run 3** — a trustworthy measurement, and with the harness explanation
   removed, F43 became legible.
4. **Run 4** — proof that the F43 mechanism works on its target (both null cases
   passed) and proof that its second pass was wrong (recall halved). Neither
   fact was available from reading the code; the second was invisible to the
   unit test written specifically to prevent it.

### What the arithmetic said, and why there was no fifth run

Before dispatching another ninety minutes, the obvious question: what would it
actually take to clear this gate?

```
 9 of 12 correct -> precision 0.750  FAIL
10 of 12 correct -> precision 0.833  FAIL
11 of 12 correct -> precision 0.917  PASS
```

**At most one wrong case out of twelve**, and four distinct failure classes were
in play. So the useful question stopped being "which threshold next" and became
**"are all twelve of these cases even measuring the detector?"**

Two of them are not — **F44**. Case 009 injects a 250 ms CPU burn and asserts it
carries `order-svc->user-svc` past `order-svc->payment-svc`. That margin is
computed against a constant recorded at 157.0 ms. The same edge has now been
measured at **187.97 ms** and at **~289 ms** on CI. The thing the margin is
measured against moves by more than the margin, and at 289 ms the burn does not
overtake at all — `user-svc` correctly carries no blame, and a detector doing
exactly the right thing is scored a false negative.

That is F42's lesson generalised: F42 was one declared constant that had stopped
being true, and fixing them one at a time as each is discovered is how the third
instance gets found in production.

**Cases now declare their preconditions**, checked against the topology
snapshotted before any fault goes in:

```yaml
preconditions:
  - kind: overtakes
    edge: "order-svc->payment-svc"
    margin_ms: 40
```

A case whose premise does not hold is not run, is recorded as
`invalid_precondition`, and is excluded from every ratio. **That is not adjusting
a score** — it is declining to make a measurement the apparatus cannot support,
exactly as preflight declines to start on an idle stack rather than producing a
number from one.

Three guards, because "exclude the inconvenient cases" is otherwise the easiest
way to turn a gate green: preconditions live in version-controlled case files;
the scoreboard prints **"Scored over 11 of 12 cases"** directly under the
verdict rather than in a details block; and past `MAX_EXCLUSIONS = 2` the run
fails outright, while `check_regression --update` refuses to record a baseline
from a run with *any* exclusions.

### Run 14 answered the open question — and asked a better one

The nightly schedule settled it without a dispatch. Run 14 carries F43's
dispersion bar with **correct gating** plus F44's precondition checks, and it is
the first run whose traps *and* premises were both validated against the host.

**Both null cases passed.** The F43 gain was real, not an artefact of the
runaway — including `null_traffic_ramp`, which is F38. That question is closed.

`user_cpu_saturation` was excluded, exactly as F44 predicted, and the run's own
snapshot shows why: it budgets a 250 ms burn to overtake an edge recorded at
157.0 ms and **measured at 307.8 ms that night**.

But the remaining failures are not what four runs of threshold work assumed.

### F48 — the confirmation window cannot complete on a contended runner (measured)

The detector confirms a breach with **3 cycles out of the last 5**, one a minute.
That assumes bucket-to-bucket critical p99 is stable enough for three of five
consecutive minutes to stay over the bar. Run 14's own logs say it is not:

| | |
|---|---|
| cycles where a breach was seen and the window **reset before completing** | **32** |
| of those, at a delta above 500 ms | **19** |
| largest delta whose window never completed | **+2170.4 ms** |
| alerts that actually fired, across 11 scored cases | 5 |

**A 2.17-second delta failing to confirm is not a threshold set too high.** A bar
set too high produces *no breach at all*. These are breaches that keep resetting,
because the per-minute percentile is swinging across the bar on a contended
2-vCPU host.

That accounts for the shape of everything left. Three false negatives are faults
that were plainly visible and never confirmed. Both `wrong_edge` results are the
*parent* edge confirming while its child did not — not because the parent is
preferred, but because it happened to string three cycles together and the child
did not. Which means **parent suppression would not have helped**: it would have
removed the parent's alert and left nothing, converting two `wrong_edge` into two
false negatives. That is the same trap run 4 fell into, and the arithmetic caught
it this time before the change was made rather than after.

**It is not being fixed by widening the window.** Loosening `sustained_cycles`
until CI goes green is tuning a threshold to clear a gate — M8 — and it would
make the detector twitchier everywhere to accommodate one host. The confirmation
policy exists because one bad minute must never page anyone, and that does not
stop being true because the runner is busy.

**It names its own test.** The same detector confirmed cleanly on a quiet host in
Phase 6. The cheapest experiment is one nightly run on a 4-vCPU runner: if the
numbers move, the gate's redness was substantially the runner; if they do not,
this entry is wrong and the detector owns it.

This was predicted before the very first dispatch — *"it may also fail, and if it
does, that is a genuine finding about where this suite can run, not a wasted
afternoon."* Five runs later it is measured rather than predicted.

**Measured, 12 Sep 2026.** F48 named its own test and the test has been run — not
as a full control eval, but as a direct measurement of the quantity the claim is
actually about. The substrate, not the scoreboard.

Same stack, same commit, same 20 rps load profile, on a machine with 18 vCPU
available to Docker instead of the runner's 2.

| | this host (18 vCPU) | CI run 14 (2 vCPU) |
|---|---|---|
| `gateway-svc→order-svc` critical p99, mean | **176.4 ms** | 391.2 ms |
| `order-svc→payment-svc` critical p99, mean | **148.3 ms** | 307.8 ms |
| `gateway-svc→order-svc`, range over 38 one-minute buckets | **133.2 – 274.0 ms** | — |
| largest excursion the detector logged | **+97.6 ms** | **+2170.4 ms** |

And the detector's own learned dispersion, read straight out of Redis after
warm-up on this host:

```
gateway-svc:order-svc:mad    5.06 ms     ->  bar = max(50, 20.2) = 50 ms  (the floor)
order-svc:payment-svc:mad    0.18 ms     ->  bar = max(50,  0.7) = 50 ms  (the floor)
```

**On this host the F43 dispersion mechanism is completely inert.** Every edge
sits on the brief's original 50 ms floor, so the detector here behaves exactly
as the flat-threshold version did. That is the design working: dispersion only
ever raises the bar, and there is no noise here to justify raising it.

The CI figure can be bounded from the other direction. Run 14's null cases
*passed*, so the 4 × MAD bar was high enough to suppress a measured +234 ms
excursion on `gateway-svc→order-svc` — which requires **MAD > 58.5 ms** there.

> **The same edge, the same code, the same load: a noise floor of 5.06 ms here
> and at least 58.5 ms on the runner. A factor of eleven, minimum.**

The whole observed range on this host — 141 ms wide — is narrower than a single
CI excursion, and CI logged breach deltas up to twenty-two times the largest
excursion seen here.

**What this establishes, stated narrowly.** The measurement substrate differs by
an order of magnitude between the two hosts, which is precisely what F48 claims
and precisely what determines whether a 3-of-5 confirmation window can hold. It
does **not** establish that a full twelve-case run would go green here — that run
was not performed. Two attempts were made and **the harness refused to score
either one**: the analytics API became unreachable partway through, and the
runner abandoned at 0/4 and 0/1 cases rather than charging every remaining case
as a false negative. It declined to record the run at all -- *"eval_runs is a
table of measurements, not of attempts"* -- and stamped the empty scoreboard
*"diagnostic only. Do not quote them and do not record them as a baseline."*

That is the harness working correctly, and it is worth more than the data would
have been: the one thing a quality gate must never do is emit a plausible number
from a broken measurement. It emitted zeros and said why.

The honest sentence is therefore *"the substrate is eleven times quieter and the
detector's own dispersion estimate says so"*, not *"the suite passes on a bigger
box"*.

**What would close it completely** is still one full-set run on a 4-vCPU runner,
which `vars.EVAL_RUNNER` now exists to enable without a code change.

### What is not being done about it

**No threshold has been moved to clear the gate.** `4 × MAD` came from the
distribution — σ ≈ 1.253 × MAD, so 4× is ≈ 3.2σ, roughly 0.1 % per observation
with the 3-of-5 window on top — and not from running the set until it went green.
That distinction matters more than the number.

`eval/baselines/main-full.json` remains **absent** and `check_regression.py`
continues to exit 2, which is the designed behaviour for "not yet measured". A
baseline will be committed from a green run and from nothing else.

**No score has been adjusted.** Runs 1 and 2 stand as recorded, labelled with the
reason they cannot be trusted. Adjusting a score for a fault found in your own
measurement apparatus is how a scoreboard stops meaning anything.
