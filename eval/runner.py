"""inject -> observe -> score -> report.

Runs the golden set against a live stack and writes one `eval_runs` row.

Three properties are non-negotiable and each exists because of a specific way
this harness lies to you if it is missing:

  SEQUENTIAL. One fault at a time. Two concurrent faults make attribution
  ambiguous -- there is no correct answer to score against -- and the run
  becomes unusable rather than merely slow. Case 006 applies two faults, but it
  is still one case with one expected answer, which is the whole question it
  asks.

  SETTLED. A gap between cases, with the detector's alert-suppression state
  cleared inside it, so case N's fault is not case N+1's baseline. Without the
  gap the second case in every family fails and the natural conclusion is that
  the detector regressed.

  CLEAN. `stop()` in a `finally`, and an all-clear assertion before the first
  case and after the last. A leaked injector (M29) does not announce itself; it
  quietly makes every later measurement wrong in a direction that looks like a
  detector bug.

If everything passes on the first run, suspect this file before celebrating.
An eval suite that has never failed is evidence of an untested harness. Run
`make eval-gate-guard` -- it feeds the scorer a broken detector's results and
requires the gate to go red.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import yaml

from eval.injectors import assert_all_clear, clear_all, get_injector
from eval.scorer import FALSE_NEGATIVE, FALSE_POSITIVE, TRUE_POSITIVE, WRONG_EDGE, Scores, score_run

log = logging.getLogger("eval.runner")

CASES_DIR = Path(__file__).parent / "cases"

API_BASE = os.environ.get("ANALYTICS_API", "http://localhost:8090")
DEPLOY_CONTROL = os.environ.get("DEPLOY_CONTROL", "http://localhost:9103")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
# The ramp generator joins the compose network rather than the host's.
#
# `--network host` is not portable: on Docker Desktop it does not give the
# container the host's loopback, so a ramp launched that way reaches nothing,
# silently. A null case whose ramp never arrived still passes -- it just stops
# being the case that tests load sensitivity, which is the only reason it
# exists.
LOAD_NETWORK = os.environ.get("EVAL_LOAD_NETWORK", "tracemap_default")
LOAD_BASE_URL = os.environ.get("EVAL_LOAD_BASE_URL", "http://gateway-svc:3000")

POLL_INTERVAL_S = int(os.environ.get("EVAL_POLL_INTERVAL_S", "10"))
SETTLE_S = int(os.environ.get("EVAL_SETTLE_S", "60"))

# After the expected edge is found, keep watching for this long before scoring.
#
# The reference runner stopped as soon as it had *both* a detection and a trap,
# which means a clean case ran its whole budget while a case whose trap fired
# late never saw it. A trap that gets a shorter window than the expected edge
# is not a fair test -- it flatters precision. One full detector cycle plus
# margin makes the windows comparable without paying the full budget every time.
TRAP_GRACE_S = int(os.environ.get("EVAL_TRAP_GRACE_S", "90"))

# Consecutive failed polls before a case gives up. At a 10s poll interval that
# is a minute of an unreachable API, which is long past transient.
MAX_POLL_FAILURES = int(os.environ.get("EVAL_MAX_POLL_FAILURES", "6"))

# The API aggregates across ClickHouse on every call, and a loaded host makes
# that slow well before it makes it broken. 15s was too tight: a single read
# timeout aborted a 45-minute run.
API_TIMEOUT_S = float(os.environ.get("EVAL_API_TIMEOUT_S", "30"))

# The four smoke cases. Chosen to cover each failure mode the gate exists to
# catch, not simply the fastest four (D5):
#   001  false positives -- the measurement everyone forgets
#   003  latency sensitivity at the threshold, the canonical brief case
#   006  the flagship; a wall-clock detector fails it by design
#   007  the error path, which latency percentiles cannot see
SMOKE_TIER = ("001_null_baseline", "003_payment_latency_60ms",
              "006_parallel_decoy", "007_payment_errors_10pct")


# --------------------------------------------------------------------------
# stack access
# --------------------------------------------------------------------------
class Api:
    """Read-only view of the platform, as the eval harness sees it."""

    def __init__(self, base: str = API_BASE) -> None:
        self.base = base.rstrip("/")

    async def anomalies_since(self, since: datetime) -> list[dict]:
        async with httpx.AsyncClient(timeout=API_TIMEOUT_S) as c:
            r = await c.get(f"{self.base}/api/anomalies", params={"limit": 200})
            r.raise_for_status()
            rows = r.json().get("anomalies", [])
        out = []
        for a in rows:
            try:
                ts = datetime.fromisoformat(a["detectedAt"])
            except (KeyError, ValueError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if ts >= since:
                out.append(a)
        return out

    async def edge_traffic(self) -> dict[str, int]:
        """Request counts per edge over the recent window.

        Used as a pre-flight assertion. Failure mode #1 in the harness table is
        "every case is a false negative", and by far its most common cause is
        that nothing was driving load -- an idle stack scores a working
        detector as broken.
        """
        async with httpx.AsyncClient(timeout=API_TIMEOUT_S) as c:
            r = await c.get(f"{self.base}/api/graph", params={"window": 5})
            r.raise_for_status()
            data = r.json()
        counts: dict[str, int] = {}
        for e in data.get("edges", []):
            src = e.get("source") or e.get("src")
            dst = e.get("target") or e.get("dst")
            if src and dst:
                counts[f"{src}->{dst}"] = int(e.get("requestCount")
                                              or e.get("requests") or 0)
        return counts

    async def healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                return (await c.get(f"{self.base}/healthz")).status_code == 200
        except Exception:  # noqa: BLE001 - "can I reach it" has exactly two
            # answers; preflight turns a False here into a named problem.
            return False


async def record_deploy(spec: dict, git_sha: str) -> None:
    body = {
        "service": spec["service"],
        "revision": spec.get("revision", "eval"),
        "git_sha": spec.get("git_sha", git_sha),
        "trigger": spec.get("trigger", "rollout"),
    }
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"{DEPLOY_CONTROL}/_internal/deploy", json=body)
        r.raise_for_status()
    log.info("deploy  %s rev=%s", body["service"], body["revision"])


async def reset_alert_suppression() -> int:
    """Clear dedup and breach-history keys between cases.

    NOT a detector tuning knob, and the distinction matters. Dedup exists so
    that N replicas produce one Slack message per incident, and so a firing
    edge does not re-page every minute for ten minutes. Two eval cases are two
    separate incidents that happen to share an edge -- 003 and 004 both fault
    `order-svc->payment-svc` roughly seven minutes apart, well inside the 600s
    dedup TTL. Leaving the key in place makes 004 a guaranteed false negative,
    and the scoreboard would be measuring Redis.

    Baselines are deliberately NOT cleared. They are warm, they are legitimate,
    and M6 guarantees a fault case cannot have corrupted them. Clearing them
    would drop every edge back into warmup and cost five cycles per case.
    """
    try:
        import redis.asyncio as aioredis
    except ImportError:
        log.warning("redis client unavailable; alert suppression NOT reset "
                    "-- repeat-edge cases will false-negative")
        return 0
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        removed = 0
        for pattern in ("anomaly:dedup:*", "anomaly:hist:*"):
            keys = [k async for k in r.scan_iter(match=pattern, count=500)]
            if keys:
                removed += await r.delete(*keys)
        return removed
    except Exception:
        log.exception("could not reset alert suppression")
        return 0
    finally:
        await r.aclose()


class LoadRamp:
    """A second k6 process, for the traffic-ramp null case.

    Offered load doubles without any service changing behaviour. That is the
    point of case 002: p99 legitimately moves under load, and a detector that
    reads queueing as regression fires here.
    """

    def __init__(self, script: str, rps: int, duration_s: int) -> None:
        self.script, self.rps, self.duration_s = script, rps, duration_s
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        script_path = Path(self.script)
        cmd = [
            "docker", "run", "--rm", "--network", LOAD_NETWORK,
            "-v", f"{script_path.parent.resolve()}:/scripts",
            "-e", "SHORT=1", "-e", f"RPS={self.rps}",
            "-e", f"DURATION={self.duration_s}s",
            "-e", f"BASE_URL={LOAD_BASE_URL}",
            "grafana/k6", "run", f"/scripts/{script_path.name}",
        ]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
            log.info("ramp    +%d rps for %ds on %s", self.rps,
                     self.duration_s, LOAD_NETWORK)
        except FileNotFoundError:
            # Say so loudly. Silently degrading case 002 to a second plain
            # null case would leave the scoreboard claiming a load-sensitivity
            # measurement that never happened.
            log.error("docker not on PATH; traffic ramp SKIPPED -- case 002 "
                      "is NOT measuring load sensitivity in this run")

    def stop(self) -> None:
        """Idempotent, like every other teardown here."""
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            with contextlib.suppress(Exception):
                self.proc.wait(timeout=10)
        self.proc = None


# --------------------------------------------------------------------------
# one case
# --------------------------------------------------------------------------
def _edge(a: dict) -> str:
    return f"{a.get('source')}->{a.get('target')}"


def _faulted_services(spec: dict) -> set[str]:
    """Which services this case actually injects into.

    Needed only so the failure message can tell the truth. In a decoy case the
    trap edge IS faulted, deliberately -- calling it "untouched" describes the
    wrong experiment and sends the reader looking for a bug in the injector.
    """
    parts = spec.get("parts", [spec])
    return {p["target"] for p in parts if p.get("target")}


def _deploy_matches(anomaly: dict, want: dict) -> bool:
    got = anomaly.get("deploy") or {}
    if not got.get("found"):
        return False
    return all(str(got.get(key, "")) == str(expected)
               for key, expected in want.items())


async def run_case(case: dict, api: Api, git_sha: str) -> dict:
    name = case["name"]
    expect = case["expect"]
    expected_edge = expect["edge"]
    budget_s = int(expect["detect_within_s"])
    traps = set(expect.get("must_not_alert", []))
    want_deploy = expect.get("correlated_deploy")
    is_null = expected_edge == "none"
    # "*" means every edge is a trap, which is what a null case is.
    trap_all = "*" in traps

    injector = get_injector(case["injector"])
    load = case.get("load", {})
    ramp: LoadRamp | None = None
    if int(load.get("ramp_multiplier", 1)) > 1:
        ramp = LoadRamp(load["script"],
                        int(load["rps"]) * (int(load["ramp_multiplier"]) - 1),
                        int(load.get("ramp_hold_s", 300)))
    ramp_at = float(load.get("ramp_after_s", 0))

    # Anomalies are timestamped by Postgres in wall-clock UTC; elapsed time is
    # measured on the monotonic clock. Mixing the two is how MTTD ends up
    # negative when NTP steps the machine mid-run.
    since = datetime.now(UTC) - timedelta(seconds=5)
    t0 = time.monotonic()

    if case.get("deploy"):
        await record_deploy(case["deploy"], git_sha)
        await asyncio.sleep(float(case["deploy"].get("before_fault_s", 30)))
        # The clock starts at the FAULT, not at the deploy. Counting the
        # deliberate 30s gap as detection latency would inflate MTTD by a
        # number the detector had no way to influence.
        t0 = time.monotonic()

    detected_at: float | None = None
    detected_edge: str | None = None
    tripped_trap: str | None = None
    stray_edge: str | None = None
    deploy_ok = want_deploy is None
    seen: set[str] = set()

    poll_failures = 0

    log.info("case    %-28s expect=%s budget=%ds", name, expected_edge, budget_s)
    await injector.start()
    try:
        stop_after: float | None = None
        while True:
            elapsed = time.monotonic() - t0
            if elapsed >= budget_s:
                break
            if stop_after is not None and elapsed >= stop_after:
                break
            if ramp and ramp_at and elapsed >= ramp_at and ramp.proc is None:
                ramp.start()

            await asyncio.sleep(POLL_INTERVAL_S)

            # A single slow API response must not destroy a 45-minute
            # measurement, and a dead API must not be scored as "the detector
            # found nothing".
            #
            # Those two requirements pull in opposite directions, so the loop
            # tolerates transient failures and counts them: one timeout is the
            # host being busy, MAX_POLL_FAILURES in a row is the API being
            # gone. Swallowing every failure would be worse than crashing --
            # it would turn an unreachable API into a clean sweep of false
            # negatives, which is a wrong number rather than a missing one.
            try:
                fresh = await api.anomalies_since(since)
                poll_failures = 0
            except Exception as exc:
                # Every transport failure counts the same. Distinguishing a
                # timeout from a refused connection would be tidier and would
                # change nothing: what matters is how many in a row.
                poll_failures += 1
                log.warning("poll failed (%d/%d): %s", poll_failures,
                            MAX_POLL_FAILURES, exc.__class__.__name__)
                if poll_failures >= MAX_POLL_FAILURES:
                    raise RuntimeError(
                        f"analytics API unreachable for {poll_failures} "
                        f"consecutive polls; abandoning the run rather than "
                        f"scoring every remaining case as a false negative"
                    ) from exc
                continue

            for a in fresh:
                edge = _edge(a)
                if edge in seen:
                    continue
                seen.add(edge)
                log.info("        alert %s  +%.0fms  (t+%.0fs)", edge,
                         a.get("deltaMs", 0), time.monotonic() - t0)

                if edge == expected_edge:
                    if detected_edge is None:
                        detected_edge = edge
                        detected_at = round(time.monotonic() - t0, 1)
                        if want_deploy:
                            deploy_ok = _deploy_matches(a, want_deploy)
                        # Give the traps a comparable window before scoring.
                        stop_after = (time.monotonic() - t0) + TRAP_GRACE_S
                elif trap_all or edge in traps:
                    tripped_trap = tripped_trap or edge
                else:
                    stray_edge = stray_edge or edge
    finally:
        # ALWAYS. Even on KeyboardInterrupt, even if the API died. A fault that
        # outlives its case is the most expensive bug in this directory.
        await injector.stop()
        if ramp:
            ramp.stop()

    detail = ""
    if tripped_trap:
        outcome = FALSE_POSITIVE
        faulted = _faulted_services(case["injector"])
        trap_callee = tripped_trap.partition("->")[2]
        if trap_callee in faulted:
            # The decoy case. The edge was slowed on purpose; the assertion is
            # that it should not be BLAMED, because it was not on the critical
            # path. Blurring that into "untouched" loses the whole point.
            detail = (f"blamed {tripped_trap}, which was slowed deliberately "
                      f"but should not have been on the critical path")
        else:
            detail = f"blamed {tripped_trap}, which was untouched"
    elif is_null:
        outcome = FALSE_POSITIVE if stray_edge else TRUE_POSITIVE
        if stray_edge:
            detail = f"alerted on {stray_edge} with no fault present"
    elif detected_edge and not deploy_ok:
        # Scored as a miss, deliberately. Case 011 is not asking "did you find
        # the edge" -- ten other cases ask that. It asks whether the alert
        # named the release, and an alert that sends someone to a dashboard
        # instead of to a diff has not answered it.
        outcome = FALSE_NEGATIVE
        detail = "edge detected but the release was not correlated"
    elif detected_edge:
        outcome = TRUE_POSITIVE
    elif stray_edge:
        outcome = WRONG_EDGE
        detail = f"alerted on {stray_edge}, expected {expected_edge}"
    else:
        outcome = FALSE_NEGATIVE
        detail = f"no alert on {expected_edge} within {budget_s}s"

    result = {
        "case_name": name,
        "expected_edge": expected_edge,
        "detected_edge": detected_edge or tripped_trap or stray_edge,
        "detected_at_s": detected_at,
        "outcome": outcome,
        "detail": detail,
    }
    log.info("        -> %-15s %s", outcome, detail or "")
    return result


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------
def load_cases(tier: str, only: str | None) -> list[tuple[str, dict]]:
    paths = sorted(CASES_DIR.glob("*.yaml"))
    if not paths:
        raise SystemExit(f"no cases found in {CASES_DIR}")
    if only:
        paths = [p for p in paths if only in p.stem]
        if not paths:
            raise SystemExit(f"no case matching {only!r}")
    elif tier == "smoke":
        paths = [p for p in paths if p.stem in SMOKE_TIER]
        # M9 in its most tempting form: a smoke tier without its null case
        # cannot measure false positives, and it would be ten minutes faster.
        if not any("null" in p.stem for p in paths):
            raise SystemExit("the smoke tier must retain a null case (M9)")
    return [(p.stem, yaml.safe_load(p.read_text(encoding="utf-8"))) for p in paths]


async def preflight(api: Api, cases: list[tuple[str, dict]]) -> None:
    """Refuse to produce a number the stack cannot support.

    A run that starts on a cold or idle stack still prints a scoreboard, and
    that scoreboard is worse than none: it looks like a detector regression and
    sends you to tune thresholds against an artefact of the harness.
    """
    problems: list[str] = []

    if not await api.healthy():
        problems.append(f"analytics API not answering at {api.base}")

    dirty = await assert_all_clear()
    if dirty:
        problems += [f"fault control plane dirty -- {d}" for d in dirty]

    traffic: dict[str, int] = {}
    try:
        traffic = await api.edge_traffic()
    except Exception as exc:  # noqa: BLE001 - preflight collects every
        # problem before reporting, so one unreachable dependency does not
        # hide the other three.
        problems.append(f"cannot read edge traffic: {exc}")

    # Reachable Redis is a HARD requirement, not a nice-to-have.
    #
    # Without it the between-case suppression reset degrades to a no-op, and
    # cases 003 and 006 -- which both fault `order-svc->payment-svc` inside one
    # 600s dedup TTL -- guarantee a false negative on the second one. The run
    # still finishes and still prints a scoreboard; the scoreboard is just
    # measuring Redis, and it reads as a detector regression.
    #
    # A wrong number is worse than a missing one, so this refuses rather than
    # warns. Redis is unpublished in the `core` profile by design; run with
    # `-f docker-compose.eval.yml`, which opens it.
    if len(cases) > 1:
        try:
            import redis.asyncio as aioredis

            r = aioredis.from_url(REDIS_URL, decode_responses=True)
            try:
                await r.ping()
            finally:
                await r.aclose()
        except ImportError:
            problems.append("redis client not installed -- "
                            "pip install -r eval/requirements.txt")
        except Exception as exc:  # noqa: BLE001 - any failure to reach Redis
            # is disqualifying, whatever its type; the message names the fix.
            problems.append(
                f"redis unreachable at {REDIS_URL} ({exc.__class__.__name__}). "
                "Alert suppression cannot be reset between cases, so every "
                "repeat-edge case would false-negative. Boot with "
                "-f docker-compose.eval.yml.")

    needed = {c["expect"]["edge"] for _, c in cases
              if c["expect"]["edge"] != "none"}
    for edge in sorted(needed):
        if traffic.get(edge, 0) <= 0:
            problems.append(f"no traffic on {edge} -- is load running?")

    if problems:
        detail = "".join(f"    - {p}\n" for p in problems)
        raise SystemExit("\n  preflight FAILED\n" + detail
                         + "\n  Fix the stack, not the thresholds.\n")
    log.info("preflight OK -- %d edges carrying traffic", len(traffic))


def detector_config() -> dict:
    """The model artifact, recorded per run.

    Storing this next to the score is what makes the history useful: a score
    change is attributable either to a config change or to drift, and without
    this column there is no way to tell which.
    """
    keys = ("DETECTOR_ALPHA", "DETECTOR_THRESHOLD_MS", "DETECTOR_MIN_REQUESTS",
            "DETECTOR_SUSTAINED_CYCLES", "DETECTOR_WARMUP_CYCLES",
            "DETECTOR_ERROR_RATE_THRESHOLD",
            "DETECT_INTERVAL_S", "ASSEMBLE_INTERVAL_S", "LATE_SPAN_GRACE_S")
    return {k.lower(): os.environ[k] for k in keys if os.environ.get(k)}


async def persist(run_id: str, git_sha: str, tier: str,
                  scores: Scores, results: list[dict]) -> bool:
    from eval.store import EvalStore

    store = await EvalStore.connect()
    if store is None:
        log.warning("postgres unavailable; scoreboard NOT recorded")
        return False
    try:
        await store.insert_run(run_id, git_sha, tier, detector_config(),
                               scores, results)
        log.info("recorded eval_runs row %s", run_id[:8])
        return True
    except Exception:
        log.exception("could not record the run")
        return False
    finally:
        await store.close()


async def main_async(args: argparse.Namespace) -> int:
    cases = load_cases(args.tier, args.case)
    api = Api(args.api)

    if not args.skip_preflight:
        await preflight(api, cases)

    run_id = str(uuid.uuid4())
    log.info("run %s  tier=%s  cases=%d  sha=%s", run_id[:8], args.tier,
             len(cases), args.git_sha[:9] or "local")

    results: list[dict] = []
    aborted: str | None = None
    try:
        for i, (_stem, case) in enumerate(cases):
            results.append(await run_case(case, api, args.git_sha))
            if i < len(cases) - 1:
                cleared = await reset_alert_suppression()
                log.info("settle  %ds (cleared %d suppression keys)",
                         SETTLE_S, cleared)
                await asyncio.sleep(SETTLE_S)
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, the cases
        # that already completed are real measurements and are worth keeping.
        # Losing forty minutes of injected faults to one exception is how a
        # harness earns a reputation for being flaky and stops being run.
        aborted = f"{exc.__class__.__name__}: {exc}"
        log.error("run ABANDONED after %d/%d cases -- %s",
                  len(results), len(cases), aborted)
    finally:
        # Belt and braces. Every injector already stops itself in its own
        # `finally`; this catches the runner being killed between cases, which
        # no per-case handler can see.
        await clear_all()

    leaked = await assert_all_clear()
    if leaked:
        log.error("FAULTS LEAKED after the run: %s", leaked)

    scores = score_run(results)
    print(scores.as_table())
    if aborted:
        # An incomplete run must never be quoted as a score, and must never be
        # recorded as a baseline. Say so on the scoreboard itself, where the
        # number is, rather than only in a log line above it.
        print(f"    INCOMPLETE: {len(results)}/{len(cases)} cases ran "
              f"before {aborted}")
        print("    These scores are diagnostic only. Do not quote them and do "
              "not record them as a baseline.")
    for r in results:
        if r["outcome"] != TRUE_POSITIVE:
            print(f"    {r['case_name']:<28} {r['outcome']:<15} {r['detail']}")
    print()

    recorded = (False if aborted
                else await persist(run_id, args.git_sha, args.tier,
                                   scores, results))
    if aborted:
        log.warning("incomplete run NOT recorded -- eval_runs is a table of "
                    "measurements, not of attempts")

    payload = {"run_id": run_id, "git_sha": args.git_sha, "tier": args.tier,
               "recorded": recorded, "detector_cfg": detector_config(),
               "aborted": aborted, "cases_expected": len(cases),
               "scores": scores.as_dict(), "cases": results}
    # Two small synchronous writes at the very end of a ~90-minute run, after
    # every case has finished. Reaching for aiofiles here would add a
    # dependency to avoid blocking an event loop that has nothing left to do.
    if args.json:
        Path(args.json).write_text(  # noqa: ASYNC240
            json.dumps(payload, indent=2), encoding="utf-8")
        log.info("wrote %s", args.json)
    if args.output:
        Path(args.output).write_text(  # noqa: ASYNC240
            scores.as_markdown(args.git_sha, args.tier), encoding="utf-8")
        log.info("wrote %s", args.output)

    # An abandoned run is a failure whatever the partial scores say.
    return 0 if (scores.passed and not aborted) else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Run the TraceMap golden set.")
    p.add_argument("--tier", choices=("full", "smoke"), default="full")
    p.add_argument("--case", help="run a single case matching this substring")
    p.add_argument("--git-sha", default=os.environ.get("GIT_SHA", ""))
    p.add_argument("--api", default=API_BASE)
    p.add_argument("--output", help="write a markdown report here")
    p.add_argument("--json", help="write the full run payload here")
    p.add_argument("--skip-preflight", action="store_true",
                   help="not for CI; preflight is what stops a bad stack "
                        "producing a plausible-looking number")
    args = p.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="  %(asctime)s  %(message)s", datefmt="%H:%M:%S")

    # Windows defaults to the Proactor event loop and psycopg's async mode
    # cannot run on it. Left alone the pool fails to open, EvalStore.connect()
    # returns None, and the run finishes having recorded nothing -- while
    # reporting "postgres unreachable", which sends you to check the database.
    #
    # `loop_factory` rather than `set_event_loop_policy`: the policy API is
    # deprecated in 3.14 and slated for removal in 3.16, and this repo is 3.14.
    # CI is Linux and takes the None branch.
    loop_factory = (asyncio.SelectorEventLoop if sys.platform == "win32"
                    else None)

    try:
        return asyncio.run(main_async(args), loop_factory=loop_factory)
    except KeyboardInterrupt:
        # Interrupting a run must still heal the stack.
        asyncio.run(clear_all(), loop_factory=loop_factory)
        print("\n  interrupted -- faults cleared\n", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
