"""Fault injectors. One family per module, one shared interface."""

from __future__ import annotations

from collections.abc import Callable

from .base import Injector, InjectorError, assert_all_clear, clear_all
from .composite import CompositeInjector
from .dep_kill import DepKillInjector
from .error import ErrorInjector
from .latency import LatencyInjector
from .null import NullInjector
from .saturation import SaturationInjector

# Every value is a class whose instances satisfy the Injector protocol. The
# annotation is explicit because the five classes share no base -- structural
# typing is the point, and without it mypy infers `type[object]` and the
# factory's return type degrades to `object`.
_REGISTRY: dict[str, Callable[[dict], Injector]] = {
    "latency": LatencyInjector,
    "error": ErrorInjector,
    "saturation": SaturationInjector,
    "dep_kill": DepKillInjector,
    "null": NullInjector,
    "composite": CompositeInjector,
}


def get_injector(spec: dict) -> Injector:
    kind = spec.get("type")
    if not isinstance(kind, str):
        # YAML 1.1 parses a bare `null` as None, which is exactly how both
        # null cases once ended up with no injector type at all -- while still
        # loading, still running, and still printing a passing row.
        raise InjectorError(
            f"injector type must be a string, got {kind!r}. "
            "If you meant the null injector, quote it: type: \"null\"")
    try:
        factory = _REGISTRY[kind]
    except KeyError:
        raise InjectorError(
            f"unknown injector type {kind!r}; known: {', '.join(sorted(_REGISTRY))}"
        ) from None
    return factory(spec)


__all__ = [
    "Injector",
    "InjectorError",
    "assert_all_clear",
    "clear_all",
    "get_injector",
]
