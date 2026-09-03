# TraceMap eval harness — golden set, runner, scorer, CI

> **Corrected against the built harness, Phase 7.** This document was written
> before the harness existed, and building it found six defects *in this
> document*. They are marked **CORRECTED** inline, each with the measurement
> that overturned it. The authority is now `eval/`; this is the narrative.
>
> The four worth knowing before reading anything else:
>
> | Was | Is | Why |
> |---|---|---|
> | case 009 edge `gateway-svc->user-svc` | `order-svc->user-svc` | that edge does not exist — the fan-out lives in order-svc (F23) |
> | decoy = product-svc | decoy = user-svc | product carries critical-path blame on 12.4% of traces, so it is not a sibling (F30) |
> | budgets 120–180s | 480s | 425s pipeline worst case; 180s is below the detector's own confirmation floor (F28) |
> | MTTD p95 baseline 180s | 432s | same reason (F28) |

Contents:
1. Directory layout
2. The twelve golden cases
3. Case file schema
4. Injectors
5. Runner
6. Scorer and outcome taxonomy
7. Postgres system of record
8. CI wiring
9. Failure modes when writing evals

---

## 1. Directory layout

```
eval/
├── cases/            # 12 YAML fault scenarios (the golden set)
├── injectors/        # latency, error, saturation, dependency-kill, null
├── runner.py         # inject -> observe -> score -> report
├── scorer.py         # precision / recall / F1 / MTTD
├── check_regression.py
└── baselines/
    └── main.json     # committed scores; CI compares against this
```

---

## 2. The twelve golden cases

| # | Case | Family | Expected edge | Budget |
|---|---|---|---|---|
| 1 | `001_null_baseline` | Null | `none` | 600s quiet |
| 2 | `002_null_traffic_ramp` | Null | `none` | 600s quiet under 2x load ramp |
| 3 | `003_payment_latency_60ms` | Latency | `order-svc->payment-svc` | 480s |
| 4 | `004_payment_latency_200ms` | Latency | `order-svc->payment-svc` | 480s |
| 5 | `005_product_latency_800ms` | Latency | `order-svc->product-svc` | 480s |
| 6 | `006_parallel_decoy` | **Decoy** | `order-svc->payment-svc` | 480s |
| 7 | `007_payment_errors_10pct` | Error | `order-svc->payment-svc` | 480s |
| 8 | `008_payment_errors_50pct` | Error | `order-svc->payment-svc` | 480s |
| 9 | `009_user_cpu_saturation` | Saturation | `order-svc->user-svc` **CORRECTED** | 480s |
| 10 | `010_payment_db_down` | Dependency kill | `order-svc->payment-svc` | 480s |
| 11 | `011_deploy_correlated` | Deploy | `order-svc->payment-svc` | 480s |
| 12 | `012_gateway_latency_cascade` | Cascade | `gateway-svc->order-svc` | 480s |

Case 6 is the one that justifies the whole critical-path feature: a sibling is
made slow *in parallel* with a slightly slower payment-svc. A wall-clock
detector alerts on both and scores a false positive. Only critical-path
attribution gets it right.

**CORRECTED — the decoy is user-svc, not product-svc.** Measured over ~11,000
traces of healthy load:

| edge | critical on | crit p99 |
|---|---|---|
| `order-svc->payment-svc` | 87.3% | 157ms |
| `order-svc->product-svc` | **12.4%** | **49.3ms** |
| `order-svc->user-svc` | 0.3% | **0.0ms** |

product-svc has an N+1 query path that fires on large carts, so on those traces
it genuinely *is* the slowest leg and genuinely does carry blame. A service that
already carries critical-path blame cannot be a decoy **at any delay** — the
injected time lands on the critical path in full. Measured live: +120ms on
product moved its critical p99 from 49.3 to 168.8 and tripped the trap, scoring
a correct detector as a false positive.

