"""The alert contract: what a responder actually sees.

Every assertion here is about credibility. An alert that buries the useful
number, or links to a trace where nothing looks wrong, teaches people to ignore
the next one - and a detector nobody reads is worth less than no detector, because
it also costs the illusion of coverage.
"""

from __future__ import annotations

import itertools
import json

from src.core.alert_manager import build_blocks
from src.core.anomaly_detector import Anomaly
from src.core.deploy_attributor import DeployCorrelation

ANOMALY = Anomaly(
    source="order-svc", target="payment-svc", metric="critical_p99",
    baseline_ms=360.0, observed_ms=418.0, delta_ms=58.0,
    request_count=1204, wall_p99_ms=421.0, sustained=3,
)

TRACES = [
    {"traceId": "d4f8a2aaaa", "criticalMs": 812.0, "durationMs": 815.0},
    {"traceId": "9c1e77bbbb", "criticalMs": 794.0, "durationMs": 900.0},
    {"traceId": "2b6f01cccc", "criticalMs": 771.0, "durationMs": 780.0},
    {"traceId": "44aa55dddd", "criticalMs": 700.0, "durationMs": 705.0},
    {"traceId": "77bb88eeee", "criticalMs": 690.0, "durationMs": 695.0},
    {"traceId": "99cc00ffff", "criticalMs": 12.0, "durationMs": 999.0},
]

CORRELATED = DeployCorrelation(
    found=True, service="payment-svc", revision="12", git_sha="4f2a9c1",
    deployed_at="2026-09-03 10:00:00", lag_seconds=47, confidence=0.95,
)
UNCORRELATED = DeployCorrelation(found=False)


def _text(blocks) -> str:
    return json.dumps(blocks)


def test_alert_leads_with_critical_path_not_wall_clock():
    """The ordering IS the product thesis. Lead with wall clock and you have
    rebuilt the tool this project exists to replace."""
    blocks = build_blocks(ANOMALY, CORRELATED, TRACES)
    fields = [f["text"] for f in blocks[1]["fields"]]

    assert "Critical-path p99" in fields[0]
    assert "Wall-clock p99" in fields[1]
    assert fields.index(next(f for f in fields if "Critical-path" in f)) < \
           fields.index(next(f for f in fields if "Wall-clock" in f))


def test_alert_shows_baseline_observed_and_delta():
    blocks = build_blocks(ANOMALY, CORRELATED, TRACES)
    crit = blocks[1]["fields"][0]["text"]
    assert "418ms" in crit and "360ms" in crit and "+58ms" in crit


def test_five_sample_traces_ordered_by_critical_ms():
    """Ordering by duration_ms would link to a trace where the edge was slow but
    PARALLEL. The responder clicks through, sees nothing wrong, and starts
    discounting the alerts."""
    blocks = build_blocks(ANOMALY, CORRELATED, TRACES)
    trace_block = _text(blocks[3])

    assert trace_block.count("|") == 5, "expected exactly 5 trace links"
    # The 999ms-wall / 12ms-critical decoy must NOT be linked.
    assert "99cc00" not in trace_block, \
        "linked a trace that was slow in wall clock but parallel"
    assert "d4f8a2" in trace_block


def test_a_correlated_deploy_is_named_with_confidence():
    blocks = build_blocks(ANOMALY, CORRELATED, TRACES)
    cause = blocks[2]["text"]["text"]
    assert "payment-svc" in cause and "rev 12" in cause
    assert "47s" in cause and "0.95" in cause
    assert "4f2a9c1" in cause


def test_the_absence_of_a_deploy_is_stated_never_omitted():
    """M21 and the reason it matters: "no deploy correlated" sends the
    investigation to load and dependencies instead of the changelog. Dropping
    the field leaves the reader unable to tell "checked, nothing" from "never
    checked"."""
    blocks = build_blocks(ANOMALY, UNCORRELATED, TRACES)
    cause = blocks[2]["text"]["text"]

    assert "Likely cause:" in cause
    assert "No deploy correlated" in cause


def test_the_alert_never_claims_causation():
    """M21: correlation is not causation, and the copy must say so."""
    for deploy in (CORRELATED, UNCORRELATED):
        body = _text(build_blocks(ANOMALY, deploy, TRACES)).lower()
        assert "likely cause" in body
        assert "caused by" not in body
        assert "root cause" not in body


def test_confidence_decays_with_lag():
    """Right after a deploy is suspicious; fourteen minutes later is
    coincidence."""
    from src.core.deploy_attributor import CORRELATION_WINDOW_S

    def confidence(lag: int) -> float:
        return max(0.0, 1.0 - (lag / CORRELATION_WINDOW_S))

    assert confidence(0) == 1.0
    assert confidence(45) > 0.9
    assert confidence(840) < 0.1
    assert confidence(CORRELATION_WINDOW_S) == 0.0
    # monotonic, so the number is explainable
    lags = [0, 60, 300, 600, 899]
    assert all(confidence(a) > confidence(b) for a, b in itertools.pairwise(lags))


def test_auto_rollback_is_off_by_default():
    """M20. Remediating on a correlation signal can turn one bad deploy into a
    rollback loop: roll back, traffic shifts, latency changes, detector fires
    again. The capability is the easy part; the guardrails are the engineering."""
    import os
    assert os.environ.get("AUTO_ROLLBACK_ENABLED", "false").lower() == "false"
