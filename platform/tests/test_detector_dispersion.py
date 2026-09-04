"""F43 -- the breach bar scales with each edge's own noise, not its magnitude.

A flat 50ms threshold makes the *noisiest* edge the twitchiest, which is
precisely backwards. `gateway-svc->order-svc` absorbs order-svc's whole subtree
by design, so it carries the largest baseline and the largest absolute jitter in
the graph -- and on a contended CI runner it false-positived on cases that
inject nothing at all:

    null_baseline        alert gateway-svc->order-svc  +178ms
    null_traffic_ramp    alert gateway-svc->order-svc  +234ms

while the edge that run was supposed to catch alerted at +109ms. No single
global number separates those. The numbers in this file are those measurements,
not invented ones, and `test_the_measured_ci_failure_is_separable` is the one
that states the whole point.

The bar is `max(threshold_ms, mad_multiplier * mad)`. Dispersion may only ever
RAISE it: a stable edge keeps the brief's 50ms and does not become
hypersensitive because its noise floor happens to be small.
"""

from __future__ import annotations

import pytest

from src.core.anomaly_detector import (
    AnomalyDetector,
    DetectorConfig,
    EdgeObservation,
)


class FakeRedis:
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

    async def delete(self, k):
        self.store.pop(k, None)
        return 1


def obs(crit: float, *, src="a", dst="b", requests: int = 500):
    return EdgeObservation(source=src, target=dst, critical_p99_ms=crit,
                           wall_p99_ms=crit, request_count=requests)


def detector(**kw) -> tuple[AnomalyDetector, FakeRedis]:
    r = FakeRedis()
    return AnomalyDetector(r, DetectorConfig(**kw)), r


async def arm(d: AnomalyDetector, r: FakeRedis, *, baseline: float,
              mad: float, src="a", dst="b") -> None:
    """Put an edge straight into the armed state with a chosen noise floor.

    Driving warmup with synthetic jitter would make every assertion below
    depend on the exact sequence fed in; setting the two learned values
    directly keeps each test about the gate it names.
    """
    r.store[d._k_base(src, dst)] = str(baseline)
    r.store[d._k_mad(src, dst)] = str(mad)
    r.store[d._k_warm(src, dst)] = str(d.cfg.warmup_cycles)


async def sustained(d: AnomalyDetector, o: EdgeObservation, cycles: int = 5):
    """Feed the same observation until the sustained window is satisfied.

    Returns the FIRST anomaly, not the last call's result: the alert fires on
    the cycle that completes the 3-of-5 window, and every later cycle is
    correctly swallowed by the dedup key. Returning the last result would make
    every one of these tests read as "no alert".
    """
    for _ in range(cycles):
        out = await d.evaluate(o)
        if out is not None:
            return out
    return None


# ---------------------------------------------------------------------------
# The measurement this exists for
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_measured_ci_failure_is_separable():
    """Both real numbers, one detector, opposite outcomes.

    gateway-svc->order-svc: +234ms of pure noise, nothing injected -> silent.
    order-svc->payment-svc: +109ms of real fault                   -> alerts.

    A single global threshold cannot do this: anything above 234 loses the 109.
    """
    d, r = detector()

    # The noisy parent. Its own jitter at rest is large, so its bar is high.
    await arm(d, r, baseline=450.0, mad=70.0, src="gateway-svc", dst="order-svc")
    noise = await sustained(d, obs(450.0 + 234.0, src="gateway-svc", dst="order-svc"))
    assert noise is None, "the null-case false positive is back"

    # The quiet child. Its jitter is small, so it keeps the 50ms floor.
    await arm(d, r, baseline=289.1, mad=8.0, src="order-svc", dst="payment-svc")
    real = await sustained(d, obs(289.1 + 109.0, src="order-svc", dst="payment-svc"))
    assert real is not None, "the real detection was lost"
    assert real.delta_ms == pytest.approx(109.0, abs=0.1)


