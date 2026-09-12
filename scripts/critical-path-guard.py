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


# F49. Minutes of settling before a bucket is comparable. The reconciler's
# rolling 5-minute lookback means the newest bucket is always partially written
# on its side; 2 minutes clears it without discarding useful history.
RECON_SETTLE_MIN = 2

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

    # M5 is a PER-ROW mandate and has to be checked per row.
    #
    # This previously asserted avg(critical_ms) == 0.0 for both siblings, and
    # that is a claim the application deliberately contradicts: product-svc has
    # an N+1 query path that fires on carts above a threshold, and on those
    # traces product genuinely IS the slowest leg and genuinely does carry
    # blame. eval/cases/006 documents the same thing with its own measurements.
    # A mean of exactly zero is only satisfiable while traffic never crosses
    # that threshold, so the old check passed by luck and would have gone red on
    # a correct system under a heavier cart mix.
    #
    # Measured here, 359 rows: product-svc 98.1% zero, user-svc 100% zero,
    # payment-svc 1.9% zero. That mirror image is the thesis, and it is visible
    # per row in a way no average can express.
    rowwise = q(f"""
        SELECT target_service AS dst, count() AS n,
               countIf(critical_ms = 0) AS zero_rows,
               round(100 * countIf(critical_ms = 0) / count(), 1) AS pct_zero
        FROM tracemap.trace_edges
        WHERE source_service = 'order-svc'
          AND time_bucket >= now() - INTERVAL {args.window}
        GROUP BY dst
    """)
    zero_by = {r["dst"]: r for r in rowwise}

    for sibling in ("product-svc", "user-svc"):
        row = by.get(("order-svc", sibling))
        z = zero_by.get(sibling)
        # Present AND mostly zero. Missing is a different, worse failure: the
        # map would drop the edge rather than fade it, and a dropped edge is a
        # lie because the call really happened.
        check(f"order->{sibling}: zero-blame rows EXIST and dominate (M5)",
              bool(row) and bool(z) and z["zero_rows"] > 0 and z["pct_zero"] >= 90.0,
              "row missing entirely - the map would drop the edge, not fade it"
              if not row else
              f"{z['zero_rows']}/{z['n']} rows at exactly 0.0 ({z['pct_zero']}%)")

    prd_z = zero_by.get("product-svc")
    pay_z = zero_by.get("payment-svc")
    if prd_z and pay_z:
        # The thesis, as the mirror image it actually is: the slow-but-parallel
        # sibling is almost always blameless, the slowest sibling almost never
        # is. Stated as a comparison so it cannot be satisfied by a detector
        # that simply blames nothing.
        check("the thesis: parallel sibling blameless, slowest sibling blamed",
              prd_z["pct_zero"] >= 90.0 and pay_z["pct_zero"] <= 10.0,
              f"product-svc {prd_z['pct_zero']}% zero-blame vs "
              f"payment-svc {pay_z['pct_zero']}%")

    # F41. An edge cannot contribute more user-visible time than the call itself
    # took. This is arithmetic, not a tuning question, and it held silently false
    # for weeks: the assembler filled per-CALL rows from a map keyed by SERVICE
    # PAIR, so a retry or the saga's compensation gave both calls the pair total.
    #
    # Measured before the fix: 4.83% of order->payment rows, against 3.9% of its
    # traces making two or more calls. The most over-blamed edge was the one the
    # flagship exists to find. Averages hid it completely, which is why this
    # check counts ROWS rather than comparing means.
    viol = q(f"""
        SELECT source_service AS src, target_service AS dst,
               count() AS n,
               countIf(critical_ms > duration_ms + 0.001) AS bad,
               round(max(critical_ms - duration_ms), 1) AS worst_ms
        FROM tracemap.trace_edges
        WHERE time_bucket >= now() - INTERVAL {args.window}
        GROUP BY src, dst
        HAVING bad > 0
    """)
    check("F41 - no row is blamed for more than its own duration",
          not viol,
          "; ".join(f"{r['src']}->{r['dst']} {r['bad']}/{r['n']} rows, "
                    f"worst +{r['worst_ms']}ms" for r in viol) or "")

    # F41, second consequence. The invariant above catches per-row inflation;
    # this catches the roll-up. `crit_ms_sum` must equal the sum of the per-call
    # rows it aggregates, because the map's blame percentages are computed from
    # it. When each call carried the service-pair total, the roll-up
    # over-attributed by roughly the call count -- and unlike the row invariant,
    # this one produces no impossible value anywhere. It is simply too big, and
    # nothing about the number looks wrong.
    #
    # An INNER join per edge over the same window, not two grand totals: summing
    # each side independently compares periods where only one has rows.
    drift = q(f"""
        SELECT r.src AS src, r.dst AS dst,
               round(r.rollup, 1) AS rollup,
               round(t.raw, 1)    AS raw,
               abs(r.rollup - t.raw) / nullIf(t.raw, 0) AS frac
        FROM (
            SELECT source_service AS src, target_service AS dst,
                   sum(crit_ms_sum) AS rollup
            FROM tracemap.service_edges_1m
            WHERE time_bucket >= now() - INTERVAL {args.window}
            GROUP BY src, dst
        ) r
        INNER JOIN (
            SELECT source_service AS src, target_service AS dst,
                   sum(critical_ms) AS raw
            FROM tracemap.trace_edges
            WHERE time_bucket >= now() - INTERVAL {args.window}
            GROUP BY src, dst
        ) t ON r.src = t.src AND r.dst = t.dst
        WHERE frac > 0.001
    """)
    check("F41 - roll-up crit_ms_sum equals the sum of its per-call rows",
          not drift,
          "; ".join(f"{d['src']}->{d['dst']} rollup={d['rollup']} raw={d['raw']}"
                    for d in drift) or "")

    # Compare per (time_bucket, edge), not as two grand totals. The two sides
    # cover different spans of history - the reconciler works from tracemap.spans
    # on a rolling 5-minute lookback, the assembler from a watermark it advances
    # itself - so summing each over a wide window compares periods where only one
    # of them has rows and reports a divergence that is an artifact of the query.
    # An INNER join on the bucket is the apples-to-apples comparison.
    # F49. The TRAILING bucket is excluded, and that is not a loosened
    # tolerance -- it is declining to compare a number that is still being
    # written.
    #
    # The reconciler works from `tracemap.spans` on a rolling 5-minute
    # lookback, so the most recent minute is always partially populated on its
    # side while the assembler has already finished with it. Measured, on a
    # stack under steady load:
    #
    #   18:13  assembled 3366  reconciled 3366   0.00%
    #   18:14  assembled 3414  reconciled 3414   0.00%
    #   ...
    #   18:19  assembled 3520  reconciled 2881  22.18%   <- still being written
    #
    # Seven settled buckets agreeing to within 0.85%, and one in-flight bucket
    # 22% low, is not a 3.5% divergence in the pipeline. It is a comparison
    # that included data that had not arrived. The same reasoning as the
    # assembler's own LATE_SPAN_GRACE_S, applied to the thing that checks it.
    joined = q(f"""
        SELECT a.time_bucket AS tb, a.src AS src, a.dst AS dst,
               a.n AS assembled, r.n AS reconciled
        FROM (
            SELECT toStartOfMinute(time_bucket) AS time_bucket,
                   source_service AS src, target_service AS dst, count() AS n
            FROM tracemap.trace_edges
            WHERE time_bucket >= now() - INTERVAL {args.window}
              AND time_bucket <  now() - INTERVAL {RECON_SETTLE_MIN} MINUTE
            GROUP BY time_bucket, src, dst
        ) a
        INNER JOIN (
            SELECT time_bucket, source_service AS src, target_service AS dst,
                   max(request_count) AS n
            FROM tracemap.service_edges_recon
            WHERE time_bucket >= now() - INTERVAL {args.window}
              AND time_bucket <  now() - INTERVAL {RECON_SETTLE_MIN} MINUTE
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
