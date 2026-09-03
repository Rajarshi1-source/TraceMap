"""The injector that does nothing, and is a real injector anyway.

Keeping the null case on the identical runner path is what makes it a
measurement rather than a claim. If the null case skipped injection, it would
also skip whatever the injection path does to timing, and it would stop being
the same experiment as the other eleven.

This is the false-positive measurement. It is never dropped to improve a
scoreboard (M9); a detector that cannot stay quiet on healthy traffic has not
earned the right to speak.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class NullInjector:
    family = "null"

    def __init__(self, spec: dict) -> None:
        self.spec = spec
        self.target = spec.get("target", "none")
        self.name = "null:none"

    async def start(self) -> None:
        log.info("inject  %-28s (nothing, by design)", self.name)

    async def stop(self) -> None:
        """Idempotent -- there is nothing to undo, and that must stay true."""

    def describe(self) -> dict:
        return {"family": "null", "target": None}
