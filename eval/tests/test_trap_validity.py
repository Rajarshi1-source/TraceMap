"""F42 -- a scoreboard has to be able to say whether its own traps were valid.

Two full twelve-case runs were scored against a decoy leg that was measurably
carrying critical-path blame. Finding that out required reading a CI log line by
line: the run artifact itself said nothing, so nothing about the score revealed
that two of its six failures were the harness rather than the detector.

`invalid_decoy_traps` is the check, extracted from `main_async` so it can be
tested without a stack -- the bug it replaces survived two runs precisely
because the equivalent logic was buried where nothing exercised it. The runner
records its output in `eval-run.json` as `traps_valid` and `invalid_traps`
alongside the scores, so a number and the reason it cannot be trusted travel
together.

A number is not a measurement unless you can tell afterwards what it was
measuring.
"""

from __future__ import annotations

import pathlib
import re

from eval.runner import invalid_decoy_traps
from eval.scorer import OUTCOMES
from eval.topology import CRIT_P99_BASELINE_MS, THRESHOLD_MS

ROOT = pathlib.Path(__file__).resolve().parents[2]

DECOYS = [e for e, ms in CRIT_P99_BASELINE_MS.items() if ms < THRESHOLD_MS]
NON_DECOYS = [e for e, ms in CRIT_P99_BASELINE_MS.items() if ms >= THRESHOLD_MS]


def test_the_declaration_actually_contains_a_decoy():
    """Guards the guard: with no declared decoy every test below is vacuous."""
    assert DECOYS, "eval/topology.py declares no zero-blame sibling"


def test_a_quiet_decoy_is_valid():
    measured = {e: 0.0 for e in CRIT_P99_BASELINE_MS}
    assert invalid_decoy_traps(measured) == []


def test_a_decoy_carrying_blame_invalidates_the_traps():
    """The exact CI condition: 56.6ms against a declared 0.0."""
    measured = {e: 0.0 for e in CRIT_P99_BASELINE_MS}
    measured[DECOYS[0]] = 56.6

    bad = invalid_decoy_traps(measured)

    assert len(bad) == 1
    assert DECOYS[0] in bad[0]
    assert "56.6" in bad[0]
    # The message has to name what was declared as well as what was seen, or a
    # reader cannot tell whether the declaration or the system moved.
    assert "declared" in bad[0]


def test_the_threshold_is_inclusive():
    """At exactly THRESHOLD_MS the leg can trip the detector, so it is invalid.

    An off-by-one here is silent: the trap stays declared valid at precisely the
    value where the detector starts firing on it.
    """
    measured = {e: 0.0 for e in CRIT_P99_BASELINE_MS}
    measured[DECOYS[0]] = THRESHOLD_MS
    assert invalid_decoy_traps(measured)

    measured[DECOYS[0]] = THRESHOLD_MS - 0.1
    assert invalid_decoy_traps(measured) == []


def test_a_leg_that_was_never_a_decoy_is_not_flagged():
    """order-svc->payment-svc is SUPPOSED to carry blame.

    Flagging it would make the check fire on every healthy run, and a check that
    always fires gets deleted.
    """
    if not NON_DECOYS:
        return
    measured = {e: 0.0 for e in CRIT_P99_BASELINE_MS}
    measured[NON_DECOYS[0]] = 900.0
    assert invalid_decoy_traps(measured) == []


def test_a_missing_edge_is_not_treated_as_valid_or_invalid():
    """An absent edge means no traffic, which is the preflight's problem.

    Inventing a verdict from a missing measurement would either mask a real
    invalidation or manufacture a false one.
    """
    assert invalid_decoy_traps({}) == []


def test_every_invalid_leg_is_reported_not_just_the_first():
    """A run stopped by one bad leg only to trip over another wastes 90 minutes."""
    if len(DECOYS) < 2:
        return
    measured = {e: 0.0 for e in CRIT_P99_BASELINE_MS}
    for e in DECOYS[:2]:
        measured[e] = 120.0
    assert len(invalid_decoy_traps(measured)) == 2


# ---------------------------------------------------------------------------
# F45 -- the outcome vocabulary is declared twice and must agree.
# ---------------------------------------------------------------------------

def test_the_sql_check_constraint_matches_the_python_outcomes():
    """F44 added a fifth outcome in Python and not in the schema.

    The CHECK constraint caught it -- in CI, at the end of a 105-minute run,
    with `psycopg.errors.CheckViolation` and a lost scoreboard. The constraint
    was right to catch it; the comment above it says as much, that a new
    outcome "would have to be argued for in a migration rather than slipped
    into a dict". That is exactly what had happened.

    So the vocabulary lives in two places by necessity -- one is a Python tuple
    the scorer branches on, the other is a database constraint that has to
    reject a typo written by some future caller. This test is the price of
    that, and it costs milliseconds against a 105-minute feedback loop.
    """
    sql = (ROOT / "platform" / "db" / "002_eval.sql").read_text(encoding="utf-8")

    # The live ADD CONSTRAINT at the end of the file is authoritative: it runs
    # after the CREATE TABLE and converges an existing database onto the same
    # set, so it is the one a deployed instance actually ends up with.
    m = re.search(r"ADD CONSTRAINT eval_cases_outcome_check\s*"
                  r"CHECK \(outcome IN \((.*?)\)\)", sql, re.DOTALL)
    assert m, "no idempotent ADD CONSTRAINT for eval_cases_outcome_check"

    in_sql = set(re.findall(r"'([a-z_]+)'", m.group(1)))
    assert in_sql == set(OUTCOMES), (
        f"eval/scorer.OUTCOMES and the SQL CHECK disagree.\n"
        f"  only in Python: {sorted(set(OUTCOMES) - in_sql)}\n"
        f"  only in SQL:    {sorted(in_sql - set(OUTCOMES))}\n"
        "A value the scorer emits and the constraint rejects fails at the very "
        "END of a run, after every case has already been paid for."
    )


def test_the_inline_create_table_constraint_agrees_too():
    """A fresh database gets the inline constraint and never runs a migration
    that would widen it, so the two have to start out identical."""
    sql = (ROOT / "platform" / "db" / "002_eval.sql").read_text(encoding="utf-8")
    m = re.search(r"outcome\s+TEXT NOT NULL CHECK \(outcome IN\s*\((.*?)\)\)",
                  sql, re.DOTALL)
    assert m, "no inline CHECK on eval_cases.outcome"
    assert set(re.findall(r"'([a-z_]+)'", m.group(1))) == set(OUTCOMES)


def test_the_migration_is_idempotent():
    """This file is executed in full on every runner start.

    Without DROP IF EXISTS the second run fails with "constraint already
    exists", which would turn a fix into a different outage.
    """
    sql = (ROOT / "platform" / "db" / "002_eval.sql").read_text(encoding="utf-8")
    assert "DROP CONSTRAINT IF EXISTS eval_cases_outcome_check" in sql
    assert sql.index("DROP CONSTRAINT IF EXISTS") < sql.index("ADD CONSTRAINT")
