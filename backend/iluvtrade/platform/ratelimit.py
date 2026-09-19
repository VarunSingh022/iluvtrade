"""Application-level rate limiting.

Why in the application and not only at a proxy
----------------------------------------------

A reverse proxy limits by IP, which is the right place for volumetric abuse. It
cannot limit by *principal* — and the limits that matter here are per-account:
one user submitting a thousand backtests, or exchanging broker tokens in a loop,
is not distinguishable from legitimate traffic at the IP layer when several
users share an office NAT. So this limits by a key that names the actor, and a
proxy still belongs in front of it. Neither replaces the other.

What this is
------------

A **fixed-window counter** kept in memory, with a pluggable backend. Fixed
window rather than a token bucket because the failure mode people actually care
about — "how many logins can I attempt per minute" — is expressible exactly, and
the boundary burst a fixed window allows (up to 2x the limit across a window
edge) is acceptable for every policy below. A sliding window would need to store
every hit; a token bucket makes "10 per minute" an approximation.

What this is **not**
--------------------

**In-process, and therefore not distributed.** Two API workers each keep their
own counters, so the effective limit across a deployment is
``limit x workers``. That is stated rather than hidden, and it is the honest
description: this is a single-process limiter that a multi-instance deployment
must replace.

:class:`RateLimitBackend` is the seam. :class:`SharedBackend` below documents
exactly what a Redis (or Postgres) implementation has to provide, and
:func:`describe_enforcement` reports which kind is actually running so a
deployment can tell — rather than assuming the limits it configured are the
limits it gets.

No shared backend is implemented here. One cannot be written honestly without a
running Redis to test the atomicity it depends on, and an untested
``INCR``/``EXPIRE`` pair that races is worse than a limiter known to be local.

Determinism
-----------

The clock is injected. A test advances it explicitly rather than sleeping, so a
rate-limit test is fast and cannot flake on a slow machine.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "InMemoryBackend",
    "Policy",
    "RateLimitBackend",
    "RateLimitError",
    "RateLimiter",
    "get_limiter",
    "reset_limiter",
]


class RateLimitError(Exception):
    """The caller exceeded a policy. Carries when they may retry."""

    def __init__(self, policy: str, retry_after_seconds: int) -> None:
        super().__init__(
            f"Too many requests for {policy}. Retry in {retry_after_seconds} second(s)."
        )
        self.policy = policy
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class Policy:
    """How many of something, over how long.

    ``name`` appears in the error and in the audit record, so an operator
    reading a log knows which limit fired rather than only that one did.
    """

    name: str
    limit: int
    window_seconds: int

    def __post_init__(self) -> None:
        if self.limit < 1 or self.window_seconds < 1:
            raise ValueError("A policy needs a positive limit and window.")


class RateLimitBackend(Protocol):
    """Where counters live. The seam a shared store implements."""

    @property
    def is_shared(self) -> bool:
        """Whether counters are shared across processes.

        ``False`` for anything in memory. Read by
        :func:`describe_enforcement`, so a deployment can report what it
        actually enforces instead of what it configured.
        """
        ...

    def hit(self, key: str, window_seconds: int, now: float) -> int:
        """Record one hit and return the count within the current window.

        **Must be atomic.** A shared implementation that reads, increments and
        writes in separate round trips will undercount under exactly the
        concurrency a rate limit exists to handle. In Redis this is a single
        ``INCR`` followed by an ``EXPIRE`` on first write, or one Lua script.
        """
        ...

    def reset(self) -> None:
        """Forget everything. Tests and administrative clearing only."""
        ...


class SharedBackend(RateLimitBackend, Protocol):
    """What a multi-instance deployment needs, written down but not built.

    An implementation must provide, beyond :class:`RateLimitBackend`:

    * **Atomic increment-and-expire.** One round trip, or a script. Two round
      trips race.
    * **Server-side expiry.** Keys must expire without the application sweeping
      them, or a busy deployment accumulates one key per identity per window
      forever.
    * **Fail-open or fail-closed, chosen deliberately.** When the store is
      unreachable, either every request is allowed (availability) or every one
      is refused (safety). Both are defensible; silently doing one while the
      operator assumes the other is not. :attr:`fail_open` states which.
    * **A clock the store owns.** Window boundaries computed from each
      application instance's clock drift apart; the store's own time does not.

    None of this is implemented. Writing it without a Redis to test the
    atomicity against would produce something that looks right and races.
    """

    @property
    def fail_open(self) -> bool:
        """Whether an unreachable store allows requests or refuses them."""
        ...


class InMemoryBackend:
    """Per-process counters, pruned lazily.

    Pruning on write rather than on a timer keeps this free of a background
    thread; the cost is that a key never hit again lingers until the next sweep,
    which is bounded because the sweep runs whenever the map grows past a
    threshold.
    """

    #: Sweep expired windows once the map exceeds this many keys.
    _SWEEP_AT = 4096

    def __init__(self) -> None:
        self._counts: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    @property
    def is_shared(self) -> bool:
        """Never. These counters live in one process's memory."""

        return False

    def hit(self, key: str, window_seconds: int, now: float) -> int:
        window = int(now // window_seconds)
        with self._lock:
            if len(self._counts) > self._SWEEP_AT:
                self._counts = {k: v for k, v in self._counts.items() if v[0] >= window - 1}
            stored_window, count = self._counts.get(key, (window, 0))
            count = count + 1 if stored_window == window else 1
            self._counts[key] = (window, count)
            return count

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


class RateLimiter:
    """Applies policies against a backend."""

    def __init__(
        self,
        backend: RateLimitBackend | None = None,
        clock: Callable[[], float] | None = None,
        *,
        enabled: bool = True,
    ) -> None:
        self._backend = backend if backend is not None else InMemoryBackend()
        self._clock = clock if clock is not None else time.time
        self.enabled = enabled

    def check(self, policy: Policy, identity: str) -> None:
        """Record a hit for ``identity`` under ``policy``, or refuse.

        ``identity`` is whatever names the actor: a user id, an organization id,
        or a client address when there is no authenticated principal yet.
        """

        if not self.enabled:
            return
        now = self._clock()
        count = self._backend.hit(f"{policy.name}:{identity}", policy.window_seconds, now)
        if count > policy.limit:
            elapsed = now % policy.window_seconds
            raise RateLimitError(policy.name, max(1, int(policy.window_seconds - elapsed)))

    def reset(self) -> None:
        self._backend.reset()


#: The policies this application enforces.
#:
#: Each is named for the abuse it prevents rather than for the endpoint it sits
#: on, because several endpoints can share one budget — every broker token
#: exchange draws on ``broker_auth`` regardless of which venue it targets.
POLICIES: dict[str, Policy] = {
    # Credential guessing. Deliberately the tightest: a human logging in does
    # not need ten attempts a minute, and an attacker does.
    "login": Policy("login", limit=10, window_seconds=60),
    "register": Policy("register", limit=5, window_seconds=3600),
    # Ingestion is expensive in CPU and disk, and fetching reaches the network.
    "ingest": Policy("ingest", limit=30, window_seconds=60),
    "fetch": Policy("fetch", limit=10, window_seconds=60),
    # A backtest occupies a worker for as long as the dataset is large.
    "backtest": Policy("backtest", limit=60, window_seconds=60),
    # Token exchange hits the venue, which has its own limits we must not trip.
    "broker_auth": Policy("broker_auth", limit=10, window_seconds=300),
    # Marketplace writes are cheap but create durable records.
    "marketplace": Policy("marketplace", limit=60, window_seconds=60),
    # Starting a session spawns a thread.
    "session": Policy("session", limit=30, window_seconds=60),
}

_LIMITER: RateLimiter | None = None
_LIMITER_LOCK = threading.Lock()


def get_limiter() -> RateLimiter:
    """The process-wide limiter, built from settings on first use."""

    global _LIMITER
    if _LIMITER is None:
        with _LIMITER_LOCK:
            if _LIMITER is None:
                from iluvtrade.config import get_settings

                _LIMITER = RateLimiter(enabled=get_settings().rate_limit_enabled)
    return _LIMITER


def reset_limiter() -> None:
    """Drop the cached limiter. Tests use this between cases."""

    global _LIMITER
    with _LIMITER_LOCK:
        _LIMITER = None


@dataclass(frozen=True, slots=True)
class Enforcement:
    """What this process actually enforces, as opposed to what it configured."""

    enabled: bool
    shared: bool
    backend: str
    caveat: str

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "shared_across_instances": self.shared,
            "backend": self.backend,
            "caveat": self.caveat,
        }


def describe_enforcement(limiter: RateLimiter | None = None) -> Enforcement:
    """Report the limiter's real scope, for a health check or a deployment doc.

    Exists so "we rate limit logins to 10 a minute" cannot be believed without
    also knowing it is 10 a minute *per process*.
    """

    limiter = limiter or get_limiter()
    backend = limiter._backend
    shared = bool(getattr(backend, "is_shared", False))
    return Enforcement(
        enabled=limiter.enabled,
        shared=shared,
        backend=type(backend).__name__,
        caveat=(
            ""
            if shared
            else (
                "Counters are per-process. With N API workers the effective limit is "
                "N x the configured limit. A multi-instance deployment needs a shared "
                "backend; none is implemented."
            )
        ),
    )
