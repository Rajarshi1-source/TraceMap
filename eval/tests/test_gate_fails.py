"""Exit criterion 5: prove the gate fails.

The build plan asks for a deliberately broken detector in a draft PR and a
screenshot of the red check. That demonstration is worth doing once and is kept
in `docs/adr/0006-eval-gates-ci.md`, but a screenshot is not a test -- it
rots, it cannot run in CI, and it proves the gate failed on one afternoon in
week seven rather than that it fails today.

These tests do the same job continuously. Each one describes a specific way a
detector breaks, constructs the results that break would produce, and requires
the gate to go red. If someone loosens a tolerance, deletes the null case, or
"simplifies" the scorer, one of these fails first.

An eval suite that has never failed is evidence of an untested harness. This
file is where the harness is made to fail on purpose.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from eval.scorer import FALSE_NEGATIVE, FALSE_POSITIVE, TRUE_POSITIVE, WRONG_EDGE, score_run

REPO = Path(__file__).resolve().parents[2]


def case(name: str, outcome: str, at: float | None = 200.0) -> dict:
    return {"case_name": name, "expected_edge": "order-svc->payment-svc",
            "detected_edge": "order-svc->payment-svc",
            "detected_at_s": at if outcome == TRUE_POSITIVE else None,
            "outcome": outcome, "detail": ""}


def healthy_run() -> list[dict]:
    """Twelve cases, all correct. The control."""
    return [case(f"case_{i:03d}", TRUE_POSITIVE) for i in range(1, 13)]


def test_the_control_passes():
    """If this fails, every other test in this file proves nothing."""
    scores = score_run(healthy_run())
    assert scores.passed, scores.as_table()
    assert scores.f1 == 1.0


def test_a_wall_clock_detector_fails_the_gate():
    """The failure the flagship feature exists to prevent.

    A detector reading wall-clock p99 instead of critical-path p99 blames the
    parallel sibling in case 006 and in every case where a decoy is present.
    Three false positives out of twelve is precision 0.75 -- well under the
    0.87 floor -- so the gate goes red without anyone having to notice the
    detector changed.
    """
    results = healthy_run()
    for i in (5, 8, 11):
        results[i] = case(results[i]["case_name"], FALSE_POSITIVE)
    scores = score_run(results)
    assert not scores.passed
    assert any("precision" in f for f in scores.failures), scores.failures


def test_a_deaf_detector_fails_the_gate():
    """Thresholds raised until nothing fires. Precision stays perfect."""
    results = healthy_run()
    for i in range(6):
        results[i] = case(results[i]["case_name"], FALSE_NEGATIVE)
    scores = score_run(results)
    assert scores.precision == 1.0, \
        "a detector that never fires has perfect precision -- which is why " \
        "precision alone is not a gate"
    assert not scores.passed
    assert any("f1" in f for f in scores.failures), scores.failures


def test_a_boiling_frog_detector_fails_the_gate():
    """M6 violated: the baseline learns from anomalies and goes blind.

    The signature is a clean sweep of false negatives on the sustained latency
    cases while the sharp cases still fire, because a step fault outruns the
    EWMA and a ramp does not.
    """
    results = healthy_run()
    for i in (2, 3, 4, 5, 8, 10):
        results[i] = case(results[i]["case_name"], FALSE_NEGATIVE)
    assert not score_run(results).passed


def test_a_slow_detector_fails_the_gate():
    """Correct answers, far too late. Precision and F1 are perfect."""
    results = [case(f"case_{i:03d}", TRUE_POSITIVE, at=900.0)
               for i in range(1, 13)]
    scores = score_run(results)
    assert scores.f1 == 1.0
    assert not scores.passed
    assert any("mttd" in f for f in scores.failures), scores.failures


def test_blaming_the_wrong_service_counts_against_precision():
    """`wrong_edge` is not a free pass.

    An alert naming the wrong service costs the same on-call hour as one
    naming an innocent service, so it has to cost the same on the scoreboard.
    """
    results = healthy_run()
    for i in range(4):
        results[i] = case(results[i]["case_name"], WRONG_EDGE)
    scores = score_run(results)
    assert scores.fp == 4
    assert not scores.passed


def test_a_single_null_case_failure_is_visible():
    """One false positive in twelve must not be absorbed by the tolerance.

    Precision 11/12 = 0.917 still clears the 0.87 floor -- correctly, one bad
    case is not a broken detector. The regression gate is what catches it, by
    naming the case that changed. This test pins that division of labour so a
    future edit cannot quietly make single-case regressions invisible.
    """
    clean = healthy_run()
    clean[0]["case_name"] = "001_null_baseline"

    degraded = [dict(c) for c in clean]
    degraded[0] = case("001_null_baseline", FALSE_POSITIVE)

    scores = score_run(degraded)
    assert scores.passed, "the absolute floor should tolerate one bad case"

    from eval.check_regression import compare_cases
    notes = compare_cases({"cases": degraded}, {"cases": clean})
    assert any("001_null_baseline" in n and "regressed" in n for n in notes), notes


def test_regression_gate_catches_a_drop_the_floor_tolerates(tmp_path):
    """End to end, through the actual CLI that CI runs."""
    baseline = {"git_sha": "aaaaaaaaa", "tier": "full",
                "scores": {"f1": 0.98, "precision": 1.0, "mttd_p95_s": 200.0},
                "cases": healthy_run()}
    degraded = healthy_run()
    for i in (0, 1):
        degraded[i] = case(degraded[i]["case_name"], FALSE_POSITIVE)
    current = {"git_sha": "bbbbbbbbb", "tier": "full",
               "scores": score_run(degraded).as_dict(), "cases": degraded}

    bp = tmp_path / "main.json"
    cp = tmp_path / "run.json"
    bp.write_text(json.dumps(baseline), encoding="utf-8")
    cp.write_text(json.dumps(current), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "eval.check_regression",
         "--baseline", str(bp), "--current", str(cp)],
        cwd=REPO, capture_output=True, text=True)
    assert proc.returncode == 1, \
        f"the gate passed a real regression\n{proc.stdout}\n{proc.stderr}"
    assert "FAIL" in proc.stdout


def test_regression_gate_refuses_a_cross_tier_comparison(tmp_path):
    """Smoke and full scores are different measurements (D5).

    Comparing them is not a lenient check, it is a meaningless one -- and it
    would silently pass a real regression whenever the tiers happened to
    differ in the convenient direction.
    """
    bp = tmp_path / "main.json"
    cp = tmp_path / "run.json"
    bp.write_text(json.dumps({"tier": "full", "scores":
                              {"f1": 0.9, "precision": 0.9, "mttd_p95_s": 200.0}}),
                  encoding="utf-8")
    cp.write_text(json.dumps({"tier": "smoke", "scores":
                              {"f1": 0.9, "precision": 0.9, "mttd_p95_s": 200.0}}),
                  encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "eval.check_regression",
         "--baseline", str(bp), "--current", str(cp)],
        cwd=REPO, capture_output=True, text=True)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "tier mismatch" in proc.stderr


def test_regression_gate_refuses_a_missing_run(tmp_path):
    """A run that did not happen must not pass the gate.

    The obvious way to make a red gate green is to stop producing the artifact
    it reads. Exiting 0 on a missing file would make that work.
    """
    bp = tmp_path / "main.json"
    bp.write_text(json.dumps({"scores": {"f1": 0.9}}), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "eval.check_regression",
         "--baseline", str(bp), "--current", str(tmp_path / "nope.json")],
        cwd=REPO, capture_output=True, text=True)
    assert proc.returncode == 2, proc.stdout + proc.stderr


@pytest.mark.parametrize("dropped", ["001_null_baseline", "002_null_traffic_ramp"])
def test_dropping_a_null_case_is_reported_not_rewarded(dropped):
    """M9, enforced at the gate rather than trusted to discipline.

    Removing a null case is the cheapest way to improve every number on the
    scoreboard. The regression check names it explicitly, because a silently
    shorter case list reads as an improvement.
    """
    from eval.check_regression import compare_cases

    full = healthy_run()
    full[0]["case_name"] = "001_null_baseline"
    full[1]["case_name"] = "002_null_traffic_ramp"
    trimmed = [c for c in full if c["case_name"] != dropped]

    notes = compare_cases({"cases": trimmed}, {"cases": full})
    assert any(dropped in n and "MISSING" in n for n in notes), notes
