# ADR 0005 — An EWMA baseline, and what it took to make one actually work

- **Status:** Accepted
- **Date:** 2026-09-03 (Phase 6)
- **Deciders:** Rajarshi Basu

## Context

The detector needs to decide, once a minute, whether an edge's critical-path p99
has degraded. Three options:

| | Why not |
|---|---|
| **Static threshold** (`p99 > 400ms`) | Traffic at 3am is not traffic at 3pm. A threshold loose enough for the peak is blind at the trough, and one tight enough for the trough pages every afternoon. |
| **Trained ML model** (LSTM, prophet) | An LSTM on eight weeks of synthetic traffic is overfitting theatre. It cannot be justified per-alert, needs training data this project does not have, and would be defended with vibes. |
| **EWMA baseline per edge** | One tunable parameter, no training data, and every alert traceable to two numbers a human can check. |

## Decision

**An exponentially weighted moving average per edge, compared against
critical-path p99, behind five gates.**

The honest framing when asked why not a model: *"I have the eval harness — if a
model beat this on the golden set, I would switch, and I would have the number
to prove it."* That is a stronger position than either building one or
dismissing one.

## What building it actually taught

The design above is the easy part. Making it fire on a real 60ms fault took
three corrections, each found by running it rather than reasoning about it.

### 1. The evaluation window was hiding the fault

The dashboard reads a 5-minute rolling window because a human reading a graph
wants the trend. **The detector must not.** A p99 over a rolling window blends
pre-fault and post-fault requests, so an injected step arrives as a *ramp*: each
cycle's delta lands under the threshold, the EWMA legitimately learns it, and
the baseline chases the fault into place without a single cycle ever breaching.

Measured: a 60ms injection **never fired at all** in 422 seconds, while the
baseline drifted 113 → 123.6.

Narrowing to a 1-minute rolling window does not fix it either, and measuring
says why: the pipeline lag from trace to roll-up is **~54s**, so
`now() - INTERVAL 1 MINUTE` excludes the newest bucket entirely and the detector
reads `nan`.

**The detector now evaluates a single minute-bucket** — the newest one that
exists. A bucket is homogeneous in time: every request in it shares one fault
state, so it cannot dilute, and taking the newest rides whatever the lag
happens to be instead of encoding an assumption about it.

Measured step response, 60ms injection:

| | pre | +46s | +91s | +136s | +408s |
|---|---|---|---|---|---|
| single bucket | 169.5 | 113.9 | **173.5** | **198.4** | 202.6 |
| rolling 5-min | 124.4 | 120.9 | 167.9 | 176.3 | **206.4 — still climbing** |

### 2. The baseline armed 28ms too high

The baseline is seeded from a *single* first observation. If that sample is an
outlier, `alpha=0.2` cannot walk it off in five warmup cycles — and every
millisecond of leftover inflation is subtracted from every future delta.

Measured: a seed on a 146ms outlier against a true level of ~110ms armed the
baseline at **139.5ms**. A real 65ms fault then presented as a **40ms delta**,
under the 50ms threshold, and went undetected.

**The detector was behaving correctly. The baseline was wrong.** That
distinction matters: no amount of threshold tuning fixes a mis-estimated
baseline, and lowering the threshold to compensate would have traded a missed
detection for a permanent false-positive rate.

Fixed with **two regimes of the same estimator**: `warmup_alpha = 0.5` to
converge fast while warming, `alpha = 0.2` to resist noise once armed. The
baseline now arms within 3ms of the true level.

### 3. "Three consecutive" was too brittle for a real p99

With a correct baseline, a +60ms fault gives a ~57ms delta against a 50ms
threshold — and bucket-to-bucket p99 noise is **±20ms**. The delta oscillates
either side of the threshold, and under a *consecutive* rule a single dip
resets the run. Measured: the counter reached 2, reset, reached 1, reset, and
the fault went undetected for 472 seconds.

**Changed to 3 breaches within a 5-cycle window.** Same evidence bar — one bad
minute still never alerts — but it tolerates the noise a real percentile has.

This created a second, subtler boiling frog, and closing it is the part worth
remembering. M6 says *do not fold a breaching observation into the baseline*.
That is necessary and **not sufficient**: under a sustained fault with noise,
the occasional sub-threshold cycle is not a breach, so it teaches the baseline,
and the baseline walks up onto the fault one dip at a time. M6 satisfied to the
letter, defeated in practice.

So the baseline now moves **only when the entire recent window is clean**, not
merely when the current cycle is.

## Consequences

**Good.** Explainable end to end: two numbers, one threshold, a window. Every
correction above is encoded as a test, including two that reproduce the exact
live failures.

**Bad — and this is the number to quote honestly.** Measured time from injection
to alert is **375 seconds**, against the 180s in the build plan and the 245s
worst case in `eval-harness.md`. Both are optimistic because neither accounts
for the assembler lag plus bucket completion:

```
  assembler lag (trace -> roll-up)        ~54s
+ bucket must complete                    ~60s
+ detector notices on its next cycle      ~60s
+ 3 breaches at 60s each                 ~180s
                                        = ~354s
```

`detect_within_s: 180` in the golden set would fail against reality. **Phase 7
must widen those budgets to ~400s before the harness is trusted** — otherwise
the first eval run scores a working detector as broken, and the natural
response is to weaken the detector rather than fix the number.

**Neutral.** `min_requests = 100` stays at the plan's value. It is tunable for a
laptop demo, but changing it changes the thing being measured, and any score
quoted afterwards must say which value produced it.
