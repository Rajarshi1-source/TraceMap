---
name: tracemap-anomaly-eval
description: Build and maintain TraceMap's detection layer and its MLOps wrapper — the EWMA-baselined anomaly detector with sustained-window confirmation, request-count floors and Redis dedup, plus the fault-injection eval harness that scores precision, recall, F1 and MTTD against a golden set and gates CI merges on regression. Use for ANY work on anomaly detection, alert thresholds, baselines, alert fatigue, false positives, the golden set, eval scoring, detector drift, or the CI quality gate — and also when the user says "it alerts too much", "it missed the spike", "tune the detector", "prove it works", or "add MLOps to this". MANDATE - the baseline updates ONLY on healthy observations, because folding anomalous values into the EWMA makes a slow degradation the new normal and blinds the detector; and every detector change must be justified by an eval number, not an argument. Read references/eval-harness.md for the golden set and runner. Pair with tracemap-deploy-attribution for alert enrichment.
---

# TraceMap — Anomaly Detection and the Eval Harness

Two halves that only make sense together: a detector deliberately simple enough to defend, and a harness that measures whether it actually works. The harness is the differentiator — plenty of tools detect; almost none publish a scoreboard of their own accuracy.

## Detector design

Requirements, and the mechanism each one forces:

| Requirement | Mechanism |
|---|---|
| Traffic at 3am differs from 3pm | EWMA baseline per edge, not a fixed threshold |
| One bad minute is noise | Sustained-window confirmation (3 consecutive cycles) |
| p99 over 4 requests is meaningless | Minimum request count floor (100) |
| No Slack storms across replicas | Redis dedup via `SET NX EX` |
| Detect what hurts users | Evaluate **critical-path p99**, wall clock secondary |
| Don't alert on a cold baseline | Warmup counter before the detector is armed |

```python
# platform/src/core/anomaly_detector.py  (core gates)
@dataclass(frozen=True)
class DetectorConfig:
    alpha: float = 0.2               # EWMA smoothing; higher adapts faster
    threshold_ms: float = 50.0       # the brief's ">50ms" requirement
    min_requests: int = 100
    sustained_cycles: int = 3
    dedup_ttl_s: int = 600
    baseline_ttl_s: int = 86_400
    warmup_cycles: int = 5


async def evaluate(self, obs: EdgeObservation) -> Anomaly | None:
    cfg = self.cfg

    # Gate 1 — statistical floor
    if obs.request_count < cfg.min_requests:
        return None

    baseline_raw = await self.redis.get(self._k_base(obs.source, obs.target))

    # Gate 2 — cold start: seed, never alert on first sight
    if baseline_raw is None:
        await self._store_baseline(obs, obs.critical_p99_ms)
        await self.redis.incr(self._k_warm(obs.source, obs.target))
        return None

    baseline = float(baseline_raw)
    delta = obs.critical_p99_ms - baseline

    warm = int(await self.redis.get(self._k_warm(obs.source, obs.target)) or 0)
    if warm < cfg.warmup_cycles:
        await self.redis.incr(self._k_warm(obs.source, obs.target))
        await self._update_ewma(obs, baseline)
        return None

    # Gate 3 — breach?
    if delta <= cfg.threshold_ms:
        await self.redis.delete(self._k_count(obs.source, obs.target))
        await self._update_ewma(obs, baseline)      # learn ONLY from normality
        return None

    # Gate 4 — sustained confirmation
    n = await self.redis.incr(self._k_count(obs.source, obs.target))
    await self.redis.expire(self._k_count(obs.source, obs.target), 600)
    if n < cfg.sustained_cycles:
        return None

    # Gate 5 — dedup; SET NX EX is atomic, one replica wins
    won = await self.redis.set(self._k_dedup(obs.source, obs.target), "1",
                               nx=True, ex=cfg.dedup_ttl_s)
    if not won:
        return None

    return Anomaly(obs.source, obs.target, "critical_p99",
                   baseline, obs.critical_p99_ms, delta, obs.request_count)
```

