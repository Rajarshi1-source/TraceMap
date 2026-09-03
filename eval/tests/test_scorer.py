"""Scorer arithmetic.

Small, boring, and worth having: every number quoted about this project comes
out of these twenty lines, so an off-by-one here is an off-by-one in the claim.
"""

from __future__ import annotations

import math

import pytest

from eval.scorer import (
    FALSE_NEGATIVE,
    FALSE_POSITIVE,
    TRUE_POSITIVE,
    WRONG_EDGE,
    percentile,
    score_run,
)


def r(outcome: str, at: float | None = None) -> dict:
    return {"case_name": "c", "expected_edge": "a->b", "detected_edge": "a->b",
            "detected_at_s": at, "outcome": outcome, "detail": ""}


@pytest.mark.parametrize("n", range(1, 41))
def test_percentile_never_indexes_off_the_end(n):
    """The reference implementation used `lat[int(len(lat) * 0.95)]`.

    That is in range for most lengths and raises IndexError whenever
    len * 0.95 is a whole number -- n = 20 and n = 40 among them. Twelve cases
    with ten detections is uncomfortably close to those, and a crash in the
    scorer reads as a broken harness at exactly the moment the number matters.

    The property asserted is nearest-rank: the result is one of the observed
    values, and it sits in the slowest tenth. Asserting "equals the maximum"
    would be wrong for n >= 20 and would quietly re-introduce the bug.
    """
    values = [float(i) for i in range(n)]
    got = percentile(values, 0.95)
    assert got in values
    assert got >= values[max(0, math.ceil(0.9 * n) - 1)]
    assert got <= values[-1]


def test_percentile_of_nothing_is_infinite_not_zero():
    """A run that detected nothing has an unmeasured MTTD, not a fast one.

    Returning 0.0 here would make a completely deaf detector post the best
    MTTD on the board.
    """
    assert percentile([], 0.95) == math.inf


def test_precision_recall_f1():
    results = [r(TRUE_POSITIVE, 100.0)] * 8 + [r(FALSE_POSITIVE)] * 2 \
        + [r(FALSE_NEGATIVE)] * 2
    s = score_run(results)
    assert s.precision == pytest.approx(8 / 10)
    assert s.recall == pytest.approx(8 / 10)
    assert s.f1 == pytest.approx(0.8)


def test_wrong_edge_counts_against_precision():
    a = score_run([r(TRUE_POSITIVE, 100.0)] * 9 + [r(FALSE_POSITIVE)])
    b = score_run([r(TRUE_POSITIVE, 100.0)] * 9 + [r(WRONG_EDGE)])
    assert a.precision == b.precision
    assert a.fp == b.fp == 1


def test_mttd_ignores_cases_that_never_detected():
    """Null cases contribute no latency. Say it out loud before being asked --
    otherwise the number looks better than it is."""
    results = [r(TRUE_POSITIVE, 300.0)] * 3 + [r(TRUE_POSITIVE, None)] * 2
    s = score_run(results)
    assert s.detected == 3
    assert s.mttd_p50_s == pytest.approx(300.0)
    assert "3 detected case(s)" in s.as_table()


def test_an_empty_run_does_not_pass():
    """Zero cases is zero evidence, and 0/0 must not become 1.0."""
    s = score_run([])
    assert s.f1 == 0.0
    assert not s.passed


def test_a_perfect_but_slow_run_fails_on_mttd_alone():
    s = score_run([r(TRUE_POSITIVE, 1000.0)] * 12)
    assert s.precision == 1.0 and s.f1 == 1.0
    assert not s.passed
    assert len(s.failures) == 1 and "mttd" in s.failures[0]


def test_table_and_markdown_render_an_undetected_run():
    """`inf` must not reach a report as `infs` or crash the formatter."""
    s = score_run([r(FALSE_NEGATIVE)] * 12)
    assert "n/a" in s.as_table()
    assert "n/a" in s.as_markdown("abc123def", "full")
    assert "FAIL" in s.as_table()


def test_markdown_names_its_tier():
    """Smoke-tier and full-set precision are different numbers, and a report
    that does not say which it is invites them being quoted as one (D5)."""
    s = score_run([r(TRUE_POSITIVE, 200.0)] * 4)
    md = s.as_markdown("abc123def", "smoke")
    assert "smoke" in md
