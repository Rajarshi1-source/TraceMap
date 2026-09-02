"""payment-svc - the slowest hop by design, and the fault-injection target.

Baseline latency is load-bearing: payment must be the slowest service and
product a close second. That ordering is what makes the wall-clock /
critical-path toggle visibly disagree in Phase 5.
"""

from __future__ import annotations

import asyncio
import os
import random
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import db
from faults import Fault, burn_cpu, fault_config

SERVICE = os.environ.get("SERVICE_NAME", "payment-svc")
VERSION = os.environ.get("SERVICE_VERSION", "dev-local")
BASE_MS = int(os.environ.get("SVC_PAYMENT_BASE_MS", "90"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await db.close()


app = FastAPI(title=SERVICE, version=VERSION, lifespan=lifespan)


class ChargeRequest(BaseModel):
    userId: str = Field(min_length=1)
    amount: int = Field(ge=0)


class RefundRequest(BaseModel):
    chargeId: str = Field(min_length=1)


class FaultRequest(BaseModel):
    delay_ms: int = 0
    error_rate: float = 0.0
    cpu_burn_ms: int = 0
    endpoint: str = ""
    ttl_s: int = 600


async def _apply_fault(path: str) -> Fault:
    """Apply the configured fault and report what was applied.

    Phase 2 attaches these as `tracemap.fault.injected` span attributes. Making
    an injected fault visible in telemetry is not decoration: an invisible fault
    is indistinguishable from a real regression, and you will eventually spend
    an hour debugging your own injector.
    """
    cfg = fault_config.for_endpoint(path)
    if cfg.cpu_burn_ms:
        burn_cpu(cfg.cpu_burn_ms)
    return cfg


@app.post("/charge")
async def charge(body: ChargeRequest):
    cfg = await _apply_fault("/charge")

    await asyncio.sleep((BASE_MS + cfg.delay_ms) / 1000)

    if cfg.error_rate and random.random() < cfg.error_rate:
        raise HTTPException(status_code=502, detail="payment_provider_error")

    charge_id = await db.insert_payment("captured")
    return {"id": charge_id, "status": "captured", "amount": body.amount}


@app.post("/refund")
async def refund(body: RefundRequest):
    # Compensating action for the saga in order-svc. Must be tolerant: it is
    # called on paths where the charge may not exist.
    ok = await db.mark_refunded(body.chargeId)
    return {"id": body.chargeId, "status": "refunded" if ok else "not_found"}


# --------------------------------------------------------------------------
# Probes - M17
# --------------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    """Process liveness ONLY. Checking the database here would mean one slow
    query fails liveness on every replica at once, Kubernetes restarts them
    all, and a degradation becomes an outage."""
    return {"status": "ok", "service": SERVICE, "version": VERSION}


@app.get("/readyz")
async def readyz():
    """Dependencies are checked HERE."""
    try:
        await db.ping()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="db_unreachable") from exc
    return {"status": "ready"}


# --------------------------------------------------------------------------
# Fault control plane - internal network only, never public ingress
# --------------------------------------------------------------------------
@app.post("/_internal/fault")
async def set_fault(body: FaultRequest):
    fault_config.set(
        Fault(delay_ms=body.delay_ms, error_rate=body.error_rate, cpu_burn_ms=body.cpu_burn_ms),
        body.endpoint,
        body.ttl_s,
    )
    return {"ok": True, **fault_config.describe()}


@app.delete("/_internal/fault")
async def clear_fault():
    fault_config.clear()
    return {"ok": True, **fault_config.describe()}


@app.get("/_internal/fault")
async def get_fault():
    return fault_config.describe()
