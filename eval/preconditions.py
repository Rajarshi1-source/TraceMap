"""Does a case's premise still hold on the host it is about to run on?

Every latency case in the golden set is a claim about an ORDERING. Case 009
says a 250ms CPU burn carries `order-svc->user-svc` past `order-svc->payment-svc`
and onto the critical path. Case 006 says a 100ms delay on the same edge does
NOT. Both claims were computed against measurements taken on one machine, and
the set runs on another.

That difference is not hypothetical, and it is not small:

    order-svc->payment-svc critical p99
      157.0 ms   declared in eval/topology.py
      187.97 ms  measured, run 33887207039 (pre-fault snapshot)
      ~289 ms    measured, run 33870357809

Case 009 budgets a 250ms burn against a declared 157ms -- a 93ms margin. The
thing it is measured against moves by more than that between runs. When payment
sits at 289ms the burn does not overtake it, user-svc correctly carries no
blame, and a detector doing exactly the right thing is scored as a false
negative.

This is F42's lesson generalised. F42 was one declared constant (a decoy's
zero-blame baseline) that had stopped being true; the fix asserted that one
constant against measurement. The same failure exists for every case that
depends on an ordering, and asserting them one at a time as each is discovered
is how you find the third instance in production.

WHAT THIS DELIBERATELY DOES NOT DO. It does not adjust anyone's score. A case
whose premise is false is not a case the detector failed -- it is a measurement
the apparatus could not support, and the honest response is to decline to make
it and say so loudly, the same way preflight declines to start on an idle
stack. `eval/scorer.py` excludes them; the scoreboard names them; and
`check_regression.py` refuses to record a baseline from a run that had any.

The guard against that becoming an excuse: preconditions live in the case
files, version-controlled and reviewable, and the number excluded is printed on
the scoreboard. A run that cannot measure most of its own set has not produced
a score worth quoting, and `MAX_EXCLUSIONS` is where that line is drawn.
"""

from __future__ import annotations

from dataclasses import dataclass

# More than this many cases invalidated and the run is not a measurement of the
# detector at all -- it is a measurement of the gap between the golden set and
# the hardware. Two is already generous.
MAX_EXCLUSIONS = 2


@dataclass(frozen=True)
class PreconditionResult:
    case: str
    kind: str
    holds: bool
    detail: str

    def __str__(self) -> str:
        return f"{self.case}: {self.detail}"


def _injected_ms(case: dict) -> float:
    """How much latency this case's injector adds to its target edge.

    Only latency-shaped faults move an ordering. A saturation fault's
    `cpu_burn_ms` occupies the event loop and shows up as latency, so it counts;
    an error-rate fault does not move a percentile in a way an ordering claim
    depends on, and returns 0.
    """
    inj = case.get("injector") or {}
    parts = inj.get("parts") or [inj]
    total = 0.0
    for p in parts:
        total = max(total, float(p.get("delay_ms", 0) or 0)
                    + float(p.get("cpu_burn_ms", 0) or 0))
    return total


def check(case: dict, measured: dict[str, float]) -> list[PreconditionResult]:
    """Evaluate one case's declared preconditions against a live topology.

    `measured` is the pre-fault critical p99 per edge, snapshotted by the runner
    before anything is injected -- the same source the trap-validity check uses,
    for the same reason: the only baseline worth asserting against is the one
    this host actually produced minutes ago.
    """
    out: list[PreconditionResult] = []
    name = case.get("name", "?")
    expected = (case.get("expect") or {}).get("edge")
    if not expected or expected == "none":
        return out

    base = measured.get(expected)
    if base is None:
        # No traffic on the edge at all. Preflight owns that failure; saying it
        # twice in different words helps nobody.
        return out

    injected = _injected_ms(case)

    for pre in case.get("preconditions") or []:
        other = pre.get("edge")
        margin = float(pre.get("margin_ms", 0) or 0)
        other_ms = measured.get(other)
        if other_ms is None:
            out.append(PreconditionResult(
                name, "missing", False,
                f"precondition names {other}, which carried no traffic"))
            continue

        projected = base + injected

        if "overtakes" in pre or pre.get("kind") == "overtakes":
            holds = projected >= other_ms + margin
            out.append(PreconditionResult(
                name, "overtakes", holds,
                f"{expected} at {base:.1f}ms +{injected:.0f}ms injected "
                f"= {projected:.1f}ms vs {other} at {other_ms:.1f}ms "
                f"+{margin:.0f}ms margin -- "
                + ("clears it" if holds else
                   "DOES NOT clear it, so this case cannot detect what it "
                   "claims to and would score a correct detector as wrong")))

        elif "stays_below" in pre or pre.get("kind") == "stays_below":
            holds = projected <= other_ms - margin
            out.append(PreconditionResult(
                name, "stays_below", holds,
                f"{expected} at {base:.1f}ms +{injected:.0f}ms injected "
                f"= {projected:.1f}ms vs {other} at {other_ms:.1f}ms "
                f"-{margin:.0f}ms margin -- "
                + ("stays under it" if holds else
                   "OVERTAKES it, so the edge this case requires to stay "
                   "blameless is genuinely on the critical path")))

    return out


def invalid_cases(results: list[PreconditionResult]) -> list[str]:
    """Case names whose premise does not hold, de-duplicated and ordered."""
    seen: dict[str, None] = {}
    for r in results:
        if not r.holds:
            seen.setdefault(r.case, None)
    return list(seen)
