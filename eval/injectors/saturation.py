"""CPU pressure -- degradation that nobody injected directly.

`cpu_burn_ms` busy-waits rather than sleeping, deliberately: it occupies the
event loop the way real CPU pressure does. A sleep would model a slow
dependency, which is the latency injector's job and a different failure.
"""

from __future__ import annotations

from .base import FaultInjector


class SaturationInjector(FaultInjector):
    family = "saturation"

    def payload(self) -> dict:
        return {"cpu_burn_ms": int(self.spec.get("cpu_burn_ms", 0))}