The trap in the original design is subtle and general: it compared *mean*
service latencies, where 45ms against 90ms looks like a comfortable margin. The
critical path is a question about the **tail**. product's mean says sibling; its
p99 says contender.

Cases 1 and 2 are the false-positive measurement. Case 2 matters because a
traffic ramp changes latency distributions legitimately — a detector that
cannot tell "more load" from "regression" will fire here.

Case 12 checks attribution under cascade: gateway slowness propagates
downstream, and the detector should blame the origin edge, not every edge.

---

## 3. Case file schema

```yaml
name: payment_latency_60ms          # unique, matches filename stem
description: >
  Free text. Explain what the case proves, not just what it does.
injector:
  type: latency                     # latency | error | saturation | dep_kill | null
  target: payment-svc
  endpoint: /charge                 # optional, injector-specific
  delay_ms: 60
  duration_s: 300
load:
  script: load-testing/normal-checkout-flow.js
  rps: 20
expect:
  edge: "order-svc->payment-svc"    # or "none" for null cases
  detect_within_s: 180
  must_not_alert:                   # false-positive traps
    - "order-svc->product-svc"
    - "gateway-svc->user-svc"
```

`detect_within_s` must be derived from the real pipeline budget, not chosen
optimistically. **CORRECTED** — the original derivation omitted minute-bucket
completion and counted the sustained window as 3 cycles rather than the 5 it
spans:

```
  collector batch                          5s
+ tail sampling decision_wait             10s
+ collector export batch                   5s
+ assembler late-span grace               30s
+ assembler interval                      15s
+ minute-bucket completion                60s   <- omitted
+ detector confirmation (3-in-5 x 60s)   300s   <- was counted as 180s
= 425s worst case
```

Phase 6 measured 375s injection-to-alert against a documented ~245s. Every
fault case therefore gets 480s.

The consequence worth stating plainly: **a 180s budget is unreachable by a
correct detector**, because the detector's own confirmation window is already
180s before any pipeline time is counted at all. A gate nothing can pass goes
red on a working detector, and the natural response to a red gate is to weaken
the thing being gated.

If a budget is tighter than that, the case measures your patience rather than
the detector. Either widen the budget or reduce `sustained_cycles` for the
eval profile — and if you do the latter, note it, because it changes the thing
being measured.

---

## 4. Injectors

Every injector implements the same tiny interface, so the runner never knows
what kind of fault it is running:

```python
# eval/injectors/base.py
from typing import Protocol

class Injector(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...      # MUST be idempotent
```

```python
# eval/injectors/latency.py  (shape)
class LatencyInjector:
    """Toggles a fault-injection env var / config flag on the target service.

    The services read TRACEMAP_FAULT_DELAY_MS at request time so no restart is
    needed — a restart would itself perturb latency and confound the measurement.
    """
    async def start(self) -> None:
        await self._set_flag(delay_ms=self.delay_ms, endpoint=self.endpoint)

    async def stop(self) -> None:
        await self._set_flag(delay_ms=0, endpoint=self.endpoint)
```

Injector rules:

- **Idempotent `stop()`** — it gets called from `finally` and possibly twice.
- **No restarts** to apply a fault; a pod restart changes latency on its own
  and pollutes the measurement.
- **Bounded by `duration_s`** as a backstop, so a crashed runner still heals.
- **`dep_kill` uses a NetworkPolicy or a paused container**, not `kubectl delete`
  on a database with a PVC.
- **The null injector is a real injector** that does nothing — keeps the runner
  path identical across cases.

---

## 5. Runner

