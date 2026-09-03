"""Added latency on one endpoint of one service."""

from __future__ import annotations

from .base import FaultInjector


class LatencyInjector(FaultInjector):
    family = "latency"

    def payload(self) -> dict:
        return {"delay_ms": int(self.spec.get("delay_ms", 0))}
