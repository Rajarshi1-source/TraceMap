"""D5 -- the cost meter, and the drift guard that keeps it honest.

Two kinds of test here.

The arithmetic tests pin the one modelling decision that is easy to get wrong
and impossible to notice: roll-up tables must NOT be projected off the span
ingest rate. `service_edges_1m` grows with the number of distinct edges times
minutes, which the topology bounds; projecting it off spans-per-day overstates
it by three or four orders of magnitude and produces a cost model that says
"stop keeping roll-ups", which is precisely backwards.

The drift guard compares RETENTION_DAYS against the actual TTLs in the schema.
That table is hand-maintained -- parsing SQL to find a number is fragile, and a
projection that silently disagrees with the DDL is worse than one that is
obviously duplicated. This test is the price of that choice.
"""

from __future__ import annotations

import asyncio
import pathlib
import re

import pytest

from src.core import cost_meter
from src.core.cost_meter import GIB, RETENTION_DAYS, CostMeter

SCHEMA_DIR = pathlib.Path(__file__).resolve().parents[2] / "clickhouse" / "schema"


class FakeCH:
    def __init__(self, parts, ingest, fail=False):
        self.parts, self.ingest, self.fail = parts, ingest, fail

    async def query(self, sql, params=None):
        if self.fail:
            raise RuntimeError("connection refused")
        return self.ingest if "spans" in sql and "rows_last_hour" in sql else self.parts


def part(table, rows, bytes_on_disk, *, uncompressed=None, parts=1):
    return {
        "table": table, "rows": rows, "bytes_on_disk": bytes_on_disk,
        "compressed": bytes_on_disk,
        "uncompressed": uncompressed if uncompressed is not None else bytes_on_disk * 6,
        "parts": parts,
    }


def report(parts_rows, rows_last_hour=36_000,
           oldest="2026-09-04 08:00:00", newest="2026-09-04 12:00:00"):
    ch = FakeCH(parts_rows,
                [{"rows_last_hour": rows_last_hour,
                  "oldest": oldest, "newest": newest}])
    return asyncio.run(CostMeter(ch).report())


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def test_bytes_per_row_is_measured_not_assumed():
    """The one figure the entire model rests on, taken from system.parts."""
    r = report([part("spans", rows=1_000_000, bytes_on_disk=180_000_000)])
    spans = r["measured"]["tables"][0]

    assert spans["bytesPerRow"] == pytest.approx(180.0)
    assert spans["compressionRatio"] == pytest.approx(6.0)
    assert r["basis"]["bytesPerRow"].startswith("measured")


def test_an_empty_table_reports_no_bytes_per_row_rather_than_dividing_by_zero():
    r = report([part("spans", rows=0, bytes_on_disk=0)])
    assert r["measured"]["tables"][0]["bytesPerRow"] is None
    assert r["measured"]["tables"][0]["compressionRatio"] is None


# ---------------------------------------------------------------------------
# Projection -- the modelling decision worth a test
# ---------------------------------------------------------------------------

def test_each_table_is_projected_from_its_own_growth():
    """current_bytes x retention / observed_age, per table, no taxonomy.

    The fixture gives four hours of history (0.1667 days), so a 7-day table
    scales by 42x and a 30-day table by 180x. Same input bytes, different TTL,
    proportionally different projection -- and no rule anywhere that has to
    know what kind of table it is looking at.
    """
    r = report([
        part("spans", rows=1_000_000, bytes_on_disk=180_000_000),
        part("service_edges_1m", rows=1_000_000, bytes_on_disk=180_000_000),
    ])
    proj = {t["table"]: t for t in r["projected"]["tables"]}
    current_gib = 180_000_000 / GIB
    age_days = 4.0 / 24.0

    assert proj["spans"]["projectedGiB"] == pytest.approx(
        current_gib * 7 / age_days, rel=1e-3)
    assert proj["service_edges_1m"]["projectedGiB"] == pytest.approx(
        current_gib * 30 / age_days, rel=1e-3)


