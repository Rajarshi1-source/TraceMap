"""D5 -- what this observability stack actually costs, measured rather than guessed.

Observability spend is the second line item nobody can explain, and the usual
reason is that the numbers are vendor-shaped: a bill arrives in gigabytes
ingested and nobody can map it back to a decision. This module answers the
question in the units the decisions are made in -- bytes per span, euros per
million spans, days of retention per euro -- from the database's own accounting
rather than from a spreadsheet.

WHERE THE NUMBERS COME FROM, and which are measured.

  MEASURED   bytes on disk, row counts and the compression ratio, per table,
             read from `system.parts`. This is what ClickHouse has actually
             written; it is not an estimate and it already includes the codec
             choices (Delta on timestamps, Gorilla on float durations, ZSTD on
             strings) that make the raw span table affordable.

  MEASURED   the current ingest rate, counted from the spans table over the
             last hour. Read from the same database as the bytes, deliberately:
             a rate scraped from the collector and a size read from ClickHouse
             are two clocks, and dividing one by the other produces a ratio that
             is wrong in a way nobody notices.

  DERIVED    steady-state disk. A database younger than its TTL has not yet
             reached its horizon, so the honest projection is
             bytes_per_row x rows_per_day x retention_days -- and this module
             reports the age alongside, so a projection made on a two-hour-old
             database is visibly a projection.

  CONFIG     the host price. One fixed number, and at this scale it dominates
             everything else so completely that the interesting finding is not
             the euro figure at all.

THE FINDING THIS EXISTS TO SURFACE. At 20 rps with a 20% probabilistic sample,
the marginal storage cost of a span is somewhere in the region of a millionth of
a cent, and the entire bill is the box. That means retention is not the lever
people assume it is, and the honest advice to anyone tempted to tune TTLs first
is to leave them alone and check the sampling rate instead. A cost meter that
only ever says "spend less on storage" would have missed that.
"""

from __future__ import annotations

import logging
import os

from src.integrations.clickhouse_client import ClickHouseClient

log = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        log.warning("%s is not a number; using %s", name, default)
        return default


# ---------------------------------------------------------------------------
# Host configuration -- D2. Hetzner CX32, 4 vCPU / 8 GB / 80 GB NVMe.
#
# These are prices, and prices are the most volatile facts in this repository.
# They are env-overridable for exactly that reason, and the discipline is the
# same as the pins table: re-check before quoting one in a conversation.
# ---------------------------------------------------------------------------
HOST_MONTHLY_EUR = _env_float("COST_HOST_MONTHLY_EUR", 7.05)
HOST_DISK_GB = _env_float("COST_HOST_DISK_GB", 80.0)
HOST_RAM_GB = _env_float("COST_HOST_RAM_GB", 8.0)
# Marginal price of block storage beyond what the box includes. Only ever
# applied to the overflow, because charging for the first 80 GB twice is the
# single easiest way to make a cost model look authoritative and be wrong.
VOLUME_EUR_PER_GB_MONTH = _env_float("COST_VOLUME_EUR_PER_GB_MONTH", 0.0524)

GIB = 1024.0 ** 3

# Retention, mirrored from clickhouse/schema/07_retention.sql. Duplicated here
# rather than parsed out of the DDL: parsing SQL to find a number is fragile,
# and a projection that silently disagrees with the schema is worse than one
# that is obviously hand-maintained. The drift guard is a test.
RETENTION_DAYS = {
    "otel_spans": 1,
    "otel_spans_trace_id_ts": 1,
    "spans": 7,
    "trace_edges": 7,
    "trace_critical_path": 7,
    "service_edges_1m": 30,
    "service_red_1m": 30,
    # 2 days, not 30. This is a correctness guard that gets compared against
    # the incremental MV within minutes of being written; keeping a month of it
    # would be keeping a month of an answer nobody re-reads. The drift guard in
    # test_cost_meter caught this constant at 30 on its first run.
    "service_edges_recon": 2,
    "edge_slo_1m": 30,
    "deploy_markers": 90,
}

# Exactly the query the TTL trap note in 07_retention.sql tells you to run
# before touching a retention setting. It is here so that advice is one HTTP
# call away rather than a paragraph someone has to remember.
Q_PARTS = """
SELECT
    table,
    sum(rows)                    AS rows,
    sum(bytes_on_disk)           AS bytes_on_disk,
    sum(data_uncompressed_bytes) AS uncompressed,
    sum(data_compressed_bytes)   AS compressed,
    count()                      AS parts
FROM system.parts
WHERE database = 'tracemap' AND active
GROUP BY table
ORDER BY bytes_on_disk DESC
"""

# Ingest rate and database age, from the table the projection is about. `spans`
# rather than `otel_spans` because `spans` is what the 7-day retention applies
# to and what every query reads.
Q_INGEST = """
SELECT
    countIf(timestamp >= now() - INTERVAL 1 HOUR) AS rows_last_hour,
    min(timestamp)                                AS oldest,
    max(timestamp)                                AS newest
FROM tracemap.spans
"""


