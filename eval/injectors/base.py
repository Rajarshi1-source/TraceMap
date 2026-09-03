"""The injector interface, and the fault control plane it drives.

Every injector implements the same three-method shape, so the runner never
learns what kind of fault it is running. That is the whole point: adding a
sixth fault family must not touch `runner.py`.

Two rules are load-bearing and both come from the same lesson (M29):

  * `stop()` is idempotent. It is called from `finally`, and on a crash path it
    may be called twice. A leaked fault does not announce itself -- it silently
    becomes the next case's baseline, and every subsequent measurement is
    wrong in a way that looks like a detector regression.

  * No restarts. A container restart perturbs latency on its own, so restarting
    to inject a latency fault measures the restart. Every fault here is a
    runtime flag read per request, TTL-bounded so a crashed runner still heals.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Protocol, runtime_checkable

import httpx

log = logging.getLogger(__name__)

# Where each service's control plane lives. Overridable so the same harness
# runs against compose (localhost), a kind cluster, or k3s.
#
# gateway-svc is deliberately absent: it has no fault control plane, because
# nothing needs one. Case 012 injects into order-svc, which is the *target* of
# the `gateway-svc->order-svc` edge -- the edge's latency is the callee's.
SERVICE_URLS: dict[str, str] = {
    "order-svc": os.environ.get("ORDER_URL", "http://localhost:3001"),
    "user-svc": os.environ.get("USER_URL", "http://localhost:8000"),
    "payment-svc": os.environ.get("PAYMENT_URL", "http://localhost:8001"),
    "product-svc": os.environ.get("PRODUCT_URL", "http://localhost:8080"),
}

FAULT_PATH = "/_internal/fault"


class InjectorError(RuntimeError):
    pass


@runtime_checkable
class Injector(Protocol):
    name: str

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def describe(self) -> dict: ...


def service_url(target: str) -> str:
    try:
        return SERVICE_URLS[target]
    except KeyError:
        raise InjectorError(
            f"no control plane for {target!r}; known targets: "
            f"{', '.join(sorted(SERVICE_URLS))}"
        ) from None


class FaultInjector:
    """Shared implementation for every flag-driven fault.

    Subclasses supply `payload()`. Everything else -- the POST, the idempotent
    DELETE, the TTL backstop -- is identical across latency, error, saturation
    and dependency-kill, and duplicating it four times is how the four copies
    drift.
    """

    family = "fault"

    def __init__(self, spec: dict) -> None:
        self.spec = spec
        self.target = spec["target"]
        # Validate NOW, not at injection time. A case whose target has no
        # control plane should fail before the run starts, not eight minutes
        # in with a warm stack and a half-scored set behind it.
        service_url(self.target)
        self.endpoint = spec.get("endpoint", "")
        # The TTL is a backstop, not the mechanism. The runner stops the fault
        # explicitly; this only matters when the runner dies without unwinding.
        # It is deliberately longer than the case budget and shorter than the
        # next case's measurement window.
        self.ttl_s = int(spec.get("duration_s", 600))
        self.name = f"{self.family}:{self.target}{self.endpoint or ''}"
        self._started = False

    def payload(self) -> dict:
        raise NotImplementedError

    async def _post(self, body: dict) -> dict:
        url = service_url(self.target) + FAULT_PATH
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=body)
            r.raise_for_status()
            return r.json()

    async def start(self) -> None:
        body = {**self.payload(), "endpoint": self.endpoint, "ttl_s": self.ttl_s}
        state = await self._post(body)
        self._started = True
        log.info("inject  %-28s %s", self.name, body)
        if not state.get("active"):
            raise InjectorError(
                f"{self.name}: control plane accepted the fault but reports "
                f"inactive -- {state}"
            )

    async def stop(self) -> None:
        """Idempotent. Safe to call twice, and safe to call before start()."""
        url = service_url(self.target) + FAULT_PATH
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.delete(url)
                r.raise_for_status()
                state = r.json()
        except Exception:
            # Never raise out of a `finally`. Losing the original failure to a
            # cleanup error is how a one-line bug becomes an evening.
            log.exception("cleanup failed for %s -- TTL backstop will heal it", self.name)
            return
        if state.get("active"):
            log.error("%s: fault STILL ACTIVE after clear -- %s", self.name, state)
        elif self._started:
            log.info("healed  %s", self.name)
        self._started = False

    def describe(self) -> dict:
        return {"family": self.family, "target": self.target,
                "endpoint": self.endpoint or None, **self.payload()}


async def assert_all_clear() -> list[str]:
    """Ask every control plane whether a fault is active.

    Called before the first case and after the last one. A fault leaked by a
    previous run is indistinguishable from a broken detector, and this is the
    cheapest place to catch it.
    """
    dirty: list[str] = []
    async with httpx.AsyncClient(timeout=5) as client:
        for svc, base in SERVICE_URLS.items():
            try:
                r = await client.get(base + FAULT_PATH)
            except Exception as exc:  # noqa: BLE001 - a control plane that cannot be reached is reported, not raised
                dirty.append(f"{svc}: unreachable ({exc.__class__.__name__})")
                continue
            if r.status_code != 200:
                # A service that cannot answer "am I clean?" is not clean, it
                # is unaudited -- and treating a 405 as an all-clear is how a
                # leaked fault survives the one check designed to catch it.
                # user-svc shipped without a GET handler and read as clean for
                # exactly this reason.
                dirty.append(f"{svc}: control plane returned "
                             f"{r.status_code} to a state query")
                continue
            if r.json().get("active"):
                dirty.append(f"{svc}: {r.json()}")
    return dirty


async def clear_all() -> None:
    """Best-effort DELETE against every control plane."""
    async with httpx.AsyncClient(timeout=5) as client:
        for base in SERVICE_URLS.values():
            # Best effort by design: this is the belt to the per-injector
            # braces, and a service that is already gone cannot hold a fault.
            with contextlib.suppress(Exception):
                await client.delete(base + FAULT_PATH)
