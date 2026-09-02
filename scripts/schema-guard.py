#!/usr/bin/env python3
"""Static guards on the ClickHouse schema, checked against SQL rather than prose.

The build plan phrases criterion 6 as:

    ! grep -riE "JOIN" clickhouse/schema/04_materialized_views.sql

which is right in spirit and wrong in practice: it fires on the comment that
*explains* why a join must not be there. Documenting a trap should not fail the
check for the trap. This strips comments first, then asserts on the DDL.

Four invariants, each guarding a defect that is silent in production:

  M2/M3  no join inside a classic (non-refreshable) materialized view
  F1     every counter in an AggregatingMergeTree is an aggregate-function type
  M4     no SummingMergeTree anywhere near a TDigest column
  M16    nothing reads or builds on the `jaeger` database
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "clickhouse" / "schema"

COUNTER_RE = re.compile(r"^\s*(\w*(?:request|error)_count)\s+(.+?),?\s*$", re.M | re.I)


def strip_sql_comments(sql: str) -> str:
    """Remove -- line comments and /* */ blocks, leaving executable SQL."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return "\n".join(re.sub(r"--.*$", "", line) for line in sql.splitlines())


def statements(sql: str) -> list[str]:
    return [s.strip() for s in strip_sql_comments(sql).split(";") if s.strip()]


def main() -> int:
    if not SCHEMA_DIR.is_dir():
        print(f"  no schema directory at {SCHEMA_DIR}")
        return 1

    files = sorted(SCHEMA_DIR.glob("*.sql"))
    fail = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal fail
        print(f"  {label:<62} {'OK' if ok else 'FAIL'}")
        if detail:
            print(f"      {detail}")
        if not ok:
            fail = 1

    print(f"\n  ClickHouse schema guard  ({len(files)} files)\n")

    all_stmts: list[tuple[str, str]] = []
    for f in files:
        for st in statements(f.read_text(encoding="utf-8")):
            all_stmts.append((f.name, st))

    # ---- M2/M3: no join in a classic materialized view --------------------
    offenders = []
    for name, st in all_stmts:
        if not re.search(r"CREATE\s+MATERIALIZED\s+VIEW", st, re.I):
            continue
        refreshable = re.search(r"\bREFRESH\s+EVERY\b", st, re.I)
        has_join = re.search(r"\bJOIN\b", st, re.I)
        if has_join and not refreshable:
            offenders.append(name)
    check(
        "M2/M3 no join inside a classic materialized view",
        not offenders,
        f"offending files: {offenders}" if offenders else
        "(the reconciler's self-join is exempt: REFRESH EVERY runs the full "
        "query on a timer, not per insert block)",
    )

    # ---- F1: counters in an AggregatingMergeTree --------------------------
    bad_counters = []
    agg_tables = 0
    for name, st in all_stmts:
        if not re.search(r"ENGINE\s*=\s*AggregatingMergeTree", st, re.I):
            continue
        agg_tables += 1
        for col, typ in COUNTER_RE.findall(st):
            if "SimpleAggregateFunction" not in typ and "AggregateFunction" not in typ:
                bad_counters.append(f"{name}: {col} {typ.strip()}")
    check(
        f"F1 counters in AggregatingMergeTree are aggregate types ({agg_tables} tables)",
        not bad_counters,
        "; ".join(bad_counters) if bad_counters else "",
    )

    # ---- M4: SummingMergeTree with sketches -------------------------------
    summing = [n for n, st in all_stmts
               if re.search(r"ENGINE\s*=\s*SummingMergeTree", st, re.I)]
    check("M4 no SummingMergeTree (it cannot merge TDigest sketches)",
          not summing, f"found in {summing}" if summing else "")

    # ---- M16: never build on Jaeger's database ----------------------------
    jaeger_refs = [n for n, st in all_stmts if re.search(r"\bjaeger\.", st, re.I)]
    check("M16 nothing reads or builds on the jaeger database",
          not jaeger_refs, f"found in {jaeger_refs}" if jaeger_refs else "")

    print("\n  SCHEMA GUARD " + ("PASS" if not fail else "FAIL") + "\n")
    return fail


if __name__ == "__main__":
    sys.exit(main())
