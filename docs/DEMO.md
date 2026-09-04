# The demo

Three minutes, one claim, and a moment in the middle that the whole project
exists to produce.

This is the script the recorded walkthrough follows, written down because a demo
that lives only in someone's head cannot be rehearsed, handed over, or recovered
when it breaks live. R8 in the risk register says a live demo fails *eventually*
— so the mitigation is a recording plus committed screenshots, and this file is
what both are made from.

> **Recording status.** Not yet recorded. The screenshots in
> [`docs/screenshots/`](screenshots/) are committed and current, and the sequence
> below has been run end to end against a live stack. The video is the remaining
> gap and this file says so rather than linking to something that does not exist.

---

## Before you start

```bash
make demo          # boots the core profile and seeds 60s of traffic
```

Wait for the dashboard to show five services and five edges. **Never open on an
empty graph** — that is why `make demo` seeds rather than just booting, and it is
the single most common way this demo is lost in the first ten seconds.

Have three tabs open and nothing else:

| Tab | |
|---|---|
| 1 | http://localhost:3002 — the dependency map |
| 2 | Slack, or `curl -N localhost:8090/api/stream/alerts` |
| 3 | http://localhost:16686 — Jaeger |

---

## The sequence

### 0:00 — State the claim before showing anything

> "Every service map tells you which edge is slow. In a parallel fan-out that is
> the wrong question, and I can show you why in about a minute."

Show the map in **wall clock** mode. Five services, four edges out of `order-svc`.
Point at `order-svc → product-svc`: real traffic, real latency, red-ish.

### 0:20 — Inject

```bash
curl -sX POST localhost:8001/_internal/fault \
  -H 'Content-Type: application/json' \
  -d '{"delay_ms": 60, "endpoint": "/charge", "ttl_s": 600}'
```

`payment-svc` gains 60 ms. Say what you did and what you expect: *the detector
should name `order-svc → payment-svc` and nothing else.*

**The fault is TTL-bounded and expires on its own** (M23). Nothing here restarts
a container — a restart perturbs latency and then measures the restart.

### 0:30–2:00 — Talk while the pipeline works

Detection takes a measured 120–210 seconds, and that is not dead air; it is the
most interesting thing to explain. Walk the budget:

```
collector batch                     5s
tail-sampling decision_wait        10s
export batch                        5s
assembler grace + interval         45s
detector interval x 3 sustained   180s
                                  ────
                              ~245s worst case
```

> "Three sustained cycles, not one. A single bad minute is weather. The
> confirmation window is most of that budget and it is why the false-positive
> rate is what it is."

Measured MTTD on the full twelve-case set, in CI: **p50 122 s, p95 213 s**.

### ~2:00 — The alert

```
order-svc → payment-svc
critical-path contribution +58ms above baseline (wall-clock +61ms)
sustained 3 cycles · 1204 requests
likely cause: no deploy correlated in the last 15 minutes
```

Two things to point at:

1. **"likely cause", with a number.** Never "caused by" (M21). Correlation is
   not causation and the alert says so in the copy rather than in a footnote.
2. **"no deploy correlated" is a result, not a blank.** It actively points the
   investigation away from a code change. An alert that omits the field when it
   finds nothing has thrown away its most useful sentence.

### 2:20 — **THE MOMENT**

Click through to the map. Flip the toggle to **Critical path**
(`?metric=critical` — it is in the URL, so this view is linkable).

The table re-ranks. `order-svc → product-svc` drops to the bottom and its edge
fades to almost nothing.

> **"product-svc is running at 50 milliseconds of wall clock right now. Its
> contribution to what the user waited for is zero. A normal service map would
> have sent me to go debug it. Mine tells me it is irrelevant."**

Stop talking for a second and let the two numbers sit side by side:

| Edge | Wall p99 | Critical p99 | Blame |
|---|---|---|---|
| `gateway-svc → order-svc` | 133.0 ms | 133.0 ms | 54% |
| `order-svc → payment-svc` | 102.6 ms | 102.6 ms | 46% |
| `order-svc → product-svc` | **50.4 ms** | **0.0 ms** | **0%** |
| `order-svc → user-svc` | 36.8 ms | 0.0 ms | 0% |

That is the whole project in one row.

### 2:40 — Land it in a real trace

Click the edge → a sample slow trace → Jaeger waterfall. The three siblings
overlap on the timeline; `payment-svc` ends last. The picture in the waterfall is
the same fact the table just asserted, which is what makes it credible rather
than a number someone computed.

### 2:55 — Clean up, visibly

```bash
curl -sX DELETE localhost:8001/_internal/fault
```

Say that it was already going to expire on its own. A leaked fault silently
becomes the next run's baseline (M29), so `stop()` is idempotent and called from
a `finally`.

---

## If you have another ninety seconds

Pick **one**. Do not try to show all three.

**The eval set.** `docs/EVAL.md`, and specifically that it currently **fails**:
precision 0.583 against a 0.870 floor, recall 1.000, on the full twelve-case
tier. The interesting sentence is not the score, it is: *the gate is red, the
diagnosis is one structural gap, and the fix is not to move the floor.* A gate
nobody has watched go red is a decoration.

**Error budgets on critical path** (`/api/slo`). The same claim as the toggle,
expressed as an SLO. `order-svc → product-svc` and `order-svc → payment-svc`
carry the *same* 250 ms objective; product burns essentially no budget while
running 700 ms of wall clock, and the wall-clock ratio it *would* have burnt is
exported next to it so the difference is graphable. No other tool does this.

**The chaos experiment** (`docs/CHAOS.md`). ClickHouse killed under live load:
zero span loss across 131 seconds, `/healthz` up while `/readyz` was down — and a
hypothesis that was **falsified**, because the staleness banner survived only 30
seconds of it. Fixed as F40, and the fix re-verified against the same failure.

---

## What to do when it breaks live

It will, eventually. R8.

| Symptom | Say this, then do this |
|---|---|
| Empty graph | "The assembler writes edges about 45 seconds behind traffic." Run `make seed`. |
| Staleness banner | "That is the feature working — it is serving last-good data and telling you how old it is." Then talk about F40 while it recovers. |
| No alert after 4 minutes | Check the fault is actually active: `curl localhost:8001/_internal/fault`. If the stack was booted under 6 minutes ago, the warmup gate is suppressing — say so, it is honest and it is `docs/EVAL.md` failure mode #1. |
| Anything else | Cut to the screenshots and the recording. That is what they are for. |

Never debug live for more than about twenty seconds. The recording exists so the
demo survives the machine.
