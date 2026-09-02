# ADR 0007 — Skill archives are build artifacts, not source

- **Status:** Accepted
- **Date:** 2026-09-02 (Phase 0)
- **Deciders:** Rajarshi Basu

> Numbering note: ADRs 0001–0006 are reserved by `IMPLEMENTATION_PLAN.md` for
> decisions taken in Phases 2–7. This one was taken in Phase 0 and takes the next
> free number rather than renumbering the others across three documents.

## Context

The project's rules live in eight `.skill` files under `.claude/skills/`. Each is
a zip containing `SKILL.md` and, for three of them, a copy of a reference
document that also exists at the repository root:

```
schema-and-queries.md   also inside tracemap-clickhouse-analytics.skill
eval-harness.md         also inside tracemap-anomaly-eval.skill
starter-code.md         also inside tracemap-demo-services.skill
```

Two copies of the same document, with nothing enforcing agreement.

The risk stopped being hypothetical during the design audit. Fixing defect F1
(roll-up counters declared as plain `UInt64` in an `AggregatingMergeTree`)
required editing `schema-and-queries.md` at the root, then *remembering* to copy
it into the skill directory and repack the archive. Nothing would have failed if
that second step had been skipped — the root document would have said one thing
and the file an agent actually loads would have said another.

A second problem surfaced in the same change. Repacking all eight archives made
all eight show as modified in `git status`, even though only two had changed in
substance; the other six differed solely in zip timestamps. Reviewing that diff
was impossible, so the six had to be identified by extracting and hashing every
member.

## Decision

**The root documents and `skills/<name>/SKILL.md` are the source of truth.
`.claude/skills/*.skill` are generated build artifacts.**

```
skills/<name>/SKILL.md      source, diffable, reviewed
schema-and-queries.md       source, single copy
eval-harness.md             source, single copy
starter-code.md             source, single copy
        |  make skills   (scripts/build-skills.py)
        v
.claude/skills/<name>.skill build artifact
```

`scripts/build-skills.py` copies each reference document in at pack time and
writes archives **deterministically** — fixed `date_time`, fixed compression,
sorted entries — so identical content always produces byte-identical output.

`make skills-check` rebuilds and fails if any archive differs from what is
committed. This runs in CI, which makes a stale archive impossible to merge.

## Alternatives considered

**Make the skill copies authoritative and reduce the root files to pointers.**
This was the initial recommendation and it was wrong. A zip does not diff in a
pull request, cannot be commented on line by line, and hides its own drift.
Source of truth belongs in text that git can show.

**Leave both copies and sync by hand.** This is the status quo whose failure
prompted the ADR. An invariant that depends on remembering is not an invariant.

**Stop committing the archives; build them on demand.** Rejected because the
archives must be present for an agent to load the skills from a fresh clone,
with no build step run first.

## Consequences

**Good.** One authoritative copy of every document. Skill changes are reviewable
as text. Determinism removes the binary churn, so a diff touching the archives
now means content genuinely changed. CI enforces it instead of a person.

**Bad.** Generated files are committed, which is mild duplication — accepted for
the same reason a lockfile is committed. Editing a `.skill` directly is now
wrong, and `make skills` silently reverts it; the README says so.

**Neutral.** The transition repacked all eight archives once. Content was
verified byte-identical member-by-member before and after, so the commit changes
packing only.