# ---------------------------------------------------------------------------
# The bar itself
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_quiet_edge_is_exactly_as_sensitive_as_before():
    """No dispersion means no change: the brief's 50ms still applies."""
    d, r = detector()
    await arm(d, r, baseline=100.0, mad=0.0)

    assert await sustained(d, obs(100.0 + 49.0)) is None
    await arm(d, r, baseline=100.0, mad=0.0)
    assert await sustained(d, obs(100.0 + 51.0)) is not None


@pytest.mark.asyncio
async def test_dispersion_can_only_raise_the_bar_never_lower_it():
    """A very stable edge must not become hypersensitive.

    4 * 2ms = 8ms, which is far below the floor. If `max()` were ever changed
    to a plain multiplication, this edge would start alerting on 9ms of drift.
    """
    d, r = detector()
    await arm(d, r, baseline=100.0, mad=2.0)
    assert await sustained(d, obs(100.0 + 30.0)) is None


@pytest.mark.asyncio
async def test_the_bar_is_reported_on_the_anomaly():
    """'+58ms over baseline' is uninterpretable once the bar is per-edge.

    A responder has to be able to see why THIS edge needed more evidence than
    another one, so the effective threshold and the noise floor ride along.
    """
    d, r = detector()
    await arm(d, r, baseline=200.0, mad=30.0)          # bar = 120ms
    a = await sustained(d, obs(200.0 + 150.0))

    assert a is not None
    assert a.threshold_ms == pytest.approx(120.0)
    assert a.mad_ms == pytest.approx(30.0)


@pytest.mark.asyncio
async def test_a_noisy_edge_still_alerts_on_a_big_enough_signal():
    """Raising the bar must not make an edge undetectable.

    This is the risk the fix carries, and it is the reason the multiplier is
    4x and not 10x.
    """
    d, r = detector()
    await arm(d, r, baseline=450.0, mad=70.0)          # bar = 280ms
    assert await sustained(d, obs(450.0 + 400.0)) is not None


# ---------------------------------------------------------------------------
# M6, one derivative up
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispersion_does_not_learn_from_a_breaching_window():
    """The boiling frog applied to noise instead of to level.

    If |delta| were folded in on every observation, a noisy fault would raise
    its own bar until it stopped being detectable -- and nothing about the
    baseline would look wrong, which makes it harder to see than the original
    M6 failure.
    """
    d, r = detector()
    await arm(d, r, baseline=100.0, mad=5.0)
    before = float(r.store[d._k_mad("a", "b")])

    for _ in range(6):
        await d.evaluate(obs(100.0 + 300.0))

    after = float(r.store[d._k_mad("a", "b")])
    assert after == pytest.approx(before), (
        "dispersion learned from a breaching window; a sustained fault can now "
        "raise its own threshold out of range"
    )


@pytest.mark.asyncio
async def test_dispersion_learns_from_healthy_windows():
    """The converse: without this the bar never rises and F43 is not fixed."""
    d, r = detector()
    await arm(d, r, baseline=100.0, mad=0.0)

    # ALTERNATING deviation, not a constant offset. A constant offset is
    # something the baseline EWMA correctly learns, so |delta| decays to zero
    # and the noise floor with it -- which is right, and would make this test
    # measure the wrong thing. Jitter is what dispersion is supposed to track.
    for i in range(20):
        await d.evaluate(obs(100.0 + (40.0 if i % 2 else -40.0)))

    assert float(r.store[d._k_mad("a", "b")]) > 20.0


@pytest.mark.asyncio
async def test_warmup_establishes_the_noise_floor():
    """Otherwise the first armed cycle judges at the bare floor.

    That is exactly the pre-F43 behaviour, arriving at the moment the detector
    is first allowed to alert -- the worst possible time for it.
    """
    d, r = detector()
    r.store[d._k_base("a", "b")] = "100.0"

    for i in range(d.cfg.warmup_cycles):
        await d.evaluate(obs(100.0 + (60.0 if i % 2 else -60.0)))

    assert float(r.store.get(d._k_mad("a", "b"), 0)) > 10.0


@pytest.mark.asyncio
async def test_a_cold_edge_still_never_alerts_on_first_sight():
    """Gate 2 is untouched by any of this."""
    d, _ = detector()
    assert await d.evaluate(obs(500.0)) is None
