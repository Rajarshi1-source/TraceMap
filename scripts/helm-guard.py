#!/usr/bin/env python3
"""Static invariants over the RENDERED Helm manifests.

`scripts/probe-guard.sh` proves M17 at runtime by stopping Postgres and
watching /healthz stay up while /readyz goes 503. That is the better proof and
it needs a running stack, which CI's chart job does not have.

This is the cheap complement: render the chart and assert the shape. It catches
the specific edit the runtime guard cannot see until deploy time -- someone
putting a dependency-checking path into a liveness probe, or a rollout that can
run two singleton workers at once.

    python scripts/helm-guard.py [--manifest rendered.yaml] [--chart charts/tracemap]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml

# Paths that check dependencies. If one of these appears in a liveness probe,
# one slow database fails liveness on every replica at once, Kubernetes
# restarts the fleet, the restarts add load to the already-slow database, and a
# degradation becomes an outage.
READINESS_ONLY_PATHS = {"/readyz", "/ready", "/health/ready"}

# Workers that must never run two at once. Two assemblers stitch the same trace
# twice and write duplicate edges, corrupting every percentile downstream (F2).
SINGLETON_WORKERS = {"trace-assembler", "graph-builder",
                     "anomaly-detector", "deploy-watcher"}

FAULT_ENV = {
    "TRACEMAP_FAULT_DELAY_MS",
    "TRACEMAP_FAULT_ERROR_RATE",
    "TRACEMAP_FAULT_CPU_BURN_MS",
}

fail = 0


def say(what: str, ok: bool, detail: str = "") -> None:
    global fail
    print(f"  {what:<58} {'OK' if ok else 'FAIL'}")
    if not ok:
        fail = 1
        if detail:
            print(f"      {detail}")


def render(chart: str) -> str:
    out = subprocess.run(["helm", "template", "tracemap", chart],
                         capture_output=True, text=True)
    if out.returncode != 0:
        print(out.stderr, file=sys.stderr)
        raise SystemExit(f"helm template failed for {chart}")
    return out.stdout


def workloads(docs: list[dict]):
    for d in docs:
        if d and d.get("kind") in ("Deployment", "StatefulSet", "DaemonSet"):
            yield d


def check_liveness_never_checks_dependencies(docs: list[dict]) -> None:
    """M17."""
    offenders = []
    for w in workloads(docs):
        name = w["metadata"]["name"]
        for c in w["spec"]["template"]["spec"].get("containers", []):
            probe = c.get("livenessProbe", {}).get("httpGet", {})
            path = probe.get("path")
            if path in READINESS_ONLY_PATHS:
                offenders.append(f"{name}/{c['name']} liveness -> {path}")
    say("M17  no dependency check in any liveness probe", not offenders,
        "; ".join(offenders))


def check_readiness_exists(docs: list[dict]) -> None:
    missing = [w["metadata"]["name"] for w in workloads(docs)
               for c in w["spec"]["template"]["spec"].get("containers", [])
               if "readinessProbe" not in c]
    # maxUnavailable: 0 is only honest if readiness is. Without a readiness
    # probe Kubernetes considers a pod ready the moment it starts, and a
    # "zero-downtime" rollout routes traffic into a process that cannot serve.
    say("every workload declares a readiness probe", not missing,
        ", ".join(missing))


def check_resource_requests(docs: list[dict]) -> None:
    missing = []
    for w in workloads(docs):
        for c in w["spec"]["template"]["spec"].get("containers", []):
            req = c.get("resources", {}).get("requests", {})
            if not req.get("cpu") or not req.get("memory"):
                missing.append(f"{w['metadata']['name']}/{c['name']}")
    # Requests drive scheduling; limits only cap. A pod with limits and no
    # requests schedules anywhere, and the node discovers the problem later.
    say("every container sets CPU and memory requests", not missing,
        ", ".join(missing))


def check_singleton_workers(docs: list[dict]) -> None:
    problems = []
    for w in workloads(docs):
        name = w["metadata"]["name"]
        if name not in SINGLETON_WORKERS:
            continue
        if w["spec"].get("replicas", 1) != 1:
            problems.append(f"{name} has {w['spec'].get('replicas')} replicas")
        strategy = w["spec"].get("strategy", {}).get("type")
        if strategy != "Recreate":
            problems.append(f"{name} uses {strategy}, which overlaps two pods")
    say("F2  singleton workers cannot overlap", not problems,
        "; ".join(problems))


def check_faults_default_off(docs: list[dict]) -> None:
    """M23. A fault must never be on by accident."""
    problems = []
    for w in workloads(docs):
        for c in w["spec"]["template"]["spec"].get("containers", []):
            for env in c.get("env", []) or []:
                if env.get("name") in FAULT_ENV:
                    value = str(env.get("value", "0"))
                    if value not in ("0", "0.0", ""):
                        problems.append(
                            f"{w['metadata']['name']} {env['name']}={value}")
    say("M23  every fault flag defaults to off", not problems,
        "; ".join(problems))


def check_git_sha_label(docs: list[dict]) -> None:
    """M24. Deploy attribution keys off this label."""
    missing = [w["metadata"]["name"] for w in workloads(docs)
               if not w["spec"]["template"]["metadata"]
               .get("labels", {}).get("git-sha")]
    say("M24  every pod carries a git-sha label", not missing,
        ", ".join(missing))


def check_no_write_rbac_by_default(docs: list[dict]) -> None:
    """M20. Auto-rollback ships off, and so does the permission to do it."""
    writes = []
    for d in docs:
        if not d or d.get("kind") != "Role":
            continue
        for rule in d.get("rules", []) or []:
            bad = {"create", "update", "patch", "delete", "*"} & set(rule.get("verbs", []))
            if bad and "deployments" in rule.get("resources", []):
                writes.append(f"{d['metadata']['name']}: {sorted(bad)}")
    say("M20  no Deployment-write RBAC with rollback disabled", not writes,
        "; ".join(writes))


def check_non_root(docs: list[dict]) -> None:
    problems = []
    for w in workloads(docs):
        spec = w["spec"]["template"]["spec"]
        if not spec.get("securityContext", {}).get("runAsNonRoot"):
            problems.append(w["metadata"]["name"])
    say("every pod runs as non-root", not problems, ", ".join(problems))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", help="pre-rendered YAML; renders the chart if absent")
    ap.add_argument("--chart", default="charts/tracemap")
    args = ap.parse_args()

    text = (Path(args.manifest).read_text(encoding="utf-8")
            if args.manifest else render(args.chart))
    docs = [d for d in yaml.safe_load_all(text) if d]

    print()
    print(f"  Helm invariant guard  ({len(docs)} manifests)")
    print()
    check_liveness_never_checks_dependencies(docs)
    check_readiness_exists(docs)
    check_resource_requests(docs)
    check_singleton_workers(docs)
    check_faults_default_off(docs)
    check_git_sha_label(docs)
    check_no_write_rbac_by_default(docs)
    check_non_root(docs)
    print()
    print("  HELM GUARD PASS" if not fail else "  HELM GUARD FAIL")
    print()
    return fail


if __name__ == "__main__":
    raise SystemExit(main())