### The one decision to be able to defend

**The baseline updates only when the edge is healthy** (the `_update_ewma` call sits inside the non-breach branch). Keep folding anomalous values in and a slow degradation becomes the new normal — the classic boiling-frog failure of adaptive thresholds. This is the sentence that shows you thought past the happy path.

### Why not a machine-learning model

An EWMA is explainable, has one tunable parameter, needs no training data, and every alert it fires can be justified. An LSTM trained on eight weeks of synthetic traffic is overfitting theatre. The honest framing: *"I have the eval harness — if a model beat EWMA on it, I'd switch, and I'd have the number to prove it."* That is a stronger position than either building or dismissing the model.

Redis key layout, all with TTLs:

```
baseline:edge:{src}:{dst}:cp99   24h   EWMA baseline
baseline:warmup:{src}:{dst}      24h   warmup counter
anomaly:counter:{src}:{dst}      10m   sustained-window counter
anomaly:dedup:{src}:{dst}        10m   Slack suppression
```

Baselines live in Redis rather than Postgres because they are written every cycle per edge, read on the detector hot path, and rebuildable from ClickHouse in one query if lost. Alert *rules* — human-authored, must never be lost — live in Postgres. Choosing storage by durability requirement rather than habit is the point.

## The eval harness

Observability tools all claim they find problems. Prove it with a number, continuously.

Golden-set case families — the last two matter most:

| Family | Cases | Tests |
|---|---|---|
| Latency injection | 60ms, 200ms, 800ms | Sensitivity across magnitudes |
| **Parallel-sibling decoy** | slow but parallel product-svc | Does critical-path logic suppress false blame? |
| Error injection | 10% / 50% 5xx | Error-rate path |
| Saturation | CPU pressure | Indirect degradation |
| Dependency kill | payment DB down | Cascading-failure attribution |
| **Null case** | no fault, 10 minutes | **False-positive rate** |
| Deploy-correlated | fault 30s after rollout | Attribution correctness |

The **null case** is the one that impresses. Anyone can build a detector that fires; proving it stays quiet on healthy traffic is harder and more honest. The **decoy case** is what proves the flagship feature earns its place — a wall-clock detector fails it by design.

Every case is a YAML file with an expected edge, a detection budget, and traps that must not fire:

```yaml
# eval/cases/003_payment_latency_60ms.yaml
name: payment_latency_60ms
injector:
  type: latency
  target: payment-svc
  endpoint: /charge
  delay_ms: 60
  duration_s: 300
load:
  script: load-testing/normal-checkout-flow.js
  rps: 20
expect:
  edge: "order-svc->payment-svc"
  detect_within_s: 480          # see the budget below - NOT 180
  must_not_alert:
    # Siblings only. Ancestors are expected to get slower: an edge's
    # critical-path time includes its callee's whole subtree, so a payment
    # slowdown really does make gateway-svc->order-svc slower, and saying so
    # is correct attribution rather than a false positive.
    - "order-svc->product-svc"
    - "order-svc->user-svc"
```

Scoring, outcome taxonomy, the runner and the CI wiring are in `references/eval-harness.md`. Read it before adding cases or changing thresholds.

Non-negotiables when writing injectors and the runner:

- **Always stop the injector in a `finally` block.** A leaked fault poisons every subsequent case and you will spend an evening confused.
- **Sleep ~60s between cases** so baselines settle; otherwise case N's fault is case N+1's baseline.
- **Warm baselines before the first case** with several minutes of clean load, or the warmup gate makes every case a false negative.
- **Set `detect_within_s` from the real pipeline budget.** Measured, not estimated — the first two attempts at this number were both wrong in the same direction:

  ```
    collector batch                          5s
  + tail sampling decision_wait             10s
  + collector export batch                   5s
  + assembler late-span grace               30s
  + assembler interval                      15s
  + minute-bucket completion                60s   <- routinely omitted
  + detector confirmation (3-in-5 x 60s)   300s   <- often counted as 180s
  = 425s worst case
  ```

  The detector's own confirmation window is already 180s before any pipeline
  time is counted, so a 180s budget can never be met by a correct detector. A
  budget tighter than the pipeline does not measure the detector; it measures
  your patience, and the natural response to the red result it produces is to
  weaken the detector.

