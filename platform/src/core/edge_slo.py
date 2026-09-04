"""D6 -- service-level objectives on critical-path latency, per dependency edge.

The premise of the whole project, expressed as an error budget.

An ordinary latency SLO on a dependency edge measures wall-clock duration, and
in a parallel fan-out that measurement is wrong in both directions. A sibling
that runs for 700 ms alongside a 720 ms sibling contributes nothing to what the
user waited for; billing it against an error budget pages an engineer for a
non-problem, and after the third such page nobody reads the alert. Conversely
the edge that IS on the critical path gets averaged together with every request
where it was not, so its budget burn understates real user impact.

So the objectives here are stated against `critical_ms`. Read one way this is a
small change -- a different column in a WHERE clause. Read another way it is the
only latency SLO here that can be satisfied by an edge that is slow and violated
by an edge that is fast, which is exactly right.

WHY THE CATALOGUE IS PYTHON AND NOT YAML. Four objectives do not justify a new
dependency, and a frozen dataclass is diffable, type-checked and reviewable in a
pull request in a way a parsed dict is not. If this ever grows past a page, move
it -- but move it because it grew, not because YAML feels more like config.

WHAT THIS MODULE DOES NOT DO. It does not page anyone. It computes ratios and
burn rates and exposes them; the multi-window burn-rate alerting is expressed in
`monitoring/prometheus-rules.yml` alongside the checkout SLO it was modelled on,
because two burn-rate policies written in two different systems drift apart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.integrations.clickhouse_client import ClickHouseClient

log = logging.getLogger(__name__)

# The bucket bounds materialised by clickhouse/schema/08_edge_slo.sql. An
# objective must land on one of these: the histogram is what makes the SLI a
# ratio of countable events rather than a threshold on a percentile, and a bound
# that does not exist cannot be counted after the fact.
BOUNDS_MS: tuple[float, ...] = (25.0, 50.0, 100.0, 250.0, 500.0, 1000.0)

_COL = {
    25.0: "crit_le_25_count",
    50.0: "crit_le_50_count",
    100.0: "crit_le_100_count",
    250.0: "crit_le_250_count",
    500.0: "crit_le_500_count",
    1000.0: "crit_le_1000_count",
}

# The one wall-clock bound the roll-up carries, kept purely for the
# counterfactual below. It is evidence, never an objective.
WALL_BOUND_MS = 250.0


@dataclass(frozen=True)
class Objective:
    """One edge's promise, in the only three numbers an SLO needs."""

    source: str
    target: str
    objective_ms: float          # must be a member of BOUNDS_MS
    target_ratio: float          # e.g. 0.99 -- the fraction that must be good
    note: str = ""

    def __post_init__(self) -> None:
        if self.objective_ms not in _COL:
            raise ValueError(
                f"{self.source}->{self.target}: objective {self.objective_ms}ms "
                f"is not a materialised bound {sorted(_COL)}. Add the bucket to "
                "08_edge_slo.sql first; an objective the roll-up cannot count is "
                "an objective that silently never fires."
            )
        if not 0.0 < self.target_ratio < 1.0:
            raise ValueError("target_ratio must be strictly between 0 and 1")

    @property
    def edge(self) -> str:
        return f"{self.source}->{self.target}"

    @property
    def budget(self) -> float:
        """The fraction of requests allowed to be bad. 0.99 -> 0.01."""
        return 1.0 - self.target_ratio


# ---------------------------------------------------------------------------
# The catalogue.
#
# Objectives are set with roughly 2x headroom over the measured baselines in the
# README service table, because an SLO pinned to the current p99 is a promise
# never to have a slow day and will be muted within a fortnight.
#
# order-svc's three downstream edges are the interesting ones, and two of them
# are deliberately given the SAME objective despite very different wall-clock
# behaviour. product-svc runs ~700 ms of wall clock and payment-svc ~720 ms;
# under a wall-clock SLO product-svc would be in permanent violation. Under this
# one it sits near zero burn, because those 700 ms are spent alongside a slower
# sibling and cost the user nothing. That contrast is the demo.
# ---------------------------------------------------------------------------
OBJECTIVES: tuple[Objective, ...] = (
    Objective(
        "gateway-svc", "order-svc", 1000.0, 0.99,
        "The user-facing edge. Its critical-path share is nearly the whole "
        "checkout, so this is the closest thing here to a product SLO.",
    ),
    Objective(
        "order-svc", "payment-svc", 250.0, 0.99,
        "Slowest sibling by design, so normally ON the critical path, and the "
        "edge that should burn budget when checkout degrades.",
    ),
    Objective(
        "order-svc", "product-svc", 250.0, 0.99,
        "The same objective as payment-svc, on purpose. ~700ms of wall clock "
        "and near-zero critical-path share: under a wall-clock SLO this edge is "
        "permanently red, under this one it is healthy. Case 006 is the proof.",
    ),
    Objective(
        "order-svc", "user-svc", 100.0, 0.99,
        "The fast sibling. A tight objective because it has no excuse to be on "
        "the critical path at all.",
    ),
)

# Below this many requests a ratio is noise. One bad request in three is not a
# 33% burn rate, it is three requests. The same guard the detector applies as
# min_requests, for the same reason, and it is why an idle stack reports
# insufficient data rather than a healthy 100%.
MIN_REQUESTS = 100


