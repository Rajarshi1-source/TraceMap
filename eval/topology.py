"""The service graph the golden set is written against.

This file exists because of a bug it would have caught. The reference harness
specifies case 009's edge as `gateway-svc->user-svc`. No such edge exists: the
fan-out lives in order-svc, so user-svc is a child of order-svc and gateway-svc
has never called it. The case would have run, injected a real fault, watched
for an alert on an edge that cannot produce one, and scored a working detector
as a false negative -- and the obvious response to a red eval is to weaken the
detector.

An edge name is a string, and strings do not fail loudly. So they are declared
once here, `test_cases.py` asserts every case uses a declared edge, and
`make topology-guard` asserts the declaration still matches the live graph. Three cheap checks against a class of bug that is otherwise invisible
until it has already cost you a threshold.
"""

from __future__ import annotations

# gateway-svc
#   |-- order-svc                  (POST /checkout -> POST /orders)
#   |     |-- user-svc             \
#   |     |-- product-svc           |  THE concurrent fan-out (M1)
#   |     +-- payment-svc          /
#   +-- product-svc                (GET /products, the browse path)
#
# Baseline latencies, which decide who is on the critical path:
#   user-svc 30ms   <   product-svc 45ms   <   payment-svc 90ms
#
# Payment is the slowest fan-out leg, so it carries the critical path by
# default. Its siblings do NOT both carry zero blame, and assuming they did
# produced a broken decoy case -- see CRIT_P99_BASELINE_MS below, which is the
# measured version of this comment and the one to trust.
#
# Every latency case in the golden set is a statement about that ordering:
# 006 keeps it, 005 and 009 invert it.
EDGES: frozenset[str] = frozenset({
    "gateway-svc->order-svc",
    "gateway-svc->product-svc",
    "order-svc->user-svc",
    "order-svc->product-svc",
    "order-svc->payment-svc",
})

# Services with a fault control plane at /_internal/fault. gateway-svc has none
# and needs none: an edge's latency belongs to its callee, so a fault aimed at
# the `gateway-svc->order-svc` edge is injected into order-svc.
FAULT_TARGETS: frozenset[str] = frozenset({
    "order-svc", "user-svc", "payment-svc", "product-svc",
})

# Configured base latency per service. What the code sleeps for.
BASELINE_MS: dict[str, int] = {
    "user-svc": 30,
    "product-svc": 45,
    "payment-svc": 90,
}

# What each fan-out leg actually does on the CRITICAL PATH, measured over
# ~11,000 traces of healthy load at 20 rps.
#
#   edge                    critical on   crit p99   wall p99
#   order-svc->payment-svc      87.3%       157ms      154ms
#   order-svc->product-svc      12.4%        49ms       56ms
#   order-svc->user-svc          0.3%         0ms       56ms
#
# This table exists because the mean latencies above are the wrong statistic
# for designing a decoy, and using them produced a case that failed against a
# correct detector.
#
# product-svc looks like a parallel sibling and is not one: its N+1 query path
# fires on carts above PRODUCT_N_PLUS_ONE_THRESHOLD, and on those traces it
# genuinely is the slowest leg. Injecting delay into it raises its critical p99
# by the injected amount, so a critical-path detector correctly alerts -- and
# the case scores that correct alert as a false positive.
#
# user-svc is the real parallel sibling. Critical p99 of exactly 0.0 is what
# one looks like.
CRIT_P99_BASELINE_MS: dict[str, float] = {
    "order-svc->payment-svc": 157.0,
    "order-svc->product-svc": 49.3,
    "order-svc->user-svc": 0.0,
}

# The detector's breach threshold. Duplicated from DetectorConfig deliberately:
# the eval set has to be able to reason about it without importing the thing it
# is testing.
THRESHOLD_MS = 50.0


def parse(edge: str) -> tuple[str, str]:
    src, _, dst = edge.partition("->")
    return src, dst


# ---------------------------------------------------------------------------
# The detection budget every case's `detect_within_s` has to clear.
#
# Quote these numbers; do not re-derive them per file, and do not shorten them
# to make a scoreboard look faster. Phase 6 measured 375s injection-to-alert
# against a documented ~245s, and the gap was entirely in the two terms the
# original budget omitted: assembler lag and minute-bucket completion.
# ---------------------------------------------------------------------------
BUDGET_TERMS_S: dict[str, int] = {
    "collector batch": 5,
    "tail sampling decision_wait": 10,
    "collector export batch": 5,
    "assembler late-span grace": 30,
    "assembler interval": 15,
    "minute-bucket completion": 60,   # not tunable: the roll-up IS per minute
    "detector confirmation": 300,     # 5 cycles x 60s -- the 3-in-5 window
}

# 425s worst case. Anything tighter measures the pipeline, not the detector.
#
# Note what is NOT here: an "eval profile" that shortens these. Every term
# except the last two is a knob, and turning them down would buy roughly 40
# seconds and cost the only claim that matters -- that the number was measured
# against the configuration that actually ships. A detector scored on a faster
# pipeline than the one in production is a detector scored on a system nobody
# runs.
PIPELINE_FLOOR_S = sum(BUDGET_TERMS_S.values())


def budget_table() -> str:
    width = max(len(k) for k in BUDGET_TERMS_S)
    lines = [f"  {k:<{width}}  {v:>4}s" for k, v in BUDGET_TERMS_S.items()]
    lines.append("  " + "-" * (width + 6))
    lines.append(f"  {'worst case':<{width}}  {PIPELINE_FLOOR_S:>4}s")
    return "\n".join(lines)
