---
name: tracemap-deploy-attribution
description: Build TraceMap's change-intelligence layer — the Kubernetes deploy watcher that records Deployment revisions as markers, the confidence-decaying correlation between anomalies and recent rollouts, Slack alert blocks with trace deep links and a diff link, trace-to-code via service.version, and the guarded rollback path. Use for ANY work on deploy markers, change correlation, "did a deploy cause this", release annotations on latency charts, alert enrichment, Slack alert formatting, or automated rollback — and also when the user says "correlate with releases", "add deploy tracking", or "make the alert actionable". MANDATE - correlation is not causation and the alert must say so; automated rollback ships behind a feature flag defaulting to OFF, gated on a confidence floor, rate-limited per service, with a kill switch and an audit trail, because remediating on a correlation signal can turn one bad deploy into a rollback loop. Pair with tracemap-anomaly-eval for the anomalies this enriches.
---

# TraceMap — Deploy Attribution and Guarded Rollback

Most incidents are caused by a change. An alert saying *"payment-svc is slow"* starts an investigation. An alert saying *"payment-svc got slow 47 seconds after rev 12 rolled out, here's the diff"* usually ends one. That difference is what this layer buys, and it is what commercial tools charge for.

## Prerequisite: `service.version` from day one

None of this works without release identity on every span:

```yaml
env:
  - name: OTEL_RESOURCE_ATTRIBUTES
    value: "service.version={{ .Values.gitSha }}"
```

and the matching pod label used by the watcher:

```yaml
metadata:
  labels:
    app: payment-svc
    git-sha: "{{ .Values.gitSha }}"
```

Set both on the first day of the project. Backfilling means a stretch of traces that can never be correlated to a release, and the gap always lands in the window you most want to analyse.

## The deploy watcher

Watch the Kubernetes API rather than polling, and dedupe on the revision annotation so a status-only update does not create a phantom marker:

```python
# platform/workers/deploy_watcher_worker.py
from kubernetes_asyncio import client, config, watch

async def watch_deployments(ch, namespace: str = "tracemap") -> None:
    await config.load_incluster_config()
    apps = client.AppsV1Api()
    seen: dict[str, str] = {}

    async with watch.Watch().stream(apps.list_namespaced_deployment,
                                    namespace=namespace) as stream:
        async for event in stream:
            dep   = event["object"]
            name  = dep.metadata.name
            rev   = dep.metadata.annotations.get(
                        "deployment.kubernetes.io/revision", "?")
            image = dep.spec.template.spec.containers[0].image
            git_sha = dep.spec.template.metadata.labels.get("git-sha", "")

            if seen.get(name) == rev:      # status churn, not a new rollout
                continue
            seen[name] = rev

            await ch.insert("tracemap.deploy_markers", [{
                "deployed_at": dep.metadata.creation_timestamp,
                "service_name": name,
                "revision": rev,
                "image_sha": image.split("@")[-1] if "@" in image else image,
                "git_sha": git_sha,
                "replicas": dep.spec.replicas or 1,
                "trigger": "rollout",
            }])
```

Operational notes:

- **RBAC: `get, list, watch` on `deployments` only.** Least privilege on a read-only watcher is a security answer you get credit for, and it costs nothing.
- **Watch streams die.** Kubernetes closes them routinely; wrap in a reconnect loop with backoff and resume from `resourceVersion`, or the watcher goes quiet and nobody notices.
- **`seen` is in-process memory.** On restart the first event per Deployment may re-record; dedupe on `(service_name, revision)` at read time or accept the harmless duplicate.
- **Distinguish triggers.** A replica scale is not an image change. Record `trigger` so a scaling event is not mistaken for a release when correlating.

## Correlation with confidence decay

```python
# platform/src/core/deploy_attributor.py
CORRELATION_WINDOW_S = 900          # 15 minutes

async def attribute(anomaly, ch) -> dict | None:
    rows = await ch.query("""
        SELECT service_name, revision, git_sha, deployed_at,
               dateDiff('second', deployed_at, %(t)s) AS lag_s
        FROM tracemap.deploy_markers
        WHERE service_name IN (%(src)s, %(dst)s)
          AND deployed_at BETWEEN %(t)s - INTERVAL %(w)s SECOND AND %(t)s
        ORDER BY deployed_at DESC LIMIT 1
    """, {"t": anomaly.detected_at, "src": anomaly.source,
          "dst": anomaly.target, "w": CORRELATION_WINDOW_S})

    if not rows:
        return None

    d = rows[0]
    # Confidence decays with lag: right after a deploy is suspicious,
    # fourteen minutes later is coincidence.
    confidence = max(0.0, 1.0 - (d["lag_s"] / CORRELATION_WINDOW_S))
    return {**d, "confidence": round(confidence, 2)}
```

Design points worth defending:

