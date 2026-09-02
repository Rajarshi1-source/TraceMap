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


def attribute_to_edges(spans: list[Span],
                       segments: list[Segment]) -> dict[tuple[str, str], float]:
    """Attribute critical-path milliseconds to each source->target service edge.

    An edge's contribution is the critical-path time spent anywhere inside the
    callee's subtree - its own work plus everything it waited on.

    M5: every cross-service edge gets a key, INCLUDING zero-blame ones. The
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

    edges: dict[tuple[str, str], float] = defaultdict(float)
    for sid, span in by_id.items():
        parent = by_id.get(span.parent_span_id)
        if parent and parent.service != span.service:
            edges[(parent.service, span.service)] += subtree_ms.get(sid, 0.0)
    return dict(edges)
