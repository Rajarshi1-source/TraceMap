# Chaos experiment 001 — killing ClickHouse under live load

**Run:** 4 Sep 2026, 07:26–07:31 UTC · **Outage:** 131 s · **Load:** ~372 spans/s
sustained throughout · **Raw timeline:** `docs/chaos-001.json`

One experiment, run once, on the real stack. It confirmed two claims this
project makes and **falsified a third**, which is the only reason it was worth
running.

> **The finding.** The staleness banner — the feature that exists so degraded
> data beats no data — was visible for **30 seconds of a 131-second outage**.
> After that the graph API returned HTTP 500. ClickHouse's own restart takes 31
> seconds, so the banner could never have covered a real database restart. The
> one scenario it was designed for is the one it could not survive.

---

## 1. Hypotheses

Chosen because each one is a claim made elsewhere in this repository, and a
claim nobody has tried to break is a hope.

| | Claim | Where it is asserted |
|---|---|---|
| **H1** | The map keeps serving last-good data, flagged stale, rather than erroring | M26, `redis_cache.py` |
| **H2** | Spans produced *during* the outage are not lost — the collector's persistent queue replays them | M15, `otel/` |
| **H3** | `/healthz` survives a dead dependency; `/readyz` does not | M17, `routes.py` |

## 2. Method

```
docker stop  tracemap-clickhouse-1     # not a pause, not a network partition
                                       # -- the failure people actually get
```

Load ran throughout: the checkout flow at its normal rate, ~372 spans/s
measured. `/api/graph`, `/readyz` and `/healthz` were polled every 10 s. After
120 s the container was started again and polling continued until the graph went
fresh. Span counts were taken before, and after a 90-second drain window, with
the outage bounded by ClickHouse's own clock so the replayed rows could be
counted by their own timestamps.

Nothing was pre-warmed and nothing was restarted to help it along.

## 3. Timeline

| t (s) | Event | `/api/graph` | `stale` | age | `/readyz` | `/healthz` |
|---|---|---|---|---|---|---|
| 0 | steady state | 200 | false | 4–10 s | 200 | 200 |
| **17** | **ClickHouse killed** | | | | | |
| 25 | | 200 | false | 8.9 s | **503** | 200 |
| 39 | | 200 | false | 23.0 s | 503 | 200 |
| 53 | banner appears | 200 | **true** | 36.9 s | 503 | 200 |
| 67 | | 200 | true | 50.8 s | 503 | 200 |
| **85** | **cache expires** | **500** | — | — | 503 | 200 |
| 103–138 | | 500 | — | — | 503 | 200 |
| **148** | **`docker start`** | 500 | — | — | 503 | 200 |
| 179 | ClickHouse answers | | | | | |
| **184** | graph fresh again | 200 | false | **0.3 s** | 200 | 200 |

## 4. Results

### H3 — confirmed, cleanly

`/healthz` returned 200 for every single probe across the entire outage.
`/readyz` returned 503 within 8 seconds of the kill and stayed there until
ClickHouse answered.

This is M17 working exactly as intended, and the reason it matters is what
*would* have happened otherwise: had the liveness probe checked ClickHouse, an
orchestrator would have killed and rescheduled every replica of every service
that depends on it — turning a degradation into an outage, and adding cold
starts to a recovery that was already in progress. One slow database must not
fail liveness fleet-wide.

### H2 — confirmed, and it is the strongest result here

| | |
|---|---|
| Spans before | 6,618,766 |
| Spans after (post-drain) | 6,747,463 |
| Rows whose **own timestamp** falls inside the 120 s outage window | **52,840** |
| Expected at the measured rate (371.7/s × 120 s) | 44,608 |

Had the pipeline dropped what it could not write, that window would have been
close to empty — nothing can be inserted while the database is down, and a
dropped span never arrives late. Instead the window is **fully populated**, at
1.18× the steady-state rate, the excess being ordinary rate variance across the
sample.

**Zero span loss across a 131-second database outage.** The collector's
`file_storage` persistent queue held them on disk and drained them on recovery.

This is the claim most worth testing because its failure is invisible: dropped
spans leave no trace, which is precisely what makes them dangerous. A tracing
pipeline that quietly loses the spans covering an incident is worse than one
that is obviously down, because the resulting graph is confidently wrong.

### H1 — **falsified**

The banner worked. It appeared at 36.9 s of age, against a `STALE_AFTER_S` of
30 — correct to the second.

Then, 68 seconds after the kill, `/api/graph` returned **HTTP 500** and kept
returning it for the remaining 100 seconds of the outage, including the whole
restart.

The arithmetic made it inevitable, and it was sitting in plain sight in two
constants that had never been read next to each other:

```
GRAPH_TTL_S      60    graph:current expires 60s after the last good build
STALE_AFTER_S    30    the banner appears at 30s of age
                 ──
                 30s   the entire life of the staleness banner
```

Past 60 seconds the key is gone, `/api/graph` falls through to a live ClickHouse
query, and that query had nothing to talk to. The exception escaped as a 500.

**Measured in the same experiment: ClickHouse took 31 seconds to answer after
`docker start`,** on a machine where the whole stack was already warm and the
page cache hot. Any restart-shaped outage — the most ordinary kind there is —
outlives a 30-second banner by construction.

