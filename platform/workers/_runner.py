"""Shared supervision for every TraceMap worker.

A worker that dies quietly is the WORST failure mode in this system. Everything
looks fine: the dashboard renders, the API answers, no error appears anywhere -
and nothing is being watched. The dependency map just stops moving, and a stale
graph is indistinguishable from a quiet period.

So every worker gets two things from here:

  1. A loop that never exits. Exceptions are logged and the cycle continues.
     A single malformed trace must not take down assembly for everything else.

  2. A heartbeat counter incremented every cycle, scraped by Prometheus. The
     alert is on its ABSENCE:

         absent_over_time(tracemap_worker_cycle_total[5m])

     Monitoring the monitor is the part people forget.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from prometheus_client import Counter, Gauge, start_http_server

log = logging.getLogger(__name__)

CYCLES = Counter(
    "tracemap_worker_cycle_total",
    "Worker cycles completed",
    ["worker"],
)
FAILURES = Counter(
    "tracemap_worker_cycle_failures_total",
    "Worker cycles that raised",
    ["worker"],
)
LAST_SUCCESS = Gauge(
    "tracemap_worker_last_success_timestamp_seconds",
    "Unix time of the last successful cycle",
    ["worker"],
)
CYCLE_SECONDS = Gauge(
    "tracemap_worker_cycle_duration_seconds",
    "Duration of the most recent cycle",
    ["worker"],
)
ITEMS = Counter(
    "tracemap_worker_items_total",
    "Items processed (worker-defined unit)",
    ["worker"],
)


def serve_metrics(port: int = 9100) -> None:
    start_http_server(port)
    log.info("metrics on :%d", port)


async def run_forever(
    name: str,
    cycle: Callable[[], Awaitable[int]],
    interval_s: float,
) -> None:
    """Run `cycle` forever, every `interval_s`, and never die.

    `cycle` returns a count of items processed, purely for the metric.
    """
    # Touch the series at startup so `absent_over_time` can distinguish
    # "never started" from "started and then went silent".
    CYCLES.labels(worker=name)
    FAILURES.labels(worker=name)
    LAST_SUCCESS.labels(worker=name).set(0)

    log.info("%s starting, interval %.1fs", name, interval_s)

    while True:
        started = time.monotonic()
        try:
            processed = await cycle()
            CYCLES.labels(worker=name).inc()
            LAST_SUCCESS.labels(worker=name).set(time.time())
            if processed:
                ITEMS.labels(worker=name).inc(processed)
        except Exception:  # noqa: BLE001 - log and continue, NEVER re-raise
            FAILURES.labels(worker=name).inc()
            log.exception("%s cycle failed", name)
        finally:
            CYCLE_SECONDS.labels(worker=name).set(time.monotonic() - started)

        await asyncio.sleep(interval_s)