def _query(window_min: int) -> str:
    """One query, every edge, one window. Three round trips total, not 3xN."""
    cols = ",\n    ".join(f"sum({c}) AS {c}" for c in _COL.values())
    return f"""
SELECT
    source_service         AS source,
    target_service         AS target,
    sum(total_count)       AS total,
    sum(error_count)       AS errors,
    sum(wall_le_250_count) AS wall_le_250,
    {cols}
FROM tracemap.edge_slo_1m
WHERE time_bucket >= now() - INTERVAL {window_min} MINUTE
GROUP BY source, target
HAVING total > 0
"""


class EdgeSLOEvaluator:
    """Reads the roll-up and returns budget state. Holds no state of its own."""

    # 30 days is the budget window; 1h and 5m are the two burn-rate windows,
    # matching CheckoutErrorBudgetFastBurn. The long window proves a real burn,
    # the short one proves it is still happening -- without the short window the
    # alert keeps paging for an hour after the incident is over, which trains
    # people to ignore it.
    LONG_MIN = 60
    SHORT_MIN = 5
    BUDGET_DAYS = 30
    FAST_BURN = 14.4

    def __init__(self, ch: ClickHouseClient,
                 objectives: tuple[Objective, ...] = OBJECTIVES) -> None:
        self.ch = ch
        self.objectives = objectives

    async def _rows(self, window_min: int) -> dict[tuple[str, str], dict]:
        rows = await self.ch.query(_query(window_min))
        return {(r["source"], r["target"]): r for r in rows}

    async def evaluate(self) -> dict:
        """Budget state for every catalogued edge.

        Returns a note and no edges rather than raising if the roll-up is
        missing. A stack booted on a pre-D6 ClickHouse volume will not have
        `edge_slo_1m`, and an SLO page that 500s is worse than one saying it has
        nothing to show yet.
        """
        try:
            long_w = await self._rows(self.LONG_MIN)
            short_w = await self._rows(self.SHORT_MIN)
            budget_w = await self._rows(self.BUDGET_DAYS * 24 * 60)
        except Exception as exc:  # noqa: BLE001 - the expected failure here is a
            # missing table on a pre-D6 ClickHouse volume, which surfaces as a
            # generic 404-ish server error rather than a typed exception. An SLO
            # page that 500s reads as a broken platform; one that says the
            # roll-up is absent points at the fix.
            log.warning("edge SLO roll-up unavailable: %s", exc)
            return {
                "edges": [],
                "note": ("tracemap.edge_slo_1m is not available -- apply "
                         "clickhouse/schema/08_edge_slo.sql, or boot on a clean "
                         "volume so initdb applies it"),
            }

        return {
            "windows": {
                "burnLongMin": self.LONG_MIN,
                "burnShortMin": self.SHORT_MIN,
                "budgetDays": self.BUDGET_DAYS,
            },
            "minRequests": MIN_REQUESTS,
            "measuredOn": "critical_ms",
            "edges": [self._one(o, long_w, short_w, budget_w)
                      for o in self.objectives],
        }

    def _one(self, o: Objective, long_w: dict, short_w: dict,
             budget_w: dict) -> dict:
        key = (o.source, o.target)
        col = _COL[o.objective_ms]

        def bad(rows: dict) -> float | None:
            r = rows.get(key)
            if r is None:
                return None
            total = float(r["total"] or 0)
            if total < MIN_REQUESTS:
                return None
            good = float(r[col] or 0)
            return max(0.0, 1.0 - good / total)

        bad_long = bad(long_w)
        bad_short = bad(short_w)
        bad_budget = bad(budget_w)

        def burn(b: float | None) -> float | None:
            return None if b is None else b / o.budget

        burn_long, burn_short = burn(bad_long), burn(bad_short)

        # The counterfactual, and the reason this table carries a wall-clock
        # column it never bills against: the same edge, the same window, budget
        # burnt under the measure everyone else uses. On a parallel sibling the
        # two numbers are wildly different, and that difference is the argument.
        row = budget_w.get(key)
        wall_bad = None
        if (row is not None
                and o.objective_ms == WALL_BOUND_MS
                and float(row["total"] or 0) >= MIN_REQUESTS):
            wall_bad = max(0.0,
                           1.0 - float(row["wall_le_250"] or 0) / float(row["total"]))

        return {
            "edge": o.edge,
            "source": o.source,
            "target": o.target,
            "objectiveMs": o.objective_ms,
            "targetRatio": o.target_ratio,
            "note": o.note,
            "requests": int(float(row["total"])) if row else 0,
            # The SLI is stated as the good ratio, because that is the number
            # the objective is written in. Burn rate is reported separately
            # rather than left for the reader to derive.
            "sli": None if bad_budget is None else 1.0 - bad_budget,
            "budgetRemaining": (None if bad_budget is None
                                else 1.0 - bad_budget / o.budget),
            "burnRateLong": burn_long,
            "burnRateShort": burn_short,
            # 14.4x on both windows is the page condition: it consumes 2% of a
            # 30-day budget in an hour.
            "fastBurn": bool(burn_long is not None and burn_short is not None
                             and burn_long > self.FAST_BURN
                             and burn_short > self.FAST_BURN),
            "wallClockBad": wall_bad,
            "insufficientData": bad_budget is None,
        }
