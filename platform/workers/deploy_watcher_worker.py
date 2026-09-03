"""deploy-watcher: record releases as markers, so anomalies can be correlated.

Two sources, because the platform runs in two places:

  * KUBERNETES - watch the API for Deployment rollouts. A watch stream, not a
    poll: polling the API server on a timer is both slower and ruder.
  * COMPOSE / CI - an HTTP endpoint. There is no Deployment object on a laptop,
    and eval case 011 needs to simulate a rollout deterministically anyway.

Same table, same downstream correlation. The source is recorded in `trigger` so
a marker's provenance is never ambiguous.

RBAC for the Kubernetes path: get, list, watch on `deployments` ONLY. A
read-only watcher scoped to one resource type costs nothing and is the kind of
least-privilege answer that gets credit unprompted.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from aiohttp import web

from src.integrations.clickhouse_client import ClickHouseClient
from workers._runner import CYCLES, serve_metrics

log = logging.getLogger(__name__)

NAMESPACE = os.environ.get("K8S_NAMESPACE", "tracemap")
CONTROL_PORT = int(os.environ.get("DEPLOY_CONTROL_PORT", "9103"))


async def record_marker(ch: ClickHouseClient, *, service: str, revision: str,
                        image_sha: str = "", git_sha: str = "",
                        replicas: int = 1, trigger: str = "rollout") -> None:
    await ch.insert("tracemap.deploy_markers", [{
        "deployed_at": _now(),
        "service_name": service,
        "revision": str(revision),
        "image_sha": image_sha,
        "git_sha": git_sha,
        "replicas": replicas,
        "trigger": trigger,
    }])
    CYCLES.labels(worker="deploy_watcher").inc()
    log.info("deploy marker: %s rev=%s trigger=%s git=%s",
             service, revision, trigger, git_sha or "-")


def _now() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Kubernetes watch
# ---------------------------------------------------------------------------
async def watch_deployments(ch: ClickHouseClient) -> None:
    try:
        from kubernetes_asyncio import client, config, watch
    except ImportError:
        log.info("kubernetes_asyncio not installed; k8s watch disabled")
        return

    try:
        config.load_incluster_config()
    except Exception:  # noqa: BLE001 - outside a cluster this is the normal path
        log.info("not running in-cluster; k8s watch disabled")
        return

    apps = client.AppsV1Api()
    seen: dict[str, str] = {}

    # Watch streams die routinely - the API server closes them on its own
    # schedule. Without a reconnect loop the watcher goes quiet and, because
    # nothing errors, nobody notices until a correlation comes back empty.
    while True:
        try:
            async with watch.Watch().stream(
                apps.list_namespaced_deployment, namespace=NAMESPACE
            ) as stream:
                async for event in stream:
                    dep = event["object"]
                    name = dep.metadata.name
                    rev = dep.metadata.annotations.get(
                        "deployment.kubernetes.io/revision", "?")
                    if seen.get(name) == rev:
                        continue          # status churn, not a new rollout
                    prev_replicas = seen.get(f"{name}:replicas")
                    seen[name] = rev

                    image = dep.spec.template.spec.containers[0].image
                    git_sha = (dep.spec.template.metadata.labels or {}).get("git-sha", "")
                    replicas = dep.spec.replicas or 1

                    # A replica change is NOT a release. Recording it as one
                    # would let the correlator blame a scaling event for a
                    # latency regression, with a confidence number attached.
                    trigger = ("scale"
                               if prev_replicas and prev_replicas != str(replicas)
                               and seen.get(f"{name}:image") == image
                               else "rollout")
                    seen[f"{name}:replicas"] = str(replicas)
                    seen[f"{name}:image"] = image

                    await record_marker(
                        ch, service=name, revision=rev,
                        image_sha=image.split("@")[-1] if "@" in image else image,
                        git_sha=git_sha, replicas=replicas, trigger=trigger,
                    )
        except Exception:
            log.exception("k8s watch stream failed; reconnecting in 5s")
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# HTTP control plane (compose / CI)
# ---------------------------------------------------------------------------
def build_app(ch: ClickHouseClient) -> web.Application:
    async def post_deploy(request: web.Request) -> web.Response:
        body: dict[str, Any] = await request.json()
        service = body.get("service")
        if not service:
            return web.json_response({"error": "service required"}, status=400)
        await record_marker(
            ch,
            service=service,
            revision=body.get("revision", "1"),
            image_sha=body.get("image_sha", ""),
            git_sha=body.get("git_sha", os.environ.get("GIT_SHA", "")),
            replicas=int(body.get("replicas", 1)),
            trigger=body.get("trigger", "rollout"),
        )
        return web.json_response({"ok": True})

    async def get_deploys(_request: web.Request) -> web.Response:
        rows = await ch.query(
            "SELECT service_name, revision, git_sha, toString(deployed_at) AS deployed_at,"
            " trigger FROM tracemap.deploy_markers"
            " ORDER BY deployed_at DESC LIMIT 20"
        )
        return web.json_response({"deploys": rows})

    async def healthz(_r: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "service": "deploy-watcher"})

    app = web.Application()
    app.add_routes([
        web.post("/_internal/deploy", post_deploy),
        web.get("/deploys", get_deploys),
        web.get("/healthz", healthz),
    ])
    return app


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format='{"level":"%(levelname)s","worker":"deploy-watcher","msg":"%(message)s"}',
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    serve_metrics(int(os.environ.get("METRICS_PORT", "9104")))

    ch = ClickHouseClient()
    for _ in range(60):
        if await ch.ping():
            break
        await asyncio.sleep(2)

    CYCLES.labels(worker="deploy_watcher")     # touch the series at startup

    runner = web.AppRunner(build_app(ch))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", CONTROL_PORT).start()
    log.info("deploy control plane on :%d", CONTROL_PORT)

    await watch_deployments(ch)                # returns immediately outside k8s
    await asyncio.Event().wait()               # keep the HTTP server alive


if __name__ == "__main__":
    asyncio.run(main())