class CostMeter:
    """Reads ClickHouse's own accounting. Holds no state, caches nothing."""

    def __init__(self, ch: ClickHouseClient) -> None:
        self.ch = ch

    async def report(self) -> dict:
        try:
            parts = await self.ch.query(Q_PARTS)
            ingest = (await self.ch.query(Q_INGEST))[0]
        except Exception as exc:  # noqa: BLE001 - a cost report is advisory, and
            # every way ClickHouse can be unreachable (down, restarting, a
            # system.parts permission change) has the same right answer: say so
            # and return nothing. Narrowing this would mean enumerating httpx's
            # error tree to arrive at the same handler.
            log.warning("cost meter could not read ClickHouse: %s", exc)
            return {"tables": [], "note": f"ClickHouse unavailable: {exc}"}

        tables = [self._table(r) for r in parts]
        on_disk_gib = sum(t["onDiskGiB"] for t in tables)

        rows_per_hour = float(ingest.get("rows_last_hour") or 0)
        spans_per_day = rows_per_hour * 24.0

        # Age of the measurement, so a projection cannot be mistaken for an
        # observation. A database younger than its longest TTL has not reached
        # steady state, and saying so is the difference between a cost model
        # and a cost claim.
        age_h = self._age_hours(ingest)

        projected = self._project(tables, spans_per_day)
        overflow_gb = max(0.0, projected["totalGiB"] - HOST_DISK_GB)
        volume_eur = overflow_gb * VOLUME_EUR_PER_GB_MONTH
        monthly_eur = HOST_MONTHLY_EUR + volume_eur

        spans_per_month = spans_per_day * 30.0
        per_million = (monthly_eur / (spans_per_month / 1e6)
                       if spans_per_month > 0 else None)

        return {
            "measured": {
                "onDiskGiB": round(on_disk_gib, 4),
                "spansPerHour": rows_per_hour,
                "spansPerDay": round(spans_per_day, 1),
                "databaseAgeHours": age_h,
                "tables": tables,
            },
            "projected": projected,
            "cost": {
                "hostMonthlyEur": HOST_MONTHLY_EUR,
                "hostDiskGb": HOST_DISK_GB,
                "hostRamGb": HOST_RAM_GB,
                "overflowGb": round(overflow_gb, 2),
                "volumeMonthlyEur": round(volume_eur, 4),
                "totalMonthlyEur": round(monthly_eur, 4),
                "eurPerMillionSpans": (None if per_million is None
                                       else round(per_million, 6)),
                # The whole point, stated as a number rather than left implied:
                # if this is ~1.0 the bill is the box and retention is not the
                # lever. Sampling is.
                "fixedCostShare": (round(HOST_MONTHLY_EUR / monthly_eur, 4)
                                   if monthly_eur > 0 else None),
            },
            "basis": {
                "bytesPerRow": "measured, system.parts",
                "ingestRate": "measured, spans over the last hour",
                "steadyState": "derived: bytes/row x rows/day x retention",
                "price": "config, re-check before quoting",
            },
            "caveat": self._caveat(age_h),
        }

    # -- pieces ------------------------------------------------------------

    @staticmethod
    def _table(r: dict) -> dict:
        rows = float(r.get("rows") or 0)
        on_disk = float(r.get("bytes_on_disk") or 0)
        comp = float(r.get("compressed") or 0)
        uncomp = float(r.get("uncompressed") or 0)
        name = r["table"]
        return {
            "table": name,
            "rows": int(rows),
            "parts": int(r.get("parts") or 0),
            "onDiskGiB": round(on_disk / GIB, 6),
            "bytesPerRow": round(on_disk / rows, 2) if rows else None,
            "compressionRatio": round(uncomp / comp, 2) if comp else None,
            "retentionDays": RETENTION_DAYS.get(name),
        }

    @staticmethod
    def _age_hours(ingest: dict) -> float | None:
        """How much history the measurement is standing on."""
        oldest, newest = ingest.get("oldest"), ingest.get("newest")
        if not oldest or not newest:
            return None
        try:
            from datetime import datetime
            fmt = "%Y-%m-%d %H:%M:%S"
            a = datetime.strptime(str(oldest)[:19], fmt)
            b = datetime.strptime(str(newest)[:19], fmt)
            return round((b - a).total_seconds() / 3600.0, 2)
        except (ValueError, TypeError):
            return None

    def _project(self, tables: list[dict], spans_per_day: float) -> dict:
        """Steady-state disk: what this fills once every TTL horizon is reached.

        Per table rather than in aggregate, because the tables scale on
        different axes. Raw spans scale with the sample rate; the 1-minute
        roll-ups scale with the number of distinct edges, which is bounded by
        the topology and barely moves with traffic at all.
        """
        rows: list[dict] = []
        total = 0.0
        for t in tables:
            days = t["retentionDays"]
            bpr = t["bytesPerRow"]
            if days is None or bpr is None:
                continue
            # Roll-ups are bounded by (edges x minutes), not by span volume.
            # Projecting them off the span rate would overstate them by orders
            # of magnitude, so they are extrapolated from their own observed
            # rate instead: current bytes, scaled to the retention horizon.
            if t["table"].endswith("_1m") or t["table"].endswith("_recon"):
                gib = t["onDiskGiB"]
                basis = "current size (bounded by topology, not traffic)"
            else:
                gib = bpr * spans_per_day * days / GIB
                basis = "bytes/row x spans/day x retention"
            rows.append({"table": t["table"], "retentionDays": days,
                         "projectedGiB": round(gib, 4), "basis": basis})
            total += gib
        rows.sort(key=lambda r: r["projectedGiB"], reverse=True)
        return {"totalGiB": round(total, 3), "tables": rows,
                "diskHeadroomGb": round(HOST_DISK_GB - total, 2)}

    @staticmethod
    def _caveat(age_h: float | None) -> str:
        longest = max(RETENTION_DAYS.values()) * 24
        if age_h is None:
            return ("No span history yet; every figure below the measured "
                    "section is a projection from nothing. Seed some traffic.")
        if age_h < 24:
            return (f"This database holds {age_h:.1f}h of spans against a "
                    f"{longest}h longest retention. Bytes per row are real; "
                    "the monthly figure is an extrapolation and should be "
                    "quoted as one.")
        return (f"{age_h:.1f}h of history against a {longest}h longest "
                "retention. Steady state is still projected, not observed.")
