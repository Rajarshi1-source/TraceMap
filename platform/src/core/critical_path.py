"""Critical-path extraction over a single trace's span tree.

The critical path is the chain of spans that determines end-to-end latency.
Work that overlaps the critical path (parallel fan-out) contributes zero,
even if its wall-clock duration is large.

----------------------------------------------------------------------------
DO NOT "TIDY" THIS FILE.

It is reproduced verbatim from the design and is verified by the six tests in
platform/tests/test_critical_path.py. Three things look redundant and are not:

  * the `kid.end_ns <= span.start_ns` guard in _walk
  * the `cursor_ns = max(kid.start_ns, span.start_ns)` cursor move
  * the final `segments.reverse()`

Each is load-bearing for at least one test. Simplifying any of them produces
code that still runs, still returns plausible numbers, and is wrong.

Complexity is O(n log n), dominated by sorting children by end time.
----------------------------------------------------------------------------
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class Span:
    span_id: str
    parent_span_id: str
    service: str
    operation: str
    start_ns: int
    end_ns: int
    is_error: bool = False
    version: str = ""

    @property
    def duration_ms(self) -> float:
        return (self.end_ns - self.start_ns) / 1_000_000


@dataclass(slots=True, frozen=True)
class Segment:
    """A contiguous slice of the critical path owned by exactly one span."""

    span_id: str
    service: str
    operation: str
    start_ns: int
    end_ns: int

    @property
    def ms(self) -> float:
        return (self.end_ns - self.start_ns) / 1_000_000


def compute_critical_path(spans: list[Span]) -> list[Segment]:
    """Return the critical path as ordered, non-overlapping segments."""
    if not spans:
        return []

    by_id: dict[str, Span] = {s.span_id: s for s in spans}
    children: dict[str, list[Span]] = defaultdict(list)
    for s in spans:
        if s.parent_span_id and s.parent_span_id in by_id:
            children[s.parent_span_id].append(s)

    # Orphan-tolerant root selection: a trace may arrive with the true root
    # still in flight, or dropped by sampling. Take the earliest orphan.
    roots = [s for s in spans
             if not s.parent_span_id or s.parent_span_id not in by_id]
    if not roots:
        return []                      # cycle or corrupt trace: refuse to guess
    root = min(roots, key=lambda s: s.start_ns)

    segments: list[Segment] = []
    _walk(root, root.end_ns, children, segments)
    segments.reverse()                 # produced backwards; return chronological
    return segments


def _walk(span: Span,
          cursor_ns: int,
          children: dict[str, list[Span]],
          out: list[Segment]) -> None:
    """Walk one span backwards from cursor_ns to span.start_ns."""
    kids = sorted(children.get(span.span_id, []),
                  key=lambda c: c.end_ns, reverse=True)

    for kid in kids:
        # Skip anything that overlaps the region we've already attributed:
        # that work ran in parallel and is NOT on the critical path.
        if kid.end_ns > cursor_ns or kid.end_ns <= span.start_ns:
            continue

        # Gap between the child's end and the cursor = parent's own work.
        if cursor_ns > kid.end_ns:
            out.append(Segment(span.span_id, span.service, span.operation,
                               kid.end_ns, cursor_ns))

        _walk(kid, kid.end_ns, children, out)
        cursor_ns = max(kid.start_ns, span.start_ns)

        if cursor_ns <= span.start_ns:
            return

    if cursor_ns > span.start_ns:
        out.append(Segment(span.span_id, span.service, span.operation,
                           span.start_ns, cursor_ns))


def attribute_to_child_spans(spans: list[Span],
                             segments: list[Segment]) -> dict[str, float]:
    """Critical-path milliseconds per CROSS-SERVICE CHILD SPAN.

    One entry per call, keyed by the child span id -- not per service pair.
    A call's contribution is the critical-path time spent anywhere inside its
    own subtree: its own work plus everything it waited on.

    F41, and this is the primitive that fixes it. `attribute_to_edges` folds
    these into one number per service pair, which is the right shape for a
    whole-trace summary and the WRONG shape for the per-call rows the assembler
    writes. When `order-svc` calls `payment-svc` twice in a trace -- a retry, or
    the saga's compensation after a sibling fails -- the pair total was written
    to BOTH rows, so:

      * `crit_ms_sum` in the roll-up counted that blame twice;
      * the TDigest was fed the pair total once per call, overstating the
        per-call critical latency the dependency map ranks on;
      * individual rows carried `critical_ms > duration_ms`, which is
        definitionally impossible -- an edge cannot contribute more
        user-visible time than the call itself took.

    Measured on this repository before the fix: 4.83% of `order-svc->payment-svc`
    rows violated that invariant, against 3.9% of its traces making two or more
    calls. The edge the flagship exists to identify was the edge most
    over-blamed, because it is the one with a compensation path.

    M5: every cross-service child gets a key, INCLUDING zero-blame ones. The
    dependency map must still draw an edge that has traffic and errors but no
    blame; a missing key would make it disappear instead of fade.
    """
    by_id = {s.span_id: s for s in spans}

    own_ms: dict[str, float] = defaultdict(float)
    for seg in segments:
        own_ms[seg.span_id] += seg.ms

    # Roll subtree totals up from leaves. Depth-sorted so children settle first.
    depth: dict[str, int] = {}

    def _depth(sid: str) -> int:
        if sid in depth:
            return depth[sid]
        span = by_id[sid]
        d = 0 if span.parent_span_id not in by_id \
            else _depth(span.parent_span_id) + 1
        depth[sid] = d
        return d

    subtree_ms: dict[str, float] = dict(own_ms)
    for sid in sorted(by_id, key=_depth, reverse=True):
        span = by_id[sid]
        if span.parent_span_id in by_id:
            subtree_ms[span.parent_span_id] = (
                subtree_ms.get(span.parent_span_id, 0.0)
                + subtree_ms.get(sid, 0.0)
            )

    out: dict[str, float] = {}
    for sid, span in by_id.items():
        parent = by_id.get(span.parent_span_id)
        if parent and parent.service != span.service:
            out[sid] = subtree_ms.get(sid, 0.0)
    return out


def attribute_to_edges(spans: list[Span],
                       segments: list[Segment]) -> dict[tuple[str, str], float]:
    """Attribute critical-path milliseconds to each source->target service edge.

    The whole-trace summary: the sum of every call on that edge. Defined in
    terms of `attribute_to_child_spans` rather than recomputing, so the per-call
    rows and the per-edge total cannot drift apart -- which is the state F41
    left them in.

    An edge's contribution is the critical-path time spent anywhere inside the
    callee's subtree - its own work plus everything it waited on.

    M5: every cross-service edge gets a key, INCLUDING zero-blame ones. The
    dependency map must still draw an edge that has traffic and errors but no
    blame; a missing key would make it disappear instead of fade.
    """
    by_id = {s.span_id: s for s in spans}
    edges: dict[tuple[str, str], float] = defaultdict(float)
    for sid, ms in attribute_to_child_spans(spans, segments).items():
        span = by_id[sid]
        edges[(by_id[span.parent_span_id].service, span.service)] += ms
    return dict(edges)

