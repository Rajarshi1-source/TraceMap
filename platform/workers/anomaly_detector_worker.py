"""anomaly-detector: evaluate every edge once a minute, alert on what hurts.

The cycle is deliberately boring - read the roll-up, run the gates, enrich, send.
All the judgement lives in AnomalyDetector; this worker is plumbing.

The one thing it must never do is die quietly. A silent detector is the worst
failure mode in the system: the dashboard renders, the API answers, nothing
errors, and nothing is being watched. run_forever() guarantees the loop
survives, and the heartbeat counter makes its ABSENCE alertable.
"""

from __future__ import annotations

import asyncio
import logging
import os

from src.cache.redis_cache import Cache
from src.core.alert_manager import AlertManager
from src.core.anomaly_detector import AnomalyDetector, DetectorConfig, EdgeObservation
from src.core.deploy_attributor import attribute
from src.core.query_engine import QueryEngine
from src.integrations.clickhouse_client import ClickHouseClient
from src.integrations.postgres_client import PostgresClient
from workers._runner import run_forever, serve_metrics

log = logging.getLogger(__name__)

DETECT_INTERVAL_S = int(os.environ.get("DETECT_INTERVAL_S", "60"))
# The detector reads ONE minute-bucket, not a rolling window - see
# Q_LATEST_BUCKET in query_engine.py for why. This constant is only the window
# used for the sample-trace lookup that decorates the alert.
TRACE_LOOKUP_WINDOW_MIN = int(os.environ.get("TRACE_LOOKUP_WINDOW_MIN", "5"))

# Written to Postgres so anomalies survive a restart and the eval harness in
# Phase 7 has a system of record to score against.
INSERT_EVENT = """
INSERT INTO anomaly_events
    (src_service, dst_service, metric, baseline_ms, observed_ms, delta_ms,
     request_count, sample_trace_ids, correlated_deploy)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def config_from_env() -> DetectorConfig:
    return DetectorConfig(
        alpha=float(os.environ.get("DETECTOR_ALPHA", "0.2")),
        threshold_ms=float(os.environ.get("DETECTOR_THRESHOLD_MS", "50")),
        # The floor is 100 by design: a p99 over a handful of requests is noise.
        # A laptop demo runs well under that, so it is tunable - but changing it
        # changes the thing being measured, and any eval score quoted afterwards
        # must say which value produced it.
        min_requests=int(os.environ.get("DETECTOR_MIN_REQUESTS", "100")),
        sustained_cycles=int(os.environ.get("DETECTOR_SUSTAINED_CYCLES", "3")),
        warmup_cycles=int(os.environ.get("DETECTOR_WARMUP_CYCLES", "5")),
    )


async def detect_once(engine: QueryEngine, detector: AnomalyDetector,
                      alerts: AlertManager, ch: ClickHouseClient, pg) -> int:
    edges = await engine.latest_bucket_edges()
    if not edges:
        log.debug("no complete bucket yet")
        return 0
    log.debug("evaluating bucket %s across %d edges", edges[0]["bucket"], len(edges))
    fired = 0

    for e in edges:
        obs = EdgeObservation(
            source=e["source"],
            target=e["target"],
            critical_p99_ms=e["critP99"],     # M7: critical path, not wall clock
            wall_p99_ms=e["wallP99"],
            request_count=e["requestCount"],
            error_rate=e["errorRate"],
        )
        anomaly = await detector.evaluate(obs)
        if anomaly is None:
            continue

        # Ordered by critical_ms DESC inside slow_traces(). Ordering by wall
        # clock would link to a trace where the edge was slow but parallel.
        traces = await engine.slow_traces(anomaly.source, anomaly.target,
                                          TRACE_LOOKUP_WINDOW_MIN, limit=5)
        now = await ch.query("SELECT toString(now()) AS t")
        deploy = await attribute(ch, anomaly.source, anomaly.target, now[0]["t"])

        await alerts.send(anomaly, deploy, traces)
        if pg is not None:
            await _record(pg, anomaly, deploy, traces)

        log.info("ALERT %s +%.0fms (baseline %.0f) | %s",
                 anomaly.edge, anomaly.delta_ms, anomaly.baseline_ms,
                 deploy.summary())
        fired += 1

    return fired


async def _record(pg, anomaly, deploy, traces) -> None:
    import json
    try:
        await pg.execute(INSERT_EVENT, (
            anomaly.source, anomaly.target, anomaly.metric,
            anomaly.baseline_ms, anomaly.observed_ms, anomaly.delta_ms,
            anomaly.request_count,
            [t["traceId"] for t in traces],
            json.dumps({
                "found": deploy.found, "service": deploy.service,
                "revision": deploy.revision, "gitSha": deploy.git_sha,
                "lagSeconds": deploy.lag_seconds, "confidence": deploy.confidence,
            }),
        ))
    except Exception:  # noqa: BLE001 - never lose the alert over a bookkeeping failure
        log.exception("failed to record anomaly_event")


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format='{"level":"%(levelname)s","worker":"anomaly-detector","msg":"%(message)s"}',
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    serve_metrics(int(os.environ.get("METRICS_PORT", "9102")))

    ch = ClickHouseClient()
    cache = Cache()
    engine = QueryEngine(ch)
    detector = AnomalyDetector(cache.redis, config_from_env())
    alerts = AlertManager(cache.redis)

    for _ in range(60):
        if await ch.ping() and await cache.ping():
            break
        log.info("waiting for clickhouse and redis")
        await asyncio.sleep(2)

    pg = await PostgresClient.connect()
    if pg is not None:
        await _apply_schema(pg)
    cfg = config_from_env()
    log.info("detector armed: alpha=%.2f threshold=%.0fms min_requests=%d sustained=%d",
             cfg.alpha, cfg.threshold_ms, cfg.min_requests, cfg.sustained_cycles)

    await run_forever(
        "anomaly_detector",
        lambda: detect_once(engine, detector, alerts, ch, pg),
        DETECT_INTERVAL_S,
    )


async def _apply_schema(pg) -> None:
    """Idempotent DDL at startup.

    A migration tool is the right answer for a team; for three tables behind
    CREATE TABLE IF NOT EXISTS it is ceremony. The SQL lives in a file rather
    than a string so it stays reviewable.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "db", "001_platform.sql")
    try:
        with open(path, encoding="utf-8") as fh:
            await pg.execute_script(fh.read())
        log.info("platform schema applied")
    except Exception:  # noqa: BLE001
        log.exception("failed to apply platform schema")


if __name__ == "__main__":
    asyncio.run(main())
