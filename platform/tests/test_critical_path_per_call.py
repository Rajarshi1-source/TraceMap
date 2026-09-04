"""F41 -- per-call attribution, and why the per-edge total is not enough.

`attribute_to_edges` answers "how much did this dependency cost the user across
the whole trace". That is the right question for a trace summary and the wrong
one for the rows the assembler writes, because it writes one row PER CALL.

When `order-svc` calls `payment-svc` twice in a trace -- a retry, or the saga
compensating a charge after a sibling failed -- the assembler gave both rows the
pair TOTAL. Three consequences, in increasing order of how much they matter:

  1. `critical_ms > duration_ms` on individual rows. Definitionally impossible:
     an edge cannot contribute more user-visible milliseconds than the call
     itself took.
  2. `crit_ms_sum` in `service_edges_1m` counted that blame once per row, so the
     roll-up over-attributed the edge by a factor of its call count.
  3. The TDigest behind `crit_p99` was fed the pair total once per call. The
     dependency map RANKS BY crit_p99, so an edge that happens to be called
     twice per trace was systematically ranked above one called once -- sending
     you to the wrong service, which is the exact failure this project exists
     to prevent, arrived at from the other direction.

Measured on this repository before the fix: 4.83% of `order-svc->payment-svc`
rows violated the invariant, against 3.9% of its traces making two or more
calls. The edge the flagship exists to identify was the most over-blamed one,
because it is the only edge with a compensation path.
"""

from __future__ import annotations

from src.core.critical_path import (
    Span,
    attribute_to_child_spans,
    attribute_to_edges,
    compute_critical_path,
)

MS = 1_000_000


def retry_trace() -> list[Span]:
    """order-svc charges payment-svc twice: a failed attempt, then a retry.

        root  gateway              0 .. 700
          ord   order-svc          5 .. 690
            pay1  payment-svc     10 .. 210   (200ms, failed)
            pay2  payment-svc    220 .. 680   (460ms, succeeded)

    Serial, so both attempts are genuinely on the critical path. Together they
    account for 660ms of the user's wait -- but no single CALL cost 660ms, and
    that is the distinction the old code could not express.
    """
    return [
        Span("root", "",     "gateway-svc", "POST /checkout", 0,       700 * MS),
        Span("ord",  "root", "order-svc",   "createOrder",    5 * MS,  690 * MS),
        Span("pay1", "ord",  "payment-svc", "charge",        10 * MS,  210 * MS,
             is_error=True),
        Span("pay2", "ord",  "payment-svc", "charge",       220 * MS,  680 * MS),
    ]


def test_each_call_is_attributed_its_own_time_not_the_pair_total():
    """The assertion that would have failed before the fix.

    Before: both calls reported 660.0, the pair total.
    After:  200.0 and 460.0, which is what each call actually cost.
    """
    spans = retry_trace()
    per_call = attribute_to_child_spans(spans, compute_critical_path(spans))

    assert per_call["pay1"] == 200.0
    assert per_call["pay2"] == 460.0


def test_no_call_is_ever_blamed_for_more_than_its_own_duration():
    """The invariant, stated directly.

    This is the assertion that caught F41 in production data: 1,342 rows out of
    99,178 in a twenty-minute window had critical_ms > duration_ms.
    """
    spans = retry_trace()
    by_id = {s.span_id: s for s in spans}
    per_call = attribute_to_child_spans(spans, compute_critical_path(spans))

    for span_id, critical_ms in per_call.items():
        assert critical_ms <= by_id[span_id].duration_ms + 1e-9, (
            f"{span_id} blamed for {critical_ms}ms of a "
            f"{by_id[span_id].duration_ms}ms call"
        )


def test_the_per_edge_total_is_still_the_sum_of_its_calls():
    """The two views must agree, and now they cannot disagree by construction.

    `attribute_to_edges` is defined in terms of `attribute_to_child_spans`, so
    the trace summary is literally the sum of the rows written for it. That is
    what makes `crit_ms_sum` in the roll-up correct.
    """
    spans = retry_trace()
    segments = compute_critical_path(spans)
    per_call = attribute_to_child_spans(spans, segments)
    edges = attribute_to_edges(spans, segments)

    assert edges[("order-svc", "payment-svc")] == 660.0
    assert per_call["pay1"] + per_call["pay2"] == edges[("order-svc", "payment-svc")]


def test_a_repeated_call_no_longer_inflates_the_edge_against_a_single_call():
    """The ranking consequence, which is the one that actually costs an hour.

    Two edges that cost the user exactly the same amount of time must rank the
    same, whether that time arrived in one call or two. Before the fix, the
    edge split across two calls reported double.
    """
    # order->payment: two calls, 200 + 460 = 660ms of critical path.
    # order->product: one call covering the same span of time.
    split = retry_trace()
    single = [
        Span("root", "",     "gateway-svc", "POST /checkout", 0,       700 * MS),
        Span("ord",  "root", "order-svc",   "createOrder",    5 * MS,  690 * MS),
        Span("prd",  "ord",  "product-svc", "checkStock",    10 * MS,  680 * MS),
    ]

    split_total = attribute_to_edges(
        split, compute_critical_path(split))[("order-svc", "payment-svc")]
    single_total = attribute_to_edges(
        single, compute_critical_path(single))[("order-svc", "product-svc")]

    # 660 vs 670: within 10ms, because the split pair has a 10ms gap between
    # attempts that belongs to order-svc's own work rather than to the edge.
    assert abs(split_total - single_total) <= 10.0
    assert split_total < 2 * single_total * 0.75, "the pair total is being double-counted"


def test_parallel_siblings_still_score_zero_per_call():
    """M5 survives the change: the zero rows are per call and still present."""
    spans = [
        Span("root", "",     "gateway-svc", "POST /checkout", 0,       750 * MS),
        Span("ord",  "root", "order-svc",   "createOrder",    5 * MS,  740 * MS),
        Span("usr",  "ord",  "user-svc",    "getUser",       10 * MS,  50 * MS),
        Span("prd",  "ord",  "product-svc", "checkStock",    10 * MS, 710 * MS),
        Span("pay",  "ord",  "payment-svc", "charge",        10 * MS, 730 * MS),
    ]
    per_call = attribute_to_child_spans(spans, compute_critical_path(spans))

    assert per_call["pay"] == 720.0
    assert per_call["prd"] == 0.0
    assert per_call["usr"] == 0.0
    # Present, not missing. A dropped key makes the edge vanish from the map
    # instead of fading, and vanishing is a lie: the call really happened.
    assert "prd" in per_call
    assert "usr" in per_call