- **Linear decay is a deliberate simplification.** It is explainable and monotonic. Anything fancier needs evidence from the eval set to justify.
- **Both endpoints of the edge are candidates.** A rollout of the *caller* can change call patterns just as a rollout of the *callee* can change service time.
- **Absence is a finding.** "No deploy correlated" is genuinely useful — it points the investigation at load, dependencies or infrastructure instead. Always render that case explicitly rather than omitting the field.
- **Never assert causation.** The alert says "likely cause" with a number attached, never "caused by".

## Alert format

```json
{
  "blocks": [
    { "type": "header",
      "text": { "type": "plain_text", "text": "Latency regression: order-svc -> payment-svc" } },
    { "type": "section", "fields": [
      { "type": "mrkdwn", "text": "*Critical-path p99*\n418ms (baseline 360ms, +58ms)" },
      { "type": "mrkdwn", "text": "*Wall-clock p99*\n421ms (+61ms)" },
      { "type": "mrkdwn", "text": "*Requests*\n1,204 in window" },
      { "type": "mrkdwn", "text": "*Sustained*\n3/3 cycles" }
    ]},
    { "type": "section",
      "text": { "type": "mrkdwn",
        "text": "*Likely cause:* `payment-svc` rev 12 deployed *47s* before onset (confidence 0.95)\n`git 4f2a9c1` - <https://github.com/OWNER/REPO/compare/3e1b...4f2a|view diff>" } },
    { "type": "section",
      "text": { "type": "mrkdwn",
        "text": "*Sample slow traces*\n<LINK|d4f8a2> 812ms - <LINK|9c1e77> 794ms - <LINK|2b6f01> 771ms" } },
    { "type": "actions", "elements": [
      { "type": "button", "text": {"type":"plain_text","text":"Open dependency map"},
        "url": "https://tracemap.example.dev/edge/order-svc/payment-svc" },
      { "type": "button", "text": {"type":"plain_text","text":"Rollback rev 12"},
        "style": "danger", "value": "rollback:payment-svc:11" }
    ]}
  ]
}
```

Lead with **critical-path** delta and show wall clock second — the ordering is the product thesis. Sample traces are selected by `critical_ms` descending so the person who clicks through actually sees the problem.

Deploy markers also render as vertical lines on the edge latency chart. That visual — a step change in p99 landing right on a release line — persuades faster than any alert text.

## Guarded rollback

The rollback button can call `kubectl rollout undo`. It demos spectacularly and it is the most dangerous thing in the project. Ship it with all five guards or do not ship it:

1. **Human-in-the-loop by default.** The button asks; it does not act autonomously.
2. **Confidence floor.** No automated action below 0.9 correlation confidence.
3. **Rate limit.** One automated rollback per service per hour, hard stop.
4. **Kill switch.** `AUTO_ROLLBACK_ENABLED=false` in Helm values, default off.
5. **Audit trail.** Every action written to Postgres with the triggering anomaly ID, actor, and outcome.

The failure mode to name unprompted: roll back, traffic shifts, latency changes, detector fires again, roll back again. Automated remediation on a correlation signal can convert one bad deploy into a rollback loop, which is worse than the original incident.

Also require the rollback target to be an actual previous revision from `deploy_markers`, never a value taken straight from a Slack payload. A button value is user input crossing a trust boundary into a cluster-mutating call — validate it server-side against known revisions and verify the Slack request signature.

## Eval coverage

Case 11 in the golden set (`011_deploy_correlated`) injects a fault 30 seconds after a rollout and asserts the alert names the right revision with high confidence. Without that case the attribution logic is untested narrative. Consider a companion negative case: a rollout with **no** fault, asserting no anomaly is raised — otherwise nothing proves the correlator is not just decorating every alert with the nearest deploy.

## What not to do

- Do not poll the API server on a timer when a watch stream will do.
- Do not treat a scale event as a release.
- Do not claim causation in alert copy.
- Do not enable auto-rollback by default, or without all five guards.
- Do not trust the Slack button payload as the rollback target.
- Do not silently omit the "no deploy correlated" case — it is a real result.

## Interview soundbites

- *"An alert that says a service is slow starts an investigation. An alert that says it got slow 47 seconds after rev 12 rolled out, with the diff attached, usually ends one."*
- *"I built automated rollback and I default it to off. Remediating on a correlation signal can turn one bad deploy into a rollback loop, so it's human-in-the-loop, gated on 0.9 confidence, rate-limited to one action per service per hour, with a kill switch and an audit log. The capability is the easy part; the guardrails are the engineering."*
- *"Confidence decays with lag — right after a deploy is suspicious, fourteen minutes later is coincidence. And 'no deploy correlated' is a useful answer, because it sends you to look at load and dependencies instead."*

## Pairing

- `tracemap-anomaly-eval` — produces the anomalies this enriches
- `tracemap-clickhouse-analytics` — the `deploy_markers` table and Q4
- `tracemap-platform-ops` — Helm values, RBAC, and the feature flags
