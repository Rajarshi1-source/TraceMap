"""TraceMap analytics API.

A backend-for-frontend: it aggregates ClickHouse and Redis into exactly the
shape the dependency map needs, so the browser makes one request rather than
four and the join happens where the data is.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.eval_metrics import router as eval_router
from src.api.routes import router
from src.cache.redis_cache import Cache
from src.core.cost_meter import CostMeter
from src.core.edge_slo import EdgeSLOEvaluator
from src.core.query_engine import QueryEngine
from src.integrations.clickhouse_client import ClickHouseClient
from src.integrations.postgres_client import PostgresClient

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format='{"level":"%(levelname)s","service":"analytics-api","msg":"%(message)s"}',
)
# Credentials ride in headers, but httpx still logs a line per request at INFO
# and the volume is pure noise at 10s cadence.
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.ch = ClickHouseClient()
    app.state.cache = Cache()
    app.state.engine = QueryEngine(app.state.ch)
    # D5 and D6. Both are stateless readers over the same ClickHouse
    # connection -- neither owns a client, a cache or a background task,
    # which is why two stretch features cost one line each here.
    app.state.cost = CostMeter(app.state.ch)
    app.state.slo = EdgeSLOEvaluator(app.state.ch)
    app.state.pg = await PostgresClient.connect()
    yield
    if app.state.pg is not None:
        await app.state.pg.close()
    await app.state.ch.close()
    await app.state.cache.close()


app = FastAPI(title="TraceMap analytics API", version=os.environ.get("GIT_SHA", "dev"),
              lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "http://localhost:3002").split(","),
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(router)
# The eval scoreboard and /metrics. Kept in its own module because it is
# the only part of the API that reads the harness's tables rather than the
# telemetry ones -- a different system of record, and worth keeping visibly
# separate from the graph endpoints.
app.include_router(eval_router)
