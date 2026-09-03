"""Assertions about the golden set itself.

The cases are data, and data rots quietly. A typo in an edge name, a budget
shortened during a late-night debugging session, a null case commented out to
make a run finish faster -- none of these fail. They just change what the
scoreboard means, and the scoreboard keeps printing a number either way.

These tests are the thing that makes the golden set an artifact rather than a
directory of YAML.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from eval.injectors import _REGISTRY
from eval.topology import CRIT_P99_BASELINE_MS, EDGES, FAULT_TARGETS, PIPELINE_FLOOR_S, THRESHOLD_MS

CASES_DIR = Path(__file__).resolve().parents[1] / "cases"
CASE_PATHS = sorted(CASES_DIR.glob("*.yaml"))
CASES = [(p.stem, yaml.safe_load(p.read_text(encoding="utf-8"))) for p in CASE_PATHS]


def test_the_set_is_twelve_cases():
    assert len(CASE_PATHS) == 12, \
        f"expected 12 golden cases, found {len(CASE_PATHS)}"


def test_null_cases_are_present():
    """M9, as a test rather than a note.

    Dropping the null case is the single most effective way to improve every
    number on the scoreboard, and it is the one edit that makes the scoreboard
    meaningless -- precision without a false-positive measurement is precision
    over cases chosen to contain no false positives.
    """
    nulls = [stem for stem, c in CASES if c["expect"]["edge"] == "none"]
    assert len(nulls) >= 2, (
        "the golden set must keep both null cases: 001 measures false "
        "positives on flat traffic, 002 measures them under a load ramp, "
        "which is a different failure (M9)")


def test_the_decoy_case_survives():
    """The case that justifies the flagship feature.

    Three properties, and the third is the one that was got wrong first time.
    """
    decoy_case = dict(CASES)["006_parallel_decoy"]
    parts = {p["target"]: p for p in decoy_case["injector"]["parts"]}
    assert len(parts) == 2, "a decoy needs a decoy and a real cause"

    critical_svc = "payment-svc"
    decoy_svc = next(t for t in parts if t != critical_svc)
    assert critical_svc in parts,         "payment-svc carries the critical path 87% of the time; it is the cause"

    decoy_edge = f"order-svc->{decoy_svc}"
    decoy_delay = parts[decoy_svc]["delay_ms"]
    critical_delay = parts[critical_svc]["delay_ms"]

    # 1. The decoy must be visibly slow, or a wall-clock detector passes this
    #    case by accident and the case proves nothing.
    assert decoy_delay > THRESHOLD_MS, (
        f"the decoy's {decoy_delay}ms must breach the {THRESHOLD_MS}ms "
        f"threshold on wall clock")

    # 2. The decoy must stay off the critical path, or the detector is RIGHT to
    #    alert on it and the case scores a correct answer as a false positive.
    assert critical_delay > decoy_delay, (
        f"the decoy (+{decoy_delay}ms) must stay faster than the critical "
        f"service (+{critical_delay}ms), or it IS the critical path")

    # 3. The margin must clear the decoy's OWN critical-path baseline, not its
    #    mean latency.
    #
    #    This is the check that would have caught the first attempt. product-svc
    #    has a mean latency well under payment's -- so a mean-based margin looked
    #    comfortable -- while carrying real blame on 12.4% of traces at a
    #    critical p99 of 49ms. Injecting 120ms into it moved that to 169ms and
    #    tripped the trap. A service that already carries blame cannot be a decoy
    #    at any delay: the injected time lands on the critical path in full.
    decoy_crit_baseline = CRIT_P99_BASELINE_MS[decoy_edge]
    assert decoy_crit_baseline < THRESHOLD_MS, (
        f"{decoy_edge} already carries {decoy_crit_baseline}ms of critical-path "
        f"blame at p99. Any delay injected into it raises that by the delay, so "
        f"it cannot be a decoy -- pick a leg whose critical p99 is ~0.")
    assert critical_delay - decoy_delay > decoy_crit_baseline, (
        f"the {critical_delay - decoy_delay}ms margin must exceed the decoy's "
        f"{decoy_crit_baseline}ms existing critical share, or it wins on the "
        f"traces where it already won")

    assert decoy_edge in decoy_case["expect"]["must_not_alert"]
    assert decoy_case["expect"]["edge"] == f"order-svc->{critical_svc}"


@pytest.mark.parametrize("stem,case", CASES, ids=[s for s, _ in CASES])
def test_case_schema(stem, case):
    assert case["name"] == stem.split("_", 1)[1], \
        f"{stem}: name must match the filename stem"
    assert case.get("description", "").strip(), \
        f"{stem}: a case without a description is a case nobody can maintain"
    assert case["injector"]["type"] in _REGISTRY, \
        f"{stem}: unknown injector type {case['injector'].get('type')!r}"


@pytest.mark.parametrize("stem,case", CASES, ids=[s for s, _ in CASES])
def test_edges_exist(stem, case):
    """Every edge named must be one the application can actually produce.

    This is the test that catches `gateway-svc->user-svc`.
    """
    edge = case["expect"]["edge"]
    if edge != "none":
        assert edge in EDGES, (
            f"{stem}: expected edge {edge!r} does not exist in the service "
            f"graph. Known edges: {sorted(EDGES)}")
    for trap in case["expect"].get("must_not_alert", []):
        if trap == "*":
            continue
        assert trap in EDGES, \
            f"{stem}: trap edge {trap!r} does not exist -- it can never fire"


@pytest.mark.parametrize("stem,case", CASES, ids=[s for s, _ in CASES])
def test_fault_targets_have_a_control_plane(stem, case):
    spec = case["injector"]
    parts = spec.get("parts", [spec])
    for part in parts:
        target = part.get("target")
        if part["type"] == "null":
            continue
        assert target in FAULT_TARGETS, (
            f"{stem}: {target!r} has no /_internal/fault endpoint. An edge's "
            f"latency belongs to its callee -- inject into the callee.")


@pytest.mark.parametrize("stem,case", CASES, ids=[s for s, _ in CASES])
def test_budget_clears_the_pipeline(stem, case):
    """A budget tighter than the pipeline measures your patience.

    Phase 6 measured 375s injection-to-alert against a documented ~245s. The
    reference budgets of 120s and 150s are below the detector's own confirmation
    floor of 3 cycles x 60s and could never pass, no matter how good the
    detector is. A red eval invites weakening the detector, so a budget that
    cannot be met is worse than no gate at all.
    """
    budget = case["expect"]["detect_within_s"]
    if case["expect"]["edge"] == "none":
        # Null cases are observation windows, not deadlines. Longer is
        # strictly better -- more chances to false-positive.
        assert budget >= 600, f"{stem}: a null case needs a long window"
        return
    assert budget >= PIPELINE_FLOOR_S, (
        f"{stem}: budget {budget}s is below the {PIPELINE_FLOOR_S}s pipeline "
        f"worst case. It would score a working detector as a false negative.")


@pytest.mark.parametrize("stem,case", CASES, ids=[s for s, _ in CASES])
def test_traps_are_never_ancestors(stem, case):
    """Cascade propagation upward is correct attribution, not a false positive.

    An edge's critical-path time includes its callee's entire subtree, so a
    payment slowdown genuinely does make `gateway-svc->order-svc` slower.
    Listing an ancestor as a trap would penalise the detector for being right,
    and the fix would be to make it wrong.
    """
    edge = case["expect"]["edge"]
    if edge == "none":
        return
    _src = edge.partition("->")[0]
    for trap in case["expect"].get("must_not_alert", []):
        if trap == "*":
            continue
        trap_dst = trap.partition("->")[2]
        assert trap_dst != _src, (
            f"{stem}: {trap!r} is an ANCESTOR of {edge!r}. It is expected to "
            f"get slower and saying so is correct.")


def test_injector_durations_outlast_their_budgets():
    """A fault that expires mid-case turns a detection into a miss."""
    for stem, case in CASES:
        spec = case["injector"]
        if spec["type"] == "null":
            continue
        budget = case["expect"]["detect_within_s"]
        for part in spec.get("parts", [spec]):
            duration = part.get("duration_s", 0)
            assert duration >= budget, (
                f"{stem}: fault TTL {duration}s is shorter than the {budget}s "
                f"budget -- the fault would heal before the detector ran out "
                f"of time, and the case would score as a false negative")
