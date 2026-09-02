"""Runtime-mutable fault state with a TTL backstop (M23).

Faults are read PER REQUEST from mutable config. Never restart to apply a
fault: a restart perturbs latency on its own, so restarting to inject a latency
fault measures the restart rather than the fault.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class Fault:
    delay_ms: int = 0
    error_rate: float = 0.0
    cpu_burn_ms: int = 0

    @property
    def active(self) -> bool:
        return bool(self.delay_ms or self.error_rate or self.cpu_burn_ms)


NO_FAULT = Fault()


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    return int(raw) if raw.strip() else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    return float(raw) if raw.strip() else default


class FaultConfig:
    """Mutable fault state, scoped to an endpoint and bounded by a TTL.

    The TTL matters: if the eval runner crashes mid-case, the fault expires on
    its own instead of poisoning every subsequent measurement.
    """

    def __init__(self) -> None:
        self._fault = NO_FAULT
        self._endpoint = os.environ.get("TRACEMAP_FAULT_ENDPOINT", "")
        self._expires_at = 0.0
        self._load_from_env()

    def _load_from_env(self) -> None:
        # Everything defaults to zero/off. A fault must never be on by accident.
        fault = Fault(
            delay_ms=_int("TRACEMAP_FAULT_DELAY_MS", 0),
            error_rate=_float("TRACEMAP_FAULT_ERROR_RATE", 0.0),
            cpu_burn_ms=_int("TRACEMAP_FAULT_CPU_BURN_MS", 0),
        )
        if fault.active:
            self.set(fault, self._endpoint, _int("TRACEMAP_FAULT_TTL_S", 600))

    def set(self, fault: Fault, endpoint: str = "", ttl_s: int = 600) -> None:
        self._fault = fault
        self._endpoint = endpoint
        self._expires_at = time.monotonic() + ttl_s

    def clear(self) -> None:
        """Idempotent - called from `finally`, and possibly twice."""
        self._fault = NO_FAULT
        self._endpoint = ""
        self._expires_at = 0.0

    def for_endpoint(self, path: str) -> Fault:
        if time.monotonic() > self._expires_at:
            return NO_FAULT                      # expired: heals automatically
        if self._endpoint and self._endpoint != path:
            return NO_FAULT                      # scoped to one endpoint
        return self._fault

    def describe(self) -> dict:
        active = time.monotonic() <= self._expires_at
        f = self._fault if active else NO_FAULT
        return {
            "active": active,
            "endpoint": self._endpoint or None,
            "expires_in_s": max(0, round(self._expires_at - time.monotonic())) if active else 0,
            "delay_ms": f.delay_ms,
            "error_rate": f.error_rate,
            "cpu_burn_ms": f.cpu_burn_ms,
        }


def burn_cpu(ms: int) -> None:
    """Busy-wait, for saturation cases. Deliberately blocking: it must consume
    the event loop the way real CPU pressure does."""
    if ms <= 0:
        return
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        pass


fault_config = FaultConfig()