def test_an_event_driven_table_is_not_billed_as_span_traffic():
    """The regression test for the bug that running this thing exposed.

    `deploy_markers` is a row per release with 90-day retention. The first
    version of this model classified tables by name -- anything not ending
    `_1m` or `_recon` was projected as bytes/row x SPANS/day x retention --
    and produced 3,477 GiB for a table holding a few kilobytes, turning a
    EUR 7.05 bill into EUR 191.

    Ninety days of a table that gains a row per deploy must stay small no
    matter how many spans are flowing past it.
    """
    r = report([
        part("spans", rows=5_000_000, bytes_on_disk=280_000_000),
        part("deploy_markers", rows=40, bytes_on_disk=8_000),
    ], rows_last_hour=1_700_000)          # a very high span rate
    proj = {t["table"]: t for t in r["projected"]["tables"]}

    # 8 KB over 4 hours, at 90 days: about 4.3 MB. Not 3.4 TB.
    assert proj["deploy_markers"]["projectedGiB"] < 0.01, (
        "deploy_markers is being scaled by span volume again")
    assert proj["deploy_markers"]["projectedGiB"] < proj["spans"]["projectedGiB"]


def test_a_table_older_than_its_own_ttl_projects_to_its_current_size():
    """Past the horizon there is nothing left to project: it IS steady state."""
    # 200 hours of history against otel_spans' 1-day retention.
    r = report([part("otel_spans", rows=1_000_000, bytes_on_disk=90_000_000)],
               oldest="2026-08-27 04:00:00", newest="2026-09-04 12:00:00")
    row = r["projected"]["tables"][0]

    assert row["projectedGiB"] == pytest.approx(90_000_000 / GIB, rel=1e-3)
    assert "past its own TTL" in row["basis"]


def test_a_recent_traffic_ramp_is_named_in_the_caveat():
    """The known weakness of scaling from a lifetime average, stated aloud.

    Measured on this repository: a lifetime average of ~4.5M spans/day against
    a last-hour rate of 41.5M/day, because the eval overlay's load generator
    had recently started. The projection is then ~9x low, and a reader has no
    way to see that from the numbers alone.
    """
    # 1M rows over 4h is 6M/day lifetime; 3M/h is 72M/day current -- 12x.
    r = report([part("spans", rows=1_000_000, bytes_on_disk=180_000_000)],
               rows_last_hour=3_000_000)

    assert r["measured"]["rateRatio"] > 2.0
    assert "UNDERSTATES" in r["caveat"]


def test_a_table_with_no_declared_retention_is_skipped_not_guessed():
    """system.parts also returns tables this model knows nothing about.

    Assigning them a default retention would invent disk. Leaving them out
    understates by a known amount, which is the better error.
    """
    r = report([part("some_new_table", rows=1000, bytes_on_disk=1_000_000)])
    assert r["projected"]["tables"] == []


# ---------------------------------------------------------------------------
# The finding the meter exists to surface
# ---------------------------------------------------------------------------

def test_at_demo_scale_the_bill_is_the_box():
    """The whole point of D5, asserted rather than claimed in a README.

    A few gigabytes fits inside the 80GB the CX32 already includes, so storage
    is marginally free and `fixedCostShare` is 1.0. The lever is the sampling
    rate, not the TTLs -- and this test is what stops that sentence from
    becoming a thing someone wrote once and never checked.
    """
    r = report([part("spans", rows=1_000_000, bytes_on_disk=180_000_000)])
    c = r["cost"]

    assert c["overflowGb"] == 0.0
    assert c["volumeMonthlyEur"] == 0.0
    assert c["totalMonthlyEur"] == pytest.approx(cost_meter.HOST_MONTHLY_EUR)
    assert c["fixedCostShare"] == pytest.approx(1.0)
    assert c["eurPerMillionSpans"] is not None


def test_overflow_is_charged_only_beyond_what_the_box_includes():
    """Charging for the first 80GB twice is the easiest way to be wrong."""
    # 3 GiB accumulated in four hours, at a 7-day TTL: a 42x scale, well past 80.
    r = report([part("spans", rows=20_000_000, bytes_on_disk=3 * 1024**3)])
    c = r["cost"]

    projected = r["projected"]["totalGiB"]
    assert projected > cost_meter.HOST_DISK_GB
    assert c["overflowGb"] == pytest.approx(
        projected - cost_meter.HOST_DISK_GB, abs=0.01)
    assert c["volumeMonthlyEur"] > 0
    assert c["fixedCostShare"] < 1.0


def test_no_ingest_means_no_cost_per_span_rather_than_a_division_by_zero():
    r = report([part("spans", rows=0, bytes_on_disk=0)], rows_last_hour=0)
    assert r["cost"]["eurPerMillionSpans"] is None