Two features, each individually correct, that had never been considered
together. This is the class of defect a chaos experiment exists to find, and it
is not one any unit test would have reached: every component behaved to
specification.

## 5. The fix

Tracked as **F40**.

**A second cached copy with a longer life.** `graph:lastgood` holds the same
payload for an hour under its own key, written in the same pipeline as
`graph:current` so the two cannot disagree about which is newer.

**A fallback chain that never 500s.**

```
graph:current   <60s old        200, fresh
     ↓ miss
live query      cold start      200, fresh
     ↓ raises
graph:lastgood  up to 1h old    200, stale:true, degraded:true, real age
     ↓ absent
                                503, naming which of the three failed
```

Last-good is returned with **HTTP 200**, deliberately. M26 says never display
stale data *without the banner* — not never display it. A 503 would be more
literal and less useful: the dashboard renders nothing, and an operator
mid-incident loses the last known shape of their system at the moment they most
want it. Age and `degraded` ride in the payload so nothing can be mistaken for
current.

> **The fix needed a second pass, and the reason is worth reading.** The first
> version wrote both keys in one unconditional pipeline. But an empty graph is a
> *truthful* statement about now and a *useless* fallback — and written to both
> keys it silently destroys the thing this whole fix exists to provide.
>
> The failure lands exactly when the fallback matters most: ClickHouse comes
> back, the roll-up views have not caught up, the builder's next cycle
> **succeeds** and returns zero edges, and that empty result overwrites the last
> real graph. The operator is then shown an empty map — indistinguishable from a
> system with no traffic — while trying to find out what broke.
>
> `graph:current` is still written unconditionally. `graph:lastgood` is written
> only when the graph has edges. **A last-known-good that a degenerate result
> can overwrite is not a last-known-good.**
>
> This was found by re-auditing the fix, not by a second failure — which is the
> same lesson one level up: F40 itself was two behaviours that were individually
> correct and wrong together.

503 is reserved for genuinely having nothing, and its body says which layer
failed rather than leaving the dashboard to render a generic error.

`platform/tests/test_graph_degradation.py` pins the corrected chain. One of its
assertions is the outage state at t+68 s directly; another asserts that the
last-good TTL is more than ten times the *measured* 31-second ClickHouse
restart, so the fix cannot silently become cosmetic if someone tunes the
constant later.

### Verified against the same failure

The fix was not accepted on the strength of its unit tests. ClickHouse was
stopped again, under the same conditions, and the endpoint polled past the cliff
that used to break it:

| t | Status | Source | `stale` | `degraded` | Age |
|---|---|---|---|---|---|
| +15 s | 200 | cache | false | — | 21.5 s |
| +30 s | 200 | cache | **true** | — | 36.5 s |
| +45 s | 200 | cache | true | — | 51.5 s |
| **+60 s** | **200** | **last-good** | **true** | **true** | 70.4 s |
| +98 s | 200 | last-good | true | true | 108.2 s |
| +154 s | 200 | last-good | true | true | 164.8 s |

Every row past +60 s was an HTTP 500 before the fix. The age keeps climbing and
is reported honestly rather than reset, so a viewer can see exactly how far
behind the data is.

And the same state as a viewer sees it, 101 seconds into an outage:

![degraded, serving last-good](screenshots/03-degraded-last-good.png)

Five services, five edges, real numbers, and a banner that says how old they
are. Before F40 this page was rendering an error.

## 6. Recovery, measured

| | |
|---|---|
| `docker start` → ClickHouse answering `SELECT 1` | **30.9 s** |
| `docker start` → graph fresh again | **31.0 s** |
| Platform's own contribution to recovery | **~0.1 s** |

The platform adds essentially nothing to the recovery time; the database's boot
is the entire budget. The graph-builder was already looping and failing every 10
seconds, so the first cycle after ClickHouse came back succeeded immediately and
the cache was repopulated within one interval.

That is a good result and it is also a limit worth naming: **recovery time here
is ClickHouse's start-up time**, and nothing in this project can improve it. On
a larger database with more parts to load, it will be longer, and the last-good
window is what has to cover it. One hour is a wide margin over 31 seconds — but
it is a margin over *this* measurement, not a universal claim.

## 7. What this did not test

Stated so nobody quotes this document for more than it did.

- **A partial failure.** A stopped container is a clean, fast, unambiguous
  failure. A ClickHouse that is up and answering slowly is harder and more
  common, and this experiment says nothing about it.
- **Redis failing.** Redis is the *fallback*, so its loss removes the mitigation
  rather than triggering it. That is a different and probably worse experiment.
- **A longer outage than the queue can absorb.** The persistent queue is
  bounded. At 372 spans/s it held two minutes comfortably; the point at which it
  starts shedding was not found, and `SpanIngestionLoss` is the alert that
  should catch it in the wild. **Finding that limit is the obvious next
  experiment.**
- **Whether anything is lost at the sampler.** Span *counts* were checked. Trace
  completeness across the outage boundary — a trace whose spans straddle the
  gap — was not.

## 8. Reproducing it

```bash
make up && make seed        # get the stack warm and busy
docker stop tracemap-clickhouse-1
watch -n5 'curl -s localhost:8090/api/graph?window=5 | jq "{stale,ageSeconds,source}"'
docker start tracemap-clickhouse-1
```

You should now see `"source": "last-good"` through the middle of the outage
instead of a 500. If you see the 500, you are running a build from before F40.