```python
# eval/runner.py
POLL_INTERVAL_S = 10

async def run_case(case: dict, api, pg) -> dict:
    name          = case["name"]
    expected_edge = case["expect"]["edge"]
    budget_s      = case["expect"]["detect_within_s"]
    traps         = set(case["expect"].get("must_not_alert", []))

    injector = get_injector(case["injector"])
    t0 = time.monotonic()
    await injector.start()
    try:
        detected_edge, detected_at, tripped_trap = None, None, None
        while time.monotonic() - t0 < budget_s:
            await asyncio.sleep(POLL_INTERVAL_S)
            for a in await api.anomalies_since(t0):
                edge = f"{a['src_service']}->{a['dst_service']}"
                if edge in traps and tripped_trap is None:
                    tripped_trap = edge
                if edge == expected_edge and detected_edge is None:
                    detected_edge = edge
                    detected_at = time.monotonic() - t0
            # CORRECTED. This stops only when BOTH have happened, so a
            # clean case runs its whole budget while a case whose trap fires
            # late never sees it -- the trap gets a shorter window than the
            # thing it is meant to catch, and precision comes out flattered.
            # The built runner starts a bounded 90s trap-grace window on
            # detection instead, so both get a comparable look.
            if detected_edge and tripped_trap:
                break
    finally:
        await injector.stop()          # ALWAYS clean up, even on failure

    if tripped_trap:
        outcome = "false_positive"
    elif detected_edge:
        outcome = "true_positive"
    elif expected_edge == "none":
        outcome = "true_positive"      # null case: correctly stayed silent
    else:
        outcome = "false_negative"

    return {"case_name": name, "expected_edge": expected_edge,
            "detected_edge": detected_edge or tripped_trap,
            "detected_at_s": detected_at, "outcome": outcome}


async def main(git_sha: str, api, pg) -> bool:
    run_id = uuid.uuid4()
    cases = [yaml.safe_load(p.read_text())
             for p in sorted(Path("eval/cases").glob("*.yaml"))]

    results = []
    for case in cases:
        results.append(await run_case(case, api, pg))
        await asyncio.sleep(60)        # let baselines settle between cases

    scores = score_run(results)
    await pg.insert_eval_run(run_id, git_sha, scores, results)
    print(scores.as_table())
    return scores.passed
```

Cases run **sequentially, not in parallel**. Two concurrent faults make
attribution ambiguous and the result unusable — the same reason ChaosProof runs
experiments sequentially. That is a documented scaling decision, not a
limitation to apologise for.

---

## 6. Scorer and outcome taxonomy

Four outcomes, and the distinction between the last two matters:

| Outcome | Meaning |
|---|---|
| `true_positive` | Correct edge detected in budget, or null case stayed silent |
| `false_negative` | Fault present, never detected (or not within budget) |
| `false_positive` | A trap edge alerted — blamed something innocent |
| `wrong_edge` | Something alerted, but not the expected or trap edge |

`wrong_edge` counts against precision alongside `false_positive`, because an
alert pointing at the wrong service costs on-call time the same way.

```python
# eval/scorer.py
BASELINE  = {"f1": 0.85, "precision": 0.90, "mttd_p95_s": 360.0}  # CORRECTED from 180
TOLERANCE = 0.03

def score_run(results: list[dict]) -> Scores:
    tp = sum(r["outcome"] == "true_positive" for r in results)
    fp = sum(r["outcome"] in ("false_positive", "wrong_edge") for r in results)
    fn = sum(r["outcome"] == "false_negative" for r in results)

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall    = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    lat = sorted(r["detected_at_s"] for r in results if r["detected_at_s"])
    p50 = median(lat) if lat else float("inf")
    # CORRECTED. This raises IndexError whenever len * 0.95 is a whole
    # number -- n = 20 and n = 40 among them. The built scorer uses
    # nearest-rank with math.ceil (NOT round, which is banker's rounding in
    # Python and makes the answer depend on the parity of the case count).
    p95 = lat[int(len(lat) * 0.95)] if lat else float("inf")

    passed = (f1 >= BASELINE["f1"] - TOLERANCE
              and precision >= BASELINE["precision"] - TOLERANCE
              and p95 <= BASELINE["mttd_p95_s"] * 1.2)
    return Scores(precision, recall, f1, p50, p95, passed)
```

