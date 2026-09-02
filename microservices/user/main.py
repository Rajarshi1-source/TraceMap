"""user-svc - cheap profile lookup. Baseline 30ms: the fastest of the three
fan-out legs, so it never lands on the critical path in the demo trace."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

import db
from faults import burn_cpu, fault_config

SERVICE = os.environ.get("SERVICE_NAME", "user-svc")
VERSION = os.environ.get("SERVICE_VERSION", "dev-local")
BASE_MS = int(os.environ.get("SVC_USER_BASE_MS", "30"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await db.close()


app = FastAPI(title=SERVICE, version=VERSION, lifespan=lifespan)


@app.get("/users/{handle}")
async def get_user(handle: str):
    cfg = fault_config.for_endpoint("/users")
    if cfg.cpu_burn_ms:
        burn_cpu(cfg.cpu_burn_ms)

    await asyncio.sleep((BASE_MS + cfg.delay_ms) / 1000)

    if cfg.error_rate:
        import random
        if random.random() < cfg.error_rate:
            raise HTTPException(status_code=502, detail="injected_fault")

    user = await db.get_user(handle)
    if user is None:
        raise HTTPException(status_code=404, detail="user_not_found")
    return {"id": str(user["id"]), "handle": user["handle"], "name": user["name"]}


@app.get("/healthz")
async def healthz():
    """Process liveness ONLY - no dependency checks (M17)."""
    return {"status": "ok", "service": SERVICE, "version": VERSION}


@app.get("/readyz")
async def readyz():
    try:
        await db.ping()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="db_unreachable") from exc
    return {"status": "ready"}


@app.post("/_internal/fault")
async def set_fault(body: dict):
    from faults import Fault
    fault_config.set(
        Fault(
            delay_ms=int(body.get("delay_ms", 0)),
            error_rate=float(body.get("error_rate", 0.0)),
            cpu_burn_ms=int(body.get("cpu_burn_ms", 0)),
        ),
        body.get("endpoint", ""),
        int(body.get("ttl_s", 600)),
    )
    return {"ok": True, **fault_config.describe()}


@app.delete("/_internal/fault")
async def clear_fault():
    fault_config.clear()
    return {"ok": True, **fault_config.describe()}
