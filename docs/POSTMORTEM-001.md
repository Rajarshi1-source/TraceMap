# Postmortem 001 — The dependency map would have under-counted every edge, silently

**Status.** Resolved before any code shipped.
**Severity.** Would have been SEV-2 in production: no error, no alert, wrong
numbers on every surface.
**Detected.** 2 Sep 2026, by reading the schema against the engine's merge
semantics — before a line of DDL was executed.
**Author's note.** This postmortem is about a bug that never ran. That is the
point of it.

---

## Summary

`service_edges_1m` and `service_red_1m` declared `request_count` and
`error_count` as plain `UInt64` columns in an `AggregatingMergeTree` table.
That engine merges rows sharing a sorting key and keeps **one arbitrary value**
for any column that is not an aggregate function. The incremental materialized
view fires once per insert block, so a single minute-bucket for a single edge
receives many rows with an identical sorting key.

On merge, all but one `request_count` would have been discarded.

The dependency map would have under-counted traffic on every edge, and
`error_rate` — a ratio of two independently mangled counters — would have been
wrong in an unbounded direction. Nothing would have errored.

## Impact

None. The defect was found during a pre-implementation review of the design
document and corrected in all three source documents before the schema was
applied.

Had it shipped, the blast radius was every number the project exists to
produce:

- **Request counts** on the dependency map, under-reported by roughly the
  insert-block multiple — which grows with load, so the graph would have looked
  most wrong exactly when it mattered most.
- **Error rate**, and with it the red edge colouring. This is the dangerous
  half: with `error_count` collapsing toward zero, a demo would show a healthy
  green graph while payment-svc returned 5xx.
- **The detector's request-count floor.** `min_requests: 100` is the gate that
  stops the detector alerting on a p99 computed over four requests. Feed it an
  under-counted denominator and the floor silently stops protecting anything.

## Detection

Not by a test, a metric or an incident. By cross-checking a column declaration
against the engine's documented merge behaviour while reading the design.

The tell was **inconsistency within a single statement**:

```sql
ENGINE = AggregatingMergeTree
ORDER BY (source_service, target_service, time_bucket)
--
request_count    UInt64,                                 -- plain column
error_count      UInt64,                                 -- plain column
crit_ms_sum      SimpleAggregateFunction(sum, Float64)   -- correct
```

`crit_ms_sum` was already right. The pattern was known to whoever wrote the
statement and simply had not been applied to the two columns next to it. A
schema that is correct in one column and wrong in the adjacent one is not a
knowledge gap; it is an attention gap, and those are found by reading rather
than by thinking.

## Verification

The finding was not left as an argument. It was reproduced on ClickHouse 26.3
— three inserts into one minute-bucket for one edge, one of them an error,
through the real materialized view into the real roll-up table, run twice with
only the column declaration changed:

| Declaration | `request_count` | `error_count` |
|---|---|---|
| Ground truth (`trace_edges`) | 3 | 1 |
| `UInt64` — as originally written | **1** | **0** |
| `SimpleAggregateFunction(sum, UInt64)` | 3 | 1 |

The minimal case is starker: two identical inserts of `10` give `plain=10,
agg=20`.

**`error_count` collapsing to zero is worse than the under-count**, and worse
than predicted before the experiment ran. The reproduction is what turned "this
looks wrong" into "the red edges would never fire", and those are different
sentences with different urgency.

## Root cause

`AggregatingMergeTree` merges rows with the same sorting key and applies merge
logic **only** to `AggregateFunction` and `SimpleAggregateFunction` columns.
Every other column keeps an unspecified surviving value. This is documented and
unsurprising in isolation.

It becomes a defect because of a second fact one layer away: a classic
materialized view fires **per insert block**, not per minute. So the design
guaranteed many rows per sorting key, and the engine guaranteed that all but
one would be thrown away.

Neither fact is obscure. The bug lives in the seam between them, which is
where this class of bug always lives.

## What went right

**The review found it before the compiler would have, because the compiler
never would have.** This is not a bug any type checker, linter or unit test
catches. `UInt64` is a valid column type. The DDL applies cleanly. The queries
run and return numbers. The only signal available before production is someone
reading the declaration and asking what the engine does with it on merge.

**The reproduction was run rather than reasoned.** The predicted symptom was
"under-counts"; the measured symptom included "error rate never fires". The
experiment was cheap — three inserts — and it changed the severity assessment.

**The fix was applied to every copy.** `schema-and-queries.md`,
`IMPLEMENTATION_PLAN_REV3.md` (with a visible correction note, because that
file is the one most likely to be copy-pasted from) and the
`tracemap-clickhouse-analytics` skill, which now carries the rule, the measured
table and an explicit "what not to do".

## What went wrong

**The same defect class had already been caught one layer up.** Rev 3 exists in
part because Rev 2 put a self-join in a materialized view over spans — a bug
about MVs seeing one insert block at a time. F1 is that same fact about insert
blocks, applied to counters instead of joins, and it survived the revision that
was written to catch it.

Knowing a rule is not the same as having applied it everywhere the rule bites.

**Nothing mechanical enforced it.** The rule lived in prose, and prose does not
fail a build.

## Corrective actions

| Action | Status |
|---|---|
| Declare both counters `SimpleAggregateFunction(sum, UInt64)` | Done |
| Reproduce on ClickHouse 26.3 with both declarations | Done — table above |
| Correct all three source documents and the owning skill | Done |
| **Make the rule mechanical:** `scripts/schema-guard.py` asserts the DDL invariants — no plain counters in an `AggregatingMergeTree`, no self-join in an MV (M2), roll-up MVs read one table (M3), no `SummingMergeTree` for TDigest columns (M4), nothing built on `jaeger.*` (M16) | Done — runs in CI on every commit |
| `ReconcilerDivergence` alert: page if the incremental MV and the batch reconciler disagree by more than 2% for 10 minutes | Rule written; the exporter that feeds it is not yet wired — see `docs/LIMITATIONS.md` |

The fourth row is the one that matters. A postmortem whose only output is "we
fixed it and we'll be careful" has not produced anything durable. The guard
means the next person to write a plain counter into an `AggregatingMergeTree`
finds out from CI in twenty seconds rather than from a demo in week eight.

## Why this postmortem and not the other one

The obvious candidate was the inherited materialized-view self-join, which is a
better war story: a real bug, in a real prior revision, caught by review.

F1 is the better postmortem because of *when* it was found. The self-join
demonstrates the debugging habit. F1 demonstrates the review habit — the bug
was found in a design document, by reading a column declaration against an
engine's merge semantics, before any DDL existed to debug.

The strongest version of this story is not "I fixed a hard bug". It is: **I
found a bug in my own design, before it shipped, by not trusting that a
statement which is correct in one column is correct in the next one.**

---

## Related

The habit generalises, and this project has since produced two more findings of
the same shape — a rule known but not applied everywhere it bites:

- **F2** — the assembler must dedupe by `span_id` at the fetch boundary. An
  at-least-once redelivery writes duplicate edges and corrupts every percentile
  downstream. Same silence, same absence of an error.
- **F37** — the eval harness scored a *correct* detector as wrong, twice,
  because the assertion was about a percentile rather than about whether the
  edge mattered. Fixed in the detector, not in the case, once the measurement
  said which one was wrong.

Both are written up in `IMPLEMENTATION_PLAN.md` §2. F37 is the one to read
next: it is the only finding here where the instrument was wrong rather than
the system.