MTTD is computed only over detected cases — null cases contribute no latency.
Say that out loud if asked, because otherwise the number looks suspiciously good.

---

## 7. Postgres system of record

```sql
CREATE TABLE eval_runs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    git_sha       TEXT NOT NULL,
    detector_cfg  JSONB NOT NULL,       -- the "model artifact"
    precision     NUMERIC,
    recall        NUMERIC,
    f1            NUMERIC,
    mttd_p50_s    NUMERIC,
    mttd_p95_s    NUMERIC,
    passed        BOOLEAN
);

CREATE TABLE eval_cases (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id         UUID REFERENCES eval_runs(id) ON DELETE CASCADE,
    case_name      TEXT NOT NULL,
    expected_edge  TEXT NOT NULL,
    detected_edge  TEXT,
    detected_at_s  NUMERIC,
    outcome        TEXT NOT NULL CHECK (outcome IN
                   ('true_positive','false_negative','false_positive','wrong_edge'))
);
```

Storing `detector_cfg` as JSONB per run is what makes the history useful: any
score change can be traced to a config change. If the LLM copilot is present,
record the prompt hash here too.

---

## 8. CI wiring

```yaml
# .github/workflows/eval.yml
name: detector-eval
on:
  pull_request:
    paths:
      - 'platform/src/core/**'
      - 'otel/**'
      - 'eval/**'
  schedule:
    - cron: '0 3 * * *'

jobs:
  eval:
    runs-on: ubuntu-latest
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@v4
      - name: Boot the full stack
        run: docker compose -f docker-compose.yml -f docker-compose.eval.yml up -d --wait
      - name: Warm baselines
        run: k6 run --duration 5m --vus 10 load-testing/normal-checkout-flow.js
      - name: Run the golden set
        run: python -m eval.runner --git-sha ${{ github.sha }} --output eval-report.md
      - name: Comment scores on the PR
        uses: marocchino/sticky-pull-request-comment@v2
        with: { path: eval-report.md }
      - name: Fail on regression
        run: python -m eval.check_regression --baseline eval/baselines/main.json
```

The 45-minute timeout is real: twelve cases at 2-5 minutes each plus 60s
settling plus a 5-minute warmup. If that is too slow for every PR, split the
set — a 4-case smoke tier on PRs, the full twelve nightly — and say which tier
produced any score you quote.

---

## 9. Failure modes when writing evals

| Symptom | Cause |
|---|---|
| Every case is a false negative | Baselines never warmed; the warmup gate suppressed everything |
| Case N+1 always fails | Previous injector never stopped, or no settling gap |
| Scores swing between runs | Load too low for the request-count floor; raise rps |
| Null case fails intermittently | `alpha` too high — baseline chases noise |
| Decoy case passes with a wall-clock detector | Decoy is mis-built; the parallel service must not be the slowest |
| MTTD suspiciously low | Budget started before load stabilised; measure from steady state |
| Everything passes on the first try | Suspect the harness before celebrating — break the detector deliberately and confirm it goes red |
| Error cases never detect | The detector may have no error path at all. Latency percentiles cannot see a fast 5xx: an injected error returns *faster* than a success, so critical-path p99 does not move (F32) |
| A decoy case fails against a detector that looks correct | The decoy service probably carries critical-path blame already. Check its critical p99 on healthy traffic; if it is not ~0 it cannot be a decoy (F30) |
| `type: null` behaves oddly | YAML 1.1 parses bare `null` as None, so the injector type silently becomes absent. Quote it (F24) |
| Cases sharing an edge fail in sequence | Alert-suppression state was not reset between them; the dedup TTL outlives the settling gap (F31) |
| Runs record no history on Windows | psycopg cannot run async on the Proactor event loop, and the error says "postgres unreachable" (F33). A native Postgres on 5432 will also shadow the container (F34) |

That last row is the important one. An eval suite that has never failed is not
evidence of a working detector; it is evidence of an untested harness.
