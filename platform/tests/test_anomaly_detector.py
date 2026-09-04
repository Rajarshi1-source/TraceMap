"""Detector gates, with the M6 boiling-frog guard as the centrepiece.

Run against a tiny in-memory Redis stand-in rather than a container: these are
statements about the gate LOGIC, and they should fail in milliseconds on a
laptop with no infrastructure, so nobody is tempted to skip them.
"""

from __future__ import annotations

import pytest

from src.core.anomaly_detector import (
    AnomalyDetector,
    DetectorConfig,
    EdgeObservation,
)


class FakeRedis:
    """Just enough Redis: get/set/incr/expire/delete with NX semantics."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, k):
        return self.store.get(k)

    async def set(self, k, v, nx: bool = False, ex: int | None = None):
        if nx and k in self.store:
            return None
        self.store[k] = str(v)
        return True

    async def incr(self, k):
        self.store[k] = str(int(self.store.get(k, 0)) + 1)
        return int(self.store[k])

    async def expire(self, k, ttl):
        return True

    async def delete(self, k):
        self.store.pop(k, None)
        return 1


def obs(crit: float, *, requests: int = 500, wall: float | None = None):
    return EdgeObservation(
        source="a", target="b",
        critical_p99_ms=crit,
        wall_p99_ms=crit if wall is None else wall,
        request_count=requests,
    )


async def warm_up(d: AnomalyDetector, level: float = 100.0, cycles: int = 10):
    """Get past cold-start and warmup with healthy observations."""
    for _ in range(cycles):
        await d.evaluate(obs(level))


# ===========================================================================
# M6 - the one that matters
# ===========================================================================
@pytest.mark.asyncio
async def test_baseline_does_not_learn_from_anomalies():
    """The boiling-frog guard.

    If `_update_ewma` is ever moved out of the non-breach branch so it runs on
    every observation, the baseline chases the fault: delta shrinks back under
    the threshold and the detector goes SILENT on a service that is still
    broken. No error, no alert - it has just quietly stopped detecting.

    This test is the reason that edit cannot land unnoticed.
    """
    r = FakeRedis()
    d = AnomalyDetector(r)

    await warm_up(d, 100.0)
    before = float(await r.get("baseline:edge:a:b:cp99"))

    for _ in range(10):                       # sustained, unambiguous anomaly
        await d.evaluate(obs(400.0))

    after = float(await r.get("baseline:edge:a:b:cp99"))
    assert after == before, (
        f"boiling frog: baseline moved {before:.1f} -> {after:.1f} while the "
        "edge was anomalous. _update_ewma has escaped the healthy branch."
    )


@pytest.mark.asyncio
async def test_baseline_does_track_genuine_healthy_drift():
    """The complement, and the reason the EWMA exists at all: an edge that
    legitimately gets a little slower should move the baseline. A detector that
    never adapts is just a static threshold with extra steps."""
    r = FakeRedis()
    d = AnomalyDetector(r)

    await warm_up(d, 100.0)
    before = float(await r.get("baseline:edge:a:b:cp99"))

    for _ in range(15):                       # +20ms: real, but under threshold
        await d.evaluate(obs(120.0))

    after = float(await r.get("baseline:edge:a:b:cp99"))
    assert after > before + 5, "baseline failed to adapt to healthy drift"
    assert after <= 120.0


# ===========================================================================
# The five gates
# ===========================================================================
@pytest.mark.asyncio
async def test_gate1_low_traffic_edges_never_alert():
    """A p99 over four requests is noise wearing a number."""
    r = FakeRedis()
    d = AnomalyDetector(r)
    for _ in range(20):
        assert await d.evaluate(obs(5000.0, requests=4)) is None
    assert await r.get("baseline:edge:a:b:cp99") is None


@pytest.mark.asyncio
async def test_gate2_never_alerts_on_first_sight_of_an_edge():
    """A newly deployed service must not page someone merely for existing."""
    r = FakeRedis()
    d = AnomalyDetector(r)
    assert await d.evaluate(obs(9999.0)) is None
    assert float(await r.get("baseline:edge:a:b:cp99")) == 9999.0


@pytest.mark.asyncio
async def test_gate3_stays_quiet_while_the_baseline_is_cold():
    r = FakeRedis()
    d = AnomalyDetector(r, DetectorConfig(warmup_cycles=5))
    fired = [await d.evaluate(obs(100.0 if i == 0 else 400.0)) for i in range(5)]
    assert all(f is None for f in fired), "alerted before the baseline was warm"


@pytest.mark.asyncio
async def test_gate5_requires_sustained_breaches():
    """One bad minute is weather. Three consecutive is a trend."""
    r = FakeRedis()
    d = AnomalyDetector(r, DetectorConfig(sustained_cycles=3))
    await warm_up(d, 100.0)

    assert await d.evaluate(obs(400.0)) is None      # 1/3
    assert await d.evaluate(obs(400.0)) is None      # 2/3
    a = await d.evaluate(obs(400.0))                 # 3/3 -> fires
    assert a is not None
    assert a.sustained == 3


@pytest.mark.asyncio
async def test_three_of_five_tolerates_one_noisy_dip():
    """breach, breach, DIP, breach = 3 in 4 cycles -> alerts.

    Under the old "3 consecutive" rule this stayed silent, and that was the live
    failure: a real p99 dips below the threshold occasionally, the run resets,
    and a genuine fault goes undetected indefinitely. Three breached minutes out
    of four IS a sustained problem.
    """
    r = FakeRedis()
    d = AnomalyDetector(r)
    await warm_up(d, 100.0)

    assert await d.evaluate(obs(400.0)) is None      # 1 of 5
    assert await d.evaluate(obs(400.0)) is None      # 2 of 5
    assert await d.evaluate(obs(100.0)) is None      # dip - evidence retained
    fired = await d.evaluate(obs(400.0))             # 3 of 5 -> alert
    assert fired is not None


@pytest.mark.asyncio
async def test_an_isolated_blip_still_never_alerts():
    """The bar must not have been lowered: one bad minute is still weather."""
    r = FakeRedis()
    d = AnomalyDetector(r)
    await warm_up(d, 100.0)

    assert await d.evaluate(obs(400.0)) is None
    for _ in range(5):
        assert await d.evaluate(obs(100.0)) is None


@pytest.mark.asyncio
async def test_a_low_duty_flap_stays_under_the_bar():
    """One breach in every three cycles is "10010" - 2 of 5, under the bar.

    Note the deliberate consequence at the other end: an edge breaching every
    OTHER cycle reaches 3 of 5 and does alert. That is correct. Half the minutes
    over the threshold is a degradation, not noise, and a rule that stayed quiet
    through it would be tuned for a quiet pager rather than a working service.
    """
    r = FakeRedis()
    d = AnomalyDetector(r)
    await warm_up(d, 100.0)

    for i in range(12):
        fired = await d.evaluate(obs(400.0 if i % 3 == 0 else 100.0))
        assert fired is None, f"alerted on a 1-in-3 flap at cycle {i}"


@pytest.mark.asyncio
async def test_the_baseline_does_not_creep_during_a_noisy_fault():
    """The boiling frog arriving through NOISE rather than through the branch.

    A sustained fault produces occasional sub-threshold cycles. If each of those
    teaches the baseline, it walks up onto the fault one dip at a time and the
    detector goes quiet - M6 satisfied to the letter, defeated in practice. The
    baseline may only move when the whole recent window is clean.
    """
    r = FakeRedis()
    d = AnomalyDetector(r)
    await warm_up(d, 100.0)
    before = float(await r.get("baseline:edge:a:b:cp99"))

    # Establish the fault first so the window is dirty, THEN dip. The dips sit
    # at the healthy level, so any movement is the baseline being taught by a
    # sub-threshold cycle inside a known-bad run.
    for i in range(20):
        await d.evaluate(obs(100.0 if i % 3 == 2 else 400.0))

    after = float(await r.get("baseline:edge:a:b:cp99"))
    assert after == before, (
        f"baseline crept {before:.1f} -> {after:.1f} on the sub-threshold dips "
        "of a sustained fault"
    )


@pytest.mark.asyncio
async def test_gate6_dedup_means_one_alert_not_n():
    """SET NX EX is atomic, so N replicas evaluating the same edge produce one
    Slack message rather than N."""
    r = FakeRedis()
    a = AnomalyDetector(r)
    b = AnomalyDetector(r)                           # a second replica
    await warm_up(a, 100.0)

    for _ in range(2):
        await a.evaluate(obs(400.0))
    first = await a.evaluate(obs(400.0))
    second = await b.evaluate(obs(400.0))

    assert first is not None
    assert second is None, "both replicas alerted - dedup is not holding"


# ===========================================================================
# M7 - what is being measured
# ===========================================================================
@pytest.mark.asyncio
async def test_detection_is_on_critical_path_not_wall_clock():
    """A parallel edge can be enormously slow in wall clock and contribute
    nothing. Alerting on it is exactly the behaviour this project exists to
    improve on, so a huge wall-clock jump with a flat critical path must stay
    silent."""
    r = FakeRedis()
    d = AnomalyDetector(r)
    await warm_up(d, 100.0)

    for _ in range(6):
        fired = await d.evaluate(obs(100.0, wall=5000.0))   # wall 50x, crit flat
        assert fired is None, "alerted on wall-clock latency (M7)"


@pytest.mark.asyncio
async def test_the_alert_carries_what_the_slack_message_needs():
    r = FakeRedis()
    d = AnomalyDetector(r)
    await warm_up(d, 100.0)
    for _ in range(2):
        await d.evaluate(obs(400.0, wall=420.0))
    a = await d.evaluate(obs(400.0, wall=420.0))

    assert a is not None
    assert a.metric == "critical_p99"          # leads with the right metric
    assert a.edge == "a->b"
    assert a.delta_ms > 250
    assert a.wall_p99_ms == 420.0              # wall clock carried, secondary
    assert a.request_count == 500


@pytest.mark.asyncio
async def test_warmup_converges_away_from_an_unlucky_seed():
    """The baseline is seeded from a SINGLE first observation. If that sample is
    an outlier, alpha=0.2 cannot walk it off in five warmup cycles - and every
    millisecond of leftover inflation is subtracted from every future delta.

    Measured live: a seed on a 146ms outlier against a true ~110ms level left
    the baseline at 139.5ms. A real 65ms fault then presented as a 40ms delta,
    under the 50ms threshold, and went undetected. The detector was correct; the
    baseline was wrong.
    """
    r = FakeRedis()
    d = AnomalyDetector(r, DetectorConfig(warmup_cycles=5))

    await d.evaluate(obs(146.0))                  # unlucky seed
    for _ in range(5):                            # true level
        await d.evaluate(obs(110.0))

    baseline = float(await r.get("baseline:edge:a:b:cp99"))
    assert baseline < 118.0, (
        f"baseline armed at {baseline:.1f}ms against a true level of 110ms - "
        "that inflation is subtracted from every future delta"
    )


@pytest.mark.asyncio
async def test_a_60ms_fault_is_detectable_after_a_noisy_warmup():
    """End-to-end arithmetic of eval case 003, in a unit test.

    A +60ms injection on a ~110ms edge must still clear a 50ms threshold even
    when warmup saw an outlier. This is the regression guard for the live
    failure: it went undetected for 472 seconds.
    """
    r = FakeRedis()
    d = AnomalyDetector(r)

    await d.evaluate(obs(146.0))                  # unlucky seed
    for _ in range(6):
        await d.evaluate(obs(110.0))              # healthy, arms the detector

    fired = None
    for _ in range(4):                            # fault: level shifts to ~175
        fired = await d.evaluate(obs(175.0)) or fired

    assert fired is not None, "a 60ms fault on a 110ms edge failed to alert"
    assert fired.delta_ms > 50


# ===========================================================================
# The error path, added in Phase 7 because the eval harness found it missing
# ===========================================================================
#
# An injected 5xx returns FASTER than a success -- payment-svc raises before
# the database write -- so critical-path p99 does not move. A detector reading
# only latency is blind to an edge failing half its requests, and eval cases
# 007 and 008 scored as false negatives against a detector that was working
# exactly as designed on the metric it had.
def err_obs(rate: float, *, requests: int = 500, crit: float = 100.0):
    return EdgeObservation(
        source="a", target="b",
        critical_p99_ms=crit,
        wall_p99_ms=crit,
        request_count=requests,
        error_rate=rate,
    )


@pytest.mark.asyncio
async def test_error_rate_alerts_after_the_sustained_window():
    d = AnomalyDetector(FakeRedis())

    assert await d.evaluate_error_rate(err_obs(0.10)) is None, "one cycle is weather"
    assert await d.evaluate_error_rate(err_obs(0.10)) is None, "two is still weather"
    fired = await d.evaluate_error_rate(err_obs(0.10))

    assert fired is not None, "10% errors sustained for three cycles must alert"
    assert fired.metric == "error_rate"
    assert fired.observed_ms == pytest.approx(0.10)


@pytest.mark.asyncio
async def test_error_rate_is_invisible_to_the_latency_gate():
    """The reason this path exists at all.

    Identical latency, half the requests failing. The latency gate is right to
    stay silent -- nothing got slower -- and that is precisely why it cannot be
    the only gate.
    """
    d = AnomalyDetector(FakeRedis())
    for _ in range(10):
        await d.evaluate(err_obs(0.0, crit=100.0))

    latency_fired = None
    error_fired = None
    for _ in range(5):
        latency_fired = await d.evaluate(err_obs(0.50, crit=100.0)) or latency_fired
        error_fired = await d.evaluate_error_rate(err_obs(0.50, crit=100.0)) or error_fired

    assert latency_fired is None, "latency did not change; the latency gate is correct to be quiet"
    assert error_fired is not None, "50% of requests failing must reach someone"


@pytest.mark.asyncio
async def test_healthy_error_rate_never_alerts():
    """The null-case equivalent for the error path.

    A measured healthy floor of ~0.08% sits far below the 2% threshold. If this
    ever fires, the threshold is wrong, not the traffic.
    """
    d = AnomalyDetector(FakeRedis())
    for _ in range(20):
        assert await d.evaluate_error_rate(err_obs(0.0008)) is None


@pytest.mark.asyncio
async def test_error_rate_respects_the_request_floor():
    """An error rate over four requests is one bad request wearing a percentage."""
    d = AnomalyDetector(FakeRedis())
    for _ in range(5):
        assert await d.evaluate_error_rate(err_obs(1.0, requests=4)) is None


@pytest.mark.asyncio
async def test_error_and_latency_alerts_do_not_suppress_each_other():
    """Separate dedup keys, and the reason is a real failure mode.

    An edge that is both slow and failing is two incidents. Sharing a dedup key
    would mean whichever fired first silences the other for ten minutes, and
    the one that gets silenced is whichever the responder most needed.
    """
    d = AnomalyDetector(FakeRedis())
    for _ in range(10):
        await d.evaluate(err_obs(0.0, crit=100.0))

    latency_fired = None
    error_fired = None
    for _ in range(5):
        latency_fired = await d.evaluate(err_obs(0.30, crit=300.0)) or latency_fired
        error_fired = await d.evaluate_error_rate(err_obs(0.30, crit=300.0)) or error_fired

    assert latency_fired is not None and error_fired is not None
    assert {latency_fired.metric, error_fired.metric} == {"critical_p99", "error_rate"}


# ===========================================================================
# F37 - blame share. A p99 says how slow; it does not say whether it mattered.
# ===========================================================================
#
# The decoy case failed twice before this gate existed, and both times the
# detector was doing exactly what it was told. An edge is off the critical path
# AT p99 only if it fails to finish last on more than 99% of traces, and
# fan-out finish order has real variance -- so injecting enough delay to breach
# a wall-clock threshold also pushes a sibling's last-finisher rate toward 1%.
# Measured live: user-svc went from a critical p99 of exactly 0.0 to 135.4ms
# while owning 0.02% of the critical path, and the detector alerted on it.
def blame_obs(crit: float, share: float, *, requests: int = 500):
    return EdgeObservation(
        source="a", target="b",
        critical_p99_ms=crit,
        wall_p99_ms=crit,
        request_count=requests,
        blame_share=share,
    )


@pytest.mark.asyncio
async def test_a_negligible_edge_never_alerts_however_slow_its_p99():
    """The decoy, in a unit test.

    user-svc's real numbers: baseline critical p99 0.0, faulted 135.4ms, blame
    share 0.0002. Removing this edge entirely would not be noticed, so blaming
    it for a user-visible regression is wrong no matter what the percentile says.
    """
    d = AnomalyDetector(FakeRedis())
    for _ in range(10):
        await d.evaluate(blame_obs(0.0, 0.0002))

    fired = None
    for _ in range(6):
        fired = await d.evaluate(blame_obs(135.4, 0.0002)) or fired

    assert fired is None, (
        "alerted on an edge owning 0.02% of the critical path -- this is F37, "
        "and it is the failure the decoy case exists to catch")


@pytest.mark.asyncio
async def test_the_culprit_still_alerts():
    """The other half. A gate that suppresses everything is not a fix.

    payment-svc under the same decoy: blame share 0.434.
    """
    d = AnomalyDetector(FakeRedis())
    for _ in range(10):
        await d.evaluate(blame_obs(155.0, 0.434))

    fired = None
    for _ in range(6):
        fired = await d.evaluate(blame_obs(300.0, 0.434)) or fired

    assert fired is not None, "the edge that actually owns the critical path must alert"
    assert fired.delta_ms > 50


@pytest.mark.asyncio
async def test_blame_gate_does_not_touch_the_baseline():
    """M6 is unchanged by F37.

    The gate sits AFTER the sustained window and before dedup, so it can only
    suppress an alert -- it must never alter what the baseline learns. If a
    suppressed edge later becomes genuinely critical, its baseline has to still
    be the healthy one, or the fix would have introduced a boiling frog of its
    own.
    """
    r = FakeRedis()
    d = AnomalyDetector(r)
    for _ in range(10):
        await d.evaluate(blame_obs(100.0, 0.001))
    before = float(await r.get("baseline:edge:a:b:cp99"))

    for _ in range(10):
        await d.evaluate(blame_obs(400.0, 0.001))
    after = float(await r.get("baseline:edge:a:b:cp99"))

    assert after == before, (
        "the blame gate changed what the baseline learned; it must only "
        "suppress the alert (M6)")


@pytest.mark.asyncio
async def test_an_observation_without_blame_share_is_not_suppressed():
    """A missing signal must not look like an innocent edge.

    `blame_share` defaults to 1.0 so any caller that has not been updated keeps
    alerting rather than silently going quiet -- the failure mode that is
    hardest to notice is the one where the detector stops talking.
    """
    d = AnomalyDetector(FakeRedis())
    for _ in range(10):
        await d.evaluate(obs(100.0))
    fired = None
    for _ in range(6):
        fired = await d.evaluate(obs(300.0)) or fired
    assert fired is not None
