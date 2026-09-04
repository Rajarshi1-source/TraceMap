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
import time

from prometheus_client import Counter, Gauge

from src.cache.redis_cache import Cache
from src.core.alert_manager import AlertManager
from src.core.anomaly_detector import AnomalyDetector, DetectorConfig, EdgeObservation
from src.core.deploy_attributor import attribute
from src.core.query_engine import QueryEngine
from src.integrations.clickhouse_client import ClickHouseClient
from src.integrations.postgres_client import PostgresClient
from workers._runner import run_forever, serve_metrics

# An alert on a metric nothing emits is worse than no alert: Prometheus renders
# "no data" as neither firing nor healthy, and everyone reads it as fine. These
# three back the AlertVolumeSpike, DetectorSilentTooLong and baseline panels
# added in Phase 7, and they exist because those rules were written first and
# would otherwise have been decoration.
ANOMALIES = Counter(
    "tracemap_anomalies_emitted_total",
    "Anomalies emitted, by edge and metric",
    ["src", "dst", "metric"],
)
LAST_ANOMALY = Gauge(
    "tracemap_last_anomaly_timestamp_seconds",
    "Unix time of the most recent anomaly, any edge",
)
# The boiling frog, made visible. A baseline that has climbed to sit on top of
# a known-degraded edge is the M6 failure, and it shows here before it shows
# anywhere else -- by which point the detector has already gone quiet.
EDGE_BASELINE = Gauge(
    "tracemap_edge_baseline_ms",
    "Current EWMA baseline for an edge's critical-path p99",
    ["src", "dst"],
)

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
        min_blame_share=float(os.environ.get("DETECTOR_MIN_BLAME_SHARE", "0.05")),
        sustained_cycles=int(os.environ.get("DETECTOR_SUSTAINED_CYCLES", "3")),
        # The error path's only knob. Fixed rather than learned, because zero
        # is the target for errors in a way it never is for latency.
        error_rate_threshold=float(os.environ.get("DETECTOR_ERROR_RATE_THRESHOLD", "0.02")),
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
            blame_share=e.get("blameShare", 1.0),
        )
        # Two independent questions about the same edge, each with its own
        # history and dedup keys. An edge can be slow, failing, or both, and
        # collapsing them would let one alert suppress the other.
        for anomaly in (await detector.evaluate(obs),
                        await detector.evaluate_error_rate(obs)):
            if anomaly is None:
                continue

            # Ordered by critical_ms DESC inside slow_traces(). Ordering by
            # wall clock would link to a trace where the edge was slow but
            # parallel.
            traces = await engine.slow_traces(anomaly.source, anomaly.target,
                                              TRACE_LOOKUP_WINDOW_MIN, limit=5)
            now = await ch.query("SELECT toString(now()) AS t")
            deploy = await attribute(ch, anomaly.source, anomaly.target,
                                     now[0]["t"])

            ANOMALIES.labels(src=anomaly.source, dst=anomaly.target,
                             metric=anomaly.metric).inc()
            LAST_ANOMALY.set(time.time())

            await alerts.send(anomaly, deploy, traces)
            if pg is not None:
                await _record(pg, anomaly, deploy, traces)

            if anomaly.metric == "error_rate":
                log.info("ALERT %s error rate %.1f%% (threshold %.1f%%) | %s",
                         anomaly.edge, anomaly.observed_ms * 100,
                         anomaly.baseline_ms * 100, deploy.summary())
            else:
                log.info("ALERT %s +%.0fms (baseline %.0f) | %s",
                         anomaly.edge, anomaly.delta_ms, anomaly.baseline_ms,
                         deploy.summary())
                fired += 1

    await _export_baselines(detector)
    return fired


async def _export_baselines(detector) -> None:
    """Publish every edge's current baseline as a gauge.

    One SCAN over a handful of keys per minute. Cheap, and it turns the single
    most important piece of detector state from something you inspect with
    redis-cli during an incident into something with a graph.
    """
    try:
        async for key in detector.redis.scan_iter(match="baseline:edge:*:cp99",
                                                  count=200):
            parts = key.split(":")
            if len(parts) != 5:
                continue
            value = await detector.redis.get(key)
            if value is not None:
                EDGE_BASELINE.labels(src=parts[2], dst=parts[3]).set(float(value))
    except Exception:
        # be the thing that stops the detector.
        log.debug("could not export baselines", exc_info=True)


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
    except Exception:
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
    # The model artifact, in the log. `eval_runs.detector_cfg` records it per
    # run; this is so the config is legible from a container's first ten lines
    # rather than only from a database.
    log.info("detector armed: alpha=%.2f threshold=%.0fms min_requests=%d "
             "sustained=%d/%d error_rate>%.1f%% blame_share>=%.0f%%",
             cfg.alpha, cfg.threshold_ms, cfg.min_requests,
             cfg.sustained_cycles, cfg.sustained_window,
             cfg.error_rate_threshold * 100, cfg.min_blame_share * 100)

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
    # Blocking file I/O inside an async function, once, at startup, before the
    # loop has anything to do. Pulling in aiofiles to avoid stalling an event
    # loop that is otherwise idle would be machinery for its own sake.
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),  # noqa: ASYNC240
                        "db", "001_platform.sql")
    try:
        with open(path, encoding="utf-8") as fh:  # noqa: ASYNC230
            await pg.execute_script(fh.read())
        log.info("platform schema applied")
    except Exception:
        log.exception("failed to apply platform schema")


if __name__ == "__main__":
    asyncio.run(main())
