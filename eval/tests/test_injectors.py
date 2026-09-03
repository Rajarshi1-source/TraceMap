"""Injector contract tests -- M29, made mechanical.

A leaked fault is the most expensive bug this directory can produce, and it is
silent: the next case simply measures the previous case's fault and the
scoreboard blames the detector. The contract that prevents it is small enough
to test exhaustively, so it is.
"""

from __future__ import annotations

import pytest

from eval.injectors import get_injector
from eval.injectors.base import FaultInjector, InjectorError
from eval.injectors.composite import CompositeInjector


class RecordingInjector(FaultInjector):
    """A FaultInjector with the network removed."""

    family = "recording"

    def __init__(self, spec, fail_on_start=False, fail_on_stop=False):
        super().__init__(spec)
        self.starts = 0
        self.stops = 0
        self.fail_on_start = fail_on_start
        self.fail_on_stop = fail_on_stop

    def payload(self):
        return {"delay_ms": 1}

    async def start(self):
        self.starts += 1
        if self.fail_on_start:
            raise InjectorError("boom")
        self._started = True

    async def stop(self):
        self.stops += 1
        if self.fail_on_stop:
            raise RuntimeError("cleanup exploded")
        self._started = False


SPEC = {"type": "latency", "target": "payment-svc", "delay_ms": 60,
        "duration_s": 300}


def test_every_declared_type_constructs():
    for kind, spec in (
        ("latency", {"type": "latency", "target": "payment-svc", "delay_ms": 60}),
        ("error", {"type": "error", "target": "payment-svc", "error_rate": 0.1}),
        ("saturation", {"type": "saturation", "target": "user-svc",
                        "cpu_burn_ms": 150}),
        ("dep_kill", {"type": "dep_kill", "target": "payment-svc"}),
        ("null", {"type": "null"}),
    ):
        inj = get_injector(spec)
        assert inj.name
        assert inj.describe()["family"] in (kind, "null", "composite")


def test_unknown_type_is_refused_loudly():
    with pytest.raises(InjectorError) as exc:
        get_injector({"type": "chaosmonkey", "target": "payment-svc"})
    assert "chaosmonkey" in str(exc.value)


def test_unknown_target_is_refused_before_the_case_runs():
    """`gateway-svc` has no control plane. Failing at construction beats
    failing eight minutes into a case with a warm stack behind it."""
    with pytest.raises(InjectorError) as exc:
        get_injector({"type": "latency", "target": "gateway-svc",
                      "delay_ms": 60})
    assert "gateway-svc" in str(exc.value)


async def test_stop_is_idempotent():
    """It is called from `finally`, and on a crash path possibly twice."""
    inj = RecordingInjector(SPEC)
    await inj.start()
    await inj.stop()
    await inj.stop()
    await inj.stop()
    assert inj.stops == 3
    assert not inj._started


async def test_stop_before_start_is_safe():
    """Preflight can abort a case between construction and injection."""
    inj = RecordingInjector(SPEC)
    await inj.stop()
    assert inj.starts == 0


async def test_null_injector_stop_is_a_no_op_twice():
    inj = get_injector({"type": "null"})
    await inj.start()
    await inj.stop()
    await inj.stop()


async def test_composite_unwinds_a_partial_start():
    """The decoy applies two faults. Applying one and failing on the second
    leaves a fault nobody wrote down, which is worse than no fault at all."""
    good = RecordingInjector(SPEC)
    bad = RecordingInjector(SPEC, fail_on_start=True)

    comp = CompositeInjector.__new__(CompositeInjector)
    comp.parts = [good, bad]
    comp.spec = {}
    comp.name = "composite:test"

    with pytest.raises(InjectorError):
        await comp.start()
    assert good.stops == 1, "the part that DID start was not unwound"


async def test_composite_stops_every_part_even_when_one_raises():
    first = RecordingInjector(SPEC)
    exploding = RecordingInjector(SPEC, fail_on_stop=True)
    last = RecordingInjector(SPEC)

    comp = CompositeInjector.__new__(CompositeInjector)
    comp.parts = [first, exploding, last]
    comp.spec = {}
    comp.name = "composite:test"

    await comp.stop()
    assert first.stops == 1 and exploding.stops == 1 and last.stops == 1, \
        "one failing teardown must not strand the others"


def test_composite_refuses_a_single_part():
    """A one-part composite is a plain injector with extra indirection, and
    the decoy case depends on there being two."""
    with pytest.raises(ValueError):
        get_injector({"type": "composite", "parts": [SPEC]})


def test_ttl_backstop_covers_the_case():
    """The TTL is what heals the stack when the runner is killed.

    Set it shorter than the case and a long case heals itself mid-measurement;
    the fault is not leaked, it is worse -- it is intermittent.
    """
    inj = get_injector(SPEC)
    assert inj.ttl_s == SPEC["duration_s"]
    assert inj.ttl_s > 0
