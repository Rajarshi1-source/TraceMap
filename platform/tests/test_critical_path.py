"""The six critical-path tests. THIS FILE IS THE SPECIFICATION.

Written before the worker, on purpose. The first test states the thesis of the
whole project in a dozen lines, and it is the one test a reader of this
repository should read.

The semantics that trip people up, encoded here so they cannot be "tidied" away:

  * Parallel edges are KEPT with critical_ms == 0.0, never omitted (M5). A test
    asserting ("a","b") not in edges for a parallel sibling is WRONG. The map
    still has to draw an edge that carries real traffic, real wall-clock latency
    and a real error rate - what it lacks is blame, and a zero row is what lets
    the UI fade it out instead of making it vanish.

  * A parent edge absorbs its whole subtree. gateway->order is 735.0, not 15.0:
    the gateway really did wait 735ms on order.

  * Segments are contiguous and non-overlapping, so they sum exactly to the root
    duration. That invariant is the cheapest regression test on the walk.
"""

from __future__ import annotations

from src.core.critical_path import (
    Span,
    attribute_to_edges,
    compute_critical_path,
)

MS = 1_000_000


def test_parallel_fanout_blames_only_the_slowest_sibling():
    """order fans out to three services concurrently. Only the one that
    finishes last is on the critical path.

    Making product-svc infinitely fast would save the user ZERO milliseconds,
    yet every OSS service map paints its 700ms edge red and sends you there.
    That gap is the entire reason this project exists.
    """
    spans = [
        Span("root", "",     "gateway-svc", "POST /checkout", 0,        750 * MS),
        Span("ord",  "root", "order-svc",   "createOrder",    5  * MS,  740 * MS),
        Span("usr",  "ord",  "user-svc",    "getUser",        10 * MS,  50  * MS),
        Span("prd",  "ord",  "product-svc", "checkStock",     10 * MS,  710 * MS),
        Span("pay",  "ord",  "payment-svc", "charge",         10 * MS,  730 * MS),
    ]
    edges = attribute_to_edges(spans, compute_critical_path(spans))

    assert edges[("order-svc", "payment-svc")] == 720.0   # 100% of the blame
    assert edges[("order-svc", "product-svc")] == 0.0     # 700ms wall clock -> 0ms
    assert edges[("order-svc", "user-svc")] == 0.0
    assert edges[("gateway-svc", "order-svc")] == 735.0   # order's whole subtree

    # M5: the zero-blame edges must be PRESENT, not missing.
    assert ("order-svc", "product-svc") in edges
    assert ("order-svc", "user-svc") in edges


def test_critical_path_covers_the_root_span_exactly():
    """Invariant: segments are contiguous and non-overlapping, so they sum to
    the root duration. If this breaks, the walk has developed a gap or a
    double-count."""
    spans = [
        Span("root", "",     "gateway-svc", "POST /checkout", 0,       750 * MS),
        Span("ord",  "root", "order-svc",   "createOrder",    5 * MS,  740 * MS),
        Span("pay",  "ord",  "payment-svc", "charge",         10 * MS, 730 * MS),
    ]
    assert abs(sum(s.ms for s in compute_critical_path(spans)) - 750.0) < 0.01


def test_serial_chain_attributes_every_hop():
    spans = [
        Span("root", "",     "gateway-svc", "POST /checkout", 0,       300 * MS),
        Span("ord",  "root", "order-svc",   "createOrder",    10 * MS, 290 * MS),
        Span("pay",  "ord",  "payment-svc", "charge",         20 * MS, 280 * MS),
    ]
    edges = attribute_to_edges(spans, compute_critical_path(spans))
    assert edges[("gateway-svc", "order-svc")] == 280.0
    assert edges[("order-svc", "payment-svc")] == 260.0


def test_sequential_siblings_both_count():
    """Not-parallel siblings: both are on the path, both get blame."""
    spans = [
        Span("root", "",     "gw",    "GET /x", 0,         300 * MS),
        Span("a",    "root", "svc-a", "a",      10  * MS,  150 * MS),
        Span("b",    "root", "svc-b", "b",      150 * MS,  290 * MS),
    ]
    edges = attribute_to_edges(spans, compute_critical_path(spans))
    assert abs(edges[("gw", "svc-a")] - 140.0) < 0.01
    assert abs(edges[("gw", "svc-b")] - 140.0) < 0.01


def test_partial_overlap_excludes_the_overlapped_sibling():
    """b starts before a finishes and ends last, so a contributes nothing."""
    spans = [
        Span("root", "",     "gw",    "GET /x", 0,        300 * MS),
        Span("a",    "root", "svc-a", "a",      10  * MS, 200 * MS),
        Span("b",    "root", "svc-b", "b",      100 * MS, 290 * MS),
    ]
    edges = attribute_to_edges(spans, compute_critical_path(spans))
    assert edges[("gw", "svc-b")] == 190.0
    assert edges[("gw", "svc-a")] == 0.0


def test_orphan_root_survives_a_dropped_parent():
    """Sampling or loss can remove the true root. Take the earliest orphan
    rather than returning nothing - a partial answer beats a blank map."""
    spans = [
        Span("ord", "MISSING", "order-svc",   "createOrder", 5  * MS, 740 * MS),
        Span("pay", "ord",     "payment-svc", "charge",      10 * MS, 730 * MS),
    ]
    edges = attribute_to_edges(spans, compute_critical_path(spans))
    assert edges[("order-svc", "payment-svc")] == 720.0
