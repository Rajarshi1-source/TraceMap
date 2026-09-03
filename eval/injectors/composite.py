"""Two or more faults applied as one, for the decoy case.

Case 006 needs product-svc and payment-svc slow *simultaneously*, which sounds
like the concurrent-faults situation the runner forbids -- and is not. The rule
is one fault per CASE, so that one attribution question has one answer. Here
the two faults are the case: the whole point is which of two simultaneously
slow services the detector blames.

Ordering matters on the way out, not on the way in. `stop()` unwinds every part
even if an earlier one raises, because a half-cleaned composite is exactly the
leaked-fault failure (M29) that poisons the next case.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class CompositeInjector:
    family = "composite"

    def __init__(self, spec: dict) -> None:
        from . import get_injector  # local: the registry imports this module

        parts = spec.get("parts") or []
        if len(parts) < 2:
            raise ValueError("composite injector needs at least two parts")
        self.parts = [get_injector(p) for p in parts]
        self.spec = spec
        self.name = "composite:" + "+".join(p.name for p in self.parts)

    async def start(self) -> None:
        started = []
        try:
            for part in self.parts:
                await part.start()
                started.append(part)
        except Exception:
            # Unwind what did start. A partially applied composite is a fault
            # nobody wrote down, which is worse than no fault at all.
            for part in reversed(started):
                await part.stop()
            raise

    async def stop(self) -> None:
        """Idempotent, and total: every part is attempted regardless."""
        for part in reversed(self.parts):
            try:
                await part.stop()
            except Exception:
                log.exception("composite cleanup failed for %s", part.name)

    def describe(self) -> dict:
        return {"family": "composite", "parts": [p.describe() for p in self.parts]}
