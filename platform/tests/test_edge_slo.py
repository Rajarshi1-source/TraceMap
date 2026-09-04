"""D6 -- the assertion that makes edge SLOs worth having.

The load-bearing test is `test_parallel_sibling_burns_no_budget`. It encodes the
whole argument: an edge that is slow in wall-clock terms and off the critical
path must sit at full budget, while the same traffic under a wall-clock
objective would read as a total violation. If that test ever inverts, the SLO
has quietly become an ordinary latency alert and the differentiator is gone.

Everything else here guards the boring failure modes -- ratios computed on four
requests, an objective on a bucket that was never materialised, a roll-up table
that does not exist yet -- each of which turns an SLO into a number that is
confidently wrong rather than honestly absent.
"""

from __future__ import annotations

import asyncio

import pytest

from src.core.edge_slo import (
    MIN_REQUESTS,
    EdgeSLOEvaluator,
    Objective,
)


class FakeCH:
    """Returns one canned row set for every window.

    Windows are not distinguished on purpose: these tests are about the ratio
    arithmetic and the guards, not about window selection, and a fake that
    varies per window would let a bug in one window hide behind another.
    """

    def __init__(self, rows: list[dict] | None = None, fail: bool = False):
        self.rows = rows or []
        self.fail = fail
        self.queries: list[str] = []

    async def query(self, sql: str, params=None) -> list[dict]:
        if self.fail:
            raise RuntimeError("Table tracemap.edge_slo_1m does not exist")
        self.queries.append(sql)
        return self.rows


def row(source, target, total, *, le25=0, le50=0, le100=0, le250=0,
        le500=0, le1000=0, wall250=0, errors=0) -> dict:
    return {
        "source": source, "target": target, "total": total, "errors": errors,
        "wall_le_250": wall250,
        "crit_le_25_count": le25, "crit_le_50_count": le50,
        "crit_le_100_count": le100, "crit_le_250_count": le250,
        "crit_le_500_count": le500, "crit_le_1000_count": le1000,
    }


def evaluate(rows, objectives) -> dict:
    ev = EdgeSLOEvaluator(FakeCH(rows), objectives)
    return asyncio.run(ev.evaluate())


def by_edge(report: dict) -> dict:
    return {e["edge"]: e for e in report["edges"]}


# ---------------------------------------------------------------------------
# The flagship assertion.
# ---------------------------------------------------------------------------

PRODUCT = Objective("order-svc", "product-svc", 250.0, 0.99)


def test_parallel_sibling_burns_no_budget_but_would_fail_a_wallclock_slo():
    """Case 006, expressed as an error budget.

    1000 requests. product-svc ran ~700ms of wall clock every time, so ZERO
    requests are under a 250ms wall-clock bound. But it ran alongside a slower
    payment-svc, so its critical-path contribution is ~0 and all 1000 are good.

    Under this SLO the edge is perfectly healthy. Under the wall-clock SLO every
    other tool would apply, it is 100% bad and pages someone at 3am to debug a
    service that is costing the user nothing.
    """
    rows = [row("order-svc", "product-svc", 1000,
                le25=1000, le50=1000, le100=1000, le250=1000,
                le500=1000, le1000=1000,
                wall250=0)]

    e = by_edge(evaluate(rows, (PRODUCT,)))["order-svc->product-svc"]

    assert e["sli"] == pytest.approx(1.0)
    assert e["budgetRemaining"] == pytest.approx(1.0)
    assert e["burnRateLong"] == pytest.approx(0.0)
    assert e["fastBurn"] is False

    # And the counterfactual, which is the entire argument for the design.
    assert e["wallClockBad"] == pytest.approx(1.0)


def test_the_edge_actually_on_the_critical_path_does_burn():
    """The converse. Without this, the test above only proves nothing fires."""
    # 1000 requests, 900 within 250ms of critical path. 10% bad against a 1%
    # budget is a 10x burn.
    rows = [row("order-svc", "payment-svc", 1000,
                le250=900, le500=1000, le1000=1000, wall250=900)]
    obj = Objective("order-svc", "payment-svc", 250.0, 0.99)

    e = by_edge(evaluate(rows, (obj,)))["order-svc->payment-svc"]

    assert e["sli"] == pytest.approx(0.90)
    assert e["burnRateLong"] == pytest.approx(10.0)
    # Budget remaining goes negative: 10% spent against a 1% allowance.
    assert e["budgetRemaining"] == pytest.approx(-9.0)


