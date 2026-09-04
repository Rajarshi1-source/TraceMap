"""Turn an Anomaly into something a human can act on.

The ordering of this message is the product thesis, not a style choice:
critical-path delta FIRST, wall clock second. Lead with wall clock and you have
built the tool this project exists to replace.

Sample traces are ordered by `critical_ms` descending for the same reason. Order
them by `duration_ms` and the link lands on a trace where the edge was slow but
PARALLEL - the responder clicks through, sees nothing wrong, and starts
distrusting the alerts. One such link costs more credibility than the alert
gained.
"""

from __future__ import annotations

import json
import logging
import os

import httpx

from src.core.anomaly_detector import Anomaly
from src.core.deploy_attributor import DeployCorrelation

log = logging.getLogger(__name__)

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
DASHBOARD_BASE = os.environ.get("DASHBOARD_BASE", "http://localhost:3002")
JAEGER_BASE = os.environ.get("JAEGER_BASE", "http://localhost:16686")
ALERT_CHANNEL = "tracemap:alerts"          # Redis pub/sub -> SSE feed


def build_blocks(a: Anomaly, deploy: DeployCorrelation,
                 traces: list[dict]) -> list[dict]:
    """Slack Block Kit payload."""
    trace_links = " · ".join(
        f"<{JAEGER_BASE}/trace/{t['traceId']}|{t['traceId'][:6]}> "
        f"{t['criticalMs']:.0f}ms"
        for t in traces[:5]
    ) or "_no sample traces in window_"

    # An error-rate anomaly carries RATES in the same fields a latency one
    # carries milliseconds. Rendering 0.1 as "0ms" would be worse than not
    # alerting at all: it reads as a measurement rather than as a mistake.
    if a.metric == "error_rate":
        headline = f"Error-rate regression: {a.source} -> {a.target}"
        primary = {"type": "mrkdwn",
                   "text": f"*Error rate*\n{a.observed_ms * 100:.1f}% "
                           f"(threshold {a.baseline_ms * 100:.1f}%)"}
        secondary = {"type": "mrkdwn",
                     "text": f"*Failing requests*\n"
                             f"~{round(a.observed_ms * a.request_count):,} "
                             f"of {a.request_count:,}"}
    else:
        headline = f"Latency regression: {a.source} -> {a.target}"
        # Critical path FIRST. This ordering is the whole point.
        # F43: the bar is per-edge now, so "+234ms" alone is not a judgement --
        # on a noisy edge that is nothing, on a quiet one it is enormous. Naming
        # the bar is what lets a responder tell those apart, and it is also the
        # fastest way to see the detector was not merely being twitchy.
        bar = (f", bar {a.threshold_ms:.0f}ms for this edge"
               if a.threshold_ms > 0 else "")
        primary = {"type": "mrkdwn",
                   "text": f"*Critical-path p99*\n{a.observed_ms:.0f}ms "
                           f"(baseline {a.baseline_ms:.0f}ms, "
                           f"+{a.delta_ms:.0f}ms{bar})"}
        secondary = {"type": "mrkdwn",
                     "text": f"*Wall-clock p99*\n{a.wall_p99_ms:.0f}ms"}

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": headline},
        },
        {
            "type": "section",
            "fields": [
                primary,
                secondary,
                {"type": "mrkdwn",
                 "text": f"*Requests*\n{a.request_count:,} in window"},
                {"type": "mrkdwn",
                 "text": f"*Sustained*\n{a.sustained}/3 cycles"},
            ],
        },
        {
            # Always present, including the "nothing correlated" case.
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Likely cause:* {deploy.summary()}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f"*Slowest traces by critical path*\n{trace_links}"},
        },
        {
            "type": "actions",
            "elements": [
                {"type": "button",
                 "text": {"type": "plain_text", "text": "Open dependency map"},
                 "url": f"{DASHBOARD_BASE}/"},
            ],
        },
    ]

    if deploy.found and deploy.git_sha:
        blocks[2]["text"]["text"] += f"\n`git {deploy.git_sha}`"

    return blocks


class AlertManager:
    def __init__(self, redis=None, webhook: str | None = None) -> None:
        self.redis = redis
        self.webhook = webhook if webhook is not None else SLACK_WEBHOOK_URL
        self._client = httpx.AsyncClient(timeout=10.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def send(self, a: Anomaly, deploy: DeployCorrelation,
                   traces: list[dict]) -> dict:
        blocks = build_blocks(a, deploy, traces)
        payload = {"blocks": blocks}

        # Publish to the SSE feed regardless of Slack. The dashboard should show
        # the alert even when no webhook is configured, which is the normal
        # state on a laptop.
        if self.redis is not None:
            try:
                await self.redis.publish(ALERT_CHANNEL, json.dumps({
                    "edge": a.edge,
                    "metric": a.metric,
                    "baselineMs": a.baseline_ms,
                    "observedMs": a.observed_ms,
                    "deltaMs": a.delta_ms,
                    # The bar this edge had to clear, and the noise floor it was
                    # derived from (F43).
                    "thresholdMs": a.threshold_ms,
                    "madMs": a.mad_ms,
                    "wallP99Ms": a.wall_p99_ms,
                    "requestCount": a.request_count,
                    "deploy": deploy.summary(),
                    "deployFound": deploy.found,
                    "traces": traces[:5],
                }))
            except Exception:  # broad by design: a broken feed must not eat the alert
                log.exception("failed to publish alert to the SSE channel")

        if not self.webhook:
            log.info("ALERT (no webhook configured) %s +%.0fms | %s",
                     a.edge, a.delta_ms, deploy.summary())
            return {"delivered": False, "reason": "no_webhook", "blocks": blocks}

        try:
            r = await self._client.post(self.webhook, json=payload)
            ok = r.status_code < 300
            if not ok:
                log.error("slack rejected the alert: %s %s", r.status_code, r.text[:200])
            return {"delivered": ok, "status": r.status_code, "blocks": blocks}
        except Exception as exc:
            # An alerting outage must never take down the detector: the next
            # cycle still needs to run.
            log.exception("slack delivery failed")
            return {"delivered": False, "reason": str(exc), "blocks": blocks}
