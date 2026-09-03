"""Expose the latest eval scoreboard as metrics and as JSON.

Without this the Phase 7 alerting rules reference metrics nothing produces,
and `EvalGateRegressed` would sit permanently in the "no data" state -- which
Prometheus renders as neither firing nor healthy, and which everyone reads as
fine. An alert on a metric that is never emitted is worse than no alert,
because it looks like coverage.

The numbers come from `eval_runs`, written by the eval runner. That table is
the system of record; this module is a window onto it and holds no state of
its own. If the harness has not run, these series are absent rather than zero,
and `EvalNotRunning` fires on that absence -- the same absent_over_time pattern
the worker heartbeats use, for the same reason: a quality gate that stopped
running is a quality gate that passes everything.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Request, Response

log = logging.getLogger(__name__)

router = APIRouter()

LATEST_RUN = """
SELECT id, started_at, git_sha, tier, detector_cfg,
       precision, recall, f1, mttd_p50_s, mttd_p95_s, passed
FROM eval_runs
WHERE tier = %s
ORDER BY started_at DESC
LIMIT 1
"""

RUN_CASES = """
SELECT case_name, expected_edge, detected_edge, detected_at_s, outcome, detail
FROM eval_cases
WHERE run_id = %s
ORDER BY case_name
"""

HISTORY = """
SELECT started_at, git_sha, f1, precision, recall, mttd_p95_s, passed
FROM eval_runs
WHERE tier = %s
ORDER BY started_at DESC
LIMIT %s
"""


def _f(v) -> float | None:
    return None if v is None else float(v)


async def _latest(pg, tier: str) -> dict | None:
    rows = await pg.fetch(LATEST_RUN, (tier,))
    return rows[0] if rows else None


@router.get("/api/eval")
async def get_eval(request: Request, tier: str = "full", history: int = 20):
    """The scoreboard, as the dashboard and any human wants it.

    `detectorCfg` rides along deliberately. A score without the configuration
    that produced it is not reproducible, and the difference between "we tuned
    alpha and F1 improved" and "F1 improved" is the entire MLOps claim.
    """
    pg = getattr(request.app.state, "pg", None)
    if pg is None:
        return {"latest": None, "note": "postgres unavailable"}

    latest = await _latest(pg, tier)
    if latest is None:
        # Not an error. A repository that has never run the harness is a real
        # state, and saying so beats returning zeros that look like a score.
        return {"latest": None, "tier": tier,
                "note": "no eval run recorded for this tier yet"}

    cases = await pg.fetch(RUN_CASES, (latest["id"],))
    runs = await pg.fetch(HISTORY, (tier, max(1, min(history, 100))))

    return {
        "tier": tier,
        "latest": {
            "id": str(latest["id"]),
            "startedAt": latest["started_at"].isoformat(),
            "gitSha": latest["git_sha"],
            "detectorCfg": latest["detector_cfg"],
            "precision": _f(latest["precision"]),
            "recall": _f(latest["recall"]),
            "f1": _f(latest["f1"]),
            "mttdP50S": _f(latest["mttd_p50_s"]),
            "mttdP95S": _f(latest["mttd_p95_s"]),
            "passed": bool(latest["passed"]),
        },
        # MTTD is computed over DETECTED cases only; null cases contribute no
        # latency. Stated here so a consumer of this API cannot quote the
        # number without the caveat attached to it.
        "mttdNote": "computed over detected cases; null cases contribute no latency",
        "cases": [
            {
                "name": c["case_name"],
                "expectedEdge": c["expected_edge"],
                "detectedEdge": c["detected_edge"],
                "detectedAtS": _f(c["detected_at_s"]),
                "outcome": c["outcome"],
                "detail": c["detail"],
            }
            for c in cases
        ],
        "history": [
            {
                "startedAt": r["started_at"].isoformat(),
                "gitSha": r["git_sha"],
                "f1": _f(r["f1"]),
                "precision": _f(r["precision"]),
                "recall": _f(r["recall"]),
                "mttdP95S": _f(r["mttd_p95_s"]),
                "passed": bool(r["passed"]),
            }
            for r in runs
        ],
    }


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    """Prometheus exposition for the API and the eval scoreboard.

    Hand-rolled rather than routed through prometheus_client because there is
    exactly one source and it is a database row. A registry with a collector
    hook would be more machinery than the six gauges justify, and the text
    format is stable.
    """
    lines: list[str] = []

    def gauge(name: str, help_text: str, value, labels: str = "") -> None:
        if value is None:
            # Emit nothing rather than zero. Absence is the signal every
            # self-monitoring rule here keys off; a zero would read as a
            # detector scoring 0.0, which is a very different alert.
            return
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name}{labels} {value}")

    pg = getattr(request.app.state, "pg", None)
    if pg is not None:
        try:
            for tier in ("full", "smoke"):
                row = await _latest(pg, tier)
                if row is None:
                    continue
                lbl = f'{{tier="{tier}"}}'
                gauge("tracemap_eval_f1",
                      "F1 of the most recent eval run", _f(row["f1"]), lbl)
                gauge("tracemap_eval_precision",
                      "Precision of the most recent eval run",
                      _f(row["precision"]), lbl)
                gauge("tracemap_eval_recall",
                      "Recall of the most recent eval run",
                      _f(row["recall"]), lbl)
                gauge("tracemap_eval_mttd_p50_seconds",
                      "Median detection latency over detected cases",
                      _f(row["mttd_p50_s"]), lbl)
                gauge("tracemap_eval_mttd_p95_seconds",
                      "p95 detection latency over detected cases",
                      _f(row["mttd_p95_s"]), lbl)
                gauge("tracemap_eval_passed",
                      "1 if the most recent eval run cleared the gate",
                      int(bool(row["passed"])), lbl)
                gauge("tracemap_eval_last_run_timestamp_seconds",
                      "Unix time of the most recent eval run",
                      row["started_at"].timestamp(), lbl)
        except Exception:
            # A metrics endpoint must never be the thing that takes the API
            # down. Losing these series is visible through EvalNotRunning,
            # which is the correct way for this to fail.
            log.exception("could not read the eval scoreboard for /metrics")

    gauge("tracemap_api_up", "Analytics API is serving", 1)
    gauge("tracemap_api_scrape_timestamp_seconds",
          "Unix time this endpoint was scraped", time.time())

    return Response("\n".join(lines) + "\n",
                    media_type="text/plain; version=0.0.4; charset=utf-8")
