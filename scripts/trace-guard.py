#!/usr/bin/env python3
"""Phase 2 exit criteria, checked against Jaeger rather than eyeballed.

  1. One trace spans all five services.
  2. The three fan-out client spans OVERLAP in time (M1, visually in the trace).
  3. Every service carries service.version = the git SHA (M24).

Criterion 2 is the one that matters. The unit test in microservices/order proves
the orchestration is concurrent; this proves it survived the network, the
instrumentation and the collector. If these spans are ever staggered
end-to-start, the fan-out has regressed and critical-path attribution in Phase 4
will have nothing to show.

    python scripts/trace-guard.py [--jaeger URL] [--expect-sha SHA]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

FANOUT_TARGETS = {"user-svc", "product-svc", "payment-svc"}
ALL_SERVICES = {"gateway-svc", "order-svc"} | FANOUT_TARGETS


def fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)


def services_of(trace: dict) -> set[str]:
    return {p["serviceName"] for p in trace["processes"].values()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jaeger", default="http://localhost:16686")
    ap.add_argument("--expect-sha", default=None)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--gateway", default="http://localhost:3000")
    ap.add_argument(
        "--no-generate",
        action="store_true",
        help="do not fire a checkout first; rely on ambient traffic instead",
    )
    args = ap.parse_args()

    fail = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal fail
        print(f"  {label:<58} {'OK' if ok else 'FAIL'}")
        if detail:
            print(f"      {detail}")
        if not ok:
            fail = 1

    print("\n  Phase 2 trace guard\n")

    # Generate the trace this guard asserts on. Relying on ambient traffic makes
    # the result depend on whatever ran last: browse traffic produces
    # two-service traces, and the guard then fails for a reason that has nothing
    # to do with the fan-out.
    if not args.no_generate:
        body = json.dumps(
            {"userId": "user-1", "items": [{"sku": "SKU-1", "qty": 1}], "amount": 1999}
        ).encode()
        for _ in range(3):
            req = urllib.request.Request(
                f"{args.gateway}/checkout",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            try:
                urllib.request.urlopen(req, timeout=15).read()
            except Exception as exc:  # noqa: BLE001
                print(f"  could not reach the gateway at {args.gateway}: {exc}")
                return 1
        # collector batch (5s) + tail sampling decision_wait (10s) + export.
        print("  fired 3 checkouts; waiting 30s for the pipeline\n")
        time.sleep(30)

    data = fetch(f"{args.jaeger}/api/traces?service=gateway-svc&limit={args.limit}")
    traces = data.get("data") or []
    if not traces:
        print("  no traces found - is the stack up? try `make up`")
        return 1

    # The richest trace is the one that completed through the whole fan-out.
    trace = max(traces, key=lambda t: len(services_of(t)))
    found = services_of(trace)
    pname = {k: v["serviceName"] for k, v in trace["processes"].items()}

    # ---- 1 ---------------------------------------------------------------
    missing = ALL_SERVICES - found
    check(
        f"one trace spans all five services ({len(trace['spans'])} spans)",
        not missing,
        f"trace {trace['traceID']}" + (f" missing {sorted(missing)}" if missing else ""),
    )

    # ---- 2 ---------------------------------------------------------------
    # Take each downstream SERVER span; those are the fan-out legs as observed
    # by the callee, which is the honest place to measure overlap.
    legs: dict[str, tuple[int, int]] = {}
    for s in trace["spans"]:
        svc = pname[s["processID"]]
        if svc in FANOUT_TARGETS and s["references"]:
            start, end = s["startTime"], s["startTime"] + s["duration"]
            # keep the outermost span per service
            if svc not in legs or (end - start) > (legs[svc][1] - legs[svc][0]):
                legs[svc] = (start, end)

    check(
        "all three fan-out legs present",
        set(legs) == FANOUT_TARGETS,
        f"found {sorted(legs)}",
    )

    if set(legs) == FANOUT_TARGETS:
        names = sorted(legs)
        t0 = min(v[0] for v in legs.values())
        for n in names:
            s, e = legs[n]
            print(f"      {n:<14} [{(s-t0)/1000:7.1f} .. {(e-t0)/1000:7.1f}] ms")
        overlapped = True
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = legs[names[i]], legs[names[j]]
                if not (a[0] < b[1] and b[0] < a[1]):
                    overlapped = False
                    print(f"      {names[i]} and {names[j]} DO NOT overlap")
        check("the three fan-out legs overlap in time (M1)", overlapped)

        slowest = max(legs, key=lambda k: legs[k][1] - legs[k][0])
        check(
            "payment-svc is the slowest leg (baseline ordering)",
            slowest == "payment-svc",
            f"slowest was {slowest}",
        )

    # ---- 3 ---------------------------------------------------------------
    versions: dict[str, str] = {}
    for proc in trace["processes"].values():
        for tag in proc.get("tags", []):
            if tag["key"] == "service.version":
                versions[proc["serviceName"]] = tag["value"]

    tagged = set(versions)
    check(
        "every service carries service.version (M24)",
        ALL_SERVICES <= tagged,
        ", ".join(f"{k}={v}" for k, v in sorted(versions.items()))
        or "no service.version tags found",
    )

    if args.expect_sha:
        wrong = {k: v for k, v in versions.items() if v != args.expect_sha}
        check(f"service.version == {args.expect_sha}", not wrong, str(wrong) if wrong else "")

    # ---- 4: M28 ----------------------------------------------------------
    # Spans are retained for days and readable by anyone with dashboard access,
    # so a literal amount or user handle in db.statement is a data leak with a
    # long tail. The parameterised text is kept - it is what makes the N+1
    # diagnosable - but the values must never ride along. This regresses
    # silently the first time someone builds a query by string concatenation.
    statements = [
        str(tag["value"])
        for s in trace["spans"]
        for tag in s.get("tags", [])
        if tag["key"] in ("db.statement", "db.query.text")
    ]
    leaked = [
        (needle, st)
        for st in statements
        for needle in ("user-1", "SKU-1", "1999")
        if needle in st
    ]
    check(
        f"no literal parameter values in db.statement ({len(statements)} captured, M28)",
        not leaked,
        f"LEAKED {leaked[0][0]!r} in: {leaked[0][1][:120]}" if leaked else "",
    )

    print("\n  TRACE GUARD " + ("PASS" if not fail else "FAIL") + "\n")
    return fail


if __name__ == "__main__":
    sys.exit(main())
