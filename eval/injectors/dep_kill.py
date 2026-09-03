"""The target's own dependency becomes unreachable.

Deliberately NOT `docker compose stop postgres`. Postgres is shared by
order-svc, user-svc and payment-svc, so stopping it injects four faults at once
and attribution becomes meaningless -- the exact ambiguity that makes the
runner refuse to run cases in parallel.

Instead the fault is severed at the *client* boundary inside payment-svc:
`dep_down` makes its database calls time out and fail, which is precisely what
a NetworkPolicy blocking payment-svc -> postgres would produce, without
touching any other service. Under Helm the same case would use a real
NetworkPolicy; see charts/tracemap/templates/networkpolicy.yaml.

The observable result on `order-svc->payment-svc` is a dead dependency's real
signature: latency up to the connect timeout, and 5xx on every request.
"""

from __future__ import annotations

from .base import FaultInjector


class DepKillInjector(FaultInjector):
    family = "dep_kill"

    def payload(self) -> dict:
        return {
            "dep_down": True,
            # How long the doomed call hangs before giving up. Modelled on a
            # connect timeout rather than an instant refusal, because an
            # instant refusal is the easy case -- a hung dependency is the one
            # that eats a connection pool.
            "dep_timeout_ms": int(self.spec.get("dep_timeout_ms", 250)),
        }
