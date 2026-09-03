#!/usr/bin/env python3
"""Prove the eval gate fails, without a stack and without a screenshot.

Phase 7 exit criterion 5 asks for a deliberately broken detector in a draft PR
and a screenshot of the red check. That was done once and is written up in
`docs/adr/0006-eval-gates-ci.md`.

This is the version that keeps working. It synthesises the results each way of
breaking the detector would produce, runs them through the real scorer and the
real `check_regression` CLI, and fails if any of them come out green.

    make eval-gate-guard

The failure it defends against is not a broken detector -- it is a broken
GATE. A tolerance loosened to get a merge through, a null case quietly
dropped, a scorer "simplified" until it cannot fail: all three leave a gate
that reports PASS forever, and a gate that always passes is indistinguishable
from no gate except that people believe it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from eval.scorer import (  # noqa: E402
    FALSE_NEGATIVE,
    FALSE_POSITIVE,
    TRUE_POSITIVE,
    WRONG_EDGE,
    score_run,
)

CASE_NAMES = [
    "001_null_baseline", "002_null_traffic_ramp", "003_payment_latency_60ms",
    "004_payment_latency_200ms", "005_product_latency_800ms",
    "006_parallel_decoy", "007_payment_errors_10pct",
    "008_payment_errors_50pct", "009_user_cpu_saturation",
    "010_payment_db_down", "011_deploy_correlated",
    "012_gateway_latency_cascade",
]

fail = 0


def result(name: str, outcome: str, at: float | None = 250.0) -> dict:
    return {"case_name": name, "expected_edge": "order-svc->payment-svc",
            "detected_edge": "order-svc->payment-svc",
            "detected_at_s": at if outcome == TRUE_POSITIVE else None,
            "outcome": outcome, "detail": ""}


def clean() -> list[dict]:
    return [result(n, TRUE_POSITIVE) for n in CASE_NAMES]


def broken(indices, outcome, at=250.0) -> list[dict]:
    out = clean()
    for i in indices:
        out[i] = result(CASE_NAMES[i], outcome, at)
    return out


def check(label: str, results: list[dict], *, want_pass: bool) -> None:
    global fail
    scores = score_run(results)
    ok = scores.passed == want_pass
    verdict = "PASS" if scores.passed else "FAIL"
    print(f"  {label:<48} gate={verdict:<5} {'OK' if ok else 'WRONG'}")
    if not ok:
        fail = 1
        print(f"      expected the gate to {'pass' if want_pass else 'FAIL'}; "
              f"f1={scores.f1} precision={scores.precision} "
              f"mttd_p95={scores.mttd_p95_s}")


def check_cli(label: str, baseline: dict, current: dict, want_code: int) -> None:
    global fail
    with tempfile.TemporaryDirectory() as td:
        bp, cp = Path(td) / "main.json", Path(td) / "run.json"
        bp.write_text(json.dumps(baseline), encoding="utf-8")
        cp.write_text(json.dumps(current), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "eval.check_regression",
             "--baseline", str(bp), "--current", str(cp)],
            cwd=REPO, capture_output=True, text=True)
    ok = proc.returncode == want_code
    print(f"  {label:<48} exit={proc.returncode:<5} {'OK' if ok else 'WRONG'}")
    if not ok:
        fail = 1
        print(f"      expected exit {want_code}")
        print("      " + (proc.stdout or proc.stderr).replace("\n", "\n      "))


def main() -> int:
    print()
    print("  eval gate guard - the gate must go red on a broken detector")
    print()

    # The control. If this is not green, nothing below proves anything.
    check("a correct detector", clean(), want_pass=True)

    # Reads wall-clock p99 instead of critical-path p99. Blames the parallel
    # sibling on the decoy and on every case where one is slow. This is the
    # failure the flagship feature exists to prevent, so it is the one the
    # gate must be loudest about.
    check("wall-clock detector (blames the decoy)",
          broken([5, 8, 11], FALSE_POSITIVE), want_pass=False)

    # Threshold raised until nothing fires. Precision stays a perfect 1.0,
    # which is why precision alone is not a gate.
    check("deaf detector (threshold too high)",
          broken([2, 3, 4, 5, 6, 7], FALSE_NEGATIVE), want_pass=False)

    # M6 violated: _update_ewma hoisted out of the healthy branch, so the
    # baseline chases the fault and the detector goes silent on a service that
    # is still broken. Sustained cases miss; sharp ones still fire.
    check("boiling-frog baseline (M6 violated)",
          broken([2, 3, 4, 5, 8, 10], FALSE_NEGATIVE), want_pass=False)

    # Right answers, far too late. F1 is 1.0.
    check("correct but too slow", [result(n, TRUE_POSITIVE, 900.0)
                                   for n in CASE_NAMES], want_pass=False)

    # Alerts on a service that is neither the culprit nor a listed trap.
    check("blames the wrong service",
          broken([0, 1, 2, 3], WRONG_EDGE), want_pass=False)

    print()

    # And through the CLI CI actually runs.
    base = {"git_sha": "baseline0", "tier": "full",
            "scores": {"f1": 0.95, "precision": 0.97, "mttd_p95_s": 300.0},
            "cases": clean()}
    dropped = [c for c in clean() if c["case_name"] != "001_null_baseline"]

    check_cli("regression below tolerance",
              base,
              {"git_sha": "bad", "tier": "full",
               "scores": score_run(broken([0, 1, 2], FALSE_POSITIVE)).as_dict(),
               "cases": broken([0, 1, 2], FALSE_POSITIVE)},
              want_code=1)

    check_cli("no run produced at all",
              base,
              {"git_sha": "same", "tier": "full",
               "scores": base["scores"], "cases": clean()},
              want_code=0)

    check_cli("smoke scores compared to a full baseline",
              base,
              {"git_sha": "x", "tier": "smoke",
               "scores": base["scores"], "cases": clean()},
              want_code=2)

    # The per-tier baseline default. A smoke run must not be gated against a
    # full-tier baseline -- the tier check refuses, and with a single shared
    # baseline file that refusal would block every PR.
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        cp = Path(td) / "run.json"
        cp.write_text(json.dumps({"tier": "smoke", "scores": base["scores"],
                                  "cases": clean()}), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "eval.check_regression", "--current", str(cp)],
            cwd=REPO, capture_output=True, text=True)
    picked = "main-smoke.json" in (proc.stdout + proc.stderr)
    print(f"  {'smoke run selects the smoke baseline':<48} "
          f"{'selected':<10} {'OK' if picked else 'WRONG'}")
    if not picked:
        globals()["fail"] = 1
        print(f"      {(proc.stdout + proc.stderr).strip()[:200]}")

    # M9. Dropping the null case improves every number, so the diff has to
    # name it rather than silently reporting an improvement.
    from eval.check_regression import compare_cases
    notes = compare_cases({"cases": dropped}, {"cases": clean()})
    named = any("001_null_baseline" in n and "MISSING" in n for n in notes)
    print(f"  {'dropping the null case is named (M9)':<48} "
          f"{'reported':<10} {'OK' if named else 'WRONG'}")
    if not named:
        globals()["fail"] = 1

    print()
    print("  GATE GUARD PASS" if not fail else "  GATE GUARD FAIL")
    print()
    return fail


if __name__ == "__main__":
    raise SystemExit(main())
