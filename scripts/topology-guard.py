#!/usr/bin/env python3
"""Assert the declared service graph still matches the live one.

`eval/topology.py` declares the edges the golden set is written against, and
every case is checked against that declaration by `test_cases.py`. That catches
a typo. It does not catch the declaration itself going stale -- somebody adding
a service, or removing a call, and the eval set quietly continuing to describe
an application that no longer exists.

Run against a warm stack:

    make topology-guard

The critical-path shares are checked too, and they are the more interesting
half. A decoy case is only valid while the service it uses as the decoy carries
no blame; `order-svc->product-svc` looked like a sibling and turned out to be
on the critical path for 12.4% of traces, which broke case 006 in a way that
scored a correct detector as wrong.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eval.topology import CRIT_P99_BASELINE_MS, EDGES, THRESHOLD_MS

API = os.environ.get("ANALYTICS_API", "http://localhost:8090")

# How far a measured critical p99 may drift from the recorded baseline before
# the decoy design has to be revisited. Generous: this is a "has the shape
# changed" check, not a regression gate.
DRIFT_TOLERANCE_MS = 30.0

fail = 0


def say(what: str, ok: bool, detail: str = "") -> None:
    """A hard check. Structural facts about the graph belong here."""
    global fail
    print(f"  {what:<56} {'OK' if ok else 'FAIL'}")
    if not ok:
        fail = 1
        if detail:
            print(f"      {detail}")


def note(what: str, ok: bool, detail: str = "") -> None:
    """A soft check.

    Critical-path shares move with load, and they move a lot for several
    minutes after any injected fault -- the graph API reports over a rolling
    window, so a run immediately after an eval case reads the fault as drift.
    Failing on that would make this guard flaky, and a flaky guard gets
    disabled, which is worse than a guard that reports.

    Structure is asserted; shape is reported.
    """
    print(f"  {what:<56} {'OK' if ok else 'DRIFT'}")
    if not ok and detail:
        print(f"      {detail}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--api", default=API)
    ap.add_argument("--window", type=int, default=5)
    args = ap.parse_args()

    print()
    print(f"  topology guard -> {args.api}")
    print()

    try:
        r = httpx.get(f"{args.api}/api/graph", params={"window": args.window},
                      timeout=15)
        r.raise_for_status()
        edges = r.json().get("edges", [])
    except Exception as exc:  # noqa: BLE001 - a guard reports, it does not crash
        print(f"  cannot reach the graph API: {exc}\n")
        return 1

    live = {f"{e['source']}->{e['target']}": e for e in edges}

    missing = sorted(EDGES - set(live))
    say("every declared edge exists in the live graph", not missing,
        f"declared but absent: {missing}. Either the app changed or load is "
        f"not exercising these paths.")

    extra = sorted(set(live) - EDGES)
    say("no undeclared edges in the live graph", not extra,
        f"live but undeclared: {extra}. The golden set does not know about "
        f"these, so nothing tests them.")

    # The decoy invariant, checked against reality rather than against a
    # comment. A leg whose critical p99 has risen past the detector threshold
    # can no longer be used as a decoy at any injected delay.
    for edge, recorded in sorted(CRIT_P99_BASELINE_MS.items()):
        e = live.get(edge)
        if e is None:
            continue
        measured = float(e.get("critP99") or 0.0)
        drift = abs(measured - recorded)
        note(f"crit p99 {edge:<26} {measured:>7.1f} (recorded {recorded:.1f})",
             drift <= DRIFT_TOLERANCE_MS,
             f"drifted {drift:.1f}ms. Expected right after an eval case, since "
             f"the window still contains the fault. On QUIET traffic this "
             f"means the shape changed -- and if a leg that was a sibling now "
             f"carries blame, case 006 needs a different decoy, not a "
             f"different delay.")

    # F42. This check was reading CRIT_P99_BASELINE_MS -- the DECLARED values,
    # a hardcoded dict -- so it asked "does the declaration still contain a
    # decoy?", which is a constant and can never be false. It printed OK on a
    # CI runner where `order-svc->user-svc` measured 56.6ms of critical path
    # against a declared 0.0, and the run proceeded to score six cases whose
    # `must_not_alert` traps were already invalid.
    #
    # A tautological hard check is worse than no hard check: it occupies the
    # place where the real one should be and reports OK while doing nothing.
    #
    # Now measured, and now specific. A leg declared as a zero-blame sibling
    # that is observed carrying blame past the detector threshold cannot be a
    # decoy at ANY injected delay -- so every case naming it in `must_not_alert`
    # is asserting something false, and the scoreboard those cases produce
    # measures the harness rather than the detector.
    declared_decoys = [e for e, ms in CRIT_P99_BASELINE_MS.items()
                       if ms < THRESHOLD_MS]
    invalid = []
    still_valid = []
    for edge in declared_decoys:
        e = live.get(edge)
        if e is None:
            continue
        measured = float(e.get("critP99") or 0.0)
        (invalid if measured >= THRESHOLD_MS else still_valid).append(
            (edge, measured))

    if not (invalid or still_valid):
        # No declared decoy appears in the live graph at all. That is the
        # "edges absent" failure above, already reported; asserting decoy
        # validity against nothing would add a second, misleading failure
        # pointing at the case design rather than at the missing traffic.
        print("  decoy validity                                           "
              "SKIP")
        print("      no declared decoy leg is present in the live graph -- "
              "send traffic and re-run")
    else:
        say("every declared decoy leg still carries no blame", not invalid,
            "; ".join(f"{edge} measured {ms:.1f}ms critical p99 against a "
                      f"declared 0.0 (threshold {THRESHOLD_MS:.0f}ms)"
                      for edge, ms in invalid)
            + ". Every case naming it in `must_not_alert` is now asserting "
              "something false. Re-measure the decoy on THIS host and update "
              "eval/topology.py, or pick a different leg -- do not change the "
              "injected delay, and do not run the set until this passes.")

        say("at least one leg is still a true parallel sibling",
            bool(still_valid),
            "every fan-out leg now carries critical-path blame; case 006 has "
            "no valid decoy and must be redesigned rather than retuned.")

    print()
    print("  Structure and decoy validity are ASSERTED; share drift is reported.")
    print("  Run this on quiet traffic -- a rolling window still holding a")
    print("  recent fault reads as drift, and will fail the decoy check for a")
    print("  real reason: the set must not run while a fault is still in the")
    print("  window either.")
    print()
    print("  TOPOLOGY GUARD PASS" if not fail else "  TOPOLOGY GUARD FAIL")
    print()
    return fail


if __name__ == "__main__":
    raise SystemExit(main())
