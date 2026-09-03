"""Precision, recall, F1 and MTTD over one run of the golden set.

Pure functions over a list of case results. No I/O, no clock, no network --
which is what makes the gate testable without booting the stack, and is why
`eval/tests/` can prove the gate fails without anyone taking a screenshot.

The outcome taxonomy is four values and the last two are the interesting pair:

  true_positive   correct edge inside budget, or a null case that stayed silent
  false_negative  a fault was present and never surfaced in time
  false_positive  a trap edge alerted -- something innocent got blamed
  wrong_edge      something alerted, but neither the expected edge nor a trap

`wrong_edge` counts against precision alongside `false_positive`. An alert
pointing at the wrong service costs the same on-call hour as an alert pointing
at an innocent one; the distinction is kept only so the failure is legible
afterwards.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from statistics import median

# The ABSOLUTE floor: below this the detector is not merely worse than main,
# it is broken. `check_regression.py` handles the relative question against
# eval/baselines/main.json, and the two gates fail for different reasons.
#
# The MTTD ceiling is 360s, not the 180s the reference harness carried, and the
# correction is not a concession -- 180s is arithmetically unreachable. The
# detector's own confirmation window is three cycles at sixty seconds, so 180s
# is the floor BEFORE any pipeline time is counted, and roughly 125s of
# collector batching, sampling decision, assembler grace and minute-bucket
# completion sits in front of it. Phase 6 measured 375s end to end against a
# documented ~245s (F19).
#
# A ceiling nothing can clear is worse than no ceiling: it goes red on a
# working detector, and the natural response to a red gate is to weaken the
# thing being gated. 360s is the pipeline worst case (425s, eval/topology.py)
# less the margin the 1.2x tolerance already provides.
BASELINE = {"f1": 0.85, "precision": 0.90, "mttd_p95_s": 360.0}
TOLERANCE_ABS = 0.03      # absolute, on f1 and precision
TOLERANCE_MTTD = 1.2      # multiplicative, on MTTD p95

TRUE_POSITIVE = "true_positive"
FALSE_NEGATIVE = "false_negative"
FALSE_POSITIVE = "false_positive"
WRONG_EDGE = "wrong_edge"

OUTCOMES = (TRUE_POSITIVE, FALSE_NEGATIVE, FALSE_POSITIVE, WRONG_EDGE)


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile.

    Written out rather than imported because the reference implementation had
    `lat[int(len(lat) * 0.95)]`, which indexes off the end of the list for
    every length that is a multiple of 20 -- including exactly the 10-detection
    run this harness produces. A p95 that raises IndexError on the happy path
    is the kind of bug that only ever fires in CI at 3am.
    """
    if not values:
        return float("inf")
    ordered = sorted(values)
    # ceil, not round: round() is banker's rounding in Python, so a rank
    # landing exactly on .5 alternates between two different answers
    # depending on the parity of the case count.
    idx = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return float(ordered[idx])


