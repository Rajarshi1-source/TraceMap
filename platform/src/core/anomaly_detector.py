"""EWMA-baselined anomaly detection over per-edge critical-path latency.

Deliberately NOT a machine-learning model. An EWMA is explainable, has one
tunable parameter, needs no training data, and every alert it fires can be
justified to the person it woke up. That is worth more than an unexplainable
couple of points of F1 - and the eval harness in Phase 7 means the claim is
testable rather than asserted: if a model beat this on the golden set, the
number would say so.

Five gates, in order. Each exists because of a specific way alerting goes wrong:

  1. request-count floor   a p99 over four requests is noise wearing a number
  2. cold start            never alert the first time an edge is seen
  3. warmup                a baseline built from two samples is not a baseline
  4. breach + EWMA update  <- M6 LIVES HERE. READ THE COMMENT BEFORE EDITING.
  5. sustained window      one bad minute is weather, three is a trend
  6. dedup                 SET NX EX, so N replicas produce one alert
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DetectorConfig:
    alpha: float = 0.2            # EWMA smoothing once armed; higher adapts faster
    # Convergence rate DURING warmup only.
    #
    # alpha=0.2 is right for a steady state - it resists noise. It is wrong for
    # warmup, because the baseline is seeded from a SINGLE first observation and
    # 0.2 cannot walk that off in five cycles. Measured: a seed that happened to
    # land on a 146ms outlier against a true level of ~110ms left the baseline
    # at 139.5ms when the detector armed - 28ms high.
    #
    # That inflation is subtracted directly from every future delta. A 60ms
    # injection raised the real level by ~65ms, but against an inflated baseline
    # the observed delta was only ~40ms, under the 50ms threshold, and the fault
    # went undetected. The detector was behaving correctly; the baseline was
    # wrong.
    #
    # So: converge fast while warming, then settle. Same estimator, two regimes.
    warmup_alpha: float = 0.5
    threshold_ms: float = 50.0    # the brief's ">50ms" requirement
    min_requests: int = 100       # statistical floor per evaluation window
    # Breaches required, and the window they must land in.
    #
    # "3 CONSECUTIVE" is fragile at a realistic signal-to-noise ratio. Measured
    # live: baseline 135ms, a +60ms fault, and bucket-to-bucket p99 noise of
    # +/-20ms - so the delta oscillates either side of the 50ms threshold and a
    # single dip resets the run. The counter reached 2, reset, reached 1, reset,
    # and a genuine fault went undetected for 472 seconds.
    #
    # "3 within 5" keeps the same evidence bar - one bad minute still never
    # alerts - while tolerating the noise that a real p99 actually has.
    sustained_cycles: int = 3
    sustained_window: int = 5
    dedup_ttl_s: int = 600
    baseline_ttl_s: int = 86_400
    warmup_cycles: int = 5        # don't alert while the baseline is cold

    # Error rate gets a FIXED threshold, not an EWMA baseline, and the
    # asymmetry with latency is deliberate.
    #
    # "Normal" latency is genuinely environment-dependent -- 3am differs from
    # 3pm, a bigger instance differs from a smaller one -- so a baseline that
    # learns is the right tool and a fixed millisecond threshold is not.
    # "Normal" error rate is not environment-dependent. Zero is the target.
    # An EWMA here would mean a service that has been failing 5% of requests
    # all week reads as healthy, which is the boiling frog again but starting
    # from a position that was never acceptable.
    #
    # 2% against a measured healthy floor of ~0.08% leaves a wide margin, so
    # this does not fire on the ordinary failures every system has.
    error_rate_threshold: float = 0.02

    # F37. The minimum share of the bucket's critical path an edge must own
    # before a latency alert about it is allowed out.
    #
    # A p99 answers "how slow was this edge on its worst 1% of traces". It does
    # NOT answer "did this edge matter", and at the tail those come apart. An
    # edge is off the critical path at p99 only if it fails to finish last on
    # more than 99% of traces, and fan-out finish order has real variance -- so
    # injecting enough delay to breach a wall-clock threshold also pushes a
    # sibling's last-finisher rate toward 1%. Measured: user-svc went from a
    # critical p99 of exactly 0.0 to 135.4ms at a 0.6% last-finisher rate, and
    # the detector alerted on an edge owning 0.02% of the critical path.
    #
    # 5% is not tuned to the golden set. It is the point below which an edge
    # cannot be the cause of a user-visible regression by construction: if it
    # owns a twentieth of the critical path, removing it entirely would not be
    # noticed. Measured shares on this application, for scale --
    #   gateway-svc->order-svc   52%     order-svc->payment-svc  43%
    #   order-svc->product-svc  4.2%     order-svc->user-svc   0.02%
    min_blame_share: float = 0.05


@dataclass(frozen=True)
class EdgeObservation:
    source: str
    target: str
    critical_p99_ms: float
    wall_p99_ms: float
    request_count: int
    error_rate: float = 0.0
    # Fraction of the bucket's total critical-path time this edge owns.
    # Defaults to 1.0 so a caller that does not supply it is never silently
    # suppressed -- an omitted signal must not look like an innocent edge.
    blame_share: float = 1.0


@dataclass(frozen=True)
class Anomaly:
    source: str
    target: str
    metric: str
    baseline_ms: float
    observed_ms: float
    delta_ms: float
    request_count: int
    wall_p99_ms: float = 0.0
    sustained: int = 0
    sample_trace_ids: list[str] = field(default_factory=list)

    @property
    def edge(self) -> str:
        return f"{self.source}->{self.target}"


class AnomalyDetector:
    def __init__(self, redis, cfg: DetectorConfig | None = None) -> None:
        self.redis = redis
        self.cfg = cfg or DetectorConfig()

    # ---- Redis keys -------------------------------------------------------
    # Baselines live in Redis rather than Postgres because they are written
    # every cycle per edge, read on the detector's hot path, and rebuildable
    # from ClickHouse in one query if lost. Alert RULES - human-authored, must
    # never be lost - live in Postgres. Choosing storage by durability
    # requirement rather than by habit is the point.
    def _k_base(self, s: str, t: str) -> str:  return f"baseline:edge:{s}:{t}:cp99"
    def _k_warm(self, s: str, t: str) -> str:  return f"baseline:warmup:{s}:{t}"
    def _k_hist(self, s: str, t: str) -> str:  return f"anomaly:hist:{s}:{t}"
    def _k_dedup(self, s: str, t: str) -> str: return f"anomaly:dedup:{s}:{t}"

    # The error path gets its OWN history and dedup keys. Sharing them with
    # latency would mean a firing latency alert suppresses a genuine error
    # alert on the same edge for ten minutes -- two different failures, one of
    # which silently never reaches anyone.
    def _k_hist_err(self, s: str, t: str) -> str:  return f"anomaly:hist:err:{s}:{t}"
    def _k_dedup_err(self, s: str, t: str) -> str: return f"anomaly:dedup:err:{s}:{t}"

    async def evaluate(self, obs: EdgeObservation) -> Anomaly | None:
        cfg = self.cfg
        s, t = obs.source, obs.target

        # --- Gate 1: statistical floor -------------------------------------
        # Low-traffic edges produce wild p99s. Alerting on them is how a
        # detector earns a reputation for crying wolf in its first week.
        if obs.request_count < cfg.min_requests:
            return None

        baseline_raw = await self.redis.get(self._k_base(s, t))

        # --- Gate 2: cold start --------------------------------------------
        # Seed the baseline; never alert on the first sight of an edge. A newly
        # deployed service would otherwise page someone for existing.
        if baseline_raw is None:
            await self._store_baseline(obs, obs.critical_p99_ms)
            await self.redis.incr(self._k_warm(s, t))
            return None

        baseline = float(baseline_raw)

        # M7: the comparison is on CRITICAL-PATH p99, not wall clock. Detecting
        # on wall clock is precisely the behaviour this project exists to
        # improve on - it is what sends you to debug a parallel sibling.
        delta = obs.critical_p99_ms - baseline

        # --- Gate 3: warmup -------------------------------------------------
        warm = int(await self.redis.get(self._k_warm(s, t)) or 0)
        if warm < cfg.warmup_cycles:
            await self.redis.incr(self._k_warm(s, t))
            await self._update_ewma(obs, baseline, alpha=cfg.warmup_alpha)
            return None

        # --- Gate 4: breach? ------------------------------------------------
        #
        # ===================================================================
        # M6. THE BASELINE UPDATES *ONLY* ON THE HEALTHY BRANCH.
        #
        # Moving `_update_ewma` below this `if` - so it runs on every
        # observation - looks like harmless deduplication and is the single
        # most damaging edit possible to this file. Fold anomalous values into
        # the EWMA and a slow degradation becomes the new normal: the baseline
        # chases the fault, delta shrinks back under the threshold, and the
        # detector goes SILENT on a service that is still broken.
        #
        # That is the boiling-frog failure of every adaptive threshold, and it
        # is invisible - no error, no alert, just a detector that has quietly
        # stopped detecting. `test_baseline_does_not_learn_from_anomalies`
        # exists to make this edit fail loudly.
        # ===================================================================
        breached = delta > cfg.threshold_ms
        history = await self._push_history(s, t, breached, cfg.sustained_window)
        recent = history.count("1")

        if not breached:
            # ===============================================================
            # Learn ONLY from an UNAMBIGUOUSLY clean window.
            #
            # M6 says do not fold a breaching observation into the baseline.
            # That is necessary and not sufficient: with real noise, a sustained
            # fault produces occasional sub-threshold cycles, and letting each
            # of those teach the baseline walks it up onto the fault one dip at
            # a time. The boiling frog gets in through the noise instead of
            # through the branch.
            #
            # So the baseline only moves when the whole recent window is clean.
            # ===============================================================
            if recent == 0:
                await self._update_ewma(obs, baseline)
            return None

        # --- Gate 5: sustained confirmation ---------------------------------
        if recent < cfg.sustained_cycles:
            log.info("edge %s->%s breach %d/%d in last %d (+%.1fms)",
                     s, t, recent, cfg.sustained_cycles, cfg.sustained_window, delta)
            return None
        n = recent

        # --- Gate 6: blame share (F37) --------------------------------------
        #
        # Placed HERE, after the sustained window and before dedup, on purpose.
        # Everything above -- the EWMA, the M6 healthy-only learning, the
        # breach history -- is untouched, so this cannot change what the
        # baseline learns. It only decides whether a confirmed breach is worth
        # anyone's attention.
        #
        # Without it the detector blames whatever the percentile points at,
        # including an edge that owns 0.02% of the critical path and whose
        # removal nobody would notice.
        if obs.blame_share < cfg.min_blame_share:
            log.info("edge %s->%s breached (+%.1fms) but owns %.2f%% of the "
                     "critical path (floor %.0f%%) - not the cause",
                     s, t, delta, obs.blame_share * 100,
                     cfg.min_blame_share * 100)
            return None

        # --- Gate 7: dedup --------------------------------------------------
        # SET NX EX is atomic: with N detector replicas exactly one wins and
        # Slack gets one message instead of N.
        won = await self.redis.set(self._k_dedup(s, t), "1",
                                   nx=True, ex=cfg.dedup_ttl_s)
        if not won:
            return None

        return Anomaly(
            source=s,
            target=t,
            metric="critical_p99",
            baseline_ms=round(baseline, 2),
            observed_ms=round(obs.critical_p99_ms, 2),
            delta_ms=round(delta, 2),
            request_count=obs.request_count,
            wall_p99_ms=round(obs.wall_p99_ms, 2),
            sustained=n,
        )

    async def evaluate_error_rate(self, obs: EdgeObservation) -> Anomaly | None:
        """The error path, which latency percentiles structurally cannot see.

        An injected 5xx returns FASTER than a success -- the handler raises
        before the database write -- so a detector watching only critical-path
        p99 is blind to an edge failing half its requests. The eval harness
        found this by scoring cases 007 and 008 as false negatives against a
        detector that was working exactly as designed on the metric it had.

        Deliberately kept separate from `evaluate()` rather than folded in as
        another branch: the latency path is the flagship and is not worth
        destabilising to save a method. Same floors, same sustained window,
        same dedup discipline -- different question.
        """
        cfg = self.cfg
        s, t = obs.source, obs.target

        # Same statistical floor. An error rate over four requests is one bad
        # request wearing a percentage.
        if obs.request_count < cfg.min_requests:
            return None

        breached = obs.error_rate > cfg.error_rate_threshold
        key = self._k_hist_err(s, t)
        prev = await self.redis.get(key) or ""
        hist = (prev + ("1" if breached else "0"))[-cfg.sustained_window:]
        await self.redis.set(key, hist, ex=cfg.dedup_ttl_s * 2)

        if not breached:
            return None

        # Same evidence bar as latency: three breaches inside five cycles. A
        # single bad minute is a deploy or a restart, not a regression.
        recent = hist.count("1")
        if recent < cfg.sustained_cycles:
            log.info("edge %s->%s error breach %d/%d in last %d (%.1f%%)",
                     s, t, recent, cfg.sustained_cycles, cfg.sustained_window,
                     obs.error_rate * 100)
            return None

        won = await self.redis.set(self._k_dedup_err(s, t), "1",
                                   nx=True, ex=cfg.dedup_ttl_s)
        if not won:
            return None

        # baseline/observed/delta carry RATES here, not milliseconds. `metric`
        # is what tells a reader which, and the alert copy renders it as a
        # percentage so nobody reads 0.1 as a tenth of a millisecond.
        return Anomaly(
            source=s,
            target=t,
            metric="error_rate",
            baseline_ms=cfg.error_rate_threshold,
            observed_ms=round(obs.error_rate, 4),
            delta_ms=round(obs.error_rate - cfg.error_rate_threshold, 4),
            request_count=obs.request_count,
            wall_p99_ms=round(obs.wall_p99_ms, 2),
            sustained=recent,
        )

    async def _push_history(self, s: str, t: str, breached: bool,
                            window: int) -> str:
        """Append this cycle's verdict and keep the last `window` of them."""
        key = self._k_hist(s, t)
        prev = await self.redis.get(key) or ""
        hist = (prev + ("1" if breached else "0"))[-window:]
        await self.redis.set(key, hist, ex=self.cfg.dedup_ttl_s * 2)
        return hist

    async def _update_ewma(self, obs: EdgeObservation, baseline: float,
                           alpha: float | None = None) -> None:
        a = self.cfg.alpha if alpha is None else alpha
        new = a * obs.critical_p99_ms + (1 - a) * baseline
        await self._store_baseline(obs, new)

    async def _store_baseline(self, obs: EdgeObservation, value: float) -> None:
        await self.redis.set(self._k_base(obs.source, obs.target), value,
                             ex=self.cfg.baseline_ttl_s)