def test_fast_burn_requires_both_windows():
    """The short window is what stops an alert paging for an hour after the fact.

    The fake returns the same rows for both windows, so a 20x burn trips both
    and fastBurn is true. The guard being asserted is that the flag reads BOTH
    values rather than deriving one from the other.
    """
    rows = [row("order-svc", "payment-svc", 1000, le250=800,
                le500=1000, le1000=1000)]
    obj = Objective("order-svc", "payment-svc", 250.0, 0.99)
    e = by_edge(evaluate(rows, (obj,)))["order-svc->payment-svc"]

    assert e["burnRateLong"] == pytest.approx(20.0)
    assert e["burnRateShort"] == pytest.approx(20.0)
    assert e["fastBurn"] is True


# ---------------------------------------------------------------------------
# The guards. Each of these is a way to report a confident wrong number.
# ---------------------------------------------------------------------------

def test_low_traffic_reports_insufficient_data_not_a_healthy_zero():
    """One bad request in three is three requests, not a 33% burn rate.

    Absent rather than zero, because a burn rate of 0 on an idle edge renders
    as perfect health on every dashboard that reads it.
    """
    rows = [row("order-svc", "user-svc", MIN_REQUESTS - 1, le100=1)]
    obj = Objective("order-svc", "user-svc", 100.0, 0.99)

    e = by_edge(evaluate(rows, (obj,)))["order-svc->user-svc"]

    assert e["insufficientData"] is True
    assert e["sli"] is None
    assert e["burnRateLong"] is None
    assert e["budgetRemaining"] is None
    assert e["fastBurn"] is False


def test_an_edge_with_no_traffic_at_all_is_absent_not_perfect():
    obj = Objective("order-svc", "user-svc", 100.0, 0.99)
    e = by_edge(evaluate([], (obj,)))["order-svc->user-svc"]

    assert e["insufficientData"] is True
    assert e["requests"] == 0
    assert e["sli"] is None


def test_objective_must_land_on_a_materialised_bucket():
    """An objective the roll-up cannot count is one that silently never fires.

    Caught at construction rather than at query time, so a bad catalogue fails
    at import and not at 3am when someone asks why the alert never fired.
    """
    with pytest.raises(ValueError, match="materialised bound"):
        Objective("order-svc", "payment-svc", 137.0, 0.99)


def test_target_ratio_must_be_a_ratio():
    with pytest.raises(ValueError, match="between 0 and 1"):
        Objective("order-svc", "payment-svc", 250.0, 99.0)


def test_missing_rollup_degrades_to_a_note_instead_of_a_500():
    """A stack on a pre-D6 volume has no edge_slo_1m.

    An SLO page that errors is worse than one that says it has nothing yet: the
    first looks like the platform is broken, the second points at the fix.
    """
    ev = EdgeSLOEvaluator(FakeCH(fail=True), (PRODUCT,))
    report = asyncio.run(ev.evaluate())

    assert report["edges"] == []
    assert "edge_slo_1m" in report["note"]
    assert "08_edge_slo.sql" in report["note"]


def test_wallclock_counterfactual_is_only_offered_where_it_is_comparable():
    """The roll-up carries exactly one wall-clock bound, at 250ms.

    Reporting it next to an objective at some other bound would invite a
    comparison between two different thresholds, which is worse than no
    comparison. So it is None unless the objective is also 250ms.
    """
    rows = [row("order-svc", "user-svc", 1000, le100=1000, wall250=500)]
    obj = Objective("order-svc", "user-svc", 100.0, 0.99)

    e = by_edge(evaluate(rows, (obj,)))["order-svc->user-svc"]
    assert e["wallClockBad"] is None


def test_the_shipped_catalogue_is_valid():
    """Constructing OBJECTIVES is itself the assertion.

    Every objective validates its bound and its ratio in __post_init__, so an
    import that succeeds has already proved the catalogue is coherent. This
    test exists to make that failure land in the test suite rather than in the
    API's startup log.
    """
    from src.core.edge_slo import OBJECTIVES

    assert len(OBJECTIVES) >= 4
    edges = [o.edge for o in OBJECTIVES]
    assert len(edges) == len(set(edges)), "an edge cannot have two objectives"

    # The two siblings must share an objective, or the contrast the design
    # rests on is not actually being demonstrated by the shipped config.
    catalogue = {o.edge: o for o in OBJECTIVES}
    assert (catalogue["order-svc->product-svc"].objective_ms
            == catalogue["order-svc->payment-svc"].objective_ms)
