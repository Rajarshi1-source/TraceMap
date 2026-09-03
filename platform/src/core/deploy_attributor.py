"""Correlate an anomaly with a recent deploy.

An alert that says "payment-svc is slow" starts an investigation. An alert that
says "payment-svc got slow 47 seconds after rev 12 rolled out, here's the diff"
usually ends one. That difference is what this buys, and it is what commercial
tools charge for.

M21: this reports CORRELATION and says so. Never "caused by".
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

CORRELATION_WINDOW_S = int(os.environ.get("CORRELATION_WINDOW_S", "900"))  # 15 min

Q_RECENT_DEPLOY = """
SELECT
    service_name,
    revision,
    git_sha,
    -- deployed_at_str, NOT `AS deployed_at`. Aliasing to the column's own name
    -- shadows it, so dateDiff below would receive the String alias instead of
    -- the DateTime column and fail with "illegal type ... got: String". This is
    -- the third time this pattern has bitten in this codebase; in ClickHouse,
    -- never alias a derived value to the name of the column it derives from.
    toString(deployed_at) AS deployed_at_str,
    trigger,
    dateDiff('second', deployed_at, parseDateTimeBestEffort('{at}')) AS lag_s
FROM tracemap.deploy_markers
WHERE service_name IN ('{src}', '{dst}')
  AND deployed_at BETWEEN parseDateTimeBestEffort('{at}') - INTERVAL {window} SECOND
                      AND parseDateTimeBestEffort('{at}')
  AND trigger != 'scale'
ORDER BY deployed_at DESC
LIMIT 1
"""


@dataclass(frozen=True)
class DeployCorrelation:
    """A correlation, or explicitly the absence of one.

    `found = False` is a REAL RESULT and must be rendered, not omitted. "No
    deploy correlated" tells on-call to stop reading the changelog and start
    looking at load, dependencies and infrastructure. Dropping the field when
    there is no match makes the alert silently ambiguous - the reader cannot
    tell "we checked and there was nothing" from "we never checked".
    """

    found: bool
    service: str = ""
    revision: str = ""
    git_sha: str = ""
    deployed_at: str = ""
    lag_seconds: int = 0
    confidence: float = 0.0
    trigger: str = ""

    def summary(self) -> str:
        if not self.found:
            return "No deploy correlated in the last 15 minutes."
        return (
            f"`{self.service}` rev {self.revision} deployed "
            f"{self.lag_seconds}s before onset (confidence {self.confidence:.2f})"
        )


async def attribute(ch, source: str, target: str, detected_at: str) -> DeployCorrelation:
    """Find a deploy that plausibly precedes this anomaly.

    Both endpoints of the edge are candidates: a rollout of the CALLER can
    change call patterns just as a rollout of the CALLEE can change service
    time. Checking only the target would miss half the real causes.
    """
    rows = await ch.query(
        Q_RECENT_DEPLOY,
        {"src": source, "dst": target, "at": detected_at, "window": CORRELATION_WINDOW_S},
    )
    if not rows:
        return DeployCorrelation(found=False)

    d = rows[0]
    lag = max(0, int(d["lag_s"]))

    # Linear decay: right after a deploy is suspicious, fourteen minutes later
    # is coincidence. Deliberately simple - it is explainable and monotonic, and
    # anything cleverer needs evidence from the eval set to justify itself.
    confidence = max(0.0, 1.0 - (lag / CORRELATION_WINDOW_S))

    return DeployCorrelation(
        found=True,
        service=d["service_name"],
        revision=str(d["revision"]),
        git_sha=d.get("git_sha") or "",
        deployed_at=d["deployed_at_str"],
        lag_seconds=lag,
        confidence=round(confidence, 2),
        trigger=d.get("trigger") or "",
    )
