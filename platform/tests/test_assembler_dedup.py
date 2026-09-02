"""F2 guard - a duplicated span_id must not corrupt the assembler.

Span ingestion is at-least-once, and the project deliberately ACCEPTS duplicate
spans in raw storage: dedup costs merge CPU forever and a duplicate moves a p99
imperceptibly. That reasoning is sound for percentiles over raw spans and does
NOT survive into the derived path, which is where these tests live.

Reproduced during the design audit: a trace containing two spans sharing a
span_id produced

    {('gw', 'svc-b'): 85.0}

with the ('gw','svc-a') edge gone entirely. Two distinct failures, both silent:

  1. `by_id` is keyed on span_id, so the last copy wins and a span's service and
     timings are replaced - blame moves to a different edge.
  2. the edge loop iterates spans, so a duplicate emits TWO rows into
     trace_edges, double-counting the edge and feeding the TDigest twice.

The primary fix is argMax(..., timestamp) + GROUP BY trace_id, span_id in the
fetch query. These tests exercise the second line of defence in rows_to_traces,
so a future edit to that SQL cannot quietly reintroduce the bug.
"""

from __future__ import annotations

from workers.trace_assembler_worker import assemble_trace, rows_to_traces

MS = 1_000_000


def _row(span_id, parent, service, op, start_ms, end_ms, *, is_error=0, version="v1"):
    return {
        "trace_id": "t1",
        "span_id": span_id,
        "parent_span_id": parent,
        "service_name": service,
        "operation_name": op,
        "start_time_unix_ns": int(start_ms * MS),
        "end_time_unix_ns": int(end_ms * MS),
        "is_error": is_error,
        "service_version": version,
        "span_kind": "SERVER",
    }


FANOUT = [
    _row("root", "",     "gateway-svc", "POST /checkout", 0,  750),
    _row("ord",  "root", "order-svc",   "createOrder",    5,  740),
    _row("usr",  "ord",  "user-svc",    "getUser",        10, 50),
    _row("prd",  "ord",  "product-svc", "checkStock",     10, 710),
    _row("pay",  "ord",  "payment-svc", "charge",         10, 730),
]


def _edges(rows):
    traces = rows_to_traces(rows)
    edge_rows, _ = assemble_trace("t1", traces["t1"])
    return edge_rows


def test_clean_trace_produces_one_row_per_edge():
    edge_rows = _edges(FANOUT)
    assert len(edge_rows) == 4
    pairs = sorted((e["source_service"], e["target_service"]) for e in edge_rows)
    assert pairs == [
        ("gateway-svc", "order-svc"),
        ("order-svc", "payment-svc"),
        ("order-svc", "product-svc"),
        ("order-svc", "user-svc"),
    ]


def test_duplicate_span_id_produces_exactly_one_row_per_edge():
    """The whole point: an at-least-once redelivery must not double-count."""
    duplicated = FANOUT + [
        _row("pay", "ord", "payment-svc", "charge", 10, 730),  # exact redelivery
    ]
    edge_rows = _edges(duplicated)

    assert len(edge_rows) == 4, "a duplicated span_id emitted an extra edge row"
    payment = [e for e in edge_rows if e["target_service"] == "payment-svc"]
    assert len(payment) == 1


def test_duplicate_span_id_does_not_move_blame():
    """Attribution must be byte-identical with and without the duplicate."""
    before = {(e["source_service"], e["target_service"]): e["critical_ms"]
              for e in _edges(FANOUT)}
    after = {(e["source_service"], e["target_service"]): e["critical_ms"]
             for e in _edges(FANOUT + [_row("pay", "ord", "payment-svc", "charge", 10, 730)])}

    assert before == after
    assert before[("order-svc", "payment-svc")] == 720.0
    assert before[("order-svc", "product-svc")] == 0.0


