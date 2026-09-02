#!/usr/bin/env python3
"""Phase 4 exit criteria, checked against the live database.

The unit tests prove the algorithm. This proves the whole pipeline: real traces,
through the collector, into ClickHouse, through the assembler, out as blame.

  * a parallel sibling has a critical_ms = 0.0 row, AND THE ROW EXISTS (M5)
  * order->payment has non-zero critical_ms
  * assembler edge counts agree with the independent reconciler within 2%
  * the assembler heartbeat is advancing
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

CH = "http://localhost:8123"
USER, PWD, DB = "tracemap", "change-me-locally", "tracemap"


def q(sql: str) -> list[dict]:
    req = urllib.request.Request(
        f"{CH}/?database={DB}",
        data=(sql.strip().rstrip(";") + "\nFORMAT JSONEachRow").encode(),
        headers={"X-ClickHouse-User": USER, "X-ClickHouse-Key": PWD},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return [json.loads(x) for x in r.read().decode().splitlines() if x.strip()]


def metric(name: str) -> float:
    with urllib.request.urlopen("http://localhost:9100/metrics", timeout=10) as r:
        for line in r.read().decode().splitlines():
            if line.startswith(name):
                return float(line.rsplit(" ", 1)[1])
    return -1.0


def main() -> int:
    ap = argparse.ArgumentParser()
    # Narrow by default and deliberately so. The reconciler works from
    # tracemap.spans on a rolling lookback; the assembler works from a watermark
    # it advances itself and which resets to now()-lookback on restart. Compare
    # over a wide window and you are comparing periods where only one of them
    # was running, which reports a divergence that says nothing about
    # correctness. This check is meaningful in steady state.
    ap.add_argument("--window", default="6 MINUTE")
    ap.add_argument("--no-generate", action="store_true",
                    help="rely on ambient traffic instead of firing checkouts")
    args = ap.parse_args()

    fail = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal fail
        print(f"  {label:<58} {'OK' if ok else 'FAIL'}")
        if detail:
            print(f"      {detail}")
        if not ok:
            fail = 1

    print("\n  Phase 4 critical-path guard\n")

    rows = q(f"""
        SELECT source_service AS src, target_service AS dst, count() AS n,
               round(avg(duration_ms), 1) AS wall_ms,
               round(avg(critical_ms), 1) AS crit_ms
        FROM tracemap.trace_edges
        WHERE time_bucket >= now() - INTERVAL {args.window}
        GROUP BY src, dst
    """)
    if not rows:
        print("  no assembled edges in the window - is trace-assembler running?")
        return 1

    by = {(r["src"], r["dst"]): r for r in rows}
    for r in sorted(rows, key=lambda r: -r["wall_ms"]):
        print(f"      {r['src']:<13} -> {r['dst']:<13} "
              f"n={r['n']:<4} wall={r['wall_ms']:>7}ms  crit={r['crit_ms']:>7}ms")
    print()

    pay = by.get(("order-svc", "payment-svc"))
    check("order->payment has non-zero critical_ms",
          bool(pay) and pay["crit_ms"] > 0,
          f"crit_ms={pay['crit_ms']}" if pay else "edge missing")

    for sibling in ("product-svc", "user-svc"):
        row = by.get(("order-svc", sibling))
        # M5 - present AND zero. Missing is a different, worse failure.
        check(f"order->{sibling} row EXISTS with critical_ms = 0.0 (M5)",
              bool(row) and row["crit_ms"] == 0.0,
              "row missing entirely - the map would drop the edge, not fade it"
              if not row else f"crit_ms={row['crit_ms']}")

    if pay and by.get(("order-svc", "product-svc")):
        prd = by[("order-svc", "product-svc")]
        check("the thesis: a slow edge carries zero blame",
              prd["wall_ms"] > 20 and prd["crit_ms"] == 0.0,
              f"product-svc: {prd['wall_ms']}ms wall clock, {prd['crit_ms']}ms attributed")

    # Compare per (time_bucket, edge), not as two grand totals. The two sides
    # cover different spans of history - the reconciler works from tracemap.spans
    # on a rolling 5-minute lookback, the assembler from a watermark it advances
    # itself - so summing each over a wide window compares periods where only one
    # of them has rows and reports a divergence that is an artifact of the query.
    # An INNER join on the bucket is the apples-to-apples comparison.
    joined = q(f"""
        SELECT a.time_bucket AS tb, a.src AS src, a.dst AS dst,
               a.n AS assembled, r.n AS reconciled
        FROM (
            SELECT toStartOfMinute(time_bucket) AS time_bucket,
                   source_service AS src, target_service AS dst, count() AS n
            FROM tracemap.trace_edges
            WHERE time_bucket >= now() - INTERVAL {args.window}
            GROUP BY time_bucket, src, dst
        ) a
        INNER JOIN (
            SELECT time_bucket, source_service AS src, target_service AS dst,
                   max(request_count) AS n
            FROM tracemap.service_edges_recon
            WHERE time_bucket >= now() - INTERVAL {args.window}
            GROUP BY time_bucket, src, dst
        ) r USING (time_bucket, src, dst)
    """)
    if not joined:
        check("assembler agrees with the reconciler within 2%", False,
              "no overlapping buckets yet - let both run for a few minutes")
    else:
        tot_a = sum(r["assembled"] for r in joined)
        tot_r = sum(r["reconciled"] for r in joined)
        div = abs(tot_a - tot_r) / tot_r * 100 if tot_r else 0.0
        check("assembler agrees with the reconciler within 2%",
              div <= 2.0,
              f"{len(joined)} overlapping buckets: "
              f"assembled={tot_a} reconciled={tot_r} divergence={div:.2f}%")

    before = metric("tracemap_worker_cycle_total")
    time.sleep(20)
    after = metric("tracemap_worker_cycle_total")
    check("assembler heartbeat is advancing",
          after > before, f"{before} -> {after}")

    print("\n  CRITICAL-PATH GUARD " + ("PASS" if not fail else "FAIL") + "\n")
    return fail


if __name__ == "__main__":
    sys.exit(main())
