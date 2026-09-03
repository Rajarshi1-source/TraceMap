"""A fraction of requests answered 5xx.

Note this is an *error* fault, not a latency one: the edge's error_rate moves
and its critical-path p99 barely does. It is here because a detector that only
watches latency is half a detector, and the golden set has to say so.
"""

from __future__ import annotations

from .base import FaultInjector


class ErrorInjector(FaultInjector):
    family = "error"

    def payload(self) -> dict:
        return {"error_rate": float(self.spec.get("error_rate", 0.0))}