# ---------------------------------------------------------------------------
# Honesty about what is measured
# ---------------------------------------------------------------------------

def test_a_young_database_says_so_in_the_caveat():
    """Four hours of history against a 90-day retention is a projection."""
    r = report([part("spans", rows=1000, bytes_on_disk=180_000)])
    assert r["measured"]["databaseAgeHours"] == pytest.approx(4.0)
    assert "extrapolation" in r["caveat"]


def test_an_empty_database_says_it_has_nothing_to_stand_on():
    r = report([], rows_last_hour=0, oldest=None, newest=None)
    assert r["measured"]["databaseAgeHours"] is None
    assert "projection from nothing" in r["caveat"]


def test_clickhouse_being_down_returns_a_note_not_an_exception():
    ch = FakeCH([], [], fail=True)
    r = asyncio.run(CostMeter(ch).report())
    assert r["tables"] == []
    assert "unavailable" in r["note"]


# ---------------------------------------------------------------------------
# The drift guard
# ---------------------------------------------------------------------------

# TTLs are declared two ways in this schema and both have to be parsed, because
# missing one silently makes a table look retention-free.
#
#   inline    CREATE TABLE ... ENGINE = ... TTL deployed_at + INTERVAL 90 DAY;
#   deferred  ALTER TABLE ... MODIFY TTL ... INTERVAL 7 DAY;    (07_retention.sql)
#
# Statements are split on the semicolon and each one is asked for its table and
# its interval, rather than writing one regex that spans both forms.
TABLE_RE = re.compile(
    r"(?:CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?|ALTER\s+TABLE)\s+tracemap\.(\w+)",
    re.IGNORECASE,
)
TTL_RE = re.compile(r"(?:^|\s)TTL\s.*?INTERVAL\s+(\d+)\s+(DAY|HOUR)",
                    re.IGNORECASE | re.DOTALL)


def _strip_comments(sql: str) -> str:
    """A TTL inside a `-- TRAP:` note is documentation, not a declaration."""
    return chr(10).join(line.split("--", 1)[0] for line in sql.splitlines())


def _ttls_from_schema() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in sorted(SCHEMA_DIR.glob("*.sql")):
        for stmt in _strip_comments(path.read_text(encoding="utf-8")).split(";"):
            table = TABLE_RE.search(stmt)
            ttl = TTL_RE.search(stmt)
            if not (table and ttl):
                continue
            n, unit = int(ttl.group(1)), ttl.group(2).upper()
            days = n / 24 if unit == "HOUR" else n
            # A sub-day TTL rounds up to 1: the projection is monthly, and the
            # difference between 1 hour and 24 is noise against 30 days.
            found[table.group(1)] = max(1, int(days))
    return found


def test_the_schema_actually_declares_ttls():
    """Guards the guard.

    A parser that silently matches nothing passes both tests below while
    asserting precisely nothing -- and that is not hypothetical: the first
    version of this parser only understood `ALTER TABLE ... MODIFY TTL` and
    missed the two tables that declare their TTL inline.
    """
    schema = _ttls_from_schema()
    assert len(schema) >= 10, f"only parsed {schema} -- the TTL parser is broken"
    assert schema["deploy_markers"] == 90, "inline TTLs are not being parsed"


def test_retention_table_matches_the_schema():
    """RETENTION_DAYS is duplicated from 07_retention.sql. This is the tax.

    A projection computed against a retention the database no longer uses is
    wrong in a way that never surfaces -- the numbers stay plausible. So the
    duplication is allowed, and this test is what makes it safe.
    """
    schema = _ttls_from_schema()
    assert schema, "no TTLs parsed out of clickhouse/schema -- fix this test"

    mismatched = {t: (RETENTION_DAYS.get(t), d) for t, d in schema.items()
                  if RETENTION_DAYS.get(t) != d}
    assert not mismatched, (
        f"cost_meter.RETENTION_DAYS disagrees with the schema {mismatched} "
        "(table: (in cost_meter, in DDL))"
    )


def test_every_table_the_meter_projects_actually_has_a_ttl():
    """The reverse direction: a retention for a table that no longer exists."""
    schema = _ttls_from_schema()
    orphans = sorted(set(RETENTION_DAYS) - set(schema))
    assert not orphans, f"RETENTION_DAYS names tables with no TTL in the DDL: {orphans}"
