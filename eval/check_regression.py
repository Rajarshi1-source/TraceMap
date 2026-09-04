"""The merge gate: compare a run against the committed baseline.

Separate from `scorer.py` on purpose. The scorer answers "is this detector good
enough in absolute terms"; this answers "is it worse than the one on main".
They fail for different reasons and a run can pass one and fail the other --
a detector that was always mediocre passes this and fails the scorer, and a
detector that dropped from excellent to merely good fails this and passes the
scorer. Collapsing them into one number hides which happened.

Tolerances are asymmetric by design:

  f1, precision   absolute, 0.03. Percentile-based metrics over a twelve-case
                  set move by roughly one case, and 1/12 is 0.083 -- so 0.03 is
                  tighter than the quantisation of the measurement and will
                  catch any real drop while ignoring nothing.
  mttd p95        multiplicative, 1.2x. Detection latency is dominated by the
                  pipeline, which varies with host load in a way the detector
                  cannot control, so an absolute second-count would fail on a
                  busy CI runner and teach people to re-run until green.

Recall is reported but NOT gated. Recall alone is trivially maximised by
alerting on everything, and gating it invites exactly that trade. Precision and
F1 together already constrain it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TOLERANCE_ABS = 0.03
TOLERANCE_MTTD = 1.2

# (metric, tolerance kind). "abs" gates on a floor below the baseline,
# "mult" on a ceiling above it -- see the module docstring for why the two are
# asymmetric.
GATED = (
    ("f1", "abs"),
    ("precision", "abs"),
    ("mttd_p95_s", "mult"),
)


def load(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    # Accept either a bare scores object or a full runner payload, so the same
    # file can be a committed baseline and a run artifact.
    return data.get("scores", data)


def _fmt(key: str, value) -> str:
    if value is None:
        return "n/a"
    if key.endswith("_s"):
        return f"{float(value):.0f}s"
    return f"{float(value):.3f}"


def compare(current: dict, baseline: dict) -> tuple[bool, list[str], list[str]]:
    failures: list[str] = []
    lines: list[str] = []

    for key, kind in GATED:
        cur = current.get(key)
        base = baseline.get(key)
        if base is None:
            lines.append(f"  {key:<14} {_fmt(key, cur):>8}   (no baseline)")
            continue
        if cur is None:
            failures.append(f"{key} missing from the run")
            continue

        cur_f, base_f = float(cur), float(base)
        if kind == "abs":
            floor = base_f - TOLERANCE_ABS
            ok = cur_f >= floor
            bound = f"floor {floor:.3f}"
        else:
            ceiling = base_f * TOLERANCE_MTTD
            ok = cur_f <= ceiling
            bound = f"ceiling {ceiling:.0f}s"

        delta = cur_f - base_f
        sign = "+" if delta >= 0 else ""
        lines.append(
            f"  {key:<14} {_fmt(key, cur):>8}   baseline {_fmt(key, base):>8}"
            f"  {sign}{delta:.3f}   {bound}   {'ok' if ok else 'REGRESSED'}")
        if not ok:
            failures.append(
                f"{key} {_fmt(key, cur)} vs baseline {_fmt(key, base)} "
                f"({bound})")

    return not failures, lines, failures


def compare_cases(current: dict, baseline: dict) -> list[str]:
    """Name the cases that changed outcome.

    An aggregate that moved is a fact; which case moved is the actionable part,
    and a reviewer should not have to diff two JSON files by eye to get it. A
    newly failing decoy or null case is worth calling out by name even when the
    aggregate still clears the gate.
    """
    def outcomes(payload: dict) -> dict[str, str]:
        return {c["case_name"]: c["outcome"] for c in payload.get("cases", [])}

    cur, base = outcomes(current), outcomes(baseline)
    if not cur or not base:
        return []
    notes = []
    for name in sorted(set(cur) | set(base)):
        a, b = base.get(name), cur.get(name)
        if a and b and a != b:
            arrow = "regressed" if b != "true_positive" else "recovered"
            notes.append(f"  {name:<28} {a} -> {b}   ({arrow})")
        elif b and not a:
            notes.append(f"  {name:<28} new case ({b})")
        elif a and not b:
            notes.append(f"  {name:<28} MISSING from this run "
                         "-- was it dropped to improve the score? (M9)")
    return notes


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # One baseline per tier. Smoke and full are computed over different case
    # mixes, and the tier check below refuses to compare them -- so a single
    # baseline file would make every smoke run exit 2 and block every PR.
    p.add_argument("--baseline", default=None,
                   help="defaults to eval/baselines/main-<tier>.json, "
                        "chosen from the run's own tier")
    p.add_argument("--current", default="eval-run.json",
                   help="runner --json output")
    p.add_argument("--update", action="store_true",
                   help="overwrite the baseline with the current run "
                        "(only ever on main, and only with the run attached)")
    args = p.parse_args()

    current_path = Path(args.current)

    if not current_path.exists():
        print(f"\n  no run to check: {current_path} missing.\n"
              "  The gate cannot pass a run that did not happen.\n",
              file=sys.stderr)
        return 2

    current_full = json.loads(current_path.read_text(encoding="utf-8"))
    current = current_full.get("scores", current_full)

    tier = current_full.get("tier", "full")
    baseline_path = Path(args.baseline or f"eval/baselines/main-{tier}.json")

    if args.update:
        # F44. A baseline is the number every future run is judged against, so
        # it has to come from a run that measured the whole set. A run with
        # excluded cases produced a real score over a SUBSET, and freezing that
        # as the reference would quietly redefine what "the golden set" means --
        # and would do it in the direction of whichever cases this host happens
        # to find inconvenient. Same reasoning as refusing to update a baseline
        # from a red run, one level further out.
        excluded = current.get("excluded") or []
        if excluded:
            print(f"\n  refusing to record a baseline from a run that skipped "
                  f"{len(excluded)} case(s): {', '.join(excluded)}.\n"
                  "  Their preconditions did not hold on that host, so the run\n"
                  "  measured a subset. Re-derive the case constants for this\n"
                  "  hardware, or run where the set's premises hold.\n",
                  file=sys.stderr)
            return 2
        if current_full.get("invalid_traps"):
            print("\n  refusing to record a baseline from a run whose traps "
                  "were invalid.\n", file=sys.stderr)
            return 2
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(current_full, indent=2) + "\n",
                                 encoding="utf-8")
        print(f"  baseline updated from {current_path}")
        return 0

    if not baseline_path.exists():
        print(f"\n  no baseline at {baseline_path}.\n"
              "  Record one with --update on main before gating.\n",
              file=sys.stderr)
        return 2

    baseline_full = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline = baseline_full.get("scores", baseline_full)

    cur_tier = current_full.get("tier")
    base_tier = baseline_full.get("tier")
    if cur_tier and base_tier and cur_tier != base_tier:
        # Smoke and full scores are computed over different case mixes. A
        # cross-tier comparison is not a regression check, it is a category
        # error dressed as one (D5).
        print(f"\n  tier mismatch: run is '{cur_tier}', baseline is "
              f"'{base_tier}'.\n  These are different measurements and must "
              "not be compared.\n", file=sys.stderr)
        return 2

    ok, lines, failures = compare(current, baseline)

    print()
    print(f"  regression check   run {current_full.get('git_sha', '')[:9] or 'local'}"
          f"  vs baseline {baseline_full.get('git_sha', '')[:9] or 'main'}"
          f"  tier={cur_tier or 'full'}")
    print("  " + "-" * 68)
    for line in lines:
        print(line)
    case_notes = compare_cases(current_full, baseline_full)
    if case_notes:
        print("  " + "-" * 68)
        print("  case-level changes")
        for note in case_notes:
            print(note)
    print("  " + "-" * 68)
    print(f"  {'PASS' if ok else 'FAIL'}")
    print()

    if not ok:
        print("  regressions:", file=sys.stderr)
        for f in failures:
            print(f"    - {f}", file=sys.stderr)
        print("\n  Fix the detector, or change the baseline in a commit that "
              "says why.\n  Tuning a threshold to clear this gate without "
              "re-running the set is M8.\n", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