- **Name edges against the real topology, not the diagram in your head.** The
  fan-out lives in `order-svc`, so `user-svc` is a child of order-svc and
  `gateway-svc->user-svc` does not exist. A case naming an impossible edge
  injects a real fault, waits for an alert that can never arrive, and scores a
  working detector as a false negative. Declare the graph in one place and
  test every case against it.

## The CI gate

```yaml
# .github/workflows/eval.yml
on:
  pull_request:
    paths: ['platform/src/core/**', 'otel/**', 'eval/**']
  schedule:
    - cron: '0 3 * * *'          # nightly drift check on main
```

Fail the merge when F1 drops more than 0.03 below the committed baseline, or MTTD p95 exceeds 1.2x baseline. Post the scores as a PR comment:

```
detector-eval @ 4f2a9c1
  precision  0.917  (baseline 0.900  +0.017)
  recall     0.833  (baseline 0.850  -0.017)
  f1         0.873  (baseline 0.850  +0.023)
  mttd p50   95s    p95  164s
  PASS
```

**Prove the gate works.** Break the detector deliberately in a draft PR, screenshot the red check, and keep it. A gate nobody has seen fail is a gate nobody believes.

## Why this counts as an MLOps wrapper

| MLOps concept | Equivalent here |
|---|---|
| Model artifact | Detector config (alpha, thresholds, window) — versioned in git |
| Training data | EWMA baselines in Redis, rebuildable from ClickHouse |
| Held-out eval set | 12 golden cases including null and decoy |
| Offline metrics | Precision, recall, F1, MTTD p50/p95 |
| Regression gate | CI fails on F1 drop above tolerance |
| Drift monitoring | Nightly eval on main; alert on score drift |
| Experiment tracking | `eval_runs` table keyed by git SHA plus config JSON |
| Rollback | Revert the config commit; the eval score is the evidence |

## Self-monitoring

The detector dying quietly is the worst failure in the system — everything looks fine and nothing is watched. Emit `tracemap_detector_cycle_total` every cycle and page on `absent_over_time(...[5m])`. Also surface alerts-per-hour, so rising alert volume is visible before humans start ignoring Slack.

## What not to do

- Do not update the baseline on anomalous observations.
- Do not alert without the request-count floor or the sustained window.
- Do not detect on wall-clock p99 as the primary signal — that is the behaviour the project exists to improve on.
- Do not tune `alpha` or thresholds without rerunning the eval set.
- Do not drop the null case to make the scoreboard look better.
- Do not let the LLM copilot (if present) sit on the alert path; alerts must never wait on it.

## Interview soundbites

- *"I treated my detector like a model: a golden set of twelve injected faults including a null case for false positives and a decoy where a parallel sibling is slow but irrelevant, scored on precision, recall and MTTD, with CI blocking merges on regression. When I tuned alpha from 0.3 to 0.2 I didn't argue about it — I had a number."*
- *"The baseline only learns from healthy observations. Fold anomalies in and a slow degradation becomes the new normal, and the detector goes blind."*
- *"My MTTD p95 is measured by injecting known faults, not estimated — and the first budget I wrote was wrong by 2x. The detector's confirmation window alone is three minutes, and about two more of collector batching, sampling decision wait, assembler grace and bucket completion sit in front of it. I moved the budget rather than the detector, because a gate a correct detector cannot pass teaches people to break the detector."*

## Pairing

- `tracemap-critical-path` — the metric the detector evaluates
- `tracemap-deploy-attribution` — enriches alerts with change correlation
- `tracemap-platform-ops` — CI wiring and the burn-rate/self-monitoring alerts