@dataclass(frozen=True)
class Scores:
    precision: float
    recall: float
    f1: float
    mttd_p50_s: float
    mttd_p95_s: float
    passed: bool
    tp: int = 0
    fp: int = 0
    fn: int = 0
    detected: int = 0
    total: int = 0
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)

    def as_table(self) -> str:
        def fmt(v: float) -> str:
            return "n/a" if v == float("inf") else f"{v:.0f}s"

        lines = [
            "",
            "  detector-eval scoreboard",
            "  " + "-" * 52,
            f"  cases            {self.total:>6d}   tp {self.tp}  fp {self.fp}  fn {self.fn}",
            f"  precision        {self.precision:>6.3f}   (floor {BASELINE['precision'] - TOLERANCE_ABS:.3f})",
            f"  recall           {self.recall:>6.3f}",
            f"  f1               {self.f1:>6.3f}   (floor {BASELINE['f1'] - TOLERANCE_ABS:.3f})",
            f"  mttd p50         {fmt(self.mttd_p50_s):>6}",
            f"  mttd p95         {fmt(self.mttd_p95_s):>6}   (ceiling {BASELINE['mttd_p95_s'] * TOLERANCE_MTTD:.0f}s)",
            "  " + "-" * 52,
            f"  {'PASS' if self.passed else 'FAIL'}",
        ]
        if self.failures:
            lines.append("")
            lines.append("  gate failures")
            lines.extend(f"    - {f}" for f in self.failures)
        # MTTD is computed over DETECTED cases only. Null cases contribute no
        # latency, so a run that detects nothing has a flattering p95 and a
        # recall of zero. Say this out loud before anyone asks.
        lines.append("")
        lines.append(f"  mttd computed over {self.detected} detected case(s); "
                     "null cases contribute no latency")
        lines.append("")
        return "\n".join(lines)

    def as_markdown(self, git_sha: str = "", tier: str = "full") -> str:
        def fmt(v: float) -> str:
            return "n/a" if v == float("inf") else f"{v:.0f}s"

        head = f"### detector-eval `{tier}` @ `{git_sha[:9] or 'local'}`"
        rows = [
            "| metric | value | gate |",
            "|---|---|---|",
            f"| precision | {self.precision:.3f} | >= {BASELINE['precision'] - TOLERANCE_ABS:.3f} |",
            f"| recall | {self.recall:.3f} | - |",
            f"| f1 | {self.f1:.3f} | >= {BASELINE['f1'] - TOLERANCE_ABS:.3f} |",
            f"| mttd p50 | {fmt(self.mttd_p50_s)} | - |",
            f"| mttd p95 | {fmt(self.mttd_p95_s)} | <= {BASELINE['mttd_p95_s'] * TOLERANCE_MTTD:.0f}s |",
        ]
        verdict = "**PASS**" if self.passed else "**FAIL**"
        out = [head, "", *rows, "", f"{verdict} - {self.tp} tp / {self.fp} fp / {self.fn} fn "
               f"over {self.total} cases"]
        if self.failures:
            out += ["", "<details><summary>gate failures</summary>", ""]
            out += [f"- {f}" for f in self.failures]
            out += ["", "</details>"]
        out += ["", f"_MTTD over {self.detected} detected case(s); null cases "
                "contribute no latency. Tier: `{}`._".format(tier)]
        return "\n".join(out)


def score_run(results: list[dict]) -> Scores:
    tp = sum(r["outcome"] == TRUE_POSITIVE for r in results)
    fp = sum(r["outcome"] in (FALSE_POSITIVE, WRONG_EDGE) for r in results)
    fn = sum(r["outcome"] == FALSE_NEGATIVE for r in results)

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = (2 * precision * recall / (precision + recall)
          if precision + recall else 0.0)

    latencies = [float(r["detected_at_s"]) for r in results
                 if r.get("detected_at_s") is not None]
    p50 = median(latencies) if latencies else float("inf")
    p95 = percentile(latencies, 0.95)

    failures: list[str] = []
    if f1 < BASELINE["f1"] - TOLERANCE_ABS:
        failures.append(f"f1 {f1:.3f} below floor "
                        f"{BASELINE['f1'] - TOLERANCE_ABS:.3f}")
    if precision < BASELINE["precision"] - TOLERANCE_ABS:
        failures.append(f"precision {precision:.3f} below floor "
                        f"{BASELINE['precision'] - TOLERANCE_ABS:.3f}")
    if p95 > BASELINE["mttd_p95_s"] * TOLERANCE_MTTD:
        failures.append(f"mttd p95 {p95:.0f}s above ceiling "
                        f"{BASELINE['mttd_p95_s'] * TOLERANCE_MTTD:.0f}s")

    return Scores(
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(f1, 4),
        mttd_p50_s=round(p50, 1) if p50 != float("inf") else p50,
        mttd_p95_s=round(p95, 1) if p95 != float("inf") else p95,
        passed=not failures,
        tp=tp, fp=fp, fn=fn,
        detected=len(latencies),
        total=len(results),
        failures=failures,
    )