def test_the_original_corruption_no_longer_reproduces():
    """The exact shape found during the audit: two DIFFERENT spans sharing an
    id. Previously the svc-a edge disappeared completely."""
    rows = [
        _row("root", "",     "gw",    "GET /x", 0, 100),
        _row("dup",  "root", "svc-a", "first",  5, 90),
        _row("dup",  "root", "svc-b", "second", 5, 50),
    ]
    edge_rows = _edges(rows)
    # One span_id can only be one span. What must NOT happen is two rows, or a
    # row whose blame was silently reassigned without the count changing.
    assert len(edge_rows) == 1
    targets = {e["target_service"] for e in edge_rows}
    assert len(targets) == 1


def test_zero_blame_edges_are_written_not_omitted():
    """M5 - the trap this phase is most likely to fall into.

    A parallel sibling has real traffic, real wall-clock latency and a real
    error rate. What it lacks is blame. Dropping the row would make the edge
    VANISH from the dependency map instead of fading, which is a lie: the call
    really did happen.
    """
    edge_rows = _edges(FANOUT)
    by_target = {e["target_service"]: e for e in edge_rows}

    assert by_target["product-svc"]["critical_ms"] == 0.0
    assert by_target["product-svc"]["duration_ms"] == 700.0   # real, and large
    assert by_target["user-svc"]["critical_ms"] == 0.0
    assert by_target["payment-svc"]["critical_ms"] == 720.0


def test_critical_path_row_summarises_the_trace():
    traces = rows_to_traces(FANOUT)
    _, path_rows = assemble_trace("t1", traces["t1"])
    assert len(path_rows) == 1
    row = path_rows[0]
    assert row["root_service"] == "gateway-svc"
    assert abs(row["total_duration_ms"] - 750.0) < 0.01
    assert row["path_length"] >= 1


# ---------------------------------------------------------------------------
# F12 - a child span that outlives its parent silently deletes its subtree.
# ---------------------------------------------------------------------------
def test_child_outliving_its_parent_does_not_erase_the_subtree():
    """Observed on the real demo path, not invented.

    Express closes its `request handler` span a tick AFTER the HTTP server span
    that contains it:

        order-svc POST (SERVER)              [ 5.0 .. 122.6]
          order-svc request handler /orders  [ 6.0 .. 122.8]   <-- 0.2ms late

    _walk skips any child ending after the cursor, because that is exactly how
    it recognises parallel work. So a child overrunning its parent by a fraction
    of a millisecond takes its WHOLE SUBTREE with it - payment, product and user
    all scored 0.0, on the one trace this project exists to get right. Nothing
    errored; the flagship number just quietly became zero.
    """
    rows = [
        _row("root",  "",      "gateway-svc", "POST",            0,  126),
        _row("gwh",   "root",  "gateway-svc", "handler",         1,  125),
        _row("ordsv", "gwh",   "order-svc",   "POST",            5,  122),
        # 0.6ms past its parent - the instrumentation artifact
        _row("ordh",  "ordsv", "order-svc",   "request handler", 6,  122.6),
        _row("cpay",  "ordh",  "order-svc",   "POST",            8,  113),
        _row("pay",   "cpay",  "payment-svc", "POST /charge",    14, 112),
        _row("cprd",  "ordh",  "order-svc",   "POST",            8,  60),
        _row("prd",   "cprd",  "product-svc", "POST /stock",     13, 59),
    ]
    edge_rows = _edges(rows)
    blame = {(e["source_service"], e["target_service"]): e["critical_ms"]
             for e in edge_rows}

    assert blame[("order-svc", "payment-svc")] > 90, (
        "the subtree was erased by a child overrunning its parent - "
        f"got {blame[('order-svc', 'payment-svc')]}"
    )
    # product still runs in parallel and still gets zero, with its row present.
    assert blame[("order-svc", "product-svc")] == 0.0
    assert ("order-svc", "product-svc") in blame


def test_normalisation_leaves_well_formed_traces_untouched():
    """The clamp must be a no-op on a trace that already nests correctly,
    otherwise it would be quietly rewriting good data too."""
    from workers.trace_assembler_worker import normalize_spans

    traces = rows_to_traces(FANOUT)
    original = sorted(traces["t1"], key=lambda s: s.span_id)
    normalised = sorted(normalize_spans(traces["t1"]), key=lambda s: s.span_id)
    assert original == normalised
