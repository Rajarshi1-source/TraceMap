"""Does a case's premise hold on the host it is about to run on?

F44, and it is F42's lesson generalised. F42 was one declared constant -- a
decoy's zero-blame baseline -- that had stopped being true on CI, and the fix
asserted that one constant against measurement. The same failure exists for
every case whose premise is an ordering, and asserting them one at a time as
each is discovered is how you find the third instance in production.

The numbers below are the measured ones:

    order-svc->payment-svc critical p99
      157.0 ms   declared in eval/topology.py
      187.97 ms  measured, run 33887207039 (pre-fault snapshot)
      ~289 ms    measured, run 33870357809

Case 009 budgets a 250 ms burn against the declared 157 ms. The thing it is
measured against moves by more than that margin between runs.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from eval.preconditions import MAX_EXCLUSIONS, check, invalid_cases

CASES = pathlib.Path(__file__).resolve().parents[1] / "cases"


def case(name: str) -> dict:
    path = next(CASES.glob(f"*{name}*.yaml"))
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# The three topologies this project has actually observed.
DECLARED = {"order-svc->payment-svc": 157.0, "order-svc->user-svc": 0.0,
            "order-svc->product-svc": 49.3}
RUN4 = {"order-svc->payment-svc": 187.97, "order-svc->user-svc": 0.0,
        "order-svc->product-svc": 47.89, "gateway-svc->order-svc": 297.92}
RUN3 = {"order-svc->payment-svc": 289.1, "order-svc->user-svc": 0.0,
        "order-svc->product-svc": 49.3}


# ---------------------------------------------------------------------------
# The measurement this exists for
# ---------------------------------------------------------------------------

def test_saturation_premise_holds_on_the_declared_topology():
    """0 + 250 = 250 against 157 + 40 = 197. Clears it comfortably."""
    res = check(case("009"), DECLARED)
    assert res and all(r.holds for r in res)


def test_saturation_premise_still_holds_at_run_4_measurements():
    """188 + 40 = 228 against 250. Tighter, and still true."""
    res = check(case("009"), RUN4)
    assert all(r.holds for r in res), [str(r) for r in res]


def test_saturation_premise_FAILS_at_run_3_measurements():
    """289 + 40 = 329 against 250. The burn does not overtake.

    This is the whole point. On that host `order-svc->user-svc` correctly
    carries no blame, so the case cannot detect what it claims to, and a
    detector doing exactly the right thing is scored a false negative.
    """
    res = check(case("009"), RUN3)
    assert not all(r.holds for r in res)
    bad = next(r for r in res if not r.holds)
    assert "DOES NOT clear it" in bad.detail
    # The message has to carry both numbers, or a reader cannot tell whether
    # the case or the system moved.
    assert "289.1" in bad.detail and "250" in bad.detail


def test_the_800ms_case_holds_on_every_topology_observed():
    """800ms is chosen to clear payment on any plausible host."""
    for topo in (DECLARED, RUN4, RUN3):
        assert all(r.holds for r in check(case("005"), topo))


# ---------------------------------------------------------------------------
# Mechanics
# ---------------------------------------------------------------------------

def test_a_case_with_no_preconditions_produces_no_verdict():
    """Silence means 'nothing declared', never 'checked and fine'."""
    assert check(case("003"), RUN4) == []


def test_a_null_case_is_never_checked():
    """It injects nothing and expects no edge; there is no ordering to hold."""
    assert check(case("001"), RUN4) == []


def test_an_edge_with_no_traffic_is_reported_not_assumed():
    res = check(case("009"), {"order-svc->user-svc": 0.0})
    assert res and not res[0].holds
    assert "carried no traffic" in res[0].detail


def test_a_missing_expected_edge_defers_to_preflight():
    """Preflight owns 'nothing is driving load'. Saying it twice helps nobody."""
    assert check(case("009"), {"order-svc->payment-svc": 187.97}) == []


def test_composite_injectors_use_their_largest_leg():
    """Case 006 injects into two services at once.

    The ordering claim is about the edge the case EXPECTS, so the figure that
    matters is the largest single injection, not their sum -- the legs run
    concurrently and do not add.
    """
    from eval.preconditions import _injected_ms
    assert _injected_ms(case("006")) == 150.0


def test_invalid_cases_deduplicates():
    res = check(case("009"), RUN3) * 3
    assert invalid_cases(res) == ["user_cpu_saturation"]


def test_there_is_a_ceiling_on_how_much_can_be_excused():
    """The guard against this becoming a way to make the gate green.

    A run that cannot measure most of its own set has not produced a score
    worth quoting.
    """
    assert MAX_EXCLUSIONS <= 2


# ---------------------------------------------------------------------------
# The declarations themselves
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stem", ["005", "009"])
def test_declared_preconditions_are_well_formed(stem):
    """A malformed precondition silently checks nothing, which is worse than
    having none -- it looks like coverage."""
    for pre in case(stem)["preconditions"]:
        assert pre["kind"] in ("overtakes", "stays_below")
        assert pre["edge"]
        # A margin under bucket-to-bucket p99 noise (~20ms) makes the check
        # itself intermittent, which is the failure it exists to prevent.
        assert pre["margin_ms"] >= 20
